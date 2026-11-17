import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from lazygerrit.demo import gerrit_split
from lazygerrit.gerrit import CommandError, GerritSSH, Runner, format_vote, quote_arg
from lazygerrit.localgit import LocalGit


class RecordingRunner(Runner):
    def __init__(self, rc=0, out="", err="", **kw):
        super().__init__(**kw)
        self.calls = []
        self.rc, self.out, self.err = rc, out, err

    def _exec(self, args, input, cwd, env, timeout):
        self.calls.append((list(args), input))
        return self.rc, self.out, self.err


def server_argv(args):
    """What Gerrit sees: ssh joins argv with spaces, the server splits it again."""
    idx = args.index("gerrit")
    return gerrit_split(" ".join(args[idx + 1 :]))


class QuotingTest(unittest.TestCase):
    def test_arguments_survive_gerrit_splitting(self):
        tricky = [
            "recheck",
            "owner:self status:open -is:wip project:src",
            'it\'s a "quoted" message',
            "multi\nline\nmessage",
            "back\\slash",
            "(change:I1 OR change:I2)",
            "tab\there",
        ]
        for arg in tricky:
            with self.subTest(arg=arg):
                self.assertEqual(gerrit_split(quote_arg(arg)), [arg])


class GerritSSHTest(unittest.TestCase):
    def ssh(self, **kw):
        runner = RecordingRunner(**kw)
        return GerritSSH("gerrit.example.com", 29418, runner, user="me", multiplex=False), runner

    def test_comment_command_matches_rng(self):
        ssh, runner = self.ssh()
        ssh.review(703425, 1, message="recheck")
        args, stdin = runner.calls[0]
        self.assertEqual(args[:2], ["ssh", "-x"])
        self.assertIn("me@gerrit.example.com", args)
        self.assertEqual(server_argv(args), ["review", "703425,1", "-m", "recheck"])
        self.assertIsNone(stdin)

    def test_label_votes_format_sign(self):
        ssh, runner = self.ssh()
        ssh.review(1, 2, labels={"Code-Review": 2, "Secondary-Review": -1, "Verified": 0})
        self.assertEqual(
            server_argv(runner.calls[0][0]),
            ["review", "--label", "Code-Review=+2", "--label", "Secondary-Review=-1", "--label", "Verified=0", "1,2"],
        )
        self.assertEqual(format_vote(1), "+1")

    def test_json_review_goes_through_stdin(self):
        ssh, runner = self.ssh()
        body = {"comments": [["/PATCHSET_LEVEL", [{"message": "prio_high reason: hotfix"}]]]}
        ssh.review(5, 3, json_input=body)
        args, stdin = runner.calls[0]
        self.assertEqual(server_argv(args), ["review", "5,3", "--json"])
        self.assertEqual(json.loads(stdin), body)

    def test_topic_with_spaces_and_clearing(self):
        ssh, runner = self.ssh()
        ssh.set_topic(7, "my topic")
        ssh.set_topic(7, None)
        self.assertEqual(server_argv(runner.calls[0][0]), ["set-topic", "7", "--topic", "my topic"])
        self.assertEqual(server_argv(runner.calls[1][0]), ["set-topic", "7"])

    def test_set_reviewers(self):
        ssh, runner = self.ssh()
        ssh.set_reviewers(9, add=["a@example.com", "Some Group"], remove=["s1"])
        self.assertEqual(
            server_argv(runner.calls[0][0]),
            ["set-reviewers", "-a", "a@example.com", "-a", "Some Group", "-r", "s1", "9"],
        )

    def test_query_appends_limit_and_parses_rows(self):
        out = '{"number": 1, "subject": "x"}\n{"type":"stats","rowCount":1}\n'
        ssh, runner = self.ssh(out=out)
        rows, stats = ssh.query("owner:self", ["--current-patch-set"], limit=5)
        self.assertEqual(server_argv(runner.calls[0][0]), ["query", "--format=JSON", "--current-patch-set", "owner:self limit:5"])
        self.assertEqual(rows, [{"number": 1, "subject": "x"}])
        self.assertEqual(stats["rowCount"], 1)

    def test_dry_run_skips_writes_but_logs_them(self):
        runner = RecordingRunner(dry_run=True)
        events = []
        runner.subscribe(events.append)
        ssh = GerritSSH("h", 29418, runner, multiplex=False)
        ssh.review(1, 1, message="recheck")
        self.assertEqual(runner.calls, [])
        self.assertEqual([(e.status, e.display) for e in events], [("dry-run", "gerrit review 1,1 -m recheck")])
        ssh.query("owner:self")
        self.assertEqual(len(runner.calls), 1)

    def test_failures_raise_with_stderr(self):
        ssh, _ = self.ssh(rc=1, err="error: Change is already up to date.\n")
        with self.assertRaises(CommandError) as cm:
            ssh.review(1, 1, rebase=True)
        self.assertIn("already up to date", cm.exception.message)

    def test_multiplexing_options(self):
        ssh = GerritSSH("h", 29418, Runner(), multiplex=True)
        opts = " ".join(ssh.base_command())
        self.assertIn("ControlMaster=auto", opts)
        self.assertIn("BatchMode=yes", opts)
        self.assertIn("ControlPath=", ssh.git_ssh_command())
        # One master per lazygerrit process: quitting one instance must not cut off another.
        self.assertIn(f"lazygerrit-{os.getpid()}-", ssh.control_path)


class MetaCommentsTest(unittest.TestCase):
    """Comments read from a change's meta ref (NoteDb), fetched from a real (local) git remote."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="lazygerrit-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.server, self.repo = tmp / "server.git", tmp / "repo"
        subprocess.run(["git", "init", "-q", "--bare", str(self.server)], check=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", str(self.server)], check=True)
        self.git = LocalGit(self.repo, Runner())
        self.addCleanup(self.git.close)

    def write_meta(self, number, notes):
        """Store ``notes`` (path in the notes tree -> blob text) as the change's meta ref on the server."""
        env = dict(os.environ, GIT_INDEX_FILE=str(self.server / "meta-index"))
        env.update(GIT_AUTHOR_NAME="Gerrit", GIT_AUTHOR_EMAIL="g@example.com", GIT_COMMITTER_NAME="Gerrit")
        env.update(GIT_COMMITTER_EMAIL="g@example.com")

        def git(*args, input=None):
            cmd = ["git", f"--git-dir={self.server}", *args]
            return subprocess.run(cmd, input=input, capture_output=True, text=True, check=True, env=env).stdout.strip()

        for path, text in notes.items():
            git("update-index", "--add", "--cacheinfo", f"100644,{git('hash-object', '-w', '--stdin', input=text)},{path}")
        git("update-ref", f"refs/changes/{number % 100:02d}/{number}/meta", git("commit-tree", git("write-tree"), "-m", "Update"))

    def test_reads_every_note(self):
        a = {"key": {"uuid": "u1", "filename": "a.py", "patchSetId": 1}, "lineNbr": 3, "message": "Fix ✓\n{this}\n"}
        b = {"key": {"uuid": "u2", "filename": "b.py", "patchSetId": 2}, "lineNbr": 1, "message": "Done", "parentUuid": "u0"}
        rev1, rev2 = "ab" + "1" * 38, "cd" + "2" * 38
        # Gerrit fans note paths out into directories once there are many of them
        self.write_meta(
            703424, {rev1: json.dumps({"comments": [a]}, indent=2), f"{rev2[:2]}/{rev2[2:]}": json.dumps({"comments": [b]})}
        )
        self.assertEqual(sorted(self.git.change_meta_comments(703424, str(self.server)), key=lambda c: c["key"]["uuid"]), [a, b])
        refs = subprocess.run(["git", "-C", str(self.repo), "for-each-ref"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(refs, "")  # fetched into a scratch repository, not the user's

    def test_unreadable_or_missing_notes_fail(self):
        self.write_meta(703425, {"ab" + "1" * 38: "Revision: ab11\nPatch-set: 1\n"})  # pre-JSON format
        with self.assertRaises(ValueError):
            self.git.change_meta_comments(703425, str(self.server))
        with self.assertRaises(CommandError):
            self.git.change_meta_comments(703426, str(self.server))


class RunnerTest(unittest.TestCase):
    def test_terminate_all_stops_running_commands(self):
        runner = Runner()
        results = []
        t = threading.Thread(target=lambda: results.append(runner.run(["sleep", "30"], check=False)))
        start = time.monotonic()
        t.start()
        time.sleep(0.3)
        runner.terminate_all()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - start, 5)
        self.assertNotEqual(results[0].returncode, 0)
        self.assertEqual(runner.run(["true"], check=False).returncode, 130)


if __name__ == "__main__":
    unittest.main()
