"""lazygit's default theme."""

from __future__ import annotations

from .text import BLUE, CYAN, GREEN, MAGENTA, RED, YELLOW, Style

ACTIVE_BORDER = Style(fg=GREEN, bold=True)
INACTIVE_BORDER = Style()
SEARCH_BORDER = Style(fg=CYAN, bold=True)
ERROR_BORDER = Style(fg=RED, bold=True)
OPTIONS = Style(fg=BLUE)
SELECTED_BG = BLUE
INACTIVE_SELECTED = Style(bold=True)
KEY = Style(fg=CYAN)
DIM = Style(dim=True)
BOLD = Style(bold=True)
TITLE_TAB_ACTIVE = Style(fg=GREEN, bold=True)
TITLE_TAB_SELECTED = Style(fg=GREEN)
HASH = Style(fg=YELLOW)
NUMBER = Style(fg=YELLOW)
OK = Style(fg=GREEN)
OK_BOLD = Style(fg=GREEN, bold=True)
BAD = Style(fg=RED)
BAD_BOLD = Style(fg=RED, bold=True)
WARN = Style(fg=YELLOW)
INFO = Style(fg=CYAN)
MAGIC = Style(fg=MAGENTA)
HEADING = Style(bold=True, underline=False)
LABEL = Style(fg=BLUE, bold=True)
LINK = Style(fg=CYAN, underline=True)
SPINNER_STYLE = Style(fg=CYAN)
TOAST_OK = Style(fg=GREEN)
TOAST_ERR = Style(fg=RED, bold=True)
DRY_RUN = Style(fg=RED, bold=True, reverse=True)

AUTHOR_COLORS = [CYAN, GREEN, YELLOW, BLUE, MAGENTA, RED, 208, 141, 39, 172, 77, 205]

BOX_ROUNDED = {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"}
SCROLL_THUMB = "▐"
SPINNER = ["|", "/", "-", "\\"]


def author_style(key: str) -> Style:
    h = 0
    for ch in key:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return Style(fg=AUTHOR_COLORS[h % len(AUTHOR_COLORS)])
