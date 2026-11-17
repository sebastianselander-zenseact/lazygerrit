"""High-level read API on top of the Gerrit SSH/REST, Zuul and git clients."""

from __future__ import annotations

import threading
import time
from typing import Optional, Sequence

from .config import Config
from .gerrit import CommandError, GerritREST, GerritSSH, RestError, Runner
from .localgit import LocalGit
from .models import Change, CIJob, FileInfo, Thread
from .parsing import parse_change, parse_patchset, parse_zuul_live, threads_from_change, threads_from_notedb, threads_from_rest
from .zuul import ZuulClient, ZuulError

LIST_FLAGS = ("--current-patch-set", "--submit-records")
DETAIL_FLAGS = (
    "--current-patch-set",
    "--patch-sets",
    "--all-approvals",
    "--dependencies",
    "--submit-records",
    "--all-reviewers",
    "--commit-message",
    "--comments",
)
# Never combine --files with --patch-sets: Gerrit computes file lists for every patchset (very slow).
FILES_FLAGS = ("--current-patch-set", "--files")
LIGHT_FLAGS = ("--current-patch-set", "--dependencies", "--submit-records")
STACK_FLAGS = ("--current-patch-set", "--patch-sets", "--submit-records")


class NotFound(Exception):
    pass


class NeedsRest(Exception):
    pass


def _has_limit(query: str) -> bool:
    return "limit:" in query


class Backend:
    def __init__(
        self,
        cfg: Config,
        runner: Runner,
        ssh: GerritSSH,
        rest: Optional[GerritREST],
        zuul: Optional[ZuulClient],
        git: LocalGit,
    ):
        self.cfg = cfg
        self.runner = runner
        self.ssh = ssh
        self.rest = rest
        self.zuul = zuul
        self.git = git
        # number -> (key, notes or None if reading them failed, when)
        self._meta_comments: dict[int, tuple[tuple[int, int], Optional[list[dict]], float]] = {}
        self._meta_locks: dict[int, threading.Lock] = {}
        self._meta_guard = threading.Lock()

    @classmethod
    def create(cls, cfg: Config, runner: Runner) -> "Backend":
        ssh = GerritSSH(cfg.host, cfg.port, runner, user=cfg.user, multiplex=cfg.ssh_multiplex, extra_options=cfg.ssh_options)
        creds = GerritREST.find_credentials(cfg.host)
        rest = GerritREST(cfg.base_url, creds[0], creds[1], runner) if creds else None
        zuul = ZuulClient(cfg.zuul_url, cfg.zuul_tenant) if cfg.zuul_url else None
        git = LocalGit(cfg.repo, runner, cfg.remote, cfg.branch, ssh.git_ssh_command())
        return cls(cfg, runner, ssh, rest, zuul, git)

    # --- reads -------------------------------------------------------------------------------

    def list_changes(self, query: str, limit: Optional[int] = None) -> list[Change]:
        lim = None if _has_limit(query) else (limit or self.cfg.limit)
        rows, _ = self.ssh.query(query, LIST_FLAGS, limit=lim)
        return [parse_change(r) for r in rows]

    def unresolved_numbers(self, query: str, limit: Optional[int] = None) -> set[int]:
        lim = None if _has_limit(query) else (limit or self.cfg.limit)
        rows, _ = self.ssh.query(f"({query}) has:unresolved", (), limit=lim)
        return {int(r["number"]) for r in rows if "number" in r}

    def details(self, number: int) -> Change:
        rows, _ = self.ssh.query(f"change:{number}", DETAIL_FLAGS)
        if not rows:
            raise NotFound(f"Change {number} was not found")
        return parse_change(rows[0], detailed=True)

    def files(self, number: int) -> tuple[int, list[FileInfo]]:
        rows, _ = self.ssh.query(f"change:{number}", FILES_FLAGS)
        if not rows or "currentPatchSet" not in rows[0]:
            raise NotFound(f"Change {number} was not found")
        ps = parse_patchset(rows[0]["currentPatchSet"])
        return ps.number, ps.files or []

    def light(self, number: int) -> Optional[Change]:
        rows, _ = self.ssh.query(f"change:{number}", LIGHT_FLAGS)
        return parse_change(rows[0]) if rows else None

    def by_change_ids(self, change_ids: Sequence[str]) -> dict[str, Change]:
        found: dict[str, Change] = {}
        ids = [c for c in dict.fromkeys(change_ids) if c]
        for i in range(0, len(ids), 25):
            chunk = ids[i : i + 25]
            terms = " OR ".join(f"change:{cid}" for cid in chunk)
            q = f"project:{self.cfg.project} branch:{self.cfg.branch} ({terms})"
            rows, _ = self.ssh.query(q, STACK_FLAGS)
            for r in rows:
                ch = parse_change(r)
                found[ch.change_id] = ch
        return found

    def threads(self, change: Change, retry: bool = False) -> tuple[list[Thread], bool]:
        """Comment threads; (threads, exact) where exact means real comment ids and resolution state,
        from REST or else from the change's meta ref (NoteDb), which git can fetch over SSH.
        ``retry`` reads the meta ref again even if that failed less than a minute ago."""
        if self.rest is not None:
            try:
                return threads_from_rest(self.rest.comments(change.number)), True
            except RestError:
                pass
        notes = self.meta_comments(change, retry)
        if notes is not None:
            return threads_from_notedb(notes, change), True
        return threads_from_change(change), False

    def meta_comments(self, change: Change, retry: bool = False) -> Optional[list[dict]]:
        """The change's comments from its meta ref (None if it can't be read). Fetched again only when
        the change was updated or got new comments, or a minute after a failure."""
        if self.git is None or not self.git.available:
            return None
        key = (change.last_updated, sum(len(ps.comments) for ps in change.patch_sets))
        with self._meta_guard:
            lock = self._meta_locks.setdefault(change.number, threading.Lock())
        with lock:  # one fetch of a change at a time; a second caller then gets its result
            cached = self._meta_comments.get(change.number)
            if (
                cached is not None
                and cached[0] == key
                and (cached[1] is not None or (not retry and time.monotonic() - cached[2] < 60))
            ):
                return cached[1]
            try:
                url = f"ssh://{self.ssh.destination}:{self.ssh.port}/{change.project or self.cfg.project}"
                notes: Optional[list[dict]] = self.git.change_meta_comments(change.number, url)
            except (CommandError, OSError, ValueError):
                notes = None  # e.g. the server hides meta refs: approximate threads from the SSH data
            self._meta_comments[change.number] = (key, notes, time.monotonic())
            return notes

    def zuul_live(self, number: int, patch_set: int) -> list[CIJob]:
        if self.zuul is None:
            return []
        try:
            items = self.zuul.change_status(number, patch_set)
        except ZuulError:
            return []
        return parse_zuul_live(items, self.cfg.zuul_url, self.cfg.zuul_tenant)

    def tower(self, change: Change, border: Optional[str] = None, max_depth: int = 60) -> list[Change]:
        """The change and its open ancestors, top first (same semantics as rng's get_tower)."""
        cur = self.light(change.number) or change
        result = [cur]
        while len(result) < max_depth:
            if len(cur.depends_on) != 1:
                break
            dep = self.light(cur.depends_on[0].number)
            if dep is None or not dep.open or str(dep.number) == str(border):
                break
            result.append(dep)
            cur = dep
        return result

    # --- writes ------------------------------------------------------------------------------

    def set_hashtags(self, number: int, add: Sequence[str] = (), remove: Sequence[str] = ()) -> None:
        if self.rest is None:
            raise NeedsRest(
                "Hashtags can only be changed through the Gerrit REST API, which needs an HTTP password.\n"
                f"Generate one at {self.cfg.base_url}/settings/#HTTPCredentials and add it to ~/.netrc:\n"
                f"  machine {self.cfg.host} login <username> password <http-password>"
            )
        self.rest.set_hashtags(number, add, remove)

    def close(self) -> None:
        self.ssh.close()
        if self.git is not None:
            self.git.close()
