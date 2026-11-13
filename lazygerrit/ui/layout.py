"""Window layout, modelled on lazygit's: stacked side panels, main view + command log, bottom line."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SIDE_PANELS = ["status", "changes", "stack", "files", "comments"]
FIXED_HEIGHTS = {"status": 3}
WEIGHTS = {"changes": 2, "stack": 1, "files": 1, "comments": 1}
MIN_PANEL = 3
TIGHT = 6


@dataclass(frozen=True)
class Rect:
    y: int
    x: int
    h: int
    w: int

    def contains(self, y: int, x: int) -> bool:
        return self.y <= y < self.y + self.h and self.x <= x < self.x + self.w

    @property
    def inner(self) -> "Rect":
        return Rect(self.y + 1, self.x + 1, max(0, self.h - 2), max(0, self.w - 2))


def distribute(total: int, weights: list[int]) -> list[int]:
    if not weights:
        return []
    s = sum(weights) or 1
    raw = [total * w / s for w in weights]
    out = [int(r) for r in raw]
    rest = total - sum(out)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - out[i], reverse=True)
    for i in order[:rest]:
        out[i] += 1
    return out


def stack_heights(total: int, names: list[str], expand: Optional[str]) -> dict[str, int]:
    fixed = {n: FIXED_HEIGHTS[n] for n in names if n in FIXED_HEIGHTS}
    flex = [n for n in names if n not in FIXED_HEIGHTS]
    remaining = total - sum(fixed.values())
    heights = dict(fixed)
    if not flex:
        return heights
    natural = distribute(remaining, [WEIGHTS.get(n, 1) for n in flex])
    if min(natural) >= TIGHT or remaining < MIN_PANEL * len(flex):
        heights.update(zip(flex, natural, strict=True))
        return heights
    # Accordion mode: collapse unfocused panels to a single row of content.
    target = expand if expand in flex else flex[0]
    for n in flex:
        heights[n] = MIN_PANEL
    heights[target] = remaining - MIN_PANEL * (len(flex) - 1)
    return heights


def compute_layout(
    width: int,
    height: int,
    *,
    focused: str,
    expand: Optional[str] = None,
    screen_mode: int = 0,
    show_cmdlog: bool = True,
    cmdlog_size: int = 8,
    side_ratio: float = 0.3333,
) -> dict[str, Rect]:
    """Rectangles for every visible window. ``screen_mode``: 0 normal, 1 half, 2 full."""
    rects: dict[str, Rect] = {"bottom": Rect(height - 1, 0, 1, width)}
    avail = max(0, height - 1)
    if screen_mode == 2:
        rects[focused] = Rect(0, 0, avail, width)
        return rects

    portrait = width < 70 and avail >= 20
    if portrait:
        side_h = max(len(SIDE_PANELS) * MIN_PANEL, avail // 2)
        y = 0
        for name, h in stack_heights(side_h, SIDE_PANELS, expand).items():
            rects[name] = Rect(y, 0, h, width)
            y += h
        rects["main"] = Rect(y, 0, avail - y, width)
        return _ordered(rects)

    ratio = 0.5 if screen_mode == 1 else side_ratio
    side_w = int(width * ratio)
    side_w = max(min(24, width // 2), min(side_w, width - 20))
    y = 0
    heights = stack_heights(avail, SIDE_PANELS, expand)
    for name in SIDE_PANELS:
        h = heights[name]
        rects[name] = Rect(y, 0, h, side_w)
        y += h
    main_w = width - side_w
    if show_cmdlog and avail >= cmdlog_size + 10:
        rects["main"] = Rect(0, side_w, avail - cmdlog_size, main_w)
        rects["cmdlog"] = Rect(avail - cmdlog_size, side_w, cmdlog_size, main_w)
    else:
        rects["main"] = Rect(0, side_w, avail, main_w)
    return _ordered(rects)


def _ordered(rects: dict[str, Rect]) -> dict[str, Rect]:
    order = SIDE_PANELS + ["main", "cmdlog", "bottom"]
    return {k: rects[k] for k in order if k in rects}
