import unittest

from lazygerrit.models import Account, ChangeMessage, LocalCommit
from lazygerrit.parsing import (
    QueryError,
    ci_state,
    filter_threads,
    is_ci_comment,
    parse_change,
    parse_query_output,
    parse_zuul_live,
    parse_zuul_report,
    threads_from_change,
    threads_from_rest,
    zuul_merge_failure,
)

ZUUL = {"name": "svc00140", "email": "svc00140@example.com", "username": "svc00140"}
ME = {"name": "Alex Doe", "email": "alex@example.com", "username": "s0000001"}

REPORT = """Patch Set 1: Verified-1

Check failed, check why, rebase will invalidate smart-recheck. Rerun by a comment "recheck"
https://zuul.example.com/t/src/buildset/99d19e0b15ca4360b8a9aaf53997d316

- src@build_application_baseline https://zuul.example.com/t/src/build/ade7 : SUCCESS in 7m 51s
- src@spa2u_p519_open_loop_hil_smoke_test https://zuul.example.com/t/src/build/fe02 : FAILURE in 1h 27m 04s
- src@optional_job https://zuul.example.com/t/src/build/0001 : FAILURE in 3m 02s (non-voting)
- src@lint : SKIPPED
Skipped 203 jobs"""


def change_json(**over):
    d = {
        "project": "src",
        "branch": "master",
        "id": "I4b18e3c7503f3f130dbcf62e6ac6224fbc717d6b",
        "number": 703424,
        "subject": "Move CropDltLogs trace",
        "owner": ME,
        "url": "https://gerrit.example.com/c/src/+/703424",
        "hashtags": [],
        "createdOn": 1794408488,
        "lastUpdated": 1794417890,
        "open": True,
        "status": "NEW",
        "currentPatchSet": {
            "number": 2,
            "revision": "ac822e415cda786d49432c38842c8d389e2ae45d",
            "parents": ["8630df4969bb3a630bcbe7fcbaf7b6d6f7acb01d"],
            "ref": "refs/changes/24/703424/2",
            "uploader": ME,
            "author": ME,
            "createdOn": 1794408488,
            "kind": "REWORK",
            "approvals": [
                {"type": "Verified", "description": "Verified", "value": "-1", "grantedOn": 1794417890, "by": ZUUL},
                {"type": "Code-Review", "value": "1", "by": {"name": "R", "username": "s2"}},
                {"type": "Code-Review", "value": "-2", "by": {"name": "S", "username": "s3"}},
            ],
            "files": [
                {"file": "/COMMIT_MSG", "type": "ADDED", "insertions": 14, "deletions": 0},
                {"file": "a/b.py", "type": "MODIFIED", "insertions": 34, "deletions": -34},
            ],
            "sizeInsertions": 48,
            "sizeDeletions": -34,
        },
        "submitRecords": [
            {
                "status": "NOT_READY",
                "labels": [{"label": "Code-Review", "status": "REJECT"}, {"label": "Verified", "status": "NEED"}],
            }
        ],
        "dependsOn": [
            {"id": "I75", "number": 703347, "revision": "8630df", "ref": "refs/changes/47/703347/1", "isCurrentPatchSet": True}
        ],
        "comments": [
            {"timestamp": 100, "reviewer": ME, "message": "Uploaded patch set 1."},
            {
                "timestamp": 200,
                "reviewer": ZUUL,
                "message": "Patch Set 2:\n\nStarting check jobs: https://zuul.example.com/t/src/status/change/703424,2",
            },
            {"timestamp": 300, "reviewer": ZUUL, "message": REPORT.replace("Patch Set 1", "Patch Set 2")},
        ],
    }
    d.update(over)
    return d


class ChangeParsingTest(unittest.TestCase):
    def test_parse_change_fields(self):
        ch = parse_change(change_json())
        self.assertEqual((ch.number, ch.ps, ch.ref_str), (703424, 2, "703424,2"))
        self.assertEqual(ch.current.files[1].deletions, 34)
        self.assertEqual(ch.current.size_deletions, 34)
        self.assertEqual(ch.depends_on[0].number, 703347)
        self.assertEqual(ch.messages[1].patch_set, 2)
        self.assertEqual(ch.labels(), ["Code-Review", "Verified"])

    def test_label_summary_prefers_rejection(self):
        ch = parse_change(change_json())
        self.assertEqual(ch.label_value("Code-Review"), -2)
        self.assertEqual(ch.label_value("Verified"), -1)
        self.assertIsNone(ch.label_value("Secondary-Review"))

    def test_query_output_errors(self):
        with self.assertRaises(QueryError):
            parse_query_output('{"type":"error","message":"bad query"}\n')

    def test_local_commit_sync_state(self):
        ch = parse_change(change_json(patchSets=[{"number": 1, "revision": "old"}, change_json()["currentPatchSet"]]))
        c = LocalCommit(sha=ch.current.revision, short="ac822e4", subject="", author="", timestamp=0, change=ch)
        self.assertEqual(c.sync_state, "synced")
        c.sha = "old"
        self.assertEqual(c.sync_state, "outdated")
        c.sha = "something else"
        self.assertEqual(c.sync_state, "modified")
        c.change = None
        self.assertEqual(c.sync_state, "not-pushed")


class ZuulParsingTest(unittest.TestCase):
    def test_report(self):
        msg = ChangeMessage(300, Account("svc00140", "", "svc00140"), REPORT, 1)
        rep = parse_zuul_report(msg)
        self.assertEqual(rep.pipeline, "check")
        self.assertEqual(rep.votes, "Verified-1")
        self.assertEqual(rep.skipped, 203)
        self.assertTrue(rep.buildset_url.endswith("/buildset/99d19e0b15ca4360b8a9aaf53997d316"))
        self.assertEqual([j.short_name for j in rep.failed_jobs], ["spa2u_p519_open_loop_hil_smoke_test", "optional_job"])
        self.assertEqual(rep.jobs[1].duration, "1h 27m 04s")
        self.assertFalse(rep.jobs[2].voting)
        self.assertEqual(rep.jobs[3].state, "skipped")
        self.assertFalse(rep.succeeded)

    def test_non_voting_failure_still_succeeds(self):
        text = "Patch Set 1: Verified+1\n\nBuild succeeded (check pipeline).\n\n- src@a https://z/b : SUCCESS in 1m\n- src@b https://z/c : FAILURE in 1m (non-voting)"
        rep = parse_zuul_report(ChangeMessage(1, Account(), text, 1))
        self.assertTrue(rep.succeeded)

    def test_ci_state_running_after_start(self):
        ch = parse_change(change_json())
        self.assertEqual(ci_state(ch)[0], "failure")
        msgs = change_json()["comments"] + [
            {"timestamp": 400, "reviewer": ME, "message": "Patch Set 2:\n\nrecheck"},
            {"timestamp": 410, "reviewer": ZUUL, "message": "Patch Set 2:\n\nStarting check jobs: https://zuul/x"},
        ]
        self.assertEqual(ci_state(parse_change(change_json(comments=msgs)))[0], "running")

    def test_merge_failure_is_zuuls_latest_word_on_the_current_patch_set(self):
        failed = (
            "Patch Set 2:\n\nMerge Failed.\n\nThis change or one of its cross-repo dependencies was unable to be "
            "automatically merged with the current state of its repository.\nWarning:\n  Error merging src for 703347,4"
        )
        msgs = change_json()["comments"] + [
            {"timestamp": 400, "reviewer": ME, "message": "Patch Set 2:\n\nrecheck"},
            {"timestamp": 410, "reviewer": ZUUL, "message": failed},
        ]
        msg, change = zuul_merge_failure(parse_change(change_json(comments=msgs)))
        self.assertEqual((msg.timestamp, change), (410, "703347,4"))
        later = msgs + [{"timestamp": 500, "reviewer": ZUUL, "message": "Patch Set 2:\n\nStarting check jobs: https://zuul/x"}]
        self.assertIsNone(zuul_merge_failure(parse_change(change_json(comments=later))))
        old_ps = [dict(m, message=m["message"].replace("Patch Set 2", "Patch Set 1")) for m in msgs]
        self.assertIsNone(zuul_merge_failure(parse_change(change_json(comments=old_ps))))

    def test_live_status(self):
        items = [
            {
                "jobs": [
                    {
                        "name": "src@a",
                        "result": None,
                        "elapsed_time": 30000,
                        "estimated_time": 60,
                        "queued": False,
                        "pipeline": "check",
                        "url": "stream/u1?logfile=console.log",
                    },
                    {
                        "name": "src@b",
                        "result": None,
                        "elapsed_time": None,
                        "queued": True,
                        "pipeline": "check",
                        "waiting_status": "executor",
                    },
                    {"name": "src@c", "result": "SKIPPED", "pipeline": "check"},
                ]
            }
        ]
        jobs = parse_zuul_live(items, "https://zuul.example.com", "src")
        self.assertEqual([j.state for j in jobs], ["running", "queued", "skipped"])
        self.assertAlmostEqual(jobs[0].progress, 0.5)
        self.assertEqual(jobs[0].stream_url, "https://zuul.example.com/t/src/stream/u1?logfile=console.log")


class ThreadTest(unittest.TestCase):
    def test_rest_threads_follow_latest_comment(self):
        data = {
            "a.py": [
                {
                    "id": "1",
                    "author": ME,
                    "message": "Fix this",
                    "unresolved": True,
                    "updated": "2026-01-01 10:00:00.000000000",
                    "line": 3,
                    "patch_set": 1,
                },
                {
                    "id": "2",
                    "in_reply_to": "1",
                    "author": ME,
                    "message": "Done",
                    "unresolved": False,
                    "updated": "2026-01-01 11:00:00.000000000",
                    "line": 3,
                    "patch_set": 1,
                },
                {
                    "id": "3",
                    "author": ME,
                    "message": "Another",
                    "unresolved": True,
                    "updated": "2026-01-01 09:00:00.000000000",
                    "line": 9,
                    "patch_set": 2,
                },
                {
                    "id": "4",
                    "in_reply_to": "2",
                    "author": ME,
                    "message": "Not really",
                    "unresolved": True,
                    "updated": "2026-01-01 12:00:00.000000000",
                    "line": 3,
                    "patch_set": 1,
                },
            ],
            "/PATCHSET_LEVEL": [
                {
                    "id": "5",
                    "author": ME,
                    "message": "recheck",
                    "unresolved": False,
                    "updated": "2026-01-01 08:00:00.000000000",
                    "patch_set": 2,
                },
            ],
        }
        threads = threads_from_rest(data)
        by_root = {t.root.id: t for t in threads}
        self.assertEqual([c.id for c in by_root["1"].comments], ["1", "2", "4"])
        self.assertTrue(by_root["1"].unresolved)
        self.assertTrue(by_root["5"].is_ci)
        self.assertEqual(threads[0].root.id, "1")  # unresolved, most recently updated first

    def test_filter_like_rng(self):
        data = {
            "a.py": [
                {"id": "1", "author": ME, "message": "nit", "unresolved": False, "updated": "1", "line": 1, "patch_set": 1},
                {"id": "2", "author": ME, "message": "bug", "unresolved": True, "updated": "2", "line": 2, "patch_set": 1},
            ],
            "/PATCHSET_LEVEL": [
                {"id": "3", "author": ME, "message": "run-selected:\nsrc@a", "unresolved": False, "updated": "3", "patch_set": 1},
                {"id": "4", "author": ME, "message": "Please split this", "unresolved": True, "updated": "4", "patch_set": 1},
                {
                    "id": "5",
                    "in_reply_to": "4",
                    "author": ME,
                    "message": "recheck",
                    "unresolved": True,
                    "updated": "5",
                    "patch_set": 1,
                },
            ],
        }
        threads = threads_from_rest(data)
        visible = filter_threads(threads, show_resolved=False, show_ci=False)
        self.assertEqual(sorted(t.root.id for t in visible), ["2", "4"])
        mixed = next(t for t in visible if t.root.id == "4")
        self.assertEqual([c.message for c in mixed.comments], ["Please split this"])
        self.assertEqual(len(filter_threads(threads, show_resolved=True, show_ci=True)), 4)

    def test_ssh_threads_group_by_location(self):
        ps = change_json()["currentPatchSet"]
        ps["comments"] = [
            {"file": "/COMMIT_MSG", "line": 13, "reviewer": ME, "message": "Jira issue?"},
            {"file": "/COMMIT_MSG", "line": 13, "reviewer": ME, "message": "Done"},
            {"file": "/PATCHSET_LEVEL", "line": 0, "reviewer": ME, "message": "recheck"},
            {"file": "/PATCHSET_LEVEL", "line": 0, "reviewer": ME, "message": "LGTM"},
        ]
        threads = threads_from_change(parse_change(change_json(currentPatchSet=ps)))
        self.assertEqual(
            [(t.path, len(t.comments), t.is_ci) for t in threads],
            [
                ("/PATCHSET_LEVEL", 1, True),
                ("/PATCHSET_LEVEL", 1, False),
                ("/COMMIT_MSG", 2, False),
            ],
        )
        self.assertTrue(all(t.unresolved is None for t in threads))

    def test_ci_comment_detection(self):
        self.assertTrue(is_ci_comment("/PATCHSET_LEVEL", " recheck "))
        self.assertTrue(is_ci_comment("/PATCHSET_LEVEL", "run-selected:\nsrc@x"))
        self.assertFalse(is_ci_comment("a.py", "recheck"))
        self.assertFalse(is_ci_comment("/PATCHSET_LEVEL", "prio_high reason: x"))


if __name__ == "__main__":
    unittest.main()
