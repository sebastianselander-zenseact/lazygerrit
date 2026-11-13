"""The lazygerrit application: state, background tasks, key bindings and actions."""

from __future__ import annotations

import curses
import json
import os
import queue
import shlex
import subprocess
import sys
import tempfile
import time
import traceback
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from .. import __version__
from .. import operations as ops
from ..backend import Backend, NeedsRest
from ..config import Config
from ..diff import LineRef, parse_unified
from ..drafts import Draft, StateStore
from ..gerrit import CommandError, CommandEvent, RestError
from ..models import COMMIT_MSG, Account, Change, CIJob, FileInfo, LocalCommit, Thread
from ..parsing import ci_messages, ci_state, filter_threads, latest_report, threads_from_change
from ..renderers import BUILTIN, DiffRenderer, detect_color_scheme, syntax_command
from ..srcrepo import CI_COMMANDS, discover_run_selected_jobs, known_hashtags, prio_high_hashtags
from ..zuul import ZuulError
from . import render, theme
from .layout import SIDE_PANELS, compute_layout
from .render import CIHeader
from .review import ReviewMixin
from .screen import Canvas, CursesCanvas, CursesInput, Event, Key, Mouse, Paste, Resize, VirtualCanvas
from .system import copy_to_clipboard, editor_command, open_url
from .text import PLAIN, Line, Style, line_width, text_block, text_width, truncate
from .widgets import (
    ConfirmPopup,
    ListPanel,
    MenuItem,
    MenuPopup,
    MessagePopup,
    Popup,
    PromptPopup,
    TextPanel,
    options_line,
)

SCISSORS = "# ------------------------ >8 ------------------------"
READ_ONLY_COMMANDS = {
    "query",
    "version",
    "ls-projects",
    "ls-groups",
    "ls-members",
    "ls-user-refs",
    "show-caches",
    "show-connections",
    "show-queue",
    "apropos",
    "check-project-access",
}


@dataclass
class Binding:
    key: str
    desc: str
    fn: Callable[[], None]
    ctx: str
    show: bool = False
    alts: tuple[str, ...] = ()
    label: Optional[str] = None
    bar: Any = None
    bar_key: Optional[str] = None


class App(ReviewMixin):
    def __init__(self, backend: Backend, cfg: Config, *, demo: bool = False, state: Optional[StateStore] = None):
        self.be = backend
        self.cfg = cfg
        self.demo = demo
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="lazygerrit")
        self.inflight: dict[str, Future] = {}
        self.busy: dict[str, str] = {}
        self.timers: list[tuple[float, Callable[[], None]]] = []
        backend.runner.subscribe(lambda ev: self.events.put(("cmd", ev)))
        self.canvas: Canvas = VirtualCanvas()
        self.input: Optional[CursesInput] = None
        self.quitting = False
        self.dirty = True
        self.suspend_hook: Callable[[Callable[[], Any]], Any] = lambda fn: fn()

        self.p_status = TextPanel("status", "Status", "1")
        self.p_changes = ListPanel("changes", "Changes", "2", tabs=[t.name for t in cfg.tabs])
        self.p_stack = ListPanel("stack", "Local stack", "3")
        self.p_files = ListPanel("files", "Files", "4", tabs=["Files", "CI"])
        self.p_comments = ListPanel("comments", "Comments", "5", tabs=["Comments", "Messages"])
        self.p_main = TextPanel("main", "Change")
        self.p_cmdlog = TextPanel("cmdlog", "Command log")
        self.panels: dict[str, Any] = {
            "status": self.p_status,
            "changes": self.p_changes,
            "stack": self.p_stack,
            "files": self.p_files,
            "comments": self.p_comments,
            "main": self.p_main,
            "cmdlog": self.p_cmdlog,
        }
        self.focus = "changes"
        self.last_side = "changes"
        self.change_source = "changes"
        self.screen_mode = 0
        self.show_cmdlog = cfg.show_command_log
        self.popups: list[Popup] = []
        self.input_mode: Optional[str] = None
        self.input_text = ""
        self.toast: Optional[tuple[str, Style, float]] = None
        self.spin = 0

        self.tab_data: dict[int, list[Change]] = {}
        self.tab_unresolved: dict[int, set[int]] = {}
        self.tab_loaded: dict[int, float] = {}
        self.tab_errors: dict[int, str] = {}
        self.custom_query = cfg.custom_query.format(project=cfg.project, branch=cfg.branch)
        self.custom_query_set = False
        self.details: dict[int, Change] = {}
        self.details_at: dict[int, float] = {}
        self.files_cache: dict[int, tuple[int, list[FileInfo]]] = {}
        self.threads_cache: dict[int, tuple[list[Thread], bool]] = {}
        self.threads_at: dict[int, float] = {}
        self.zuul_cache: dict[tuple[int, int], tuple[float, list[CIJob]]] = {}
        self.chains: dict[int, tuple[float, list[Change]]] = {}
        self.stack: list[LocalCommit] = []
        self.stack_base: Optional[str] = None
        self.stack_error = ""
        self.stack_loaded = False
        self.local_branch = ""
        self.server_version = ""
        self.connected: Optional[bool] = None
        self.conn_error = ""
        self.rest_user: Optional[str] = None
        self.rest_error = ""
        self.ctx: Optional[int] = None
        self.ctx_pending: Optional[tuple[int, float]] = None
        self.show_resolved = False
        self.show_ci_comments = False
        self.show_bot_messages = True
        self.ci_filter = 0
        self.diff_rows: dict[tuple, list] = {}
        self.state = state if state is not None else StateStore(None)
        self.draft_counts: dict[int, int] = self.state.draft_counts()
        self.view_ps: dict[int, int] = {}
        self.base_ps: dict[int, int] = {}
        self.full_file = False
        self.local_files: dict[tuple[int, int, int], list[FileInfo]] = {}
        self.pending_cursor: Optional[tuple[Any, Callable[[list[Any]], Optional[int]]]] = None
        self._reviewed_now: set[str] = set()
        self.rendered_diff: Any = None
        self.publishing: set[int] = set()
        self.seen_ps: dict[int, int] = {}
        self.own_files: dict[tuple[int, int], set[str]] = {}
        self.pending_goto: Optional[tuple[int, int, str, Callable[[list[Any]], Optional[int]]]] = None
        self.last_input = time.monotonic()
        self.context_cache: dict[tuple, list[tuple[int, str]]] = {}
        self.diff_context = cfg.diff_context
        self.main_key: Any = None
        self.main_diff: Optional[tuple[int, int, str]] = None
        self.main_thread: Optional[Thread] = None
        self.known_accounts: dict[str, Account] = {}
        self.seen_hashtags: set[str] = set(known_hashtags(cfg.repo))
        self.seen_topics: set[str] = set()
        self.run_selected_jobs: Optional[list[str]] = None
        self.cmdlog_entries: list[CommandEvent] = [
            CommandEvent("You can hide/focus this panel by pressing '@'", "", "info"),
        ]
        if cfg.dry_run:
            self.cmdlog_entries.append(CommandEvent("Dry run: write commands are logged but not sent", "", "info"))
        self.last_auto_refresh = time.monotonic()
        self.last_zuul_refresh = time.monotonic()
        # Diff renderers (delta, difftastic, …); the review view is always one of them.
        self.renderers: list[DiffRenderer] = list(cfg.diff_renderers)
        if not any(r.builtin for r in self.renderers):
            self.renderers.append(BUILTIN)
        self.renderer_idx = 0
        self.review_override = False  # showing the review view (to comment) instead of the chosen renderer
        self.rendered_out: OrderedDict[tuple, Any] = OrderedDict()
        self.pending_map: Optional[tuple[Any, tuple[str, float]]] = None
        self.color_scheme = detect_color_scheme(cfg.color_scheme)
        self.syntax_command = syntax_command(cfg.syntax_highlight)
        self.diff_styles: dict[tuple, dict[int, Any]] = {}
        self.syntax_warned = False
        self.bindings: list[Binding] = []
        self._build_bindings()
        self._setup_panels()
        self._update_cmdlog()

    # ------------------------------------------------------------------ setup

    def _setup_panels(self) -> None:
        ch = self.p_changes
        ch.row_fn = lambda c, w: render.change_row(
            c,
            w,
            show_owner=self.show_owner_for_tab(),
            hide_wip=self.cfg.tabs[self.p_changes.tab].name == "WIP",
            drafts=self.draft_counts.get(c.number, 0),
        )
        ch.key_fn = lambda c: c.number
        ch.filter_fn = (
            lambda c: f"{c.number} {c.subject} {c.owner.display} {c.owner.username} {c.topic or ''} {' '.join(c.hashtags)}"
        )
        ch.empty_text = "Loading…"
        st = self.p_stack
        st.row_fn = render.stack_row
        st.key_fn = lambda c: c.change_id or c.sha
        st.filter_fn = lambda c: f"{c.short} {c.subject} {c.change.number if c.change else ''}"
        st.empty_text = "Loading…"
        fp = self.p_files
        fp.row_fn = self._files_row
        fp.key_fn = self._files_key
        fp.filter_fn = lambda it: it.path if isinstance(it, FileInfo) else (it.title if isinstance(it, CIHeader) else it.name)
        fp.empty_text = "Select a change"
        cp = self.p_comments
        cp.row_fn = self._comments_row
        cp.key_fn = self._comments_key
        cp.filter_fn = lambda it: (
            f"{it.location} {' '.join(c.message for c in it.comments)} {' '.join(c.author.display for c in it.comments)}"
            if isinstance(it, Thread)
            else (f"draft {it.location} {it.message}" if isinstance(it, Draft) else f"{it.author.display} {it.message}")
        )
        cp.empty_text = "Select a change"

    def _files_row(self, it: Any, w: int) -> Line:
        if isinstance(it, FileInfo):
            counts = self.file_comment_counts().get(it.path, (0, 0, 0))
            return render.file_row(it, w, counts[0], counts[1], counts[2], it.path in self._reviewed_now)
        return render.ci_row(it, w)

    @staticmethod
    def _files_key(it: Any) -> Any:
        if isinstance(it, FileInfo):
            return ("f", it.path)
        if isinstance(it, CIHeader):
            return ("h", it.title)
        return ("j", it.pipeline, it.name)

    def _comments_row(self, it: Any, w: int) -> Line:
        if isinstance(it, Thread):
            return render.thread_row(it, w)
        if isinstance(it, Draft):
            return render.draft_row(it, w)
        return render.message_row(it, w, self.cfg.bot_usernames)

    @staticmethod
    def _comments_key(it: Any) -> Any:
        if isinstance(it, Thread):
            return ("t", it.path, it.line, it.patch_set, it.root.id or it.root.message[:40])
        if isinstance(it, Draft):
            return ("d", it.id)
        return ("m", it.timestamp, it.author.username, it.message[:40])

    # ------------------------------------------------------------------ lifecycle

    def run(self) -> None:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise SystemExit("lazygerrit needs an interactive terminal")
        os.environ.setdefault("ESCDELAY", "25")
        curses.wrapper(self._curses_main)

    def _curses_main(self, stdscr: "curses.window") -> None:
        curses.raw()
        curses.nonl()
        stdscr.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
            except curses.error:
                pass
        if self.cfg.mouse:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(0)
        self._term_write("\x1b[?2004h")
        self.canvas = CursesCanvas(stdscr)
        self.input = CursesInput(stdscr)
        self.suspend_hook = self._make_suspend(stdscr)
        try:
            self.start()
            self.loop()
        finally:
            self._term_write("\x1b[?2004l")
            self.shutdown()

    @staticmethod
    def _term_write(seq: str) -> None:
        try:
            sys.stdout.write(seq)
            sys.stdout.flush()
        except OSError:
            pass

    def _make_suspend(self, stdscr: "curses.window") -> Callable[[Callable[[], Any]], Any]:
        def suspend(fn: Callable[[], Any]) -> Any:
            curses.def_prog_mode()
            curses.endwin()
            self._term_write("\x1b[?2004l")
            try:
                return fn()
            finally:
                self._term_write("\x1b[?2004h")
                curses.reset_prog_mode()
                stdscr.clear()
                stdscr.refresh()
                if isinstance(self.canvas, CursesCanvas):
                    self.canvas.resize()
                self.dirty = True

        return suspend

    def start(self) -> None:
        self.load_status()
        self.load_tab(self.p_changes.tab)
        self.load_stack()
        self.update_main()
        if self.cfg.problems:
            self.error("\n".join(self.cfg.problems), "Problems in config.toml")
        if self.state.load_error:
            self.show_toast(self.state.load_error, error=True, seconds=8)
        elif self.draft_counts:
            total = sum(self.draft_counts.values())
            where = ", ".join(str(n) for n in sorted(self.draft_counts))
            self.show_toast(f"You have {total} unpublished draft(s) on {where} — publish with p", seconds=8)

    def loop(self) -> None:
        assert self.input is not None
        while not self.quitting:
            self.drain()
            self.tick()
            if self.dirty:
                self.render()
                self.dirty = False
            timeout = 90 if (self.busy or self.toast or self.timers) else 500
            ev = self.input.read(timeout)
            if ev is not None:
                try:
                    self.handle(ev)
                except Exception as e:  # noqa: BLE001
                    self.push(MessagePopup("Internal error", "".join(traceback.format_exception(e)), error=True))
                self.dirty = True
            elif self.busy:
                self.spin += 1
                self.dirty = True

    def shutdown(self) -> None:
        self.quitting = True
        self.be.runner.terminate_all()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def quit(self) -> None:
        self.quitting = True

    # ------------------------------------------------------------------ tasks

    def submit(
        self,
        key: str,
        fn: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_error: Optional[Callable[[BaseException], None]] = None,
        label: Optional[str] = None,
    ) -> bool:
        if key in self.inflight:
            return False
        fut = self.pool.submit(fn)
        self.inflight[key] = fut
        if label:
            self.busy[key] = label
        fut.add_done_callback(lambda f: self.events.put(("task", key, f, on_done, on_error)))
        self.dirty = True
        return True

    def later(self, delay: float, fn: Callable[[], None]) -> None:
        self.timers.append((time.monotonic() + delay, fn))

    def drain(self) -> None:
        while True:
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                break
            self.dirty = True
            if item[0] == "cmd":
                self._on_command(item[1])
                continue
            _, key, fut, on_done, on_error = item
            self.inflight.pop(key, None)
            self.busy.pop(key, None)
            if fut.cancelled():
                continue
            exc = fut.exception()
            try:
                if exc is not None:
                    if on_error:
                        on_error(exc)
                    else:
                        self.background_error(key, exc)
                else:
                    on_done(fut.result())
            except Exception as e:  # noqa: BLE001
                self.push(MessagePopup("Internal error", "".join(traceback.format_exception(e)), error=True))

    def wait_idle(self, timeout: float = 15.0) -> None:
        """Process tasks until none are running (used by tests and the demo recorder)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain()
            self.tick()
            if not self.inflight and self.events.empty() and not self.ctx_pending:
                break
            time.sleep(0.01)
        self.drain()
        self.render()

    def tick(self) -> None:
        now = time.monotonic()
        if self.ctx_pending and now >= self.ctx_pending[1]:
            n = self.ctx_pending[0]
            self.ctx_pending = None
            if n == self.ctx:
                self.ensure_details(n)
                self.ensure_files(n)
                ch = self.ctx_change()
                if ch and ch.open:
                    self.ensure_zuul(n, ch.ps)
        if self.timers:
            due = [t for t in self.timers if t[0] <= now]
            self.timers = [t for t in self.timers if t[0] > now]
            for _, fn in due:
                fn()
        idle = bool(self.cfg.idle_pause) and now - self.last_input > self.cfg.idle_pause
        if self.cfg.refresh_interval and now - self.last_auto_refresh > self.cfg.refresh_interval and not idle:
            self.last_auto_refresh = now
            self.reload_drafts()
            self.load_tab(self.p_changes.tab, quiet=True)
            self.load_stack(quiet=True)
            if self.ctx is not None:
                self.ensure_details(self.ctx, force=True, quiet=True)
        if self.cfg.zuul_refresh_interval and now - self.last_zuul_refresh > self.cfg.zuul_refresh_interval and not idle:
            self.last_zuul_refresh = now
            ch = self.ctx_change()
            if ch is not None and ch.open and ch.detailed:
                cached = self.zuul_cache.get((ch.number, ch.ps))
                running = cached is None or any(j.state in ("running", "queued") for j in cached[1])
                if running or ci_state(ch)[0] == "running":
                    self.ensure_zuul(ch.number, ch.ps, force=True)
        if self.toast and now > self.toast[2]:
            self.toast = None
            self.dirty = True

    # ------------------------------------------------------------------ loading

    def tab_query(self, idx: int) -> str:
        tab = self.cfg.tabs[idx]
        if tab.name == "Query" and not tab.query:
            return self.custom_query if self.custom_query_set else ""
        return self.cfg.tab_query(tab)

    def query_tab_index(self) -> int:
        for i, t in enumerate(self.cfg.tabs):
            if t.name == "Query":
                return i
        return len(self.cfg.tabs) - 1

    def load_tab(self, idx: int, quiet: bool = False) -> None:
        q = self.tab_query(idx)
        if not q:
            self.tab_data[idx] = []
            if idx == self.p_changes.tab:
                self.refresh_changes_panel()
            return
        tab = self.cfg.tabs[idx]
        limit = tab.limit or self.cfg.limit
        label = None if quiet else f"Loading {tab.name.lower()}"
        self.submit(
            f"tab:{idx}",
            lambda: self.be.list_changes(q, limit),
            lambda chs: self._tab_loaded(idx, q, chs),
            on_error=lambda e: self._tab_failed(idx, q, e),
            label=label,
        )
        self.submit(
            f"unres:{idx}",
            lambda: self.be.unresolved_numbers(q, limit),
            lambda nums: self._unresolved_loaded(idx, q, nums),
            on_error=lambda e: None,
        )

    def _tab_loaded(self, idx: int, q: str, changes: list[Change]) -> None:
        if q != self.tab_query(idx):
            return
        unres = self.tab_unresolved.get(idx)
        for c in changes:
            if unres is not None:
                c.has_unresolved = c.number in unres
            self._learn(c)
            self._drop_if_outdated(c)
        first_load = idx not in self.tab_loaded
        self.tab_data[idx] = changes
        self.tab_loaded[idx] = time.monotonic()
        self.tab_errors.pop(idx, None)
        self.connected = True
        if idx == self.p_changes.tab:
            self.refresh_changes_panel()
        if first_load and idx == self.p_changes.tab:
            for other in range(len(self.cfg.tabs)):
                if other != idx and other not in self.tab_loaded and self.tab_query(other):
                    self.load_tab(other, quiet=True)
        if self.focus == "status":
            self.update_main()

    def _drop_if_outdated(self, fresh: Change) -> None:
        """Forget cached details that are older than freshly listed data for the same change."""
        n = fresh.number
        d = self.details.get(n)
        if d is None or (d.ps == fresh.ps and fresh.last_updated <= d.last_updated):
            return
        self.details.pop(n, None)
        self.details_at.pop(n, None)
        self.chains.pop(n, None)
        self.threads_at.pop(n, None)
        cached = self.files_cache.get(n)
        if cached and cached[0] != fresh.ps:
            self.files_cache.pop(n, None)
        if n == self.ctx:
            self.ensure_details(n, force=True, quiet=True)
            self.ensure_files(n)

    def _tab_failed(self, idx: int, q: str, e: BaseException) -> None:
        self.tab_errors[idx] = self.error_text(e)
        self.tab_data.setdefault(idx, [])
        if isinstance(e, CommandError) and ("ssh" in e.display or "gerrit" in e.display) and idx not in self.tab_loaded:
            self.connected = self.connected if self.connected else False
            self.conn_error = self.conn_error or e.message
        if idx == self.p_changes.tab:
            self.refresh_changes_panel()
            self.show_toast(f"Loading {self.cfg.tabs[idx].name} failed: {self.error_text(e).splitlines()[0]}", error=True)

    def _unresolved_loaded(self, idx: int, q: str, nums: set[int]) -> None:
        if q != self.tab_query(idx):
            return
        self.tab_unresolved[idx] = nums
        for c in self.tab_data.get(idx, []):
            c.has_unresolved = c.number in nums
        for n, d in self.details.items():
            if any(c.number == n for c in self.tab_data.get(idx, [])):
                d.has_unresolved = n in nums

    def refresh_changes_panel(self) -> None:
        idx = self.p_changes.tab
        p = self.p_changes
        if idx in self.tab_errors:
            p.empty_text = "Error: " + self.tab_errors[idx]
        elif not self.tab_query(idx):
            p.empty_text = "Press s to enter a Gerrit search query (e.g. 'status:open project:src topic:foo')."
        elif idx not in self.tab_loaded:
            p.empty_text = "Loading…"
        else:
            p.empty_text = "No changes"
        p.set_items(self.tab_data.get(idx, []))
        if self.focus in ("changes",) or (self.focus == "main" and self.last_side == "changes"):
            self.on_selection()
        elif self.ctx is None and self.focus not in ("stack",) and p.current() is not None and self.change_source == "changes":
            self.ctx = p.current().number
            self.refresh_ctx_panels()
            self.ctx_pending = (self.ctx, time.monotonic())

    def load_stack(self, quiet: bool = False) -> None:
        git = self.be.git
        if not git.available:
            self.stack_error = "No local git repository (use --repo PATH)"
            self.p_stack.empty_text = self.stack_error
            self.stack_loaded = True
            return

        def work() -> tuple[str, Optional[str], list[LocalCommit]]:
            branch = git.current_branch()
            base, commits = git.stack(self.cfg.stack_limit)
            ids = [c.change_id for c in commits if c.change_id]
            mapping = self.be.by_change_ids(ids) if ids else {}
            for c in commits:
                c.change = mapping.get(c.change_id) if c.change_id else None
            return branch, base, commits

        self.submit(
            "stack", work, self._stack_loaded, on_error=self._stack_failed, label=None if quiet else "Loading local stack"
        )

    def _stack_loaded(self, result: tuple[str, Optional[str], list[LocalCommit]]) -> None:
        branch, base, commits = result
        self.local_branch = branch
        self.stack_base = base
        self.stack = commits
        self.stack_loaded = True
        self.stack_error = ""
        for c in commits:
            if c.change:
                self._learn(c.change)
                self._drop_if_outdated(c.change)
        p = self.p_stack
        p.title = f"Local stack ({base}..HEAD)" if base else "Local stack"
        p.empty_text = f"No local commits on top of {base}" if base else "Could not determine the base branch"
        p.set_items(commits)
        if self.focus == "stack" or (self.focus == "main" and self.last_side == "stack"):
            self.on_selection()

    def _stack_failed(self, e: BaseException) -> None:
        self.stack_error = self.error_text(e)
        self.p_stack.empty_text = "Error: " + self.stack_error
        self.p_stack.set_items([])
        self.stack_loaded = True

    def load_status(self) -> None:
        def version() -> str:
            return self.be.ssh.version()

        def ok(v: str) -> None:
            self.server_version = v.replace("gerrit version ", "")
            self.connected = True
            self.conn_error = ""
            if self.focus == "status":
                self.update_main()

        def fail(e: BaseException) -> None:
            self.connected = False
            self.conn_error = self.error_text(e)
            self.show_toast("Could not connect to Gerrit: " + self.conn_error.splitlines()[0], error=True)
            if self.focus == "status":
                self.update_main()

        self.submit("version", version, ok, fail)
        if self.be.git.available:
            self.submit("branch", self.be.git.current_branch, lambda b: setattr(self, "local_branch", b), lambda e: None)
        if self.be.rest is not None:

            def rest_ok(user: str) -> None:
                self.rest_user = user

            def rest_fail(e: BaseException) -> None:
                self.rest_error = str(e)

            self.submit("rest", self.be.rest.check, rest_ok, rest_fail)

    def ensure_details(self, n: int, force: bool = False, quiet: bool = False) -> None:
        if not force and n in self.details and time.monotonic() - self.details_at.get(n, 0) < 45:
            return
        self.submit(
            f"details:{n}",
            lambda: self.be.details(n),
            lambda ch: self._details_loaded(n, ch),
            on_error=lambda e: self.background_error(f"Loading {n}", e),
            label=None if quiet else f"Loading {n}",
        )

    def _details_loaded(self, n: int, ch: Change) -> None:
        old = self.details.get(n) or self.find_list_change(n)
        if old is not None:
            ch.merge_from(old)
        files = self.files_cache.get(n)
        if files and ch.current and files[0] == ch.current.number and ch.current.files is None:
            ch.current.files = files[1]
        elif files and ch.current and files[0] != ch.current.number:
            self.ensure_files(n, force=True)
        self.details[n] = ch
        self.details_at[n] = time.monotonic()
        self._learn(ch)
        if self.be.rest is None:
            self.threads_cache[n] = (threads_from_change(ch), False)
        else:
            self.ensure_threads(n, force=True)
        if ch.open:
            self.ensure_zuul(n, ch.ps)
        self.ensure_chain(n)
        if self.ctx == n:
            self.refresh_ctx_panels()
        self.update_main(data_changed=True)

    def ensure_files(self, n: int, force: bool = False) -> None:
        if not force and n in self.files_cache:
            return
        self.submit(
            f"files:{n}",
            lambda: self.be.files(n),
            lambda r: self._files_loaded(n, r),
            on_error=lambda e: self.background_error(f"Files of {n}", e),
        )

    def _files_loaded(self, n: int, result: tuple[int, list[FileInfo]]) -> None:
        self.files_cache[n] = result
        d = self.details.get(n)
        if d and d.current and d.current.number == result[0]:
            d.current.files = result[1]
        if self.ctx == n:
            self.refresh_ctx_panels()
            self.update_main(data_changed=True)

    def ensure_threads(self, n: int, force: bool = False) -> None:
        ch = self.details.get(n)
        if ch is None:
            return
        if not force and n in self.threads_cache and time.monotonic() - self.threads_at.get(n, 0) < 45:
            return
        self.submit(
            f"threads:{n}",
            lambda: self.be.threads(ch),
            lambda r: self._threads_loaded(n, r),
            on_error=lambda e: self.background_error(f"Comments of {n}", e),
        )

    def _threads_loaded(self, n: int, result: tuple[list[Thread], bool]) -> None:
        self.threads_cache[n] = result
        self.threads_at[n] = time.monotonic()
        threads, exact = result
        if exact:
            unresolved = any(t.unresolved and not t.is_ci for t in threads)
            for c in [self.details.get(n), self.find_list_change(n)]:
                if c is not None:
                    c.has_unresolved = unresolved
        if self.ctx == n:
            self.refresh_ctx_panels()
            self.update_main(data_changed=True)

    def ensure_zuul(self, n: int, ps: int, force: bool = False) -> None:
        if self.be.zuul is None or not ps:
            return
        key = (n, ps)
        cached = self.zuul_cache.get(key)
        if not force and cached and time.monotonic() - cached[0] < max(5, self.cfg.zuul_refresh_interval):
            return
        self.submit(f"zuul:{n},{ps}", lambda: self.be.zuul_live(n, ps), lambda jobs: self._zuul_loaded(key, jobs), lambda e: None)

    def _zuul_loaded(self, key: tuple[int, int], jobs: list[CIJob]) -> None:
        self.zuul_cache[key] = (time.monotonic(), jobs)
        if self.ctx == key[0]:
            self.refresh_ctx_panels()
            self.update_main(data_changed=True)

    def ensure_chain(self, n: int, force: bool = False) -> None:
        ch = self.details.get(n)
        if ch is None:
            return
        cached = self.chains.get(n)
        if not force and cached and time.monotonic() - cached[0] < 60:
            return
        if not ch.depends_on and not ch.needed_by:
            self.chains[n] = (time.monotonic(), [ch])
            return
        self.submit(f"chain:{n}", lambda: self.compute_chain(ch), lambda chain: self._chain_loaded(n, chain), lambda e: None)

    def compute_chain(self, ch: Change, depth: int = 12) -> list[Change]:
        seen = {ch.number}
        down: list[Change] = []
        cur = ch
        for _ in range(depth):
            if len(cur.depends_on) != 1:
                break
            num = cur.depends_on[0].number
            if num in seen:
                break
            dep = self.be.light(num)
            if dep is None:
                break
            seen.add(num)
            down.append(dep)
            if not dep.open:
                break
            cur = dep
        up: list[Change] = []
        cur = ch
        for _ in range(depth):
            if not cur.needed_by:
                break
            num = cur.needed_by[0].number
            if num in seen:
                break
            child = self.be.light(num)
            if child is None:
                break
            seen.add(num)
            up.append(child)
            cur = child
        return list(reversed(up)) + [ch] + down

    def _chain_loaded(self, n: int, chain: list[Change]) -> None:
        self.chains[n] = (time.monotonic(), chain)
        if self.ctx == n:
            self.update_main(data_changed=True)

    # ------------------------------------------------------------------ derived state

    def _learn(self, ch: Change) -> None:
        accounts = [ch.owner, *ch.reviewers]
        if ch.current:
            accounts += [a.by for a in ch.current.approvals]
        for a in accounts:
            if a.username and not a.is_bot(tuple(self.cfg.bot_usernames)):
                self.known_accounts[a.username] = a
        self.seen_hashtags.update(ch.hashtags)
        if ch.topic:
            self.seen_topics.add(ch.topic)

    def find_list_change(self, n: int) -> Optional[Change]:
        for chs in self.tab_data.values():
            for c in chs:
                if c.number == n:
                    return c
        for c in self.stack:
            if c.change and c.change.number == n:
                return c.change
        return None

    def change(self, n: int) -> Optional[Change]:
        return self.details.get(n) or self.find_list_change(n)

    def ctx_change(self) -> Optional[Change]:
        return self.change(self.ctx) if self.ctx is not None else None

    def show_owner_for_tab(self) -> bool:
        q = self.tab_query(self.p_changes.tab)
        return not ("owner:self" in q and "-owner:self" not in q)

    def file_comment_counts(self) -> dict[str, tuple[int, int, int]]:
        """path -> (threads, unresolved threads, drafts) on the patch set being viewed."""
        ch = self.ctx_change()
        if ch is None:
            return {}
        view = self.viewed_ps(ch)
        psn = view.number if view else ch.ps
        counts: dict[str, tuple[int, int, int]] = {}
        for t in self.threads_cache.get(ch.number, ([], False))[0]:
            if t.patch_set != psn or t.is_ci:
                continue
            n, u, d = counts.get(t.path, (0, 0, 0))
            counts[t.path] = (n + 1, u + (1 if t.unresolved else 0), d)
        for dr in self.state.drafts_for(ch.number):
            if dr.patch_set != psn:
                continue
            n, u, d = counts.get(dr.path, (0, 0, 0))
            counts[dr.path] = (n, u, d + 1)
        return counts

    def current_files(self, ch: Change) -> Optional[list[FileInfo]]:
        if ch.current and ch.current.files is not None:
            return ch.current.files
        cached = self.files_cache.get(ch.number)
        if cached and (not ch.current or cached[0] == ch.current.number):
            return cached[1]
        return None

    def visible_threads(self, n: int) -> list[Thread]:
        threads = self.threads_cache.get(n, ([], False))[0]
        return filter_threads(threads, self.show_resolved, self.show_ci_comments)

    def ci_items(self, ch: Change) -> list[Any]:
        items: list[Any] = []
        cached = self.zuul_cache.get((ch.number, ch.ps))
        live = cached[1] if cached else []
        if live:
            by_pipe: dict[str, list[CIJob]] = {}
            for j in live:
                by_pipe.setdefault(j.pipeline or "zuul", []).append(j)
            for pipe, jobs in by_pipe.items():
                shown = self._filter_jobs(jobs)
                state = (
                    "failure"
                    if any(j.state == "failure" and j.voting for j in jobs)
                    else ("running" if any(j.state in ("running", "queued") for j in jobs) else "success")
                )
                url = self.be.zuul.status_page(ch.number, ch.ps) if self.be.zuul else ""
                items.append(CIHeader(f"{pipe} · live · ps{ch.ps}", state, url, live=True, jobs=jobs))
                items += shown
        if ch.detailed:
            reports, _ = ci_messages(ch)
            for rep in list(reversed(reports))[:4]:
                state = "success" if rep.succeeded else "failure"
                title = f"{rep.pipeline or 'zuul'} · ps{rep.patch_set} · {'passed' if rep.succeeded else 'failed'}"
                items.append(CIHeader(title, state, rep.buildset_url, report=rep, timestamp=rep.timestamp))
                items += self._filter_jobs(rep.jobs)
        return items

    def _filter_jobs(self, jobs: Sequence[CIJob]) -> list[CIJob]:
        if self.ci_filter == 2:
            return list(jobs)
        if self.ci_filter == 1:
            return [j for j in jobs if j.state in ("failure", "running", "queued")]
        return [j for j in jobs if j.state != "skipped"]

    def refresh_ctx_panels(self) -> None:
        ch = self.ctx_change()
        fp, cp = self.p_files, self.p_comments
        fp.suffix = f" · {self.view_label(ch)}" if ch else ""
        cp.suffix = f" · {ch.number},{ch.ps}" if ch else ""
        self._reviewed_now = set()
        if ch is None:
            msg = "Select a change"
            if self.change_source == "stack":
                cur = self.p_stack.current()
                if cur is not None and cur.change is None:
                    msg = "This commit is not on Gerrit yet (push it with P in the stack panel)"
            fp.empty_text = cp.empty_text = msg
            fp.set_items([])
            cp.set_items([])
            return
        if fp.tab == 0:
            files = self.files_for_view(ch)
            view = self.viewed_ps(ch)
            self._reviewed_now = self.state.reviewed_paths(ch.number, view.number) if view else set()
            fp.empty_text = "Loading files…" if files is None else "No files"
            fp.set_items(files or [])
        else:
            items = self.ci_items(ch)
            if items:
                fp.empty_text = ""
            elif not ch.detailed:
                fp.empty_text = "Loading CI results…"
            else:
                fp.empty_text = "No CI results for this change"
            fp.set_items(items)
        if cp.tab == 0:
            drafts: list[Any] = list(self.state.drafts_for(ch.number))
            if ch.number not in self.threads_cache:
                cp.empty_text = "Loading comments…"
                cp.set_items(drafts)
            else:
                threads = self.visible_threads(ch.number)
                total = len(self.threads_cache[ch.number][0])
                cp.empty_text = (
                    "No comments"
                    if total == 0
                    else (
                        f"No unresolved comments ({total} hidden) — press f to show all"
                        if not self.show_resolved
                        else "No comments"
                    )
                )
                cp.set_items(drafts + threads)
        else:
            msgs = list(reversed(ch.messages)) if ch.detailed else []
            if not self.show_bot_messages:
                msgs = [m for m in msgs if not m.author.is_bot(tuple(self.cfg.bot_usernames))]
            cp.empty_text = "Loading messages…" if not ch.detailed else "No messages"
            cp.set_items(msgs)
        self.resume_goto()

    def selected_change_number(self) -> Optional[int]:
        src = self.change_source
        if src == "stack":
            c = self.p_stack.current()
            return c.change.number if c is not None and c.change is not None else None
        c = self.p_changes.current()
        return c.number if c is not None else None

    def on_selection(self) -> None:
        f = self.last_side if self.focus == "main" else self.focus
        if f in ("changes", "stack"):
            self.change_source = f
            n = self.selected_change_number()
            if n != self.ctx:
                self.ctx = n
                self.refresh_ctx_panels()
                if n is not None:
                    self.ctx_pending = (n, time.monotonic() + 0.12)
            elif n is not None and n not in self.details and not self.ctx_pending:
                self.ctx_pending = (n, time.monotonic() + 0.12)
        self.update_main()

    # ------------------------------------------------------------------ main view

    @property
    def main_width(self) -> int:
        return self.p_main.width if self.p_main.rect else max(40, self.canvas.width * 2 // 3 - 2)

    def set_main(
        self,
        key: Any,
        title: str,
        lines: list[Line],
        *,
        wrap: bool = True,
        meta: Optional[list[Any]] = None,
        cursor_mode: bool = False,
    ) -> None:
        same = key == self.main_key
        self.p_main.set_lines(lines, title=title, wrap=wrap, meta=meta, keep_scroll=same, cursor_mode=cursor_mode)
        self.main_key = key
        if not same:
            self.p_main.search = ""
            self.rendered_diff = None

    def update_main(self, data_changed: bool = False) -> None:
        if self.focus != "main":
            self.review_override = False
        if self.focus == "main" and not data_changed:
            return
        f = self.last_side if self.focus == "main" else self.focus
        if self.focus == "main":
            key = self.main_key
            if not key:
                return
            kind = key[0]
            if kind == "change":
                self.show_change(key[1])
            elif kind == "commit":
                cur = self.p_stack.current()
                if cur is not None and cur.sha == key[1]:
                    self.show_commit(cur)
            elif kind == "diff" and self.main_diff:
                self._render_diff(key)
            elif kind == "draft":
                d = self.state.draft(key[1])
                if d is not None:
                    self.show_draft(d)
            return
        if f == "status":
            self.set_main(("status",), "Status", self.status_lines())
        elif f == "changes":
            c = self.p_changes.current()
            if c is None:
                self.set_main(("empty", "changes"), "Change", text_block(self.p_changes.empty_text, theme.DIM))
            else:
                self.show_change(c.number)
        elif f == "stack":
            c = self.p_stack.current()
            if c is None:
                self.set_main(("empty", "stack"), "Commit", text_block(self.p_stack.empty_text, theme.DIM))
            else:
                self.show_commit(c)
        elif f == "files":
            item = self.p_files.current()
            if item is None:
                self.set_main(("empty", "files"), "Diff", text_block(self.p_files.empty_text, theme.DIM))
            elif isinstance(item, FileInfo):
                self.show_diff(item)
            else:
                self.set_main(("job", self._files_key(item)), "CI", render.job_view(item, self.main_width))
        elif f == "comments":
            item = self.p_comments.current()
            if item is None:
                self.set_main(("empty", "comments"), "Comments", text_block(self.p_comments.empty_text, theme.DIM))
            elif isinstance(item, Thread):
                self.show_thread(item)
            elif isinstance(item, Draft):
                self.show_draft(item)
            else:
                self.set_main(("message", self._comments_key(item)), "Message", render.message_view(item, self.main_width))

    def status_lines(self) -> list[Line]:
        counts = []
        for i, t in enumerate(self.cfg.tabs):
            if not self.tab_query(i):
                continue
            counts.append((t.name, len(self.tab_data[i]) if i in self.tab_loaded else None))
        return render.status_dashboard(
            host=self.cfg.host,
            port=self.cfg.port,
            user=self.cfg.user,
            project=self.cfg.project,
            branch=self.cfg.branch,
            repo=str(self.cfg.repo) if self.be.git.available else None,
            local_branch=self.local_branch,
            server_version=self.server_version,
            connected=self.connected,
            conn_error=self.conn_error,
            rest=self.rest_user if self.be.rest is not None and not self.rest_error else None,
            rest_error=self.rest_error,
            zuul=f"{self.cfg.zuul_url} (tenant {self.cfg.zuul_tenant})" if self.be.zuul else "",
            dry_run=self.cfg.dry_run,
            demo=self.demo,
            counts=counts,
            base_url=self.cfg.base_url,
            drafts=sorted(self.draft_counts.items()),
        )

    def show_change(self, n: int) -> None:
        ch = self.change(n)
        if ch is None:
            return
        chain = self.chains.get(n, (0, None))[1]
        live = self.zuul_cache.get((n, ch.ps), (0, None))[1]
        zurl = self.be.zuul.status_page(n, ch.ps) if self.be.zuul else ""
        lines = render.change_details(
            ch,
            base_url=self.cfg.base_url,
            width=self.main_width,
            chain=chain,
            threads=self.threads_cache.get(n),
            live=live,
            zuul_status_url=zurl,
            loading=n not in self.details,
        )
        self.set_main(("change", n), f"Change {n}", lines)

    def show_commit(self, c: LocalCommit) -> None:
        lines = render.commit_details(c, self.main_width)
        if c.change is not None:
            n = c.change.number
            ch = self.details.get(n) or c.change
            lines.append([("─" * min(self.main_width, 80), theme.DIM)])
            lines.append([])
            lines += render.change_details(
                ch,
                base_url=self.cfg.base_url,
                width=self.main_width,
                chain=self.chains.get(n, (0, None))[1],
                threads=self.threads_cache.get(n),
                live=self.zuul_cache.get((n, ch.ps), (0, None))[1],
                loading=n not in self.details,
            )
        self.set_main(("commit", c.sha), f"Commit {c.short}", lines)

    def change_subjects(self) -> dict[str, str]:
        """Revision -> subject for every patch set we know about (for /COMMIT_MSG parent lines)."""
        out: dict[str, str] = {}
        for c in list(self.details.values()) + [c for chs in self.tab_data.values() for c in chs]:
            for ps in c.patch_sets or ([c.current] if c.current else []):
                if ps.revision:
                    out[ps.revision] = c.subject
        return out

    def show_draft(self, d: Draft) -> None:
        thread = None
        if d.is_reply:
            for t in self.threads_cache.get(d.change, ([], False))[0]:
                if (d.in_reply_to and any(c.id == d.in_reply_to for c in t.comments)) or (
                    not d.in_reply_to and (t.path, t.line, t.patch_set) == (d.path, d.line, d.patch_set)
                ):
                    thread = t
                    break
        self.set_main(("draft", d.id), f"Draft · {d.short_location}", render.draft_view(d, self.main_width, thread))

    def show_thread(self, t: Thread) -> None:
        ch = self.ctx_change()
        if ch is None:
            return
        n = ch.number
        key = ("thread", n, t.patch_set, t.path, t.line, t.root.id or t.root.message[:40])
        ckey = (n, t.patch_set, t.path, t.line, t.side)
        exact = self.threads_cache.get(n, ([], False))[1]
        lines = render.thread_view(t, width=self.main_width, context=self.context_cache.get(ckey), exact=exact)
        self.main_thread = t
        self.set_main(key, f"Thread · {t.short_location}", lines)
        if ckey in self.context_cache or t.line <= 0 or not self.be.git.available:
            return
        if t.path.startswith("/") and t.path != COMMIT_MSG:
            return
        ps = ch.patch_set(t.patch_set)
        if ps is None or not ps.revision:
            return
        git = self.be.git

        def work() -> list[tuple[int, str]]:
            git.ensure_commit(ps.revision, ps.ref)
            if t.path == COMMIT_MSG:
                text = git.commit_message_file(ps.revision)
            else:
                rev = ps.parents[0] if t.side == "PARENT" and ps.parents else ps.revision
                text = git.show_file(rev, t.path)
            src = text.split("\n")
            lo, hi = max(1, t.line - 3), min(len(src), t.line + 3)
            return [(i, src[i - 1]) for i in range(lo, hi + 1)]

        def done(ctx: list[tuple[int, str]]) -> None:
            self.context_cache[ckey] = ctx
            if self.main_key == key:
                self.set_main(
                    key, f"Thread · {t.short_location}", render.thread_view(t, width=self.main_width, context=ctx, exact=exact)
                )

        self.submit(f"context:{ckey}", work, done, lambda e: None)

    # ------------------------------------------------------------------ command log & toasts

    def _on_command(self, ev: CommandEvent) -> None:
        self.cmdlog_entries.append(ev)
        if len(self.cmdlog_entries) > 300:
            self.cmdlog_entries = self.cmdlog_entries[-300:]
        self._update_cmdlog()

    def _update_cmdlog(self) -> None:
        lines = render.cmdlog_lines(self.cmdlog_entries, self.p_cmdlog.width)
        self.p_cmdlog.set_lines(lines, title="Command log", wrap=True, keep_scroll=True)
        self.p_cmdlog.scroll = self.p_cmdlog.max_scroll()

    def show_toast(self, text: str, error: bool = False, seconds: float = 4.0) -> None:
        self.toast = (text, theme.TOAST_ERR if error else theme.TOAST_OK, time.monotonic() + seconds)
        self.dirty = True

    def error_text(self, e: BaseException) -> str:
        if isinstance(e, CommandError):
            return f"{e.message}\n\n$ {e.display}"
        if isinstance(e, (NeedsRest, RestError, ZuulError, RuntimeError)):
            return str(e)
        return f"{type(e).__name__}: {e}"

    def background_error(self, what: str, e: BaseException) -> None:
        self.show_toast(f"{what} failed: {self.error_text(e).splitlines()[0]}", error=True, seconds=6)

    # ------------------------------------------------------------------ rendering

    def render(self) -> None:
        c = self.canvas
        c.clear()
        W, H = c.width, c.height
        if W < 40 or H < 12:
            c.put(0, 0, "Terminal too small for lazygerrit", theme.BAD)
            c.flush()
            return
        expand = self.focus if self.focus in SIDE_PANELS else self.last_side
        rects = compute_layout(
            W,
            H,
            focused=self.focus if self.focus in self.panels else "changes",
            expand=expand,
            screen_mode=self.screen_mode,
            show_cmdlog=self.show_cmdlog,
            cmdlog_size=self.cfg.command_log_size,
            side_ratio=self.cfg.side_panel_width,
        )
        for name, panel in self.panels.items():
            panel.rect = rects.get(name)
        self.p_status.set_lines([self.status_line()], wrap=False, keep_scroll=True)
        if self.p_cmdlog.rect is not None:
            self._update_cmdlog()
        for name, panel in self.panels.items():
            if panel.rect is None:
                continue
            focused = self.focus == name and not self.popups
            searching = self.input_mode is not None and self.focus == name
            panel.render(c, focused, searching)
        self.render_bottom(rects["bottom"].y, W)
        cursor = None
        for p in self.popups:
            p.render(c)
        if self.popups:
            cursor = self.popups[-1].cursor()
        elif self.input_mode:
            label = "Filter: " if self.input_mode == "filter" else "Search: "
            cursor = (H - 1, min(W - 1, text_width(label) + text_width(self.input_text)))
        c.set_cursor(cursor)
        c.flush()

    def status_line(self) -> Line:
        if self.connected:
            glyph = ("✓", theme.OK)
        elif self.connected is False:
            glyph = ("✗", theme.BAD)
        else:
            glyph = (theme.SPINNER[self.spin % 4], theme.DIM)
        parts: Line = [glyph, (" ", PLAIN), (self.cfg.project, PLAIN), (" → ", PLAIN)]
        parts.append(
            (self.local_branch or ("-" if not self.be.git.available else "…"), theme.OK if self.local_branch else theme.DIM)
        )
        host = f"{self.cfg.user}@{self.cfg.host}" if self.cfg.user else self.cfg.host
        parts.append((f"  {host}", theme.DIM))
        return parts

    def render_bottom(self, y: int, W: int) -> None:
        c = self.canvas
        right: Line = []
        if self.cfg.dry_run:
            right += [(" DRY RUN ", theme.DRY_RUN), (" ", PLAIN)]
        if self.demo:
            right += [(" DEMO ", theme.DRY_RUN), (" ", PLAIN)]
        right.append((f"lazygerrit {__version__}", PLAIN))
        rw = line_width(right)
        left: Line = []
        if self.input_mode:
            label = "Filter: " if self.input_mode == "filter" else "Search: "
            left = [(label, theme.INFO), (self.input_text, PLAIN)]
        else:
            if self.busy:
                label = next(iter(self.busy.values()))
                more = f" (+{len(self.busy) - 1})" if len(self.busy) > 1 else ""
                left += [(f"{label}{more} {theme.SPINNER[self.spin % 4]}", theme.SPINNER_STYLE), ("  ", PLAIN)]
            elif self.toast:
                left += [(self.toast[0], self.toast[1]), ("  ", PLAIN)]
            opts = self.popups[-1].options() if self.popups else self.context_options()
            left += options_line(opts)
        avail = max(0, W - rw - 1)
        c.put_line(y, 0, truncate(left, avail), avail)
        c.put_line(y, W - rw, right, rw)

    def context_options(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        ctxs = self.active_contexts()
        if self.focus in ("changes", "stack") and "change" in ctxs:
            ctxs = ["change"] + [c for c in ctxs if c != "change"]
        for ctx in ctxs:
            for b in self.bindings:
                if b.ctx == ctx and b.show and b.key not in seen:
                    seen.add(b.key)
                    bar = b.bar() if callable(b.bar) else b.bar
                    out.append((bar or b.desc, b.bar_key or b.label or b.key))
        out.append(("Keybindings", "?"))
        return out

    # ------------------------------------------------------------------ input

    def reload_drafts(self) -> None:
        """Pick up drafts written by another lazygerrit instance."""
        before, err = self.state.snapshot(), self.state.load_error
        self.state.load()
        if self.state.load_error != err:
            self.show_toast(self.state.load_error, error=True, seconds=8)
        if self.state.snapshot() != before:
            self._drafts_changed()

    def handle(self, ev: Event) -> None:
        if not isinstance(ev, Resize):
            self.last_input = time.monotonic()
        if isinstance(ev, Resize):
            if isinstance(self.canvas, CursesCanvas):
                curses.update_lines_cols()
                self.canvas.resize()
            # The new size is known after the next render; diffs depend on the width.
            self.later(0.05, self._rerender_diff)
            return
        if isinstance(ev, Mouse):
            self.handle_mouse(ev)
            return
        if self.popups:
            self.popups[-1].handle(ev)
            self.popups = [p for p in self.popups if not p.closed]
            return
        if self.input_mode:
            self.handle_input(ev)
            return
        if isinstance(ev, Key):
            self.handle_key(ev.name)

    def handle_key(self, k: str) -> None:
        for ctx in self.active_contexts():
            for b in self.bindings:
                if b.ctx == ctx and (b.key == k or k in b.alts):
                    b.fn()
                    return

    def active_contexts(self) -> list[str]:
        f = self.focus
        if f == "main":
            kind = self.main_key[0] if self.main_key else ""
            # Also while a diff is loading or failed, so | and ./, still work.
            mode = "diff" if self.p_main.cursor_mode or kind == "diff" else (kind if kind in ("thread", "draft") else "text")
            return [f"main:{mode}", "main", "global"]
        if f == "cmdlog":
            return ["main", "global"]
        ctxs: list[str] = []
        p = self.panels[f]
        if isinstance(p, ListPanel) and p.tabs:
            ctxs.append(f"{f}:{p.tabs[p.tab]}")
        ctxs.append(f)
        if f in ("changes", "stack", "files", "comments"):
            ctxs.append("change")
        if isinstance(p, ListPanel):
            ctxs.append("list")
        ctxs.append("global")
        return ctxs

    def handle_input(self, ev: Event) -> None:
        if isinstance(ev, Paste):
            self.input_text += ev.text.replace("\n", " ")
        elif isinstance(ev, Key):
            k = ev.name
            if k == "<esc>":
                self.input_text = ""
                self._apply_input(final=True)
                self.input_mode = None
                return
            if k == "<enter>":
                self._apply_input(final=True)
                self.input_mode = None
                return
            if k == "<backspace>":
                self.input_text = self.input_text[:-1]
            elif k == "<c-u>":
                self.input_text = ""
            elif k == "<c-w>":
                self.input_text = self.input_text.rstrip().rsplit(" ", 1)[0] if " " in self.input_text.strip() else ""
            elif k == "<space>":
                self.input_text += " "
            elif len(k) == 1:
                self.input_text += k
            else:
                return
        self._apply_input(final=False)

    def _apply_input(self, final: bool) -> None:
        if self.input_mode == "filter":
            p = self.panels[self.focus]
            if isinstance(p, ListPanel):
                p.set_filter(self.input_text)
                self.on_selection()
        elif self.input_mode == "search":
            panel = self.p_main if self.focus in ("main",) else self.p_main
            panel.search = self.input_text
            if self.input_text:
                row = panel.find(self.input_text, True, start=(panel.cursor if panel.cursor_mode else panel.scroll) - 1)
                if row is not None:
                    panel.goto_row(row)

    def handle_mouse(self, ev: Mouse) -> None:
        if self.popups or self.input_mode:
            return
        for name, panel in self.panels.items():
            r = panel.rect
            if r is None or not r.contains(ev.y, ev.x):
                continue
            if ev.kind in ("wheel-up", "wheel-down"):
                d = -1 if ev.kind == "wheel-up" else 1
                if isinstance(panel, ListPanel):
                    if name != self.focus:
                        self.focus_panel(name)
                    panel.move(d)
                    self.on_selection()
                else:
                    panel.scroll_by(3 * d)
                return
            if name == "cmdlog":
                return
            if name != self.focus:
                self.focus_panel(name)
            if isinstance(panel, ListPanel):
                if ev.y == r.y and panel.tabs:
                    self._click_tab(panel, ev.x - r.x - 2)
                    return
                idx = panel.index_at(ev.y)
                if idx is not None:
                    panel.anchor = None
                    panel.goto(idx)
                    self.on_selection()
            elif name == "main" and panel.cursor_mode:
                panel.set_cursor(panel.scroll + ev.y - r.y - 1)
            return

    def _click_tab(self, panel: ListPanel, x: int) -> None:
        pos = len(f"[{panel.jump}]") + 1 if panel.jump else 0
        for i, t in enumerate(panel.tabs):
            if pos <= x < pos + len(t):
                self.set_tab(panel, i)
                return
            pos += len(t) + 3

    # ------------------------------------------------------------------ popups

    def push(self, popup: Popup) -> None:
        self.popups.append(popup)
        self.dirty = True

    def menu(self, title: str, items: Sequence[MenuItem], **kw: Any) -> None:
        kw.setdefault("on_error", lambda msg: self.push(MessagePopup("Not available", msg, error=True)))
        self.push(MenuPopup(title, items, **kw))

    def confirm(self, title: str, text: str, fn: Callable[[], None]) -> None:
        self.push(ConfirmPopup(title, text, fn))

    def prompt(self, title: str, on_submit: Callable[[str], None], **kw: Any) -> None:
        self.push(PromptPopup(title, on_submit, **kw))

    def error(self, text: str, title: str = "Error") -> None:
        self.push(MessagePopup(title, text, error=True))

    # ------------------------------------------------------------------ bindings

    def _build_bindings(self) -> None:
        B = self.bindings

        def bind(
            ctx: str,
            key: str,
            desc: str,
            fn: Callable[[], None],
            show: Any = False,
            alts: tuple[str, ...] = (),
            label: Optional[str] = None,
            bar_key: Optional[str] = None,
        ) -> None:
            bar = show if isinstance(show, str) or callable(show) else None
            B.append(Binding(key, desc, fn, ctx, bool(show), alts, label, bar, bar_key))

        # global
        bind("global", "q", "Quit", self.quit, alts=("<c-c>",))
        bind("global", "<c-z>", "Suspend to the shell (resume with fg)", self.act_suspend)
        bind("global", "?", "Open keybindings menu", self.act_keybindings)
        for i, name in enumerate(SIDE_PANELS, 1):
            bind("global", str(i), f"Focus {name} panel", lambda n=name: self.focus_panel(n))
        bind("global", "<tab>", "Next panel", lambda: self.cycle_panel(1), alts=("l", "<right>"))
        bind("global", "<backtab>", "Previous panel", lambda: self.cycle_panel(-1), alts=("h", "<left>"))
        bind("global", "]", "Next tab", lambda: self.switch_tab(1))
        bind("global", "[", "Previous tab", lambda: self.switch_tab(-1))
        bind("global", "R", "Refresh", self.act_refresh)
        bind("global", "+", "Next screen mode (normal/half/fullscreen)", lambda: self.cycle_screen(1))
        bind("global", "_", "Previous screen mode", lambda: self.cycle_screen(-1))
        bind("global", "@", "Command log options", self.act_cmdlog_menu)
        bind("global", ":", "Run a gerrit SSH command", self.act_gerrit_command)
        bind("global", "<pgdown>", "Scroll down main view", lambda: self.scroll_main(1), alts=("J", "<c-d>"), label="J")
        bind("global", "<pgup>", "Scroll up main view", lambda: self.scroll_main(-1), alts=("K", "<c-u>"), label="K")
        bind("global", "<esc>", "Return / cancel", self.act_escape)

        # lists
        bind("list", "j", "Next item", lambda: self.move(1), alts=("<down>",))
        bind("list", "k", "Previous item", lambda: self.move(-1), alts=("<up>",))
        bind("list", ".", "Next page", lambda: self.move_page(1))
        bind("list", ",", "Previous page", lambda: self.move_page(-1))
        bind("list", "<", "Go to top", lambda: self.goto(0), alts=("g", "<home>"))
        bind("list", ">", "Go to bottom", lambda: self.goto(10**9), alts=("G", "<end>"))
        bind("list", "v", "Toggle range select", self.act_toggle_range)
        bind("list", "<s-down>", "Extend range down", lambda: self.range_move(1))
        bind("list", "<s-up>", "Extend range up", lambda: self.range_move(-1))
        bind("list", "/", "Filter list", self.act_filter)

        # status
        bind("status", "<enter>", "Show dashboard", lambda: self.update_main(), show="Dashboard")
        bind(
            "status",
            "o",
            "Open Gerrit dashboard in browser",
            lambda: self.open(f"{self.cfg.base_url}/dashboard/self"),
            show="Open dashboard",
        )

        # changes
        bind("changes", "<enter>", "Show files", self.act_enter_change)
        bind("changes", "<space>", "Check out change locally (git review -d)", self.act_checkout)
        bind("changes", "s", "Search (custom Gerrit query)", self.act_query, show="Search")

        # stack
        bind("stack", "<enter>", "Show files / local diff", self.act_enter_commit)
        bind("stack", "P", "Push for review…", self.act_push_menu, show="Push")

        # change actions shared by changes/stack/files/comments
        bind("change", "p", "Review: publish drafts with a vote and message", lambda: self.act_publish(), show=self.publish_bar)
        bind("change", "r", "CI: recheck, run-selected, prio_high…", self.act_ci_menu, show="Recheck/CI")
        bind("change", "a", "Vote (Code-Review, Secondary-Review)…", self.act_vote_menu, show="Vote")
        bind(
            "change",
            "c",
            "Reply / comment (review dialog; selected changes: comment on all)",
            lambda: self.act_comment(),
            show="Comment",
        )
        bind("change", "C", "Reply / comment using $EDITOR", lambda: self.act_comment(editor=True))
        bind("change", "b", "Rebase…", self.act_rebase_menu, show="Rebase")
        bind("change", "t", "Set topic", lambda: self.act_topic(False))
        bind("change", "T", "Set topic on tower", lambda: self.act_topic(True))
        bind("change", "#", "Hashtags…", self.act_hashtag_menu)
        bind("change", "i", "Add reviewers", self.act_add_reviewers)
        bind("change", "I", "Remove reviewer…", self.act_remove_reviewer)
        bind("change", "w", "Toggle work in progress / ready", self.act_toggle_wip)
        bind("change", "D", "Abandon / restore…", self.act_abandon)
        bind("change", "y", "Copy to clipboard…", self.act_copy_menu, show="Copy")
        bind("change", "o", "Open in browser", self.act_open, show="Open")
        bind("change", "x", "All actions for this change…", self.act_change_menu, show="Actions")

        # files
        bind("files:Files", "<enter>", "Review the diff (comment with c)", self.act_focus_main, show="Review diff")
        bind("files:Files", "<space>", "Toggle file reviewed", self.act_toggle_reviewed, show="Reviewed")
        bind("files:Files", "P", "View another patch set…", self.act_choose_patchset, show="Patch set")
        bind("files:Files", "B", "Compare against base / older patch set…", self.act_choose_base, show="Base")
        bind("files:Files", "z", "Toggle whole-file diffs", self.act_full_file)
        bind("files:Files", "|", "Switch the diff view (review view, delta, difftastic, …)", self.act_cycle_renderer)
        bind("files:Files", "e", "Edit file", self.act_edit_file)
        bind("files:CI", "<enter>", "Show job details", self.act_focus_main)
        bind("files:CI", "f", "Cycle job filter (all/failed/incl. skipped)", self.act_ci_filter, show="Filter jobs")

        # comments
        bind("comments:Comments", "<enter>", "Show in code (or view thread)", self.act_comment_enter, show="Show in code")
        bind("comments:Comments", "r", "Reply (draft)", lambda: self.act_reply(), show="Reply")
        bind("comments:Comments", "d", "Reply 'Done' and resolve (draft)", lambda: self.act_reply("Done", True), show="Done")
        bind("comments:Comments", "A", "Reply 'Ack' and resolve (draft)", lambda: self.act_reply("Ack", True))
        bind("comments:Comments", "u", "Toggle resolved (reply / draft)", self.act_reply_toggle)
        bind("comments:Comments", "c", "Edit draft / reply to the change", self.act_comments_c)
        bind("comments:Comments", "D", "Delete draft", self.act_delete_draft)
        bind("comments:Comments", "f", "Cycle filter (unresolved/all/incl. CI)", self.act_comment_filter, show="Show resolved")
        bind("comments:Comments", "e", "Open file at comment in $EDITOR", self.act_edit_thread_file)
        bind("comments:Messages", "<enter>", "View message", self.act_focus_main)
        bind("comments:Messages", "f", "Toggle CI bot messages", self.act_toggle_bots, show="Toggle bots")

        # main view
        bind("main", "j", "Down", lambda: self.main_move(1), alts=("<down>",))
        bind("main", "k", "Up", lambda: self.main_move(-1), alts=("<up>",))
        bind("main", "g", "Top", lambda: self.main_goto(0), alts=("<", "<home>"))
        bind("main", "G", "Bottom", lambda: self.main_goto(10**9), alts=(">", "<end>"))
        bind("main", "H", "Scroll left", lambda: self.main_hscroll(-8))
        bind("main", "L", "Scroll right", lambda: self.main_hscroll(8))
        bind("main", "/", "Search", self.act_search, show="Search")
        bind("main", "n", "Next match", lambda: self.next_match(True))
        bind("main", "N", "Previous match", lambda: self.next_match(False))
        bind("main", "h", "Back to side panel", lambda: self.focus_panel(self.last_side), alts=("<left>",))
        bind("main", "o", "Open in browser", self.act_open, show="Open")
        bind("main", "p", "Review: publish drafts with a vote and message", lambda: self.act_publish(), show=self.publish_bar)
        rv = self.in_review_view
        bind(
            "main:diff",
            "c",
            "Comment on line / range (draft); edit draft under cursor",
            rv(self.act_comment_line),
            show="Comment",
        )
        bind("main:diff", "v", "Select a range of lines", rv(self.act_diff_range))
        bind("main:diff", "r", "Reply to the thread under the cursor (draft)", rv(lambda: self.act_reply()), show="Reply")
        bind(
            "main:diff", "p", "Review: publish drafts with a vote and message", lambda: self.act_publish(), show=self.publish_bar
        )
        bind("main:diff", "d", "Reply 'Done' and resolve (draft)", rv(lambda: self.act_reply("Done", True)))
        bind("main:diff", "A", "Reply 'Ack' and resolve (draft)", rv(lambda: self.act_reply("Ack", True)))
        bind("main:diff", "u", "Toggle resolved (reply / draft)", rv(self.act_reply_toggle))
        bind("main:diff", "D", "Delete the draft under the cursor", rv(self.act_delete_draft))
        bind("main:diff", "]", "Next hunk", rv(lambda: self.diff_jump_hunk(1)), show="Hunk", bar_key="]/[")
        bind("main:diff", "[", "Previous hunk", rv(lambda: self.diff_jump_hunk(-1)))
        bind("main:diff", "}", "Next comment", rv(lambda: self.diff_jump_comment(1)), show="Thread", bar_key="}/{")
        bind("main:diff", "{", "Previous comment", rv(lambda: self.diff_jump_comment(-1)))
        cycle = "Switch the diff view (review view, delta, difftastic, …)"
        bind("main:diff", "|", cycle, self.act_cycle_renderer, show="Diff view" if len(self.renderers) > 1 else False)
        bind("main:diff", ".", "Next file", lambda: self.diff_next_file(1), show="File", bar_key="./,")
        bind("main:diff", ",", "Previous file", lambda: self.diff_next_file(-1))
        bind(
            "main:diff",
            "<space>",
            "Toggle reviewed (and go to the next file)",
            lambda: self.act_toggle_reviewed(advance=True),
            show="Reviewed",
        )
        bind("main:diff", "z", "Toggle whole-file diff", self.act_full_file)
        bind("main:diff", "P", "View another patch set…", self.act_choose_patchset)
        bind("main:diff", "B", "Compare against base / older patch set…", self.act_choose_base)
        bind("main:diff", "e", "Edit file at line", self.act_edit_file)
        bind("main:thread", "r", "Reply (draft)", lambda: self.act_reply(), show="Reply")
        bind("main:thread", "d", "Reply 'Done' and resolve (draft)", lambda: self.act_reply("Done", True), show="Done")
        bind("main:thread", "A", "Reply 'Ack' and resolve (draft)", lambda: self.act_reply("Ack", True))
        bind("main:thread", "u", "Toggle resolved (reply)", self.act_reply_toggle)
        bind("main:draft", "c", "Edit draft", self.act_comments_c, show="Edit")
        bind("main:draft", "u", "Toggle resolved", self.act_reply_toggle)
        bind("main:draft", "D", "Delete draft", self.act_delete_draft)

    # ------------------------------------------------------------------ navigation

    def focus_panel(self, name: str) -> None:
        if name not in self.panels:
            return
        if name == "cmdlog" and self.p_cmdlog.rect is None:
            return
        prev = self.focus
        self.focus = name
        if name in SIDE_PANELS:
            self.last_side = name
        if name == "main":
            return
        if prev != name:
            self.on_selection()

    def cycle_panel(self, d: int) -> None:
        if self.focus == "main":
            self.focus_panel(self.last_side)
            return
        cur = self.focus if self.focus in SIDE_PANELS else self.last_side
        i = SIDE_PANELS.index(cur)
        self.focus_panel(SIDE_PANELS[(i + d) % len(SIDE_PANELS)])

    def cycle_screen(self, d: int) -> None:
        self.screen_mode = (self.screen_mode + d) % 3

    def list_panel(self) -> Optional[ListPanel]:
        p = self.panels.get(self.focus)
        return p if isinstance(p, ListPanel) else None

    def move(self, d: int) -> None:
        p = self.list_panel()
        if p is None:
            return
        if p.anchor is not None:
            p.range_move(d)
        else:
            p.move(d)
        self.on_selection()

    def move_page(self, d: int) -> None:
        p = self.list_panel()
        if p:
            p.move(d * p.page_size)
            self.on_selection()

    def goto(self, idx: int) -> None:
        p = self.list_panel()
        if p:
            p.goto(idx)
            self.on_selection()

    def range_move(self, d: int) -> None:
        p = self.list_panel()
        if p:
            p.range_move(d)
            self.on_selection()

    def act_toggle_range(self) -> None:
        p = self.list_panel()
        if p:
            p.toggle_range()

    def switch_tab(self, d: int) -> None:
        p = self.panels.get(self.focus if self.focus != "main" else self.last_side)
        if isinstance(p, ListPanel) and p.tabs:
            self.set_tab(p, (p.tab + d) % len(p.tabs))

    def set_tab(self, p: ListPanel, idx: int) -> None:
        if idx == p.tab:
            return
        p.tab = idx
        p.anchor = None
        p.filter = ""
        p.selected = 0
        p.scroll = 0
        if p is self.p_changes:
            loaded = self.tab_loaded.get(idx)
            if loaded is None or time.monotonic() - loaded > 30:
                self.load_tab(idx)
            self.refresh_changes_panel()
            self.on_selection()
        else:
            self.refresh_ctx_panels()
            self.update_main()

    def scroll_main(self, d: int) -> None:
        self.p_main.scroll_by(d * max(1, self.p_main.height // 2))

    def main_move(self, d: int) -> None:
        if self.p_main.cursor_mode:
            self.p_main.move_cursor(d)
        else:
            self.p_main.scroll_by(d)

    def main_goto(self, row: int) -> None:
        if self.p_main.cursor_mode:
            self.p_main.set_cursor(row)
        else:
            self.p_main.scroll_to(row)

    def main_hscroll(self, d: int) -> None:
        if not self.p_main.wrap:
            self.p_main.hscroll = max(0, self.p_main.hscroll + d)

    def act_escape(self) -> None:
        if self.focus == "main" and self.p_main.anchor is not None:
            self.p_main.anchor = None
            return
        if self.focus in ("main", "cmdlog"):
            self.focus_panel(self.last_side)
            return
        p = self.list_panel()
        if p is not None:
            if p.anchor is not None:
                p.anchor = None
                return
            if p.filter:
                p.set_filter("")
                self.on_selection()
                return
        if self.focus in ("files", "comments"):
            self.focus_panel(self.change_source)

    def act_filter(self) -> None:
        p = self.list_panel()
        if p is None:
            return
        self.input_mode = "filter"
        self.input_text = p.filter

    def act_search(self) -> None:
        self.input_mode = "search"
        self.input_text = self.p_main.search

    def next_match(self, forward: bool) -> None:
        panel = self.p_main
        if not panel.search:
            return
        row = panel.find(panel.search, forward)
        if row is not None:
            panel.goto_row(row)

    def act_focus_main(self) -> None:
        self.update_main()
        self.focus = "main"

    def act_enter_change(self) -> None:
        if self.p_changes.current() is None:
            return
        self.p_files.tab = 0
        self.refresh_ctx_panels()
        self.focus_panel("files")

    def act_enter_commit(self) -> None:
        c = self.p_stack.current()
        if c is None:
            return
        if c.change is not None:
            self.p_files.tab = 0
            self.refresh_ctx_panels()
            self.focus_panel("files")
            return
        key = ("localdiff", c.sha)
        self.set_main(key, f"Commit {c.short} (local)", [[("Loading…", theme.DIM)]], wrap=False)
        self.focus = "main"

        def done(text: str) -> None:
            if self.main_key == key:
                lines, meta = render.diff_lines("", parse_unified(text), [], self.main_width)
                self.set_main(key, f"Commit {c.short} (local)", lines, wrap=False)

        self.submit(f"localdiff:{c.sha}", lambda: self.be.git.show_patch(c.sha), done, lambda e: self.error(self.error_text(e)))

    # ------------------------------------------------------------------ actions: generic

    def act_refresh(self) -> None:
        self.reload_drafts()
        # Retry diff tools that failed (successful renders of a patch set never change).
        self.rendered_out = OrderedDict((k, v) for k, v in self.rendered_out.items() if isinstance(v, tuple) and not v[0])
        self.load_status()
        self.load_tab(self.p_changes.tab)
        self.load_stack()
        if self.ctx is not None:
            n = self.ctx
            self.chains.pop(n, None)
            self.ensure_details(n, force=True)
            self.ensure_files(n, force=True)
            ch = self.ctx_change()
            if ch and ch.open:
                self.ensure_zuul(n, ch.ps, force=True)
        self.show_toast("Refreshing…")

    def act_keybindings(self) -> None:
        items: list[MenuItem] = []
        seen: set[str] = set()
        names = {"global": "Global", "list": "List navigation", "change": "Change actions"}
        for ctx in self.active_contexts():
            section = names.get(ctx, "Local")
            for b in self.bindings:
                if b.ctx != ctx or b.key in seen:
                    continue
                seen.add(b.key)
                items.append(MenuItem(b.desc, action=b.fn, key=b.label or b.key, section=section))
        self.menu("Keybindings", items)

    def act_suspend(self) -> None:
        import signal

        if isinstance(self.canvas, CursesCanvas):
            self.suspend_hook(lambda: os.kill(os.getpid(), signal.SIGTSTP))

    def act_cmdlog_menu(self) -> None:
        def toggle() -> None:
            self.show_cmdlog = not self.show_cmdlog

        def clear() -> None:
            self.cmdlog_entries = []
            self._update_cmdlog()

        self.menu(
            "Command log",
            [
                MenuItem("Toggle command log", toggle, key="t", description="shown" if self.show_cmdlog else "hidden"),
                MenuItem("Clear command log", clear, key="c"),
            ],
        )

    def open(self, url: str) -> None:
        try:
            open_url(url)
            self.show_toast(f"Opened {url}")
        except RuntimeError as e:
            self.error(str(e))

    def copy(self, text: str, what: str) -> None:
        try:
            how = copy_to_clipboard(text)
        except RuntimeError as e:
            self.error(f"Could not copy {what}: {e}\n\n{text}")
            return
        first = text.splitlines()[0] if text else ""
        if len(first) > 60:
            first = first[:59] + "…"
        self.show_toast(f"Copied {what} ({how}): {first}")

    def act_gerrit_command(self) -> None:
        def run(text: str) -> None:
            try:
                args = shlex.split(text)
            except ValueError as e:
                self.error(f"Could not parse command: {e}")
                return
            if args and args[0] == "gerrit":
                args = args[1:]
            if not args:
                return
            write = args[0] not in READ_ONLY_COMMANDS
            key = ("gerrit", text)

            def done(out: str) -> None:
                body = out
                if args[0] == "query" and any(a.startswith("--format=JSON") or a == "JSON" for a in args):
                    pretty = []
                    for ln in out.splitlines():
                        try:
                            pretty.append(json.dumps(json.loads(ln), indent=2))
                        except json.JSONDecodeError:
                            pretty.append(ln)
                    body = "\n".join(pretty)
                lines = [[(ln, PLAIN)] for ln in (body or "(no output)").split("\n")]
                self.set_main(key, "gerrit " + " ".join(args)[:60], lines, wrap=True)
                self.focus = "main"

            self.submit(
                f"gerrit:{text}:{time.monotonic()}",
                lambda: self.be.ssh.call(args, write=write, title=f"gerrit {args[0]}"),
                done,
                lambda e: self.error(self.error_text(e)),
                label=f"gerrit {args[0]}",
            )

        self.prompt(
            "Gerrit command (ssh … gerrit <command>)", run, hint="e.g. query --format=JSON owner:self status:open limit:3"
        )

    # ------------------------------------------------------------------ actions: change

    def target_changes(self) -> list[Change]:
        f = self.last_side if self.focus == "main" else self.focus
        if f == "changes":
            return [self.change(c.number) or c for c in self.p_changes.selection()]
        if f == "stack":
            return [self.change(c.change.number) or c.change for c in self.p_stack.selection() if c.change is not None]
        ch = self.ctx_change()
        return [ch] if ch else []

    def need_targets(self, single: bool = False) -> Optional[list[Change]]:
        ts = self.target_changes()
        if not ts:
            f = self.last_side if self.focus == "main" else self.focus
            if f == "stack" and self.p_stack.current() is not None:
                self.error("This commit is not on Gerrit yet. Push it first (P).", "Not on Gerrit")
            else:
                self.show_toast("No change selected", error=True)
            return None
        if single:
            return ts[:1]
        return ts

    @staticmethod
    def names(ts: Sequence[Change]) -> str:
        if len(ts) <= 3:
            return ", ".join(str(c.number) for c in ts)
        return f"{len(ts)} changes"

    def run_op(
        self,
        title: str,
        fn: Callable[[], Any],
        *,
        refresh: Sequence[int] = (),
        done_popup: bool = False,
        after: Optional[Callable[[Any], None]] = None,
        reload_stack: bool = False,
        on_fail: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        def on_done(result: Any) -> None:
            if isinstance(result, list):
                msg = "\n".join(str(r) for r in result)
            elif isinstance(result, str):
                msg = result
            else:
                msg = f"{title}: done"
            if self.cfg.dry_run:
                msg = "[dry run] " + msg
            if done_popup:
                self.push(MessagePopup(title, msg or "Done"))
            else:
                self.show_toast(msg.splitlines()[0] if msg else f"{title}: done")
            self.after_write(refresh, reload_stack)
            if after:
                after(result)

        def on_error(e: BaseException) -> None:
            if on_fail is not None:
                on_fail(e)
            self.error(self.error_text(e), f"{title} failed")
            self.after_write(refresh, reload_stack)

        self.submit(f"op:{title}:{time.monotonic()}", fn, on_done, on_error, label=title)

    def after_write(self, numbers: Sequence[int], reload_stack: bool = False) -> None:
        for n in numbers:
            self.details_at.pop(n, None)
            self.chains.pop(n, None)
            self.threads_at.pop(n, None)
        self.load_tab(self.p_changes.tab, quiet=True)
        if reload_stack or any(c.change and c.change.number in numbers for c in self.stack):
            self.load_stack(quiet=True)
        ctx = self.ctx
        if ctx is not None and (ctx in numbers or not numbers):
            self.ensure_details(ctx, force=True, quiet=True)
            self.ensure_files(ctx, force=True)

            def again() -> None:
                if self.ctx == ctx:
                    self.ensure_details(ctx, force=True, quiet=True)
                    ch = self.ctx_change()
                    if ch and ch.open:
                        self.ensure_zuul(ctx, ch.ps, force=True)

            self.later(8.0, again)

    def act_change_menu(self) -> None:
        ts = self.need_targets()
        if not ts:
            return
        ch = ts[0]
        items = [
            MenuItem(
                "CI…", self.act_ci_menu, key="r", description="recheck, slowcheck, regate, priocheck, run-selected, prio_high"
            ),
            MenuItem(
                "Review / publish drafts…",
                lambda: self.act_publish(),
                key="p",
                description=f"{self.draft_counts.get(ch.number, 0)} draft(s), votes and a message in one go",
            ),
            MenuItem("Vote…", self.act_vote_menu, key="a", description="Code-Review, Secondary-Review"),
            MenuItem("Comment", lambda: self.act_comment(), key="c", description="patchset-level comment"),
            MenuItem(
                "Comment (unresolved)", lambda: self.act_comment(unresolved=True), key="u", description="needs to be addressed"
            ),
            MenuItem("Comment using $EDITOR", lambda: self.act_comment(editor=True), key="C"),
            MenuItem("Rebase…", self.act_rebase_menu, key="b", description="change or tower, optionally recheck"),
            MenuItem("Set topic", lambda: self.act_topic(False), key="t", description=ch.topic or ""),
            MenuItem("Set topic on tower", lambda: self.act_topic(True), key="T"),
            MenuItem("Hashtags…", self.act_hashtag_menu, key="#", description=" ".join(ch.hashtags)),
            MenuItem("Add reviewers", self.act_add_reviewers, key="i"),
            MenuItem("Remove reviewer…", self.act_remove_reviewer, key="I"),
            MenuItem("Mark ready for review" if ch.wip else "Mark work in progress", self.act_toggle_wip, key="w"),
            MenuItem(
                "Restore" if ch.is_abandoned else "Abandon",
                self.act_abandon,
                key="D",
                disabled="Change is merged" if ch.is_merged else None,
            ),
            MenuItem("Check out locally", self.act_checkout, key="<space>", description=f"git review -d {ch.number}"),
            MenuItem("Copy…", self.act_copy_menu, key="y"),
            MenuItem("Open in browser", self.act_open, key="o"),
        ]
        self.menu(f"Actions for {self.names(ts)}", items)

    def act_ci_menu(self) -> None:
        ts = self.need_targets()
        if not ts:
            return
        names = self.names(ts)
        items: list[MenuItem] = []
        job = self.p_files.current() if self.focus == "files" and self.p_files.tab == 1 else None
        if isinstance(job, CIJob):
            items.append(
                MenuItem(
                    f"run-selected {job.short_name}",
                    lambda: self.do_run_selected(ts[:1], [job.short_name]),
                    key="J",
                    section="Selected job",
                    description="re-run just this job",
                )
            )
        for cmd, key, desc in CI_COMMANDS:
            items.append(
                MenuItem(cmd, lambda c=cmd: self.do_comment_command(ts, c), key=key, description=desc, section="Comment commands")
            )
        items.append(
            MenuItem(
                "run-selected…",
                lambda: self.act_run_selected(ts),
                key="R",
                description="pick jobs from src_config/premerge",
                section="Jobs",
            )
        )
        items.append(
            MenuItem(
                "prio_high…",
                lambda: self.act_prio_high(ts),
                key="h",
                description="priority hashtags + reason comment",
                section="Priority",
                disabled=None if self.be.rest is not None else self._rest_hint(),
            )
        )
        items.append(
            MenuItem(
                "recheck tower",
                lambda: self.do_tower(ts[0], "recheck"),
                key="t",
                description="this change and open changes below it",
                section="Tower",
            )
        )
        self.menu(f"CI for {names}", items)

    def _rest_hint(self) -> str:
        return (
            "This needs the Gerrit REST API (hashtags cannot be set over SSH).\n\n"
            f"Create an HTTP password at {self.cfg.base_url}/settings/#HTTPCredentials and add it to ~/.netrc:\n\n"
            f"  machine {self.cfg.host} login <username> password <http-password>"
        )

    def do_comment_command(self, ts: list[Change], cmd: str) -> None:
        self.run_op(
            f"{cmd} {self.names(ts)}",
            lambda: [ops.comment_command(self.be, t, cmd) for t in ops.resolve(self.be, ts)],
            refresh=[c.number for c in ts],
        )

    def act_run_selected(self, ts: list[Change]) -> None:
        def open_picker(jobs: list[str]) -> None:
            ch = ts[0]
            rep = latest_report(ch) if ch.detailed else None
            failed = [j.short_name for j in rep.failed_jobs] if rep else []
            ordered = list(dict.fromkeys(failed + jobs))
            items = [
                MenuItem(
                    name,
                    value=name,
                    checked=name in failed,
                    description="failed on the current patch set" if name in failed else "",
                )
                for name in ordered
            ]
            hint = "" if jobs else " (no run-selected jobs found in src_config/premerge; type a job name)"
            self.push(
                MenuPopup(
                    f"run-selected on {self.names(ts)} — type to filter, space to toggle{hint}",
                    items,
                    multi=True,
                    type_to_filter=True,
                    allow_custom=True,
                    on_submit=lambda values: self.do_run_selected(ts, values),
                )
            )

        if self.run_selected_jobs is not None:
            open_picker(self.run_selected_jobs)
            return

        def loaded(jobs: list[str]) -> None:
            self.run_selected_jobs = jobs
            open_picker(jobs)

        self.submit(
            "run-selected-jobs",
            lambda: discover_run_selected_jobs(self.cfg.repo),
            loaded,
            lambda e: open_picker([]),
            label="Reading src_config/premerge",
        )

    def do_run_selected(self, ts: list[Change], jobs: list[str]) -> None:
        if not jobs:
            return
        from ..srcrepo import run_selected_comment

        preview = run_selected_comment(ts[0].project, jobs)
        self.confirm(
            "run-selected",
            f"Post this comment on {self.names(ts)}?\n\n{preview}",
            lambda: self.run_op(
                f"run-selected {self.names(ts)}",
                lambda: [ops.run_selected(self.be, t, jobs) for t in ops.resolve(self.be, ts)],
                refresh=[c.number for c in ts],
            ),
        )

    def act_prio_high(self, ts: list[Change]) -> None:
        if self.be.rest is None:
            self.error(self._rest_hint(), "prio_high needs REST access")
            return
        tags = prio_high_hashtags(self.cfg.repo)

        def submit(reason: str) -> None:
            self.run_op(
                f"prio_high {self.names(ts)}",
                lambda: [ops.prio_high(self.be, t, reason.strip(), tags) for t in ops.resolve(self.be, ts)],
                refresh=[c.number for c in ts],
            )

        self.prompt(
            f"prio_high reason for {self.names(ts)}",
            submit,
            hint=f"Adds {', '.join('#' + t for t in tags)} and posts 'prio_high reason: <reason>'",
        )

    def do_tower(self, ch: Change, kind: str, **kw: Any) -> None:
        def fn(t: ops.Target) -> str:
            if kind == "recheck":
                return ops.comment_command(self.be, t, "recheck")
            if kind == "rebase":
                return ops.rebase(self.be, t, recheck=kw.get("recheck", False))
            if kind == "topic":
                return ops.set_topic(self.be, t, kw.get("topic"))
            if kind == "hashtag":
                return ops.hashtags(self.be, t, add=kw.get("hashtags", []))
            raise ValueError(kind)

        touched: list[int] = [ch.number]

        def work() -> list[str]:
            targets = ops.tower_targets(self.be, ch)
            touched.extend(t.number for t in targets if t.number != ch.number)
            chain = " → ".join(str(t.number) for t in reversed(targets))
            return [f"Tower: {chain} (bottom first)"] + ops.apply_bottom_up(targets, fn)

        label = {
            "recheck": "Recheck",
            "rebase": "Rebase" + (" and recheck" if kw.get("recheck") else ""),
            "topic": f"Set topic '{kw.get('topic') or ''}' on",
            "hashtag": "Add hashtags to",
        }[kind]
        self.confirm(
            f"{label} tower",
            f"{label} {ch.number} and every open change below it, bottom first?",
            lambda: self.run_op(f"{label} tower from {ch.number}", work, refresh=touched, done_popup=True, reload_stack=True),
        )

    def act_vote_menu(self) -> None:
        ts = self.need_targets()
        if not ts:
            return
        ch = ts[0]
        mine = {
            a.label: a.value
            for a in (ch.current.approvals if ch.current else [])
            if self.cfg.user and a.by.username == self.cfg.user
        }

        def item(label: str, value: int, key: str, desc: str) -> MenuItem:
            current = " (your current vote)" if mine.get(label) == value else ""
            text = f"{label} {value:+d}" if value else f"{label} 0"
            return MenuItem(text, lambda: self.do_vote(ts, label, value), key=key, description=desc + current, section=label)

        items = [
            item("Code-Review", 2, "a", "Looks good to me, approved"),
            item("Code-Review", 1, "1", "Looks good to me, but someone else must approve"),
            item("Code-Review", 0, "0", "No score"),
            item("Code-Review", -1, "n", "I would prefer this is not merged as is"),
            item("Code-Review", -2, "N", "This shall not be merged"),
            item("Secondary-Review", 1, "s", "Secondary review approved"),
            item("Secondary-Review", 0, "S", "No score"),
            item("Secondary-Review", -1, "x", "Secondary review rejected"),
            MenuItem("Other label…", self.act_custom_label, key="l", section="Other", description="e.g. Verified=0"),
            MenuItem(
                "Review dialog…", lambda: self.act_publish(), key="p", section="Other", description="vote + message + your drafts"
            ),
        ]
        self.menu(f"Vote on {self.names(ts)}", items)

    def do_vote(self, ts: list[Change], label: str, value: int) -> None:
        def go() -> None:
            seen = dict(self.seen_ps)
            self.run_op(
                f"{label}{value:+d} {self.names(ts)}" if value else f"{label} 0 {self.names(ts)}",
                lambda: [ops.vote(self.be, t, label, value) for t in ops.resolve(self.be, ts, strict=True, seen=seen)],
                refresh=[c.number for c in ts],
            )

        if value <= -2 or len(ts) > 1:
            self.confirm("Vote", f"Vote {label} {value:+d} on {self.names(ts)}?", go)
        else:
            go()

    def act_custom_label(self) -> None:
        ts = self.need_targets()
        if not ts:
            return

        def submit(text: str) -> None:
            label, _, value = text.replace(" ", "").partition("=")
            try:
                v = int(value)
            except ValueError:
                self.error("Expected LABEL=VALUE, e.g. Code-Review=+1")
                return
            self.do_vote(ts, label, v)

        self.prompt("Vote (LABEL=VALUE)", submit, hint="e.g. Code-Review=+1, Secondary-Review=0")

    def edit_text(self, header: Sequence[str], initial: str, on_done: Callable[[str], None]) -> None:
        fd, path = tempfile.mkstemp(prefix="lazygerrit-", suffix=".md")
        with os.fdopen(fd, "w") as fh:
            fh.write(initial + ("\n" if initial and not initial.endswith("\n") else ""))
            fh.write("\n" + SCISSORS + "\n")
            for h in header:
                fh.write(f"# {h}\n")
            fh.write("# Everything below the scissors line is ignored. An empty message aborts.\n")
        cmd = editor_command(self.cfg.editor(), path)
        try:
            self.suspend_hook(lambda: subprocess.call(cmd))
            with open(path) as fh:
                text = fh.read()
        except OSError as e:
            self.error(f"Could not run the editor ({' '.join(cmd)}): {e}")
            return
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        text = text.split(SCISSORS, 1)[0].rstrip()
        if text.strip():
            on_done(text)
        else:
            self.show_toast("Empty message, aborted", error=True)

    def act_rebase_menu(self) -> None:
        ts = self.need_targets()
        if not ts:
            return
        ch = ts[0]
        items = [
            MenuItem(
                "Rebase", lambda: self.do_rebase(ts, False), key="b", description="server-side rebase (gerrit review --rebase)"
            ),
            MenuItem("Rebase and recheck", lambda: self.do_rebase(ts, True), key="r"),
            MenuItem(
                "Rebase tower",
                lambda: self.do_tower(ch, "rebase", recheck=False),
                key="t",
                description=f"{ch.number} and open changes below, bottom first",
            ),
            MenuItem("Rebase tower and recheck", lambda: self.do_tower(ch, "rebase", recheck=True), key="T"),
        ]
        self.menu(f"Rebase {self.names(ts)}", items)

    def do_rebase(self, ts: list[Change], recheck: bool) -> None:
        self.run_op(
            f"Rebase {self.names(ts)}",
            lambda: [ops.rebase(self.be, t, recheck) for t in ops.resolve(self.be, ts)],
            refresh=[c.number for c in ts],
            reload_stack=True,
        )

    def act_topic(self, tower: bool) -> None:
        ts = self.need_targets()
        if not ts:
            return
        ch = ts[0]

        def submit(text: str) -> None:
            topic = text.strip() or None
            if tower:
                self.do_tower(ch, "topic", topic=topic)
            else:
                self.run_op(
                    f"Topic {self.names(ts)}",
                    lambda: [ops.set_topic(self.be, ops.Target.of(c), topic) for c in ts],
                    refresh=[c.number for c in ts],
                )

        def suggest(token: str) -> list[str]:
            return sorted(t for t in self.seen_topics if token.lower() in t.lower())

        self.prompt(
            f"Topic for {ch.number if tower else self.names(ts)}" + (" and the tower below it" if tower else ""),
            submit,
            initial=ch.topic or "",
            allow_empty=True,
            suggest=suggest,
            hint="Leave empty to clear the topic.",
        )

    def act_hashtag_menu(self) -> None:
        ts = self.need_targets()
        if not ts:
            return
        ch = ts[0]
        no_rest = None if self.be.rest is not None else self._rest_hint()
        items = [
            MenuItem("Add hashtag(s)…", lambda: self.act_add_hashtags(ts), key="a", disabled=no_rest),
            MenuItem(
                "Remove hashtag…",
                lambda: self.act_remove_hashtag(ts),
                key="d",
                disabled=no_rest or (None if any(c.hashtags for c in ts) else "No hashtags on this change"),
                description=" ".join("#" + h for h in ch.hashtags),
            ),
            MenuItem("Add hashtag(s) to tower…", lambda: self.act_add_hashtags(ts, tower=True), key="t", disabled=no_rest),
            MenuItem(
                "prio_high…",
                lambda: self.act_prio_high(ts),
                key="p",
                disabled=no_rest,
                description=" ".join(prio_high_hashtags(self.cfg.repo)),
            ),
        ]
        self.menu(f"Hashtags of {self.names(ts)}", items)

    def act_add_hashtags(self, ts: list[Change], tower: bool = False) -> None:
        def submit(text: str) -> None:
            tags = [t.strip().lstrip("#") for t in text.replace(" ", ",").split(",") if t.strip().lstrip("#")]
            if not tags:
                return
            if tower:
                self.do_tower(ts[0], "hashtag", hashtags=tags)
            else:
                self.run_op(
                    f"Hashtags {self.names(ts)}",
                    lambda: [ops.hashtags(self.be, ops.Target.of(c), add=tags) for c in ts],
                    refresh=[c.number for c in ts],
                )

        def suggest(token: str) -> list[str]:
            token = token.lstrip("#").lower()
            return sorted(h for h in self.seen_hashtags if token in h.lower())

        self.prompt(
            "Add hashtags", submit, suggest=suggest, token_sep=",", hint="Comma separated, e.g. low_risk, zuul_no_smart_recheck"
        )

    def act_remove_hashtag(self, ts: list[Change]) -> None:
        tags = sorted({h for c in ts for h in c.hashtags})
        items = [
            MenuItem(
                f"#{tag}",
                lambda tag=tag: self.run_op(
                    f"Remove #{tag}",
                    lambda: [ops.hashtags(self.be, ops.Target.of(c), remove=[tag]) for c in ts if tag in c.hashtags],
                    refresh=[c.number for c in ts],
                ),
            )
            for tag in tags
        ]
        self.menu("Remove hashtag", items)

    def suggest_accounts(self, token: str) -> list[tuple[str, str]]:
        t = token.lower()
        out = []
        for a in sorted(self.known_accounts.values(), key=lambda a: a.display.lower()):
            if not t or t in a.username.lower() or t in a.name.lower() or t in a.email.lower():
                out.append((a.username, f"{a.display} ({a.username}{', ' + a.email if a.email else ''})"))
        return out

    def act_add_reviewers(self) -> None:
        ts = self.need_targets()
        if not ts:
            return

        def submit(text: str) -> None:
            who = [w.strip() for w in text.split(",") if w.strip()]
            if not who:
                return
            self.run_op(
                f"Add reviewers to {self.names(ts)}",
                lambda: [ops.add_reviewers(self.be, ops.Target.of(c), who) for c in ts],
                refresh=[c.number for c in ts],
            )

        self.prompt(
            f"Add reviewers to {self.names(ts)}",
            submit,
            suggest=self.suggest_accounts,
            token_sep=",",
            hint="Usernames, emails or groups, comma separated. Tab completes people seen in your changes.",
        )

    def act_remove_reviewer(self) -> None:
        ts = self.need_targets(single=True)
        if not ts:
            return
        ch = ts[0]
        people = [r for r in ch.reviewers if r.username != ch.owner.username]
        if not people:
            self.show_toast("No reviewers to remove", error=True)
            return
        items = [
            MenuItem(
                r.display,
                lambda r=r: self.confirm(
                    "Remove reviewer",
                    f"Remove {r.display} from {ch.number}?",
                    lambda: self.run_op(
                        f"Remove {r.short}",
                        lambda: ops.remove_reviewers(self.be, ops.Target.of(ch), [r.username or r.email]),
                        refresh=[ch.number],
                    ),
                ),
                description=r.username,
            )
            for r in people
        ]
        self.menu(f"Remove reviewer from {ch.number}", items)

    def act_toggle_wip(self) -> None:
        ts = self.need_targets()
        if not ts:
            return
        wip = not ts[0].wip
        what = "work in progress" if wip else "ready for review"
        self.confirm(
            "Work in progress" if wip else "Ready for review",
            f"Mark {self.names(ts)} as {what}?",
            lambda: self.run_op(
                f"Mark {what}",
                lambda: [ops.set_wip(self.be, t, wip) for t in ops.resolve(self.be, ts)],
                refresh=[c.number for c in ts],
            ),
        )

    def act_abandon(self) -> None:
        ts = self.need_targets(single=True)
        if not ts:
            return
        ch = ts[0]
        if ch.is_merged:
            self.error(f"{ch.number} is merged.")
            return
        if ch.is_abandoned:
            self.confirm(
                "Restore",
                f"Restore {ch.number} ({ch.subject})?",
                lambda: self.run_op(
                    f"Restore {ch.number}", lambda: ops.restore(self.be, ops.resolve(self.be, [ch])[0]), refresh=[ch.number]
                ),
            )
            return

        def submit(text: str) -> None:
            self.run_op(
                f"Abandon {ch.number}",
                lambda: ops.abandon(self.be, ops.resolve(self.be, [ch])[0], text.strip() or None),
                refresh=[ch.number],
            )

        self.prompt(
            f"Abandon {ch.number}: {ch.subject}",
            submit,
            allow_empty=True,
            hint="Optional message. Enter abandons the change, Esc cancels.",
        )

    def act_checkout(self) -> None:
        ts = self.need_targets(single=True)
        if not ts:
            return
        ch = ts[0]
        if not self.be.git.available:
            self.error("No local repository to check out into (use --repo PATH).")
            return
        if ch.current is None:
            return

        def work() -> str:
            if self.be.git.is_dirty():
                raise RuntimeError("You have uncommitted changes in the working tree. Commit or stash them first.")
            cur = self.be.light(ch.number) or ch
            ps = cur.current or ch.current
            out = self.be.git.checkout_change(ch.number, ps.number, ps.ref, ps.revision)
            return out.strip().splitlines()[-1] if out.strip() else f"Checked out {ch.number},{ps.number}"

        def done(_: Any) -> None:
            self.load_status()

        self.confirm(
            "Check out",
            f"Check out the latest patch set of {ch.number} ({ch.subject}) in {self.cfg.repo}?\n\n"
            f"Runs: git review -d {ch.number},<current patch set>",
            lambda: self.run_op(f"Checkout {ch.number}", work, reload_stack=True, after=done),
        )

    def act_copy_menu(self) -> None:
        ts = self.need_targets()
        if not ts:
            return
        ch = ts[0]
        url = self.cfg.change_url(ch.number, ch.project)
        ps = ch.current
        items: list[MenuItem] = []
        if len(ts) > 1:
            listing = "\n".join(f"- {c.number}: {c.subject} | {self.cfg.change_url(c.number, c.project)}" for c in ts)
            items.append(
                MenuItem(
                    "Selected changes as markdown list",
                    lambda: self.copy(listing, "links"),
                    key="L",
                    description=f"{len(ts)} changes",
                    section="Selection",
                )
            )
        items += [
            MenuItem(
                "Change number",
                lambda: self.copy(str(ch.number), "change number"),
                key="n",
                description=str(ch.number),
                section="Change",
            ),
            MenuItem("URL", lambda: self.copy(url, "URL"), key="u", description=url, section="Change"),
            MenuItem(
                "Link with subject",
                lambda: self.copy(f"{ch.number}: {ch.subject} | {url}", "link"),
                key="l",
                description=f"{ch.number}: {ch.subject} | …",
                section="Change",
            ),
            MenuItem(
                "Markdown link",
                lambda: self.copy(f"[{ch.number}: {ch.subject}]({url})", "markdown link"),
                key="m",
                section="Change",
            ),
            MenuItem(
                "Change-Id", lambda: self.copy(ch.change_id, "Change-Id"), key="i", description=ch.change_id, section="Change"
            ),
        ]
        if ps is not None:
            items += [
                MenuItem(
                    "Commit SHA",
                    lambda: self.copy(ps.revision, "commit SHA"),
                    key="s",
                    description=ps.revision[:12],
                    section="Patch set",
                ),
                MenuItem("Patch set ref", lambda: self.copy(ps.ref, "ref"), key="r", description=ps.ref, section="Patch set"),
                MenuItem(
                    "Fetch & checkout command",
                    lambda: self.copy(f"git fetch {self.cfg.remote} {ps.ref} && git checkout FETCH_HEAD", "command"),
                    key="f",
                    description=f"git fetch {self.cfg.remote} {ps.ref} …",
                    section="Patch set",
                ),
                MenuItem(
                    "git review -d command",
                    lambda: self.copy(f"git review -d {ch.number},{ps.number}", "command"),
                    key="d",
                    section="Patch set",
                ),
            ]
        items += [
            MenuItem(
                "Tower as markdown list",
                lambda: self.copy_tower(ch, False),
                key="t",
                description="bottom first, like rng link_tower",
                section="Tower",
            ),
            MenuItem("Tower URLs", lambda: self.copy_tower(ch, True), key="T", section="Tower"),
        ]
        item = self.p_files.current() if self.focus in ("files", "main") else None
        if isinstance(item, FileInfo) and not item.is_magic:
            items.append(
                MenuItem("File path", lambda: self.copy(item.path, "path"), key="p", description=item.path, section="File")
            )
        if isinstance(item, CIJob) and item.url:
            items.append(MenuItem("Job URL", lambda: self.copy(item.url, "job URL"), key="J", description=item.url, section="CI"))
        self.menu("Copy to clipboard", items)

    def copy_tower(self, ch: Change, short: bool) -> None:
        def work() -> str:
            targets = ops.tower_targets(self.be, ch)
            return ops.link_tower(self.cfg.change_url, targets, short)

        self.submit(
            f"tower:{ch.number}:{short}",
            work,
            lambda text: self.copy(text, "tower links"),
            lambda e: self.error(self.error_text(e)),
            label="Resolving tower",
        )

    def act_open(self) -> None:
        ch = self.ctx_change() if self.focus not in ("changes", "stack") else (self.target_changes() or [None])[0]
        f = self.last_side if self.focus == "main" else self.focus
        if f == "files":
            item = self.p_files.current()
            if isinstance(item, FileInfo) and ch is not None:
                view = self.viewed_ps(ch)
                base = self.base_patchset(ch)
                psid = f"{base.number}..{view.number if view else ch.ps}" if base else str(view.number if view else ch.ps)
                path = item.path if item.is_magic else item.path.lstrip("/")
                url = f"{self.cfg.change_url(ch.number, ch.project)}/{psid}/{path}"
                if self.focus == "main":
                    meta = self.p_main.cursor_meta()
                    if isinstance(meta, LineRef) and meta.line:
                        left = meta.side == "PARENT" or (base is not None and meta.patch_set == base.number)
                        url += f"#{'b' if left else ''}{meta.line}"
                self.open(url)
                return
            if isinstance(item, CIJob):
                self.open(
                    item.url or item.stream_url or (self.be.zuul.status_page(ch.number, ch.ps) if self.be.zuul and ch else "")
                )
                return
            if isinstance(item, CIHeader) and item.url:
                self.open(item.url)
                return
        if f == "comments":
            item = self.p_comments.current()
            if isinstance(item, Thread) and ch is not None and item.last.id:
                self.open(f"{self.cfg.change_url(ch.number, ch.project)}/comment/{item.last.id}/")
                return
        if ch is None:
            self.show_toast("No change selected", error=True)
            return
        self.open(self.cfg.change_url(ch.number, ch.project))

    def act_query(self) -> None:
        def submit(text: str) -> None:
            self.custom_query = text.strip()
            self.custom_query_set = True
            idx = self.query_tab_index()
            self.tab_loaded.pop(idx, None)
            self.tab_data.pop(idx, None)
            if self.p_changes.tab != idx:
                self.set_tab(self.p_changes, idx)
            else:
                self.load_tab(idx)
                self.refresh_changes_panel()
            self.focus_panel("changes")

        self.prompt(
            "Gerrit search",
            submit,
            initial=self.custom_query,
            hint="Any Gerrit query, e.g. 'status:open project:src topic:foo' or 'reviewer:self -is:reviewed'",
        )

    # ------------------------------------------------------------------ actions: stack

    def act_push_menu(self) -> None:
        if not self.be.git.available:
            self.error("No local repository (use --repo PATH).")
            return
        c = self.p_stack.current()
        branch = self.cfg.branch
        remote = self.cfg.remote
        items = [
            MenuItem(
                "Push HEAD for review",
                lambda: self.do_push("HEAD", []),
                key="P",
                description=f"git push {remote} HEAD:refs/for/{branch}",
            ),
        ]
        if c is not None:
            items.append(
                MenuItem(
                    f"Push {c.short} and the commits below it", lambda: self.do_push(c.sha, []), key="s", description=c.subject
                )
            )
        items += [
            MenuItem("Push as work in progress", lambda: self.do_push("HEAD", ["wip"]), key="w", description="%wip"),
            MenuItem("Push and mark ready for review", lambda: self.do_push("HEAD", ["ready"]), key="r", description="%ready"),
            MenuItem(
                "Push with topic…",
                lambda: self.prompt("Topic", lambda t: self.do_push("HEAD", [f"topic={t.strip()}"]) if t.strip() else None),
                key="t",
            ),
            MenuItem(
                "Push with hashtag…",
                lambda: self.prompt(
                    "Hashtag", lambda t: self.do_push("HEAD", [f"hashtag={t.strip().lstrip('#')}"]) if t.strip() else None
                ),
                key="#",
            ),
            MenuItem(
                "Push with reviewers…",
                lambda: self.prompt(
                    "Reviewers",
                    lambda t: self.do_push("HEAD", [f"r={w.strip()}" for w in t.split(",") if w.strip()]),
                    suggest=self.suggest_accounts,
                    token_sep=",",
                ),
                key="i",
            ),
        ]
        self.menu("Push for review", items)

    def do_push(self, rev: str, options: list[str]) -> None:
        refspec = f"{rev if rev == 'HEAD' else rev[:12]}:refs/for/{self.cfg.branch}" + (
            "%" + ",".join(options) if options else ""
        )
        self.confirm(
            "Push for review",
            f"Run this in {self.cfg.repo}?\n\ngit push {self.cfg.remote} {refspec}",
            lambda: self.run_op(
                "Push for review",
                lambda: self.be.git.push(rev, self.cfg.branch, options) or "Pushed",
                done_popup=True,
                reload_stack=True,
            ),
        )

    # ------------------------------------------------------------------ actions: files / CI

    def act_edit_file(self) -> None:
        if not self.cfg.repo:
            return
        line = None
        item = self.p_files.current()
        path = item.path if isinstance(item, FileInfo) else None
        if self.focus == "main" and self.main_diff:
            path = self.main_diff[2]
            meta = self.p_main.cursor_meta()
            if isinstance(meta, LineRef) and meta.side is None and meta.patch_set == self.main_diff[1]:
                line = meta.line
        if not path or path.startswith("/"):
            self.show_toast("Select a file", error=True)
            return
        self._edit_path(path, line)

    def _edit_path(self, path: str, line: Optional[int]) -> None:
        full = os.path.join(str(self.cfg.repo), path)
        if not os.path.exists(full):
            self.error(f"{path} does not exist in your working tree ({self.local_branch}).")
            return
        cmd = editor_command(self.cfg.editor(), full, line)
        self.suspend_hook(lambda: subprocess.call(cmd, cwd=str(self.cfg.repo)))

    def act_ci_filter(self) -> None:
        self.ci_filter = (self.ci_filter + 1) % 3
        self.show_toast(
            ["Showing all jobs except skipped", "Showing failed and running jobs", "Showing all jobs incl. skipped"][
                self.ci_filter
            ]
        )
        self.refresh_ctx_panels()
        self.update_main()

    # ------------------------------------------------------------------ actions: comments

    def current_thread(self) -> Optional[Thread]:
        if self.focus == "main":
            if self.p_main.cursor_mode:
                meta = self.p_main.cursor_meta()
                if isinstance(meta, tuple) and meta and meta[0] == "thread":
                    return meta[1]
                return None
            if self.main_key and self.main_key[0] == "thread":
                return self.main_thread
            return None
        item = self.p_comments.current() if self.focus == "comments" and self.p_comments.tab == 0 else None
        return item if isinstance(item, Thread) else None

    def act_comment_filter(self) -> None:
        if not self.show_resolved:
            self.show_resolved, self.show_ci_comments = True, False
            msg = "Showing all comment threads"
        elif not self.show_ci_comments:
            self.show_ci_comments = True
            msg = "Showing all threads including CI comments (recheck, run-selected…)"
        else:
            self.show_resolved, self.show_ci_comments = False, False
            msg = "Showing unresolved threads only"
        self.show_toast(msg)
        self.refresh_ctx_panels()
        self.update_main()

    def act_toggle_bots(self) -> None:
        self.show_bot_messages = not self.show_bot_messages
        self.show_toast("Showing CI bot messages" if self.show_bot_messages else "Hiding CI bot messages")
        self.refresh_ctx_panels()
        self.update_main()

    def act_edit_thread_file(self) -> None:
        t = self.current_thread()
        if t is None or t.path.startswith("/"):
            self.show_toast("Select a file comment", error=True)
            return
        self._edit_path(t.path, t.line or None)
