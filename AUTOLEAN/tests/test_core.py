"""Tests for autolean.core internal helpers."""

from __future__ import annotations

import json
from collections import OrderedDict
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import patch

import pytest

from autolean.core import (
    RunConfig,
    _backoff_sleep,
    _build_formalization_eval_prompt,
    _decode_incomplete_read_partial,
    _extract_compact_error_lines,
    _extract_model_response_text,
    _extract_openrouter_message_content,
    _format_error_memory,
    _is_codex_model_not_found,
    _is_gemini_flash_preview_model,
    _normalize_codex_model_name,
    _normalize_error_line,
    _parse_formalization_eval_payload,
    _parse_json_object_from_model_text,
    _parse_shell_assignment,
    _prompt_hash,
    _read_var_from_zshrc,
    _to_str_list,
    _update_error_memory,
    _write_text,
)
from autolean.util import CommandResult


# ---------------------------------------------------------------------------
# RunConfig
# ---------------------------------------------------------------------------


class TestRunConfig:
    def test_compile_argv_basic(self, tmp_path: Path):
        cfg = RunConfig(
            input_dir=tmp_path / "in",
            output_dir=tmp_path / "out",
            logs_dir=tmp_path / "logs",
        )
        lean_file = tmp_path / "test.lean"
        argv = cfg.compile_argv(lean_file)
        assert argv[0] == "lake"
        assert len(argv) == 4
        # shlex.split may mangle Windows backslashes; just check the filename is present
        assert "test.lean" in argv[-1]

    def test_compile_argv_custom_cmd(self, tmp_path: Path):
        cfg = RunConfig(
            input_dir=tmp_path / "in",
            output_dir=tmp_path / "out",
            logs_dir=tmp_path / "logs",
            compile_cmd="lean --run {file}",
        )
        lean_file = tmp_path / "test.lean"
        argv = cfg.compile_argv(lean_file)
        assert argv[0] == "lean"
        assert argv[1] == "--run"
        assert "test.lean" in argv[2]

    def test_frozen_dataclass(self, tmp_path: Path):
        cfg = RunConfig(
            input_dir=tmp_path,
            output_dir=tmp_path,
            logs_dir=tmp_path,
        )
        with pytest.raises(AttributeError):
            cfg.max_iters = 99


# ---------------------------------------------------------------------------
# _parse_shell_assignment
# ---------------------------------------------------------------------------


class TestParseShellAssignment:
    def test_simple(self):
        assert _parse_shell_assignment("FOO=bar") == ("FOO", "bar")

    def test_export_prefix(self):
        assert _parse_shell_assignment("export MY_KEY=secret123") == ("MY_KEY", "secret123")

    def test_single_quoted(self):
        assert _parse_shell_assignment("VAR='hello world'") == ("VAR", "hello world")

    def test_double_quoted(self):
        assert _parse_shell_assignment('VAR="hello world"') == ("VAR", "hello world")

    def test_inline_comment(self):
        assert _parse_shell_assignment("KEY=value # comment") == ("KEY", "value")

    def test_empty_value(self):
        assert _parse_shell_assignment("KEY=") is None

    def test_comment_line(self):
        assert _parse_shell_assignment("# comment") is None

    def test_empty_line(self):
        assert _parse_shell_assignment("") is None

    def test_no_equals(self):
        assert _parse_shell_assignment("just a line") is None

    def test_whitespace(self):
        assert _parse_shell_assignment("  KEY = value  ") == ("KEY", "value")

    def test_export_empty_value(self):
        assert _parse_shell_assignment("export EMPTY=") is None

    def test_quoted_with_hash(self):
        result = _parse_shell_assignment("KEY='value # not a comment'")
        assert result == ("KEY", "value # not a comment")


# ---------------------------------------------------------------------------
# _read_var_from_zshrc
# ---------------------------------------------------------------------------


class TestReadVarFromZshrc:
    def test_reads_last_assignment(self, tmp_path: Path):
        zshrc = tmp_path / ".zshrc"
        zshrc.write_text("MY_VAR=first\nMY_VAR=second\n", encoding="utf-8")
        assert _read_var_from_zshrc("MY_VAR", zshrc_path=zshrc) == "second"

    def test_missing_var(self, tmp_path: Path):
        zshrc = tmp_path / ".zshrc"
        zshrc.write_text("OTHER=value\n", encoding="utf-8")
        assert _read_var_from_zshrc("MISSING", zshrc_path=zshrc) is None

    def test_missing_file(self, tmp_path: Path):
        zshrc = tmp_path / "nonexistent"
        assert _read_var_from_zshrc("ANY", zshrc_path=zshrc) is None


# ---------------------------------------------------------------------------
# _normalize_codex_model_name
# ---------------------------------------------------------------------------


class TestNormalizeCodexModelName:
    def test_none(self):
        assert _normalize_codex_model_name(None) is None

    def test_empty(self):
        assert _normalize_codex_model_name("") is None

    def test_whitespace(self):
        assert _normalize_codex_model_name("   ") is None

    def test_strips_openai_prefix(self):
        assert _normalize_codex_model_name("openai/gpt-5.2") == "gpt-5.2"

    def test_preserves_non_openai(self):
        assert _normalize_codex_model_name("my-model") == "my-model"

    def test_strips_whitespace(self):
        assert _normalize_codex_model_name("  gpt-5.2  ") == "gpt-5.2"

    def test_openai_prefix_case_insensitive(self):
        assert _normalize_codex_model_name("OpenAI/gpt-5.2") == "gpt-5.2"


# ---------------------------------------------------------------------------
# _is_codex_model_not_found
# ---------------------------------------------------------------------------


class TestIsCodexModelNotFound:
    def test_model_not_found(self):
        assert _is_codex_model_not_found("Error: model_not_found") is True

    def test_does_not_exist(self):
        assert _is_codex_model_not_found("The model does not exist.") is True

    def test_unrelated_error(self):
        assert _is_codex_model_not_found("Connection timeout") is False

    def test_empty(self):
        assert _is_codex_model_not_found("") is False


# ---------------------------------------------------------------------------
# _is_gemini_flash_preview_model
# ---------------------------------------------------------------------------


class TestIsGeminiFlashPreviewModel:
    def test_exact_match(self):
        assert _is_gemini_flash_preview_model("google/gemini-3-flash-preview") is True

    def test_with_whitespace(self):
        assert _is_gemini_flash_preview_model("  google/gemini-3-flash-preview  ") is True

    def test_case_insensitive(self):
        assert _is_gemini_flash_preview_model("Google/Gemini-3-Flash-Preview") is True

    def test_different_model(self):
        assert _is_gemini_flash_preview_model("openai/gpt-5.2") is False


# ---------------------------------------------------------------------------
# _extract_compact_error_lines
# ---------------------------------------------------------------------------


class TestExtractCompactErrorLines:
    def test_extracts_error_lines(self):
        cr = CommandResult(
            argv=["lean"],
            returncode=1,
            stdout="",
            stderr="file.lean:1:0: error: unknown identifier 'foo'\nwarning: unused var",
        )
        lines = _extract_compact_error_lines(cr)
        assert len(lines) >= 1
        assert any("error" in l.lower() for l in lines)

    def test_empty_output(self):
        cr = CommandResult(argv=["lean"], returncode=0, stdout="", stderr="")
        assert _extract_compact_error_lines(cr) == []

    def test_fallback_to_first_line(self):
        cr = CommandResult(
            argv=["lean"],
            returncode=1,
            stdout="some output without error keyword",
            stderr="",
        )
        lines = _extract_compact_error_lines(cr)
        assert len(lines) == 1

    def test_parse_failure(self):
        cr = CommandResult(
            argv=["lean"],
            returncode=1,
            stdout="",
            stderr="parse failure at position 42",
        )
        lines = _extract_compact_error_lines(cr)
        assert len(lines) >= 1


# ---------------------------------------------------------------------------
# _normalize_error_line
# ---------------------------------------------------------------------------


class TestNormalizeErrorLine:
    def test_strips_location_prefix(self):
        line = "file.lean:10:5: error: type mismatch"
        result = _normalize_error_line(line)
        assert result == "error: type mismatch"

    def test_collapses_whitespace(self):
        result = _normalize_error_line("  too   much   space  ")
        assert result == "too much space"

    def test_empty(self):
        assert _normalize_error_line("") == ""

    def test_windows_path_prefix(self):
        line = "C:\\Users\\test\\file.lean:1:0: error: test"
        result = _normalize_error_line(line)
        assert result == "error: test"


# ---------------------------------------------------------------------------
# _update_error_memory / _format_error_memory
# ---------------------------------------------------------------------------


class TestErrorMemory:
    def test_update_adds_entry(self):
        memory: OrderedDict[str, tuple[str, int, int]] = OrderedDict()
        cr = CommandResult(
            argv=["lean"], returncode=1, stdout="", stderr="error: unknown id"
        )
        _update_error_memory(memory, cr, iter_no=1)
        assert len(memory) == 1

    def test_update_increments_count(self):
        memory: OrderedDict[str, tuple[str, int, int]] = OrderedDict()
        cr = CommandResult(
            argv=["lean"], returncode=1, stdout="", stderr="error: same error"
        )
        _update_error_memory(memory, cr, iter_no=1)
        _update_error_memory(memory, cr, iter_no=2)
        values = list(memory.values())
        assert values[0][1] == 2  # count
        assert values[0][2] == 2  # last_iter

    def test_format_empty(self):
        assert _format_error_memory(OrderedDict(), limit=5) == ""

    def test_format_shows_count(self):
        memory: OrderedDict[str, tuple[str, int, int]] = OrderedDict()
        memory["err"] = ("error: test", 3, 5)
        result = _format_error_memory(memory, limit=5)
        assert "seen 3x" in result
        assert "iter 5" in result

    def test_format_limit(self):
        memory: OrderedDict[str, tuple[str, int, int]] = OrderedDict()
        for i in range(10):
            memory[f"err{i}"] = (f"error {i}", 1, i)
        result = _format_error_memory(memory, limit=3)
        lines = [l for l in result.strip().split("\n") if l.strip()]
        assert len(lines) == 3


# ---------------------------------------------------------------------------
# _extract_openrouter_message_content
# ---------------------------------------------------------------------------


class TestExtractOpenrouterMessageContent:
    def test_string_content(self):
        resp = {"choices": [{"message": {"content": "Hello world"}}]}
        assert _extract_openrouter_message_content(resp) == "Hello world"

    def test_list_content(self):
        resp = {
            "choices": [
                {"message": {"content": [{"text": "part1"}, {"text": "part2"}]}}
            ]
        }
        assert _extract_openrouter_message_content(resp) == "part1part2"

    def test_reasoning_fallback(self):
        resp = {
            "choices": [{"message": {"content": "", "reasoning": "deep thought"}}]
        }
        assert _extract_openrouter_message_content(resp) == "deep thought"

    def test_missing_choices_raises(self):
        with pytest.raises(ValueError, match="choices"):
            _extract_openrouter_message_content({})

    def test_empty_choices_raises(self):
        with pytest.raises(ValueError, match="choices"):
            _extract_openrouter_message_content({"choices": []})

    def test_missing_message_raises(self):
        with pytest.raises(ValueError, match="message"):
            _extract_openrouter_message_content({"choices": [{"finish_reason": "stop"}]})

    def test_empty_content_no_fallback_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _extract_openrouter_message_content(
                {"choices": [{"message": {"content": ""}}]}
            )


# ---------------------------------------------------------------------------
# _extract_model_response_text
# ---------------------------------------------------------------------------


class TestExtractModelResponseText:
    def test_plain_text(self):
        assert _extract_model_response_text("just plain text") == "just plain text"

    def test_openrouter_envelope(self):
        envelope = json.dumps(
            {"choices": [{"message": {"content": "extracted"}}]}
        )
        assert _extract_model_response_text(envelope) == "extracted"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _extract_model_response_text("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _extract_model_response_text("   ")


# ---------------------------------------------------------------------------
# _parse_json_object_from_model_text
# ---------------------------------------------------------------------------


class TestParseJsonObjectFromModelText:
    def test_plain_json(self):
        result = _parse_json_object_from_model_text('{"lean": "theorem x : True"}')
        assert result == {"lean": "theorem x : True"}

    def test_json_in_code_fence(self):
        text = '```json\n{"lean": "code"}\n```'
        result = _parse_json_object_from_model_text(text)
        assert result == {"lean": "code"}

    def test_json_embedded_in_text(self):
        text = 'Here is the result: {"lean": "code"} done.'
        result = _parse_json_object_from_model_text(text)
        assert result == {"lean": "code"}

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _parse_json_object_from_model_text("")

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="parse"):
            _parse_json_object_from_model_text("no json here at all")

    def test_array_not_accepted(self):
        with pytest.raises(ValueError, match="parse"):
            _parse_json_object_from_model_text("[1, 2, 3]")


# ---------------------------------------------------------------------------
# _parse_formalization_eval_payload
# ---------------------------------------------------------------------------


class TestParseFormalizationEvalPayload:
    def test_valid_payload(self):
        payload = {
            "grade": "A",
            "summary": "Fully faithful.",
            "distance_from_original": "None.",
            "key_mismatches": [],
        }
        result = _parse_formalization_eval_payload(payload)
        assert result["grade"] == "A"
        assert result["summary"] == "Fully faithful."

    def test_normalizes_grade_case(self):
        result = _parse_formalization_eval_payload({"grade": "b"})
        assert result["grade"] == "B"

    def test_missing_grade_raises(self):
        with pytest.raises(ValueError, match="grade"):
            _parse_formalization_eval_payload({})

    def test_invalid_grade_raises(self):
        with pytest.raises(ValueError, match="A/B/C/D"):
            _parse_formalization_eval_payload({"grade": "F"})

    def test_alternative_field_names(self):
        payload = {
            "grade": "C",
            "verdict": "Partial match.",
            "distance": "Missing parts.",
            "mismatches": ["missing sub-question 2"],
        }
        result = _parse_formalization_eval_payload(payload)
        assert result["summary"] == "Partial match."
        assert result["distance_from_original"] == "Missing parts."
        assert result["key_mismatches"] == ["missing sub-question 2"]

    def test_non_string_grade_raises(self):
        with pytest.raises(ValueError, match="grade"):
            _parse_formalization_eval_payload({"grade": 42})


# ---------------------------------------------------------------------------
# _to_str_list
# ---------------------------------------------------------------------------


class TestToStrList:
    def test_basic(self):
        assert _to_str_list(["a", "b", "c"]) == ["a", "b", "c"]

    def test_filters_non_strings(self):
        assert _to_str_list(["a", 1, None, "b"]) == ["a", "b"]

    def test_filters_empty_strings(self):
        assert _to_str_list(["a", "", "  ", "b"]) == ["a", "b"]

    def test_not_a_list(self):
        assert _to_str_list("not a list") == []

    def test_limit(self):
        assert len(_to_str_list(["a"] * 20, limit=5)) == 5


# ---------------------------------------------------------------------------
# _decode_incomplete_read_partial
# ---------------------------------------------------------------------------


class TestDecodeIncompleteReadPartial:
    def test_bytes_partial(self):
        exc = IncompleteRead(b"partial data", 100)
        assert _decode_incomplete_read_partial(exc) == "partial data"

    def test_empty_partial(self):
        exc = IncompleteRead(b"", 0)
        assert _decode_incomplete_read_partial(exc) == ""


# ---------------------------------------------------------------------------
# _prompt_hash
# ---------------------------------------------------------------------------


class TestPromptHash:
    def test_deterministic(self):
        h1 = _prompt_hash("test prompt")
        h2 = _prompt_hash("test prompt")
        assert h1 == h2

    def test_different_inputs(self):
        h1 = _prompt_hash("prompt A")
        h2 = _prompt_hash("prompt B")
        assert h1 != h2

    def test_returns_hex(self):
        h = _prompt_hash("test")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# _write_text
# ---------------------------------------------------------------------------


class TestWriteText:
    def test_creates_file(self, tmp_path: Path):
        target = tmp_path / "sub" / "file.txt"
        _write_text(target, "hello")
        assert target.read_text(encoding="utf-8") == "hello"

    def test_creates_parent_dirs(self, tmp_path: Path):
        target = tmp_path / "a" / "b" / "c" / "file.txt"
        _write_text(target, "content")
        assert target.exists()


# ---------------------------------------------------------------------------
# _build_formalization_eval_prompt
# ---------------------------------------------------------------------------


class TestBuildFormalizationEvalPrompt:
    def test_contains_problem_and_lean(self):
        prompt = _build_formalization_eval_prompt(
            problem_json={"uuid": "test", "problem": ["Prove P."]},
            theorem_name="problem_test",
            lean_code="theorem problem_test : True := trivial",
        )
        assert "problem_test" in prompt
        assert "Prove P." in prompt
        assert "theorem problem_test" in prompt
        assert "A|B|C|D" in prompt


# ---------------------------------------------------------------------------
# _backoff_sleep
# ---------------------------------------------------------------------------


class TestBackoffSleep:
    def test_does_not_raise(self):
        with patch("autolean.core.time.sleep") as mock_sleep:
            _backoff_sleep(0)
            mock_sleep.assert_called_once()
            delay = mock_sleep.call_args[0][0]
            assert 0 < delay <= 4.0

    def test_caps_at_4_seconds(self):
        with patch("autolean.core.time.sleep") as mock_sleep:
            _backoff_sleep(100)
            delay = mock_sleep.call_args[0][0]
            assert delay == 4.0
