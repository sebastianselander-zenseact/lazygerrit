"""Local git repository integration (read-mostly; never touches FETCH_HEAD or the index)."""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path
from typing import Optional, Sequence

from .gerrit import CommandError, Runner
from .models import LocalCommit

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
