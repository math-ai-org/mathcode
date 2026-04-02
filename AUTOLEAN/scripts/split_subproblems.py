#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_ALREADY_SPLIT_STEM_RE = re.compile(r".+_\d+$")
_FIRST_SUBQUESTION_MARKER_RE = re.compile(r"[（(]\s*1\s*[)）]")
_PARALLEL_LIST_FIELDS = {
    "solution",
    "remark",
    "reference",
    "answer",
    "proof",
    "hints",
    "hint",
    "analysis",
}


@dataclass
class SplitStats:
    scanned: int = 0
    multipart_files: int = 0
    written: int = 0
    deleted_originals: int = 0
    skipped_exists: int = 0
    skipped_not_multipart: int = 0
    skipped_already_split_name: int = 0
    skipped_delete_missing_outputs: int = 0
    delete_errors: int = 0
    invalid_json: int = 0


def _is_already_split_stem(stem: str) -> bool:
    return bool(_ALREADY_SPLIT_STEM_RE.fullmatch(stem))


def _extract_shared_problem_prefix(problem_lines: list[str]) -> str:
    if not problem_lines:
        return ""

    first = problem_lines[0]
    if not isinstance(first, str):
        return ""

    marker = _FIRST_SUBQUESTION_MARKER_RE.search(first)
    if marker is None or marker.start() <= 0:
        return ""
    return first[: marker.start()]


def _prepend_shared_problem_prefix(problem_text: str, *, shared_prefix: str) -> str:
    if not shared_prefix:
        return problem_text
    if not problem_text:
        return shared_prefix

    normalized_prefix = shared_prefix.strip()
    if normalized_prefix and problem_text.lstrip().startswith(normalized_prefix):
        return problem_text
    return shared_prefix + problem_text


def _build_subproblem_payload(
    problem_obj: dict,
    *,
    idx: int,
    total: int,
    rewrite_uuid: bool,
    shared_problem_prefix: str,
) -> dict:
    payload: dict = {}
    for key, value in problem_obj.items():
        if key == "problem":
            item = value[idx]
            if idx > 0:
                item = _prepend_shared_problem_prefix(item, shared_prefix=shared_problem_prefix)
            payload[key] = [item]
            continue

        if key in _PARALLEL_LIST_FIELDS and isinstance(value, list) and len(value) == total:
            payload[key] = [value[idx]]
            continue

        payload[key] = value

    if rewrite_uuid and isinstance(payload.get("uuid"), str):
        payload["uuid"] = f"{payload['uuid']}_{idx + 1}"

    return payload


def _write_json(path: Path, obj: dict) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


def split_subproblems_in_dir(
    *,
    input_dir: Path,
    output_dir: Path,
    force: bool,
    dry_run: bool,
    delete_original: bool,
    include_already_split: bool,
    rewrite_uuid: bool,
) -> SplitStats:
    stats = SplitStats()
    files = sorted(input_dir.glob("*.json"))

    for json_path in files:
        stats.scanned += 1
        stem = json_path.stem
        if not include_already_split and _is_already_split_stem(stem):
            stats.skipped_already_split_name += 1
            continue

        try:
            raw = json_path.read_text(encoding="utf-8")
            obj = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            stats.invalid_json += 1
            print(f"[invalid] {json_path}: {exc}", file=sys.stderr)
            continue

        if not isinstance(obj, dict):
            stats.invalid_json += 1
            print(f"[invalid] {json_path}: top-level JSON must be an object", file=sys.stderr)
            continue

        problem = obj.get("problem")
        if not isinstance(problem, list) or len(problem) <= 1:
            stats.skipped_not_multipart += 1
            continue

        if not all(isinstance(x, str) for x in problem):
            stats.invalid_json += 1
            print(f"[invalid] {json_path}: 'problem' must be a list of strings", file=sys.stderr)
            continue

        stats.multipart_files += 1
        total = len(problem)
        shared_problem_prefix = _extract_shared_problem_prefix(problem)
        expected_outputs: list[Path] = []
        for idx in range(total):
            out_name = f"{stem}_{idx + 1}.json"
            out_path = output_dir / out_name
            expected_outputs.append(out_path)
            if out_path.exists() and not force:
                stats.skipped_exists += 1
                continue

            split_payload = _build_subproblem_payload(
                obj,
                idx=idx,
                total=total,
                rewrite_uuid=rewrite_uuid,
                shared_problem_prefix=shared_problem_prefix,
            )
            if not dry_run:
                _write_json(out_path, split_payload)
            stats.written += 1
            print(f"[write] {json_path.name} -> {out_name}")

        if delete_original:
            if dry_run:
                print(f"[delete-dry-run] {json_path.name}")
            else:
                missing = [p.name for p in expected_outputs if not p.exists()]
                if missing:
                    stats.skipped_delete_missing_outputs += 1
                    print(
                        f"[skip-delete] {json_path.name}: missing split outputs: {', '.join(missing)}",
                        file=sys.stderr,
                    )
                else:
                    try:
                        json_path.unlink()
                        stats.deleted_originals += 1
                        print(f"[delete] {json_path.name}")
                    except OSError as exc:
                        stats.delete_errors += 1
                        print(f"[delete-error] {json_path}: {exc}", file=sys.stderr)

    return stats


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Split multi-part problem JSON files into one file per sub-problem. "
            "Example: 14.json -> 14_1.json, 14_2.json, ..."
        )
    )
    p.add_argument(
        "--input-dir",
        type=Path,
        default=Path("Chap4"),
        help="Directory containing source JSON files (default: Chap4).",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory for split JSON files (default: same as --input-dir).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing output files.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be written without creating files.",
    )
    p.add_argument(
        "--delete-original",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Delete original multi-part source file after successful split (default: on).",
    )
    p.add_argument(
        "--include-already-split",
        action="store_true",
        help="Also process filenames that already end with _<number>.",
    )
    p.add_argument(
        "--rewrite-uuid",
        action="store_true",
        help="Append _<index> to uuid in split outputs.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve() if args.output_dir else input_dir

    if not input_dir.is_dir():
        print(f"Input directory does not exist: {input_dir}", file=sys.stderr)
        return 2
    output_dir.mkdir(parents=True, exist_ok=True)

    stats = split_subproblems_in_dir(
        input_dir=input_dir,
        output_dir=output_dir,
        force=bool(args.force),
        dry_run=bool(args.dry_run),
        delete_original=bool(args.delete_original),
        include_already_split=bool(args.include_already_split),
        rewrite_uuid=bool(args.rewrite_uuid),
    )

    print(
        "Summary: "
        f"scanned={stats.scanned}, "
        f"multipart_files={stats.multipart_files}, "
        f"written={stats.written}, "
        f"deleted_originals={stats.deleted_originals}, "
        f"skipped_exists={stats.skipped_exists}, "
        f"skipped_not_multipart={stats.skipped_not_multipart}, "
        f"skipped_already_split_name={stats.skipped_already_split_name}, "
        f"skipped_delete_missing_outputs={stats.skipped_delete_missing_outputs}, "
        f"delete_errors={stats.delete_errors}, "
        f"invalid_json={stats.invalid_json}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
