from __future__ import annotations

from collections import OrderedDict
import hashlib
from http.client import IncompleteRead
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, TextIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .prompting import build_prompts
from .util import CommandResult, ensure_dir

_REPAIR_ERROR_MEMORY_LIMIT = 6
_LEAN_LOCATION_PREFIX_RE = re.compile(r"^(?:[A-Za-z]:)?[^:\s]*\.lean:\d+:\d+:\s*")
_WHITESPACE_RE = re.compile(r"\s+")
_EVAL_GRADES = {"A", "B", "C", "D"}
_EVAL_RETRY_RESPONSE_CHARS = 4000
_EVAL_GRADE_ORDER = {"A": 4, "B": 3, "C": 2, "D": 1}
_LEAN_MODULE_PART_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*$")
_CODEX_EXEC_CODING_MODEL = "gpt-5.3-codex-spark"
_CODEX_EXEC_CODING_FALLBACK_MODEL = "gpt-5.3-codex"
_CODEX_EXEC_CODING_REASONING_EFFORT = "xhigh"
_OPENROUTER_GEMINI_FLASH_PREVIEW_MODEL = "google/gemini-3-flash-preview"
_GEMINI_DOUBLE_CHECK_SECONDARY_EVAL_MODEL = "openai/gpt-5.2"
_GEMINI_DOUBLE_CHECK_SECONDARY_EVAL_REASONING_EFFORT = "xhigh"


@dataclass(frozen=True)
class RunConfig:
    input_dir: Path
    output_dir: Path
    logs_dir: Path
    max_iters: int = 6
    formalization_only: bool = True
    require_no_sorry: bool = False
    openrouter_model: str = "openai/gpt-5.2-codex"
    openrouter_thinking_model: Optional[str] = None
    openrouter_eval_model: str = "openai/gpt-5.2"
    openrouter_coding_reasoning_effort: str = "xhigh"
    openrouter_thinking_reasoning_effort: str = "xhigh"
    openrouter_eval_reasoning_effort: str = "xhigh"
    openrouter_eval_repair_retries: int = 2
    min_eval_grade: Optional[str] = "B"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_api_key_env: str = "PRINCIPIA_KEY"
    openrouter_timeout_s: int = 180
    openrouter_max_retries: int = 2
    openrouter_web_search: bool = False
    openrouter_web_search_engine: Optional[str] = None
    openrouter_web_search_max_results: Optional[int] = None
    use_codex_exec: bool = False
    codex_exec_sandbox: str = "read-only"  # read-only|workspace-write|danger-full-access
    use_claude_cli: bool = False
    claude_cli_cmd: str = ""  # e.g. "/path/to/mathcode -p"
    live_logs: bool = False
    compile_cmd: str = "lake env lean {file}"
    cwd: Optional[Path] = None  # where to run compiler (defaults to repo root)

    def compile_argv(self, lean_file: Path) -> list[str]:
        cmd = self.compile_cmd.replace("{file}", str(lean_file.resolve()))
        return shlex.split(cmd)


@dataclass
class IterationRecord:
    iter_no: int
    thinking: CommandResult
    coding: CommandResult
    compiler: CommandResult
    lean_path: Path


def _is_gemini_flash_preview_model(model: str) -> bool:
    return model.strip().lower() == _OPENROUTER_GEMINI_FLASH_PREVIEW_MODEL


def _run(
    argv: list[str],
    *,
    cwd: Path,
    stdin_text: Optional[str] = None,
    live: bool = False,
    stdout_sink: Optional[TextIO] = None,
    stderr_sink: Optional[TextIO] = None,
) -> CommandResult:
    if not live:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            input=stdin_text,
            text=True,
            capture_output=True,
            check=False,
        )
        return CommandResult(
            argv=argv, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
        )

    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    if proc.stdin is not None:
        if stdin_text is not None:
            proc.stdin.write(stdin_text)
        proc.stdin.close()

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def _reader(stream, chunks: list[str], sink: Optional[TextIO]) -> None:
        if stream is None:
            return
        for line in stream:
            chunks.append(line)
            if sink is not None:
                sink.write(line)
                sink.flush()
        stream.close()

    t_out = threading.Thread(target=_reader, args=(proc.stdout, stdout_chunks, stdout_sink))
    t_err = threading.Thread(target=_reader, args=(proc.stderr, stderr_chunks, stderr_sink))
    t_out.start()
    t_err.start()
    returncode = proc.wait()
    t_out.join()
    t_err.join()

    return CommandResult(
        argv=argv,
        returncode=returncode,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
    )


def _call_openrouter_chat(
    *,
    prompt: str,
    model: str,
    base_url: str,
    api_key_env: str,
    timeout_s: int,
    max_retries: int,
    reasoning_effort: Optional[str] = None,
    openrouter_web_search: bool = False,
    openrouter_web_search_engine: Optional[str] = None,
    openrouter_web_search_max_results: Optional[int] = None,
) -> CommandResult:
    api_key = _resolve_openrouter_api_key(api_key_env)
    endpoint = base_url.rstrip("/") + "/chat/completions"
    argv = ["POST", endpoint]

    if not api_key:
        return CommandResult(
            argv=argv,
            returncode=1,
            stdout="",
            stderr=(
                f"Missing OpenRouter API key. Set env var '{api_key_env}' or add "
                f"'{api_key_env}=...' to ~/.zshrc."
            ),
        )

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}
    if openrouter_web_search:
        web_plugin: dict[str, object] = {"id": "web"}
        if openrouter_web_search_engine:
            web_plugin["engine"] = openrouter_web_search_engine
        if openrouter_web_search_max_results is not None:
            web_plugin["max_results"] = openrouter_web_search_max_results
        payload["plugins"] = [web_plugin]
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Optional OpenRouter attribution headers.
    http_referer = os.environ.get("OPENROUTER_HTTP_REFERER")
    if http_referer:
        headers["HTTP-Referer"] = http_referer
    app_title = os.environ.get("OPENROUTER_APP_TITLE")
    if app_title:
        headers["X-Title"] = app_title

    attempts = max(0, max_retries) + 1
    last_err = "OpenRouter request failed."
    last_stdout = ""

    for attempt in range(attempts):
        req = Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=timeout_s) as resp:
                try:
                    raw_bytes = resp.read()
                except IncompleteRead as exc:
                    partial_text = _decode_incomplete_read_partial(exc)
                    # If the partial payload is complete JSON, accept it.
                    if partial_text:
                        try:
                            json.loads(partial_text)
                            return CommandResult(
                                argv=argv, returncode=0, stdout=partial_text, stderr=""
                            )
                        except json.JSONDecodeError:
                            pass

                    last_stdout = partial_text
                    last_err = "OpenRouter response ended early (IncompleteRead) and payload was incomplete JSON."
                    if attempt + 1 < attempts:
                        _backoff_sleep(attempt)
                        continue
                    return CommandResult(
                        argv=argv, returncode=1, stdout=last_stdout, stderr=last_err
                    )

                raw = raw_bytes.decode("utf-8", errors="replace")
                return CommandResult(argv=argv, returncode=0, stdout=raw, stderr="")
        except HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            last_stdout = err_body
            last_err = f"OpenRouter HTTP {exc.code}: {exc.reason}"
            if exc.code in {408, 409, 425, 429, 500, 502, 503, 504} and attempt + 1 < attempts:
                _backoff_sleep(attempt)
                continue
            return CommandResult(argv=argv, returncode=1, stdout=err_body, stderr=last_err)
        except URLError as exc:
            last_err = f"OpenRouter request failed: {exc.reason}"
            if attempt + 1 < attempts:
                _backoff_sleep(attempt)
                continue
            return CommandResult(argv=argv, returncode=1, stdout=last_stdout, stderr=last_err)
        except IncompleteRead as exc:
            partial_text = _decode_incomplete_read_partial(exc)
            last_stdout = partial_text
            last_err = "OpenRouter response ended early (IncompleteRead)."
            if attempt + 1 < attempts:
                _backoff_sleep(attempt)
                continue
            return CommandResult(argv=argv, returncode=1, stdout=last_stdout, stderr=last_err)
        except OSError as exc:
            last_err = f"OpenRouter request failed: {exc}"
            if attempt + 1 < attempts:
                _backoff_sleep(attempt)
                continue
            return CommandResult(argv=argv, returncode=1, stdout=last_stdout, stderr=last_err)

    return CommandResult(argv=argv, returncode=1, stdout=last_stdout, stderr=last_err)


def _call_codex_exec(
    *,
    prompt: str,
    out_message_path: Path,
    model: Optional[str],
    reasoning_effort: Optional[str],
    sandbox: str,
    workdir: Path,
    live_logs: bool = False,
    stdout_sink: Optional[TextIO] = None,
    stderr_sink: Optional[TextIO] = None,
) -> CommandResult:
    ensure_dir(out_message_path.parent)
    normalized_model = _normalize_codex_model_name(model)

    def _build_argv(target_model: Optional[str]) -> list[str]:
        argv = ["codex", "exec"]
        if target_model:
            argv += ["--model", target_model]
        if reasoning_effort:
            argv += ["-c", f"model_reasoning_effort={json.dumps(reasoning_effort)}"]
        argv += [
            "--color",
            "never",
            "--skip-git-repo-check",
            "--sandbox",
            sandbox,
            "--output-last-message",
            str(out_message_path),
            "-",
        ]
        return argv

    argv = _build_argv(normalized_model)
    codex_res = _run(
        argv,
        cwd=workdir,
        stdin_text=prompt,
        live=live_logs,
        stdout_sink=stdout_sink,
        stderr_sink=stderr_sink,
    )
    # If spark is unavailable for this account, transparently fallback once.
    if (
        codex_res.returncode != 0
        and normalized_model == _CODEX_EXEC_CODING_MODEL
        and _is_codex_model_not_found(codex_res.stderr)
    ):
        argv = _build_argv(_CODEX_EXEC_CODING_FALLBACK_MODEL)
        codex_res = _run(
            argv,
            cwd=workdir,
            stdin_text=prompt,
            live=live_logs,
            stdout_sink=stdout_sink,
            stderr_sink=stderr_sink,
        )
    if codex_res.returncode != 0:
        return codex_res

    try:
        message = out_message_path.read_text(encoding="utf-8")
    except OSError as exc:
        return CommandResult(
            argv=argv,
            returncode=1,
            stdout=codex_res.stdout,
            stderr=(
                "codex exec succeeded but --output-last-message was unreadable: "
                f"{out_message_path}: {exc}"
            ),
        )

    return CommandResult(
        argv=argv,
        returncode=0,
        stdout=message,
        stderr=codex_res.stderr,
    )


def _call_claude_cli(
    *,
    prompt: str,
    claude_cli_cmd: str,
    workdir: Path,
    timeout_s: int = 300,
) -> CommandResult:
    """Run the Claude CLI in headless (-p) mode and capture the response."""
    argv = shlex.split(claude_cli_cmd)
    # Ensure -p flag is present for headless/print mode.
    if "-p" not in argv and "--print" not in argv:
        argv.append("-p")

    try:
        proc = subprocess.run(
            argv,
            input=prompt,
            text=True,
            capture_output=True,
            cwd=str(workdir),
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            argv=argv,
            returncode=1,
            stdout="",
            stderr=f"Claude CLI timed out after {timeout_s}s.",
        )
    except FileNotFoundError:
        return CommandResult(
            argv=argv,
            returncode=1,
            stdout="",
            stderr=f"Claude CLI command not found: {argv[0]}",
        )

    return CommandResult(
        argv=argv,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _decode_incomplete_read_partial(exc: IncompleteRead) -> str:
    partial = exc.partial
    if isinstance(partial, bytes):
        return partial.decode("utf-8", errors="replace")
    if isinstance(partial, str):
        return partial
    return ""


def _normalize_codex_model_name(model: Optional[str]) -> Optional[str]:
    if model is None:
        return None
    normalized = model.strip()
    if not normalized:
        return None
    if normalized.lower().startswith("openai/"):
        _, suffix = normalized.split("/", 1)
        normalized = suffix.strip()
    return normalized or None


def _is_codex_model_not_found(stderr_text: str) -> bool:
    lowered = stderr_text.lower()
    return "model_not_found" in lowered or "does not exist" in lowered


def _backoff_sleep(attempt: int) -> None:
    # Short exponential backoff for transient transport failures.
    delay_s = min(4.0, 0.5 * (2**attempt))
    time.sleep(delay_s)


def _parse_shell_assignment(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].strip()
    if "=" not in stripped:
        return None

    name, raw_value = stripped.split("=", 1)
    name = name.strip()
    if not name:
        return None

    value = raw_value.strip()
    if not value:
        return None

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    else:
        value = value.split("#", 1)[0].strip()

    if not value:
        return None
    return name, value


def _read_var_from_zshrc(var_name: str, *, zshrc_path: Optional[Path] = None) -> Optional[str]:
    path = zshrc_path or (Path.home() / ".zshrc")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    # Use the last assignment in file order to match shell semantics.
    for line in reversed(lines):
        parsed = _parse_shell_assignment(line)
        if parsed is None:
            continue
        name, value = parsed
        if name == var_name:
            return value
    return None


def _resolve_openrouter_api_key(var_name: str) -> Optional[str]:
    env_value = os.environ.get(var_name)
    if env_value:
        return env_value
    return _read_var_from_zshrc(var_name)


def _extract_openrouter_message_content(response_obj: dict) -> str:
    choices = response_obj.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("OpenRouter response missing choices.")

    first = choices[0]
    if not isinstance(first, dict):
        raise ValueError("OpenRouter response contains invalid choice payload.")

    message = first.get("message")
    if not isinstance(message, dict):
        raise ValueError("OpenRouter response missing message object.")

    content = message.get("content")
    if isinstance(content, str):
        if content.strip():
            return content
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        merged = "".join(parts).strip()
        if merged:
            return merged

    # Some OpenRouter providers can return empty `content` but include a textual
    # `reasoning` field. This commonly happens on max-output truncation.
    for candidate in (message.get("reasoning"), first.get("reasoning")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate

    for details in (message.get("reasoning_details"), first.get("reasoning_details")):
        if not isinstance(details, list):
            continue
        parts: list[str] = []
        for item in details:
            if not isinstance(item, dict):
                continue
            summary = item.get("summary")
            if isinstance(summary, str) and summary.strip():
                parts.append(summary.strip())
        if parts:
            return "\n\n".join(parts)
    raise ValueError("OpenRouter response message content is empty or not text.")


def _extract_model_response_text(response_text: str) -> str:
    stripped = response_text.strip()
    if not stripped:
        raise ValueError("Model response was empty.")

    # OpenRouter returns a chat-completions envelope; codex exec returns plain text.
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped

    if not isinstance(parsed, dict):
        return stripped

    try:
        content = _extract_openrouter_message_content(parsed).strip()
    except ValueError:
        # Treat non-envelope JSON as direct model text (e.g., strict JSON output).
        return stripped

    if not content:
        raise ValueError("Model response message content was empty.")
    return content


def _parse_json_object_from_model_text(text: str) -> dict:
    stripped = text.strip()
    if not stripped:
        raise ValueError("Model response was empty.")

    candidates = [stripped]
    if stripped.startswith("```"):
        chunks = stripped.split("```")
        if len(chunks) >= 3:
            candidates.append(chunks[1].removeprefix("json").strip())

    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        for idx, ch in enumerate(candidate):
            if ch != "{":
                continue
            try:
                parsed, _end = decoder.raw_decode(candidate[idx:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

    raise ValueError("Could not parse a JSON object from model response text.")


def _write_text(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, payload: dict) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=True, indent=2))


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _write_iteration_meta(
    *,
    logs_dir: Path,
    theorem_name: str,
    iter_no: int,
    thinking_prompt: str,
    coding_prompt: Optional[str],
    thinking_res: CommandResult,
    coding_res: Optional[CommandResult],
    compiler_res: Optional[CommandResult] = None,
    evaluation_prompt: Optional[str] = None,
    evaluation_res: Optional[CommandResult] = None,
    evaluation_payload: Optional[dict[str, object]] = None,
) -> None:
    meta: dict[str, object] = {
        "iteration": iter_no,
        "thinking_prompt_sha256": _prompt_hash(thinking_prompt),
        "thinking_prompt_chars": len(thinking_prompt),
        "thinking": {"argv": thinking_res.argv, "returncode": thinking_res.returncode},
    }
    if coding_prompt is not None:
        meta["coding_prompt_sha256"] = _prompt_hash(coding_prompt)
        meta["coding_prompt_chars"] = len(coding_prompt)
    if coding_res is not None:
        meta["coding"] = {"argv": coding_res.argv, "returncode": coding_res.returncode}
    if compiler_res is not None:
        meta["compiler"] = {"argv": compiler_res.argv, "returncode": compiler_res.returncode}
    if evaluation_prompt is not None:
        meta["evaluation_prompt_sha256"] = _prompt_hash(evaluation_prompt)
        meta["evaluation_prompt_chars"] = len(evaluation_prompt)
    if evaluation_res is not None:
        meta["evaluation"] = {"argv": evaluation_res.argv, "returncode": evaluation_res.returncode}
    if evaluation_payload is not None:
        meta["evaluation_payload"] = evaluation_payload
    _write_json(logs_dir / f"{theorem_name}.iter{iter_no}.meta.json", meta)


def _write_compiler_logs(
    logs_dir: Path, theorem_name: str, iter_no: int, compiler_res: CommandResult
) -> None:
    _write_text(logs_dir / f"{theorem_name}.iter{iter_no}.compile_stdout.log", compiler_res.stdout)
    _write_text(logs_dir / f"{theorem_name}.iter{iter_no}.compile_stderr.log", compiler_res.stderr)


def _extract_compact_error_lines(compiler_res: CommandResult) -> list[str]:
    combined = (compiler_res.stdout + "\n" + compiler_res.stderr).strip()
    if not combined:
        return []

    lines: list[str] = []
    for raw in combined.splitlines():
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        if (
            "error" in lowered
            or "parse failure" in lowered
            or "policy failure" in lowered
            or "failed before producing lean output" in lowered
        ):
            lines.append(line)

    if lines:
        return lines

    for raw in combined.splitlines():
        line = raw.strip()
        if line:
            return [line]
    return []


def _normalize_error_line(line: str) -> str:
    normalized = _LEAN_LOCATION_PREFIX_RE.sub("", line.strip())
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized


def _update_error_memory(
    memory: OrderedDict[str, tuple[str, int, int]],
    compiler_res: CommandResult,
    *,
    iter_no: int,
) -> None:
    for line in _extract_compact_error_lines(compiler_res):
        key = _normalize_error_line(line)
        if not key:
            continue
        if key in memory:
            _last_line, count, _last_iter = memory[key]
            memory[key] = (key, count + 1, iter_no)
            memory.move_to_end(key)
        else:
            memory[key] = (key, 1, iter_no)


def _format_error_memory(memory: OrderedDict[str, tuple[str, int, int]], *, limit: int) -> str:
    if limit <= 0 or not memory:
        return ""

    recent_items = list(memory.items())[-limit:]
    lines: list[str] = []
    for idx, (_key, (display, count, last_iter)) in enumerate(reversed(recent_items), start=1):
        if count > 1:
            lines.append(f"{idx}. [seen {count}x, last iter {last_iter}] {display}")
        else:
            lines.append(f"{idx}. [iter {last_iter}] {display}")
    return "\n".join(lines)


def _build_formalization_eval_prompt(
    *,
    problem_json: dict,
    theorem_name: str,
    lean_code: str,
) -> str:
    json_blob = json.dumps(problem_json, ensure_ascii=False, indent=2)
    return f"""You are evaluating semantic fidelity of a Lean formalization against its original math problem.

Important scope:
- Evaluate ONLY the theorem statement semantics (not proof quality, style, or elegance).
- Compare the original problem requirements to the Lean theorem proposition.

Required comparison checklist (must be applied explicitly before grading):
1) Core mathematical objects and domains/types match (e.g., Set/Real/Metric/Measure).
2) Quantifier structure matches (forall/exists order and scope).
3) Hypotheses/assumptions match (none dropped or materially weakened).
4) Conclusion/claim matches (same relation/equality/inequality/content).
5) Multi-part coverage matches (if the original has multiple sub-questions, all parts are represented).

Hard grading rules (must follow exactly):
- Assign exactly one grade using this top-down decision order (stop at first match):
  1) D if theorem is trivialized/vacuous/unrelated (e.g., `True`/`False` shell) or has major semantic drift.
  2) C if any major obligation is missing/wrong/weakened (including missing any sub-question).
  3) B if all major obligations are present and correct, with at most minor wording/precision issues.
  4) A only if all checklist items pass with no material weakening.
- Never assign A or B when any major obligation is missing, wrong, or weakened.
- If uncertain between two grades, choose the lower grade.

Grading rubric:
- A: Fully faithful. Core objects/quantifiers/claims are preserved with no material weakening.
- B: Mostly faithful. Minor omissions or slight imprecision, but core meaning preserved.
- C: Partially faithful. Significant mismatch, missing subparts, or notable weakening.
- D: Not faithful. Major semantic drift, trivialization, or largely unrelated statement.

Return ONLY a JSON object:
{{
  "grade": "A|B|C|D",
  "summary": "<1-3 sentence verdict>",
  "distance_from_original": "<brief description of gaps>",
  "key_mismatches": ["<concrete mismatch>", "<concrete mismatch>"]
}}

Output-format hard constraints:
- Must be strict RFC8259 JSON (no markdown/code fences, no extra text).
- Do not use LaTeX delimiters like \\( ... \\) or \\[ ... \\].
- Avoid backslashes in field values unless required by JSON escaping.
- Prefer plain natural language like "(E)" instead of LaTeX-style escaped forms.

Original problem JSON (authoritative):
{json_blob}

Lean theorem target name: {theorem_name}

Lean file content:
```lean
{lean_code}
```"""


def _build_eval_retry_prompt(
    *,
    base_prompt: str,
    failure_reason: str,
    previous_response_text: str,
    retry_no: int,
) -> str:
    snippet = previous_response_text.strip()
    if len(snippet) > _EVAL_RETRY_RESPONSE_CHARS:
        snippet = snippet[:_EVAL_RETRY_RESPONSE_CHARS] + "\n...[truncated]"
    return (
        base_prompt
        + "\n\nThe previous evaluation response could not be accepted.\n"
        + f"Failure reason: {failure_reason}\n"
        + f"Retry number: {retry_no}\n"
        + "Please regenerate and return ONLY valid JSON that matches the required schema.\n"
        + "Do not include markdown/code fences/explanations.\n"
        + "Avoid LaTeX escapes and avoid backslashes in values.\n\n"
        + "Previous invalid response (for debugging):\n"
        + snippet
    )


def _to_str_list(value: object, *, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _parse_formalization_eval_payload(payload: dict) -> dict[str, object]:
    raw_grade = payload.get("grade")
    if not isinstance(raw_grade, str):
        raise ValueError("Evaluation output missing 'grade' field.")
    grade = raw_grade.strip().upper()
    if grade not in _EVAL_GRADES:
        raise ValueError("Evaluation grade must be one of A/B/C/D.")

    summary = ""
    for key in ("summary", "verdict", "reasoning"):
        candidate = payload.get(key)
        if isinstance(candidate, str) and candidate.strip():
            summary = candidate.strip()
            break

    distance = ""
    for key in ("distance_from_original", "distance", "distance_summary"):
        candidate = payload.get(key)
        if isinstance(candidate, str) and candidate.strip():
            distance = candidate.strip()
            break

    mismatches: list[str] = []
    for key in ("key_mismatches", "mismatches", "gap_items"):
        items = _to_str_list(payload.get(key))
        if items:
            mismatches = items
            break

    normalized: dict[str, object] = {"grade": grade}
    if summary:
        normalized["summary"] = summary
    if distance:
        normalized["distance_from_original"] = distance
    if mismatches:
        normalized["key_mismatches"] = mismatches
    return normalized


def _format_eval_failure_reason(exc: Exception) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return f"{exc.msg} (line {exc.lineno}, column {exc.colno}, char {exc.pos})"
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _format_eval_feedback_for_repair(eval_payload: dict[str, object]) -> str:
    def _extract_parts(payload: dict[str, object]) -> list[str]:
        parts: list[str] = []
        summary_obj = payload.get("summary")
        if isinstance(summary_obj, str) and summary_obj.strip():
            parts.append(f"summary={summary_obj.strip()}")

        distance_obj = payload.get("distance_from_original")
        if isinstance(distance_obj, str) and distance_obj.strip():
            parts.append(f"distance_from_original={distance_obj.strip()}")

        mismatches_obj = payload.get("key_mismatches")
        if isinstance(mismatches_obj, list):
            clean_items = [
                str(x).strip() for x in mismatches_obj if isinstance(x, str) and str(x).strip()
            ]
            if clean_items:
                parts.append("key_mismatches=" + "; ".join(clean_items[:8]))
        return parts

    def _grade_rank(payload: dict[str, object]) -> int:
        grade_obj = payload.get("grade")
        if not isinstance(grade_obj, str):
            return 999
        grade = grade_obj.strip().upper()
        if grade not in _EVAL_GRADES:
            return 999
        # Lower grade rank should win for repair targeting: D(1) is more urgent than A(4).
        return _EVAL_GRADE_ORDER.get(grade, 999)

    candidates: list[tuple[str, dict[str, object]]] = [("primary", eval_payload)]
    double_check_obj = eval_payload.get("double_check")
    if isinstance(double_check_obj, dict):
        primary_obj = double_check_obj.get("primary")
        if isinstance(primary_obj, dict):
            candidates.append(("double_check:primary", primary_obj))
        secondary_obj = double_check_obj.get("secondary")
        if isinstance(secondary_obj, dict):
            candidates.append(("double_check:secondary", secondary_obj))

    best_label = "primary"
    best_parts: list[str] = []
    best_rank = 999
    best_detail_score = -1
    for label, payload in candidates:
        parts = _extract_parts(payload)
        rank = _grade_rank(payload)
        # Prefer lower grades first; on ties, prefer richer mismatch/detail payloads.
        detail_score = 0
        if parts:
            detail_score = sum(
                1 for part in parts if part.startswith("key_mismatches=")
            ) * 10 + len(parts)
        if (rank < best_rank) or (rank == best_rank and detail_score > best_detail_score):
            best_label = label
            best_parts = parts
            best_rank = rank
            best_detail_score = detail_score

    if not best_parts:
        return ""

    if best_label == "primary":
        return "Evaluator feedback: " + " | ".join(best_parts)
    return f"Evaluator feedback ({best_label}): " + " | ".join(best_parts)


def _extract_top_level_prop_from_theorem_header(header: str) -> Optional[str]:
    depth = 0
    last_colon = -1
    for i, ch in enumerate(header):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == ":" and depth == 0:
            if i + 1 < len(header) and header[i + 1] == "=":
                continue
            last_colon = i
    if last_colon < 0:
        return None
    return header[last_colon + 1 :].strip()


def _detect_trivialized_main_theorem_statement(
    lean_code: str, *, theorem_name: str
) -> Optional[str]:
    start_re = re.compile(rf"\b(?:theorem|lemma)\s+{re.escape(theorem_name)}\b")
    m = start_re.search(lean_code)
    if m is None:
        return None
    end = lean_code.find(":=", m.end())
    if end < 0:
        return None
    header = lean_code[m.start() : end]
    prop = _extract_top_level_prop_from_theorem_header(header)
    if not prop:
        return None
    match = re.match(r"^\(?\s*(True|False)\b", prop)
    if match is None:
        return None
    return match.group(1)


def _grade_below_threshold(grade: str, min_grade: str) -> bool:
    g = _EVAL_GRADE_ORDER.get(grade.upper(), 0)
    t = _EVAL_GRADE_ORDER.get(min_grade.upper(), 0)
    return g < t


def _module_name_from_lean_path(lean_path: Path, *, run_cwd: Path) -> Optional[str]:
    try:
        rel = lean_path.resolve().relative_to(run_cwd.resolve())
    except ValueError:
        return None
    if rel.suffix != ".lean":
        return None
    parts = rel.with_suffix("").parts
    if not parts:
        return None
    for part in parts:
        if not _LEAN_MODULE_PART_RE.fullmatch(part):
            return None
    return ".".join(parts)


def _inject_imports(lean_code: str, module_names: list[str]) -> str:
    if not module_names:
        return lean_code

    ordered_modules: list[str] = []
    seen: set[str] = set()
    for module in module_names:
        module = module.strip()
        if not module or module in seen:
            continue
        seen.add(module)
        ordered_modules.append(module)
    if not ordered_modules:
        return lean_code

    lines = lean_code.splitlines()
    existing_imports: set[str] = set()
    insert_at = 0

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            insert_at = idx + 1
            continue
        if stripped.startswith("--"):
            insert_at = idx + 1
            continue
        if stripped.startswith("import "):
            module = stripped[len("import ") :].strip()
            if module:
                existing_imports.add(module)
            insert_at = idx + 1
            continue
        break

    missing_import_lines = [
        f"import {module}" for module in ordered_modules if module not in existing_imports
    ]
    if not missing_import_lines:
        return lean_code

    merged_lines = lines[:insert_at] + missing_import_lines + lines[insert_at:]
    merged = "\n".join(merged_lines)
    if lean_code.endswith("\n"):
        return merged + "\n"
    return merged


def process_problem_file(
    cfg: RunConfig,
    json_path: Path,
    *,
    repo_root: Path,
    prior_json_paths: Optional[list[Path]] = None,
    override_min_eval_grade: Optional[str] = None,
) -> tuple[bool, list[IterationRecord]]:
    problem_json = json.loads(json_path.read_text(encoding="utf-8"))

    ensure_dir(cfg.output_dir)
    ensure_dir(cfg.logs_dir)

    run_cwd = cfg.cwd or repo_root
    prior_json_paths = list(prior_json_paths or [])
    prior_problem_jsons: list[dict] = []
    prior_formalizations: list[tuple[str, str]] = []
    prior_module_imports: list[str] = []

    for prior_json_path in prior_json_paths:
        prior_problem_json = json.loads(prior_json_path.read_text(encoding="utf-8"))
        prior_problem_jsons.append(prior_problem_json)

        prior_prompts = build_prompts(
            prior_problem_json,
            out_dir=cfg.output_dir,
            name_hint=prior_json_path.stem,
            formalization_only=cfg.formalization_only,
        )
        try:
            prior_lean_code = prior_prompts.lean_path.read_text(encoding="utf-8")
        except OSError:
            continue
        prior_formalizations.append((prior_prompts.theorem_name, prior_lean_code))

        module_name = _module_name_from_lean_path(prior_prompts.lean_path, run_cwd=run_cwd)
        if module_name:
            prior_module_imports.append(module_name)

    prompts = build_prompts(
        problem_json,
        out_dir=cfg.output_dir,
        name_hint=json_path.stem,
        formalization_only=cfg.formalization_only,
        prior_subproblems=prior_problem_jsons,
        prior_formalizations=prior_formalizations,
    )

    effective_min_eval_grade = (
        override_min_eval_grade if override_min_eval_grade is not None else cfg.min_eval_grade
    )
    prev_lean = ""
    initial_thinking_notes = ""
    records: list[IterationRecord] = []
    error_memory: OrderedDict[str, tuple[str, int, int]] = OrderedDict()
    openrouter_web_search_kwargs = {}
    if cfg.openrouter_web_search:
        openrouter_web_search_kwargs = {
            "openrouter_web_search": True,
            "openrouter_web_search_engine": cfg.openrouter_web_search_engine,
            "openrouter_web_search_max_results": cfg.openrouter_web_search_max_results,
        }

    def _call_model(
        *,
        prompt: str,
        model: str,
        reasoning_effort: Optional[str],
        stage: str,
        iter_no: int,
        attempt_no: Optional[int] = None,
        enable_web_search: bool = False,
    ) -> CommandResult:
        codex_exec_model = model
        codex_exec_reasoning_effort = reasoning_effort
        if stage == "coding":
            codex_exec_model = _CODEX_EXEC_CODING_MODEL
            codex_exec_reasoning_effort = _CODEX_EXEC_CODING_REASONING_EFFORT

        if cfg.use_claude_cli:
            return _call_claude_cli(
                prompt=prompt,
                claude_cli_cmd=cfg.claude_cli_cmd,
                workdir=repo_root,
                timeout_s=cfg.openrouter_timeout_s,
            )

        if cfg.use_codex_exec:
            suffix = f"{prompts.theorem_name}.iter{iter_no}.{stage}"
            if attempt_no is not None:
                suffix += f"_attempt{attempt_no}"
            out_message_path = cfg.logs_dir / f"{suffix}.codex_last_message.log"
            return _call_codex_exec(
                prompt=prompt,
                out_message_path=out_message_path,
                model=codex_exec_model,
                reasoning_effort=codex_exec_reasoning_effort,
                sandbox=cfg.codex_exec_sandbox,
                workdir=repo_root,
                live_logs=False,
                stdout_sink=None,
                stderr_sink=None,
            )

        request_kwargs = openrouter_web_search_kwargs if enable_web_search else {}
        return _call_openrouter_chat(
            prompt=prompt,
            model=model,
            base_url=cfg.openrouter_base_url,
            api_key_env=cfg.openrouter_api_key_env,
            timeout_s=cfg.openrouter_timeout_s,
            max_retries=cfg.openrouter_max_retries,
            reasoning_effort=reasoning_effort,
            **request_kwargs,
        )

    def _finalize_iteration(
        *,
        iter_no: int,
        thinking_prompt: str,
        coding_prompt: str,
        thinking_res: CommandResult,
        coding_res: CommandResult,
        compiler_res: CommandResult,
        evaluation_prompt: Optional[str] = None,
        evaluation_res: Optional[CommandResult] = None,
        evaluation_payload: Optional[dict[str, object]] = None,
    ) -> None:
        if compiler_res.returncode != 0:
            _update_error_memory(error_memory, compiler_res, iter_no=iter_no)

        records.append(
            IterationRecord(iter_no, thinking_res, coding_res, compiler_res, prompts.lean_path)
        )
        _write_iteration_meta(
            logs_dir=cfg.logs_dir,
            theorem_name=prompts.theorem_name,
            iter_no=iter_no,
            thinking_prompt=thinking_prompt,
            coding_prompt=coding_prompt,
            thinking_res=thinking_res,
            coding_res=coding_res,
            compiler_res=compiler_res,
            evaluation_prompt=evaluation_prompt,
            evaluation_res=evaluation_res,
            evaluation_payload=evaluation_payload,
        )

    def _progress(msg: str) -> None:
        print(f"[AUTOLEAN] {msg}", file=sys.stderr, flush=True)

    for it in range(1, cfg.max_iters + 1):
        _progress(f"=== Iteration {it}/{cfg.max_iters} ===")
        if it == 1:
            thinking_prompt = prompts.initial_thinking_prompt
            coding_base_prompt = prompts.initial_prompt
        else:
            compile_output = (
                records[-1].compiler.stdout + "\n" + records[-1].compiler.stderr
            ).strip()
            thinking_prompt = "Skipped: phase 5.2 thinking runs only on iteration 1."
            if prev_lean.strip():
                coding_base_prompt = prompts.repair_prompt_template.format(
                    prev_lean=prev_lean, compile_output=compile_output
                )
            else:
                coding_base_prompt = (
                    prompts.initial_prompt
                    + "\n\nPrevious attempt failed before producing a usable Lean file.\n"
                    + "Failure summary:\n"
                    + compile_output
                )

            compact_error_memory = _format_error_memory(
                error_memory, limit=_REPAIR_ERROR_MEMORY_LIMIT
            )
            if compact_error_memory:
                coding_base_prompt += (
                    "\n\nCompact repair memory (recent recurring failures):\n"
                    + compact_error_memory
                    + "\nAvoid reusing these known-failing API names, argument names, and syntax forms."
                )

        thinking_stdout_path = cfg.logs_dir / f"{prompts.theorem_name}.iter{it}.thinking_stdout.log"
        thinking_stderr_path = cfg.logs_dir / f"{prompts.theorem_name}.iter{it}.thinking_stderr.log"
        coding_stdout_path = cfg.logs_dir / f"{prompts.theorem_name}.iter{it}.coding_stdout.log"
        coding_stderr_path = cfg.logs_dir / f"{prompts.theorem_name}.iter{it}.coding_stderr.log"
        if it == 1:
            _progress("Phase 5.2: Running thinking/planning model...")
            thinking_model = cfg.openrouter_thinking_model or cfg.openrouter_model

            thinking_res = _call_model(
                prompt=thinking_prompt,
                model=thinking_model,
                reasoning_effort=cfg.openrouter_thinking_reasoning_effort,
                stage="thinking",
                iter_no=it,
                enable_web_search=True,
            )
            _write_text(thinking_stdout_path, thinking_res.stdout)
            _write_text(thinking_stderr_path, thinking_res.stderr)

            if thinking_res.returncode != 0:
                _progress("Phase 5.2: Thinking model FAILED, proceeding with direct implementation.")
                initial_thinking_notes = (
                    "Phase 5.2 thinking request failed on iteration 1. "
                    "Proceed with direct implementation and self-correction.\n"
                    f"Failure: {thinking_res.stderr.strip()}"
                )
            else:
                _progress("Phase 5.2: Thinking model completed OK.")
                try:
                    initial_thinking_notes = _extract_model_response_text(
                        thinking_res.stdout
                    ).strip()
                except ValueError as exc:
                    initial_thinking_notes = (
                        "Phase 5.2 thinking output was unparseable on iteration 1. "
                        "Proceed with direct implementation and self-correction.\n"
                        f"Parse error: {exc}"
                    )
                # Emit the thinking/planning notes so the TypeScript UI can show them
                print("[AUTOLEAN] [THINKING_START]", file=sys.stderr, flush=True)
                for _tl in initial_thinking_notes.splitlines():
                    print(f"[AUTOLEAN] {_tl}", file=sys.stderr, flush=True)
                print("[AUTOLEAN] [THINKING_END]", file=sys.stderr, flush=True)
        else:
            thinking_res = CommandResult(
                argv=["(skipped)"],
                returncode=0,
                stdout="Skipped phase 5.2 thinking on repair iteration; reused iteration-1 planning notes.",
                stderr="",
            )
            _write_text(thinking_stdout_path, thinking_res.stdout)
            _write_text(thinking_stderr_path, thinking_res.stderr)

        coding_prompt = (
            "You are in phase 5.3 (Codex implementation) of the pipeline.\n"
            "Use the phase 5.2 planning notes (from iteration 1) to implement Lean with strict syntax "
            "and to fix holes/coercions.\n\n"
            "Phase 5.2 planning notes (iteration 1):\n"
            f"{initial_thinking_notes}\n\n"
            "Phase 5.3 task:\n"
            f"{coding_base_prompt}"
        )

        _progress(f"Phase 5.3: Running coding model (iter {it})...")
        coding_res = _call_model(
            prompt=coding_prompt,
            model=cfg.openrouter_model,
            reasoning_effort=cfg.openrouter_coding_reasoning_effort,
            stage="coding",
            iter_no=it,
            enable_web_search=True,
        )
        _write_text(coding_stdout_path, coding_res.stdout)
        _write_text(coding_stderr_path, coding_res.stderr)

        if coding_res.returncode != 0:
            _progress(f"Phase 5.3: Coding model FAILED (iter {it}).")
            compiler_err = coding_res.stderr.strip() or "see logs."
            compiler_res = CommandResult(
                argv=[],
                returncode=1,
                stdout="",
                stderr=f"Coding stage failed before producing Lean output: {compiler_err}",
            )
            _write_compiler_logs(cfg.logs_dir, prompts.theorem_name, it, compiler_res)
            _finalize_iteration(
                iter_no=it,
                thinking_prompt=thinking_prompt,
                coding_prompt=coding_prompt,
                thinking_res=thinking_res,
                coding_res=coding_res,
                compiler_res=compiler_res,
            )
            continue

        try:
            model_text = _extract_model_response_text(coding_res.stdout)
            obj = _parse_json_object_from_model_text(model_text)
        except ValueError as exc:
            compiler_res = CommandResult(
                argv=[],
                returncode=1,
                stdout="",
                stderr=f"Coding output parse failure: {exc}",
            )
            _write_compiler_logs(cfg.logs_dir, prompts.theorem_name, it, compiler_res)
            _finalize_iteration(
                iter_no=it,
                thinking_prompt=thinking_prompt,
                coding_prompt=coding_prompt,
                thinking_res=thinking_res,
                coding_res=coding_res,
                compiler_res=compiler_res,
            )
            continue

        lean_code = obj.get("lean")
        if not isinstance(lean_code, str):
            compiler_res = CommandResult(
                argv=[],
                returncode=1,
                stdout="",
                stderr="Coding output missing 'lean' field.",
            )
            _write_compiler_logs(cfg.logs_dir, prompts.theorem_name, it, compiler_res)
            _finalize_iteration(
                iter_no=it,
                thinking_prompt=thinking_prompt,
                coding_prompt=coding_prompt,
                thinking_res=thinking_res,
                coding_res=coding_res,
                compiler_res=compiler_res,
            )
            continue

        lean_code = _inject_imports(lean_code, prior_module_imports)
        _write_text(prompts.lean_path, lean_code)
        _write_text(cfg.logs_dir / f"{prompts.theorem_name}.iter{it}.lean", lean_code)
        prev_lean = lean_code
        _progress(f"Phase 5.3: Lean code generated ({len(lean_code)} chars), checking policies...")
        # Emit the generated Lean code so the TypeScript UI can show it
        print("[AUTOLEAN] [LEAN_CODE_START]", file=sys.stderr, flush=True)
        for _ll in lean_code.splitlines():
            print(f"[AUTOLEAN] {_ll}", file=sys.stderr, flush=True)
        print("[AUTOLEAN] [LEAN_CODE_END]", file=sys.stderr, flush=True)

        trivial_token = _detect_trivialized_main_theorem_statement(
            lean_code,
            theorem_name=prompts.theorem_name,
        )
        if trivial_token is not None:
            compiler_res = CommandResult(
                argv=["(policy)"],
                returncode=1,
                stdout="",
                stderr=(
                    "Policy failure: theorem statement was trivialized as "
                    f"`{trivial_token}` (expected faithful formalization of the original problem)."
                ),
            )
            _write_compiler_logs(cfg.logs_dir, prompts.theorem_name, it, compiler_res)
            _finalize_iteration(
                iter_no=it,
                thinking_prompt=thinking_prompt,
                coding_prompt=coding_prompt,
                thinking_res=thinking_res,
                coding_res=coding_res,
                compiler_res=compiler_res,
            )
            continue

        if cfg.formalization_only and "sorry" not in lean_code:
            compiler_res = CommandResult(
                argv=["(policy)"],
                returncode=1,
                stdout="",
                stderr="Policy failure: formalization-only mode requires a placeholder proof (`sorry`).",
            )
            _write_compiler_logs(cfg.logs_dir, prompts.theorem_name, it, compiler_res)
            _finalize_iteration(
                iter_no=it,
                thinking_prompt=thinking_prompt,
                coding_prompt=coding_prompt,
                thinking_res=thinking_res,
                coding_res=coding_res,
                compiler_res=compiler_res,
            )
            continue

        if cfg.require_no_sorry and "sorry" in lean_code:
            compiler_res = CommandResult(
                argv=["(policy)"],
                returncode=1,
                stdout="",
                stderr="Policy failure: generated Lean contains 'sorry'.",
            )
            _write_compiler_logs(cfg.logs_dir, prompts.theorem_name, it, compiler_res)
            _finalize_iteration(
                iter_no=it,
                thinking_prompt=thinking_prompt,
                coding_prompt=coding_prompt,
                thinking_res=thinking_res,
                coding_res=coding_res,
                compiler_res=compiler_res,
            )
            continue

        _progress(f"Compiling Lean code (iter {it})...")
        comp_argv = cfg.compile_argv(prompts.lean_path)
        compiler_stdout_path = cfg.logs_dir / f"{prompts.theorem_name}.iter{it}.compile_stdout.log"
        compiler_stderr_path = cfg.logs_dir / f"{prompts.theorem_name}.iter{it}.compile_stderr.log"

        if cfg.live_logs:
            with (
                compiler_stdout_path.open("w", encoding="utf-8") as comp_out,
                compiler_stderr_path.open("w", encoding="utf-8") as comp_err,
            ):
                compiler_res = _run(
                    comp_argv,
                    cwd=run_cwd,
                    live=cfg.live_logs,
                    stdout_sink=comp_out,
                    stderr_sink=comp_err,
                )
        else:
            compiler_res = _run(comp_argv, cwd=run_cwd, live=cfg.live_logs)

        if not cfg.live_logs:
            _write_compiler_logs(cfg.logs_dir, prompts.theorem_name, it, compiler_res)

        if compiler_res.returncode == 0:
            _progress(f"Compilation PASSED (iter {it}).")
        else:
            _progress(f"Compilation FAILED (iter {it})")
            # Emit compiler errors so the TypeScript UI can show them
            print("[AUTOLEAN] [COMPILE_ERROR_START]", file=sys.stderr, flush=True)
            for _el in compiler_res.stderr.strip().splitlines()[:30]:
                print(f"[AUTOLEAN] {_el}", file=sys.stderr, flush=True)
            print("[AUTOLEAN] [COMPILE_ERROR_END]", file=sys.stderr, flush=True)

        eval_prompt: Optional[str] = None
        eval_res: Optional[CommandResult] = None
        eval_payload: Optional[dict[str, object]] = None
        if compiler_res.returncode == 0:
            base_eval_prompt = _build_formalization_eval_prompt(
                problem_json=problem_json,
                theorem_name=prompts.theorem_name,
                lean_code=lean_code,
            )
            max_eval_attempts = max(1, cfg.openrouter_eval_repair_retries + 1)
            gemini_double_check_enabled = (
                not cfg.use_codex_exec
            ) and _is_gemini_flash_preview_model(cfg.openrouter_model)

            def _run_eval_with_retries(
                *,
                model: str,
                reasoning_effort: str,
                stage_name: str,
                log_stem: str,
            ) -> tuple[str, Optional[CommandResult], dict[str, object]]:
                stage_prompt = base_eval_prompt
                stage_attempts: list[dict[str, object]] = []
                stage_payload: Optional[dict[str, object]] = None
                stage_last_res: Optional[CommandResult] = None

                for eval_try in range(1, max_eval_attempts + 1):
                    eval_res_try = _call_model(
                        prompt=stage_prompt,
                        model=model,
                        reasoning_effort=reasoning_effort,
                        stage=stage_name,
                        iter_no=it,
                        attempt_no=eval_try,
                        enable_web_search=False,
                    )
                    stage_last_res = eval_res_try
                    _write_text(
                        cfg.logs_dir
                        / f"{prompts.theorem_name}.iter{it}.{log_stem}_attempt{eval_try}_stdout.log",
                        eval_res_try.stdout,
                    )
                    _write_text(
                        cfg.logs_dir
                        / f"{prompts.theorem_name}.iter{it}.{log_stem}_attempt{eval_try}_stderr.log",
                        eval_res_try.stderr,
                    )

                    if eval_res_try.returncode != 0:
                        reason = eval_res_try.stderr.strip() or "evaluation request failed"
                        stage_attempts.append(
                            {
                                "attempt": eval_try,
                                "status": "request_failed",
                                "error": reason,
                            }
                        )
                        if eval_try < max_eval_attempts:
                            stage_prompt = _build_eval_retry_prompt(
                                base_prompt=base_eval_prompt,
                                failure_reason=reason,
                                previous_response_text=eval_res_try.stdout,
                                retry_no=eval_try + 1,
                            )
                            continue
                        stage_payload = {"status": "request_failed", "error": reason}
                        break

                    try:
                        eval_text = _extract_model_response_text(eval_res_try.stdout)
                        eval_obj = _parse_json_object_from_model_text(eval_text)
                        normalized_eval = _parse_formalization_eval_payload(eval_obj)
                        stage_payload = {"status": "ok", **normalized_eval}
                        stage_attempts.append({"attempt": eval_try, "status": "ok"})
                        break
                    except ValueError as exc:
                        reason = _format_eval_failure_reason(exc)
                        stage_attempts.append(
                            {
                                "attempt": eval_try,
                                "status": "parse_failed",
                                "error": reason,
                            }
                        )
                        if eval_try < max_eval_attempts:
                            stage_prompt = _build_eval_retry_prompt(
                                base_prompt=base_eval_prompt,
                                failure_reason=reason,
                                previous_response_text=eval_res_try.stdout,
                                retry_no=eval_try + 1,
                            )
                            continue
                        stage_payload = {"status": "parse_failed", "error": reason}
                        break

                if stage_payload is None:
                    stage_payload = {
                        "status": "request_failed",
                        "error": "evaluation finished without a result payload",
                    }
                if stage_attempts:
                    stage_payload["attempt_count"] = len(stage_attempts)
                    stage_payload["attempts"] = stage_attempts
                if stage_last_res is not None:
                    _write_text(
                        cfg.logs_dir / f"{prompts.theorem_name}.iter{it}.{log_stem}_stdout.log",
                        stage_last_res.stdout,
                    )
                    _write_text(
                        cfg.logs_dir / f"{prompts.theorem_name}.iter{it}.{log_stem}_stderr.log",
                        stage_last_res.stderr,
                    )
                _write_json(
                    cfg.logs_dir / f"{prompts.theorem_name}.iter{it}.{log_stem}.json", stage_payload
                )
                return stage_prompt, stage_last_res, stage_payload

            primary_eval_model = cfg.openrouter_eval_model
            primary_eval_reasoning_effort = cfg.openrouter_eval_reasoning_effort
            if gemini_double_check_enabled:
                primary_eval_model = cfg.openrouter_model

            _progress(f"Running evaluation (iter {it})...")
            eval_prompt, eval_res, primary_eval_payload = _run_eval_with_retries(
                model=primary_eval_model,
                reasoning_effort=primary_eval_reasoning_effort,
                stage_name="eval",
                log_stem="eval",
            )
            eval_payload = dict(primary_eval_payload)

            if gemini_double_check_enabled:
                _, _, secondary_eval_payload = _run_eval_with_retries(
                    model=_GEMINI_DOUBLE_CHECK_SECONDARY_EVAL_MODEL,
                    reasoning_effort=_GEMINI_DOUBLE_CHECK_SECONDARY_EVAL_REASONING_EFFORT,
                    stage_name="eval_gpt52",
                    log_stem="eval_gpt52",
                )

                primary_grade_obj = primary_eval_payload.get("grade")
                secondary_grade_obj = secondary_eval_payload.get("grade")
                primary_grade = (
                    primary_grade_obj.upper() if isinstance(primary_grade_obj, str) else ""
                )
                secondary_grade = (
                    secondary_grade_obj.upper() if isinstance(secondary_grade_obj, str) else ""
                )
                primary_ok = (
                    str(primary_eval_payload.get("status", "")).strip() == "ok"
                    and primary_grade in _EVAL_GRADES
                )
                secondary_ok = (
                    str(secondary_eval_payload.get("status", "")).strip() == "ok"
                    and secondary_grade in _EVAL_GRADES
                )
                both_a_pass = (
                    primary_ok and secondary_ok and primary_grade == "A" and secondary_grade == "A"
                )

                if primary_ok and secondary_ok:
                    aggregate_grade = primary_grade
                    if _grade_below_threshold(secondary_grade, aggregate_grade):
                        aggregate_grade = secondary_grade
                    eval_payload["grade"] = aggregate_grade

                if both_a_pass:
                    eval_payload["status"] = "ok"
                    eval_payload["grade"] = "A"
                    eval_payload.pop("error", None)
                else:
                    eval_payload["status"] = "double_check_failed"
                    details: list[str] = []
                    if primary_ok:
                        details.append(f"Gemini Flash={primary_grade}")
                    else:
                        details.append(
                            "Gemini Flash status="
                            + str(primary_eval_payload.get("status", "unknown")).strip()
                        )
                    if secondary_ok:
                        details.append(f"GPT-5.2={secondary_grade}")
                    else:
                        details.append(
                            "GPT-5.2 status="
                            + str(secondary_eval_payload.get("status", "unknown")).strip()
                        )
                    eval_payload["error"] = (
                        "Double-check policy failure: requires grade A from both Gemini Flash and GPT-5.2 "
                        "evaluator. " + "; ".join(details)
                    )

                eval_payload["double_check"] = {
                    "enabled": True,
                    "required_grade": "A",
                    "primary_model": primary_eval_model,
                    "primary_reasoning_effort": primary_eval_reasoning_effort,
                    "secondary_model": _GEMINI_DOUBLE_CHECK_SECONDARY_EVAL_MODEL,
                    "secondary_reasoning_effort": _GEMINI_DOUBLE_CHECK_SECONDARY_EVAL_REASONING_EFFORT,
                    "both_a_pass": both_a_pass,
                    "primary": primary_eval_payload,
                    "secondary": secondary_eval_payload,
                }

            if eval_res is not None:
                _write_text(
                    cfg.logs_dir / f"{prompts.theorem_name}.iter{it}.eval_stdout.log",
                    eval_res.stdout,
                )
                _write_text(
                    cfg.logs_dir / f"{prompts.theorem_name}.iter{it}.eval_stderr.log",
                    eval_res.stderr,
                )

            _write_json(cfg.logs_dir / f"{prompts.theorem_name}.iter{it}.eval.json", eval_payload)
            # Mirror evaluation payloads into output_dir so each formalization file has nearby eval artifacts.
            _write_json(cfg.output_dir / f"{prompts.theorem_name}.iter{it}.eval.json", eval_payload)
            _write_json(cfg.output_dir / f"{prompts.theorem_name}.eval.json", eval_payload)

            if gemini_double_check_enabled:
                double_check_obj = eval_payload.get("double_check")
                both_a_pass = isinstance(double_check_obj, dict) and bool(
                    double_check_obj.get("both_a_pass")
                )
                if not both_a_pass:
                    fail_reason = str(eval_payload.get("error", "")).strip()
                    if not fail_reason:
                        fail_reason = (
                            "Policy failure: double-check evaluation requires grade A from both Gemini Flash "
                            "and GPT-5.2 evaluator."
                        )
                    eval_feedback = _format_eval_feedback_for_repair(eval_payload)
                    if eval_feedback:
                        fail_reason += f"\n{eval_feedback}"
                    compiler_res = CommandResult(
                        argv=["(policy)"],
                        returncode=1,
                        stdout=compiler_res.stdout,
                        stderr=((compiler_res.stderr + "\n" + fail_reason).strip()),
                    )
                    _write_compiler_logs(cfg.logs_dir, prompts.theorem_name, it, compiler_res)

            if compiler_res.returncode == 0 and effective_min_eval_grade is not None:
                min_grade = effective_min_eval_grade.upper()
                eval_status = str(eval_payload.get("status", "")).strip()
                eval_feedback = _format_eval_feedback_for_repair(eval_payload)
                if eval_status != "ok":
                    fail_reason = (
                        "Policy failure: evaluation result unavailable while enforcing minimum grade "
                        f"{min_grade}. Last status={eval_status or 'unknown'}"
                    )
                    if "error" in eval_payload:
                        fail_reason += f"; error={eval_payload['error']}"
                    if eval_feedback:
                        fail_reason += f"\n{eval_feedback}"
                    compiler_res = CommandResult(
                        argv=["(policy)"],
                        returncode=1,
                        stdout=compiler_res.stdout,
                        stderr=((compiler_res.stderr + "\n" + fail_reason).strip()),
                    )
                    _write_compiler_logs(cfg.logs_dir, prompts.theorem_name, it, compiler_res)
                else:
                    eval_grade_obj = eval_payload.get("grade")
                    eval_grade = eval_grade_obj.upper() if isinstance(eval_grade_obj, str) else ""
                    _progress(f"Evaluation grade: {eval_grade or '(none)'} (iter {it})")
                    if eval_grade not in _EVAL_GRADES:
                        fail_reason = (
                            "Policy failure: evaluator grade missing/invalid while enforcing minimum grade "
                            f"{min_grade}."
                        )
                        if eval_feedback:
                            fail_reason += f"\n{eval_feedback}"
                        compiler_res = CommandResult(
                            argv=["(policy)"],
                            returncode=1,
                            stdout=compiler_res.stdout,
                            stderr=((compiler_res.stderr + "\n" + fail_reason).strip()),
                        )
                        _write_compiler_logs(cfg.logs_dir, prompts.theorem_name, it, compiler_res)
                    elif _grade_below_threshold(eval_grade, min_grade):
                        fail_reason = (
                            "Policy failure: evaluation grade "
                            f"{eval_grade} is below required minimum {min_grade}."
                        )
                        if eval_feedback:
                            fail_reason += f"\n{eval_feedback}"
                        compiler_res = CommandResult(
                            argv=["(policy)"],
                            returncode=1,
                            stdout=compiler_res.stdout,
                            stderr=((compiler_res.stderr + "\n" + fail_reason).strip()),
                        )
                        _write_compiler_logs(cfg.logs_dir, prompts.theorem_name, it, compiler_res)

        _finalize_iteration(
            iter_no=it,
            thinking_prompt=thinking_prompt,
            coding_prompt=coding_prompt,
            thinking_res=thinking_res,
            coding_res=coding_res,
            compiler_res=compiler_res,
            evaluation_prompt=eval_prompt,
            evaluation_res=eval_res,
            evaluation_payload=eval_payload,
        )

        if compiler_res.returncode == 0:
            _progress(f"SUCCESS: Formalization passed on iteration {it}!")
            return True, records
        else:
            _progress(f"Iteration {it} did not pass, will retry..." if it < cfg.max_iters else f"Iteration {it} did not pass.")

    _progress(f"FAILED: Formalization did not pass after {cfg.max_iters} iterations.")
    return False, records


def iter_problem_files(input_dir: Path) -> Iterable[Path]:
    yield from sorted(input_dir.glob("*.json"))
