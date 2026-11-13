"""Data model for Gerrit changes, comments and CI results."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

PATCHSET_LEVEL = "/PATCHSET_LEVEL"
COMMIT_MSG = "/COMMIT_MSG"
MERGE_LIST = "/MERGE_LIST"

_BOT_RE = re.compile(r"^svc\d+$", re.IGNORECASE)

# Friendly names for the CI service accounts on gerrit.cicd.autoheim.net (overridable in the config).
BOT_NAMES: dict[str, str] = {"svc00140": "Zuul", "svc00596": "CI Platform"}


@dataclass(frozen=True)
class Account:
    name: str = ""
    email: str = ""
    username: str = ""

    @property
    def display(self) -> str:
        if self.username in BOT_NAMES and (not self.name or self.name == self.username):
            return BOT_NAMES[self.username]
        return self.name or self.username or self.email or "?"

    @property
    def short(self) -> str:
        return self.username or (self.email.split("@")[0] if self.email else "") or self.name or "?"

    @property
    def short_name(self) -> str:
        """Compact human name, e.g. 'Robin R.'; friendly names for known CI accounts."""
        if self.username in BOT_NAMES:
            return BOT_NAMES[self.username]
        parts = [p for p in (self.name or "").split() if p]
        if len(parts) >= 2:
            return f"{parts[0]} {parts[-1][0]}."
        if parts:
            return parts[0]
        return self.username or (self.email.split("@")[0] if self.email else "?")

    @property
    def initials(self) -> str:
        source = self.name or self.username or self.email.split("@")[0]
        parts = [p for p in re.split(r"[\s._-]+", source) if p]
        if len(parts) >= 2:
            return (parts[0][0] + parts[-1][0]).upper()
        if parts:
            return parts[0][:2].upper()
        return "??"

    def is_bot(self, extra_bots: tuple[str, ...] = ()) -> bool:
        u = self.username or ""
        return bool(_BOT_RE.match(u)) or u in extra_bots or "zuul" in u.lower()

    def matches(self, who: str) -> bool:
        who = who.lower()
        return who in (self.username.lower(), self.email.lower(), self.name.lower())


@dataclass
class Approval:
    label: str
    value: int
    by: Account
    granted_on: int = 0


@dataclass
class FileInfo:
    path: str
    type: str = "MODIFIED"
    insertions: int = 0
    deletions: int = 0
    old_path: Optional[str] = None

    @property
    def is_magic(self) -> bool:
        return self.path.startswith("/")

    @property
    def status_char(self) -> str:
        return {
            "ADDED": "A",
            "MODIFIED": "M",
            "DELETED": "D",
            "RENAMED": "R",
            "COPIED": "C",
            "REWRITE": "W",
        }.get(self.type, "?")

    @property
    def display_path(self) -> str:
        if self.path == COMMIT_MSG:
            return "Commit message"
        if self.path == MERGE_LIST:
            return "Merge list"
        return self.path


@dataclass
class Comment:
    path: str
    line: int
    author: Account
    message: str
    patch_set: int
    id: Optional[str] = None
    in_reply_to: Optional[str] = None
    unresolved: Optional[bool] = None
    updated: Optional[str] = None
    side: Optional[str] = None
    range: Optional[dict] = None


@dataclass
class Thread:
    path: str
    line: int
    patch_set: int
    comments: list[Comment]
    unresolved: Optional[bool]
    is_ci: bool = False
    side: Optional[str] = None
    range: Optional[dict] = None

    @property
    def root(self) -> Comment:
        return self.comments[0]

    @property
    def last(self) -> Comment:
        return self.comments[-1]

    @property
    def is_patchset_level(self) -> bool:
        return self.path == PATCHSET_LEVEL

    @property
    def location(self) -> str:
        if self.path == PATCHSET_LEVEL:
            return "Patchset"
        name = "Commit message" if self.path == COMMIT_MSG else self.path
        if self.line:
            return f"{name}:{self.line}"
        return name

    @property
    def short_location(self) -> str:
        if self.path == PATCHSET_LEVEL:
            return "Patchset"
        name = "Commit msg" if self.path == COMMIT_MSG else self.path.rsplit("/", 1)[-1]
        return f"{name}:{self.line}" if self.line else name


@dataclass
class ChangeMessage:
    timestamp: int
    author: Account
    message: str
    patch_set: Optional[int] = None

    @property
    def header(self) -> str:
        return self.message.split("\n", 1)[0]

    @property
    def body(self) -> str:
        parts = self.message.split("\n", 1)
        if len(parts) == 2 and _PS_HEADER_RE.match(parts[0]):
            return parts[1].strip("\n")
        return self.message

    @property
    def votes_text(self) -> str:
        """The label votes in a 'Patch Set N: Code-Review+2 Verified-1' header."""
        m = _PS_HEADER_RE.match(self.header)
        if not m:
            return ""
        return (m.group(2) or "").strip()


_PS_HEADER_RE = re.compile(r"^Patch Sets? (\d+)(?:-\d+)?:(.*)$")


def message_patch_set(message: str) -> Optional[int]:
    m = _PS_HEADER_RE.match(message.split("\n", 1)[0])
    if m:
        return int(m.group(1))
    m = re.match(r"^Uploaded patch set (\d+)", message)
    if m:
        return int(m.group(1))
    return None


@dataclass
class PatchSet:
    number: int
    revision: str = ""
    ref: str = ""
    parents: list[str] = field(default_factory=list)
    uploader: Account = field(default_factory=Account)
    author: Account = field(default_factory=Account)
    created_on: int = 0
    kind: str = ""
    size_insertions: int = 0
    size_deletions: int = 0
    approvals: list[Approval] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    files: Optional[list[FileInfo]] = None


@dataclass
class SubmitLabel:
    label: str
    status: str
    by: Optional[Account] = None


@dataclass
class SubmitRecord:
    status: str
    labels: list[SubmitLabel] = field(default_factory=list)


@dataclass
class Relation:
    number: int
    change_id: str = ""
    revision: str = ""
    ref: str = ""
    is_current: Optional[bool] = None


@dataclass
class Change:
    number: int
    change_id: str = ""
    project: str = ""
    branch: str = ""
    subject: str = ""
    owner: Account = field(default_factory=Account)
    status: str = "NEW"
    open: bool = True
    wip: bool = False
    url: str = ""
    topic: Optional[str] = None
    hashtags: list[str] = field(default_factory=list)
    created_on: int = 0
    last_updated: int = 0
    commit_message: str = ""
    current: Optional[PatchSet] = None
    patch_sets: list[PatchSet] = field(default_factory=list)
    submit_records: list[SubmitRecord] = field(default_factory=list)
    reviewers: list[Account] = field(default_factory=list)
    messages: list[ChangeMessage] = field(default_factory=list)
    depends_on: list[Relation] = field(default_factory=list)
    needed_by: list[Relation] = field(default_factory=list)
    has_unresolved: Optional[bool] = None
    detailed: bool = False

    @property
    def ps(self) -> int:
        return self.current.number if self.current else 0

    @property
    def ref_str(self) -> str:
        return f"{self.number},{self.ps}"

    @property
    def is_merged(self) -> bool:
        return self.status == "MERGED"

    @property
    def is_abandoned(self) -> bool:
        return self.status == "ABANDONED"

    def votes(self, label: str) -> list[Approval]:
        if not self.current:
            return []
        return [a for a in self.current.approvals if a.label == label]

    def label_value(self, label: str) -> Optional[int]:
        """Summarise a label like the Gerrit dashboard: rejections first, then approvals."""
        values = [a.value for a in self.votes(label) if a.value != 0]
        if not values:
            return None
        lo, hi = min(values), max(values)
        if lo <= -2:
            return lo
        if hi >= 2:
            return hi
        if lo < 0:
            return lo
        return hi

    def labels(self) -> list[str]:
        names: list[str] = []
        for rec in self.submit_records:
            for lab in rec.labels:
                if lab.label not in names:
                    names.append(lab.label)
        if self.current:
            for a in self.current.approvals:
                if a.label not in names:
                    names.append(a.label)
        return names

    def submit_status(self) -> Optional[str]:
        return self.submit_records[0].status if self.submit_records else None

    def patch_set(self, number: int) -> Optional[PatchSet]:
        for ps in self.patch_sets:
            if ps.number == number:
                return ps
        if self.current and self.current.number == number:
            return self.current
        return None

    def ps_for_revision(self, revision: str) -> Optional[PatchSet]:
        for ps in self.patch_sets or ([self.current] if self.current else []):
            if ps.revision == revision:
                return ps
        return None

    def merge_from(self, other: "Change") -> None:
        """Keep list-level extras (e.g. unresolved flag) when replacing with a detailed copy."""
        if self.has_unresolved is None:
            self.has_unresolved = other.has_unresolved
        if self.current and other.current and self.current.files is None and self.current.number == other.current.number:
            self.current.files = other.current.files


@dataclass
class LocalCommit:
    sha: str
    short: str
    subject: str
    author: str
    timestamp: int
    change_id: Optional[str] = None
    body: str = ""
    change: Optional[Change] = None

    @property
    def sync_state(self) -> str:
        """One of: not-pushed, merged, abandoned, synced, outdated, modified."""
        c = self.change
        if c is None:
            return "not-pushed"
        if c.is_merged:
            return "merged"
        if c.is_abandoned:
            return "abandoned"
        if c.current and c.current.revision == self.sha:
            return "synced"
        ps = c.ps_for_revision(self.sha)
        if ps is not None:
            return "outdated"
        return "modified"


@dataclass
class CIJob:
    name: str
    result: Optional[str] = None
    url: str = ""
    duration: str = ""
    voting: bool = True
    elapsed_ms: Optional[int] = None
    estimated_s: Optional[int] = None
    remaining_ms: Optional[int] = None
    queued: bool = False
    waiting_status: Optional[str] = None
    pipeline: str = ""
    live: bool = False
    stream_url: str = ""
    start_time: Optional[str] = None

    @property
    def short_name(self) -> str:
        return self.name.split("@", 1)[1] if "@" in self.name else self.name

    @property
    def state(self) -> str:
        """success, failure, running, queued, skipped, unknown."""
        r = (self.result or "").upper()
        if r == "SUCCESS":
            return "success"
        if r in ("SKIPPED",):
            return "skipped"
        if r in ("ABORTED", "CANCELED", "DEQUEUED"):
            return "canceled"
        if r:
            return "failure"
        if self.live:
            if self.queued or self.elapsed_ms is None:
                return "queued"
            return "running"
        return "unknown"

    @property
    def progress(self) -> Optional[float]:
        if self.elapsed_ms is None or not self.estimated_s:
            return None
        return max(0.0, min(1.0, self.elapsed_ms / 1000.0 / self.estimated_s))


@dataclass
class CIReport:
    """A Zuul result message posted on a change."""

    patch_set: Optional[int]
    timestamp: int
    author: Account
    summary: str
    votes: str = ""
    buildset_url: str = ""
    jobs: list[CIJob] = field(default_factory=list)
    skipped: int = 0
    pipeline: str = ""

    @property
    def failed_jobs(self) -> list[CIJob]:
        return [j for j in self.jobs if j.state == "failure"]

    @property
    def succeeded(self) -> bool:
        return bool(self.jobs) and not any(j.state == "failure" and j.voting for j in self.jobs)


@dataclass
class CIStart:
    """A 'Starting check jobs' style message."""

    patch_set: Optional[int]
    timestamp: int
    pipeline: str
    url: str
