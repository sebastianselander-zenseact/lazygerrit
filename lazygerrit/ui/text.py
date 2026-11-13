"""Styled text primitives: styles, spans, display width, truncation, wrapping, ANSI parsing."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from functools import lru_cache

DEFAULT_COLOR = -1
BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = range(8)
BRIGHT_BLACK = 8


@dataclass(frozen=True)
class Style:
    fg: int = DEFAULT_COLOR
    bg: int = DEFAULT_COLOR
    bold: bool = False
    dim: bool = False
    underline: bool = False
    reverse: bool = False
    italic: bool = False
    strike: bool = False

    def over(self, base: "Style") -> "Style":
        """Return this style layered on top of ``base`` (non-default fields win)."""
        return Style(
            fg=self.fg if self.fg != DEFAULT_COLOR else base.fg,
            bg=self.bg if self.bg != DEFAULT_COLOR else base.bg,
            bold=self.bold or base.bold,
            dim=self.dim or base.dim,
            underline=self.underline or base.underline,
            reverse=self.reverse or base.reverse,
            italic=self.italic or base.italic,
            strike=self.strike or base.strike,
        )

    def with_bg(self, bg: int) -> "Style":
        return replace(self, bg=bg)


PLAIN = Style()

Span = tuple[str, Style]
Line = list[Span]


def S(fg: int = DEFAULT_COLOR, bg: int = DEFAULT_COLOR, **attrs: bool) -> Style:
    return Style(fg=fg, bg=bg, **attrs)


def span(text: str, style: Style = PLAIN) -> Span:
    return (text, style)


def line(*parts: Span | str) -> Line:
    out: Line = []
    for p in parts:
        out.append((p, PLAIN) if isinstance(p, str) else p)
    return out


@lru_cache(maxsize=4096)
def char_width(ch: str) -> int:
    o = ord(ch)
    if o < 32 or 0x7F <= o < 0xA0:
        return 0
    if o < 0x300:
        return 1
    if unicodedata.combining(ch):
        return 0
    if o in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF) or 0xFE00 <= o <= 0xFE0F:
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def text_width(s: str) -> int:
    if s.isascii():
        return len(s)
    return sum(char_width(c) for c in s)


def line_width(ln: Line) -> int:
    return sum(text_width(t) for t, _ in ln)


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def sanitize(s: str, tab_width: int = 4) -> str:
    """Expand tabs and drop control characters so text is safe to draw."""
    if "\t" in s:
        out = []
        col = 0
        for ch in s:
            if ch == "\t":
                n = tab_width - (col % tab_width)
                out.append(" " * n)
                col += n
            else:
                out.append(ch)
                col += 1
        s = "".join(out)
    if "\n" in s:
        s = s.replace("\r\n", " ").replace("\n", " ")
    return _CONTROL_RE.sub("", s)


def text_block(text: str, style: Style = PLAIN) -> list[Line]:
    """Split possibly multi-line text into styled lines (one span per line)."""
    return [[(sanitize(ln), style)] for ln in text.replace("\r\n", "\n").split("\n")]


def truncate_str(s: str, width: int, ellipsis: str = "…") -> str:
    if width <= 0:
        return ""
    if text_width(s) <= width:
        return s
    ell_w = text_width(ellipsis)
    target = max(0, width - ell_w)
    out = []
    used = 0
    for ch in s:
        w = char_width(ch)
        if used + w > target:
            break
        out.append(ch)
        used += w
    return "".join(out) + (ellipsis if width >= ell_w else "")


def truncate(ln: Line, width: int, ellipsis: str = "…") -> Line:
    """Cut a styled line to ``width`` display cells, adding an ellipsis when cut."""
    if width <= 0:
        return []
    if line_width(ln) <= width:
        return list(ln)
    out: Line = []
    remaining = width - text_width(ellipsis)
    last_style = PLAIN
    for text, style in ln:
        if remaining <= 0:
            break
        w = text_width(text)
        last_style = style
        if w <= remaining:
            out.append((text, style))
            remaining -= w
        else:
            out.append((truncate_str(text, remaining, ellipsis=""), style))
            remaining = 0
            break
    out.append((ellipsis, last_style))
    return out


def slice_cells(ln: Line, start: int, width: int) -> Line:
    """Return the cells [start, start+width) of a styled line (for horizontal scrolling)."""
    if start <= 0:
        return clip(ln, width)
    out: Line = []
    pos = 0
    for text, style in ln:
        buf = []
        for ch in text:
            w = char_width(ch)
            if pos >= start and pos + w <= start + width:
                buf.append(ch)
            pos += w
        if buf:
            out.append(("".join(buf), style))
        if pos >= start + width:
            break
    return out


def clip(ln: Line, width: int) -> Line:
    """Cut a styled line to ``width`` cells without an ellipsis."""
    out: Line = []
    remaining = width
    for text, style in ln:
        if remaining <= 0:
            break
        w = text_width(text)
        if w <= remaining:
            out.append((text, style))
            remaining -= w
        else:
            out.append((truncate_str(text, remaining, ellipsis=""), style))
            break
    return out


def pad(ln: Line, width: int, style: Style = PLAIN) -> Line:
    w = line_width(ln)
    if w >= width:
        return clip(ln, width)
    return list(ln) + [(" " * (width - w), style)]


def pad_str(s: str, width: int, align: str = "left") -> str:
    s = truncate_str(s, width)
    gap = width - text_width(s)
    if gap <= 0:
        return s
    return (" " * gap + s) if align == "right" else (s + " " * gap)


def restyle(ln: Line, style: Style) -> Line:
    """Layer ``style`` over every span (e.g. a selection background)."""
    return [(t, style.over(s)) for t, s in ln]


def wrap(text: str, width: int) -> list[str]:
    """Greedy word wrap that respects display width and preserves leading indentation."""
    if width <= 1:
        return [text]
    result: list[str] = []
    for raw in text.split("\n"):
        raw = sanitize(raw)
        if text_width(raw) <= width:
            result.append(raw)
            continue
        indent = raw[: len(raw) - len(raw.lstrip(" "))]
        if text_width(indent) > width // 2:
            indent = ""
        words = raw.strip(" ").split(" ")
        cur = indent
        cur_w = text_width(cur)
        first_in_line = True
        for word in words:
            ww = text_width(word)
            sep = 0 if first_in_line else 1
            if cur_w + sep + ww <= width:
                cur += ("" if first_in_line else " ") + word
                cur_w += sep + ww
                first_in_line = False
                continue
            if not first_in_line:
                result.append(cur)
                cur, cur_w, first_in_line = indent, text_width(indent), True
            while text_width(word) > width - cur_w:
                avail = width - cur_w
                piece = truncate_str(word, avail, ellipsis="")
                if not piece:
                    break
                result.append(cur + piece)
                word = word[len(piece) :]
                cur, cur_w = indent, text_width(indent)
            cur += word
            cur_w += text_width(word)
            first_in_line = not word
        result.append(cur)
    return result


def wrap_line(ln: Line, width: int) -> list[Line]:
    """Hard-wrap a styled line at ``width`` cells (no word awareness)."""
    if width <= 0:
        return [ln]
    if line_width(ln) <= width:
        return [ln]
    rows: list[Line] = []
    cur: Line = []
    used = 0
    for text, style in ln:
        buf = []
        for ch in text:
            w = char_width(ch)
            if used + w > width:
                if buf:
                    cur.append(("".join(buf), style))
                rows.append(cur)
                cur, buf, used = [], [], 0
            buf.append(ch)
            used += w
        if buf:
            cur.append(("".join(buf), style))
    if cur:
        rows.append(cur)
    return rows


# --- ANSI SGR parsing -------------------------------------------------------------------------

# CSI (SGR and the rest, e.g. "\x1b[K" or "\x1b[?25l"), OSC (e.g. hyperlinks), charset and other escapes.
_ANSI_RE = re.compile(r"\x1b\[([0-?]*)[ -/]*([@-~])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[@-Z\\^_=>78]")

_CUBE = [0, 95, 135, 175, 215, 255]


def rgb_to_256(r: int, g: int, b: int) -> int:
    def nearest(v: int) -> int:
        return min(range(6), key=lambda i: abs(_CUBE[i] - v))

    ri, gi, bi = nearest(r), nearest(g), nearest(b)
    cube_idx = 16 + 36 * ri + 6 * gi + bi
    cube_rgb = (_CUBE[ri], _CUBE[gi], _CUBE[bi])
    gray = round((r + g + b) / 3)
    gi_ = max(0, min(23, round((gray - 8) / 10)))
    gray_rgb = 8 + 10 * gi_

    def dist(c: tuple[int, int, int]) -> int:
        return (c[0] - r) ** 2 + (c[1] - g) ** 2 + (c[2] - b) ** 2

    if dist((gray_rgb, gray_rgb, gray_rgb)) < dist(cube_rgb):
        return 232 + gi_
    return cube_idx


def _apply_sgr(params: str, style: Style) -> Style:
    codes = [int(p) if p.isdigit() else 0 for p in re.split(r"[;:]", params)] if params else [0]
    i = 0
    st = style
    while i < len(codes):
        c = codes[i]
        if c == 0:
            st = PLAIN
        elif c == 1:
            st = replace(st, bold=True)
        elif c == 2:
            st = replace(st, dim=True)
        elif c == 3:
            st = replace(st, italic=True)
        elif c == 4:
            st = replace(st, underline=True)
        elif c == 7:
            st = replace(st, reverse=True)
        elif c == 9:
            st = replace(st, strike=True)
        elif c == 22:
            st = replace(st, bold=False, dim=False)
        elif c == 23:
            st = replace(st, italic=False)
        elif c == 24:
            st = replace(st, underline=False)
        elif c == 27:
            st = replace(st, reverse=False)
        elif c == 29:
            st = replace(st, strike=False)
        elif 30 <= c <= 37:
            st = replace(st, fg=c - 30)
        elif c == 39:
            st = replace(st, fg=DEFAULT_COLOR)
        elif 40 <= c <= 47:
            st = replace(st, bg=c - 40)
        elif c == 49:
            st = replace(st, bg=DEFAULT_COLOR)
        elif 90 <= c <= 97:
            st = replace(st, fg=c - 90 + 8)
        elif 100 <= c <= 107:
            st = replace(st, bg=c - 100 + 8)
        elif c in (38, 48) and i + 1 < len(codes):
            mode = codes[i + 1]
            color = None
            if mode == 5 and i + 2 < len(codes):
                color = codes[i + 2]
                i += 2
            elif mode == 2 and i + 4 < len(codes):
                color = rgb_to_256(codes[i + 2], codes[i + 3], codes[i + 4])
                i += 4
            if color is not None:
                st = replace(st, fg=color) if c == 38 else replace(st, bg=color)
        i += 1
    return st


def ansi_to_lines(text: str) -> list[Line]:
    """Convert text with ANSI SGR escapes (e.g. from git/delta) into styled lines."""
    lines: list[Line] = []
    style = PLAIN
    for raw in text.split("\n"):
        cur: Line = []
        pos = 0
        for m in _ANSI_RE.finditer(raw):
            if m.start() > pos:
                cur.append((sanitize(raw[pos : m.start()]), style))
            if m.group(2) == "m" and (m.group(1) or "")[:1] not in ("<", "=", ">", "?"):
                style = _apply_sgr(m.group(1) or "", style)
            pos = m.end()
        if pos < len(raw):
            cur.append((sanitize(raw[pos:]), style))
        lines.append([(t, s) for t, s in cur if t])
    if lines and not lines[-1]:
        lines.pop()
    return lines


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def plain(ln: Line) -> str:
    return "".join(t for t, _ in ln)


def highlight_matches(ln: Line, term: str, style: Style) -> Line:
    """Layer ``style`` over every case-insensitive occurrence of ``term``."""
    if not term:
        return ln
    text = plain(ln)
    low, t = text.lower(), term.lower()
    marks = [False] * len(text)
    start = low.find(t)
    if start < 0:
        return ln
    while start >= 0:
        for i in range(start, start + len(t)):
            marks[i] = True
        start = low.find(t, start + len(t))
    out: Line = []
    pos = 0
    for seg, st in ln:
        buf = ""
        cur = None
        for ch in seg:
            m = marks[pos]
            if cur is not None and m != cur:
                out.append((buf, style.over(st) if cur else st))
                buf = ""
            cur = m
            buf += ch
            pos += 1
        if buf:
            out.append((buf, style.over(st) if cur else st))
    return out
