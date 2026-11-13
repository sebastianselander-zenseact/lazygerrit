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
    """What a rendered diff line refers to, for commenting. side None means REVISION."""

    path: str
    line: int
    side: Optional[str] = None


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


def file_as_added(text: str) -> list[DiffRow]:
    """Render a whole file (e.g. Gerrit's /COMMIT_MSG) as added lines."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [DiffRow("add", ln, None, i + 1) for i, ln in enumerate(lines)]
