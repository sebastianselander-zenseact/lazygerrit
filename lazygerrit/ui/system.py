"""OS integration: clipboard, browser, editor."""

from __future__ import annotations

import base64
import os
import shlex
import shutil
import subprocess
import sys
from typing import Optional


def _spawn_with_input(cmd: list[str], text: str) -> bool:
    try:
        p = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
        assert p.stdin is not None
        p.stdin.write(text.encode("utf-8"))
        p.stdin.close()
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        return p.returncode in (0, None)
    except OSError:
        return False


def osc52(text: str) -> bool:
    data = base64.b64encode(text.encode("utf-8")).decode("ascii")
    seq = f"\x1b]52;c;{data}\x07"
    if os.environ.get("TMUX"):
        seq = "\x1bPtmux;" + seq.replace("\x1b", "\x1b\x1b") + "\x1b\\"
    try:
        with open("/dev/tty", "w") as tty:
            tty.write(seq)
            tty.flush()
        return True
    except OSError:
        return False


def copy_to_clipboard(text: str) -> str:
    """Copy text; returns the mechanism used. Raises RuntimeError when nothing works."""
    candidates: list[list[str]] = []
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        candidates.append(["wl-copy"])
    if os.environ.get("DISPLAY"):
        if shutil.which("xclip"):
            candidates.append(["xclip", "-selection", "clipboard"])
        if shutil.which("xsel"):
            candidates.append(["xsel", "--clipboard", "--input"])
    if sys.platform == "darwin" and shutil.which("pbcopy"):
        candidates.append(["pbcopy"])
    for cmd in candidates:
        if _spawn_with_input(cmd, text):
            return cmd[0]
    if osc52(text):
        return "OSC 52"
    raise RuntimeError("no clipboard tool found (install xclip, xsel or wl-clipboard)")


def open_url(url: str) -> None:
    browser = os.environ.get("BROWSER")
    if browser:
        cmd = shlex.split(browser) + [url]
    elif sys.platform == "darwin":
        cmd = ["open", url]
    else:
        cmd = ["xdg-open", url]
    try:
        subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
    except OSError as e:
        raise RuntimeError(f"could not open a browser: {e}") from e


LINE_AWARE_EDITORS = {"vi", "vim", "nvim", "nano", "emacs", "emacsclient", "kak", "micro", "hx", "helix", "gvim", "mvim"}


def editor_command(editor: str, path: str, line: Optional[int] = None) -> list[str]:
    parts = shlex.split(editor) or ["vi"]
    name = os.path.basename(parts[0])
    if line and name in LINE_AWARE_EDITORS:
        return parts + [f"+{line}", path]
    if line and name in ("code", "codium", "cursor"):
        return parts + ["--goto", f"{path}:{line}"]
    return parts + [path]
