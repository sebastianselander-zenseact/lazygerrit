import json
import tempfile
import unittest
from pathlib import Path

from lazygerrit.drafts import Draft, StateStore


class StateStoreTest(unittest.TestCase):
    def test_drafts_and_reviewed_marks_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "state.json"
            store = StateStore(path)
            d1 = store.add_draft(Draft(change=1, patch_set=2, path="a.py", line=3, message="why?"))
            store.add_draft(Draft(change=1, patch_set=1, path="b.py", line=0, message="file comment", unresolved=False))
            store.add_draft(Draft(change=7, patch_set=1, path="/PATCHSET_LEVEL", message="hi"))
            self.assertTrue(store.toggle_reviewed(1, 2, "a.py"))

            again = StateStore(path)
            self.assertEqual([d.message for d in again.drafts_for(1)], ["why?", "file comment"])
            self.assertEqual(again.draft_counts(), {1: 2, 7: 1})
            self.assertTrue(again.is_reviewed(1, 2, "a.py"))
            self.assertFalse(again.is_reviewed(1, 1, "a.py"))

            again.update_draft(d1.id, message="why not?")
            again.delete_drafts([d for d in (x.id for x in again.drafts_for(7))])
            self.assertFalse(again.toggle_reviewed(1, 2, "a.py"))
            final = json.loads(path.read_text())
            self.assertEqual([d["message"] for d in final["drafts"]], ["why not?", "file comment"])
            self.assertEqual(final["reviewed"], {})

    def test_unreadable_state_is_backed_up_not_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json")
            store = StateStore(path)
            self.assertIn("Could not read", store.load_error)
            self.assertEqual(store.draft_counts(), {})
            store.add_draft(Draft(change=1, patch_set=1, path="a.py", line=1, message="new"))
            (backup,) = Path(tmp).glob("state.json.corrupt-*")
            self.assertEqual(backup.read_text(), "{not json")
            self.assertEqual([d.message for d in StateStore(path).drafts_for(1)], ["new"])

    def test_two_instances_do_not_lose_or_resurrect_each_others_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            a, b = StateStore(path), StateStore(path)
            d1 = a.add_draft(Draft(change=1, patch_set=1, path="a.py", line=1, message="from a"))
            b.add_draft(Draft(change=1, patch_set=1, path="a.py", line=2, message="from b"))
            a.update_draft(d1.id, message="from a, edited")
            self.assertEqual([d.message for d in StateStore(path).drafts_for(1)], ["from a, edited", "from b"])
            b.delete_drafts([d1.id])
            a.toggle_reviewed(1, 1, "a.py")
            self.assertEqual([d.message for d in StateStore(path).drafts_for(1)], ["from b"])
            self.assertEqual([d.message for d in a.drafts_for(1)], ["from b"])

    def test_comment_input(self):
        d = Draft(
            change=1,
            patch_set=2,
            path="a.py",
            line=9,
            side="PARENT",
            message="m",
            in_reply_to="c1",
            range={"start_line": 7, "start_character": 0, "end_line": 9, "end_character": 4},
        )
        self.assertEqual(
            d.comment_input(),
            {
                "message": "m",
                "unresolved": True,
                "line": 9,
                "side": "PARENT",
                "in_reply_to": "c1",
                "range": {"start_line": 7, "start_character": 0, "end_line": 9, "end_character": 4},
            },
        )
        self.assertNotIn("line", Draft(change=1, patch_set=1, path="/PATCHSET_LEVEL", line=0).comment_input())


if __name__ == "__main__":
    unittest.main()
