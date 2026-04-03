"""Tests for autolean.prompting."""

from __future__ import annotations

from pathlib import Path

import pytest

from autolean.prompting import PromptBundle, build_prompts


def _make_problem(uuid: str = "test-uuid-001", problem: list[str] | None = None) -> dict:
    return {
        "uuid": uuid,
        "problem": problem or ["Prove that 1 + 1 = 2."],
    }


# ---------------------------------------------------------------------------
# build_prompts – valid inputs
# ---------------------------------------------------------------------------


class TestBuildPromptsValid:
    def test_returns_prompt_bundle(self, tmp_path: Path):
        result = build_prompts(
            _make_problem(),
            out_dir=tmp_path,
            name_hint="test_uuid_001",
        )
        assert isinstance(result, PromptBundle)

    def test_theorem_name_prefix(self, tmp_path: Path):
        result = build_prompts(
            _make_problem(),
            out_dir=tmp_path,
            name_hint="test_uuid_001",
        )
        assert result.theorem_name.startswith("problem_")

    def test_lean_path_in_out_dir(self, tmp_path: Path):
        result = build_prompts(
            _make_problem(),
            out_dir=tmp_path,
            name_hint="test_uuid_001",
        )
        assert result.lean_path.parent == tmp_path
        assert result.lean_path.suffix == ".lean"

    def test_initial_prompt_contains_problem(self, tmp_path: Path):
        problem_text = "Prove that sqrt(2) is irrational."
        result = build_prompts(
            _make_problem(problem=[problem_text]),
            out_dir=tmp_path,
            name_hint="irr_sqrt2",
        )
        assert problem_text in result.initial_prompt

    def test_initial_thinking_contains_json(self, tmp_path: Path):
        result = build_prompts(
            _make_problem(),
            out_dir=tmp_path,
            name_hint="test",
        )
        assert "test-uuid-001" in result.initial_thinking_prompt

    def test_theorem_name_in_initial_prompt(self, tmp_path: Path):
        result = build_prompts(
            _make_problem(),
            out_dir=tmp_path,
            name_hint="hello_world",
        )
        assert result.theorem_name in result.initial_prompt

    def test_formalization_only_default(self, tmp_path: Path):
        result = build_prompts(
            _make_problem(),
            out_dir=tmp_path,
            name_hint="test",
            formalization_only=True,
        )
        assert "sorry" in result.initial_prompt

    def test_full_proof_mode(self, tmp_path: Path):
        result = build_prompts(
            _make_problem(),
            out_dir=tmp_path,
            name_hint="test",
            formalization_only=False,
        )
        assert "Full proof is allowed" in result.initial_prompt

    def test_repair_template_has_placeholders(self, tmp_path: Path):
        result = build_prompts(
            _make_problem(),
            out_dir=tmp_path,
            name_hint="test",
        )
        assert "{prev_lean}" in result.repair_prompt_template
        assert "{compile_output}" in result.repair_prompt_template

    def test_repair_thinking_template_has_placeholders(self, tmp_path: Path):
        result = build_prompts(
            _make_problem(),
            out_dir=tmp_path,
            name_hint="test",
        )
        assert "{prev_lean}" in result.repair_thinking_prompt_template
        assert "{compile_output}" in result.repair_thinking_prompt_template


# ---------------------------------------------------------------------------
# build_prompts – prior context
# ---------------------------------------------------------------------------


class TestBuildPromptsWithPriorContext:
    def test_prior_subproblems(self, tmp_path: Path):
        prior = [{"uuid": "prior-1", "problem": ["Part one."]}]
        result = build_prompts(
            _make_problem(),
            out_dir=tmp_path,
            name_hint="test",
            prior_subproblems=prior,
        )
        assert "prior-1" in result.initial_prompt
        assert "Prerequisite context" in result.initial_prompt

    def test_prior_formalizations(self, tmp_path: Path):
        prior_formal = [("prev_theorem", "theorem prev_theorem : True := trivial")]
        result = build_prompts(
            _make_problem(),
            out_dir=tmp_path,
            name_hint="test",
            prior_formalizations=prior_formal,
        )
        assert "prev_theorem" in result.initial_prompt

    def test_no_prior_context(self, tmp_path: Path):
        result = build_prompts(
            _make_problem(),
            out_dir=tmp_path,
            name_hint="test",
        )
        assert "Prerequisite context" not in result.initial_prompt


# ---------------------------------------------------------------------------
# build_prompts – validation
# ---------------------------------------------------------------------------


class TestBuildPromptsValidation:
    def test_missing_uuid_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="uuid"):
            build_prompts(
                {"problem": ["test"]},
                out_dir=tmp_path,
                name_hint="test",
            )

    def test_empty_uuid_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="uuid"):
            build_prompts(
                {"uuid": "  ", "problem": ["test"]},
                out_dir=tmp_path,
                name_hint="test",
            )

    def test_missing_problem_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="problem"):
            build_prompts(
                {"uuid": "ok"},
                out_dir=tmp_path,
                name_hint="test",
            )

    def test_problem_not_list_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="problem"):
            build_prompts(
                {"uuid": "ok", "problem": "not a list"},
                out_dir=tmp_path,
                name_hint="test",
            )

    def test_problem_with_non_string_items_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="problem"):
            build_prompts(
                {"uuid": "ok", "problem": [1, 2, 3]},
                out_dir=tmp_path,
                name_hint="test",
            )
