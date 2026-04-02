#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from autolean.core import (  # noqa: E402
    _build_eval_retry_prompt,
    _build_formalization_eval_prompt,
    _call_codex_exec,
    _call_openrouter_chat,
    _extract_model_response_text,
    _format_eval_failure_reason,
    _parse_formalization_eval_payload,
    _parse_json_object_from_model_text,
)
from autolean.prompting import build_prompts  # noqa: E402
from autolean.util import ensure_dir  # noqa: E402


_REASONING_CHOICES = ["minimal", "low", "medium", "high", "xhigh"]


@dataclasses.dataclass(frozen=True)
class EvalTarget:
    json_path: Path
    lean_path: Path
    theorem_name: str
    problem_json: dict


@dataclasses.dataclass(frozen=True)
class EvalOutcome:
    theorem_name: str
    json_path: Path
    lean_path: Path
    eval_path: Path
    status: str
    grade: Optional[str]
    error: Optional[str]


def _format_progress(completed: int, total: int, *, label: str = "Evaluating") -> str:
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


def _write_text(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, payload: dict) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _load_targets(*, input_dir: Path, formalizations_dir: Path) -> tuple[list[EvalTarget], list[Path], list[Path]]:
    json_files = sorted(input_dir.glob("*.json"))
    if not json_files:
        raise ValueError(f"No JSON problem files found in: {input_dir.as_posix()}")

    targets: list[EvalTarget] = []
    invalid_json: list[Path] = []
    missing_lean: list[Path] = []

    for json_path in json_files:
        try:
            problem_json = json.loads(json_path.read_text(encoding="utf-8"))
            prompts = build_prompts(
                problem_json,
                out_dir=formalizations_dir,
                name_hint=json_path.stem,
                formalization_only=True,
            )
        except Exception:
            invalid_json.append(json_path)
            continue

        lean_path = prompts.lean_path
        if not lean_path.exists():
            missing_lean.append(lean_path)
            continue

        targets.append(
            EvalTarget(
                json_path=json_path,
                lean_path=lean_path,
                theorem_name=prompts.theorem_name,
                problem_json=problem_json,
            )
        )

    return targets, invalid_json, missing_lean


def _call_eval_model(
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


def _evaluate_one(
    target: EvalTarget,
    *,
    out_dir: Path,
    provider: str,
    eval_model: str,
    reasoning_effort: str,
    eval_repair_retries: int,
    api_key_env: str,
    openrouter_base_url: str,
    openrouter_timeout_s: int,
    openrouter_max_retries: int,
    codex_exec_sandbox: str,
    codex_workdir: Path,
) -> EvalOutcome:
    theorem_name = target.theorem_name
    eval_path = out_dir / f"{theorem_name}.eval.json"

    try:
        lean_code = target.lean_path.read_text(encoding="utf-8")
    except OSError as exc:
        payload = {"status": "read_failed", "error": str(exc)}
        _write_json(eval_path, payload)
        return EvalOutcome(
            theorem_name=theorem_name,
            json_path=target.json_path,
            lean_path=target.lean_path,
            eval_path=eval_path,
            status="read_failed",
            grade=None,
            error=str(exc),
        )

    base_prompt = _build_formalization_eval_prompt(
        problem_json=target.problem_json,
        theorem_name=theorem_name,
        lean_code=lean_code,
    )
    _write_text(out_dir / f"{theorem_name}.eval_prompt.txt", base_prompt)

    max_attempts = max(1, eval_repair_retries + 1)
    eval_prompt = base_prompt
    attempts: list[dict[str, object]] = []
    eval_payload: Optional[dict[str, object]] = None
    last_stdout = ""
    last_stderr = ""

    for attempt_no in range(1, max_attempts + 1):
        stdout_text, stderr_text, returncode = _call_eval_model(
            provider=provider,
            prompt=eval_prompt,
            model=eval_model,
            reasoning_effort=reasoning_effort,
            api_key_env=api_key_env,
            openrouter_base_url=openrouter_base_url,
            openrouter_timeout_s=openrouter_timeout_s,
            openrouter_max_retries=openrouter_max_retries,
            codex_exec_sandbox=codex_exec_sandbox,
            codex_workdir=codex_workdir,
            codex_message_path=out_dir / f"{theorem_name}.eval_attempt{attempt_no}.codex_last_message.log",
        )
        last_stdout = stdout_text
        last_stderr = stderr_text
        _write_text(out_dir / f"{theorem_name}.eval_attempt{attempt_no}_stdout.log", stdout_text)
        _write_text(out_dir / f"{theorem_name}.eval_attempt{attempt_no}_stderr.log", stderr_text)

        if returncode != 0:
            reason = stderr_text.strip() or "evaluation request failed"
            attempts.append({"attempt": attempt_no, "status": "request_failed", "error": reason})
            if attempt_no < max_attempts:
                eval_prompt = _build_eval_retry_prompt(
                    base_prompt=base_prompt,
                    failure_reason=reason,
                    previous_response_text=stdout_text,
                    retry_no=attempt_no + 1,
                )
                continue
            eval_payload = {"status": "request_failed", "error": reason}
            break

        try:
            eval_text = _extract_model_response_text(stdout_text)
            eval_obj = _parse_json_object_from_model_text(eval_text)
            normalized_eval = _parse_formalization_eval_payload(eval_obj)
            attempts.append({"attempt": attempt_no, "status": "ok"})
            eval_payload = {"status": "ok", **normalized_eval}
            break
        except ValueError as exc:
            reason = _format_eval_failure_reason(exc)
            attempts.append({"attempt": attempt_no, "status": "parse_failed", "error": reason})
            if attempt_no < max_attempts:
                eval_prompt = _build_eval_retry_prompt(
                    base_prompt=base_prompt,
                    failure_reason=reason,
                    previous_response_text=stdout_text,
                    retry_no=attempt_no + 1,
                )
                continue
            eval_payload = {"status": "parse_failed", "error": reason}
            break

    if eval_payload is None:
        eval_payload = {
            "status": "request_failed",
            "error": "evaluation finished without a result payload",
        }
    if attempts:
        eval_payload["attempt_count"] = len(attempts)
        eval_payload["attempts"] = attempts

    _write_text(out_dir / f"{theorem_name}.eval_stdout.log", last_stdout)
    _write_text(out_dir / f"{theorem_name}.eval_stderr.log", last_stderr)
    _write_json(eval_path, eval_payload)

    grade_obj = eval_payload.get("grade")
    grade = grade_obj if isinstance(grade_obj, str) else None
    status = str(eval_payload.get("status", "")).strip() or "unknown"
    error_obj = eval_payload.get("error")
    error = error_obj if isinstance(error_obj, str) else None
    return EvalOutcome(
        theorem_name=theorem_name,
        json_path=target.json_path,
        lean_path=target.lean_path,
        eval_path=eval_path,
        status=status,
        grade=grade,
        error=error,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate semantic fidelity for Lean formalizations using the same evaluation prompt "
            "and parsing rules as Autolean."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        required=True,
        help="Directory containing original problem JSON files.",
    )
    parser.add_argument(
        "--formalizations-dir",
        type=Path,
        required=True,
        help="Directory containing formalized Lean files (problem_<stem>.lean).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Directory where evaluation outputs and logs are written.",
    )
    parser.add_argument(
        "--provider",
        type=str,
        choices=["openrouter", "codex-exec"],
        default="codex-exec",
        help="Evaluator backend (default: codex-exec).",
    )
    parser.add_argument(
        "--eval-model",
        type=str,
        default="openai/gpt-5.2",
        help="Evaluator model id (default: openai/gpt-5.2).",
    )
    parser.add_argument(
        "--reasoning-effort",
        type=str,
        default="xhigh",
        choices=_REASONING_CHOICES,
        help="Reasoning effort for evaluator requests (default: xhigh).",
    )
    parser.add_argument(
        "--eval-repair-retries",
        type=int,
        default=2,
        help="Retries when evaluator output is malformed or unparseable (default: 2).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Parallel workers (default: 1).",
    )
    parser.add_argument(
        "--progress",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Show progress bar (default: true).",
    )
    parser.add_argument(
        "--openrouter-base-url",
        type=str,
        default="https://openrouter.ai/api/v1",
        help="OpenRouter API base URL.",
    )
    parser.add_argument(
        "--openrouter-api-key-env",
        type=str,
        default="PRINCIPIA_KEY",
        help="OpenRouter API key env var name (default: PRINCIPIA_KEY).",
    )
    parser.add_argument(
        "--api-key-name",
        type=str,
        choices=["PRINCIPIA_KEY", "AUTOLEAN"],
        default=None,
        help=(
            "Convenience selector for API key env var. Overrides --openrouter-api-key-env "
            "when provided."
        ),
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
        help="OpenRouter transport/HTTP retries.",
    )
    parser.add_argument(
        "--codex-exec-sandbox",
        type=str,
        default="read-only",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="Sandbox passed to codex exec.",
    )
    parser.add_argument(
        "--codex-workdir",
        type=Path,
        default=Path("."),
        help="Working directory used by codex exec (default: current directory).",
    )
    parser.add_argument(
        "--strict-missing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Treat missing Lean files / invalid JSON as failure (default: true).",
    )
    return parser


def _write_summary_report(
    *,
    out_dir: Path,
    outcomes: list[EvalOutcome],
    invalid_json: list[Path],
    missing_lean: list[Path],
    strict_missing: bool,
) -> None:
    ok = [x for x in outcomes if x.status == "ok"]
    non_ok = [x for x in outcomes if x.status != "ok"]

    by_grade: dict[str, int] = {}
    for item in ok:
        if item.grade is None:
            continue
        by_grade[item.grade] = by_grade.get(item.grade, 0) + 1

    payload = {
        "evaluated": len(outcomes),
        "ok": len(ok),
        "non_ok": len(non_ok),
        "invalid_json": len(invalid_json),
        "missing_lean": len(missing_lean),
        "strict_missing": strict_missing,
        "grade_counts": by_grade,
        "failures": [
            {
                "theorem_name": item.theorem_name,
                "status": item.status,
                "error": item.error,
                "eval_path": item.eval_path.as_posix(),
                "lean_path": item.lean_path.as_posix(),
                "json_path": item.json_path.as_posix(),
            }
            for item in non_ok
        ],
        "missing_lean_paths": [p.as_posix() for p in missing_lean],
        "invalid_json_paths": [p.as_posix() for p in invalid_json],
    }
    _write_json(out_dir / "evaluation_summary.json", payload)

    lines = [
        "Autolean Evaluation Report",
        f"Evaluated Lean files: {len(outcomes)}",
        f"Successful evals (status=ok): {len(ok)}",
        f"Non-ok evals: {len(non_ok)}",
        f"Missing Lean files: {len(missing_lean)}",
        f"Invalid JSON files: {len(invalid_json)}",
        f"Strict missing: {strict_missing}",
        "",
        "Grade counts:",
    ]
    if by_grade:
        for grade in sorted(by_grade):
            lines.append(f"- {grade}: {by_grade[grade]}")
    else:
        lines.append("- none")

    if non_ok:
        lines += ["", "Non-ok evaluations:"]
        for item in non_ok:
            lines.append(
                f"- {item.theorem_name}: status={item.status}"
                + (f", error={item.error}" if item.error else "")
            )
    else:
        lines += ["", "Non-ok evaluations: none"]

    if missing_lean:
        lines += ["", "Missing Lean files:"]
        for path in missing_lean:
            lines.append(f"- {path.as_posix()}")
    if invalid_json:
        lines += ["", "Invalid JSON files:"]
        for path in invalid_json:
            lines.append(f"- {path.as_posix()}")

    _write_text(out_dir / "evaluation_report.txt", "\n".join(lines).rstrip() + "\n")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    input_dir = args.input_dir.resolve()
    formalizations_dir = args.formalizations_dir.resolve()
    out_dir = args.out_dir.resolve()
    codex_workdir = args.codex_workdir.resolve()

    if not input_dir.is_dir():
        print(f"Error: input directory does not exist: {input_dir}", file=sys.stderr)
        return 2
    if not formalizations_dir.is_dir():
        print(f"Error: formalizations directory does not exist: {formalizations_dir}", file=sys.stderr)
        return 2
    if not codex_workdir.is_dir():
        print(f"Error: codex workdir does not exist: {codex_workdir}", file=sys.stderr)
        return 2
    if args.workers < 1:
        print("Error: --workers must be >= 1.", file=sys.stderr)
        return 2
    if args.eval_repair_retries < 0:
        print("Error: --eval-repair-retries must be >= 0.", file=sys.stderr)
        return 2

    ensure_dir(out_dir)

    api_key_env = str(args.api_key_name) if args.api_key_name is not None else str(args.openrouter_api_key_env)

    try:
        targets, invalid_json, missing_lean = _load_targets(
            input_dir=input_dir,
            formalizations_dir=formalizations_dir,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    outcomes: list[EvalOutcome] = []
    total = len(targets)
    progress_print = _make_progress_printer() if args.progress else None
    if progress_print is not None:
        progress_print(_format_progress(0, total))

    if args.workers <= 1:
        completed = 0
        for target in targets:
            outcome = _evaluate_one(
                target,
                out_dir=out_dir,
                provider=args.provider,
                eval_model=args.eval_model,
                reasoning_effort=args.reasoning_effort,
                eval_repair_retries=args.eval_repair_retries,
                api_key_env=api_key_env,
                openrouter_base_url=args.openrouter_base_url,
                openrouter_timeout_s=args.openrouter_timeout_s,
                openrouter_max_retries=args.openrouter_max_retries,
                codex_exec_sandbox=args.codex_exec_sandbox,
                codex_workdir=codex_workdir,
            )
            outcomes.append(outcome)
            completed += 1
            if progress_print is not None:
                progress_print(_format_progress(completed, total))
    else:
        completed = 0
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_map = {
                executor.submit(
                    _evaluate_one,
                    target,
                    out_dir=out_dir,
                    provider=args.provider,
                    eval_model=args.eval_model,
                    reasoning_effort=args.reasoning_effort,
                    eval_repair_retries=args.eval_repair_retries,
                    api_key_env=api_key_env,
                    openrouter_base_url=args.openrouter_base_url,
                    openrouter_timeout_s=args.openrouter_timeout_s,
                    openrouter_max_retries=args.openrouter_max_retries,
                    codex_exec_sandbox=args.codex_exec_sandbox,
                    codex_workdir=codex_workdir,
                ): target
                for target in targets
            }
            for future in as_completed(future_map):
                target = future_map[future]
                try:
                    outcome = future.result()
                except Exception as exc:
                    eval_path = out_dir / f"{target.theorem_name}.eval.json"
                    payload = {"status": "runner_failed", "error": str(exc)}
                    _write_json(eval_path, payload)
                    outcome = EvalOutcome(
                        theorem_name=target.theorem_name,
                        json_path=target.json_path,
                        lean_path=target.lean_path,
                        eval_path=eval_path,
                        status="runner_failed",
                        grade=None,
                        error=str(exc),
                    )
                outcomes.append(outcome)
                completed += 1
                if progress_print is not None:
                    progress_print(_format_progress(completed, total))

    outcomes.sort(key=lambda x: x.theorem_name)
    _write_summary_report(
        out_dir=out_dir,
        outcomes=outcomes,
        invalid_json=invalid_json,
        missing_lean=missing_lean,
        strict_missing=bool(args.strict_missing),
    )

    ok_count = sum(1 for x in outcomes if x.status == "ok")
    non_ok_count = len(outcomes) - ok_count
    print(f"Input JSON dir: {input_dir.as_posix()}")
    print(f"Formalizations dir: {formalizations_dir.as_posix()}")
    print(f"Output eval dir: {out_dir.as_posix()}")
    print(f"Provider: {args.provider}")
    print(f"Eval model: {args.eval_model}")
    print(f"Reasoning effort: {args.reasoning_effort}")
    print(f"API key env: {api_key_env}")
    print(f"Evaluated Lean files: {len(outcomes)}")
    print(f"Successful evals (status=ok): {ok_count}")
    print(f"Non-ok evals: {non_ok_count}")
    print(f"Missing Lean files: {len(missing_lean)}")
    print(f"Invalid JSON files: {len(invalid_json)}")
    print(f"Summary JSON: {(out_dir / 'evaluation_summary.json').as_posix()}")
    print(f"Summary text: {(out_dir / 'evaluation_report.txt').as_posix()}")

    has_failures = (
        non_ok_count > 0
        or (bool(args.strict_missing) and (len(missing_lean) > 0 or len(invalid_json) > 0))
    )
    return 1 if has_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
