"""Transport layer: command runner (with command log + dry-run), Gerrit SSH and REST clients."""

from __future__ import annotations

import base64
import json
import netrc
import os
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .parsing import parse_query_output
from .ptyexec import run_in_pty


@dataclass
class CommandEvent:
    title: str
    display: str
    status: str  # "ok", "error", "dry-run", "info"
    output: str = ""
    duration: float = 0.0
    timestamp: float = field(default_factory=time.time)
    write: bool = False


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration: float = 0.0
    dry_run: bool = False


class CommandError(Exception):
    def __init__(self, display: str, returncode: int, stdout: str, stderr: str):
        self.display = display
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(self.message)

    @property
    def message(self) -> str:
        text = (self.stderr or "").strip() or (self.stdout or "").strip()
        if not text:
            text = f"command exited with status {self.returncode}"
        return text


class Runner:
    """Runs external commands and reports them to listeners (the command log).

    Write commands are skipped (but still logged) in dry-run mode.
    """

    def __init__(self, dry_run: bool = False, log_reads: bool = False):
        self.dry_run = dry_run
        self.log_reads = log_reads
        self._listeners: list[Callable[[CommandEvent], None]] = []
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()
        self._closed = False

    def subscribe(self, fn: Callable[[CommandEvent], None]) -> None:
        self._listeners.append(fn)

    def emit(self, ev: CommandEvent) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(ev)
            except Exception:  # noqa: BLE001 - listeners must never break commands
                pass

    def _exec(
        self,
        args: Sequence[str],
        input: Optional[bytes],
        cwd: Optional[str],
        env: Optional[dict],
        timeout: float,
    ) -> tuple[int, str, str]:
        if self._closed:
            return 130, "", "cancelled"
        p = subprocess.Popen(
            list(args),
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        with self._lock:
            self._procs.add(p)
        try:
            out, err = p.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            raise
        finally:
            with self._lock:
                self._procs.discard(p)
        return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def terminate_all(self) -> None:
        """Stop running commands (used on exit so a slow fetch does not keep the process alive)."""
        self._closed = True
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            try:
                p.terminate()
            except OSError:
                pass

    def run_tty(
        self,
        args: Sequence[str],
        *,
        size: tuple[int, int],
        display: Optional[str] = None,
        title: str = "",
        input: Optional[bytes] = None,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        timeout: float = 60,
        ok: Sequence[int] = (0,),
        dark: bool = True,
    ) -> CommandResult:
        """Run a read-only command with its output on a pseudo-terminal of ``size`` (columns, rows).

        stdout and stderr are combined. Never raises for a failing command (check ``returncode``);
        failures are reported to the command log.
        """
        shown = display or shlex.join(args)
        start = time.monotonic()

        def started(p: Any) -> None:
            with self._lock:
                self._procs.add(p)

        def ended(p: Any) -> None:
            with self._lock:
                self._procs.discard(p)

        if self._closed:
            rc, out = 130, "cancelled"
        else:
            try:
                rc, raw = run_in_pty(
                    args, size=size, input=input, cwd=cwd, env=env, timeout=timeout, dark=dark, on_start=started, on_exit=ended
                )
                out = raw.decode("utf-8", "replace")
            except subprocess.TimeoutExpired:
                rc, out = 124, f"timed out after {timeout:.0f}s"
            except FileNotFoundError as e:
                rc, out = 127, f"{e.filename}: command not found"
        duration = time.monotonic() - start
        failed = rc not in ok
        if failed or self.log_reads:
            output = out.strip()[-2000:] if failed else ""
            self.emit(CommandEvent(title or "Command", shown, "error" if failed else "ok", output, duration))
        return CommandResult(list(args), rc, out, "", duration)

    def run(
        self,
        args: Sequence[str],
        *,
        display: Optional[str] = None,
        title: str = "",
        input: Optional[bytes] = None,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        timeout: float = 120,
        write: bool = False,
        check: bool = True,
    ) -> CommandResult:
        shown = display or shlex.join(args)
        if write and self.dry_run:
            self.emit(CommandEvent(title or "Command", shown, "dry-run", write=True))
            return CommandResult(list(args), 0, "", "", 0.0, dry_run=True)
        start = time.monotonic()
        try:
            rc, out, err = self._exec(args, input, cwd, env, timeout)
        except subprocess.TimeoutExpired:
            rc, out, err = 124, "", f"timed out after {timeout:.0f}s"
        except FileNotFoundError as e:
            rc, out, err = 127, "", f"{e.filename}: command not found"
        duration = time.monotonic() - start
        if write or self.log_reads or (rc != 0 and check):
            status = "ok" if rc == 0 else "error"
            output = (err or out).strip() if rc != 0 else ""
            self.emit(CommandEvent(title or "Command", shown, status, output, duration, write=write))
        if check and rc != 0:
            raise CommandError(shown, rc, out, err)
        return CommandResult(list(args), rc, out, err, duration)


def quote_arg(arg: str) -> str:
    """Quote an argument for Gerrit's SSH command line splitter (POSIX-shell compatible)."""
    return shlex.quote(arg)


def runtime_dir() -> str:
    d = os.environ.get("XDG_RUNTIME_DIR")
    if d and os.path.isdir(d) and os.access(d, os.W_OK):
        return d
    return "/tmp"


class GerritSSH:
    def __init__(
        self,
        host: str,
        port: int,
        runner: Runner,
        user: Optional[str] = None,
        multiplex: bool = True,
        extra_options: Sequence[str] = (),
    ):
        self.host = host
        self.port = port
        self.user = user
        self.runner = runner
        self.extra_options = list(extra_options)
        # Per instance, so quitting one lazygerrit (ssh -O exit) never cuts off another one's sessions.
        self.control_path = os.path.join(runtime_dir(), f"lazygerrit-{os.getpid()}-%C") if multiplex else None

    def ssh_options(self) -> list[str]:
        opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30"]
        if self.control_path:
            opts += [
                "-o",
                "ControlMaster=auto",
                "-o",
                "ControlPersist=300",
                "-o",
                f"ControlPath={self.control_path}",
            ]
        return opts + self.extra_options

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def base_command(self) -> list[str]:
        return ["ssh", "-x", "-T", *self.ssh_options(), "-p", str(self.port), self.destination, "gerrit"]

    def git_ssh_command(self) -> str:
        """A GIT_SSH_COMMAND that reuses the same multiplexed connection."""
        return shlex.join(["ssh", *self.ssh_options()])

    def call(
        self,
        args: Sequence[str],
        *,
        input: Optional[bytes] = None,
        write: bool = False,
        title: str = "",
        timeout: float = 120,
    ) -> str:
        display = "gerrit " + shlex.join(list(args))
        if input is not None:
            payload = input.decode("utf-8", "replace")
            if len(payload) > 300:
                payload = payload[:300] + "…"
            display += f" <<< {payload}"
        cmd = self.base_command() + [quote_arg(a) for a in args]
        return self.runner.run(cmd, display=display, title=title, input=input, write=write, timeout=timeout).stdout

    def query(
        self,
        query: str,
        flags: Sequence[str] = (),
        limit: Optional[int] = None,
        start: Optional[int] = None,
        timeout: float = 120,
    ) -> tuple[list[dict], Optional[dict]]:
        args = ["query", "--format=JSON", *flags]
        if start:
            args += ["--start", str(start)]
        q = query if limit is None else f"{query} limit:{limit}"
        args.append(q)
        out = self.call(args, title="Query", timeout=timeout)
        return parse_query_output(out)

    def review(
        self,
        number: int,
        patch_set: int,
        *,
        message: Optional[str] = None,
        labels: Optional[dict[str, int]] = None,
        json_input: Any = None,
        rebase: bool = False,
        abandon: bool = False,
        restore: bool = False,
        submit: bool = False,
        notify: Optional[str] = None,
        title: str = "Review",
    ) -> str:
        args = ["review"]
        for label, value in (labels or {}).items():
            args += ["--label", f"{label}={format_vote(value)}"]
        args.append(f"{number},{patch_set}")
        if message is not None:
            args += ["-m", message]
        if rebase:
            args.append("--rebase")
        if abandon:
            args.append("--abandon")
        if restore:
            args.append("--restore")
        if submit:
            args.append("--submit")
        if notify:
            args += ["--notify", notify]
        data = None
        if json_input is not None:
            args.append("--json")
            data = json.dumps(json_input).encode("utf-8")
        return self.call(args, input=data, write=True, title=title)

    def set_topic(self, number: int, topic: Optional[str], title: str = "Set topic") -> str:
        args = ["set-topic", str(number)]
        if topic:
            args += ["--topic", topic]
        return self.call(args, write=True, title=title)

    def set_reviewers(
        self, number: int, add: Sequence[str] = (), remove: Sequence[str] = (), title: str = "Set reviewers"
    ) -> str:
        args = ["set-reviewers"]
        for r in add:
            args += ["-a", r]
        for r in remove:
            args += ["-r", r]
        args.append(str(number))
        return self.call(args, write=True, title=title)

    def version(self) -> str:
        return self.call(["version"], title="Version", timeout=30).strip()

    def close(self) -> None:
        if not self.control_path:
            return
        try:
            subprocess.run(
                ["ssh", "-o", f"ControlPath={self.control_path}", "-p", str(self.port), "-O", "exit", self.destination],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception:  # noqa: BLE001
            pass


def format_vote(value: int) -> str:
    return f"{value:+d}" if value else "0"


class RestError(Exception):
    pass


class GerritREST:
    """Authenticated Gerrit REST access (HTTP password from env or ~/.netrc). Optional."""

    def __init__(self, base_url: str, username: str, password: str, runner: Runner, timeout: float = 20):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.runner = runner
        self.timeout = timeout

    @staticmethod
    def find_credentials(host: str) -> Optional[tuple[str, str]]:
        user = os.environ.get("LAZYGERRIT_HTTP_USER") or os.environ.get("GERRIT_HTTP_USER")
        password = os.environ.get("LAZYGERRIT_HTTP_PASSWORD") or os.environ.get("GERRIT_HTTP_PASSWORD")
        if user and password:
            return user, password
        path = os.environ.get("NETRC") or str(Path.home() / ".netrc")
        try:
            auth = netrc.netrc(path).authenticators(host)
        except (FileNotFoundError, netrc.NetrcParseError, OSError):
            auth = None
        if auth and auth[0] and auth[2]:
            return auth[0], auth[2]
        return None

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        url = f"{self.base_url}/a{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json; charset=UTF-8")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace").strip()
            raise RestError(f"HTTP {e.code} for {method} {path}: {detail or e.reason}") from e
        except (urllib.error.URLError, OSError) as e:
            raise RestError(f"{method} {path} failed: {e}") from e
        if text.startswith(")]}'"):
            text = text.split("\n", 1)[1] if "\n" in text else ""
        return json.loads(text) if text.strip() else None

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def write(self, method: str, path: str, body: Any, title: str) -> Any:
        display = f"{method} /a{path} {json.dumps(body)}"
        if self.runner.dry_run:
            self.runner.emit(CommandEvent(title, display, "dry-run", write=True))
            return None
        start = time.monotonic()
        try:
            result = self._request(method, path, body)
        except RestError as e:
            self.runner.emit(CommandEvent(title, display, "error", str(e), time.monotonic() - start, write=True))
            raise
        self.runner.emit(CommandEvent(title, display, "ok", "", time.monotonic() - start, write=True))
        return result

    def comments(self, number: int) -> dict:
        return self.get(f"/changes/{number}/comments") or {}

    def set_hashtags(self, number: int, add: Sequence[str] = (), remove: Sequence[str] = ()) -> Any:
        body: dict[str, list[str]] = {}
        if add:
            body["add"] = list(add)
        if remove:
            body["remove"] = list(remove)
        return self.write("POST", f"/changes/{number}/hashtags", body, "Set hashtags")

    def check(self) -> str:
        acc = self.get("/accounts/self") or {}
        return acc.get("username") or acc.get("email") or "ok"
