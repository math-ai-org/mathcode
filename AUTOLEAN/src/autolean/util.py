from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_SANITIZE_RE = re.compile(r"[^0-9A-Za-z_\u00A0-\uFFFF]+")
_CJK_RE = re.compile(r"[\u3400-\u9FFF]")


def _to_pinyin(text: str) -> str:
    try:
        from pypinyin import lazy_pinyin
    except ImportError as exc:
        raise RuntimeError(
            "pypinyin is required to transliterate Chinese characters to pinyin. "
            "Install it with: pip install pypinyin"
        ) from exc

    def _keep(x: str) -> list[str]:
        return [x]

    tokens = lazy_pinyin(text, errors=_keep)
    return "".join(tokens)



def sanitize_identifier(raw: str) -> str:
    """Convert an arbitrary UUID-like string into a Lean/Python-friendly identifier.

    - Transliterates Chinese characters to pinyin.
    - Replaces path separators and whitespace with underscores.
    - Replaces any remaining disallowed characters with underscores.
    - Collapses repeated underscores.
    - Ensures non-empty output.
    """
    s = raw
    if _CJK_RE.search(s):
        s = _to_pinyin(s)
    s = s.replace("ü", "v").replace("Ü", "V")
    s = s.replace("/", "_").replace("\\", "_").replace(" ", "_").replace("-", "_")
    s = _SANITIZE_RE.sub("_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "unnamed"


def concat_problem_lines(lines: Iterable[str]) -> str:
    return "\n\n".join([ln for ln in lines if ln is not None])


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
