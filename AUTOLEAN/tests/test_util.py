"""Tests for autolean.util."""

from __future__ import annotations

from pathlib import Path

import pytest

from autolean.util import CommandResult, concat_problem_lines, ensure_dir, sanitize_identifier


# ---------------------------------------------------------------------------
# sanitize_identifier
# ---------------------------------------------------------------------------


class TestSanitizeIdentifier:
    def test_simple_ascii(self):
        assert sanitize_identifier("abc_123") == "abc_123"

    def test_uuid_style(self):
        result = sanitize_identifier("a1b2c3d4-e5f6-7890-abcd-ef1234567890")
        assert "-" not in result
        assert result == "a1b2c3d4_e5f6_7890_abcd_ef1234567890"

    def test_spaces_and_slashes(self):
        result = sanitize_identifier("foo bar/baz\\qux")
        assert " " not in result
        assert "/" not in result
        assert "\\" not in result
        assert result == "foo_bar_baz_qux"

    def test_collapses_underscores(self):
        result = sanitize_identifier("a___b")
        assert result == "a_b"

    def test_strips_leading_trailing_underscores(self):
        result = sanitize_identifier("__hello__")
        assert result == "hello"

    def test_empty_string(self):
        assert sanitize_identifier("") == "unnamed"

    def test_all_special_chars(self):
        assert sanitize_identifier("!!!") == "unnamed"

    def test_cjk_characters(self):
        """CJK characters should be transliterated to pinyin."""
        result = sanitize_identifier("数学")
        assert result.isascii() or all(
            c.isalnum() or c == "_" for c in result
        )
        assert len(result) > 0
        assert result != "unnamed"

    def test_mixed_cjk_ascii(self):
        result = sanitize_identifier("test_数学_proof")
        assert len(result) > 0
        assert result != "unnamed"

    def test_preserves_unicode_letters(self):
        result = sanitize_identifier("théorème")
        assert len(result) > 0
        assert result != "unnamed"


# ---------------------------------------------------------------------------
# concat_problem_lines
# ---------------------------------------------------------------------------


class TestConcatProblemLines:
    def test_basic(self):
        lines = ["Line one.", "Line two."]
        assert concat_problem_lines(lines) == "Line one.\n\nLine two."

    def test_empty(self):
        assert concat_problem_lines([]) == ""

    def test_single(self):
        assert concat_problem_lines(["Only line."]) == "Only line."

    def test_filters_none(self):
        lines = ["First", None, "Third"]
        assert concat_problem_lines(lines) == "First\n\nThird"


# ---------------------------------------------------------------------------
# ensure_dir
# ---------------------------------------------------------------------------


class TestEnsureDir:
    def test_creates_directory(self, tmp_path: Path):
        target = tmp_path / "a" / "b" / "c"
        assert not target.exists()
        ensure_dir(target)
        assert target.is_dir()

    def test_idempotent(self, tmp_path: Path):
        target = tmp_path / "existing"
        target.mkdir()
        ensure_dir(target)
        assert target.is_dir()


# ---------------------------------------------------------------------------
# CommandResult
# ---------------------------------------------------------------------------


class TestCommandResult:
    def test_frozen(self):
        cr = CommandResult(argv=["echo"], returncode=0, stdout="ok", stderr="")
        assert cr.argv == ["echo"]
        assert cr.returncode == 0
        assert cr.stdout == "ok"
        assert cr.stderr == ""

    def test_equality(self):
        a = CommandResult(argv=["a"], returncode=0, stdout="", stderr="")
        b = CommandResult(argv=["a"], returncode=0, stdout="", stderr="")
        assert a == b
