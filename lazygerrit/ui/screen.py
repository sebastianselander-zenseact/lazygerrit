"""Drawing surfaces (curses and an in-memory one for tests) and keyboard/mouse input decoding."""

from __future__ import annotations

import curses
from dataclasses import dataclass
from typing import Optional, Union

from .text import PLAIN, Line, Style, char_width, clip, sanitize, text_width


@dataclass(frozen=True)
class Key:
    name: str


@dataclass(frozen=True)
class Mouse:
    x: int
    y: int
    kind: str  # "left", "wheel-up", "wheel-down"


@dataclass(frozen=True)
class Paste:
    text: str


@dataclass(frozen=True)
class Resize:
    pass


Event = Union[Key, Mouse, Paste, Resize]


class Canvas:
    width: int = 80
    height: int = 24

    def clear(self) -> None:
        raise NotImplementedError

    def put(self, y: int, x: int, text: str, style: Style = PLAIN) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        pass

    def set_cursor(self, pos: Optional[tuple[int, int]]) -> None:
        pass

    def put_line(self, y: int, x: int, ln: Line, width: int, fill: Optional[Style] = None) -> None:
        """Draw spans clipped to ``width`` cells; optionally fill the remainder with ``fill``."""
        if y < 0 or y >= self.height or width <= 0:
            return
        col = x
        for text, style in clip(ln, width):
            if not text:
                continue
            self.put(y, col, text, style)
            col += text_width(text)
        if fill is not None and col < x + width:
            self.put(y, col, " " * (x + width - col), fill)


# --- curses ------------------------------------------------------------------------------------

_BASIC_RGB = [
    (0, 0, 0),
    (205, 0, 0),
    (0, 205, 0),
    (205, 205, 0),
    (0, 0, 238),
    (205, 0, 205),
    (0, 205, 205),
    (229, 229, 229),
    (127, 127, 127),
    (255, 0, 0),
    (0, 255, 0),
    (255, 255, 0),
    (92, 92, 255),
    (255, 0, 255),
    (0, 255, 255),
    (255, 255, 255),
]


def _xterm_rgb(c: int) -> tuple[int, int, int]:
    if c < 16:
        return _BASIC_RGB[c]
    if c < 232:
        c -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return steps[c // 36], steps[(c // 6) % 6], steps[c % 6]
    v = 8 + 10 * (c - 232)
    return v, v, v


def downgrade_color(c: int, available: int) -> int:
    if c < 0 or c < available:
        return c
    r, g, b = _xterm_rgb(c)
    limit = 16 if available >= 16 else 8
    return min(
        range(limit), key=lambda i: (_BASIC_RGB[i][0] - r) ** 2 + (_BASIC_RGB[i][1] - g) ** 2 + (_BASIC_RGB[i][2] - b) ** 2
    )


class CursesCanvas(Canvas):
    def __init__(self, scr: "curses.window"):
        self.scr = scr
        self.pairs: dict[tuple[int, int], int] = {}
        self.attr_cache: dict[Style, int] = {}
        self.has_colors = curses.has_colors()
        self.colors = curses.COLORS if self.has_colors else 0
        self.max_pairs = min(getattr(curses, "COLOR_PAIRS", 64), 32767)
        self.italic = getattr(curses, "A_ITALIC", 0)
        self.cursor: Optional[tuple[int, int]] = None
        self.resize()

    def resize(self) -> None:
        self.height, self.width = self.scr.getmaxyx()

    def _pair(self, fg: int, bg: int) -> int:
        key = (fg, bg)
        p = self.pairs.get(key)
        if p is not None:
            return p
        n = len(self.pairs) + 1
        if n >= self.max_pairs:
            return 0
        try:
            curses.init_pair(n, fg, bg)
        except curses.error:
            return 0
        self.pairs[key] = n
        return n

    def attr(self, st: Style) -> int:
        a = self.attr_cache.get(st)
        if a is not None:
            return a
        a = 0
        if self.has_colors and (st.fg != -1 or st.bg != -1):
            fg = downgrade_color(st.fg, self.colors)
            bg = downgrade_color(st.bg, self.colors)
            a |= curses.color_pair(self._pair(fg, bg))
        if st.bold:
            a |= curses.A_BOLD
        if st.dim:
            a |= curses.A_DIM
        if st.underline:
            a |= curses.A_UNDERLINE
        if st.reverse:
            a |= curses.A_REVERSE
        if st.italic:
            a |= self.italic
        self.attr_cache[st] = a
        return a

    def clear(self) -> None:
        self.scr.erase()

    def put(self, y: int, x: int, text: str, style: Style = PLAIN) -> None:
        if y < 0 or y >= self.height or x >= self.width or not text:
            return
        if x < 0:
            return
        if not text.isprintable():
            text = sanitize(text)
        try:
            self.scr.addstr(y, x, text, self.attr(style))
        except curses.error:
            pass

    def set_cursor(self, pos: Optional[tuple[int, int]]) -> None:
        self.cursor = pos

    def flush(self) -> None:
        if self.cursor is not None:
            try:
                curses.curs_set(1)
                self.scr.move(*self.cursor)
            except curses.error:
                pass
        else:
            try:
                curses.curs_set(0)
            except curses.error:
                pass
        self.scr.noutrefresh()
        curses.doupdate()


# --- in-memory canvas for tests ------------------------------------------------------------------


class VirtualCanvas(Canvas):
    def __init__(self, width: int = 160, height: int = 45):
        self.width = width
        self.height = height
        self.cursor: Optional[tuple[int, int]] = None
        self.clear()

    def clear(self) -> None:
        self.chars = [[" "] * self.width for _ in range(self.height)]
        self.styles = [[PLAIN] * self.width for _ in range(self.height)]

    def put(self, y: int, x: int, text: str, style: Style = PLAIN) -> None:
        if y < 0 or y >= self.height:
            return
        col = x
        for ch in sanitize(text):
            w = char_width(ch)
            if w == 0:
                continue
            if col < 0:
                col += w
                continue
            if col + w > self.width:
                break
            self.chars[y][col] = ch
            self.styles[y][col] = style
            if w == 2:
                self.chars[y][col + 1] = ""
                self.styles[y][col + 1] = style
            col += w

    def set_cursor(self, pos: Optional[tuple[int, int]]) -> None:
        self.cursor = pos

    def lines(self) -> list[str]:
        return ["".join(row).rstrip() for row in self.chars]

    def text(self) -> str:
        return "\n".join(self.lines())

    def find(self, needle: str) -> Optional[tuple[int, int]]:
        for y, row in enumerate(self.lines()):
            x = row.find(needle)
            if x >= 0:
                return y, x
        return None

    def style_at(self, y: int, x: int) -> Style:
        return self.styles[y][x]


# --- input -------------------------------------------------------------------------------------

_KEYMAP: dict[int, str] = {
    curses.KEY_UP: "<up>",
    curses.KEY_DOWN: "<down>",
    curses.KEY_LEFT: "<left>",
    curses.KEY_RIGHT: "<right>",
    curses.KEY_NPAGE: "<pgdown>",
    curses.KEY_PPAGE: "<pgup>",
    curses.KEY_HOME: "<home>",
    curses.KEY_END: "<end>",
    curses.KEY_BTAB: "<backtab>",
    curses.KEY_DC: "<delete>",
    curses.KEY_BACKSPACE: "<backspace>",
    curses.KEY_ENTER: "<enter>",
    curses.KEY_SR: "<s-up>",
    curses.KEY_SF: "<s-down>",
    curses.KEY_SLEFT: "<s-left>",
    curses.KEY_SRIGHT: "<s-right>",
    curses.KEY_IC: "<insert>",
}
for _i in range(1, 13):
    _KEYMAP[curses.KEY_F0 + _i] = f"<f{_i}>"

_EXT_NAMES = {
    b"kUP": "<s-up>",
    b"kDN": "<s-down>",
    b"kUP5": "<c-up>",
    b"kDN5": "<c-down>",
    b"kLFT5": "<c-left>",
    b"kRIT5": "<c-right>",
    b"kUP3": "<a-up>",
    b"kDN3": "<a-down>",
}

_CSI_MAP = {
    "A": "<up>",
    "B": "<down>",
    "C": "<right>",
    "D": "<left>",
    "H": "<home>",
    "F": "<end>",
    "Z": "<backtab>",
    "1;2A": "<s-up>",
    "1;2B": "<s-down>",
    "1;5A": "<c-up>",
    "1;5B": "<c-down>",
    "5~": "<pgup>",
    "6~": "<pgdown>",
    "3~": "<delete>",
    "1~": "<home>",
    "4~": "<end>",
}


def char_key(ch: str) -> Optional[str]:
    """Map a character from get_wch() to a key name (curses in raw + nonl mode)."""
    if ch == "\r":
        return "<enter>"
    if ch == "\n":
        return "<c-j>"
    if ch == "\t":
        return "<tab>"
    if ch in ("\x7f", "\x08"):
        return "<backspace>"
    if ch == " ":
        return "<space>"
    if ch == "\x00":
        return "<c-space>"
    o = ord(ch)
    if 1 <= o <= 26:
        return f"<c-{chr(o + 96)}>"
    if o < 32:
        return None
    return ch


class CursesInput:
    def __init__(self, scr: "curses.window"):
        self.scr = scr

    def _get(self, timeout_ms: int) -> Union[str, int, None]:
        self.scr.timeout(timeout_ms)
        try:
            return self.scr.get_wch()
        except curses.error:
            return None
        except KeyboardInterrupt:
            return "\x03"

    def read(self, timeout_ms: int) -> Optional[Event]:
        ch = self._get(timeout_ms)
        if ch is None:
            return None
        if isinstance(ch, int):
            if ch == curses.KEY_RESIZE:
                return Resize()
            if ch == curses.KEY_MOUSE:
                return self._mouse()
            name = _KEYMAP.get(ch)
            if name is None:
                try:
                    name = _EXT_NAMES.get(curses.keyname(ch))
                except (ValueError, curses.error):
                    name = None
            return Key(name) if name else None
        if ch == "\x1b":
            return self._escape()
        name = char_key(ch)
        return Key(name) if name else None

    def _escape(self) -> Optional[Event]:
        nxt = self._get(30)
        if nxt is None:
            return Key("<esc>")
        if isinstance(nxt, int):
            name = _KEYMAP.get(nxt)
            return Key(f"<a-{name.strip('<>')}>") if name else Key("<esc>")
        if nxt == "[":
            seq = ""
            while True:
                c = self._get(30)
                if not isinstance(c, str):
                    break
                seq += c
                if "\x40" <= c <= "\x7e" or len(seq) > 16:
                    break
            if seq == "200~":
                return Paste(self._read_paste())
            name = _CSI_MAP.get(seq)
            return Key(name) if name else None
        if nxt == "O":
            c = self._get(30)
            name = _CSI_MAP.get(c) if isinstance(c, str) else None
            return Key(name) if name else None
        if nxt == "\r":
            return Key("<a-enter>")
        if nxt == "\x1b":
            return Key("<esc>")
        name = char_key(nxt)
        if name and len(name) == 1:
            return Key(f"<a-{name}>")
        return Key("<esc>")

    def _read_paste(self) -> str:
        buf: list[str] = []
        end = "\x1b[201~"
        while True:
            c = self._get(500)
            if c is None:
                break
            if isinstance(c, int):
                continue
            buf.append(c)
            if len(buf) >= len(end) and "".join(buf[-len(end) :]) == end:
                del buf[-len(end) :]
                break
        return "".join(buf).replace("\r\n", "\n").replace("\r", "\n")

    def _mouse(self) -> Optional[Event]:
        try:
            _, x, y, _, state = curses.getmouse()
        except curses.error:
            return None
        if state & curses.BUTTON4_PRESSED:
            return Mouse(x, y, "wheel-up")
        if state & getattr(curses, "BUTTON5_PRESSED", 0x200000):
            return Mouse(x, y, "wheel-down")
        if state & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED):
            return Mouse(x, y, "left")
        return None
