from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .core import RunConfig, iter_problem_files, process_problem_file
from .prompting import build_prompts
from .util import ensure_dir

_SUBQUESTION_SUFFIX_RE = re.compile(r"^(?P<root>.+)_(?P<index>\d+)$")
_EVAL_GRADE_ORDER = {"A": 4, "B": 3, "C": 2, "D": 1}
_EVAL_GRADES = set(_EVAL_GRADE_ORDER)
_OPENROUTER_GEMINI_FLASH_PREVIEW_MODEL = "google/gemini-3-flash-preview"


@dataclass(frozen=True)
class _ProblemTask:
    json_path: Path
    prior_json_paths: tuple[Path, ...]
    required_min_eval_grade: Optional[str] = None


@dataclass(frozen=True)
class _ProblemUnit:
    tasks: tuple[_ProblemTask, ...]
    consumed_count: int
    preflight_error: Optional[str] = None


def _format_progress(completed: int, total: int, *, label: str = "Progress") -> str:
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


def _grade_below_threshold(grade: str, min_grade: str) -> bool:
    g = _EVAL_GRADE_ORDER.get(grade.upper(), 0)
    t = _EVAL_GRADE_ORDER.get(min_grade.upper(), 0)
    return g < t


def _load_latest_eval_grade(eval_dir: Path, theorem_name: str) -> Optional[str]:
    pattern = re.compile(rf"^{re.escape(theorem_name)}\.iter(\d+)\.eval\.json$")
    best_iter = -1
    best_grade: Optional[str] = None

    for eval_path in eval_dir.glob(f"{theorem_name}.iter*.eval.json"):
        m = pattern.match(eval_path.name)
        if m is None:
            continue
        try:
            iter_no = int(m.group(1))
        except ValueError:
            continue
        try:
            payload = json.loads(eval_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        if not isinstance(payload, dict):
            continue
        if str(payload.get("status", "")).strip() != "ok":
            continue
        raw_grade = payload.get("grade")
        if not isinstance(raw_grade, str):
            continue
        grade = raw_grade.strip().upper()
        if grade not in _EVAL_GRADES:
            continue

        if iter_no > best_iter:
            best_iter = iter_no
            best_grade = grade

    return best_grade


def _has_any_eval_artifact(eval_dir: Path, theorem_name: str) -> bool:
    if (eval_dir / f"{theorem_name}.eval.json").exists():
        return True
    for eval_path in eval_dir.glob(f"{theorem_name}.iter*.eval.json"):
        if eval_path.is_file():
            return True
    return False


def _output_is_acceptable(
    cfg: RunConfig,
    lean_path: Path,
    *,
    repo_root: Path,
    theorem_name: Optional[str] = None,
    required_min_eval_grade: Optional[str] = None,
    eval_grade_dir: Optional[Path] = None,
) -> bool:
    if cfg.require_no_sorry:
        try:
            if "sorry" in lean_path.read_text(encoding="utf-8"):
                return False
        except OSError:
            return False

    run_cwd = cfg.cwd or repo_root
    try:
        proc = subprocess.run(
            cfg.compile_argv(lean_path),
            cwd=str(run_cwd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    if proc.returncode != 0:
        return False

    if required_min_eval_grade is None:
        return True
    if theorem_name is None:
        return False

    eval_dir = eval_grade_dir or cfg.logs_dir
    latest_grade = _load_latest_eval_grade(eval_dir, theorem_name)
    if latest_grade is None:
        return False
    return not _grade_below_threshold(latest_grade, required_min_eval_grade)


def _parse_subquestion_stem(stem: str) -> tuple[str, int] | None:
    m = _SUBQUESTION_SUFFIX_RE.fullmatch(stem)
    if m is None:
        return None
    try:
        index = int(m.group("index"))
    except ValueError:
        return None
    return m.group("root"), index


def _build_problem_units(
    problem_files: list[Path],
    *,
    multipart_min_eval_grade: Optional[str],
) -> list[_ProblemUnit]:
    grouped: dict[str, list[tuple[int, Path]]] = {}
    for path in problem_files:
        parsed = _parse_subquestion_stem(path.stem)
        if parsed is None:
            continue
        root, index = parsed
        grouped.setdefault(root, []).append((index, path))

    multipart_units: dict[str, _ProblemUnit] = {}
    invalid_units: dict[str, _ProblemUnit] = {}
    first_path_for_root: dict[str, Path] = {}
    skipped_subpaths: set[Path] = set()

    for root, items in grouped.items():
        ordered_items = sorted(items, key=lambda pair: pair[0])
        ordered_paths = [path for _idx, path in ordered_items]
        indices = [idx for idx, _path in ordered_items]
        first_path_for_root[root] = ordered_paths[0]

        max_index = indices[-1]
        missing = [i for i in range(1, max_index + 1) if i not in set(indices)]
        has_gap = bool(missing)

        # Single *_1 file is treated as a regular standalone problem.
        if len(indices) == 1 and indices[0] == 1:
            continue

        if has_gap:
            missing_names = ", ".join(f"{root}_{i}.json" for i in missing)
            invalid_units[root] = _ProblemUnit(
                tasks=tuple(),
                consumed_count=len(ordered_paths),
                preflight_error=(
                    f"Multipart chain '{root}' has missing sub-question files: {missing_names}. "
                    "Skipping this main question."
                )
            )
            skipped_subpaths.update(ordered_paths[1:])
            continue

        # If suffix appears but does not start at 1, it is also a gap from the chain start.
        if indices[0] != 1:
            invalid_units[root] = _ProblemUnit(
                tasks=tuple(),
                consumed_count=len(ordered_paths),
                preflight_error=(
                    f"Multipart chain '{root}' must start at {root}_1.json; found starts at "
                    f"{root}_{indices[0]}.json. Skipping this main question."
                ),
            )
            skipped_subpaths.update(ordered_paths[1:])
            continue

        tasks: list[_ProblemTask] = []
        for idx, path in enumerate(ordered_paths):
            tasks.append(
                _ProblemTask(
                    json_path=path,
                    prior_json_paths=tuple(ordered_paths[:idx]),
                    required_min_eval_grade=multipart_min_eval_grade,
                )
            )
        multipart_units[root] = _ProblemUnit(
            tasks=tuple(tasks),
            consumed_count=len(tasks),
            preflight_error=None,
        )
        skipped_subpaths.update(ordered_paths[1:])

    units: list[_ProblemUnit] = []
    for path in problem_files:
        if path in skipped_subpaths:
            continue
        parsed = _parse_subquestion_stem(path.stem)
        if parsed is not None:
            root, _index = parsed
            if first_path_for_root.get(root) == path:
                if root in invalid_units:
                    units.append(invalid_units[root])
                    continue
                unit = multipart_units.get(root)
                if unit is not None:
                    units.append(unit)
                    continue

            # If it's a single *_k with k>1, fail due missing predecessors.
            if _index > 1:
                units.append(
                    _ProblemUnit(
                        tasks=tuple(),
                        consumed_count=1,
                        preflight_error=(
                            f"Multipart chain '{root}' is missing predecessors for {path.name} "
                            "(expected at least "
                            f"{root}_1.json..{root}_{_index - 1}.json). Skipping this main question."
                        ),
                    )
                )
                continue

        units.append(
            _ProblemUnit(
                tasks=(
                    _ProblemTask(
                        json_path=path,
                        prior_json_paths=tuple(),
                        required_min_eval_grade=None,
                    ),
                ),
                consumed_count=1,
                preflight_error=None,
            )
        )

    return units


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="autolean",
        description="OpenRouter/Codex-Exec ↔ Lean formalization loop for JSON problem sets.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Process JSON problems and generate Lean files with compile-check-repair loop.")
    run.add_argument("--input", type=Path, default=Path("problems"), help="Input directory containing *.json.")
    run.add_argument("--output", type=Path, default=Path("Formalizations"), help="Output directory for *.lean files.")
    run.add_argument("--logs", type=Path, default=Path("logs"), help="Directory for logs.")
    run.add_argument("--max-iters", type=int, default=6, help="Max iterations per problem.")
    run.add_argument("--workers", type=int, default=1, help="Parallel workers (1 disables parallelism).")
    run.add_argument(
        "--progress",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Show a progress bar.",
    )
    run.add_argument(
        "--live-logs",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Stream compiler output to log files while running.",
    )
    run.add_argument(
        "--formalization-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Generate statement-only formalizations (theorem body should be placeholder `sorry`).",
    )
    run.add_argument("--require-no-sorry", action="store_true", help="Reject Lean outputs containing 'sorry'.")
    run.add_argument(
        "--openrouter-model",
        type=str,
        default="openai/gpt-5.2-codex",
        help="OpenRouter model identifier for phase 5.3 (coding).",
    )
    run.add_argument(
        "--openrouter-thinking-model",
        type=str,
        default=None,
        help="Optional OpenRouter model identifier for phase 5.2 (thinking, iteration 1 only).",
    )
    run.add_argument(
        "--openrouter-gemini-flash-preview",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Convenience switch for OpenRouter path: set both phase 5.2 thinking and "
            "phase 5.3 coding models to google/gemini-3-flash-preview."
        ),
    )
    run.add_argument(
        "--openrouter-thinking-reasoning-effort",
        type=str,
        default="xhigh",
        choices=["minimal", "low", "medium", "high", "xhigh"],
        help="Reasoning effort for phase 5.2 thinking (default: xhigh).",
    )
    run.add_argument(
        "--openrouter-coding-reasoning-effort",
        type=str,
        default="xhigh",
        choices=["minimal", "low", "medium", "high", "xhigh"],
        help="Reasoning effort for phase 5.3 coding (default: xhigh).",
    )
    run.add_argument(
        "--openrouter-eval-model",
        type=str,
        default="openai/gpt-5.2",
        help="OpenRouter model identifier for post-compile semantic evaluation (A-D grade).",
    )
    run.add_argument(
        "--openrouter-eval-reasoning-effort",
        type=str,
        default="xhigh",
        choices=["minimal", "low", "medium", "high", "xhigh"],
        help="Reasoning effort for post-compile semantic evaluation (default: xhigh).",
    )
    run.add_argument(
        "--openrouter-eval-repair-retries",
        type=int,
        default=2,
        help="How many times to re-ask evaluator when output is malformed or unparseable (default: 2).",
    )
    run.add_argument(
        "--min-eval-grade",
        type=str,
        default="B",
        choices=["A", "B", "C", "D", "none"],
        help="Minimum acceptable post-compile evaluation grade (default: B). Use 'none' to disable.",
    )
    run.add_argument(
        "--multipart-min-eval-grade",
        type=str,
        default="A",
        choices=["A", "B", "C", "D", "none"],
        help=(
            "Minimum grade required for each sub-question in multipart chains before the next "
            "sub-question starts (default: A). Use 'none' to disable this chain gate."
        ),
    )
    run.add_argument(
        "--multipart-block-on-failure",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "When enabled, stop remaining sub-questions in a multipart chain after the first failure "
            "(default: enabled). Use --no-multipart-block-on-failure to continue subsequent parts."
        ),
    )
    run.add_argument(
        "--eval-grade-from-output-dir",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "When deciding whether to reuse existing outputs, read latest evaluation grades from "
            "--output instead of --logs (default: use --logs)."
        ),
    )
    run.add_argument(
        "--skip-compiled-ignore-eval-grade",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "When deciding whether to reuse existing outputs, skip any sub-question whose existing .lean "
            "currently compiles, regardless of evaluation grade thresholds (default: off)."
        ),
    )
    run.add_argument(
        "--autopass-eval-a",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "When deciding whether to reuse existing outputs, skip any sub-question whose latest "
            "evaluation grade is already A (from --logs or --output, based on --eval-grade-from-output-dir), "
            "without re-running compile."
        ),
    )
    run.add_argument(
        "--autopass-has-eval",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "When deciding whether to reuse existing outputs, skip any sub-question that already has "
            "an evaluation artifact (from --logs or --output, based on --eval-grade-from-output-dir), "
            "regardless of grade and without re-running compile."
        ),
    )
    run.add_argument(
        "--openrouter-base-url",
        type=str,
        default="https://openrouter.ai/api/v1",
        help="OpenRouter API base URL.",
    )
    run.add_argument(
        "--openrouter-api-key-env",
        type=str,
        default="PRINCIPIA_KEY",
        help="Environment variable name for the OpenRouter API key (falls back to ~/.zshrc assignment).",
    )
    run.add_argument(
        "--api-key-name",
        type=str,
        choices=["PRINCIPIA_KEY", "AUTOLEAN"],
        default=None,
        help=(
            "Convenience key variable selector. Equivalent to setting --openrouter-api-key-env "
            "to PRINCIPIA_KEY or AUTOLEAN."
        ),
    )
    run.add_argument(
        "--openrouter-timeout-s",
        type=int,
        default=180,
        help="OpenRouter request timeout in seconds.",
    )
    run.add_argument(
        "--openrouter-max-retries",
        type=int,
        default=2,
        help="Retry count for transient OpenRouter transport/HTTP failures.",
    )
    run.add_argument(
        "--openrouter-web-search",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Enable OpenRouter web search plugin for model requests.",
    )
    run.add_argument(
        "--openrouter-web-search-engine",
        type=str,
        default=None,
        help="Optional OpenRouter web plugin search engine name.",
    )
    run.add_argument(
        "--openrouter-web-search-max-results",
        type=int,
        default=None,
        help="Optional max web results for OpenRouter web search plugin.",
    )
    run.add_argument(
        "--use-codex-exec",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Use local `codex exec` for model calls (5.2/5.3/eval) instead of OpenRouter API "
            "(default: OpenRouter API)."
        ),
    )
    run.add_argument(
        "--codex-exec-sandbox",
        type=str,
        default="read-only",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="Sandbox mode passed to `codex exec` when --use-codex-exec is enabled.",
    )
    run.add_argument(
        "--use-claude-cli",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Use Claude CLI (headless -p mode) for model calls (5.2/5.3/eval) instead of "
            "OpenRouter API. Requires --claude-cli-cmd."
        ),
    )
    run.add_argument(
        "--claude-cli-cmd",
        type=str,
        default="",
        help=(
            "Full command to invoke Claude CLI in headless mode, e.g. "
            "'/path/to/mathcode -p'. The -p flag is appended automatically if missing."
        ),
    )
    run.add_argument(
        "--compile-cmd",
        type=str,
        default="lake env lean {file}",
        help="Compile command template; must include '{file}'.",
    )
    run.add_argument(
        "--cwd",
        type=Path,
        default=None,
        help="Working directory in which to run the compiler (e.g., your Lean project root).",
    )
    run.add_argument("--force", action="store_true", help="Re-run even if output file exists.")

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.cmd == "run":
        live_logs = bool(args.live_logs)
        multipart_min_eval_grade = (
            None if args.multipart_min_eval_grade == "none" else str(args.multipart_min_eval_grade).upper()
        )
        multipart_block_on_failure = bool(args.multipart_block_on_failure)
        eval_grade_dir = args.output if args.eval_grade_from_output_dir else args.logs
        skip_compiled_ignore_eval_grade = bool(args.skip_compiled_ignore_eval_grade)
        autopass_eval_a = bool(args.autopass_eval_a)
        autopass_has_eval = bool(args.autopass_has_eval)
        openrouter_model = str(args.openrouter_model)
        openrouter_thinking_model = args.openrouter_thinking_model
        openrouter_api_key_env = str(args.openrouter_api_key_env)
        if args.api_key_name is not None:
            openrouter_api_key_env = str(args.api_key_name)
        if bool(args.openrouter_gemini_flash_preview):
            openrouter_model = _OPENROUTER_GEMINI_FLASH_PREVIEW_MODEL
            openrouter_thinking_model = _OPENROUTER_GEMINI_FLASH_PREVIEW_MODEL

        cfg = RunConfig(
            input_dir=args.input,
            output_dir=args.output,
            logs_dir=args.logs,
            max_iters=args.max_iters,
            formalization_only=bool(args.formalization_only),
            require_no_sorry=args.require_no_sorry,
            openrouter_model=openrouter_model,
            openrouter_thinking_model=openrouter_thinking_model,
            openrouter_eval_model=args.openrouter_eval_model,
            openrouter_thinking_reasoning_effort=args.openrouter_thinking_reasoning_effort,
            openrouter_coding_reasoning_effort=args.openrouter_coding_reasoning_effort,
            openrouter_eval_reasoning_effort=args.openrouter_eval_reasoning_effort,
            openrouter_eval_repair_retries=args.openrouter_eval_repair_retries,
            min_eval_grade=None if args.min_eval_grade == "none" else args.min_eval_grade,
            openrouter_base_url=args.openrouter_base_url,
            openrouter_api_key_env=openrouter_api_key_env,
            openrouter_timeout_s=args.openrouter_timeout_s,
            openrouter_max_retries=args.openrouter_max_retries,
            openrouter_web_search=args.openrouter_web_search,
            openrouter_web_search_engine=args.openrouter_web_search_engine,
            openrouter_web_search_max_results=args.openrouter_web_search_max_results,
            use_codex_exec=bool(args.use_codex_exec),
            codex_exec_sandbox=args.codex_exec_sandbox,
            use_claude_cli=bool(args.use_claude_cli),
            claude_cli_cmd=str(args.claude_cli_cmd),
            live_logs=live_logs,
            compile_cmd=args.compile_cmd,
            cwd=args.cwd,
        )

        if cfg.formalization_only and cfg.require_no_sorry:
            print(
                "Invalid options: --formalization-only and --require-no-sorry cannot be used together.",
                file=sys.stderr,
            )
            return 2

        ensure_dir(cfg.output_dir)
        ensure_dir(cfg.logs_dir)

        repo_root = Path(".").resolve()
        ok_all = True

        def _process_one(task: _ProblemTask) -> bool:
            if not args.force:
                try:
                    problem_json = json.loads(task.json_path.read_text(encoding="utf-8"))
                    prompts = build_prompts(
                        problem_json,
                        out_dir=cfg.output_dir,
                        name_hint=task.json_path.stem,
                        formalization_only=cfg.formalization_only,
                    )
                    if autopass_eval_a and prompts.lean_path.exists():
                        has_sorry = False
                        if cfg.require_no_sorry:
                            try:
                                has_sorry = "sorry" in prompts.lean_path.read_text(encoding="utf-8")
                            except OSError:
                                has_sorry = True
                        if not has_sorry:
                            latest_grade = _load_latest_eval_grade(eval_grade_dir, prompts.theorem_name)
                            if latest_grade == "A":
                                return True
                    if autopass_has_eval and prompts.lean_path.exists():
                        has_sorry = False
                        if cfg.require_no_sorry:
                            try:
                                has_sorry = "sorry" in prompts.lean_path.read_text(encoding="utf-8")
                            except OSError:
                                has_sorry = True
                        if not has_sorry and _has_any_eval_artifact(eval_grade_dir, prompts.theorem_name):
                            return True

                    required_min_eval_grade_for_skip = (
                        None if skip_compiled_ignore_eval_grade else task.required_min_eval_grade
                    )
                    if prompts.lean_path.exists() and _output_is_acceptable(
                        cfg,
                        prompts.lean_path,
                        repo_root=repo_root,
                        theorem_name=prompts.theorem_name,
                        required_min_eval_grade=required_min_eval_grade_for_skip,
                        eval_grade_dir=eval_grade_dir,
                    ):
                        return True
                except Exception:
                    pass

            success, _records = process_problem_file(
                cfg,
                task.json_path,
                repo_root=repo_root,
                prior_json_paths=list(task.prior_json_paths),
                override_min_eval_grade=task.required_min_eval_grade,
            )
            return success

        problem_files = list(iter_problem_files(cfg.input_dir))
        problem_units = _build_problem_units(
            problem_files,
            multipart_min_eval_grade=multipart_min_eval_grade,
        )
        total = sum(unit.consumed_count for unit in problem_units)
        show_progress = bool(args.progress) and total > 0
        progress_print = _make_progress_printer() if show_progress else None
        if show_progress and progress_print is not None:
            progress_print(_format_progress(0, total))

        if args.workers <= 1:
            completed = 0
            for unit in problem_units:
                if unit.preflight_error:
                    ok_all = False
                    completed += unit.consumed_count
                    print(unit.preflight_error, file=sys.stderr)
                    if show_progress and progress_print is not None:
                        progress_print(_format_progress(completed, total, label="Skipped invalid chain"))
                    continue

                tasks = list(unit.tasks)
                for idx, task in enumerate(tasks):
                    if show_progress and progress_print is not None:
                        progress_print(
                            _format_progress(completed, total, label=f"Processing {task.json_path.name}")
                        )
                    ok = _process_one(task)
                    completed += 1
                    if not ok:
                        ok_all = False
                        blocked = len(tasks) - idx - 1
                        if multipart_block_on_failure:
                            completed += blocked
                            if show_progress and progress_print is not None:
                                if blocked > 0:
                                    progress_print(
                                        _format_progress(
                                            completed,
                                            total,
                                            label=f"Blocked {blocked} dependent sub-questions",
                                        )
                                    )
                                else:
                                    progress_print(_format_progress(completed, total))
                            break
                        if show_progress and progress_print is not None:
                            progress_print(
                                _format_progress(
                                    completed,
                                    total,
                                    label="Failure recorded, continuing remaining sub-questions",
                                )
                            )
                        continue
                    if show_progress and progress_print is not None:
                        progress_print(_format_progress(completed, total))
            if show_progress and progress_print is not None:
                progress_print(_format_progress(total, total), done=True)
        else:
            def _process_unit(unit: _ProblemUnit) -> tuple[bool, int, Optional[str]]:
                if unit.preflight_error:
                    return False, unit.consumed_count, unit.preflight_error
                unit_ok = True
                for task in unit.tasks:
                    if not _process_one(task):
                        unit_ok = False
                        if multipart_block_on_failure:
                            return False, unit.consumed_count, None
                return unit_ok, unit.consumed_count, None

            completed = 0
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                future_map = {executor.submit(_process_unit, unit): unit for unit in problem_units}
                for future in as_completed(future_map):
                    unit = future_map[future]
                    try:
                        success, consumed, preflight_error = future.result()
                        if not success:
                            ok_all = False
                            if preflight_error:
                                print(preflight_error, file=sys.stderr)
                    except Exception as exc:
                        ok_all = False
                        consumed = unit.consumed_count
                        lead = unit.tasks[0].json_path if unit.tasks else "<invalid-chain>"
                        print(f"Error processing chain starting at {lead}: {exc}", file=sys.stderr)
                    finally:
                        completed += consumed
                        if show_progress and progress_print is not None:
                            progress_print(_format_progress(completed, total, label="Completed"))
            if show_progress and progress_print is not None:
                progress_print(_format_progress(total, total), done=True)
        return 0 if ok_all else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
