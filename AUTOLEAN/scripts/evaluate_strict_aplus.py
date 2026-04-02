#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from autolean.core import (  # noqa: E402
    _call_openrouter_chat,
    _extract_model_response_text,
    _format_eval_failure_reason,
    _parse_json_object_from_model_text,
)
from autolean.prompting import build_prompts  # noqa: E402
from autolean.util import ensure_dir  # noqa: E402


_REASONING_CHOICES = ["minimal", "low", "medium", "high", "xhigh"]
_ITER_EVAL_RE = re.compile(r"^(?P<name>.+)\.iter(?P<iter>\d+)\.eval\.json$")


@dataclasses.dataclass(frozen=True)
class PriorEvalRef:
    path: Path
    kind: str
    status: str
    grade: Optional[str]


@dataclasses.dataclass(frozen=True)
class PriorEvalLookup:
    ref: Optional[PriorEvalRef]
    reason: str
    latest_seen_path: Optional[Path]
    latest_seen_status: Optional[str]
    latest_seen_grade: Optional[str]


@dataclasses.dataclass(frozen=True)
class StrictEvalTarget:
    json_path: Path
    relative_json_path: Path
    relative_parent: Path
    lean_path: Path
    theorem_name: str
    problem_json: dict
    prior_eval: PriorEvalRef


@dataclasses.dataclass(frozen=True)
class StrictEvalOutcome:
    theorem_name: str
    json_path: Path
    lean_path: Path
    eval_path: Path
    status: str
    grade: Optional[str]
    error: Optional[str]
    prior_eval_kind: str


def _format_progress(completed: int, total: int, *, label: str = "Strict A+ Eval") -> str:
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


def _iter_problem_json_files(input_dir: Path) -> list[Path]:
    return sorted(path for path in input_dir.rglob("*.json") if path.is_file())


def _iter_prior_eval_dirs(
    *,
    formalizations_dir: Path,
    relative_parent: Path,
    explicit_prior_eval_dir: Optional[Path],
) -> list[Path]:
    dirs: list[Path] = []
    if explicit_prior_eval_dir is not None:
        dirs.extend(
            [
                explicit_prior_eval_dir / relative_parent,
                explicit_prior_eval_dir,
            ]
        )
    else:
        dirs.extend(
            [
                formalizations_dir / relative_parent / "logs",
                formalizations_dir / relative_parent,
                formalizations_dir / "logs",
                formalizations_dir,
            ]
        )

    seen: set[Path] = set()
    out: list[Path] = []
    for path in dirs:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(path)
    return out


def _iter_eval_paths(eval_dir: Path, theorem_name: str) -> list[Path]:
    paths: list[Path] = []
    bare = eval_dir / f"{theorem_name}.eval.json"
    if bare.is_file():
        paths.append(bare)
    paths.extend(
        path
        for path in eval_dir.glob(f"{theorem_name}.iter*.eval.json")
        if path.is_file()
    )
    return sorted(set(paths))


def _read_eval_payload(eval_path: Path) -> Optional[dict]:
    try:
        payload = json.loads(eval_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _extract_eval_grade(payload: dict) -> Optional[str]:
    raw_grade = payload.get("grade")
    if not isinstance(raw_grade, str):
        return None
    grade = raw_grade.strip().upper()
    return grade or None


def _classify_prior_eval(payload: dict) -> Optional[str]:
    grade = _extract_eval_grade(payload)
    status = str(payload.get("status", "")).strip()

    double_check_obj = payload.get("double_check")
    if isinstance(double_check_obj, dict):
        primary = double_check_obj.get("primary")
        secondary = double_check_obj.get("secondary")
        primary_ok = (
            isinstance(primary, dict)
            and str(primary.get("status", "")).strip() == "ok"
            and _extract_eval_grade(primary) == "A"
        )
        secondary_ok = (
            isinstance(secondary, dict)
            and str(secondary.get("status", "")).strip() == "ok"
            and _extract_eval_grade(secondary) == "A"
        )
        if bool(double_check_obj.get("both_a_pass")) and primary_ok and secondary_ok:
            return "double_A"

    if status == "ok" and grade == "A":
        return "A"
    return None


def _pick_latest_eval(eval_paths: list[Path]) -> tuple[Optional[Path], Optional[dict]]:
    candidates: list[tuple[int, int, int, str, Path, dict]] = []
    for path in eval_paths:
        payload = _read_eval_payload(path)
        if payload is None:
            continue
        bare_rank = 1 if path.name.endswith(".eval.json") and ".iter" not in path.name else 0
        iter_no = -1
        m = _ITER_EVAL_RE.match(path.name)
        if m is not None:
            try:
                iter_no = int(m.group("iter"))
            except ValueError:
                iter_no = -1
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
        candidates.append((mtime_ns, bare_rank, iter_no, path.as_posix(), path, payload))

    if not candidates:
        return None, None

    _mtime_ns, _bare_rank, _iter_no, _path_key, path, payload = max(candidates)
    return path, payload


def _lookup_prior_eval(
    *,
    theorem_name: str,
    formalizations_dir: Path,
    relative_parent: Path,
    explicit_prior_eval_dir: Optional[Path],
) -> PriorEvalLookup:
    for eval_dir in _iter_prior_eval_dirs(
        formalizations_dir=formalizations_dir,
        relative_parent=relative_parent,
        explicit_prior_eval_dir=explicit_prior_eval_dir,
    ):
        if not eval_dir.is_dir():
            continue
        eval_paths = _iter_eval_paths(eval_dir, theorem_name)
        if not eval_paths:
            continue

        latest_path, latest_payload = _pick_latest_eval(eval_paths)
        if latest_path is None or latest_payload is None:
            return PriorEvalLookup(
                ref=None,
                reason=f"Found eval artifacts in {eval_dir.as_posix()}, but none were readable JSON.",
                latest_seen_path=None,
                latest_seen_status=None,
                latest_seen_grade=None,
            )

        latest_status = str(latest_payload.get("status", "")).strip() or None
        latest_grade = _extract_eval_grade(latest_payload)
        kind = _classify_prior_eval(latest_payload)
        if kind is not None:
            return PriorEvalLookup(
                ref=PriorEvalRef(
                    path=latest_path,
                    kind=kind,
                    status=latest_status or "",
                    grade=latest_grade,
                ),
                reason=f"Eligible prior eval found in {latest_path.as_posix()}",
                latest_seen_path=latest_path,
                latest_seen_status=latest_status,
                latest_seen_grade=latest_grade,
            )

        grade_text = latest_grade or "none"
        status_text = latest_status or "unknown"
        return PriorEvalLookup(
            ref=None,
            reason=(
                f"Latest prior eval in {eval_dir.as_posix()} was not eligible: "
                f"status={status_text}, grade={grade_text}."
            ),
            latest_seen_path=latest_path,
            latest_seen_status=latest_status,
            latest_seen_grade=latest_grade,
        )

    return PriorEvalLookup(
        ref=None,
        reason="No prior eval artifact found.",
        latest_seen_path=None,
        latest_seen_status=None,
        latest_seen_grade=None,
    )


def _build_strict_aplus_prompt(
    *,
    problem_json: dict,
    theorem_name: str,
    lean_code: str,
    prior_eval_kind: str,
) -> str:
    json_blob = json.dumps(problem_json, ensure_ascii=False, indent=2)
    return f"""You are running a strict second-pass exactness audit of a Lean formalization.

This file has already passed an earlier semantic screen with prior status: {prior_eval_kind}.
Your job is stricter than the normal A/B/C/D evaluator.

Important scope:
- Evaluate ONLY the theorem statement semantics against the original problem.
- Ignore proof quality, proof style, proof length, and proof elegance.
- Compare the original problem requirements to the Lean theorem proposition.

Strict A+ bar:
- Return grade "A+" ONLY if the Lean theorem is exactly the same as the original problem with no semantic difference at all.
- If there is any tiny difference, even if the usual evaluator would still call it A, you must return grade "A", not "A+".
- Tiny differences include, but are not limited to: broader or narrower domains, reordered or rescaled quantifiers, omitted side conditions, extra assumptions, extra conclusions, vacuous empty-interval encodings, total-function vs restricted-domain mismatches, bundled/unbundled changes that alter the exact statement, implicit coercions that broaden the claim, or any representational slack that should be tightened to match the source exactly.

Required checklist:
1) Exact core mathematical objects and domains/types.
2) Exact quantifier order, scope, and dependency structure.
3) Exact hypotheses and side conditions, with nothing omitted or added.
4) Exact conclusion/claim, with no strengthening, weakening, or rephrasing that changes meaning.
5) Exact coverage of every sub-question and only those sub-questions.
6) No tiny representational gap that should still be edited before calling it exact.

Decision rule:
1) Use "A+" if and only if every checklist item passes with literally nothing to change.
2) Otherwise use "A".

Return ONLY a JSON object:
{{
  "grade": "A+|A",
  "exact_match": true|false,
  "summary": "<1-3 sentence verdict>",
  "difference_from_exact": "<'None' if exact, otherwise concise explanation>",
  "tiny_differences": ["<every small difference>", "<every small difference>"],
  "required_changes_for_exact_match": ["<minimal edit needed>", "<minimal edit needed>"],
  "ordinary_A_consistent": true
}}

Hard output constraints:
- Must be strict RFC8259 JSON.
- No markdown/code fences/explanations outside the JSON object.
- Never use grades B/C/D in this task.
- If grade is "A+", then exact_match must be true and both difference lists must be empty.
- If grade is "A", then exact_match must be false and you must list the differences and changes needed.

Original problem JSON (authoritative):
{json_blob}

Lean theorem target name: {theorem_name}

Lean file content:
```lean
{lean_code}
```"""


def _build_retry_prompt(
    *,
    base_prompt: str,
    failure_reason: str,
    previous_response_text: str,
    retry_no: int,
) -> str:
    snippet = previous_response_text.strip()
    if len(snippet) > 2400:
        snippet = snippet[:2400] + "\n...[truncated]"
    return (
        base_prompt
        + "\n\nThe previous strict A+ evaluation response could not be accepted.\n"
        + f"Failure reason: {failure_reason}\n"
        + f"Retry number: {retry_no}\n"
        + "Please regenerate and return ONLY valid JSON that matches the exact schema.\n"
        + "Do not include markdown/code fences/explanations.\n\n"
        + "Previous invalid response (for debugging):\n"
        + snippet
    )


def _to_str_list(value: object, *, limit: int = 12) -> list[str]:
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


def _parse_strict_aplus_payload(payload: dict) -> dict[str, object]:
    raw_grade = payload.get("grade")
    if not isinstance(raw_grade, str):
        raise ValueError("Strict A+ output missing 'grade' field.")
    grade = raw_grade.strip().upper()
    if grade not in {"A", "A+"}:
        raise ValueError("Strict A+ grade must be one of A/A+.")

    exact_match_obj = payload.get("exact_match")
    if exact_match_obj is None:
        exact_match = grade == "A+"
    elif isinstance(exact_match_obj, bool):
        exact_match = exact_match_obj
    else:
        raise ValueError("Strict A+ 'exact_match' must be boolean when present.")

    summary = ""
    summary_obj = payload.get("summary")
    if isinstance(summary_obj, str) and summary_obj.strip():
        summary = summary_obj.strip()

    difference_from_exact = ""
    for key in ("difference_from_exact", "distance_from_exact", "distance_from_original"):
        candidate = payload.get(key)
        if isinstance(candidate, str) and candidate.strip():
            difference_from_exact = candidate.strip()
            break

    tiny_differences: list[str] = []
    for key in ("tiny_differences", "differences", "key_differences", "key_mismatches"):
        items = _to_str_list(payload.get(key))
        if items:
            tiny_differences = items
            break

    required_changes: list[str] = []
    for key in (
        "required_changes_for_exact_match",
        "changes_needed",
        "minimal_changes",
        "required_edits",
    ):
        items = _to_str_list(payload.get(key))
        if items:
            required_changes = items
            break

    ordinary_A_consistent = True
    ordinary_A_obj = payload.get("ordinary_A_consistent")
    if ordinary_A_obj is not None:
        if not isinstance(ordinary_A_obj, bool):
            raise ValueError("Strict A+ 'ordinary_A_consistent' must be boolean when present.")
        ordinary_A_consistent = ordinary_A_obj

    if grade == "A+":
        if not exact_match:
            raise ValueError("Strict A+ payload inconsistent: grade A+ requires exact_match=true.")
        if tiny_differences or required_changes:
            raise ValueError("Strict A+ payload inconsistent: grade A+ cannot list differences/changes.")
    else:
        if exact_match:
            raise ValueError("Strict A+ payload inconsistent: grade A requires exact_match=false.")
        if not difference_from_exact and not tiny_differences and not required_changes:
            raise ValueError(
                "Strict A payload must describe the differences or required changes for exactness."
            )

    normalized: dict[str, object] = {
        "grade": grade,
        "exact_match": exact_match,
        "ordinary_A_consistent": ordinary_A_consistent,
    }
    if summary:
        normalized["summary"] = summary
    if difference_from_exact:
        normalized["difference_from_exact"] = difference_from_exact
    if tiny_differences:
        normalized["tiny_differences"] = tiny_differences
    if required_changes:
        normalized["required_changes_for_exact_match"] = required_changes
    return normalized


def _load_targets(
    *,
    input_dir: Path,
    formalizations_dir: Path,
    explicit_prior_eval_dir: Optional[Path],
) -> tuple[
    list[StrictEvalTarget],
    list[Path],
    list[Path],
    list[dict[str, object]],
]:
    json_files = _iter_problem_json_files(input_dir)
    if not json_files:
        raise ValueError(f"No JSON problem files found in: {input_dir.as_posix()}")

    targets: list[StrictEvalTarget] = []
    invalid_json: list[Path] = []
    missing_lean: list[Path] = []
    skipped_non_eligible: list[dict[str, object]] = []

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

        relative_json_path = json_path.relative_to(input_dir)
        relative_parent = relative_json_path.parent
        theorem_name = prompts.theorem_name
        lean_path = formalizations_dir / relative_parent / f"{theorem_name}.lean"
        if not lean_path.exists():
            missing_lean.append(lean_path)
            continue

        lookup = _lookup_prior_eval(
            theorem_name=theorem_name,
            formalizations_dir=formalizations_dir,
            relative_parent=relative_parent,
            explicit_prior_eval_dir=explicit_prior_eval_dir,
        )
        if lookup.ref is None:
            skipped_non_eligible.append(
                {
                    "theorem_name": theorem_name,
                    "json_path": json_path.as_posix(),
                    "lean_path": lean_path.as_posix(),
                    "reason": lookup.reason,
                    "latest_seen_path": lookup.latest_seen_path.as_posix()
                    if lookup.latest_seen_path is not None
                    else None,
                    "latest_seen_status": lookup.latest_seen_status,
                    "latest_seen_grade": lookup.latest_seen_grade,
                }
            )
            continue

        targets.append(
            StrictEvalTarget(
                json_path=json_path,
                relative_json_path=relative_json_path,
                relative_parent=relative_parent,
                lean_path=lean_path,
                theorem_name=theorem_name,
                problem_json=problem_json,
                prior_eval=lookup.ref,
            )
        )

    return targets, invalid_json, missing_lean, skipped_non_eligible


def _evaluate_one(
    target: StrictEvalTarget,
    *,
    out_root: Path,
    eval_model: str,
    reasoning_effort: str,
    eval_repair_retries: int,
    api_key_env: str,
    openrouter_base_url: str,
    openrouter_timeout_s: int,
    openrouter_max_retries: int,
    overwrite: bool,
) -> StrictEvalOutcome:
    out_dir = out_root / target.relative_parent
    eval_path = out_dir / f"{target.theorem_name}.eval.json"

    if eval_path.exists() and not overwrite:
        payload = _read_eval_payload(eval_path)
        if isinstance(payload, dict) and str(payload.get("status", "")).strip() == "ok":
            grade = _extract_eval_grade(payload)
            error_obj = payload.get("error")
            error = error_obj if isinstance(error_obj, str) else None
            return StrictEvalOutcome(
                theorem_name=target.theorem_name,
                json_path=target.json_path,
                lean_path=target.lean_path,
                eval_path=eval_path,
                status="ok",
                grade=grade,
                error=error,
                prior_eval_kind=target.prior_eval.kind,
            )

    try:
        lean_code = target.lean_path.read_text(encoding="utf-8")
    except OSError as exc:
        payload = {
            "status": "read_failed",
            "error": str(exc),
            "prior_eval_kind": target.prior_eval.kind,
            "prior_eval_path": target.prior_eval.path.as_posix(),
        }
        _write_json(eval_path, payload)
        return StrictEvalOutcome(
            theorem_name=target.theorem_name,
            json_path=target.json_path,
            lean_path=target.lean_path,
            eval_path=eval_path,
            status="read_failed",
            grade=None,
            error=str(exc),
            prior_eval_kind=target.prior_eval.kind,
        )

    base_prompt = _build_strict_aplus_prompt(
        problem_json=target.problem_json,
        theorem_name=target.theorem_name,
        lean_code=lean_code,
        prior_eval_kind=target.prior_eval.kind,
    )
    _write_text(out_dir / f"{target.theorem_name}.eval_prompt.txt", base_prompt)

    max_attempts = max(1, eval_repair_retries + 1)
    eval_prompt = base_prompt
    attempts: list[dict[str, object]] = []
    eval_payload: Optional[dict[str, object]] = None
    last_stdout = ""
    last_stderr = ""

    for attempt_no in range(1, max_attempts + 1):
        res = _call_openrouter_chat(
            prompt=eval_prompt,
            model=eval_model,
            base_url=openrouter_base_url,
            api_key_env=api_key_env,
            timeout_s=openrouter_timeout_s,
            max_retries=openrouter_max_retries,
            reasoning_effort=reasoning_effort,
            openrouter_web_search=False,
            openrouter_web_search_engine=None,
            openrouter_web_search_max_results=None,
        )
        last_stdout = res.stdout
        last_stderr = res.stderr
        _write_text(out_dir / f"{target.theorem_name}.eval_attempt{attempt_no}_stdout.log", res.stdout)
        _write_text(out_dir / f"{target.theorem_name}.eval_attempt{attempt_no}_stderr.log", res.stderr)

        if res.returncode != 0:
            reason = res.stderr.strip() or "strict A+ evaluation request failed"
            attempts.append({"attempt": attempt_no, "status": "request_failed", "error": reason})
            if attempt_no < max_attempts:
                eval_prompt = _build_retry_prompt(
                    base_prompt=base_prompt,
                    failure_reason=reason,
                    previous_response_text=res.stdout,
                    retry_no=attempt_no + 1,
                )
                continue
            eval_payload = {"status": "request_failed", "error": reason}
            break

        try:
            eval_text = _extract_model_response_text(res.stdout)
            eval_obj = _parse_json_object_from_model_text(eval_text)
            normalized_eval = _parse_strict_aplus_payload(eval_obj)
            attempts.append({"attempt": attempt_no, "status": "ok"})
            eval_payload = {"status": "ok", **normalized_eval}
            break
        except ValueError as exc:
            reason = _format_eval_failure_reason(exc)
            attempts.append({"attempt": attempt_no, "status": "parse_failed", "error": reason})
            if attempt_no < max_attempts:
                eval_prompt = _build_retry_prompt(
                    base_prompt=base_prompt,
                    failure_reason=reason,
                    previous_response_text=res.stdout,
                    retry_no=attempt_no + 1,
                )
                continue
            eval_payload = {"status": "parse_failed", "error": reason}
            break

    if eval_payload is None:
        eval_payload = {
            "status": "request_failed",
            "error": "strict A+ evaluation finished without a result payload",
        }
    if attempts:
        eval_payload["attempt_count"] = len(attempts)
        eval_payload["attempts"] = attempts
    eval_payload["prior_eval_kind"] = target.prior_eval.kind
    eval_payload["prior_eval_status"] = target.prior_eval.status
    eval_payload["prior_eval_grade"] = target.prior_eval.grade
    eval_payload["prior_eval_path"] = target.prior_eval.path.as_posix()

    _write_text(out_dir / f"{target.theorem_name}.eval_stdout.log", last_stdout)
    _write_text(out_dir / f"{target.theorem_name}.eval_stderr.log", last_stderr)
    _write_json(eval_path, eval_payload)

    grade = _extract_eval_grade(eval_payload)
    status = str(eval_payload.get("status", "")).strip() or "unknown"
    error_obj = eval_payload.get("error")
    error = error_obj if isinstance(error_obj, str) else None
    return StrictEvalOutcome(
        theorem_name=target.theorem_name,
        json_path=target.json_path,
        lean_path=target.lean_path,
        eval_path=eval_path,
        status=status,
        grade=grade,
        error=error,
        prior_eval_kind=target.prior_eval.kind,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a stricter A+ exactness audit on Lean formalizations that already passed "
            "a prior A or double-A semantic evaluation."
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
        help="Directory containing formalized Lean files.",
    )
    parser.add_argument(
        "--out-subdir",
        type=str,
        default="new_eval",
        help="Subfolder created under --formalizations-dir for strict A+ results (default: new_eval).",
    )
    parser.add_argument(
        "--prior-eval-dir",
        type=Path,
        default=None,
        help=(
            "Optional directory containing prior eval artifacts. Defaults to checking "
            "<formalizations-dir>/logs, then <formalizations-dir>."
        ),
    )
    parser.add_argument(
        "--eval-model",
        type=str,
        default="openai/gpt-5.4",
        help="Strict A+ evaluator model id (default: openai/gpt-5.4).",
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
        "--overwrite",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Overwrite existing strict A+ eval files in the output subfolder (default: false).",
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
        default="PRINCIPIA_KEY",
        help="Convenience selector for API key env var (default: PRINCIPIA_KEY).",
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
    return parser


def _write_summary_report(
    *,
    out_dir: Path,
    outcomes: list[StrictEvalOutcome],
    invalid_json: list[Path],
    missing_lean: list[Path],
    skipped_non_eligible: list[dict[str, object]],
) -> None:
    ok = [item for item in outcomes if item.status == "ok"]
    non_ok = [item for item in outcomes if item.status != "ok"]

    by_grade: dict[str, int] = {}
    by_prior_kind: dict[str, int] = {}
    for item in ok:
        if item.grade is not None:
            by_grade[item.grade] = by_grade.get(item.grade, 0) + 1
        by_prior_kind[item.prior_eval_kind] = by_prior_kind.get(item.prior_eval_kind, 0) + 1

    payload = {
        "eligible_targets": len(outcomes),
        "ok": len(ok),
        "non_ok": len(non_ok),
        "invalid_json": len(invalid_json),
        "missing_lean": len(missing_lean),
        "skipped_non_eligible": len(skipped_non_eligible),
        "grade_counts": by_grade,
        "prior_eval_kind_counts": by_prior_kind,
        "failures": [
            {
                "theorem_name": item.theorem_name,
                "status": item.status,
                "grade": item.grade,
                "error": item.error,
                "eval_path": item.eval_path.as_posix(),
                "lean_path": item.lean_path.as_posix(),
                "json_path": item.json_path.as_posix(),
                "prior_eval_kind": item.prior_eval_kind,
            }
            for item in non_ok
        ],
        "skipped_non_eligible_items": skipped_non_eligible,
        "missing_lean_paths": [path.as_posix() for path in missing_lean],
        "invalid_json_paths": [path.as_posix() for path in invalid_json],
    }
    _write_json(out_dir / "evaluation_summary.json", payload)

    lines = [
        "Autolean Strict A+ Evaluation Report",
        f"Eligible prior-A targets: {len(outcomes)}",
        f"Successful evals (status=ok): {len(ok)}",
        f"Non-ok evals: {len(non_ok)}",
        f"Skipped without prior A/double_A: {len(skipped_non_eligible)}",
        f"Missing Lean files: {len(missing_lean)}",
        f"Invalid JSON files: {len(invalid_json)}",
        "",
        "Grade counts:",
    ]
    if by_grade:
        for grade in sorted(by_grade):
            lines.append(f"- {grade}: {by_grade[grade]}")
    else:
        lines.append("- none")

    lines += ["", "Prior eval kind counts:"]
    if by_prior_kind:
        for kind in sorted(by_prior_kind):
            lines.append(f"- {kind}: {by_prior_kind[kind]}")
    else:
        lines.append("- none")

    if non_ok:
        lines += ["", "Non-ok evaluations:"]
        for item in non_ok:
            lines.append(
                f"- {item.theorem_name}: status={item.status}"
                + (f", grade={item.grade}" if item.grade else "")
                + (f", error={item.error}" if item.error else "")
            )

    a_only: list[tuple[str, dict]] = []
    for eval_path in out_dir.rglob("*.eval.json"):
        payload = _read_eval_payload(eval_path)
        if not isinstance(payload, dict):
            continue
        if str(payload.get("status", "")).strip() != "ok":
            continue
        if _extract_eval_grade(payload) != "A":
            continue
        a_only.append((eval_path.stem.removesuffix(".eval"), payload))

    if a_only:
        lines += ["", "A but not A+:"]
        for theorem_name, payload in sorted(a_only, key=lambda pair: pair[0]):
            diffs = _to_str_list(payload.get("tiny_differences"))
            changes = _to_str_list(payload.get("required_changes_for_exact_match"))
            summary = payload.get("difference_from_exact")
            detail = ""
            if isinstance(summary, str) and summary.strip():
                detail = summary.strip()
            elif diffs:
                detail = diffs[0]
            elif changes:
                detail = changes[0]
            lines.append(
                f"- {theorem_name}"
                + (f": {detail}" if detail else "")
            )

    if skipped_non_eligible:
        lines += ["", "Skipped without prior A/double_A:"]
        for item in skipped_non_eligible[:50]:
            theorem_name = str(item.get("theorem_name", "unknown"))
            reason = str(item.get("reason", "")).strip()
            lines.append(f"- {theorem_name}: {reason}")
        if len(skipped_non_eligible) > 50:
            lines.append(f"- ... {len(skipped_non_eligible) - 50} more")

    _write_text(out_dir / "evaluation_report.txt", "\n".join(lines).rstrip() + "\n")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    input_dir = args.input_dir.resolve()
    formalizations_dir = args.formalizations_dir.resolve()
    out_dir = (formalizations_dir / args.out_subdir).resolve()
    prior_eval_dir = args.prior_eval_dir.resolve() if args.prior_eval_dir is not None else None

    if not input_dir.is_dir():
        print(f"Error: input directory does not exist: {input_dir}", file=sys.stderr)
        return 2
    if not formalizations_dir.is_dir():
        print(f"Error: formalizations directory does not exist: {formalizations_dir}", file=sys.stderr)
        return 2
    if prior_eval_dir is not None and not prior_eval_dir.is_dir():
        print(f"Error: prior eval directory does not exist: {prior_eval_dir}", file=sys.stderr)
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
        targets, invalid_json, missing_lean, skipped_non_eligible = _load_targets(
            input_dir=input_dir,
            formalizations_dir=formalizations_dir,
            explicit_prior_eval_dir=prior_eval_dir,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    outcomes: list[StrictEvalOutcome] = []
    total = len(targets)
    progress_print = _make_progress_printer() if args.progress else None
    if progress_print is not None:
        progress_print(_format_progress(0, total))

    if args.workers <= 1:
        completed = 0
        for target in targets:
            outcome = _evaluate_one(
                target,
                out_root=out_dir,
                eval_model=args.eval_model,
                reasoning_effort=args.reasoning_effort,
                eval_repair_retries=args.eval_repair_retries,
                api_key_env=api_key_env,
                openrouter_base_url=args.openrouter_base_url,
                openrouter_timeout_s=args.openrouter_timeout_s,
                openrouter_max_retries=args.openrouter_max_retries,
                overwrite=bool(args.overwrite),
            )
            outcomes.append(outcome)
            completed += 1
            if progress_print is not None:
                progress_print(_format_progress(completed, total))
    else:
        completed = 0
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_to_target = {
                executor.submit(
                    _evaluate_one,
                    target,
                    out_root=out_dir,
                    eval_model=args.eval_model,
                    reasoning_effort=args.reasoning_effort,
                    eval_repair_retries=args.eval_repair_retries,
                    api_key_env=api_key_env,
                    openrouter_base_url=args.openrouter_base_url,
                    openrouter_timeout_s=args.openrouter_timeout_s,
                    openrouter_max_retries=args.openrouter_max_retries,
                    overwrite=bool(args.overwrite),
                ): target
                for target in targets
            }
            for future in as_completed(future_to_target):
                outcomes.append(future.result())
                completed += 1
                if progress_print is not None:
                    progress_print(_format_progress(completed, total))

    outcomes.sort(key=lambda item: item.theorem_name)
    _write_summary_report(
        out_dir=out_dir,
        outcomes=outcomes,
        invalid_json=invalid_json,
        missing_lean=missing_lean,
        skipped_non_eligible=skipped_non_eligible,
    )

    if progress_print is not None:
        progress_print(_format_progress(total, total), done=True)

    ok_count = sum(1 for item in outcomes if item.status == "ok")
    non_ok_count = len(outcomes) - ok_count
    print(f"Input dir: {input_dir.as_posix()}")
    print(f"Formalizations dir: {formalizations_dir.as_posix()}")
    print(f"Strict eval output dir: {out_dir.as_posix()}")
    print(f"Eligible prior-A targets: {len(outcomes)}")
    print(f"Skipped without prior A/double_A: {len(skipped_non_eligible)}")
    print(f"Successful evals (status=ok): {ok_count}")
    print(f"Non-ok evals: {non_ok_count}")
    print(f"Summary JSON: {(out_dir / 'evaluation_summary.json').as_posix()}")
    print(f"Summary text: {(out_dir / 'evaluation_report.txt').as_posix()}")

    if non_ok_count or invalid_json or missing_lean:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
