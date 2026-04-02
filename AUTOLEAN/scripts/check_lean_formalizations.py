#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable, Optional


@dataclasses.dataclass(frozen=True)
class CompileFailure:
    lean_path: Path
    returncode: int
    error_excerpt: str


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


def _iter_problem_json(input_dir: Path) -> list[Path]:
    return sorted(input_dir.glob("*.json"))


def _iter_lean_files(input_dir: Path) -> list[Path]:
    return sorted(input_dir.glob("*.lean"))


def _expected_lean_name(problem_json_stem: str) -> str:
    return f"problem_{problem_json_stem}.lean"


def _default_formalizations_dir(input_dir: Path) -> Path:
    if input_dir.name.endswith("_Fin"):
        return input_dir
    sibling_fin = input_dir.parent / f"{input_dir.name}_Fin"
    if sibling_fin.is_dir():
        return sibling_fin
    return input_dir


def _extract_error_excerpt(stdout: str, stderr: str, *, max_lines: int) -> str:
    combined = (stderr.strip() + "\n" + stdout.strip()).strip()
    if not combined:
        return "<no compiler output>"
    lines = [line.rstrip() for line in combined.splitlines() if line.strip()]
    if not lines:
        return "<no compiler output>"
    return "\n".join(lines[:max_lines])


def _compile_one(
    *,
    lean_path: Path,
    compile_cmd: str,
    cwd: Path,
    max_error_lines: int,
) -> tuple[Path, int, str]:
    argv = shlex.split(compile_cmd.replace("{file}", str(lean_path.resolve())))
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )
    excerpt = _extract_error_excerpt(proc.stdout, proc.stderr, max_lines=max_error_lines)
    return lean_path, proc.returncode, excerpt


def _build_lean_targets(
    *,
    input_dir: Path,
    formalizations_dir: Path,
) -> tuple[list[Path], list[Path]]:
    json_files = _iter_problem_json(input_dir)
    if json_files:
        missing: list[Path] = []
        targets: list[Path] = []
        for problem_json in json_files:
            expected = formalizations_dir / _expected_lean_name(problem_json.stem)
            if expected.exists():
                targets.append(expected)
            else:
                missing.append(expected)
        return targets, missing

    lean_files = _iter_lean_files(input_dir)
    return lean_files, []


def check_formalizations(
    *,
    input_dir: Path,
    formalizations_dir: Path,
    compile_cmd: str,
    cwd: Path,
    workers: int,
    max_error_lines: int,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> tuple[list[Path], list[CompileFailure], list[Path]]:
    targets, missing = _build_lean_targets(
        input_dir=input_dir,
        formalizations_dir=formalizations_dir,
    )
    total = len(targets)
    if progress_cb is not None:
        progress_cb(0, total)
    if not targets:
        return [], [], missing

    failures: list[CompileFailure] = []
    if workers <= 1:
        completed = 0
        for lean_path in targets:
            _, returncode, excerpt = _compile_one(
                lean_path=lean_path,
                compile_cmd=compile_cmd,
                cwd=cwd,
                max_error_lines=max_error_lines,
            )
            if returncode != 0:
                failures.append(
                    CompileFailure(
                        lean_path=lean_path,
                        returncode=returncode,
                        error_excerpt=excerpt,
                    )
                )
            completed += 1
            if progress_cb is not None:
                progress_cb(completed, total)
        return targets, failures, missing

    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(
                _compile_one,
                lean_path=lean_path,
                compile_cmd=compile_cmd,
                cwd=cwd,
                max_error_lines=max_error_lines,
            ): lean_path
            for lean_path in targets
        }
        for future in as_completed(future_map):
            lean_path = future_map[future]
            try:
                _, returncode, excerpt = future.result()
            except Exception as exc:  # pragma: no cover - defensive fallback
                failures.append(
                    CompileFailure(
                        lean_path=lean_path,
                        returncode=1,
                        error_excerpt=f"compile runner error: {exc}",
                    )
                )
            else:
                if returncode != 0:
                    failures.append(
                        CompileFailure(
                            lean_path=lean_path,
                            returncode=returncode,
                            error_excerpt=excerpt,
                        )
                    )
            completed += 1
            if progress_cb is not None:
                progress_cb(completed, total)
    failures.sort(key=lambda item: item.lean_path.name)
    return targets, failures, missing


def _print_paths(paths: Iterable[Path]) -> None:
    for path in paths:
        print(f"- {path.as_posix()}")


def _write_failure_report(
    *,
    report_path: Path,
    input_dir: Path,
    formalizations_dir: Path,
    cwd: Path,
    strict_missing: bool,
    targets: list[Path],
    failures: list[CompileFailure],
    missing: list[Path],
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    has_failure = bool(failures) or (bool(missing) and strict_missing)
    result = "FAIL" if has_failure else "PASS"

    lines: list[str] = [
        "Autolean Lean Check Report",
        f"Result: {result}",
        "",
        f"Input: {input_dir.as_posix()}",
        f"Formalizations: {formalizations_dir.as_posix()}",
        f"Compile cwd: {cwd.as_posix()}",
        f"Checked Lean files: {len(targets)}",
        f"Failed Lean files: {len(failures)}",
        f"Missing Lean files: {len(missing)}",
        f"Strict missing: {strict_missing}",
        "",
    ]

    if failures:
        lines.append("Failed lean formalizations:")
        for item in failures:
            lines.append(f"- {item.lean_path.as_posix()} (exit={item.returncode})")
            excerpt = item.error_excerpt.strip()
            if excerpt:
                for ln in excerpt.splitlines():
                    lines.append(f"  {ln}")
            lines.append("")
    else:
        lines.append("Failed lean formalizations: none")
        lines.append("")

    if missing:
        lines.append("Missing lean formalizations:")
        for path in missing:
            lines.append(f"- {path.as_posix()}")
    else:
        lines.append("Missing lean formalizations: none")

    report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compile-check Lean formalizations for all problems under a folder and print only failures."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        required=True,
        help=(
            "Folder containing problems (*.json) or formalizations (*.lean). "
            "If *.json is present, expected files are problem_<stem>.lean in --formalizations-dir."
        ),
    )
    parser.add_argument(
        "--formalizations-dir",
        type=Path,
        default=None,
        help=(
            "Directory containing Lean files. Default: <input-dir>_Fin if it exists, "
            "otherwise --input-dir."
        ),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help=(
            "Directory for checker report output (default: --formalizations-dir). "
            "Report filename: check_lean_formalizations_report.txt."
        ),
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
        default=Path("."),
        help="Working directory where the compiler command runs (default: current directory).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Parallel compile workers (default: 1).",
    )
    parser.add_argument(
        "--max-error-lines",
        type=int,
        default=12,
        help="Max compiler output lines shown per failed file (default: 12).",
    )
    parser.add_argument(
        "--strict-missing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Treat missing expected .lean files as failure (default: true).",
    )
    parser.add_argument(
        "--progress",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Show a progress bar while compile-checking files (default: true).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    input_dir = args.input_dir.resolve()
    formalizations_dir = (
        args.formalizations_dir.resolve()
        if args.formalizations_dir is not None
        else _default_formalizations_dir(input_dir).resolve()
    )
    out_dir = args.out_dir.resolve() if args.out_dir is not None else formalizations_dir
    cwd = args.cwd.resolve()

    if "{file}" not in args.compile_cmd:
        print("Error: --compile-cmd must include '{file}'.", file=sys.stderr)
        return 2
    if not input_dir.is_dir():
        print(f"Error: input directory does not exist: {input_dir}", file=sys.stderr)
        return 2
    if not formalizations_dir.is_dir():
        print(f"Error: formalizations directory does not exist: {formalizations_dir}", file=sys.stderr)
        return 2
    if not cwd.is_dir():
        print(f"Error: cwd does not exist: {cwd}", file=sys.stderr)
        return 2
    if out_dir.exists() and not out_dir.is_dir():
        print(f"Error: out directory is not a directory: {out_dir}", file=sys.stderr)
        return 2
    if args.workers < 1:
        print("Error: --workers must be >= 1.", file=sys.stderr)
        return 2
    if args.max_error_lines < 1:
        print("Error: --max-error-lines must be >= 1.", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    progress_print = _make_progress_printer() if args.progress else None

    def _progress_cb(completed: int, total: int) -> None:
        if progress_print is None:
            return
        progress_print(_format_progress(completed, total, label="Checking"))

    targets, failures, missing = check_formalizations(
        input_dir=input_dir,
        formalizations_dir=formalizations_dir,
        compile_cmd=args.compile_cmd,
        cwd=cwd,
        workers=args.workers,
        max_error_lines=args.max_error_lines,
        progress_cb=_progress_cb if progress_print is not None else None,
    )
    if progress_print is not None:
        progress_print(_format_progress(len(targets), len(targets), label="Checking"), done=True)

    report_path = out_dir / "check_lean_formalizations_report.txt"
    _write_failure_report(
        report_path=report_path,
        input_dir=input_dir,
        formalizations_dir=formalizations_dir,
        cwd=cwd,
        strict_missing=bool(args.strict_missing),
        targets=targets,
        failures=failures,
        missing=missing,
    )

    print(f"Input: {input_dir.as_posix()}")
    print(f"Formalizations: {formalizations_dir.as_posix()}")
    print(f"Report: {report_path.as_posix()}")
    print(f"Compile cwd: {cwd.as_posix()}")
    print(f"Checked Lean files: {len(targets)}")
    print(f"Failed Lean files: {len(failures)}")
    print(f"Missing Lean files: {len(missing)}")

    if failures:
        print("\nFailed lean formalizations:")
        for item in failures:
            print(f"- {item.lean_path.as_posix()} (exit={item.returncode})")
            print(item.error_excerpt)
            print()

    if missing:
        print("Missing lean formalizations:")
        _print_paths(missing)

    has_failure = bool(failures) or (bool(missing) and bool(args.strict_missing))
    return 1 if has_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
