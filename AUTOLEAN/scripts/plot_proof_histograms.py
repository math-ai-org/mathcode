#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import dataclasses
import html
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from autolean.util import ensure_dir  # noqa: E402


@dataclasses.dataclass(frozen=True)
class ProblemSummary:
    summary_path: Path
    relative_path: str
    theorem_name: str
    pass_count: int
    one_shot_pass_count: int
    attempt_count: int


def _as_nonnegative_int(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _derive_counts_from_attempts(attempts: object) -> tuple[Optional[int], Optional[int], Optional[int]]:
    if not isinstance(attempts, list):
        return None, None, None

    pass_count = 0
    one_shot_pass_count = 0
    for item in attempts:
        if not isinstance(item, dict):
            return None, None, None
        passed = item.get("passed")
        iterations_used = _as_nonnegative_int(item.get("iterations_used"))
        if not isinstance(passed, bool) or iterations_used is None:
            return None, None, None
        if passed:
            pass_count += 1
            if iterations_used == 1:
                one_shot_pass_count += 1
    return pass_count, one_shot_pass_count, len(attempts)


def _parse_problem_summary(summary_path: Path, payload: object) -> Optional[ProblemSummary]:
    if not isinstance(payload, dict):
        return None

    derived_pass_count, derived_one_shot_pass_count, derived_attempt_count = _derive_counts_from_attempts(
        payload.get("attempts")
    )
    pass_count = _as_nonnegative_int(payload.get("pass_count"))
    if pass_count is None:
        pass_count = derived_pass_count
    one_shot_pass_count = _as_nonnegative_int(payload.get("one_shot_pass_count"))
    if one_shot_pass_count is None:
        one_shot_pass_count = derived_one_shot_pass_count
    attempt_count = _as_nonnegative_int(payload.get("attempt_count"))
    if attempt_count is None:
        attempt_count = derived_attempt_count

    if pass_count is None or one_shot_pass_count is None or attempt_count is None:
        return None
    if one_shot_pass_count > pass_count or pass_count > attempt_count:
        return None

    relative_path = payload.get("relative_path")
    if not isinstance(relative_path, str) or not relative_path.strip():
        relative_path = str(summary_path.parent.name)
    theorem_name = payload.get("theorem_name")
    if not isinstance(theorem_name, str) or not theorem_name.strip():
        theorem_name = summary_path.parent.name

    return ProblemSummary(
        summary_path=summary_path,
        relative_path=relative_path,
        theorem_name=theorem_name,
        pass_count=pass_count,
        one_shot_pass_count=one_shot_pass_count,
        attempt_count=attempt_count,
    )


def _load_problem_summaries(input_dir: Path) -> tuple[list[ProblemSummary], list[Path]]:
    summaries: list[ProblemSummary] = []
    invalid_paths: list[Path] = []
    for summary_path in sorted(input_dir.rglob("summary.json")):
        try:
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            invalid_paths.append(summary_path)
            continue
        parsed = _parse_problem_summary(summary_path, payload)
        if parsed is None:
            invalid_paths.append(summary_path)
            continue
        summaries.append(parsed)
    return summaries, invalid_paths


def _build_histogram_rows(values: list[int], *, bucket_key: str) -> list[dict[str, int]]:
    counter = Counter(values)
    return [
        {bucket_key: bucket, "problem_count": counter[bucket]}
        for bucket in sorted(counter)
    ]


def _build_histogram_svg(
    *,
    title: str,
    subtitle: str,
    x_label: str,
    bucket_key: str,
    histogram_rows: list[dict[str, int]],
) -> str:
    width = 960
    height = 600
    margin_left = 90
    margin_right = 40
    margin_top = 90
    margin_bottom = 100
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom

    def _x(pos: float) -> str:
        return f"{pos:.2f}"

    def _y(pos: float) -> str:
        return f"{pos:.2f}"

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" ',
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        (
            f'<text x="{width / 2:.2f}" y="36" text-anchor="middle" '
            'font-family="Arial, sans-serif" font-size="24" fill="#111827">'
            f"{html.escape(title)}</text>"
        ),
        (
            f'<text x="{width / 2:.2f}" y="62" text-anchor="middle" '
            'font-family="Arial, sans-serif" font-size="14" fill="#4b5563">'
            f"{html.escape(subtitle)}</text>"
        ),
        (
            f'<line x1="{margin_left}" y1="{margin_top + plot_height}" '
            f'x2="{margin_left + plot_width}" y2="{margin_top + plot_height}" '
            'stroke="#374151" stroke-width="2"/>'
        ),
        (
            f'<line x1="{margin_left}" y1="{margin_top}" '
            f'x2="{margin_left}" y2="{margin_top + plot_height}" '
            'stroke="#374151" stroke-width="2"/>'
        ),
    ]

    if not histogram_rows:
        parts.extend(
            [
                (
                    f'<text x="{width / 2:.2f}" y="{height / 2:.2f}" text-anchor="middle" '
                    'font-family="Arial, sans-serif" font-size="18" fill="#6b7280">'
                    "No worked-out problems found"
                    "</text>"
                ),
                (
                    f'<text x="{width / 2:.2f}" y="{height - 28}" text-anchor="middle" '
                    'font-family="Arial, sans-serif" font-size="14" fill="#111827">'
                    f"{html.escape(x_label)}</text>"
                ),
                (
                    f'<text x="24" y="{height / 2:.2f}" text-anchor="middle" '
                    'font-family="Arial, sans-serif" font-size="14" fill="#111827" '
                    'transform="rotate(-90 24 '
                    f'{height / 2:.2f})">Problems</text>'
                ),
                "</svg>",
            ]
        )
        return "".join(parts)

    max_problem_count = max(row["problem_count"] for row in histogram_rows)
    y_tick_step = max(1, math.ceil(max_problem_count / 5))
    y_max = max(y_tick_step, math.ceil(max_problem_count / y_tick_step) * y_tick_step)

    for y_value in range(0, y_max + 1, y_tick_step):
        y_pos = margin_top + plot_height - (plot_height * y_value / y_max)
        parts.append(
            (
                f'<line x1="{margin_left}" y1="{_y(y_pos)}" '
                f'x2="{margin_left + plot_width}" y2="{_y(y_pos)}" '
                'stroke="#e5e7eb" stroke-width="1"/>'
            )
        )
        parts.append(
            (
                f'<text x="{margin_left - 12}" y="{_y(y_pos + 5)}" text-anchor="end" '
                'font-family="Arial, sans-serif" font-size="12" fill="#4b5563">'
                f"{y_value}</text>"
            )
        )

    bucket_count = len(histogram_rows)
    slot_width = plot_width / max(1, bucket_count)
    bar_width = min(88.0, slot_width * 0.72)

    for idx, row in enumerate(histogram_rows):
        bucket_value = row[bucket_key]
        problem_count = row["problem_count"]
        bar_height = plot_height * problem_count / y_max
        center_x = margin_left + slot_width * (idx + 0.5)
        bar_x = center_x - bar_width / 2
        bar_y = margin_top + plot_height - bar_height

        parts.append(
            (
                f'<rect x="{_x(bar_x)}" y="{_y(bar_y)}" width="{bar_width:.2f}" '
                f'height="{bar_height:.2f}" fill="#2563eb"/>'
            )
        )
        parts.append(
            (
                f'<text x="{_x(center_x)}" y="{_y(bar_y - 8)}" text-anchor="middle" '
                'font-family="Arial, sans-serif" font-size="12" fill="#111827">'
                f"{problem_count}</text>"
            )
        )
        parts.append(
            (
                f'<text x="{_x(center_x)}" y="{margin_top + plot_height + 22}" '
                'text-anchor="middle" font-family="Arial, sans-serif" '
                'font-size="12" fill="#111827">'
                f"{bucket_value}</text>"
            )
        )

    parts.extend(
        [
            (
                f'<text x="{width / 2:.2f}" y="{height - 28}" text-anchor="middle" '
                'font-family="Arial, sans-serif" font-size="14" fill="#111827">'
                f"{html.escape(x_label)}</text>"
            ),
            (
                f'<text x="24" y="{height / 2:.2f}" text-anchor="middle" '
                'font-family="Arial, sans-serif" font-size="14" fill="#111827" '
                'transform="rotate(-90 24 '
                f'{height / 2:.2f})">Problems</text>'
            ),
            "</svg>",
        ]
    )
    return "".join(parts)


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_histogram_png(
    *,
    svg_path: Path,
    png_path: Path,
    title: str,
    subtitle: str,
    x_label: str,
    bucket_key: str,
    histogram_rows: list[dict[str, int]],
) -> str:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        plt = None

    if plt is not None:
        fig, ax = plt.subplots(figsize=(9.6, 6), dpi=150)
        fig.patch.set_facecolor("white")
        ax.set_facecolor("white")

        if histogram_rows:
            x_values = [row[bucket_key] for row in histogram_rows]
            y_values = [row["problem_count"] for row in histogram_rows]
            positions = list(range(len(histogram_rows)))
            bars = ax.bar(positions, y_values, color="#2563eb", width=0.72)
            ax.set_xticks(positions, [str(value) for value in x_values])
            y_top = max(y_values)
            for bar, problem_count in zip(bars, y_values):
                ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    problem_count + max(0.05, y_top * 0.02),
                    str(problem_count),
                    ha="center",
                    va="bottom",
                    fontsize=9,
                    color="#111827",
                )
        else:
            ax.text(
                0.5,
                0.5,
                "No worked-out problems found",
                ha="center",
                va="center",
                fontsize=14,
                color="#6b7280",
                transform=ax.transAxes,
            )
            ax.set_xticks([])

        ax.set_title(title, fontsize=18, color="#111827", pad=18)
        ax.text(
            0.5,
            1.01,
            subtitle,
            ha="center",
            va="bottom",
            fontsize=10,
            color="#4b5563",
            transform=ax.transAxes,
        )
        ax.set_xlabel(x_label, color="#111827")
        ax.set_ylabel("Problems", color="#111827")
        ax.grid(axis="y", color="#e5e7eb")
        ax.set_axisbelow(True)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        fig.tight_layout()
        fig.savefig(png_path, format="png")
        plt.close(fig)
        return "matplotlib"

    sips_path = shutil.which("sips")
    if sips_path is not None:
        proc = subprocess.run(
            [sips_path, "-s", "format", "png", str(svg_path), "--out", str(png_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return "sips"
        raise RuntimeError(
            "failed to convert SVG histogram to PNG with `sips`: "
            + (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")
        )

    raise RuntimeError(
        "unable to write PNG histogram: neither matplotlib nor `sips` is available"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate histograms for proof-completion results. The script reads per-problem "
            "`summary.json` files, keeps worked-out problems (`pass_count > 0`), and writes "
            "SVG and PNG histograms for passing attempts and one-shot passing attempts."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        required=True,
        help="Directory containing proof-completion result folders with per-problem `summary.json` files.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help=(
            "Directory where histogram SVGs, PNGs, and histogram_data.json are written "
            "(default: <input-dir>/histograms)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    input_dir = Path(args.input_dir)
    if not input_dir.exists():
        print(f"Input directory does not exist: {input_dir}", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir) if args.out_dir is not None else input_dir / "histograms"
    ensure_dir(out_dir)

    summaries, invalid_paths = _load_problem_summaries(input_dir)
    worked_out = [item for item in summaries if item.pass_count > 0]

    worked_out_attempt_histogram = _build_histogram_rows(
        [item.pass_count for item in worked_out],
        bucket_key="worked_out_attempts",
    )
    one_shot_worked_out_attempt_histogram = _build_histogram_rows(
        [item.one_shot_pass_count for item in worked_out],
        bucket_key="one_shot_worked_out_attempts",
    )

    total_hist_svg = _build_histogram_svg(
        title="Worked-out Attempts per Solved Problem",
        subtitle="Solved problems only (`pass_count > 0`)",
        x_label="Passing attempts",
        bucket_key="worked_out_attempts",
        histogram_rows=worked_out_attempt_histogram,
    )
    one_shot_hist_svg = _build_histogram_svg(
        title="One-shot Worked-out Attempts per Solved Problem",
        subtitle="Solved problems only (`pass_count > 0`)",
        x_label="One-shot passing attempts",
        bucket_key="one_shot_worked_out_attempts",
        histogram_rows=one_shot_worked_out_attempt_histogram,
    )

    total_hist_path = out_dir / "worked_out_attempts_histogram.svg"
    one_shot_hist_path = out_dir / "one_shot_worked_out_attempts_histogram.svg"
    total_hist_png_path = out_dir / "worked_out_attempts_histogram.png"
    one_shot_hist_png_path = out_dir / "one_shot_worked_out_attempts_histogram.png"
    summary_path = out_dir / "histogram_data.json"

    _write_text(total_hist_path, total_hist_svg)
    _write_text(one_shot_hist_path, one_shot_hist_svg)
    png_backend = _write_histogram_png(
        svg_path=total_hist_path,
        png_path=total_hist_png_path,
        title="Worked-out Attempts per Solved Problem",
        subtitle="Solved problems only (`pass_count > 0`)",
        x_label="Passing attempts",
        bucket_key="worked_out_attempts",
        histogram_rows=worked_out_attempt_histogram,
    )
    _write_histogram_png(
        svg_path=one_shot_hist_path,
        png_path=one_shot_hist_png_path,
        title="One-shot Worked-out Attempts per Solved Problem",
        subtitle="Solved problems only (`pass_count > 0`)",
        x_label="One-shot passing attempts",
        bucket_key="one_shot_worked_out_attempts",
        histogram_rows=one_shot_worked_out_attempt_histogram,
    )
    _write_json(
        summary_path,
        {
            "input_dir": str(input_dir),
            "out_dir": str(out_dir),
            "problem_count": len(summaries),
            "worked_out_problem_count": len(worked_out),
            "invalid_summary_paths": [str(path) for path in invalid_paths],
            "png_backend": png_backend,
            "worked_out_attempt_histogram": worked_out_attempt_histogram,
            "one_shot_worked_out_attempt_histogram": one_shot_worked_out_attempt_histogram,
        },
    )

    print(f"Loaded {len(summaries)} problem summaries from {input_dir}")
    print(f"Worked-out problems: {len(worked_out)}")
    print(f"Invalid summaries skipped: {len(invalid_paths)}")
    print(f"Wrote {total_hist_path}")
    print(f"Wrote {one_shot_hist_path}")
    print(f"Wrote {total_hist_png_path}")
    print(f"Wrote {one_shot_hist_png_path}")
    print(f"PNG backend: {png_backend}")
    print(f"Wrote {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
