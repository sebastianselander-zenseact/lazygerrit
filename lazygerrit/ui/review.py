"""Code review: patch set selection, diffs with drafts, draft comments and publishing reviews.

Mixed into :class:`lazygerrit.ui.app.App`; everything here runs on the UI thread except the
functions handed to ``self.submit``/``self.run_op``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional

from .. import operations as ops
from ..diff import LineRef, file_as_added, parse_unified, unified_text_diff
from ..drafts import Draft
from ..models import COMMIT_MSG, PATCHSET_LEVEL, Change, FileInfo, PatchSet, Thread
from ..renderers import BUILTIN, DiffRenderer, run_highlighter
from ..renderers import render as render_with
from . import render, theme
from .render import first_line
from .text import PLAIN, Line, Style, ansi_to_lines, plain, strip_ansi, text_block
from .widgets import MenuItem, ReviewPopup

if TYPE_CHECKING:  # pragma: no cover
    from .app import App

FULL_FILE_CONTEXT = 1_000_000
CODE_REVIEW = ("Code-Review", [-2, -1, 0, 1, 2])
SECONDARY_REVIEW = ("Secondary-Review", [-1, 0, 1])


def highlight(
    git: Any, command: Optional[str], diff: str, rows: list, scheme: str
) -> tuple[dict[int, tuple[Line, Optional[Style]]], str]:
    """Syntax highlighting for the review view (runs in a worker): (styles for diff_lines, problem)."""
    if not command or not rows:
        return {}, ""
    status, colored = run_highlighter(git, command, diff, color_scheme=scheme)
    if status:
        why = next((ln.strip() for ln in strip_ansi(colored).splitlines() if ln.strip()), f"exit status {status}")
        return {}, f"No syntax highlighting: {command} failed: {why}"
    styles = render.highlight_rows(rows, diff, colored)
    if not styles and any(r.kind in ("add", "del", "ctx") for r in rows):
        return {}, f"No syntax highlighting: {command} changed the diff's lines (use a filter like delta --color-only)"
    return styles, ""


def thread_key(t: Thread) -> tuple:
    return (t.path, t.line, t.patch_set, t.side, t.root.id or t.root.message[:60])


class ReviewMixin:
    # ------------------------------------------------------------------ patch set selection

    def viewed_ps(self: "App", ch: Change) -> Optional[PatchSet]:
        k = self.view_ps.get(ch.number)
        if k and k != ch.ps:
            ps = ch.patch_set(k)
            if ps is not None and ps.revision:
                return ps
        return ch.current

    def base_patchset(self: "App", ch: Change) -> Optional[PatchSet]:
        k = self.base_ps.get(ch.number)
        view = self.viewed_ps(ch)
        if k and view is not None and k < view.number:
            return ch.patch_set(k)
        return None

    def is_default_view(self: "App", ch: Change) -> bool:
        view = self.viewed_ps(ch)
        return (view is None or view.number == ch.ps) and self.base_patchset(ch) is None

    def view_label(self: "App", ch: Change) -> str:
        view = self.viewed_ps(ch)
        base = self.base_patchset(ch)
        if view is None:
            return f"{ch.number}"
        text = f"{ch.number},{view.number}"
        if base is not None:
            text = f"{ch.number} ps{base.number}→ps{view.number}"
        if view.number != ch.ps:
            text += " (old)"
        return text

    def files_for_view(self: "App", ch: Change) -> Optional[list[FileInfo]]:
        if self.is_default_view(ch):
            return self.current_files(ch)
        view = self.viewed_ps(ch)
        base = self.base_patchset(ch)
        assert view is not None
        key = (ch.number, view.number, base.number if base else 0)
        if key in self.local_files:
            return self.local_files[key]
        self.ensure_local_files(ch, view, base)
        return None

    def ensure_local_files(self: "App", ch: Change, view: PatchSet, base: Optional[PatchSet]) -> None:
        key = (ch.number, view.number, base.number if base else 0)
        git = self.be.git
        if not git.available:
            self.local_files[key] = []
            self.show_toast("Older patch sets need a local clone (use --repo)", error=True)
            return

        def own(ps: PatchSet) -> set[str]:
            parent = ps.parents[0] if ps.parents else f"{ps.revision}^"
            paths: set[str] = set()
            for f in git.files_changed(parent, ps.revision):
                paths.add(f.path)
                if f.old_path:
                    paths.add(f.old_path)
            return paths

        def work() -> tuple[list[FileInfo], dict[int, set[str]]]:
            git.ensure_commit(view.revision, view.ref)
            if base is None:
                parent = view.parents[0] if view.parents else f"{view.revision}^"
                return [FileInfo(COMMIT_MSG, "ADDED")] + git.files_changed(parent, view.revision), {}
            git.ensure_commit(base.revision, base.ref)
            owns = {view.number: own(view), base.number: own(base)}
            touched = owns[view.number] | owns[base.number]
            # Like Gerrit, leave out files that only differ because of a rebase.
            files = [
                f
                for f in git.files_changed(base.revision, view.revision)
                if f.path in touched or (f.old_path and f.old_path in touched)
            ]
            return [FileInfo(COMMIT_MSG, "ADDED")] + files, owns

        def done(result: tuple[list[FileInfo], dict[int, set[str]]]) -> None:
            files, owns = result
            for psn, paths in owns.items():
                self.own_files[(ch.number, psn)] = paths
            self.local_files[key] = files
            if self.ctx == ch.number:
                self.refresh_ctx_panels()
                self.update_main(data_changed=True)

        self.submit(f"localfiles:{key}", work, done, lambda e: self.background_error("Listing files", e), label="Listing files")

    def set_view(self: "App", n: int, view: Optional[int], base: Optional[int]) -> None:
        ch = self.change(n)
        if ch is None:
            return
        if view is None or view == ch.ps:
            self.view_ps.pop(n, None)
        else:
            self.view_ps[n] = view
        if base is None:
            self.base_ps.pop(n, None)
        else:
            self.base_ps[n] = base
        self.p_files.tab = 0
        self.refresh_ctx_panels()
        if self.focus == "main" and self.main_diff and self.main_diff[0] == n:
            self._reshow_diff()
        else:
            self.update_main(data_changed=True)
        self.show_toast(f"Viewing {self.view_label(ch)}" + ("" if base else " against its parent"))

    def act_choose_patchset(self: "App") -> None:
        ch = self.ctx_change()
        if ch is None:
            return
        if not ch.detailed or not ch.patch_sets:
            self.ensure_details(ch.number)
            self.show_toast("Loading patch sets…")
            return
        view = self.viewed_ps(ch)
        threads = self.threads_cache.get(ch.number, ([], False))[0]
        items = []
        for ps in reversed(ch.patch_sets):
            k = ps.number
            count = sum(1 for t in threads if t.patch_set == k and not t.is_ci)
            desc = f"{ps.kind.replace('_', ' ').lower() or 'patch set'} · {ps.uploader.display} · {render.rel_ago(ps.created_on)}"
            if count:
                desc += f" · {count} comment thread{'s' if count != 1 else ''}"
            label = f"Patch set {k}" + (" (current)" if k == ch.ps else "") + (" ◀ viewing" if view and view.number == k else "")
            base = self.base_ps.get(ch.number)
            items.append(
                MenuItem(
                    label,
                    lambda k=k, base=base: self.set_view(ch.number, k, base if base and base < k else None),
                    key=str(k) if k < 10 else None,
                    description=desc,
                )
            )
        self.menu(f"View patch set of {ch.number}", items)

    def act_choose_base(self: "App") -> None:
        ch = self.ctx_change()
        if ch is None:
            return
        if not ch.detailed or not ch.patch_sets:
            self.ensure_details(ch.number)
            self.show_toast("Loading patch sets…")
            return
        view = self.viewed_ps(ch)
        if view is None:
            return
        cur_base = self.base_ps.get(ch.number)
        items = [
            MenuItem(
                "Parent commit (the whole change)" + ("" if cur_base else " ◀"),
                lambda: self.set_view(ch.number, view.number, None),
                key="b",
                description="what Gerrit shows by default",
            )
        ]
        for ps in reversed([p for p in ch.patch_sets if p.number < view.number]):
            k = ps.number
            items.append(
                MenuItem(
                    f"Patch set {k}" + (" ◀" if cur_base == k else ""),
                    lambda k=k: self.set_view(ch.number, view.number, k),
                    key=str(k) if k < 10 else None,
                    description="what changed since then (includes rebases)",
                )
            )
        self.menu(f"Compare patch set {view.number} of {ch.number} against", items)

    # ------------------------------------------------------------------ diffs

    def diff_key(self: "App", ch: Change, f: FileInfo) -> tuple:
        view = self.viewed_ps(ch)
        base = self.base_patchset(ch)
        return ("diff", ch.number, view.number if view else 0, base.number if base else 0, f.path, self.full_file, f.old_path)

    def show_diff(self: "App", f: FileInfo) -> None:
        ch = self.ctx_change()
        if ch is None or ch.current is None:
            return
        view = self.viewed_ps(ch)
        base = self.base_patchset(ch)
        if view is None:
            return
        key = self.diff_key(ch, f)
        self.main_diff = (ch.number, view.number, f.path)
        renderer = self.diff_renderer()
        if not renderer.builtin:
            self._render_external(key, renderer)
            return
        title = f"Diff · {f.display_path} · ps{view.number}"
        if key in self.diff_rows:
            self._render_diff(key)
            return
        git = self.be.git
        if (
            f.path == COMMIT_MSG
            and base is None
            and view.number == ch.ps
            and ch.commit_message
            and not (git.available and git.has_commit(view.revision))
        ):
            self.diff_rows[key] = file_as_added(render.synth_commit_msg(ch, view, self.change_subjects()))
            self._render_diff(key)
            return
        if not git.available:
            self.set_main(
                key,
                title,
                [[("Diffs need a local clone of the repository (start lazygerrit inside it or pass --repo).", theme.DIM)]],
            )
            return
        if key != self.main_key:
            note = "Loading diff… (fetching the patch set from Gerrit first if it is not in your clone yet)"
            self.set_main(key, title, [[(note, theme.DIM)]], wrap=False)
        context = FULL_FILE_CONTEXT if self.full_file else self.diff_context

        highlighter, scheme = self.syntax_command, self.color_scheme

        def work() -> tuple[list, dict[int, tuple[Line, Optional[Style]]], str]:
            git.ensure_commit(view.revision, view.ref)
            if base is not None:
                git.ensure_commit(base.revision, base.ref)
            if f.path == COMMIT_MSG:
                new = git.commit_message_file(view.revision)
                if base is None:
                    return file_as_added(new), {}, ""
                return unified_text_diff(git.commit_message_file(base.revision), new, min(context, 10_000)), {}, ""
            if f.path.startswith("/"):
                return [], {}, ""
            base_rev = base.revision if base is not None else (view.parents[0] if view.parents else f"{view.revision}^")
            paths = [f.path] + ([f.old_path] if f.old_path and f.old_path != f.path else [])
            diff = git.diff(base_rev, view.revision, paths, context)
            rows = parse_unified(diff)
            return (rows, *highlight(git, highlighter, diff, rows, scheme))

        def done(result: tuple[list, dict[int, tuple[Line, Optional[Style]]], str]) -> None:
            rows, styles, problem = result
            self.diff_rows[key] = rows
            self.diff_styles[key] = styles
            self.check_merges([ch])  # the patch set may only now be in the local clone
            if problem and not self.syntax_warned:
                self.syntax_warned = True
                self.show_toast(problem, error=True, seconds=10)
            if self.main_key == key:
                self._render_diff(key)

        def failed(e: BaseException) -> None:
            if self.main_key == key:
                self.set_main(key, title, [[("Could not load the diff:", theme.BAD)]] + text_block(self.error_text(e), theme.BAD))

        self.submit(f"diff:{key}", work, done, failed, label="Loading diff")

    def _render_diff(self: "App", key: tuple) -> None:
        renderer = self.diff_renderer()
        if not renderer.builtin:
            self._render_external(key, renderer)
            return
        rows = self.diff_rows.get(key)
        if rows is None:
            return
        _, n, view_n, base_n, path, full, old_path = key
        left_path = old_path if (base_n and old_path) else path
        all_threads = [t for t in self.threads_cache.get(n, ([], False))[0] if not t.is_ci]
        threads = [t for t in all_threads if t.path == path]
        new_threads = [t for t in threads if t.patch_set == view_n and (t.side != "PARENT" or not base_n)]
        old_threads = [t for t in all_threads if base_n and t.path == left_path and t.patch_set == base_n and t.side != "PARENT"]
        drafts = [
            d
            for d in self.state.drafts_for(n)
            if (d.path == path and d.patch_set == view_n and (d.side != "PARENT" or not base_n))
            or (base_n and d.path == left_path and d.patch_set == base_n and d.side != "PARENT")
        ]
        notes: list[Line] = []
        others = sorted({t.patch_set for t in threads if t.patch_set not in (view_n, base_n)})
        if others:
            where = ", ".join(f"ps{k}" for k in others)
            notes.append([(f"This file also has comments on {where} (P to view another patch set).", theme.DIM)])
        lines, meta = render.diff_lines(
            path,
            rows,
            new_threads,
            self.main_width,
            old_path=left_path,
            threads_old=old_threads,
            drafts=drafts,
            new_ps=view_n,
            base_ps=base_n or None,
            notes=notes,
            styled=self.diff_styles.get(key),
        )
        title = self._diff_title(key)
        first = self.rendered_diff != key
        self.set_main(key, title, lines, wrap=False, meta=meta, cursor_mode=True)
        self.rendered_diff = key
        self.seen_ps[n] = max(self.seen_ps.get(n, 0), view_n)
        if self.pending_cursor and self.pending_cursor[0] == key:
            row = self.pending_cursor[1](meta)
            self.pending_cursor = None
            if row is not None:
                self.p_main.set_cursor(row)
                self.p_main.scroll_to(max(0, row - self.p_main.height // 3))
                return
        if self._apply_map(key, lines, meta):
            return
        if first:
            self.p_main.set_cursor(next((i for i, m in enumerate(meta) if isinstance(m, LineRef)), 0))

    def _diff_title(self: "App", key: tuple) -> str:
        _, n, view_n, base_n, path, full, _old_path = key
        ch = self.change(n)
        name = "Commit message" if path == COMMIT_MSG else path
        title = f"Diff · {name} · " + (f"ps{base_n}→ps{view_n}" if base_n else f"ps{view_n}")
        if ch is not None and ch.ps != view_n:
            title += " (old)"
        if full:
            title += " · full file"
        if ch is not None and self.state.is_reviewed(n, view_n, path):
            title += " · ✓ reviewed"
        return title

    def act_full_file(self: "App") -> None:
        self.full_file = not self.full_file
        self.show_toast("Showing whole files" if self.full_file else f"Showing {self.diff_context} lines of context")
        self._reshow_diff()

    def _reshow_diff(self: "App") -> None:
        if self.focus == "main" and self.main_diff:
            n, _, path = self.main_diff
            ch = self.change(n)
            f = next((x for x in (self.files_for_view(ch) or []) if x.path == path), None) if ch else None
            self.show_diff(f or FileInfo(path))
        else:
            self.update_main()

    def _rerender_diff(self: "App") -> None:
        if self.main_key and self.main_key[0] == "diff" and self.main_diff:
            self._render_diff(self.main_key)

    # ------------------------------------------------------------------ diff renderers (delta, difftastic, …)

    def diff_renderer(self: "App") -> DiffRenderer:
        """What diffs are shown with: the chosen renderer, or the review view while commenting."""
        return BUILTIN if self.review_override else self.renderers[self.renderer_idx]

    def showing_rendered_diff(self: "App") -> bool:
        key = self.main_key
        return bool(key) and key[0] == "diff" and not self.diff_renderer().builtin

    def in_review_view(self: "App", fn: Callable[[], None]) -> Callable[[], None]:
        """Wrap a diff action that needs the review view (comments, hunks): on a renderer's output it
        switches to the review view at the same line instead."""

        def run() -> None:
            if self.showing_rendered_diff():
                self.open_review_view()
            else:
                fn()

        return run

    def open_review_view(self: "App") -> None:
        self.pending_map = (self.main_key, self._cursor_probe())
        self.review_override = True
        self.show_toast(f"Review view: comment here · | goes back to {self.renderers[self.renderer_idx].label}")
        self._reshow_diff()

    def act_cycle_renderer(self: "App") -> None:
        if len(self.renderers) < 2:
            self.show_toast("No diff tools configured: add [[diff_renderers]] to config.toml (see the README)", error=True)
            return
        key = self.main_key
        shown = bool(key) and key[0] == "diff"
        probe = self._cursor_probe() if shown else None
        if self.review_override:
            self.review_override = False
        else:
            self.renderer_idx = (self.renderer_idx + 1) % len(self.renderers)
        r = self.diff_renderer()
        self.show_toast(f"Diff view: {r.label}" + (" (shows and adds comments)" if r.builtin else ""))
        if shown and probe is not None:
            self.pending_map = (key, probe)
            self._reshow_diff()

    def _cursor_probe(self: "App") -> tuple[str, float]:
        """The text under the cursor and its relative position, to find the same line in another view."""
        rows, _ = self.p_main.rows()
        if not rows:
            return "", 0.0
        i = max(0, min(self.p_main.cursor, len(rows) - 1))
        text = render.diff_code_text(rows[i]) if isinstance(self.p_main.cursor_meta(), LineRef) else plain(rows[i])
        return text, i / max(1, len(rows) - 1)

    def _apply_map(self: "App", key: tuple, lines: list[Line], meta: Optional[list[Any]]) -> bool:
        pending = self.pending_map
        if not pending or pending[0] != key:
            return False
        self.pending_map = None
        row = render.map_row(pending[1], lines, meta)
        self.p_main.set_cursor(row)
        self.p_main.scroll_to(max(0, row - self.p_main.height // 3))
        return True

    def _patchset(self: "App", ch: Change, number: int) -> Optional[PatchSet]:
        if ch.current is not None and ch.current.number == number:
            return ch.current
        return ch.patch_set(number)

    def _render_external(self: "App", key: tuple, r: DiffRenderer) -> None:
        """Show the diff ``key`` as rendered by ``r`` (rendering it in the background first)."""
        n, view_n = key[1], key[2]
        view_id = ("rendered", key, self.renderer_idx)
        ckey = (key, self.renderer_idx, self.main_width, self.color_scheme)
        title = f"{self._diff_title(key)} · {r.label}"
        out = self.rendered_out.get(ckey)
        if out is None:
            if self._start_render(ckey, key, r) and self.rendered_diff != view_id:
                self.set_main(key, title, [[(f"Rendering the diff with {r.label}…", theme.DIM)]], wrap=False)
            return
        self.rendered_out.move_to_end(ckey)
        lines: list[Line] = []
        if isinstance(out, BaseException):
            lines.append([(f"Could not render the diff with {r.label}:", theme.BAD)])
            lines += text_block(self.error_text(out), theme.BAD)
            lines += [[], [("Press | for another diff view, or fix diff_renderers in config.toml.", theme.DIM)]]
        else:
            status, body = out
            note = self._rendered_note(key)
            if note:
                lines += [note, []]
            if status:
                lines.append([(f"{r.label} failed (exit status {status}):", theme.BAD)])
                lines += body
                lines += [[], [("Press | for another diff view, or fix diff_renderers in config.toml.", theme.DIM)]]
            else:
                lines += body or [[("(no differences)", theme.DIM)]]
                self.seen_ps[n] = max(self.seen_ps.get(n, 0), view_n)
        first = self.rendered_diff != view_id
        self.set_main(key, title, lines, wrap=False, meta=[None] * len(lines), cursor_mode=True)
        self.rendered_diff = view_id
        if not self._apply_map(key, lines, None) and first:
            self.p_main.set_cursor(0)
            self.p_main.scroll_to(0)

    def _rendered_note(self: "App", key: tuple) -> Optional[Line]:
        """A reminder of the comments a renderer cannot show."""
        _, n, view_n, base_n, path, _full, old_path = key
        paths = {path, old_path}
        sets = {view_n, base_n} - {0}
        threads = [
            t for t in self.threads_cache.get(n, ([], False))[0] if not t.is_ci and t.path in paths and t.patch_set in sets
        ]
        drafts = [d for d in self.state.drafts_for(n) if d.path in paths and d.patch_set in sets]
        if not threads and not drafts:
            return None
        parts = []
        if threads:
            unresolved = sum(1 for t in threads if t.unresolved)
            parts.append(f"{len(threads)} comment thread{'s' if len(threads) != 1 else ''}")
            if unresolved:
                parts[-1] += f" ({unresolved} unresolved)"
        if drafts:
            parts.append(f"{len(drafts)} draft{'s' if len(drafts) != 1 else ''}")
        return [(" and ".join(parts) + " on this file", theme.WARN), ("  ·  c or | shows them in the review view", theme.DIM)]

    def _start_render(self: "App", ckey: tuple, key: tuple, r: DiffRenderer) -> bool:
        _, n, view_n, base_n, path, full, old_path = key
        ch = self.change(n)
        view = self._patchset(ch, view_n) if ch else None
        base = self._patchset(ch, base_n) if ch and base_n else None
        if ch is None or view is None or (base_n and base is None):
            return False
        git = self.be.git
        if not git.available:
            note = "Diffs need a local clone of the repository (start lazygerrit inside it or pass --repo)."
            self.set_main(key, self._diff_title(key), [[(note, theme.DIM)]])
            return False
        width, scheme, height = ckey[2], ckey[3], self.p_main.height
        context = FULL_FILE_CONTEXT if full else self.diff_context
        message: Optional[str] = None
        if (
            path == COMMIT_MSG
            and base is None
            and view.number == ch.ps
            and ch.commit_message
            and not git.has_commit(view.revision)
        ):
            message = render.synth_commit_msg(ch, view, self.change_subjects())

        def work() -> tuple[int, list[Line]]:
            if path == COMMIT_MSG:
                new = message
                if new is None:
                    git.ensure_commit(view.revision, view.ref)
                    new = git.commit_message_file(view.revision)
                old = ""
                if base is not None:
                    git.ensure_commit(base.revision, base.ref)
                    old = git.commit_message_file(base.revision)
                status, text = render_with(
                    git, r, texts=("COMMIT_MSG", old, new), width=width, height=height, context=context, color_scheme=scheme
                )
            elif path.startswith("/"):
                return 0, []
            else:
                git.ensure_commit(view.revision, view.ref)
                if base is not None:
                    git.ensure_commit(base.revision, base.ref)
                base_rev = base.revision if base is not None else (view.parents[0] if view.parents else f"{view.revision}^")
                paths = [path] + ([old_path] if old_path and old_path != path else [])
                status, text = render_with(
                    git,
                    r,
                    base=base_rev,
                    rev=view.revision,
                    paths=paths,
                    width=width,
                    height=height,
                    context=context,
                    color_scheme=scheme,
                )
            return status, ansi_to_lines(text)

        def store(value: Any) -> None:
            self.rendered_out[ckey] = value
            self.rendered_out.move_to_end(ckey)
            while len(self.rendered_out) > 20:
                self.rendered_out.popitem(last=False)
            if self.main_key == key:
                self._render_diff(key)

        self.submit(f"render:{ckey}", work, store, store, label=f"Rendering diff ({r.label})")
        return True

    # ------------------------------------------------------------------ diff navigation

    def _diff_meta(self: "App") -> list[Any]:
        return self.p_main.meta or []

    def diff_jump_hunk(self: "App", d: int) -> None:
        meta = self._diff_meta()
        hunks = [i for i, m in enumerate(meta) if isinstance(m, tuple) and m and m[0] == "hunk"]
        if not hunks:
            return
        cur = self.p_main.cursor
        if d > 0:
            nxt = next((h for h in hunks if h > cur), None)
        else:
            start = max((h for h in hunks if h <= cur), default=None)
            nxt = max((h for h in hunks if start is not None and h < start), default=None)
            if nxt is None and start is not None and cur > start + 1:
                nxt = start
        if nxt is None:
            self.show_toast("No more hunks in this file — . / , go to the next / previous file")
            return
        self.p_main.set_cursor(min(len(meta) - 1, nxt + 1))

    def diff_jump_comment(self: "App", d: int) -> None:
        meta = self._diff_meta()
        starts = []
        prev: Any = None
        for i, m in enumerate(meta):
            if isinstance(m, tuple) and m and m[0] in ("thread", "draft"):
                if not (isinstance(prev, tuple) and prev and prev[0] == m[0] and prev[1] is m[1]):
                    starts.append(i)
            prev = m
        cur = self.p_main.cursor
        target = next((s for s in starts if s > cur), None) if d > 0 else max((s for s in starts if s < cur), default=None)
        if target is None:
            self.show_toast("No more comments in this file")
            return
        self.p_main.set_cursor(target)

    def diff_next_file(self: "App", d: int) -> None:
        p = self.p_files
        if p.tab != 0:
            p.tab = 0
            self.refresh_ctx_panels()
        files = p.view()
        if not files:
            return
        if self.main_diff:
            idx = next((i for i, f in enumerate(files) if isinstance(f, FileInfo) and f.path == self.main_diff[2]), p.selected)
        else:
            idx = p.selected
        new = idx + d
        if not 0 <= new < len(files):
            self.show_toast("That was the last file" if d > 0 else "This is the first file")
            return
        p.anchor = None
        p.goto(new)
        f = files[new]
        if isinstance(f, FileInfo):
            self.show_diff(f)

    def act_diff_range(self: "App") -> None:
        p = self.p_main
        if not p.cursor_mode:
            return
        p.anchor = None if p.anchor is not None else p.cursor
        if p.anchor is not None:
            self.show_toast("Select lines with j/k, then c to comment on the range")

    # ------------------------------------------------------------------ drafts

    def _drafts_changed(self: "App") -> None:
        self.draft_counts = self.state.draft_counts()
        self.refresh_ctx_panels()
        self.update_main(data_changed=True)

    def _save_draft(self: "App", draft: Draft) -> None:
        try:
            self.state.add_draft(draft)
        except OSError as e:
            self.error(f"Could not save the draft: {e}")
            return
        self._drafts_changed()
        total = len(self.state.drafts_for(draft.change))
        self.show_toast(f"Draft saved ({total} on {draft.change}) — publish with p")

    def current_draft(self: "App") -> Optional[Draft]:
        if self.focus == "main":
            meta = self.p_main.cursor_meta() if self.p_main.cursor_mode else None
            if isinstance(meta, tuple) and meta and meta[0] == "draft":
                return meta[1]
            if self.main_key and self.main_key[0] == "draft":
                return self.state.draft(self.main_key[1])
            return None
        item = self.p_comments.current() if self.focus == "comments" and self.p_comments.tab == 0 else None
        return item if isinstance(item, Draft) else None

    def _snap_to_code(self: "App") -> None:
        """Move the cursor from a header/blank row to the nearest code line (below first)."""
        p = self.p_main
        meta = p.meta or []
        if not meta or isinstance(p.cursor_meta(), LineRef) or p.anchor is not None:
            return
        below = next((i for i in range(p.cursor, len(meta)) if isinstance(meta[i], LineRef)), None)
        above = next((i for i in range(p.cursor, -1, -1) if isinstance(meta[i], LineRef)), None)
        target = below if below is not None else above
        if target is not None:
            p.set_cursor(target)

    def _line_selection(self: "App") -> Optional[tuple[LineRef, Optional[dict]]]:
        p = self.p_main
        meta = p.cursor_meta()
        if not isinstance(meta, LineRef):
            return None
        if p.anchor is None or not p.meta:
            return meta, None
        lo, hi = sorted((p.anchor, p.cursor))
        refs = [
            m
            for m in p.meta[lo : hi + 1]
            if isinstance(m, LineRef) and m.side == meta.side and m.patch_set == meta.patch_set and m.path == meta.path
        ]
        if len(refs) <= 1:
            return meta, None
        start = min(refs, key=lambda r: r.line)
        end = max(refs, key=lambda r: r.line)
        rng = {"start_line": start.line, "start_character": 0, "end_line": end.line, "end_character": end.length}
        return end, rng

    def act_comment_line(self: "App") -> None:
        draft = self.current_draft()
        if draft is not None:
            self.edit_draft(draft)
            return
        meta = self.p_main.cursor_meta()
        if isinstance(meta, tuple) and meta and meta[0] == "thread":
            self.act_reply()
            return
        self._snap_to_code()
        sel = self._line_selection()
        if sel is None or not self.main_diff:
            self.error("There is no line of code here to comment on.", "Cannot comment")
            return
        ref, rng = sel
        n = self.main_diff[0]
        key = self.main_key
        if key and key[0] == "diff" and key[3]:
            own = self.own_files.get((n, ref.patch_set))
            if own is not None and ref.path not in own and ref.path != COMMIT_MSG:
                self.error(
                    f"Patch set {ref.patch_set} itself does not change {ref.path} (it only differs because of a "
                    "rebase), so Gerrit would reject a comment on it.\n\nPress B and compare against the parent "
                    "commit to comment on this file.",
                    "Cannot comment here",
                )
                return
        where = ref.path if ref.path != COMMIT_MSG else "Commit message"
        where += f":{rng['start_line']}-{rng['end_line']}" if rng else f":{ref.line}"
        if ref.side == "PARENT":
            where += " (base)"

        def save(text: str) -> None:
            if not text.strip():
                return
            self.p_main.anchor = None
            self._save_draft(
                Draft(
                    change=n,
                    patch_set=ref.patch_set,
                    path=ref.path,
                    line=ref.line,
                    side=ref.side,
                    range=rng,
                    message=text.strip("\n"),
                    unresolved=True,
                )
            )

        self.prompt(
            f"Draft comment on {where} (ps{ref.patch_set})",
            save,
            multiline=True,
            hint="Saved as a draft (unresolved). Publish all your drafts together, with a vote and message, using p.",
            on_editor=lambda cur: self.edit_text([f"Comment on {where} in {n},{ref.patch_set}"], cur, save),
        )

    def _busy_publishing(self: "App", n: int) -> bool:
        if n in self.publishing:
            self.error(f"Your review of {n} is being published right now. Try again when it has finished.", "Publishing")
            return True
        return False

    def edit_draft(self: "App", d: Draft) -> None:
        if self._busy_publishing(d.change):
            return

        def save(text: str) -> None:
            if not text.strip():
                self.state.delete_drafts([d.id])
                self._drafts_changed()
                self.show_toast("Empty draft deleted")
                return
            self.state.update_draft(d.id, message=text.strip("\n"))
            self._drafts_changed()
            self.show_toast("Draft updated")

        self.prompt(
            f"Edit draft on {d.location} (ps{d.patch_set})",
            save,
            initial=d.message,
            multiline=True,
            allow_empty=True,
            hint="An empty message deletes the draft.",
            on_editor=lambda cur: self.edit_text([f"Draft on {d.location} in {d.change},{d.patch_set}"], cur, save),
        )

    def act_delete_draft(self: "App") -> None:
        d = self.current_draft()
        if d is None:
            self.show_toast("Select a draft (✎) to delete it", error=True)
            return
        if self._busy_publishing(d.change):
            return

        def delete() -> None:
            self.state.delete_drafts([d.id])
            self._drafts_changed()
            self.show_toast("Draft deleted")

        self.confirm("Delete draft", f"Delete your draft on {d.location}?\n\n{d.message}", delete)

    def act_reply(self: "App", preset: Optional[str] = None, resolve: Optional[bool] = None) -> None:
        t = self.current_thread()
        ch = self.ctx_change()
        if t is None or ch is None:
            hint = "\n\nMove the cursor onto a comment first." if self.focus == "main" else ""
            self.error("Select a comment thread to reply to." + hint, "Cannot reply")
            return
        n = ch.number
        if resolve is None:
            unresolved = bool(t.unresolved) if t.unresolved is not None else False
        else:
            unresolved = not resolve

        def save(text: str) -> None:
            if not text.strip():
                return
            self._save_draft(
                Draft(
                    change=n,
                    patch_set=t.patch_set,
                    path=t.path,
                    line=0 if t.is_patchset_level else t.line,
                    side=t.side,
                    range=t.range,
                    message=text.strip("\n"),
                    unresolved=unresolved,
                    in_reply_to=t.last.id,
                    reply_to_text=first_line(t.last.message),
                )
            )

        if preset:
            save(preset)
            return
        exact = self.threads_cache.get(n, ([], False))[1]
        hint = "Saved as a draft; publish with p."
        if not exact:
            hint += " Without REST access the reply is posted at the same line (not threaded)."
        state = {True: " (resolves)", False: " (unresolves)", None: ""}[resolve]
        self.prompt(
            f"Reply to {t.location}{state}",
            save,
            multiline=True,
            hint=hint,
            on_editor=lambda cur: self.edit_text(
                [f"Reply to {t.location} on {n},{t.patch_set}", *("> " + ln for ln in t.last.message.splitlines()[:10])],
                cur,
                save,
            ),
        )

    def act_reply_toggle(self: "App") -> None:
        d = self.current_draft()
        if d is not None:
            if self._busy_publishing(d.change):
                return
            self.state.update_draft(d.id, unresolved=not d.unresolved)
            self._drafts_changed()
            self.show_toast("Draft marked " + ("unresolved" if not d.unresolved else "resolved"))
            return
        t = self.current_thread()
        if t is None:
            self.show_toast("Select a comment thread", error=True)
            return
        if t.unresolved is None:
            self.error("The resolution state is unknown without REST access (add an HTTP password to ~/.netrc).")
            return
        self.act_reply(resolve=bool(t.unresolved))

    # ------------------------------------------------------------------ publishing

    def _my_votes(self: "App", ch: Change) -> dict[str, int]:
        if not ch.current or not self.cfg.user:
            return {}
        return {a.label: a.value for a in ch.current.approvals if a.by.username == self.cfg.user}

    def act_publish(
        self: "App",
        focus_message: bool = False,
        message: str = "",
        votes: Optional[dict[str, int]] = None,
        change: Optional[Change] = None,
    ) -> None:
        if change is None:
            ts = self.need_targets(single=True)
            if not ts:
                return
            change = ts[0]
        # Reopened after a failure or the editor: use the freshest data for that same change.
        ch = self.change(change.number) or change
        n = ch.number
        if self._busy_publishing(n):
            return
        shown_ps = self.seen_ps.get(n, ch.ps)
        drafts = self.state.drafts_for(n)
        mine = self._my_votes(ch)
        labels = [(name, values, mine.get(name, 0)) for name, values in (CODE_REVIEW, SECONDARY_REVIEW)]
        summary: list[Line] = []
        warning = ""
        if shown_ps != ch.ps:
            warning = (
                f"⚠ Votes will be refused: you looked at patch set {shown_ps}, but patch set {ch.ps} is current. "
                f"Open a diff of patch set {ch.ps} first."
            )
        if drafts:
            summary.append([(f"{len(drafts)} draft comment{'s' if len(drafts) != 1 else ''} will be published:", theme.HEADING)])
            for d in drafts[:10]:
                summary.append(
                    [("  ✎ ", theme.MAGIC), (f"ps{d.patch_set} {d.location}", theme.INFO), (f"  {first_line(d.message)}", PLAIN)]
                )
            if len(drafts) > 10:
                summary.append([(f"  … and {len(drafts) - 10} more", theme.DIM)])
            if any(d.patch_set != ch.ps for d in drafts):
                summary.append([("Drafts on older patch sets are published on those patch sets.", theme.DIM)])
        else:
            summary.append([("No drafts. Comment on lines in the diff view (c) to add some.", theme.DIM)])

        def submit(chosen_votes: dict[str, int], msg: str) -> None:
            self.do_publish(ch, chosen_votes, msg, mine, shown_ps)

        def editor(chosen_votes: dict[str, int], msg: str) -> None:
            self.edit_text(
                [f"Review message for {n},{ch.ps}: {ch.subject}"],
                msg,
                lambda text: self.act_publish(focus_message=True, message=text, votes=chosen_votes, change=ch),
            )

        self.push(
            ReviewPopup(
                f"Review {n},{ch.ps}: {ch.subject}",
                labels,
                submit,
                summary=summary,
                chosen=votes,
                warning=warning,
                message=message,
                focus_message=focus_message,
                on_editor=editor,
            )
        )

    def do_publish(
        self: "App", ch: Change, votes: dict[str, int], message: str, mine: dict[str, int], shown_ps: Optional[int] = None
    ) -> None:
        n = ch.number
        if self._busy_publishing(n):
            return
        drafts = self.state.drafts_for(n)
        changed = {k: v for k, v in votes.items() if v != mine.get(k, 0)}
        msg = message.strip()
        if not drafts and not changed and not msg:
            self.show_toast("Nothing to publish (no drafts, vote changes or message)", error=True)
            return
        published: list[str] = []

        def work() -> str:
            return ops.publish_review(self.be, ch, drafts, changed, msg or None, shown_ps=shown_ps or ch.ps, published=published)

        def after(_: Any) -> None:
            self.publishing.discard(n)
            self._forget_published(published)

        def failed(e: BaseException) -> None:
            self.publishing.discard(n)
            self._forget_published(e.published if isinstance(e, ops.PartialPublish) else [])
            self.act_publish(focus_message=bool(msg), message=message, votes=votes, change=ch)

        def go() -> None:
            self.publishing.add(n)
            self.run_op(f"Review {n}", work, refresh=[n], after=after, on_fail=failed)

        if any(v <= -2 for v in changed.values()):
            self.confirm("Publish review", f"Vote {', '.join(f'{k} {v:+d}' for k, v in changed.items())} on {n}?", go)
        else:
            go()

    def _forget_published(self: "App", ids: list[str]) -> None:
        if not ids:
            return
        if self.cfg.dry_run:
            self.show_toast("Dry run: nothing was sent, your drafts are kept")
            return
        self.state.delete_drafts(ids)
        self._drafts_changed()

    def act_comment(self: "App", editor: bool = False, unresolved: bool = False) -> None:
        ts = self.need_targets()
        if not ts:
            return
        if len(ts) == 1 and not unresolved:
            if editor:
                ch = ts[0]
                self.edit_text(
                    [f"Review message for {ch.number},{ch.ps}: {ch.subject}"],
                    "",
                    lambda text: self.act_publish(focus_message=True, message=text),
                )
            else:
                self.act_publish(focus_message=True)
            return
        self.comment_many(ts, editor, unresolved)

    def comment_many(self: "App", ts: list[Change], editor: bool, unresolved: bool) -> None:
        names = self.names(ts)
        header = [f"Comment on {c.number},{c.ps}: {c.subject}" for c in ts]

        def open_prompt(initial: str = "") -> None:
            self.prompt(
                f"Comment on {names}" + (" (unresolved)" if unresolved else ""),
                post,
                initial=initial,
                multiline=True,
                on_editor=lambda cur: self.edit_text(header, cur, post),
                hint="Posted right away as a patchset-level comment on every selected change.",
            )

        def post(text: str) -> None:
            text = text.strip("\n")
            if not text.strip():
                self.show_toast("Empty comment, nothing posted", error=True)
                return
            self.run_op(
                f"Comment on {names}",
                lambda: [ops.patchset_comment(self.be, t, text, unresolved) for t in ops.resolve(self.be, ts)],
                refresh=[c.number for c in ts],
                on_fail=lambda e: open_prompt(text),
            )

        if editor:
            self.edit_text(header, "", post)
        else:
            open_prompt()

    def publish_bar(self: "App") -> str:
        ts = self.target_changes()
        if len(ts) == 1:
            k = self.draft_counts.get(ts[0].number, 0)
            if k:
                return f"Publish {k} draft{'s' if k != 1 else ''}"
        return "Review"

    # ------------------------------------------------------------------ reviewed files

    def act_toggle_reviewed(self: "App", advance: bool = False) -> None:
        ch = self.ctx_change()
        if ch is None:
            return
        view = self.viewed_ps(ch)
        if view is None:
            return
        if self.focus == "main" and self.main_diff:
            path = self.main_diff[2]
        else:
            item = self.p_files.current()
            if not isinstance(item, FileInfo):
                return
            path = item.path
        now = self.state.toggle_reviewed(ch.number, view.number, path)
        self.refresh_ctx_panels()
        files = self.files_for_view(ch) or []
        done = len(self.state.reviewed_paths(ch.number, view.number) & {f.path for f in files})
        self.show_toast(f"{'Reviewed' if now else 'Not reviewed'}: {path}  ({done}/{len(files)} files reviewed)")
        if self.focus == "main" and advance and now:
            self.diff_next_file(1)
        elif self.focus == "main" and self.main_key:
            self._render_diff(self.main_key)

    # ------------------------------------------------------------------ comments → code

    def act_comment_enter(self: "App") -> None:
        item = self.p_comments.current()
        if isinstance(item, Draft):
            if item.path == PATCHSET_LEVEL or (item.path.startswith("/") and item.path != COMMIT_MSG):
                self.act_focus_main()
                return
            target = item.id
            self.goto_location(
                item.patch_set,
                item.path,
                lambda meta: next(
                    (i for i, m in enumerate(meta) if isinstance(m, tuple) and m and m[0] == "draft" and m[1].id == target), None
                ),
            )
            return
        if isinstance(item, Thread):
            if item.is_patchset_level or (item.path.startswith("/") and item.path != COMMIT_MSG) or not self.be.git.available:
                self.act_focus_main()
                return
            want = thread_key(item)
            self.goto_location(
                item.patch_set,
                item.path,
                lambda meta: next(
                    (
                        i
                        for i, m in enumerate(meta)
                        if isinstance(m, tuple) and m and m[0] == "thread" and thread_key(m[1]) == want
                    ),
                    None,
                ),
            )
            return
        self.act_focus_main()

    def goto_location(self: "App", ps: int, path: str, finder: Callable[[list[Any]], Optional[int]]) -> None:
        ch = self.ctx_change()
        if ch is None:
            return
        n = ch.number
        if ps == ch.ps:
            self.view_ps.pop(n, None)
        else:
            self.view_ps[n] = ps
        self.base_ps.pop(n, None)
        self.p_files.tab = 0
        self.pending_goto = None
        self.refresh_ctx_panels()
        listed = self.files_for_view(ch)
        if listed is None:
            self.pending_goto = (n, ps, path, finder)
            self.focus = "main"
            self.review_override = True
            self.set_main(("goto", n, ps, path), "Diff", [[("Loading the files of the patch set…", theme.DIM)]])
            return
        files = listed
        idx = next((i for i, f in enumerate(files) if f.path == path), None)
        if idx is not None:
            self.p_files.goto(idx)
            f = files[idx]
        else:
            f = FileInfo(path)
        self.pending_cursor = (self.diff_key(ch, f), finder)
        self.focus = "main"
        self.review_override = True  # comments are shown in the review view
        self.show_diff(f)

    def resume_goto(self: "App") -> None:
        """Called whenever file lists change: finish a jump that was waiting for one."""
        if not self.pending_goto:
            return
        n, ps, path, finder = self.pending_goto
        ch = self.ctx_change()
        if ch is None or ch.number != n or self.main_key != ("goto", n, ps, path):
            self.pending_goto = None
            return
        if self.files_for_view(ch) is not None:
            self.pending_goto = None
            self.goto_location(ps, path, finder)

    def act_comments_c(self: "App") -> None:
        d = self.current_draft()
        if d is not None:
            self.edit_draft(d)
        else:
            self.act_comment()
