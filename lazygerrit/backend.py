"""High-level read API on top of the Gerrit SSH/REST, Zuul and git clients."""

from __future__ import annotations

from typing import Optional, Sequence

from .config import Config
from .gerrit import GerritREST, GerritSSH, RestError, Runner
from .localgit import LocalGit
from .models import Change, CIJob, FileInfo, Thread
from .parsing import parse_change, parse_patchset, parse_zuul_live, threads_from_change, threads_from_rest
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

    def threads(self, change: Change) -> tuple[list[Thread], bool]:
        """Comment threads; (threads, exact) where exact means REST data with resolution state."""
        if self.rest is not None:
            try:
                return threads_from_rest(self.rest.comments(change.number)), True
            except RestError:
                pass
        return threads_from_change(change), False

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
