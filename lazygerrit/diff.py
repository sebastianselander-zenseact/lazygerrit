"""Unified diff parsing with old/new line numbers (used to place and post inline comments)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class DiffRow:
    kind: str  # meta, hunk, ctx, add, del, nonl
    text: str
    old: Optional[int] = None
    new: Optional[int] = None


@dataclass(frozen=True)
class LineRef:
    """What a rendered diff line refers to, for commenting.

    ``side`` None means the REVISION side of ``patch_set``; "PARENT" its base. ``length`` is the
    line length (used as the end character of range comments).
    """

    path: str
    line: int
    side: Optional[str] = None
    patch_set: int = 0
    length: int = 0

    @property
    def anchor(self) -> tuple[str, int, Optional[str], int]:
        return (self.path, self.line, self.side, self.patch_set)


def parse_unified(text: str) -> list[DiffRow]:
    rows: list[DiffRow] = []
    old = new = 0
    rem_old = rem_new = 0
    for raw in text.split("\n"):
        if rem_old > 0 or rem_new > 0:
            if raw.startswith("+"):
                rows.append(DiffRow("add", raw[1:], None, new))
                new += 1
                rem_new -= 1
                continue
            if raw.startswith("-"):
                rows.append(DiffRow("del", raw[1:], old, None))
                old += 1
                rem_old -= 1
                continue
            if raw.startswith("\\"):
                rows.append(DiffRow("nonl", raw))
                continue
            rows.append(DiffRow("ctx", raw[1:] if raw.startswith(" ") else raw, old, new))
            old += 1
            new += 1
            rem_old -= 1
            rem_new -= 1
            continue
        m = _HUNK_RE.match(raw)
        if m:
            old = int(m.group(1))
            rem_old = int(m.group(2)) if m.group(2) is not None else 1
            new = int(m.group(3))
            rem_new = int(m.group(4)) if m.group(4) is not None else 1
            rows.append(DiffRow("hunk", raw))
            continue
        if raw.startswith("\\"):
            rows.append(DiffRow("nonl", raw))
            continue
        if raw == "":
            continue
        rows.append(DiffRow("meta", raw))
    return rows


def unified_text_diff(old: str, new: str, context: int = 3) -> list[DiffRow]:
    """Diff two in-memory texts (e.g. commit messages of two patch sets)."""
    import difflib

    lines = difflib.unified_diff(old.split("\n"), new.split("\n"), "a", "b", n=context, lineterm="")
    return parse_unified("\n".join(lines))


def file_as_added(text: str) -> list[DiffRow]:
    """Render a whole file (e.g. Gerrit's /COMMIT_MSG) as added lines."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [DiffRow("add", ln, None, i + 1) for i, ln in enumerate(lines)]
