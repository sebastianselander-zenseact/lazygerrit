"""Panels and popups rendered in lazygit's style."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from . import theme
from .layout import Rect
from .screen import Canvas, Event, Key, Paste
from .text import (
    PLAIN,
    Line,
    Style,
    char_width,
    clip,
    highlight_matches,
    line_width,
    pad,
    plain,
    restyle,
    sanitize,
    slice_cells,
    text_width,
    truncate,
    truncate_str,
    wrap,
    wrap_line,
)

SELECTED = Style(bg=theme.SELECTED_BG)
MATCH = Style(fg=0, bg=3)


def draw_frame(
    canvas: Canvas,
    rect: Rect,
    *,
    title: Line,
    border: Style,
    footer: str = "",
    scroll: Optional[tuple[int, int, int]] = None,
) -> None:
    if rect.h < 2 or rect.w < 2:
        return
    b = theme.BOX_ROUNDED
    y, x, h, w = rect.y, rect.x, rect.h, rect.w
    canvas.put(y, x, b["tl"] + b["h"] * (w - 2) + b["tr"], border)
    if title and w > 4:
        canvas.put_line(y, x + 2, truncate(title, w - 4), w - 4)
    for r in range(1, h - 1):
        canvas.put(y + r, x, b["v"], border)
        canvas.put(y + r, x + w - 1, b["v"], border)
    canvas.put(y + h - 1, x, b["bl"] + b["h"] * (w - 2) + b["br"], border)
    if footer:
        fw = text_width(footer)
        fx = x + w - 2 - fw
        if fx > x + 1:
            canvas.put(y + h - 1, fx, footer, border)
    if scroll and h > 2:
        offset, visible, total = scroll
        if total > visible > 0:
            track = h - 2
            thumb = max(1, round(track * visible / total))
            max_off = max(1, total - visible)
            pos = round((track - thumb) * min(offset, max_off) / max_off)
            for r in range(thumb):
                canvas.put(y + 1 + pos + r, x + w - 1, theme.SCROLL_THUMB, border)


def panel_title(
    border: Style,
    jump: Optional[str],
    title: str,
    tabs: Sequence[str] = (),
    tab: int = 0,
    focused: bool = False,
    suffix: str = "",
) -> Line:
    parts: Line = []
    if jump:
        parts += [(f"[{jump}]", border), (theme.BOX_ROUNDED["h"], border)]
    if tabs:
        for i, t in enumerate(tabs):
            if i:
                parts.append((" - ", border))
            if i == tab:
                parts.append((t, theme.TITLE_TAB_ACTIVE if focused else theme.TITLE_TAB_SELECTED))
            else:
                parts.append((t, PLAIN))
    else:
        parts.append((title, border))
    if suffix:
        parts.append((suffix, border))
    return parts


def fit_title(
    border: Style,
    jump: Optional[str],
    title: str,
    tabs: Sequence[str],
    tab: int,
    focused: bool,
    suffix: str,
    width: int,
) -> Line:
    """Full tab list if it fits; otherwise abbreviate unselected tabs, then show only the current one."""
    full = panel_title(border, jump, title, tabs, tab, focused, suffix)
    if not tabs or line_width(full) <= width:
        return full
    short = [t if i == tab else t[:3] for i, t in enumerate(tabs)]
    abbreviated = panel_title(border, jump, title, short, tab, focused, suffix)
    if line_width(abbreviated) <= width:
        return abbreviated
    only = panel_title(border, jump, title, [f"{tabs[tab]} ({tab + 1}/{len(tabs)})"], 0, focused, suffix)
    return only


class ListPanel:
    def __init__(self, name: str, title: str = "", jump: Optional[str] = None, tabs: Sequence[str] = ()):
        self.name = name
        self.title = title
        self.jump = jump
        self.tabs = list(tabs)
        self.tab = 0
        self.items: list[Any] = []
        self.selected = 0
        self.scroll = 0
        self.anchor: Optional[int] = None
        self.filter = ""
        self.filter_fn: Callable[[Any], str] = lambda it: str(it)
        self.row_fn: Callable[[Any, int], Line] = lambda it, w: [(str(it), PLAIN)]
        self.key_fn: Callable[[Any], Any] = lambda it: id(it)
        self.empty_text = ""
        self.suffix = ""
        self.rect: Optional[Rect] = None
        self._view_cache: Optional[tuple[int, str, list[Any]]] = None

    # -- data ---------------------------------------------------------------------------------

    def view(self) -> list[Any]:
        if not self.filter:
            return self.items
        c = self._view_cache
        if c and c[0] == id(self.items) and c[1] == self.filter:
            return c[2]
        f = self.filter.lower()
        result = [it for it in self.items if f in self.filter_fn(it).lower()]
        self._view_cache = (id(self.items), self.filter, result)
        return result

    def set_items(self, items: Sequence[Any], keep: bool = True) -> None:
        cur = self.current() if keep else None
        old_key = self.key_fn(cur) if cur is not None else None
        anchor_key = None
        range_keys: set[Any] = set()
        old_view = self.view()
        if keep and self.anchor is not None and self.anchor < len(old_view):
            lo, hi = self.range_bounds()
            anchor_key = self.key_fn(old_view[self.anchor])
            range_keys = {self.key_fn(it) for it in old_view[lo : hi + 1]}
        self.anchor = None
        self.items = list(items)
        self._view_cache = None
        v = self.view()
        if old_key is not None:
            for i, it in enumerate(v):
                if self.key_fn(it) == old_key:
                    self.selected = i
                    break
            else:
                self.selected = min(self.selected, max(0, len(v) - 1))
        else:
            self.selected = 0 if not keep else min(self.selected, max(0, len(v) - 1))
        if anchor_key is not None:
            keys = [self.key_fn(it) for it in v]
            if anchor_key in keys:
                a = keys.index(anchor_key)
                lo, hi = min(a, self.selected), max(a, self.selected)
                # Keep the range only if it still covers exactly the same items (the list may have been re-sorted).
                if set(keys[lo : hi + 1]) == range_keys:
                    self.anchor = a

    def set_filter(self, text: str) -> None:
        cur = self.current()
        self.filter = text
        self._view_cache = None
        v = self.view()
        if cur is not None and cur in v:
            self.selected = v.index(cur)
        else:
            self.selected = 0
        self.anchor = None

    def current(self) -> Any:
        v = self.view()
        return v[self.selected] if 0 <= self.selected < len(v) else None

    def range_bounds(self) -> tuple[int, int]:
        if self.anchor is None:
            return self.selected, self.selected
        return min(self.anchor, self.selected), max(self.anchor, self.selected)

    def selection(self) -> list[Any]:
        v = self.view()
        if not v:
            return []
        lo, hi = self.range_bounds()
        return v[lo : hi + 1]

    @property
    def page_size(self) -> int:
        return max(1, (self.rect.h - 2) if self.rect else 10)

    def move(self, delta: int) -> None:
        n = len(self.view())
        if n == 0:
            self.selected = 0
            return
        self.selected = max(0, min(n - 1, self.selected + delta))

    def goto(self, idx: int) -> None:
        n = len(self.view())
        self.selected = max(0, min(n - 1, idx)) if n else 0

    def toggle_range(self) -> None:
        self.anchor = None if self.anchor is not None else self.selected

    def range_move(self, delta: int) -> None:
        if self.anchor is None:
            self.anchor = self.selected
        self.move(delta)

    def index_at(self, screen_y: int) -> Optional[int]:
        if not self.rect:
            return None
        row = screen_y - self.rect.y - 1
        if row < 0 or row >= self.rect.h - 2:
            return None
        idx = self.scroll + row
        return idx if idx < len(self.view()) else None

    # -- drawing ------------------------------------------------------------------------------

    def render(self, canvas: Canvas, focused: bool, searching: bool = False) -> None:
        rect = self.rect
        if rect is None or rect.h < 2:
            return
        inner = rect.inner
        v = self.view()
        h = inner.h
        if self.selected < self.scroll:
            self.scroll = self.selected
        if self.selected >= self.scroll + h:
            self.scroll = self.selected - h + 1
        self.scroll = max(0, min(self.scroll, max(0, len(v) - h)))
        lo, hi = self.range_bounds()
        for row in range(h):
            idx = self.scroll + row
            if idx >= len(v):
                break
            ln = self.row_fn(v[idx], inner.w)
            if lo <= idx <= hi and focused:
                canvas.put_line(inner.y + row, inner.x, restyle(truncate(ln, inner.w), SELECTED), inner.w, fill=SELECTED)
            elif idx == self.selected:
                canvas.put_line(inner.y + row, inner.x, restyle(truncate(ln, inner.w), theme.INACTIVE_SELECTED), inner.w)
            else:
                canvas.put_line(inner.y + row, inner.x, truncate(ln, inner.w), inner.w)
        if not v and self.empty_text and h > 0:
            for i, txt in enumerate(wrap(self.empty_text, inner.w)[:h]):
                canvas.put(inner.y + i, inner.x, txt, theme.DIM)
        border = theme.SEARCH_BORDER if searching else (theme.ACTIVE_BORDER if focused else theme.INACTIVE_BORDER)
        footer = f"{self.selected + 1} of {len(v)}" if v else ""
        if self.filter and not searching:
            footer = f"filter: {self.filter} · {footer}"
        title = fit_title(border, self.jump, self.title, self.tabs, self.tab, focused, self.suffix, rect.w - 4)
        draw_frame(canvas, rect, title=title, border=border, footer=footer, scroll=(self.scroll, h, len(v)))


class TextPanel:
    """Scrollable text; optional line cursor (used for diffs so lines can be commented)."""

    def __init__(self, name: str, title: str = "", jump: Optional[str] = None):
        self.name = name
        self.title = title
        self.jump = jump
        self.lines: list[Line] = []
        self.meta: Optional[list[Any]] = None
        self.wrap = True
        self.scroll = 0
        self.hscroll = 0
        self.cursor = 0
        self.cursor_mode = False
        self.anchor: Optional[int] = None
        self.search = ""
        self.rect: Optional[Rect] = None
        self.footer = ""
        self._rows_cache: Optional[tuple[int, int, list[Line], list[int]]] = None
        self.version = 0

    def set_lines(
        self,
        lines: list[Line],
        *,
        title: Optional[str] = None,
        wrap: bool = True,
        meta: Optional[list[Any]] = None,
        keep_scroll: bool = False,
        cursor_mode: bool = False,
    ) -> None:
        self.lines = lines
        self.meta = meta
        self.wrap = wrap
        self.cursor_mode = cursor_mode
        if title is not None:
            self.title = title
        self._rows_cache = None
        self.version += 1
        if not keep_scroll:
            self.scroll = 0
            self.hscroll = 0
            self.cursor = 0
            self.anchor = None

    @property
    def width(self) -> int:
        return max(1, (self.rect.w - 2) if self.rect else 80)

    @property
    def height(self) -> int:
        return max(1, (self.rect.h - 2) if self.rect else 20)

    def rows(self) -> tuple[list[Line], list[int]]:
        w = self.width
        c = self._rows_cache
        if c and c[0] == w and c[1] == self.version:
            return c[2], c[3]
        rows: list[Line] = []
        src: list[int] = []
        for i, ln in enumerate(self.lines):
            if self.wrap:
                for part in wrap_line(ln, w):
                    rows.append(part)
                    src.append(i)
            else:
                rows.append(ln)
                src.append(i)
        self._rows_cache = (w, self.version, rows, src)
        return rows, src

    def max_scroll(self) -> int:
        rows, _ = self.rows()
        return max(0, len(rows) - self.height)

    def scroll_by(self, n: int) -> None:
        self.scroll = max(0, min(self.max_scroll(), self.scroll + n))
        if self.cursor_mode:
            self.cursor = max(self.scroll, min(self.cursor, self.scroll + self.height - 1))

    def scroll_to(self, row: int) -> None:
        self.scroll = max(0, min(self.max_scroll(), row))

    def move_cursor(self, delta: int) -> None:
        rows, _ = self.rows()
        if not rows:
            return
        self.cursor = max(0, min(len(rows) - 1, self.cursor + delta))
        self._follow_cursor()

    def set_cursor(self, row: int) -> None:
        rows, _ = self.rows()
        self.cursor = max(0, min(max(0, len(rows) - 1), row))
        self._follow_cursor()

    def _follow_cursor(self) -> None:
        h = self.height
        margin = 2 if h > 8 else 0
        if self.cursor < self.scroll + margin:
            self.scroll = max(0, self.cursor - margin)
        elif self.cursor >= self.scroll + h - margin:
            self.scroll = min(self.max_scroll(), self.cursor - h + 1 + margin)

    def cursor_meta(self) -> Any:
        if not self.meta:
            return None
        _, src = self.rows()
        if not (0 <= self.cursor < len(src)):
            return None
        i = src[self.cursor]
        return self.meta[i] if i < len(self.meta) else None

    def find(self, term: str, forward: bool = True, start: Optional[int] = None) -> Optional[int]:
        if not term:
            return None
        rows, _ = self.rows()
        t = term.lower()
        base = start if start is not None else (self.cursor if self.cursor_mode else self.scroll)
        n = len(rows)
        for k in range(1, n + 1):
            i = (base + k) % n if forward else (base - k) % n
            if t in plain(rows[i]).lower():
                return i
        return None

    def goto_row(self, row: int) -> None:
        if self.cursor_mode:
            self.set_cursor(row)
        else:
            self.scroll_to(max(0, row - self.height // 3))

    def render(self, canvas: Canvas, focused: bool, searching: bool = False) -> None:
        rect = self.rect
        if rect is None or rect.h < 2:
            return
        inner = rect.inner
        rows, _ = self.rows()
        self.scroll = max(0, min(self.scroll, max(0, len(rows) - inner.h)))
        for r in range(inner.h):
            idx = self.scroll + r
            if idx >= len(rows):
                break
            ln = rows[idx]
            if self.hscroll and not self.wrap:
                ln = slice_cells(ln, self.hscroll, inner.w)
            if self.search:
                ln = highlight_matches(ln, self.search, MATCH)
            in_range = self.anchor is not None and min(self.anchor, self.cursor) <= idx <= max(self.anchor, self.cursor)
            if self.cursor_mode and focused and (idx == self.cursor or in_range):
                canvas.put_line(inner.y + r, inner.x, restyle(clip(ln, inner.w), SELECTED), inner.w, fill=SELECTED)
            else:
                canvas.put_line(inner.y + r, inner.x, ln, inner.w)
        border = theme.SEARCH_BORDER if searching else (theme.ACTIVE_BORDER if focused else theme.INACTIVE_BORDER)
        title = panel_title(border, self.jump, self.title)
        footer = self.footer
        draw_frame(canvas, rect, title=title, border=border, footer=footer, scroll=(self.scroll, inner.h, len(rows)))


# --- popups ------------------------------------------------------------------------------------


class Popup:
    title = ""
    closed = False
    border = theme.ACTIVE_BORDER

    def render(self, canvas: Canvas) -> None:
        raise NotImplementedError

    def handle(self, ev: Event) -> bool:
        raise NotImplementedError

    def options(self) -> list[tuple[str, str]]:
        return [("Confirm", "<enter>"), ("Close/Cancel", "<esc>")]

    def cursor(self) -> Optional[tuple[int, int]]:
        return None

    def close(self) -> None:
        self.closed = True


def centered(canvas: Canvas, w: int, h: int) -> Rect:
    w = max(10, min(w, canvas.width - 2))
    h = max(3, min(h, canvas.height - 2))
    return Rect(max(0, (canvas.height - 1 - h) // 2), max(0, (canvas.width - w) // 2), h, w)


def fill_rect(canvas: Canvas, rect: Rect, style: Style = PLAIN) -> None:
    for r in range(rect.h):
        canvas.put(rect.y + r, rect.x, " " * rect.w, style)


@dataclass
class MenuItem:
    label: Any
    action: Optional[Callable[[], None]] = None
    key: Optional[str] = None
    description: str = ""
    disabled: Optional[str] = None
    section: Optional[str] = None
    value: Any = None
    checked: bool = False

    @property
    def label_line(self) -> Line:
        return [(self.label, PLAIN)] if isinstance(self.label, str) else list(self.label)

    @property
    def search_text(self) -> str:
        return plain(self.label_line) + " " + self.description


class MenuPopup(Popup):
    def __init__(
        self,
        title: str,
        items: Sequence[MenuItem],
        *,
        multi: bool = False,
        on_submit: Optional[Callable[[list[Any]], None]] = None,
        type_to_filter: bool = False,
        on_error: Optional[Callable[[str], None]] = None,
        width: Optional[int] = None,
        allow_custom: bool = False,
    ):
        self.title = title
        self.allow_custom = allow_custom
        self.items = list(items)
        self.multi = multi
        self.on_submit = on_submit
        self.type_to_filter = type_to_filter
        self.filtering = type_to_filter
        self.filter = ""
        self.sel = 0
        self.scroll = 0
        self.on_error = on_error
        self.fixed_width = width
        self.rect: Optional[Rect] = None
        self._move_to_enabled(1)

    def visible(self) -> list[MenuItem]:
        if not self.filter:
            return self.items
        f = self.filter.lower()
        return [it for it in self.items if f in it.search_text.lower()]

    def rows(self) -> list[tuple[str, Any]]:
        rows: list[tuple[str, Any]] = []
        last_section = None
        for it in self.visible():
            if it.section and it.section != last_section and not self.filter:
                rows.append(("section", it.section))
                last_section = it.section
            rows.append(("item", it))
        return rows

    def _item_rows(self) -> list[int]:
        return [i for i, (k, _) in enumerate(self.rows()) if k == "item"]

    def current(self) -> Optional[MenuItem]:
        rows = self.rows()
        if 0 <= self.sel < len(rows) and rows[self.sel][0] == "item":
            return rows[self.sel][1]
        return None

    def _move_to_enabled(self, direction: int) -> None:
        idx = self._item_rows()
        if not idx:
            self.sel = 0
            return
        if self.sel not in idx:
            after = [i for i in idx if i >= self.sel] if direction > 0 else [i for i in idx if i <= self.sel]
            self.sel = (after[0] if direction > 0 else after[-1]) if after else idx[0]

    def move(self, delta: int) -> None:
        idx = self._item_rows()
        if not idx:
            return
        pos = idx.index(self.sel) if self.sel in idx else 0
        pos = max(0, min(len(idx) - 1, pos + delta))
        self.sel = idx[pos]

    def _activate(self, item: MenuItem) -> None:
        if item.disabled:
            if self.on_error:
                self.on_error(item.disabled)
            return
        self.close()
        if item.action:
            item.action()

    def _submit_multi(self) -> None:
        values = [it.value for it in self.items if it.checked]
        if not values:
            cur = self.current()
            if cur is None:
                if not (self.allow_custom and self.filter.strip()):
                    return
                values = [v.strip() for v in self.filter.split(",") if v.strip()]
            else:
                values = [cur.value]
        self.close()
        if self.on_submit:
            self.on_submit(values)

    def handle(self, ev: Event) -> bool:
        if isinstance(ev, Paste):
            if self.filtering:
                self.filter += ev.text.replace("\n", " ")
                self.sel = 0
                self._move_to_enabled(1)
            return True
        if not isinstance(ev, Key):
            return True
        k = ev.name
        if k == "<esc>":
            if self.filter:
                self.filter = ""
                self.sel = 0
                self._move_to_enabled(1)
                if not self.type_to_filter:
                    self.filtering = False
            elif self.filtering and not self.type_to_filter:
                self.filtering = False
            else:
                self.close()
            return True
        if k == "<enter>":
            if self.filtering and not self.type_to_filter:
                self.filtering = False
                return True
            if self.multi:
                self._submit_multi()
            else:
                cur = self.current()
                if cur:
                    self._activate(cur)
            return True
        if k in ("<down>", "<c-n>") or (k == "j" and not self.filtering):
            self.move(1)
            return True
        if k in ("<up>", "<c-p>") or (k == "k" and not self.filtering):
            self.move(-1)
            return True
        if k in ("<pgdown>", "<c-d>"):
            self.move(10)
            return True
        if k in ("<pgup>", "<c-u>"):
            self.move(-10)
            return True
        if k in ("<home>",):
            self.move(-(10**6))
            return True
        if k in ("<end>",):
            self.move(10**6)
            return True
        if self.multi and (k == "<space>" or k == "<tab>"):
            cur = self.current()
            if cur and not cur.disabled:
                cur.checked = not cur.checked
                if k == "<tab>":
                    self.move(1)
            return True
        if self.filtering:
            if k == "<backspace>":
                self.filter = self.filter[:-1]
            elif k == "<c-w>":
                self.filter = self.filter.rstrip().rsplit(" ", 1)[0] if " " in self.filter.strip() else ""
            elif k == "<space>":
                self.filter += " "
            elif len(k) == 1:
                self.filter += k
            else:
                return True
            self.sel = 0
            self._move_to_enabled(1)
            return True
        if k == "/":
            self.filtering = True
            return True
        for it in self.visible():
            if it.key and it.key == k:
                self._activate(it)
                return True
        return True

    def options(self) -> list[tuple[str, str]]:
        if self.multi:
            return [("Toggle", "<space>"), ("Confirm", "<enter>"), ("Filter", "type"), ("Cancel", "<esc>")]
        if self.filtering:
            return [("Done filtering", "<enter>"), ("Clear", "<esc>")]
        return [("Execute", "<enter>"), ("Filter", "/"), ("Close", "<esc>")]

    def _row_line(self, kind: str, obj: Any, key_w: int, width: int) -> Line:
        if kind == "section":
            return [(f"--- {obj} ---", theme.BOLD)]
        it: MenuItem = obj
        parts: Line = []
        if self.multi:
            parts.append(("[x] " if it.checked else "[ ] ", theme.OK if it.checked else theme.DIM))
        if key_w:
            parts.append(((it.key or "").ljust(key_w) + " ", theme.KEY if not it.disabled else theme.DIM))
        label = it.label_line
        if it.disabled:
            label = restyle(label, Style(dim=True, strike=True))
        parts += label
        if it.description:
            used = line_width(parts)
            desc = truncate_str(it.description, max(0, width - used - 2))
            if desc:
                gap = max(2, width - used - text_width(desc))
                parts.append((" " * gap, PLAIN))
                parts.append((desc, theme.DIM))
        return parts

    def render(self, canvas: Canvas) -> None:
        rows = self.rows()
        key_w = max((text_width(it.key or "") for it in self.items), default=0)
        widest = max(
            (
                (4 if self.multi else 0)
                + (key_w + 1 if key_w else 0)
                + line_width(it.label_line)
                + (text_width(it.description) + 3 if it.description else 0)
                for it in self.items
            ),
            default=20,
        )
        w = self.fixed_width or max(40, min(widest + 4, int(canvas.width * 0.7)))
        extra = 1 if (self.filtering or self.filter) else 0
        h = min(len(rows) + 2 + extra, canvas.height - 4)
        if self.type_to_filter:
            h = min(max(h, 12), canvas.height - 4)
        rect = centered(canvas, w, max(h, 3 + extra))
        self.rect = rect
        inner = rect.inner
        fill_rect(canvas, rect)
        body_h = inner.h - extra
        if self.sel < self.scroll:
            self.scroll = self.sel
        if self.sel >= self.scroll + body_h:
            self.scroll = self.sel - body_h + 1
        self.scroll = max(0, min(self.scroll, max(0, len(rows) - body_h)))
        for r in range(body_h):
            i = self.scroll + r
            if i >= len(rows):
                break
            kind, obj = rows[i]
            ln = self._row_line(kind, obj, key_w, inner.w)
            if i == self.sel and kind == "item":
                canvas.put_line(inner.y + r, inner.x, restyle(truncate(ln, inner.w), SELECTED), inner.w, fill=SELECTED)
            else:
                canvas.put_line(inner.y + r, inner.x, truncate(ln, inner.w), inner.w)
        if not rows:
            canvas.put(inner.y, inner.x, "No matches", theme.DIM)
        if extra:
            canvas.put_line(inner.y + inner.h - 1, inner.x, [("Filter: ", theme.INFO), (self.filter, PLAIN)], inner.w)
        n_checked = sum(1 for it in self.items if it.checked)
        footer = f"{n_checked} selected" if self.multi else ""
        border = theme.SEARCH_BORDER if self.filtering and not self.type_to_filter else self.border
        draw_frame(
            canvas, rect, title=[(self.title, border)], border=border, footer=footer, scroll=(self.scroll, body_h, len(rows))
        )

    def cursor(self) -> Optional[tuple[int, int]]:
        if (self.filtering or self.filter) and self.rect:
            inner = self.rect.inner
            return inner.y + inner.h - 1, min(inner.x + 8 + text_width(self.filter), inner.x + inner.w - 1)
        return None


class ConfirmPopup(Popup):
    def __init__(self, title: str, message: str, on_yes: Callable[[], None], on_no: Optional[Callable[[], None]] = None):
        self.title = title
        self.message = message
        self.on_yes = on_yes
        self.on_no = on_no

    def handle(self, ev: Event) -> bool:
        if isinstance(ev, Key):
            if ev.name in ("<enter>", "y"):
                self.close()
                self.on_yes()
            elif ev.name in ("<esc>", "n", "q"):
                self.close()
                if self.on_no:
                    self.on_no()
        return True

    def render(self, canvas: Canvas) -> None:
        w = max(50, min(90, int(canvas.width * 0.5)))
        lines = wrap(self.message, w - 2)
        rect = centered(canvas, w, len(lines) + 2)
        fill_rect(canvas, rect)
        inner = rect.inner
        for i, ln in enumerate(lines[: inner.h]):
            canvas.put_line(inner.y + i, inner.x, [(ln, PLAIN)], inner.w)
        draw_frame(canvas, rect, title=[(self.title, self.border)], border=self.border)


class MessagePopup(Popup):
    def __init__(self, title: str, message: str, error: bool = False):
        self.title = title
        self.message = message
        self.error = error
        self.border = theme.ERROR_BORDER if error else theme.ACTIVE_BORDER
        self.scroll = 0
        self._h = 1

    def handle(self, ev: Event) -> bool:
        if isinstance(ev, Key):
            if ev.name in ("<enter>", "<esc>", "q"):
                self.close()
            elif ev.name in ("j", "<down>"):
                self.scroll += 1
            elif ev.name in ("k", "<up>"):
                self.scroll = max(0, self.scroll - 1)
            elif ev.name in ("<pgdown>", "<c-d>"):
                self.scroll += self._h
            elif ev.name in ("<pgup>", "<c-u>"):
                self.scroll = max(0, self.scroll - self._h)
        return True

    def options(self) -> list[tuple[str, str]]:
        return [("Close", "<esc>"), ("Scroll", "j/k")]

    def render(self, canvas: Canvas) -> None:
        w = max(50, min(110, int(canvas.width * 0.6)))
        lines = wrap(sanitize(self.message.replace("\t", "    ")), w - 2)
        rect = centered(canvas, w, len(lines) + 2)
        fill_rect(canvas, rect)
        inner = rect.inner
        self._h = max(1, inner.h)
        self.scroll = max(0, min(self.scroll, max(0, len(lines) - inner.h)))
        for i in range(inner.h):
            j = self.scroll + i
            if j >= len(lines):
                break
            canvas.put_line(inner.y + i, inner.x, [(lines[j], PLAIN)], inner.w)
        draw_frame(canvas, rect, title=[(self.title, self.border)], border=self.border, scroll=(self.scroll, inner.h, len(lines)))


class TextBuffer:
    """A small multi-line text editor model."""

    def __init__(self, text: str = ""):
        self.lines = text.split("\n") if text else [""]
        self.row = len(self.lines) - 1
        self.col = len(self.lines[-1])

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def set_text(self, text: str) -> None:
        self.lines = text.split("\n") if text else [""]
        self.row = len(self.lines) - 1
        self.col = len(self.lines[-1])

    def insert(self, s: str) -> None:
        parts = s.split("\n")
        cur = self.lines[self.row]
        before, after = cur[: self.col], cur[self.col :]
        if len(parts) == 1:
            self.lines[self.row] = before + s + after
            self.col += len(s)
            return
        new = [before + parts[0]] + parts[1:-1] + [parts[-1] + after]
        self.lines[self.row : self.row + 1] = new
        self.row += len(parts) - 1
        self.col = len(parts[-1])

    def backspace(self) -> None:
        if self.col > 0:
            cur = self.lines[self.row]
            self.lines[self.row] = cur[: self.col - 1] + cur[self.col :]
            self.col -= 1
        elif self.row > 0:
            prev = self.lines[self.row - 1]
            self.col = len(prev)
            self.lines[self.row - 1] = prev + self.lines[self.row]
            del self.lines[self.row]
            self.row -= 1

    def delete(self) -> None:
        cur = self.lines[self.row]
        if self.col < len(cur):
            self.lines[self.row] = cur[: self.col] + cur[self.col + 1 :]
        elif self.row < len(self.lines) - 1:
            self.lines[self.row] = cur + self.lines[self.row + 1]
            del self.lines[self.row + 1]

    def delete_word(self) -> None:
        cur = self.lines[self.row]
        left = cur[: self.col].rstrip()
        cut = left.rfind(" ") + 1 if " " in left else 0
        self.lines[self.row] = cur[:cut] + cur[self.col :]
        self.col = cut

    def kill_to_start(self) -> None:
        cur = self.lines[self.row]
        self.lines[self.row] = cur[self.col :]
        self.col = 0

    def kill_to_end(self) -> None:
        cur = self.lines[self.row]
        self.lines[self.row] = cur[: self.col]

    def left(self) -> None:
        if self.col > 0:
            self.col -= 1
        elif self.row > 0:
            self.row -= 1
            self.col = len(self.lines[self.row])

    def right(self) -> None:
        if self.col < len(self.lines[self.row]):
            self.col += 1
        elif self.row < len(self.lines) - 1:
            self.row += 1
            self.col = 0

    def up(self) -> None:
        if self.row > 0:
            self.row -= 1
            self.col = min(self.col, len(self.lines[self.row]))

    def down(self) -> None:
        if self.row < len(self.lines) - 1:
            self.row += 1
            self.col = min(self.col, len(self.lines[self.row]))

    def home(self) -> None:
        self.col = 0

    def end(self) -> None:
        self.col = len(self.lines[self.row])


class PromptPopup(Popup):
    def __init__(
        self,
        title: str,
        on_submit: Callable[[str], None],
        *,
        initial: str = "",
        multiline: bool = False,
        suggest: Optional[Callable[[str], list[Any]]] = None,
        token_sep: Optional[str] = None,
        hint: str = "",
        allow_empty: bool = False,
        on_editor: Optional[Callable[[str], None]] = None,
        width: Optional[int] = None,
    ):
        self.title = title
        self.on_submit = on_submit
        self.buf = TextBuffer(initial)
        self.multiline = multiline
        self.suggest = suggest
        self.token_sep = token_sep
        self.hint = hint
        self.allow_empty = allow_empty
        self.on_editor = on_editor
        self.fixed_width = width
        self.suggestions: list[Any] = []
        self.sug_sel = 0
        self.rect: Optional[Rect] = None
        self._cursor: Optional[tuple[int, int]] = None
        self._update_suggestions()

    @property
    def text(self) -> str:
        return self.buf.text

    def _token(self) -> str:
        t = self.buf.text
        if self.token_sep:
            t = t.rsplit(self.token_sep, 1)[-1]
        return t.strip()

    def _update_suggestions(self) -> None:
        if not self.suggest:
            self.suggestions = []
            return
        try:
            self.suggestions = self.suggest(self._token())[:8]
        except Exception:  # noqa: BLE001
            self.suggestions = []
        self.sug_sel = min(self.sug_sel, max(0, len(self.suggestions) - 1))

    def _complete(self) -> None:
        if not self.suggestions:
            return
        choice = self.suggestions[self.sug_sel]
        if isinstance(choice, tuple):
            choice = choice[0]
        t = self.buf.text
        if self.token_sep and self.token_sep in t:
            head = t.rsplit(self.token_sep, 1)[0]
            new = f"{head}{self.token_sep} {choice}"
        else:
            new = choice
        self.buf.set_text(new)
        self._update_suggestions()

    def submit(self) -> None:
        text = self.buf.text
        if not text.strip() and not self.allow_empty:
            return
        self.close()
        self.on_submit(text)

    def handle(self, ev: Event) -> bool:
        if isinstance(ev, Paste):
            text = ev.text if self.multiline else ev.text.replace("\n", " ")
            self.buf.insert(text)
            self._update_suggestions()
            return True
        if not isinstance(ev, Key):
            return True
        k = ev.name
        b = self.buf
        if k == "<esc>":
            self.close()
        elif k == "<enter>":
            self.submit()
        elif k in ("<a-enter>", "<c-j>"):
            if self.multiline:
                b.insert("\n")
            else:
                self.submit()
        elif k == "<c-o>" and self.on_editor:
            self.close()
            self.on_editor(b.text)
        elif k == "<backspace>":
            b.backspace()
        elif k == "<delete>" or k == "<c-d>":
            b.delete()
        elif k == "<c-w>":
            b.delete_word()
        elif k == "<c-u>":
            b.kill_to_start()
        elif k == "<c-k>":
            b.kill_to_end()
        elif k in ("<left>", "<c-b>"):
            b.left()
        elif k in ("<right>", "<c-f>"):
            b.right()
        elif k in ("<home>", "<c-a>"):
            b.home()
        elif k in ("<end>", "<c-e>"):
            b.end()
        elif k == "<tab>":
            self._complete()
        elif k == "<up>":
            if self.suggestions and not self.multiline:
                self.sug_sel = max(0, self.sug_sel - 1)
            else:
                b.up()
        elif k == "<down>":
            if self.suggestions and not self.multiline:
                self.sug_sel = min(len(self.suggestions) - 1, self.sug_sel + 1)
            else:
                b.down()
        elif k == "<space>":
            b.insert(" ")
        elif len(k) == 1:
            b.insert(k)
        else:
            return True
        self._update_suggestions()
        return True

    def options(self) -> list[tuple[str, str]]:
        opts = [("Confirm", "<enter>")]
        if self.multiline:
            opts.append(("New line", "<a-enter>/<c-j>"))
        if self.on_editor:
            opts.append(("Open in editor", "<c-o>"))
        if self.suggestions:
            opts.append(("Complete", "<tab>"))
        opts.append(("Cancel", "<esc>"))
        return opts

    def _visual_rows(self, width: int) -> tuple[list[str], tuple[int, int]]:
        """Soft-wrap the buffer into rows; return rows and the cursor's (row, col) in cells."""
        rows: list[str] = []
        cur = (0, 0)
        for li, text in enumerate(self.buf.lines):
            chunks: list[tuple[int, str]] = []
            start = 0
            buf = ""
            used = 0
            for ci, ch in enumerate(text):
                w = char_width(ch)
                if used + w > width:
                    chunks.append((start, buf))
                    start, buf, used = ci, "", 0
                buf += ch
                used += w
            chunks.append((start, buf))
            for idx, (st, chunk) in enumerate(chunks):
                last = idx == len(chunks) - 1
                if li == self.buf.row and st <= self.buf.col and (self.buf.col < st + len(chunk) or last):
                    cur = (len(rows), text_width(chunk[: self.buf.col - st]))
                rows.append(chunk)
        return rows, cur

    def render(self, canvas: Canvas) -> None:
        w = self.fixed_width or max(50, min(100, int(canvas.width * 0.55)))
        inner_w = max(10, w - 2)
        rows, (cr, cc) = self._visual_rows(inner_w - 1 if not self.multiline else inner_w)
        if self.multiline:
            body_h = max(4, min(12, len(rows)))
        else:
            body_h = 1
        hint_lines = wrap(self.hint, inner_w) if self.hint else []
        h = body_h + 2 + len(hint_lines)
        rect = centered(canvas, w, h)
        if self.suggestions and not self.multiline:
            rect = Rect(max(0, rect.y - len(self.suggestions) // 2 - 1), rect.x, rect.h, rect.w)
        self.rect = rect
        fill_rect(canvas, rect)
        inner = rect.inner
        top = max(0, cr - body_h + 1)
        if not self.multiline and len(rows) > 1:
            top = cr
        for i in range(body_h):
            j = top + i
            if j < len(rows):
                canvas.put(inner.y + i, inner.x, rows[j])
        for i, hl in enumerate(hint_lines):
            canvas.put(inner.y + body_h + i, inner.x, hl, theme.DIM)
        self._cursor = (inner.y + (cr - top), min(inner.x + cc, inner.x + inner.w - 1))
        draw_frame(canvas, rect, title=[(self.title, self.border)], border=self.border)
        if self.suggestions and not self.multiline:
            srect = Rect(rect.y + rect.h, rect.x, min(len(self.suggestions) + 2, canvas.height - 1 - rect.y - rect.h), rect.w)
            if srect.h >= 3:
                fill_rect(canvas, srect)
                si = srect.inner
                for i, s in enumerate(self.suggestions[: si.h]):
                    label = s[1] if isinstance(s, tuple) else s
                    style = SELECTED if i == self.sug_sel else PLAIN
                    canvas.put_line(
                        si.y + i, si.x, [(truncate_str(label, si.w), style)], si.w, fill=style if i == self.sug_sel else None
                    )
                draw_frame(canvas, srect, title=[("Suggestions", theme.INACTIVE_BORDER)], border=theme.INACTIVE_BORDER)

    def cursor(self) -> Optional[tuple[int, int]]:
        return self._cursor


class ReviewPopup(Popup):
    """Gerrit's reply dialog: votes, a cover message and the drafts that will be published."""

    def __init__(
        self,
        title: str,
        labels: Sequence[tuple[str, Sequence[int], int]],
        on_submit: Callable[[dict[str, int], str], None],
        *,
        summary: Sequence[Line] = (),
        chosen: Optional[dict[str, int]] = None,
        warning: str = "",
        message: str = "",
        focus_message: bool = False,
        on_editor: Optional[Callable[[dict[str, int], str], None]] = None,
    ):
        self.title = title
        self.warning = warning
        self.labels = [(name, list(values), current) for name, values, current in labels]
        # ``labels`` carry your current votes; ``chosen`` preselects others (e.g. when reopened after a failure).
        self.values = {name: (chosen or {}).get(name, current) for name, _, current in self.labels}
        self.on_submit = on_submit
        self.summary = list(summary)
        self.buf = TextBuffer(message)
        self.field = len(self.labels) if (focus_message or not self.labels) else 0
        self.on_editor = on_editor
        self._cursor: Optional[tuple[int, int]] = None

    @property
    def on_message(self) -> bool:
        return self.field >= len(self.labels)

    @property
    def message(self) -> str:
        return self.buf.text

    def _step(self, d: int) -> None:
        name, values, _ = self.labels[self.field]
        cur = self.values[name]
        i = values.index(cur) if cur in values else values.index(0)
        self.values[name] = values[max(0, min(len(values) - 1, i + d))]

    def _move_field(self, d: int) -> None:
        self.field = (self.field + d) % (len(self.labels) + 1)

    def submit(self) -> None:
        self.close()
        self.on_submit(dict(self.values), self.buf.text.strip("\n"))

    def handle(self, ev: Event) -> bool:
        if isinstance(ev, Paste):
            if self.on_message:
                self.buf.insert(ev.text)
            return True
        if not isinstance(ev, Key):
            return True
        k = ev.name
        b = self.buf
        if k == "<esc>":
            self.close()
        elif k == "<enter>":
            self.submit()
        elif k in ("<tab>",):
            self._move_field(1)
        elif k in ("<backtab>",):
            self._move_field(-1)
        elif k == "<c-o>" and self.on_editor:
            self.close()
            self.on_editor(dict(self.values), b.text)
        elif not self.on_message:
            if k == "<left>":
                self._step(-1)
            elif k == "<right>":
                self._step(1)
            elif k == "<down>":
                self._move_field(1)
            elif k == "<up>":
                self._move_field(-1)
            elif len(k) == 1 or k == "<space>":
                # Typing anywhere writes the message, so text can never change a vote by accident.
                self.field = len(self.labels)
                b.insert(" " if k == "<space>" else k)
        else:
            if k in ("<a-enter>", "<c-j>"):
                b.insert("\n")
            elif k == "<backspace>":
                b.backspace()
            elif k in ("<delete>", "<c-d>"):
                b.delete()
            elif k == "<c-w>":
                b.delete_word()
            elif k == "<c-u>":
                b.kill_to_start()
            elif k == "<c-k>":
                b.kill_to_end()
            elif k in ("<left>", "<c-b>"):
                b.left()
            elif k in ("<right>", "<c-f>"):
                b.right()
            elif k in ("<home>", "<c-a>"):
                b.home()
            elif k in ("<end>", "<c-e>"):
                b.end()
            elif k == "<up>":
                if b.row == 0 and self.labels:
                    self.field = len(self.labels) - 1
                else:
                    b.up()
            elif k == "<down>":
                b.down()
            elif k == "<space>":
                b.insert(" ")
            elif len(k) == 1:
                b.insert(k)
        return True

    def options(self) -> list[tuple[str, str]]:
        opts = [("Send", "<enter>"), ("Next field", "<tab>")]
        if self.on_message:
            opts.append(("New line", "<a-enter>"))
            if self.on_editor:
                opts.append(("Editor", "<c-o>"))
        else:
            opts.append(("Vote", "←/→"))
        opts.append(("Cancel (drafts are kept)", "<esc>"))
        return opts

    def render(self, canvas: Canvas) -> None:
        w = max(56, min(100, int(canvas.width * 0.6)))
        inner_w = w - 2
        rows: list[Line] = []
        if self.warning:
            rows += [[(part, theme.WARN)] for part in wrap(self.warning, inner_w - 1)]
            rows.append([])
        name_w = max((text_width(n) for n, _, _ in self.labels), default=0) + 2
        for i, (name, values, mine) in enumerate(self.labels):
            focused = i == self.field
            ln: Line = [("▶ " if focused else "  ", theme.ACTIVE_BORDER), (name.ljust(name_w), theme.BOLD if focused else PLAIN)]
            for v in values:
                text = f"{v:+d}" if v else " 0"
                chosen = self.values[name] == v
                if chosen:
                    style = SELECTED if focused else Style(reverse=True)
                else:
                    style = theme.DIM
                ln += [(" ", PLAIN), (f" {text} ", style)]
            if mine:
                ln.append((f"   your vote: {mine:+d}", theme.DIM))
            rows.append(ln)
        if self.labels:
            rows.append([])
        msg_focus = self.on_message
        rows.append(
            [
                ("▶ " if msg_focus else "  ", theme.ACTIVE_BORDER),
                ("Message", theme.BOLD if msg_focus else PLAIN),
                ("  (optional)", theme.DIM),
            ]
        )
        msg_top = len(rows)
        body_w = inner_w - 4
        vis: list[str] = []
        cur = (0, 0)
        for li, text in enumerate(self.buf.lines):
            chunks = [text[k : k + body_w] for k in range(0, max(1, len(text)), body_w)] or [""]
            for ci, chunk in enumerate(chunks):
                start = ci * body_w
                if (
                    li == self.buf.row
                    and start <= self.buf.col <= start + len(chunk)
                    and (self.buf.col < start + len(chunk) or ci == len(chunks) - 1)
                ):
                    cur = (len(vis), text_width(chunk[: self.buf.col - start]))
                vis.append(chunk)
        box_h = max(3, min(8, len(vis)))
        top = max(0, cur[0] - box_h + 1)
        for i in range(box_h):
            j = top + i
            text = vis[j] if j < len(vis) else ""
            rows.append([("  ", PLAIN), ("│ ", theme.ACTIVE_BORDER if msg_focus else theme.DIM), (text, PLAIN)])
        rows.append([])
        rows += [list(s) for s in self.summary]
        h = min(len(rows) + 2, canvas.height - 2)
        rect = centered(canvas, w, h)
        fill_rect(canvas, rect)
        inner = rect.inner
        for i, ln in enumerate(rows[: inner.h]):
            canvas.put_line(inner.y + i, inner.x, truncate(ln, inner.w), inner.w)
        self._cursor = None
        if msg_focus:
            y = inner.y + msg_top + (cur[0] - top)
            if y < inner.y + inner.h:
                self._cursor = (y, min(inner.x + 4 + cur[1], inner.x + inner.w - 1))
        draw_frame(canvas, rect, title=[(self.title, self.border)], border=self.border)

    def cursor(self) -> Optional[tuple[int, int]]:
        return self._cursor


def options_line(options: Sequence[tuple[str, str]]) -> Line:
    parts: Line = []
    for i, (desc, key) in enumerate(options):
        if i:
            parts.append((" | ", theme.OPTIONS))
        parts.append((f"{desc}: {key}", theme.OPTIONS))
    return parts


def pad_line(ln: Line, width: int) -> Line:
    return pad(ln, width)
