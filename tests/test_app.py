import time
import unittest
from unittest import mock

from lazygerrit.demo import GerritFailure, cleanup_demo, create_demo
from lazygerrit.diff import LineRef
from lazygerrit.ui.app import App
from lazygerrit.ui.screen import Key, Paste, VirtualCanvas
from lazygerrit.ui.widgets import ConfirmPopup, MenuPopup, MessagePopup, PromptPopup


class AppTestCase(unittest.TestCase):
    dry_run = False

    def setUp(self):
        self.be, self.cfg, self.server, self.tmp = create_demo(latency=0, dry_run=self.dry_run)
        self.cfg.refresh_interval = 0
        self.cfg.zuul_refresh_interval = 0
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
        c = self.server.changes[100101]["_comments"][-1]
        self.assertEqual(
            (c["path"], c["line"], c["message"], c["unresolved"]), ("stringent/rapid/crop.py", 7, "why log here", True)
        )

    def test_reply_done_in_comments_panel(self):
        self.select_change(100101)
        self.press("5")
        t = self.app.p_comments.current()
        self.assertTrue(t.unresolved)
        self.press("d")
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
    def upload_new_patch_set(self, number):
        ch = self.server.changes[number]
        last = ch["_patchsets"][-1]
        n = last["number"] + 1
        ch["_patchsets"].append(dict(last, number=n, approvals=[], ref=f"refs/changes/{number % 100:02d}/{number}/{n}"))

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
        self.assertIsInstance(self.app.popups[-2], PromptPopup)
        self.assertEqual(self.app.popups[-2].text, "important feedback")

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


class DryRunAppTest(AppTestCase):
    dry_run = True

    def test_dry_run_logs_instead_of_sending(self):
        self.select_change(100102)
        self.press("r", "r")
        self.assertEqual(self.writes(), [])
        s = self.screen()
        self.assertIn("(dry run)", s)
        self.assertIn("DRY RUN", s.splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
