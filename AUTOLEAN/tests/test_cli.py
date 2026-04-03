"""Tests for autolean.cli internal helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from autolean.cli import (
    _build_problem_units,
    _grade_below_threshold,
    _parse_subquestion_stem,
)


# ---------------------------------------------------------------------------
# _grade_below_threshold
# ---------------------------------------------------------------------------


class TestGradeBelowThreshold:
    def test_a_above_b(self):
        assert _grade_below_threshold("A", "B") is False

    def test_b_meets_b(self):
        assert _grade_below_threshold("B", "B") is False

    def test_c_below_b(self):
        assert _grade_below_threshold("C", "B") is True

    def test_d_below_a(self):
        assert _grade_below_threshold("D", "A") is True

    def test_a_meets_a(self):
        assert _grade_below_threshold("A", "A") is False

    def test_case_insensitive(self):
        assert _grade_below_threshold("a", "B") is False

    def test_d_meets_d(self):
        assert _grade_below_threshold("D", "D") is False


# ---------------------------------------------------------------------------
# _parse_subquestion_stem
# ---------------------------------------------------------------------------


class TestParseSubquestionStem:
    def test_valid_stem(self):
        result = _parse_subquestion_stem("problem_1")
        assert result == ("problem", 1)

    def test_multi_index(self):
        result = _parse_subquestion_stem("my_chain_3")
        assert result == ("my_chain", 3)

    def test_no_suffix(self):
        assert _parse_subquestion_stem("problem") is None

    def test_non_numeric_suffix(self):
        assert _parse_subquestion_stem("problem_abc") is None

    def test_nested_underscores(self):
        result = _parse_subquestion_stem("a_b_c_5")
        assert result == ("a_b_c", 5)

    def test_zero_index(self):
        result = _parse_subquestion_stem("chain_0")
        assert result == ("chain", 0)


# ---------------------------------------------------------------------------
# _build_problem_units
# ---------------------------------------------------------------------------


class TestBuildProblemUnits:
    def _make_files(self, tmp_path: Path, names: list[str]) -> list[Path]:
        paths = []
        for name in names:
            p = tmp_path / name
            p.write_text("{}", encoding="utf-8")
            paths.append(p)
        return paths

    def test_single_standalone(self, tmp_path: Path):
        files = self._make_files(tmp_path, ["standalone_1.json"])
        units = _build_problem_units(files, multipart_min_eval_grade=None)
        assert len(units) == 1
        assert len(units[0].tasks) == 1
        assert units[0].preflight_error is None

    def test_multipart_chain(self, tmp_path: Path):
        files = self._make_files(
            tmp_path, ["chain_1.json", "chain_2.json", "chain_3.json"]
        )
        units = _build_problem_units(files, multipart_min_eval_grade="A")
        assert len(units) == 1
        assert len(units[0].tasks) == 3
        assert units[0].preflight_error is None

    def test_multipart_with_gap(self, tmp_path: Path):
        files = self._make_files(tmp_path, ["chain_1.json", "chain_3.json"])
        units = _build_problem_units(files, multipart_min_eval_grade="A")
        # Should produce a unit with a preflight error about the gap
        has_error = any(u.preflight_error is not None for u in units)
        assert has_error

    def test_multipart_missing_start(self, tmp_path: Path):
        files = self._make_files(tmp_path, ["chain_2.json", "chain_3.json"])
        units = _build_problem_units(files, multipart_min_eval_grade="A")
        has_error = any(u.preflight_error is not None for u in units)
        assert has_error

    def test_empty_list(self, tmp_path: Path):
        units = _build_problem_units([], multipart_min_eval_grade=None)
        assert units == []

    def test_mixed_standalone_and_chain(self, tmp_path: Path):
        files = self._make_files(
            tmp_path,
            ["solo_1.json", "chain_1.json", "chain_2.json"],
        )
        units = _build_problem_units(files, multipart_min_eval_grade="B")
        total_tasks = sum(len(u.tasks) for u in units)
        assert total_tasks == 3  # 1 standalone + 2 chain

    def test_multipart_chain_has_prior_paths(self, tmp_path: Path):
        files = self._make_files(
            tmp_path, ["seq_1.json", "seq_2.json", "seq_3.json"]
        )
        units = _build_problem_units(files, multipart_min_eval_grade="A")
        assert len(units) == 1
        tasks = units[0].tasks
        assert len(tasks[0].prior_json_paths) == 0
        assert len(tasks[1].prior_json_paths) == 1
        assert len(tasks[2].prior_json_paths) == 2

    def test_multipart_chain_min_eval_grade(self, tmp_path: Path):
        files = self._make_files(
            tmp_path, ["graded_1.json", "graded_2.json"]
        )
        units = _build_problem_units(files, multipart_min_eval_grade="A")
        for task in units[0].tasks:
            assert task.required_min_eval_grade == "A"

    def test_none_multipart_grade(self, tmp_path: Path):
        files = self._make_files(
            tmp_path, ["ng_1.json", "ng_2.json"]
        )
        units = _build_problem_units(files, multipart_min_eval_grade=None)
        for task in units[0].tasks:
            assert task.required_min_eval_grade is None
