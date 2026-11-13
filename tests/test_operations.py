import unittest

from lazygerrit import operations as ops
from lazygerrit.backend import NeedsRest
from lazygerrit.demo import cleanup_demo, create_demo


class OperationsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.be, cls.cfg, cls.server, cls.tmp = create_demo(latency=0)

    @classmethod
    def tearDownClass(cls):
        cleanup_demo(cls.tmp)

    def setUp(self):
        self.server.calls.clear()

    def target(self, n):
        return ops.Target.of(self.be.light(n))

    def writes(self):
        return [c for c in self.server.calls if c[0] not in ("query", "version")]

    def test_tower_is_open_ancestors_top_first(self):
        tower = self.be.tower(self.be.light(100103))
        self.assertEqual([c.number for c in tower], [100103, 100102, 100101])
        self.assertEqual([c.number for c in self.be.tower(self.be.light(100103), border="100101")], [100103, 100102])

    def test_tower_recheck_runs_bottom_first(self):
        targets = ops.tower_targets(self.be, self.be.light(100103))
        ops.apply_bottom_up(targets, lambda t: ops.comment_command(self.be, t, "recheck"))
        self.assertEqual([c[1] for c in self.writes()], ["100101,2", "100102,1", "100103,1"])

    def test_link_tower_matches_rng(self):
        targets = ops.tower_targets(self.be, self.be.light(100102))
        text = ops.link_tower(self.cfg.change_url, targets)
        self.assertEqual(
            text.splitlines(),
            [
                "- 100101: Add DLT log cropping | https://gerrit.example.com/c/demo/+/100101",
                "- 100102: Raise exception if cropping fails | https://gerrit.example.com/c/demo/+/100102",
            ],
        )

    def test_prio_high_adds_hashtags_then_comments(self):
        t = self.target(100200)
        ops.prio_high(self.be, t, "release blocker", ["ci_prio_check", "ci_prio_flush_gate", "prio_high"])
        ch = self.be.details(100200)
        self.assertTrue({"ci_prio_check", "ci_prio_flush_gate", "prio_high"} <= set(ch.hashtags))
        review = self.writes()[-1]
        self.assertEqual(review[:3], ["review", "100200,1", "--json"])
        comments = self.server.changes[100200]["_comments"]
        self.assertEqual(comments[-1]["message"], "prio_high reason: release blocker")
        self.assertEqual(comments[-1]["path"], "/PATCHSET_LEVEL")

    def test_prio_high_without_rest_fails_before_commenting(self):
        rest, self.be.rest = self.be.rest, None
        try:
            with self.assertRaises(NeedsRest):
                ops.prio_high(self.be, self.target(100201), "x", ["prio_high"])
            self.assertEqual(self.writes(), [])
        finally:
            self.be.rest = rest

    def test_rebase_and_recheck_targets_new_patch_set(self):
        t = self.target(100201)
        self.assertEqual(ops.rebase(self.be, t, recheck=True), "Rebased and rechecked 100201")
        self.assertEqual([c[:2] for c in self.writes()], [["review", "100201,1"], ["review", "100201,2"]])
        self.assertEqual(self.writes()[1][2:], ["-m", "recheck"])
        again = ops.rebase(self.be, self.target(100201), recheck=True)
        self.assertIn("not required", again)

    def test_threaded_reply_resolves(self):
        ch = self.be.details(100200)
        threads, exact = self.be.threads(ch)
        self.assertTrue(exact)
        t = next(t for t in threads if t.path == "stringent/rapid/spax.py")
        self.assertTrue(t.unresolved)
        ops.reply(self.be, 100200, t, "Done", resolve=True)
        threads, _ = self.be.threads(self.be.details(100200))
        t2 = next(t for t in threads if t.path == "stringent/rapid/spax.py")
        self.assertEqual(len(t2.comments), 3)
        self.assertFalse(t2.unresolved)
        self.assertEqual(t2.last.in_reply_to, t.last.id)

    def test_inline_comment_on_base_side(self):
        ops.inline_comment(self.be, self.target(100110), "doc/scenario.md", 2, "typo", side="PARENT")
        c = self.server.changes[100110]["_comments"][-1]
        self.assertEqual((c["path"], c["line"], c["side"], c["unresolved"]), ("doc/scenario.md", 2, "PARENT", True))

    def test_wip_toggle(self):
        ops.set_wip(self.be, self.target(100110), False)
        self.assertFalse(self.be.light(100110).wip)
        ops.set_wip(self.be, self.target(100110), True)
        self.assertTrue(self.be.light(100110).wip)

    def test_run_selected_comment(self):
        ops.run_selected(self.be, self.target(100102), ["spa3_open_loop_hil_smoke_test"])
        c = self.server.changes[100102]["_comments"][-1]
        self.assertEqual(c["message"], "run-selected:\ndemo@spa3_open_loop_hil_smoke_test")

    def test_stack_mapping(self):
        base, commits = self.be.git.stack()
        self.assertEqual(base, "origin/master")
        mapping = self.be.by_change_ids([c.change_id for c in commits if c.change_id])
        for c in commits:
            c.change = mapping.get(c.change_id)
        self.assertEqual([c.sync_state for c in commits], ["not-pushed", "modified", "synced", "synced", "merged"])

    def test_synthesized_commit_msg_lines_up_with_git(self):
        from lazygerrit.ui.render import synth_commit_msg

        ch = self.be.details(100102)
        exact = self.be.git.commit_message_file(ch.current.revision).splitlines()
        synth = synth_commit_msg(ch, ch.current).splitlines()
        self.assertEqual(len(synth), len(exact))
        self.assertEqual(synth[5:], exact[5:])
        self.assertEqual([ln[:12] for ln in synth[:5]], [ln[:12] for ln in exact[:5]])


class DryRunTest(unittest.TestCase):
    def test_nothing_is_sent(self):
        be, cfg, server, tmp = create_demo(latency=0, dry_run=True)
        try:
            t = ops.Target.of(be.light(100102))
            ops.comment_command(be, t, "recheck")
            ops.vote(be, t, "Code-Review", 2)
            ops.hashtags(be, t, add=["x"])
            self.assertEqual([c for c in server.calls if c[0] != "query"], [])
            self.assertEqual(server.changes[100102]["hashtags"], [])
        finally:
            cleanup_demo(tmp)


if __name__ == "__main__":
    unittest.main()
