import dataclasses
import shlex
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from lazygerrit.demo import _FILES_BASE, ME, GerritFailure, _commit, _run, cleanup_demo, create_demo
from lazygerrit.diff import LineRef
from lazygerrit.drafts import Draft, StateStore
from lazygerrit.renderers import DiffRenderer
from lazygerrit.ui.app import App
from lazygerrit.ui.screen import Key, Paste, VirtualCanvas
from lazygerrit.ui.text import plain
from lazygerrit.ui.widgets import ConfirmPopup, MenuPopup, MessagePopup, PromptPopup, ReviewPopup


class AppTestCase(unittest.TestCase):
    dry_run = False
    renderers: list = []

    def setUp(self):
        self.be, self.cfg, self.server, self.tmp = create_demo(latency=0, dry_run=self.dry_run)
        self.cfg.refresh_interval = 0
        self.cfg.zuul_refresh_interval = 0
        self.cfg.diff_renderers = [dataclasses.replace(r) for r in self.renderers]
        self.app = App(self.be, self.cfg, demo=True)
        self.app.canvas = VirtualCanvas(160, 45)
        self.app.start()
        self.settle()

    def tearDown(self):
        self.app.shutdown()
        cleanup_demo(self.tmp)

    def settle(self):
        for _ in range(3):
            self.app.wait_idle()
            time.sleep(0.02)
        self.app.wait_idle()

    def press(self, *keys):
        for k in keys:
            self.app.handle(Key(k))
            self.settle()

    def type(self, text):
        for ch in text:
            self.app.handle(Key("<space>" if ch == " " else ch))
        self.settle()

    def screen(self):
        self.app.render()
        return self.app.canvas.text()

    def writes(self):
        return [c for c in self.server.calls if c[0] not in ("query", "version")]

    def select_change(self, number):
        p = self.app.p_changes
        idx = next(i for i, c in enumerate(p.view()) if c.number == number)
        self.press(*(["k"] * p.selected), *(["j"] * idx))
        self.assertEqual(p.current().number, number)

    def open_diff(self, number, file_index=1):
        self.select_change(number)
        self.press("4", *(["j"] * file_index), "<enter>")
        self.assertEqual(self.app.focus, "main")

    def upload_new_patch_set(self, number):
        ch = self.server.changes[number]
        last = ch["_patchsets"][-1]
        n = last["number"] + 1
        ch["_patchsets"].append(dict(last, number=n, approvals=[], ref=f"refs/changes/{number % 100:02d}/{number}/{n}"))


class LayoutAndNavigationTest(AppTestCase):
    def test_startup_shows_lazygit_layout(self):
        s = self.screen()
        for title in (
            "[1]─Status",
            "[2]─Outgoing",
            "[3]─Local stack",
            "[4]─Files - CI",
            "[5]─Comments - Messages",
            "Command log",
        ):
            self.assertIn(title, s)
        self.assertIn("100103", s)
        self.assertIn("Move declarations out one scope", s)
        self.assertIn("Recheck/CI: r", s.splitlines()[-1])
        self.assertIn("✓ demo → review/100103", s)

    def test_tabs_filter_and_panels(self):
        self.press("]")
        self.assertEqual([c.number for c in self.app.p_changes.view()], [100200, 100201])
        self.press("[", "/")
        self.type("raise")
        self.press("<enter>")
        self.assertEqual([c.number for c in self.app.p_changes.view()], [100102])
        self.press("<esc>")
        self.assertEqual(len(self.app.p_changes.view()), 3)
        self.press("3")
        self.assertEqual(self.app.focus, "stack")
        self.assertIn("not on Gerrit", self.screen())

    def test_keybindings_menu(self):
        self.press("?")
        self.assertIsInstance(self.app.popups[-1], MenuPopup)
        self.assertIn("Keybindings", self.screen())
        self.press("<esc>")
        self.assertEqual(self.app.popups, [])

    def test_change_details_in_main_view(self):
        self.select_change(100102)
        s = self.screen()
        self.assertIn("CI · check · failed", s)
        self.assertIn("spa3_open_loop_hil_smoke_test", s)
        self.assertIn("Relation chain", s)


class ActionsTest(AppTestCase):
    def test_recheck_from_ci_menu(self):
        self.select_change(100102)
        self.press("r", "r")
        self.assertEqual(self.writes(), [["review", "100102,1", "-m", "recheck"]])
        self.assertIn("gerrit review 100102,1 -m recheck", self.screen())

    def test_range_select_votes_on_all(self):
        self.select_change(100103)
        self.press("v", "j", "a", "a")
        self.assertIsInstance(self.app.popups[-1], ConfirmPopup)
        self.press("<enter>")
        self.assertEqual(
            sorted(self.writes()),
            [["review", "--label", "Code-Review=+2", "100102,1"], ["review", "--label", "Code-Review=+2", "100103,1"]],
        )

    def test_patchset_comment_with_paste(self):
        self.select_change(100101)
        self.press("c")
        self.app.handle(Paste("Thanks!\nLooks good to me"))
        self.press("<enter>")
        c = self.server.changes[100101]["_comments"][-1]
        self.assertEqual((c["path"], c["message"], c["patch_set"]), ("/PATCHSET_LEVEL", "Thanks!\nLooks good to me", 2))

    def test_comment_on_diff_line(self):
        self.select_change(100101)
        self.press("4", "j", "<enter>")
        self.assertEqual(self.app.focus, "main")
        meta = self.app.p_main.meta
        target = next(i for i, m in enumerate(meta) if isinstance(m, LineRef) and m.line == 7 and m.side is None)
        self.press(*(["j"] * (target - self.app.p_main.cursor)))
        self.press("c")
        self.type("why log here")
        self.press("<enter>")
        self.assertEqual(self.writes(), [])
        self.assertIn("✎ draft", self.screen())
        self.press("p", "<enter>")
        self.assertEqual(len(self.writes()), 1)
        c = self.server.changes[100101]["_comments"][-1]
        self.assertEqual(
            (c["path"], c["line"], c["message"], c["unresolved"]), ("stringent/rapid/crop.py", 7, "why log here", True)
        )
        self.assertEqual(self.app.state.drafts_for(100101), [])

    def test_reply_done_in_comments_panel(self):
        self.select_change(100101)
        self.press("5")
        t = self.app.p_comments.current()
        self.assertTrue(t.unresolved)
        self.press("d", "p", "<enter>")
        c = self.server.changes[100101]["_comments"][-1]
        self.assertEqual((c["message"], c["in_reply_to"], c["unresolved"]), ("Done", t.last.id, False))

    def test_topic_prompt_is_prefilled(self):
        self.select_change(100101)
        self.press("t")
        prompt = self.app.popups[-1]
        self.assertIsInstance(prompt, PromptPopup)
        self.assertEqual(prompt.text, "dlt-postprocessing")
        self.press("<c-u>")
        self.type("new topic")
        self.press("<enter>")
        self.assertEqual(self.writes(), [["set-topic", "100101", "--topic", "new topic"]])

    def test_run_selected_failed_job_from_ci_tab(self):
        self.select_change(100102)
        self.press("4", "]")
        p = self.app.p_files
        idx = next(i for i, it in enumerate(p.view()) if getattr(it, "name", "").endswith("hil_smoke_test"))
        self.press(*(["j"] * (idx - p.selected)))
        self.press("r", "J", "<enter>")
        c = self.server.changes[100102]["_comments"][-1]
        self.assertEqual(c["message"], "run-selected:\ndemo@spa3_open_loop_hil_smoke_test")

    def test_copy_menu(self):
        copied = []
        with mock.patch("lazygerrit.ui.app.copy_to_clipboard", lambda text: copied.append(text) or "test"):
            self.select_change(100102)
            self.press("y", "l")
        self.assertEqual(copied, ["100102: Raise exception if cropping fails | https://gerrit.example.com/c/demo/+/100102"])

    def test_push_from_stack(self):
        self.press("3", "P", "P", "<enter>")
        popup = self.app.popups[-1]
        self.assertIsInstance(popup, MessagePopup)
        self.assertIn("remote: SUCCESS", popup.message)


class ReviewRegressionTest(AppTestCase):
    def test_actions_target_the_current_patch_set(self):
        self.select_change(100102)
        self.upload_new_patch_set(100102)
        self.press("r", "r")
        self.assertEqual(self.writes(), [["review", "100102,2", "-m", "recheck"]])

    def test_vote_refuses_a_patch_set_that_was_not_shown(self):
        self.select_change(100102)
        self.upload_new_patch_set(100102)
        self.press("a", "a")
        self.assertEqual(self.writes(), [])
        self.assertIsInstance(self.app.popups[-1], MessagePopup)
        self.assertIn("new patch set (2)", self.app.popups[-1].message)

    def test_range_rebase_runs_bottom_first(self):
        self.select_change(100103)
        self.press("v", "j", "j", "b", "b")
        rebases = [c[1] for c in self.writes() if "--rebase" in c]
        self.assertEqual(rebases, ["100101,2", "100102,1", "100103,1"])

    def test_failed_comment_keeps_the_draft(self):
        self.select_change(100101)
        with mock.patch.object(self.server, "review", side_effect=GerritFailure("fatal: connection lost")):
            self.press("c")
            self.type("important feedback")
            self.press("<enter>")
        self.assertIsInstance(self.app.popups[-1], MessagePopup)
        self.assertIsInstance(self.app.popups[-2], ReviewPopup)
        self.assertEqual(self.app.popups[-2].message, "important feedback")

    def test_connection_errors_are_split_into_lines(self):
        app = App(self.be, self.cfg, demo=True)
        app.canvas = VirtualCanvas(160, 45)
        failure = GerritFailure("ssh: connect to host gerrit.example.com port 29418: Connection timed out")
        with mock.patch.object(self.server, "query", side_effect=failure):
            app.start()
            for _ in range(3):
                app.wait_idle()
        spans = [t for ln in app.p_main.lines for t, _ in ln]
        self.assertTrue(any("Connection timed out" in t for t in spans))
        self.assertFalse(any("\n" in t for t in spans))


class CodeReviewTest(AppTestCase):
    def cursor_to(self, line, side=None):
        meta = self.app.p_main.meta
        target = next(i for i, m in enumerate(meta) if isinstance(m, LineRef) and m.line == line and m.side == side)
        delta = target - self.app.p_main.cursor
        self.press(*(["j"] * delta if delta > 0 else ["k"] * -delta))

    def test_range_comment(self):
        self.open_diff(100101)
        self.cursor_to(7)
        self.press("v", "j", "j", "c")
        self.type("extract a helper")
        self.press("<enter>")
        (d,) = self.app.state.drafts_for(100101)
        self.assertEqual((d.line, d.range["start_line"], d.range["end_line"]), (9, 7, 9))
        self.assertEqual(d.range["end_character"], len("        data = fh.read()"))
        self.press("p", "<enter>")
        c = self.server.changes[100101]["_comments"][-1]
        self.assertEqual((c["line"], c["range"]["start_line"]), (9, 7))

    def test_review_dialog_publishes_drafts_vote_and_message_at_once(self):
        self.open_diff(100101)
        self.cursor_to(8)
        self.press("c")
        self.type("nit")
        self.press("<enter>", "p")
        dialog = self.app.popups[-1]
        self.assertIsInstance(dialog, ReviewPopup)
        self.assertIn("1 draft comment will be published", self.screen())
        self.press("<right>", "<tab>", "<tab>")
        self.type("lgtm")
        self.press("<enter>")
        (review,) = [c for c in self.writes() if c[0] == "review"]
        self.assertEqual(review[1:], ["100101,2", "--json"])
        comments = self.server.changes[100101]["_comments"]
        self.assertEqual(sorted(c["message"] for c in comments[-2:]), ["lgtm", "nit"])
        votes = [
            (a["type"], a["value"])
            for a in self.server.changes[100101]["_patchsets"][1]["approvals"]
            if a["by"]["username"] == "s0000001"
        ]
        self.assertEqual(votes, [("Code-Review", "1")])
        self.assertEqual(self.app.state.drafts_for(100101), [])

    def test_typing_in_the_review_dialog_never_changes_votes(self):
        self.select_change(100101)
        self.press("p")
        self.type("lgtm -")
        self.assertEqual(self.app.popups[-1].values, {"Code-Review": 0, "Secondary-Review": 0})
        self.assertEqual(self.app.popups[-1].message, "lgtm -")
        self.press("<esc>", "p")
        self.type("-2 +1")
        self.assertEqual(self.app.popups[-1].values, {"Code-Review": 0, "Secondary-Review": 0})
        self.assertEqual(self.app.popups[-1].message, "-2 +1")

    def test_failed_publish_reopens_the_dialog_of_that_change_and_blocks_double_publish(self):
        self.select_change(100101)
        gate = threading.Event()

        def slow_failure(*args, **kwargs):
            gate.wait(10)
            raise GerritFailure("fatal: connection lost")

        with mock.patch.object(self.server, "review", side_effect=slow_failure) as review:
            self.press("p")
            self.type("lgtm")
            self.app.handle(Key("<enter>"))
            self.app.handle(Key("p"))
            self.assertIn("being published", self.app.popups[-1].message)
            self.app.handle(Key("<esc>"))
            self.app.handle(Key("k"))
            gate.set()
            self.settle()
        self.assertEqual(review.call_count, 1)
        self.assertNotEqual(self.app.p_changes.current().number, 100101)
        self.assertIsInstance(self.app.popups[-1], MessagePopup)
        dialog = self.app.popups[-2]
        self.assertIsInstance(dialog, ReviewPopup)
        self.assertIn("Review 100101,", dialog.title)
        self.assertEqual(dialog.message, "lgtm")

    def test_vote_is_refused_when_a_new_patch_set_arrived_while_reading(self):
        self.open_diff(100101)
        self.upload_new_patch_set(100101)
        self.press("R")
        self.assertEqual(self.app.change(100101).ps, 3)
        self.press("p")
        self.assertIn("Votes will be refused", self.screen())
        self.press("<right>", "<enter>")
        self.assertEqual(self.writes(), [])
        self.assertIn("you looked at patch set 2", self.app.popups[-1].message)
        dialog = self.app.popups[-2]
        self.assertIsInstance(dialog, ReviewPopup)
        self.assertEqual(dialog.values["Code-Review"], 1)
        self.assertNotIn("your vote", self.screen())

    def test_quick_vote_needs_a_diff_of_the_newest_patch_set(self):
        self.open_diff(100101)
        self.upload_new_patch_set(100101)
        self.press("R", "2", "a", "a")
        self.assertEqual(self.writes(), [])
        self.assertIn("newest diff you opened is of patch set 2", self.app.popups[-1].message)
        self.press("<esc>", "4", "j", "<enter>")
        self.assertEqual(self.app.main_key[2], 3)
        self.press("2", "a", "a")
        self.assertEqual(self.writes(), [["review", "--label", "Code-Review=+2", "100101,3"]])

    def test_switching_patch_set_in_the_diff_view(self):
        self.open_diff(100101)
        self.assertEqual(self.app.main_key[2], 2)
        self.press("P", "1")
        self.assertEqual(self.app.main_key[2], 1)
        self.assertIn("Use a context manager here.", self.screen())

    def add_rebased_change(self):
        """Change 100300: ps2 edits spax.py and crop.py and was rebased onto an upstream edit of doc/rapid.md."""
        repo, now = self.cfg.repo, int(time.time())
        _run(repo, "checkout", "-q", "--detach", "origin/master")
        try:
            ps1_files = dict(_FILES_BASE)
            ps1_files["stringent/rapid/spax.py"] = ps1_files["stringent/rapid/spax.py"].replace("100", "200")
            ps1 = _commit(repo, ps1_files, "Tune spax", now - 600)
            _run(repo, "checkout", "-q", "--detach", "origin/master")
            upstream = dict(_FILES_BASE, **{"doc/rapid.md": "# Rapid\n\nOpen loop tooling, now documented.\n"})
            _commit(repo, upstream, "Upstream docs", now - 500)
            ps2_files = dict(upstream)
            ps2_files["stringent/rapid/spax.py"] = ps2_files["stringent/rapid/spax.py"].replace("100", "256")
            ps2_files["stringent/rapid/crop.py"] = ps2_files["stringent/rapid/crop.py"].replace(
                "open(path).read()", "open(path, 'rb').read()"
            )
            ps2 = _commit(repo, ps2_files, "Tune spax", now - 400)
        finally:
            _run(repo, "checkout", "-q", "review/100103")
        self.server.add_change(100300, "Tune spax", ME, [ps1, ps2], hours_ago=0.1)
        self.press("R")

    def test_comparing_patch_sets_leaves_out_rebase_only_files(self):
        self.add_rebased_change()
        self.select_change(100300)
        self.press("4", "B", "1")
        ch = self.app.change(100300)
        paths = [f.path for f in self.app.files_for_view(ch)]
        self.assertEqual(paths, ["/COMMIT_MSG", "stringent/rapid/crop.py", "stringent/rapid/spax.py"])

        # crop.py is only changed by ps2: Gerrit rejects comments on ps1's side of it.
        self.press("j", "<enter>")
        meta = self.app.p_main.meta
        old = next(i for i, m in enumerate(meta) if isinstance(m, LineRef) and m.patch_set == 1 and m.line == 7)
        self.press(*(["j"] * (old - self.app.p_main.cursor)))
        self.press("c")
        self.assertIsInstance(self.app.popups[-1], MessagePopup)
        self.assertIn("does not change stringent/rapid/crop.py", self.app.popups[-1].message)
        self.press("<esc>", "j", "c")
        self.assertIsInstance(self.app.popups[-1], PromptPopup)
        self.assertIn("(ps2)", self.app.popups[-1].title)

    def test_comment_on_an_older_patch_set(self):
        self.select_change(100101)
        self.press("4", "P", "1")
        self.assertIn("100101,1 (old)", self.screen())
        self.press("j", "<enter>")
        self.assertIn("Use a context manager here.", self.screen())
        self.cursor_to(8)
        self.press("c")
        self.type("same here")
        self.press("<enter>", "p", "<enter>")
        c = self.server.changes[100101]["_comments"][-1]
        self.assertEqual((c["message"], c["patch_set"], c["line"]), ("same here", 1, 8))

    def test_compare_against_an_older_patch_set(self):
        self.select_change(100101)
        self.press("4", "B", "1", "j", "<enter>")
        text = self.screen()
        self.assertIn("ps1→ps2", text)
        self.assertIn("+def crop_dlt(path):", text)
        self.assertNotIn('+    LOG.info("Cropping %s", path)', text)
        self.cursor_to(10, None)
        meta = self.app.p_main.cursor_meta()
        self.assertEqual((meta.patch_set, meta.side), (2, None))

    def test_enter_on_a_comment_jumps_to_the_code(self):
        self.select_change(100101)
        self.press("5")
        old = next(i for i, t in enumerate(self.app.p_comments.view()) if t.patch_set == 1)
        self.press(*(["j"] * old), "<enter>")
        self.assertEqual(self.app.focus, "main")
        kind, thread = self.app.p_main.cursor_meta()
        self.assertEqual((kind, thread.patch_set, thread.line), ("thread", 1, 7))

    def test_hunk_and_file_navigation(self):
        self.open_diff(100101, file_index=0)
        self.press(".")
        self.assertIn("crop.py", self.app.main_key[4])
        self.press("]")
        meta = self.app.p_main.cursor_meta()
        self.assertIsInstance(meta, LineRef)
        self.press(",")
        self.assertEqual(self.app.main_key[4], "/COMMIT_MSG")

    def test_reviewed_marks(self):
        self.open_diff(100101)
        self.press("<space>")
        self.assertTrue(self.app.state.is_reviewed(100101, 2, "stringent/rapid/crop.py"))
        self.press("<esc>")
        row = next(ln for ln in self.screen().splitlines() if "crop.py" in ln and "+7 -1" in ln)
        self.assertIn("✓M", row)


class BackgroundTest(AppTestCase):
    def test_background_refresh_pauses_while_idle(self):
        self.cfg.refresh_interval = 1
        self.cfg.idle_pause = 60
        with mock.patch.object(self.app, "load_tab") as load_tab:
            self.app.last_auto_refresh = time.monotonic() - 100
            self.app.last_input = time.monotonic() - 120
            self.app.tick()
            load_tab.assert_not_called()
            self.app.handle(Key("<f12>"))
            self.app.tick()
            load_tab.assert_called_once()

    def test_drafts_written_by_another_instance_show_up_on_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.app.state = StateStore(path)
            StateStore(path).add_draft(Draft(change=100102, patch_set=1, path="/PATCHSET_LEVEL", message="hi"))
            self.press("R")
            self.assertEqual(self.app.draft_counts, {100102: 1})


MARKER = shlex.join([sys.executable, "-c", "import sys\nfor line in sys.stdin: print('» ' + line.rstrip('\\n'))"])


class DiffRendererTest(AppTestCase):
    renderers = [DiffRenderer(command=MARKER, name="marker", color_arg="never")]

    def main_text(self):
        return [plain(ln) for ln in self.app.p_main.lines]

    def cursor_to_text(self, text):
        target = next(i for i, t in enumerate(self.main_text()) if text in t)
        delta = target - self.app.p_main.cursor
        self.press(*(["j"] * delta if delta > 0 else ["k"] * -delta))

    def test_diffs_show_the_renderer_and_switch_views_at_the_same_line(self):
        self.open_diff(100101)
        self.assertTrue(self.app.p_main.title.endswith("· marker"))
        text = self.main_text()
        self.assertIn('» +    LOG.info("Cropping %s", path)', text)
        self.assertTrue(any(t.startswith("1 comment thread") and "c or | shows them" in t for t in text))
        self.assertEqual(self.app.seen_ps[100101], 2)
        self.cursor_to_text("» +        data = fh.read()")
        self.press("|")
        self.assertNotIn("marker", self.app.p_main.title)
        meta = self.app.p_main.cursor_meta()
        self.assertEqual((meta.line, meta.side), (9, None))
        self.press("k", "|")
        self.assertTrue(self.app.p_main.title.endswith("· marker"))
        self.assertEqual(self.main_text()[self.app.p_main.cursor], "» +    with open(path) as fh:")

    def test_comment_keys_open_the_review_view_first(self):
        self.open_diff(100101)
        self.cursor_to_text("» +        data = fh.read()")
        self.press("c")
        self.assertEqual(self.app.popups, [])
        self.assertNotIn("marker", self.app.p_main.title)
        self.assertEqual(self.app.p_main.cursor_meta().line, 9)
        self.press("c")
        self.assertIsInstance(self.app.popups[-1], PromptPopup)
        self.assertIn("crop.py:9", self.app.popups[-1].title)
        self.press("<esc>", "<esc>")
        self.assertEqual(self.app.focus, "files")
        self.assertTrue(self.app.p_main.title.endswith("· marker"))

    def test_commit_message_and_jumping_to_comments(self):
        self.open_diff(100101, file_index=0)
        self.assertIn("» +Add DLT log cropping", self.main_text())
        self.press("<esc>", "5")
        idx = next(i for i, t in enumerate(self.app.p_comments.view()) if t.patch_set == 2 and t.path.endswith("crop.py"))
        self.press(*(["j"] * idx), "<enter>")
        kind, thread = self.app.p_main.cursor_meta()
        self.assertEqual((kind, thread.line), ("thread", 14))
        self.assertNotIn("marker", self.app.p_main.title)


class BrokenRendererTest(AppTestCase):
    renderers = [DiffRenderer(command="no-such-diff-tool-xyz --paging=never", name="broken"), DiffRenderer(type="builtin")]

    def test_a_failing_renderer_explains_itself(self):
        self.open_diff(100101)
        text = "\n".join(plain(ln) for ln in self.app.p_main.lines)
        self.assertIn("broken failed (exit status 127):", text)
        self.assertIn("not found", text)
        self.assertNotIn(100101, self.app.seen_ps)
        self.press("|")
        self.assertIsInstance(self.app.p_main.cursor_meta(), LineRef)
        self.assertEqual(self.app.seen_ps[100101], 2)

    def test_refresh_retries_a_failed_renderer(self):
        self.open_diff(100101)
        self.app.renderers[0].command = "cat"
        self.press("R")
        self.assertIn("+def crop_dlt(path):", [plain(ln) for ln in self.app.p_main.lines])


class DryRunAppTest(AppTestCase):
    dry_run = True

    def test_dry_run_publish_keeps_drafts(self):
        self.select_change(100101)
        self.press("4", "j", "<enter>", "c")
        self.type("kept")
        self.press("<enter>", "p", "<enter>")
        self.assertEqual(self.writes(), [])
        self.assertEqual([d.message for d in self.app.state.drafts_for(100101)], ["kept"])

    def test_dry_run_logs_instead_of_sending(self):
        self.select_change(100102)
        self.press("r", "r")
        self.assertEqual(self.writes(), [])
        s = self.screen()
        self.assertIn("(dry run)", s)
        self.assertIn("DRY RUN", s.splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
