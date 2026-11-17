"""Local git repository integration (read-mostly; never touches FETCH_HEAD or the index)."""

from __future__ import annotations

import atexit
import os
import re
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Sequence

from .gerrit import CommandError, Runner, runtime_dir
from .models import BlameCommit, FileInfo, HistoryCommit, LocalCommit

HISTORY_FORMAT = "%x1e%H%x1f%h%x1f%an%x1f%at%x1f%B%x1f"
_BLAME_LINE_RE = re.compile(r"^([0-9a-f]{40}) \d+ (\d+)(?: \d+)?$")


def parse_blame(text: str) -> tuple[dict[int, str], dict[str, BlameCommit]]:
    """Parse ``git blame --porcelain``: ({line: commit}, {commit: details})."""
    lines: dict[int, str] = {}
    commits: dict[str, BlameCommit] = {}
    sha: Optional[str] = None
    info: dict[str, str] = {}
    for raw in text.split("\n"):
        if sha is None:
            m = _BLAME_LINE_RE.match(raw)
            if m:
                sha, info = m.group(1), {}
                lines[int(m.group(2))] = sha
            continue
        if raw.startswith("\t"):  # the line's content ends its entry
            if sha not in commits and "author" in info:
                time = info.get("author-time", "")
                mail = info.get("author-mail", "").strip("<>")
                commits[sha] = BlameCommit(sha, info["author"], mail, int(time) if time.isdigit() else 0, info.get("summary", ""))
            sha = None
            continue
        key, _, value = raw.partition(" ")
        info[key] = value
    return lines, commits


def line_ranges(lines: Sequence[int]) -> list[tuple[int, int]]:
    """[1, 2, 3, 7, 8] -> [(1, 3), (7, 8)]"""
    out: list[tuple[int, int]] = []
    for n in sorted(set(lines)):
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def parse_line_history(text: str) -> list[HistoryCommit]:
    """Parse ``git log -L … --format=HISTORY_FORMAT`` output."""
    out: list[HistoryCommit] = []
    for record in text.split("\x1e")[1:]:
        parts = record.split("\x1f", 5)
        if len(parts) < 6:
            continue
        sha, short, author, at, message, rest = parts
        diff: list[str] = []
        created = False
        for line in rest.split("\n"):
            if diff or line.startswith("@@"):
                diff.append(line)
            elif line.startswith("--- /dev/null"):
                created = True
        while diff and not diff[-1]:
            diff.pop()
        out.append(HistoryCommit(sha, short, author, int(at) if at.isdigit() else 0, message.strip("\n"), diff, created))
    return out


_FIELD = "\x1f"
_RECORD = "\x1e"


def find_change_id(message: str) -> Optional[str]:
    """Return the last Change-Id footer of a commit message (like Gerrit does)."""
    found = None
    for ln in message.splitlines():
        s = ln.strip()
        if s.startswith("Change-Id:"):
            value = s.split(":", 1)[1].strip()
            if value:
                found = value
    return found


class LocalGit:
    def __init__(
        self,
        repo: Optional[Path],
        runner: Runner,
        remote: str = "origin",
        branch: str = "master",
        git_ssh_command: Optional[str] = None,
    ):
        self.repo = repo
        self.runner = runner
        self.remote = remote
        self.branch = branch
        self.git_ssh_command = git_ssh_command
        self._env: Optional[dict] = None
        self._scratch: Optional[str] = None
        self._objects: Optional[str] = None
        self._email: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.repo is not None and (self.repo / ".git").exists()

    def env(self) -> dict:
        if self._env is None:
            env = dict(os.environ)
            env["GIT_OPTIONAL_LOCKS"] = "0"
            env["GIT_TERMINAL_PROMPT"] = "0"
            env.setdefault("GIT_PAGER", "cat")
            if self.git_ssh_command and "GIT_SSH_COMMAND" not in env and not self._has_ssh_config():
                env["GIT_SSH_COMMAND"] = self.git_ssh_command
            self._env = env
        return self._env

    def _has_ssh_config(self) -> bool:
        try:
            res = self.runner.run(["git", "-C", str(self.repo), "config", "--get", "core.sshCommand"], check=False, timeout=10)
        except Exception:  # noqa: BLE001
            return False
        return res.returncode == 0 and bool(res.stdout.strip())

    def git(
        self,
        *args: str,
        write: bool = False,
        title: str = "",
        timeout: float = 60,
        check: bool = True,
        input: Optional[bytes] = None,
    ) -> str:
        if not self.available:
            raise CommandError("git " + shlex.join(args), 128, "", "not a git repository")
        cmd = ["git", "-C", str(self.repo), *args]
        res = self.runner.run(
            cmd,
            display="git " + shlex.join(args),
            title=title or "git",
            env=self.env(),
            timeout=timeout,
            write=write,
            check=check,
            input=input,
        )
        return res.stdout

    def current_branch(self) -> str:
        out = self.git("symbolic-ref", "--short", "-q", "HEAD", check=False).strip()
        if out:
            return out
        head = self.git("rev-parse", "--short", "HEAD", check=False).strip()
        return f"(detached {head})" if head else "(no HEAD)"

    def head(self) -> str:
        return self.git("rev-parse", "HEAD", check=False).strip()

    def ref_exists(self, ref: str) -> bool:
        res = self.runner.run(
            ["git", "-C", str(self.repo), "rev-parse", "--verify", "-q", ref + "^{commit}"],
            env=self.env(),
            check=False,
            timeout=15,
        )
        return res.returncode == 0

    def base_ref(self) -> Optional[str]:
        upstream = self.git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False).strip()
        if upstream and upstream.startswith(self.remote + "/"):
            return upstream
        candidate = f"{self.remote}/{self.branch}"
        if self.ref_exists(candidate):
            return candidate
        return upstream or None

    def stack(self, limit: int = 40) -> tuple[Optional[str], list[LocalCommit]]:
        """Local commits between the remote base branch and HEAD, newest first."""
        base = self.base_ref()
        if not base:
            return None, []
        fmt = _FIELD.join(["%H", "%h", "%s", "%an", "%at", "%B"]) + _RECORD
        out = self.git("log", f"-n{limit}", f"--format={fmt}", f"{base}..HEAD", check=False)
        commits: list[LocalCommit] = []
        for rec in out.split(_RECORD):
            rec = rec.strip("\n")
            if not rec:
                continue
            parts = rec.split(_FIELD)
            if len(parts) < 6:
                continue
            sha, short, subject, author, ts, body = parts[:6]
            commits.append(
                LocalCommit(
                    sha=sha,
                    short=short,
                    subject=subject,
                    author=author,
                    timestamp=int(ts or 0),
                    change_id=find_change_id(body),
                    body=body,
                )
            )
        return base, commits

    def has_commit(self, sha: str) -> bool:
        if not sha or not self.available:
            return False
        res = self.runner.run(
            ["git", "-C", str(self.repo), "cat-file", "-e", sha + "^{commit}"],
            env=self.env(),
            check=False,
            timeout=15,
        )
        return res.returncode == 0

    def fetch(self, ref: str) -> None:
        """Fetch objects for a ref without touching FETCH_HEAD, local refs or triggering gc."""
        self.git(
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
            "fetch",
            "--no-write-fetch-head",
            "--no-tags",
            "--quiet",
            self.remote,
            ref,
            title="Fetch patchset",
            timeout=300,
        )

    def ensure_commit(self, sha: str, ref: str) -> None:
        if not self.has_commit(sha):
            self.fetch(ref)

    def blame(self, rev: str, path: str, lines: Sequence[int]) -> tuple[dict[int, str], dict[str, BlameCommit]]:
        """The commit that last changed each of ``lines`` of ``path`` as it is in ``rev`` (git blame)."""
        ranges = [arg for a, b in line_ranges(lines) for arg in ("-L", f"{a},{b}")]
        return parse_blame(self.git("blame", "--porcelain", *ranges, rev, "--", path, title="Blame", timeout=180))

    def user_email(self) -> str:
        if self._email is None:
            res = self.runner.run(["git", "-C", str(self.repo), "config", "user.email"], env=self.env(), check=False, timeout=15)
            self._email = res.stdout.strip().lower()
        return self._email

    def line_history(self, rev: str, path: str, start: int, end: int, limit: int = 20) -> list[HistoryCommit]:
        """The commits that changed lines ``start``-``end`` of ``path`` as it is in ``rev``, newest first."""
        out = self.git(
            "log",
            f"-L{start},{end}:{path}",
            f"--max-count={limit}",
            "--no-color",
            f"--format={HISTORY_FORMAT}",
            rev,
            title="Line history",
            timeout=180,
        )
        return parse_line_history(out)

    # -- merge conflicts (Gerrit's "mergeable") ------------------------------------------------

    def remote_tip(self, branch: str) -> Optional[str]:
        """The commit ``branch`` points to on Gerrit right now (git ls-remote, changes nothing locally)."""
        out = self.git("ls-remote", self.remote, f"refs/heads/{branch}", title="Branch tip", timeout=60)
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.strip() == f"refs/heads/{branch}":
                return sha.strip()
        return None

    def fetch_branch(self, branch: str) -> None:
        """Fetch the objects of a branch on Gerrit without moving origin/<branch> or any other ref."""
        self.git(
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
            "fetch",
            "--refmap=",
            "--no-write-fetch-head",
            "--no-tags",
            "--quiet",
            self.remote,
            f"refs/heads/{branch}",
            title="Fetch branch",
            timeout=300,
        )

    def present_commits(self, shas: Sequence[str]) -> set[str]:
        """The commits of ``shas`` that are in the local clone."""
        wanted = [s for s in shas if s]
        if not wanted or not self.available:
            return set()
        out = self.git("cat-file", "--batch-check", input="".join(f"{s}^{{commit}}\n" for s in wanted).encode(), timeout=60)
        return {s for s, line in zip(wanted, out.splitlines(), strict=False) if not line.endswith(" missing")}

    def merge_conflicts(self, base: str, rev: str) -> list[str]:
        """The files that conflict when ``rev`` is merged into ``base``; empty if it merges cleanly.

        Uses git merge-tree (git 2.38+). The merge result is written to a scratch object directory,
        so the repository is not touched.
        """
        if self._objects is None:
            common = Path(self.git("rev-parse", "--git-common-dir").strip())
            self._objects = str((self.repo / common if not common.is_absolute() else common).resolve() / "objects")
        if self._scratch is None:
            self._scratch = tempfile.mkdtemp(prefix=f"lazygerrit-merge-{os.getpid()}-", dir=runtime_dir())
            atexit.register(shutil.rmtree, self._scratch, True)
        env = dict(self.env(), GIT_OBJECT_DIRECTORY=self._scratch, GIT_ALTERNATE_OBJECT_DIRECTORIES=self._objects)
        cmd = ["git", "-C", str(self.repo), "merge-tree", "--write-tree", "--name-only", "--no-messages", base, rev]
        res = self.runner.run(cmd, env=env, title="Merge check", timeout=120, check=False)
        if res.returncode not in (0, 1):
            raise CommandError("git " + shlex.join(cmd[3:]), res.returncode, res.stdout, res.stderr)
        files = res.stdout.splitlines()[1:] if res.returncode == 1 else []
        return list(dict.fromkeys(f for f in files if f))

    def close(self) -> None:
        if self._scratch:
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None

    def diff(self, base: str, rev: str, paths: Sequence[str], context: int = 3) -> str:
        return self.git(
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--find-renames",
            f"-U{context}",
            base,
            rev,
            "--",
            *paths,
            timeout=120,
        )

    def show_stat(self, rev: str) -> str:
        return self.git("show", "--no-color", "--stat", "--format=", rev, timeout=60)

    def files_changed(self, base: str, rev: str) -> list[FileInfo]:
        """Files changed between two commits (like Gerrit's file list, minus /COMMIT_MSG)."""
        status = self.git("diff", "--no-ext-diff", "--name-status", "-z", "-M", base, rev, timeout=120).split("\0")
        types: dict[str, tuple[str, Optional[str]]] = {}
        i = 0
        while i < len(status) and status[i]:
            code = status[i][0]
            if code in "RC":
                old, new = status[i + 1], status[i + 2]
                types[new] = ("RENAMED" if code == "R" else "COPIED", old)
                i += 3
            else:
                path = status[i + 1]
                kind = {"A": "ADDED", "D": "DELETED", "T": "MODIFIED"}.get(code, "MODIFIED")
                types[path] = (kind, None)
                i += 2
        numstat = self.git("diff", "--no-ext-diff", "--numstat", "-z", "-M", base, rev, timeout=120).split("\0")
        counts: dict[str, tuple[int, int]] = {}
        i = 0
        while i < len(numstat) and numstat[i]:
            ins, dele, path = (numstat[i].split("\t", 2) + ["", ""])[:3]
            if not path:
                path = numstat[i + 2]
                i += 3
            else:
                i += 1
            counts[path] = (int(ins) if ins.isdigit() else 0, int(dele) if dele.isdigit() else 0)
        files = []
        for path in sorted(types):
            kind, old = types[path]
            ins, dele = counts.get(path, (0, 0))
            files.append(FileInfo(path=path, type=kind, insertions=ins, deletions=dele, old_path=old))
        return files

    def show_file(self, rev: str, path: str) -> str:
        return self.git("show", f"{rev}:{path}", timeout=60)

    def show_patch(self, rev: str) -> str:
        return self.git("show", "--no-color", "--no-ext-diff", "--format=", rev, timeout=120)

    def commit_message_file(self, rev: str) -> str:
        """Reproduce Gerrit's /COMMIT_MSG magic file so comment line numbers line up."""
        date_fmt = "--date=format:%a %b %d %H:%M:%S %Y %z"
        out = self.git(
            "show",
            "-s",
            date_fmt,
            f"--format=%P{_FIELD}%an{_FIELD}%ae{_FIELD}%ad{_FIELD}%cn{_FIELD}%ce{_FIELD}%cd{_FIELD}%B",
            rev,
        )
        parts = out.split(_FIELD)
        if len(parts) < 8:
            return out
        parents, an, ae, ad, cn, ce, cd, body = parts[:8]
        lines = []
        plist = parents.split()
        for i, p in enumerate(plist):
            info = self.git("show", "-s", "--format=%h%x1f%s", "--abbrev=8", p, check=False).strip()
            short, _, subj = info.partition(_FIELD)
            label = "Parent:     " if len(plist) == 1 else ("Merge Of:   " if i == 0 else "            ")
            lines.append(f"{label}{short or p[:8]} ({subj})")
        lines.append(f"Author:     {an} <{ae}>")
        lines.append(f"AuthorDate: {ad}")
        lines.append(f"Commit:     {cn} <{ce}>")
        lines.append(f"CommitDate: {cd}")
        lines.append("")
        return "\n".join(lines) + "\n" + body.rstrip("\n") + "\n"

    def is_dirty(self) -> bool:
        out = self.git("status", "--porcelain", "--untracked-files=no", check=False, timeout=120)
        return bool(out.strip())

    def checkout_change(self, number: int, patch_set: int, ref: str, revision: str) -> str:
        if shutil.which("git-review"):
            return self.git("review", "-d", f"{number},{patch_set}", write=True, title="Checkout change", timeout=300)
        self.git("fetch", "--no-tags", "--quiet", self.remote, ref, write=True, title="Fetch change", timeout=300)
        return self.git("checkout", "-B", f"review/{number}-patch{patch_set}", revision, write=True, title="Checkout change")

    def branch_exists(self, name: str) -> bool:
        res = self.runner.run(
            ["git", "-C", str(self.repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
            env=self.env(),
            check=False,
            timeout=15,
        )
        return res.returncode == 0

    def add_worktree(self, path: str, revision: str, branch: str) -> str:
        """``git worktree add -b branch path revision``: a new checkout that shares this repository."""
        return self.git("worktree", "add", "-b", branch, path, revision, write=True, title="Create worktree", timeout=1800)

    def push(self, rev: str, target_branch: str, options: Sequence[str] = ()) -> str:
        refspec = f"{rev}:refs/for/{target_branch}"
        if options:
            refspec += "%" + ",".join(options)
        cmd = ["git", "-C", str(self.repo), "push", self.remote, refspec]
        res = self.runner.run(
            cmd,
            display="git " + shlex.join(["push", self.remote, refspec]),
            title="Push for review",
            env=self.env(),
            timeout=600,
            write=True,
        )
        return (res.stderr + res.stdout).strip()
