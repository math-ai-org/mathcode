#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from autolean.core import (  # noqa: E402
    _call_codex_exec,
    _call_openrouter_chat,
    _extract_model_response_text,
    _parse_json_object_from_model_text,
)
from autolean.util import ensure_dir  # noqa: E402


_REASONING_CHOICES = ["minimal", "low", "medium", "high", "xhigh"]
_ATTEMPT_STYLE_HINTS = [
    "Prefer a short direct proof using existing Mathlib lemmas and `simpa`/`rw` where possible.",
    "Prefer a structured proof with intermediate `have` steps and explicit lemma names.",
    "Prefer introducing small local helper lemmas inside the file if that simplifies the main theorem proof.",
    "Prefer normalizing algebraic or analytic expressions aggressively when justified.",
]
_FORBIDDEN_LINE_KEYWORDS = ("axiom", "constant", "postulate")
_THEOREM_DECL_RE_TEMPLATE = r"(?m)^\s*(?:theorem|lemma)\s+{name}\b"
_WHITESPACE_RE = re.compile(r"\s+")


@dataclasses.dataclass(frozen=True)
class ProofConfig:
    input_dir: Path
    out_dir: Path
    attempts: int
    max_iters: int
    workers: int
    provider: str
    model: str
    reasoning_effort: str
    api_key_env: str
    openrouter_base_url: str
    openrouter_timeout_s: int
    openrouter_max_retries: int
    codex_exec_sandbox: str
    codex_workdir: Path
    compile_cmd: str
    cwd: Path


@dataclasses.dataclass(frozen=True)
class ProofTarget:
    lean_path: Path
    relative_path: Path
    theorem_name: str
    original_text: str
    normalized_header: str
    original_import_block: str
    frozen_before_proof: str
    frozen_suffix: str


@dataclasses.dataclass(frozen=True)
class _DeclLayout:
    theorem_start: int
    proof_start: int
    normalized_header: str


@dataclasses.dataclass(frozen=True)
class CompileResult:
    returncode: int
    stdout: str
    stderr: str


@dataclasses.dataclass(frozen=True)
class AttemptOutcome:
    attempt_no: int
    passed: bool
    status: str
    iterations_used: int
    final_lean_path: Optional[str]
    error: Optional[str]


@dataclasses.dataclass(frozen=True)
class ProblemOutcome:
    relative_path: str
    theorem_name: str
    pass_count: int
    one_shot_pass_count: int
    attempt_count: int
    attempts: list[AttemptOutcome]


def _format_progress(completed: int, total: int, *, label: str = "Proving") -> str:
    width = 24
    if total <= 0:
        bar = "[" + "." * width + "]"
        return f"{label}: {bar} 0/0"
    filled = int(width * completed / total)
    filled = min(width, max(0, filled))
    bar = "[" + "#" * filled + "." * (width - filled) + "]"
    return f"{label}: {bar} {completed}/{total}"


def _make_progress_printer():
    last_len = 0

    def _print(msg: str, *, done: bool = False) -> None:
        nonlocal last_len
        pad = " " * max(0, last_len - len(msg))
        end = "\n" if done else ""
        print("\r" + msg + pad, end=end, flush=True)
        last_len = len(msg)

    return _print


def _normalize_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text.strip())


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _split_import_block(text: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    idx = 0
    in_block_comment = False

    while idx < len(lines):
        stripped = lines[idx].strip()
        if in_block_comment:
            idx += 1
            if "-/" in stripped:
                in_block_comment = False
            continue
        if not stripped:
            idx += 1
            continue
        if stripped.startswith("--"):
            idx += 1
            continue
        if stripped.startswith("/-"):
            idx += 1
            if "-/" not in stripped:
                in_block_comment = True
            continue
        if stripped.startswith("import "):
            idx += 1
            continue
        break

    return "".join(lines[:idx]), "".join(lines[idx:])


def _extract_decl_layout(lean_code: str, theorem_name: str) -> Optional[_DeclLayout]:
    start_re = re.compile(_THEOREM_DECL_RE_TEMPLATE.format(name=re.escape(theorem_name)))
    match = start_re.search(lean_code)
    if match is None:
        return None

    depth = 0
    for idx in range(match.end(), len(lean_code) - 1):
        ch = lean_code[idx]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == ":" and depth == 0 and lean_code[idx + 1] == "=":
            body_idx = idx + 2
            while body_idx < len(lean_code) and lean_code[body_idx].isspace():
                body_idx += 1
            if not lean_code.startswith("by", body_idx):
                continue
            header = lean_code[match.start() : idx].strip()
            return _DeclLayout(
                theorem_start=match.start(),
                proof_start=body_idx + 2,
                normalized_header=_normalize_whitespace(header),
            )
    return None


def _extract_import_lines(import_block: str) -> list[str]:
    lines: list[str] = []
    for raw_line in import_block.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("import "):
            lines.append(stripped)
    return lines


def _find_frozen_suffix_start(text: str, proof_start: int) -> int:
    suffix_re = re.compile(r"(?m)^\s*end(?:\s+[A-Za-z0-9_'.]+)?\s*$")
    matches = list(suffix_re.finditer(text, proof_start))
    if not matches:
        return len(text)
    return matches[-1].start()


def _has_placeholder_proof(lean_code: str) -> bool:
    return bool(re.search(r"\b(?:sorry|admit)\b", lean_code))


def _detect_forbidden_content(candidate_text: str, original_text: str) -> Optional[str]:
    if re.search(r"\bsorry\b", candidate_text):
        return "generated Lean still contains `sorry`"
    if re.search(r"\badmit\b", candidate_text):
        return "generated Lean still contains `admit`"

    for keyword in _FORBIDDEN_LINE_KEYWORDS:
        candidate_count = len(re.findall(rf"(?m)^\s*{keyword}\b", candidate_text))
        original_count = len(re.findall(rf"(?m)^\s*{keyword}\b", original_text))
        if candidate_count > original_count:
            return f"generated Lean introduced forbidden top-level `{keyword}` declarations"
    return None


def _load_targets(
    *,
    input_dir: Path,
    include_complete: bool,
    skip_subtree: Optional[Path] = None,
    limit: Optional[int] = None,
) -> tuple[list[ProofTarget], list[Path], list[Path]]:
    targets: list[ProofTarget] = []
    invalid_targets: list[Path] = []
    skipped_complete: list[Path] = []

    for lean_path in sorted(input_dir.rglob("*.lean")):
        if skip_subtree is not None and _is_relative_to(lean_path, skip_subtree):
            continue

        try:
            original_text = lean_path.read_text(encoding="utf-8")
        except OSError:
            invalid_targets.append(lean_path)
            continue

        if not include_complete and not _has_placeholder_proof(original_text):
            skipped_complete.append(lean_path)
            continue

        theorem_name = lean_path.stem
        original_import_block, _rest = _split_import_block(original_text)
        layout = _extract_decl_layout(original_text, theorem_name)
        if layout is None:
            invalid_targets.append(lean_path)
            continue
        suffix_start = _find_frozen_suffix_start(original_text, layout.proof_start)

        targets.append(
            ProofTarget(
                lean_path=lean_path,
                relative_path=lean_path.relative_to(input_dir),
                theorem_name=theorem_name,
                original_text=original_text,
                normalized_header=layout.normalized_header,
                original_import_block=original_import_block,
                frozen_before_proof=original_text[len(original_import_block) : layout.proof_start],
                frozen_suffix=original_text[suffix_start:],
            )
        )
        if limit is not None and len(targets) >= limit:
            break

    return targets, invalid_targets, skipped_complete


def _build_initial_prompt(target: ProofTarget, *, attempt_no: int, attempt_count: int) -> str:
    style_hint = _ATTEMPT_STYLE_HINTS[(attempt_no - 1) % len(_ATTEMPT_STYLE_HINTS)]
    return f"""You are completing a Lean 4 proof for an already-formalized theorem.

Independent worker: {attempt_no} of {attempt_count}.
Work independently. Do not assume any communication with other workers.

Goal:
- Replace the placeholder proof body with a complete proof that compiles.
- Keep the file unchanged outside the main theorem proof body, except that you may request extra imports if they are genuinely necessary.

Hard constraints:
- The main theorem name must stay exactly `{target.theorem_name}`.
- The main theorem header must stay exactly the same.
- Remove all `sorry` and `admit`.
- Do not introduce `axiom`, `constant`, or `postulate`.
- Do not rewrite the theorem statement, docstrings, namespace, earlier definitions, or trailing `end`.
- Do not add new top-level lemmas/defs. Use local `have`, `let`, `suffices`, or `calc` inside the proof body instead.
- Do not repeat the theorem header or the leading `by` in your `proof` field.
- If extra imports are needed, put them only in the `imports` list.

Strategy bias for this worker:
- {style_hint}

Return ONLY a JSON object of this form:
{{
  "imports": ["import Mathlib.X", "import Mathlib.Y"],
  "proof": "<Lean proof body that comes after the existing `by`>"
}}

If no extra imports are needed, return `"imports": []`.

The exact frozen file prefix ending at the theorem's `by` is:
```lean
{target.original_import_block}{target.frozen_before_proof}
```

The exact frozen file suffix after the proof body is:
```lean
{target.frozen_suffix}
```

Current Lean file:
```lean
{target.original_text}
```"""


def _build_repair_prompt(
    target: ProofTarget,
    *,
    attempt_no: int,
    attempt_count: int,
    prev_lean: str,
    failure_reason: str,
) -> str:
    style_hint = _ATTEMPT_STYLE_HINTS[(attempt_no - 1) % len(_ATTEMPT_STYLE_HINTS)]
    return f"""You are repairing a Lean 4 proof attempt for an already-formalized theorem.

Independent worker: {attempt_no} of {attempt_count}.
Work independently. Do not assume any communication with other workers.

Hard constraints:
- Keep the main theorem name exactly `{target.theorem_name}`.
- Keep the file unchanged outside the main theorem proof body, except for genuinely necessary extra imports.
- Remove all `sorry` and `admit`.
- Do not introduce `axiom`, `constant`, or `postulate`.
- Do not add new top-level lemmas/defs. Use local proof structure only.
- Do not repeat the theorem header or the leading `by` in your `proof` field.
- If extra imports are needed, put them only in the `imports` list.

Strategy bias for this worker:
- {style_hint}

Return ONLY a JSON object of this form:
{{
  "imports": ["import Mathlib.X", "import Mathlib.Y"],
  "proof": "<Lean proof body that comes after the existing `by`>"
}}

Previous Lean attempt:
```lean
{prev_lean}
```

Failure reason / compiler output:
```text
{failure_reason}
```

The exact frozen file prefix ending at the theorem's `by` is:
```lean
{target.original_import_block}{target.frozen_before_proof}
```

The exact frozen file suffix after the proof body is:
```lean
{target.frozen_suffix}
```

Original source file for reference:
```lean
{target.original_text}
```"""


def _normalize_extra_imports(imports_obj: object) -> list[str]:
    if imports_obj is None:
        return []
    if not isinstance(imports_obj, list):
        raise ValueError("Model output field `imports` must be a list of strings.")

    normalized: list[str] = []
    seen: set[str] = set()
    for item in imports_obj:
        if not isinstance(item, str):
            raise ValueError("Model output field `imports` must be a list of strings.")
        stripped = item.strip()
        if not stripped:
            continue
        line = stripped if stripped.startswith("import ") else f"import {stripped}"
        if "\n" in line or "\r" in line:
            raise ValueError("Import entries must be single lines.")
        if line in seen:
            continue
        seen.add(line)
        normalized.append(line)
    return normalized


def _normalize_proof_body(proof_obj: object) -> str:
    if not isinstance(proof_obj, str) or not proof_obj.strip():
        raise ValueError("Model output missing non-empty string field `proof`.")
    if re.match(r"^\s*by(\s|$)", proof_obj):
        raise ValueError("Proof body must not repeat the leading `by`.")
    if proof_obj.startswith(("\n", " ", "\t")):
        return proof_obj
    return "\n  " + proof_obj


def _extract_extra_imports_from_candidate_block(
    original_import_block: str,
    candidate_import_block: str,
) -> list[str]:
    if not candidate_import_block.startswith(original_import_block):
        raise ValueError("Candidate changed the original import block.")

    tail = candidate_import_block[len(original_import_block) :]
    for raw_line in tail.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        if stripped.startswith("/-") or stripped == "-/":
            raise ValueError("Extra imports tail may not contain new block comments.")
        if not stripped.startswith("import "):
            raise ValueError("Only extra import lines may be added before the frozen prefix.")

    original_imports = set(_extract_import_lines(original_import_block))
    extra_imports: list[str] = []
    for line in _extract_import_lines(tail):
        if line not in original_imports:
            extra_imports.append(line)
    return extra_imports


def _extract_edits_from_full_file_candidate(
    target: ProofTarget,
    candidate_text: str,
) -> tuple[list[str], str]:
    candidate_import_block, _rest = _split_import_block(candidate_text)
    candidate_layout = _extract_decl_layout(candidate_text, target.theorem_name)
    if candidate_layout is None:
        raise ValueError(f"Candidate full file was missing theorem `{target.theorem_name}`.")

    candidate_suffix_start = _find_frozen_suffix_start(candidate_text, candidate_layout.proof_start)
    candidate_frozen_before_proof = candidate_text[
        len(candidate_import_block) : candidate_layout.proof_start
    ]
    candidate_suffix = candidate_text[candidate_suffix_start:]

    if candidate_frozen_before_proof != target.frozen_before_proof:
        raise ValueError("Candidate full file changed frozen content before the proof body.")
    if candidate_suffix != target.frozen_suffix:
        raise ValueError("Candidate full file changed frozen content after the proof body.")

    extra_imports = _extract_extra_imports_from_candidate_block(
        target.original_import_block,
        candidate_import_block,
    )
    proof_body = candidate_text[candidate_layout.proof_start:candidate_suffix_start]
    return extra_imports, _normalize_proof_body(proof_body)


def _extract_candidate_edits(
    target: ProofTarget,
    payload: dict,
) -> tuple[list[str], str]:
    if "proof" in payload:
        return _normalize_extra_imports(payload.get("imports")), _normalize_proof_body(payload["proof"])

    lean_obj = payload.get("lean")
    if isinstance(lean_obj, str) and lean_obj.strip():
        return _extract_edits_from_full_file_candidate(target, lean_obj)

    raise ValueError("Model output must contain either `proof` or `lean`.")


def _build_candidate_text(
    target: ProofTarget,
    *,
    extra_imports: list[str],
    proof_body: str,
) -> str:
    import_block = target.original_import_block
    original_imports = set(_extract_import_lines(import_block))
    new_imports = [line for line in extra_imports if line not in original_imports]
    if new_imports:
        import_block = import_block.rstrip("\n")
        if import_block:
            import_block += "\n"
        import_block += "\n".join(new_imports) + "\n"
        if not target.frozen_before_proof.startswith("\n"):
            import_block += "\n"
    return import_block + target.frozen_before_proof + proof_body + target.frozen_suffix


def _detect_forbidden_proof_body_content(proof_body: str) -> Optional[str]:
    forbidden_re = re.compile(
        r"(?m)^(?:theorem|lemma|def|noncomputable\s+def|example|namespace|section|end)\b"
    )
    if forbidden_re.search(proof_body):
        return "proof body introduced top-level declarations or structure changes"
    return None


def _call_proof_model(
    *,
    provider: str,
    prompt: str,
    model: str,
    reasoning_effort: str,
    api_key_env: str,
    openrouter_base_url: str,
    openrouter_timeout_s: int,
    openrouter_max_retries: int,
    codex_exec_sandbox: str,
    codex_workdir: Path,
    codex_message_path: Path,
) -> tuple[str, str, int]:
    if provider == "codex-exec":
        res = _call_codex_exec(
            prompt=prompt,
            out_message_path=codex_message_path,
            model=model,
            reasoning_effort=reasoning_effort,
            sandbox=codex_exec_sandbox,
            workdir=codex_workdir,
            live_logs=False,
            stdout_sink=None,
            stderr_sink=None,
        )
        return res.stdout, res.stderr, res.returncode

    res = _call_openrouter_chat(
        prompt=prompt,
        model=model,
        base_url=openrouter_base_url,
        api_key_env=api_key_env,
        timeout_s=openrouter_timeout_s,
        max_retries=openrouter_max_retries,
        reasoning_effort=reasoning_effort,
        openrouter_web_search=False,
        openrouter_web_search_engine=None,
        openrouter_web_search_max_results=None,
    )
    return res.stdout, res.stderr, res.returncode


def _compile_candidate(*, lean_path: Path, compile_cmd: str, cwd: Path) -> CompileResult:
    argv = shlex.split(compile_cmd.replace("{file}", str(lean_path.resolve())))
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )
    return CompileResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def _write_text(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, payload: dict) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _run_attempt(
    target: ProofTarget,
    *,
    cfg: ProofConfig,
    problem_out_dir: Path,
    attempt_no: int,
) -> AttemptOutcome:
    attempt_dir = problem_out_dir / f"attempt{attempt_no}"
    ensure_dir(attempt_dir)
    prev_lean = target.original_text
    last_failure = "No attempt made."
    last_candidate_path: Optional[Path] = None

    for iter_no in range(1, cfg.max_iters + 1):
        if iter_no == 1:
            prompt = _build_initial_prompt(
                target,
                attempt_no=attempt_no,
                attempt_count=cfg.attempts,
            )
        else:
            prompt = _build_repair_prompt(
                target,
                attempt_no=attempt_no,
                attempt_count=cfg.attempts,
                prev_lean=prev_lean,
                failure_reason=last_failure,
            )

        _write_text(attempt_dir / f"iter{iter_no}.prompt.txt", prompt)
        stdout_text, stderr_text, returncode = _call_proof_model(
            provider=cfg.provider,
            prompt=prompt,
            model=cfg.model,
            reasoning_effort=cfg.reasoning_effort,
            api_key_env=cfg.api_key_env,
            openrouter_base_url=cfg.openrouter_base_url,
            openrouter_timeout_s=cfg.openrouter_timeout_s,
            openrouter_max_retries=cfg.openrouter_max_retries,
            codex_exec_sandbox=cfg.codex_exec_sandbox,
            codex_workdir=cfg.codex_workdir,
            codex_message_path=attempt_dir / f"iter{iter_no}.codex_last_message.log",
        )
        _write_text(attempt_dir / f"iter{iter_no}.model_stdout.log", stdout_text)
        _write_text(attempt_dir / f"iter{iter_no}.model_stderr.log", stderr_text)

        if returncode != 0:
            last_failure = stderr_text.strip() or "model request failed"
            continue

        try:
            model_text = _extract_model_response_text(stdout_text)
            payload = _parse_json_object_from_model_text(model_text)
        except ValueError as exc:
            last_failure = f"Model output parse failure: {exc}"
            continue

        try:
            extra_imports, proof_body = _extract_candidate_edits(target, payload)
        except ValueError as exc:
            last_failure = f"Model edit parse failure: {exc}"
            continue

        proof_body_policy_failure = _detect_forbidden_proof_body_content(proof_body)
        if proof_body_policy_failure is not None:
            last_failure = f"Policy failure: {proof_body_policy_failure}."
            continue

        candidate_text = _build_candidate_text(
            target,
            extra_imports=extra_imports,
            proof_body=proof_body,
        )

        forbidden_reason = _detect_forbidden_content(candidate_text, target.original_text)
        if forbidden_reason is not None:
            prev_lean = candidate_text
            last_failure = f"Policy failure: {forbidden_reason}."
            continue

        candidate_path = attempt_dir / f"iter{iter_no}.candidate.lean"
        _write_text(candidate_path, candidate_text)
        last_candidate_path = candidate_path
        prev_lean = candidate_text

        compile_res = _compile_candidate(
            lean_path=candidate_path,
            compile_cmd=cfg.compile_cmd,
            cwd=cfg.cwd,
        )
        _write_text(attempt_dir / f"iter{iter_no}.compile_stdout.log", compile_res.stdout)
        _write_text(attempt_dir / f"iter{iter_no}.compile_stderr.log", compile_res.stderr)

        if compile_res.returncode == 0:
            payload = {
                "attempt_no": attempt_no,
                "passed": True,
                "status": "ok",
                "iterations_used": iter_no,
                "final_lean_path": str(candidate_path),
                "error": None,
            }
            _write_json(attempt_dir / "outcome.json", payload)
            return AttemptOutcome(
                attempt_no=attempt_no,
                passed=True,
                status="ok",
                iterations_used=iter_no,
                final_lean_path=str(candidate_path),
                error=None,
            )

        last_failure = (compile_res.stderr.strip() + "\n" + compile_res.stdout.strip()).strip()
        if not last_failure:
            last_failure = "Lean compiler failed without stdout/stderr output."

    payload = {
        "attempt_no": attempt_no,
        "passed": False,
        "status": "failed",
        "iterations_used": cfg.max_iters,
        "final_lean_path": str(last_candidate_path) if last_candidate_path is not None else None,
        "error": last_failure,
    }
    _write_json(attempt_dir / "outcome.json", payload)
    return AttemptOutcome(
        attempt_no=attempt_no,
        passed=False,
        status="failed",
        iterations_used=cfg.max_iters,
        final_lean_path=str(last_candidate_path) if last_candidate_path is not None else None,
        error=last_failure,
    )


def _prove_target(target: ProofTarget, *, cfg: ProofConfig) -> ProblemOutcome:
    problem_out_dir = cfg.out_dir / target.relative_path.with_suffix("")
    ensure_dir(problem_out_dir)

    attempts: list[AttemptOutcome] = []
    with ThreadPoolExecutor(max_workers=cfg.attempts) as executor:
        future_map = {
            executor.submit(
                _run_attempt,
                target,
                cfg=cfg,
                problem_out_dir=problem_out_dir,
                attempt_no=attempt_no,
            ): attempt_no
            for attempt_no in range(1, cfg.attempts + 1)
        }
        for future in as_completed(future_map):
            attempt_no = future_map[future]
            try:
                attempts.append(future.result())
            except Exception as exc:  # pragma: no cover - defensive fallback
                attempts.append(
                    AttemptOutcome(
                        attempt_no=attempt_no,
                        passed=False,
                        status="runner_error",
                        iterations_used=0,
                        final_lean_path=None,
                        error=str(exc),
                    )
                )

    attempts.sort(key=lambda item: item.attempt_no)
    outcome = ProblemOutcome(
        relative_path=str(target.relative_path),
        theorem_name=target.theorem_name,
        pass_count=sum(1 for item in attempts if item.passed),
        one_shot_pass_count=sum(
            1 for item in attempts if item.passed and item.iterations_used == 1
        ),
        attempt_count=len(attempts),
        attempts=attempts,
    )
    _write_json(problem_out_dir / "summary.json", dataclasses.asdict(outcome))
    return outcome


def _write_report(
    *,
    out_dir: Path,
    cfg: ProofConfig,
    results: list[ProblemOutcome],
    invalid_targets: list[Path],
    skipped_complete: list[Path],
) -> None:
    total_passes = sum(item.pass_count for item in results)
    total_one_shot_passes = sum(item.one_shot_pass_count for item in results)
    problems_with_any_pass = sum(1 for item in results if item.pass_count > 0)
    problems_with_any_one_shot_pass = sum(1 for item in results if item.one_shot_pass_count > 0)
    summary_payload = {
        "input_dir": str(cfg.input_dir),
        "out_dir": str(cfg.out_dir),
        "provider": cfg.provider,
        "model": cfg.model,
        "attempts_per_problem": cfg.attempts,
        "max_iters": cfg.max_iters,
        "workers": cfg.workers,
        "problem_count": len(results),
        "problems_with_any_pass": problems_with_any_pass,
        "problems_with_any_one_shot_pass": problems_with_any_one_shot_pass,
        "problems_all_failed": len(results) - problems_with_any_pass,
        "total_passes": total_passes,
        "total_one_shot_passes": total_one_shot_passes,
        "invalid_targets": [str(path) for path in invalid_targets],
        "skipped_complete": [str(path) for path in skipped_complete],
        "results": [dataclasses.asdict(item) for item in results],
    }
    _write_json(out_dir / "proof_summary.json", summary_payload)

    lines = [
        "Autolean Proof Completion Report",
        f"Input: {cfg.input_dir}",
        f"Output: {cfg.out_dir}",
        f"Provider: {cfg.provider}",
        f"Model: {cfg.model}",
        f"Attempts per problem: {cfg.attempts}",
        f"Max repair iterations per attempt: {cfg.max_iters}",
        f"Workers across problems: {cfg.workers}",
        f"Problems processed: {len(results)}",
        f"Problems with at least one pass: {problems_with_any_pass}",
        f"Problems with at least one one-shot pass: {problems_with_any_one_shot_pass}",
        f"Problems with zero passes: {len(results) - problems_with_any_pass}",
        f"Total passing attempts: {total_passes}",
        f"Total one-shot passing attempts: {total_one_shot_passes}",
        f"Invalid targets skipped: {len(invalid_targets)}",
        f"Already-complete targets skipped: {len(skipped_complete)}",
        "",
        "Per-problem pass counts:",
    ]
    for item in sorted(results, key=lambda result: result.relative_path):
        lines.append(
            f"- {item.relative_path}: {item.pass_count}/{item.attempt_count} passed, "
            f"{item.one_shot_pass_count}/{item.attempt_count} one-shot"
        )
    if invalid_targets:
        lines.extend(["", "Invalid targets:"])
        lines.extend(f"- {path}" for path in invalid_targets)
    if skipped_complete:
        lines.extend(["", "Skipped already-complete targets:"])
        lines.extend(f"- {path}" for path in skipped_complete)
    _write_text(out_dir / "proof_report.txt", "\n".join(lines).rstrip() + "\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Complete proofs for existing Lean formalizations by running several independent LLM "
            "attempts per theorem and compile-checking each attempt."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("A_evaled_lean_formalizations"),
        help="Root directory containing existing Lean files. The script searches recursively.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("proof_runs"),
        help="Directory where all proof attempts, logs, and reports are written.",
    )
    parser.add_argument(
        "--provider",
        choices=["codex-exec", "openrouter"],
        default="openrouter",
        help="Model backend used for proof generation.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="openai/gpt-5.2-codex",
        help="Model name for proof generation.",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=_REASONING_CHOICES,
        default="xhigh",
        help="Reasoning effort for proof generation.",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=4,
        help="Number of independent parallel proof attempts per Lean file (default: 4).",
    )
    parser.add_argument(
        "--max-iters",
        type=int,
        default=3,
        help="Maximum compile-repair iterations per attempt (default: 3).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Parallel workers across different Lean files (default: 1).",
    )
    parser.add_argument(
        "--include-complete",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Also process Lean files that do not currently contain `sorry`/`admit`.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of target Lean files to process.",
    )
    parser.add_argument(
        "--progress",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Show a progress bar.",
    )
    parser.add_argument(
        "--api-key-name",
        type=str,
        choices=["PRINCIPIA_KEY", "AUTOLEAN"],
        default="PRINCIPIA_KEY",
        help="Environment variable name used for OpenRouter API key lookup.",
    )
    parser.add_argument(
        "--openrouter-base-url",
        type=str,
        default="https://openrouter.ai/api/v1",
        help="OpenRouter API base URL.",
    )
    parser.add_argument(
        "--openrouter-timeout-s",
        type=int,
        default=180,
        help="OpenRouter request timeout in seconds.",
    )
    parser.add_argument(
        "--openrouter-max-retries",
        type=int,
        default=2,
        help="Retry count for transient OpenRouter request failures.",
    )
    parser.add_argument(
        "--codex-exec-sandbox",
        type=str,
        choices=["read-only", "workspace-write", "danger-full-access"],
        default="read-only",
        help="Sandbox passed to `codex exec` when --provider=codex-exec.",
    )
    parser.add_argument(
        "--codex-workdir",
        type=Path,
        default=REPO_ROOT,
        help="Working directory passed to `codex exec`.",
    )
    parser.add_argument(
        "--compile-cmd",
        type=str,
        default="lake env lean {file}",
        help="Compile command template. Must include '{file}'.",
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        default=Path.cwd(),
        help="Working directory where Lean compilation runs.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    input_dir = args.input_dir.resolve()
    out_dir = args.out_dir.resolve()
    compile_cwd = args.cwd.resolve()
    codex_workdir = args.codex_workdir.resolve()

    if "{file}" not in str(args.compile_cmd):
        print("Error: --compile-cmd must include '{file}'.", file=sys.stderr)
        return 2
    if not input_dir.is_dir():
        print(f"Error: input directory does not exist: {input_dir}", file=sys.stderr)
        return 2
    if not compile_cwd.is_dir():
        print(f"Error: compile cwd does not exist: {compile_cwd}", file=sys.stderr)
        return 2
    if not codex_workdir.is_dir():
        print(f"Error: codex workdir does not exist: {codex_workdir}", file=sys.stderr)
        return 2
    if args.attempts < 1:
        print("Error: --attempts must be >= 1.", file=sys.stderr)
        return 2
    if args.max_iters < 1:
        print("Error: --max-iters must be >= 1.", file=sys.stderr)
        return 2
    if args.workers < 1:
        print("Error: --workers must be >= 1.", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit < 1:
        print("Error: --limit must be >= 1.", file=sys.stderr)
        return 2

    ensure_dir(out_dir)
    skip_subtree = out_dir if _is_relative_to(out_dir, input_dir) else None

    targets, invalid_targets, skipped_complete = _load_targets(
        input_dir=input_dir,
        include_complete=bool(args.include_complete),
        skip_subtree=skip_subtree,
        limit=args.limit,
    )

    cfg = ProofConfig(
        input_dir=input_dir,
        out_dir=out_dir,
        attempts=int(args.attempts),
        max_iters=int(args.max_iters),
        workers=int(args.workers),
        provider=str(args.provider),
        model=str(args.model),
        reasoning_effort=str(args.reasoning_effort),
        api_key_env=str(args.api_key_name),
        openrouter_base_url=str(args.openrouter_base_url),
        openrouter_timeout_s=int(args.openrouter_timeout_s),
        openrouter_max_retries=int(args.openrouter_max_retries),
        codex_exec_sandbox=str(args.codex_exec_sandbox),
        codex_workdir=codex_workdir,
        compile_cmd=str(args.compile_cmd),
        cwd=compile_cwd,
    )

    show_progress = bool(args.progress) and len(targets) > 0
    progress_print = _make_progress_printer() if show_progress else None
    if show_progress and progress_print is not None:
        progress_print(_format_progress(0, len(targets)))

    results: list[ProblemOutcome] = []
    completed = 0
    if cfg.workers <= 1:
        for target in targets:
            results.append(_prove_target(target, cfg=cfg))
            completed += 1
            if show_progress and progress_print is not None:
                progress_print(
                    _format_progress(
                        completed,
                        len(targets),
                        label=f"Processed {target.relative_path.as_posix()}",
                    )
                )
    else:
        with ThreadPoolExecutor(max_workers=cfg.workers) as executor:
            future_map = {
                executor.submit(_prove_target, target, cfg=cfg): target for target in targets
            }
            for future in as_completed(future_map):
                target = future_map[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # pragma: no cover - defensive fallback
                    results.append(
                        ProblemOutcome(
                            relative_path=str(target.relative_path),
                            theorem_name=target.theorem_name,
                            pass_count=0,
                            one_shot_pass_count=0,
                            attempt_count=1,
                            attempts=[
                                AttemptOutcome(
                                    attempt_no=0,
                                    passed=False,
                                    status="runner_error",
                                    iterations_used=0,
                                    final_lean_path=None,
                                    error=str(exc),
                                )
                            ],
                        )
                    )
                completed += 1
                if show_progress and progress_print is not None:
                    progress_print(
                        _format_progress(completed, len(targets), label="Completed")
                    )

    results.sort(key=lambda item: item.relative_path)

    if show_progress and progress_print is not None:
        progress_print(_format_progress(len(targets), len(targets)), done=True)

    _write_report(
        out_dir=out_dir,
        cfg=cfg,
        results=results,
        invalid_targets=invalid_targets,
        skipped_complete=skipped_complete,
    )

    problems_with_zero_pass = sum(1 for item in results if item.pass_count == 0)
    if invalid_targets or problems_with_zero_pass > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
