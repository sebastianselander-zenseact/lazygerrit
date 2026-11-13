"""Run a command whose output goes to a pseudo-terminal of a given size.

Diff renderers such as delta and difftastic size and color their output for the terminal they
write to, so lazygerrit runs them in a terminal the size of the diff view, like lazygit does.
The command gets its own session, so it can never touch the terminal lazygerrit draws on.
Terminal queries (colors, device attributes, cursor position) are answered right away; without
an answer delta waits a second on every diff before giving up.
"""

from __future__ import annotations

import os
import re
import select
import signal
import subprocess
import threading
import time
from typing import Callable, Optional, Sequence

_QUERY_RE = re.compile(rb"\x1b\](1[01]);\?(\x07|\x1b\\)|\x1b\[(>?)0?c|\x1b\[6n")


class TtyProcess:
    """Handle used to stop the command together with everything it started."""

    def __init__(self, popen: subprocess.Popen):
        self.popen = popen

    def terminate(self, sig: int = signal.SIGTERM) -> None:
        try:
            os.killpg(self.popen.pid, sig)
        except OSError:
            pass


def _answer(query: re.Match, dark: bool) -> bytes:
    if query.group(1):
        fg, bg = ("d0d0/d0d0/d0d0", "0000/0000/0000") if dark else ("0000/0000/0000", "ffff/ffff/ffff")
        color = fg if query.group(1) == b"10" else bg
        return b"\x1b]" + query.group(1) + b";rgb:" + color.encode() + query.group(2)
    if query.group(0).endswith(b"c"):
        return b"\x1b[>1;10;0c" if query.group(3) else b"\x1b[?62;22c"
    return b"\x1b[1;1R"


def run_in_pty(
    args: Sequence[str],
    *,
    size: tuple[int, int],
    input: Optional[bytes] = None,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    timeout: float = 60,
    dark: bool = True,
    on_start: Callable[[TtyProcess], None] = lambda p: None,
    on_exit: Callable[[TtyProcess], None] = lambda p: None,
) -> tuple[int, bytes]:
    """Run ``args`` with stdout and stderr on a ``size`` (columns, rows) terminal; returns (status, output).

    Raises subprocess.TimeoutExpired (after killing the command) and FileNotFoundError.
    """
    try:
        import fcntl
        import pty
        import struct
        import termios

        master, slave = pty.openpty()
    except (ImportError, OSError):
        return _run_with_pipes(args, input=input, cwd=cwd, env=env, timeout=timeout)
    try:
        cols, rows = size
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0))
        attrs = termios.tcgetattr(slave)
        attrs[1] &= ~termios.OPOST  # keep "\n" as it is instead of "\r\n"
        attrs[3] &= ~(termios.ICANON | termios.ECHO)  # answers to queries arrive unbuffered and unechoed
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        popen = subprocess.Popen(
            list(args),
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except BaseException:
        os.close(master)
        os.close(slave)
        raise
    os.close(slave)
    proc = TtyProcess(popen)
    on_start(proc)
    if input is not None:
        threading.Thread(target=_feed, args=(popen, input), daemon=True).start()
    chunks: list[bytes] = []
    scanned = 0
    deadline = time.monotonic() + timeout
    quiet_since: Optional[float] = None
    try:
        while True:
            now = time.monotonic()
            if now > deadline:
                proc.terminate(signal.SIGKILL)
                popen.wait()
                raise subprocess.TimeoutExpired(list(args), timeout)
            ready, _, _ = select.select([master], [], [], min(0.2, deadline - now))
            if not ready:
                # The command is done but something it started still holds the terminal open.
                if popen.poll() is not None:
                    quiet_since = quiet_since or now
                    if now - quiet_since > 0.5:
                        break
                continue
            quiet_since = None
            try:
                data = os.read(master, 65536)
            except OSError:  # EIO: every writer has closed the terminal
                break
            if not data:
                break
            chunks.append(data)
            if scanned < 65536:
                buf = b"".join(chunks)
                for q in _QUERY_RE.finditer(buf, max(0, scanned - 16)):
                    if q.end() > scanned:
                        try:
                            os.write(master, _answer(q, dark))
                        except OSError:
                            pass
                scanned = len(buf)
        status = popen.wait(timeout=max(1.0, deadline - time.monotonic()))
    finally:
        os.close(master)
        if popen.poll() is None:
            proc.terminate(signal.SIGKILL)
            popen.wait()
        on_exit(proc)
    return status, b"".join(chunks)


def _feed(popen: subprocess.Popen, data: bytes) -> None:
    assert popen.stdin is not None
    try:
        popen.stdin.write(data)
        popen.stdin.close()
    except OSError:  # the command stopped reading
        pass


def _run_with_pipes(
    args: Sequence[str], *, input: Optional[bytes], cwd: Optional[str], env: Optional[dict], timeout: float
) -> tuple[int, bytes]:
    res = subprocess.run(
        list(args), input=input, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd, env=env, timeout=timeout
    )
    return res.returncode, res.stdout
