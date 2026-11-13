"""Parsing of Gerrit SSH query JSON, REST comment threads and Zuul CI messages."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import replace
from typing import Any, Iterable, Optional

from .models import (
    COMMIT_MSG,
    MERGE_LIST,
    PATCHSET_LEVEL,
    Account,
    Approval,
    Change,
    ChangeMessage,
    CIJob,
    CIReport,
    CIStart,
    Comment,
    FileInfo,
    PatchSet,
    Relation,
    SubmitLabel,
    SubmitRecord,
    Thread,
    message_patch_set,
)

# Comments that only exist to drive CI (mirrors rng's _CI_ONLY_MESSAGES, plus priocheck).
CI_ONLY_MESSAGES = {"recheck", "slowcheck", "regate", "priocheck"}


class QueryError(Exception):
    pass


def parse_query_output(text: str) -> tuple[list[dict], Optional[dict]]:
    rows: list[dict] = []
    stats: Optional[dict] = None
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError as e:
            raise QueryError(f"Unexpected output from gerrit query: {raw[:200]}") from e
        kind = d.get("type")
        if kind == "stats":
            stats = d
        elif kind == "error":
            raise QueryError(d.get("message", "query error"))
        else:
            rows.append(d)
    return rows, stats


def parse_account(d: Optional[dict]) -> Account:
    if not d:
        return Account()
    return Account(
        name=d.get("name", "") or "",
        email=d.get("email", "") or "",
        username=d.get("username", "") or "",
    )


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def parse_approval(d: dict) -> Approval:
    return Approval(
        label=d.get("type", ""),
        value=_int(d.get("value")),
        by=parse_account(d.get("by")),
        granted_on=_int(d.get("grantedOn")),
    )


def parse_file(d: dict) -> FileInfo:
    return FileInfo(
        path=d.get("file", ""),
        type=d.get("type", "MODIFIED"),
        insertions=abs(_int(d.get("insertions"))),
        deletions=abs(_int(d.get("deletions"))),
        old_path=d.get("fileOld"),
    )


def parse_ssh_comment(d: dict, patch_set: int) -> Comment:
    return Comment(
        path=d.get("file", ""),
        line=_int(d.get("line")),
        author=parse_account(d.get("reviewer")),
        message=d.get("message", ""),
        patch_set=patch_set,
    )


def parse_patchset(d: dict) -> PatchSet:
    number = _int(d.get("number"))
    files = d.get("files")
    return PatchSet(
        number=number,
        revision=d.get("revision", ""),
        ref=d.get("ref", ""),
        parents=list(d.get("parents") or []),
        uploader=parse_account(d.get("uploader")),
        author=parse_account(d.get("author")),
        created_on=_int(d.get("createdOn")),
        kind=d.get("kind", ""),
        size_insertions=abs(_int(d.get("sizeInsertions"))),
        size_deletions=abs(_int(d.get("sizeDeletions"))),
        approvals=[parse_approval(a) for a in d.get("approvals") or []],
        comments=[parse_ssh_comment(c, number) for c in d.get("comments") or []],
        files=[parse_file(f) for f in files] if files is not None else None,
    )


def parse_relation(d: dict) -> Relation:
    return Relation(
        number=_int(d.get("number")),
        change_id=d.get("id", ""),
        revision=d.get("revision", ""),
        ref=d.get("ref", ""),
        is_current=d.get("isCurrentPatchSet"),
    )


def parse_submit_record(d: dict) -> SubmitRecord:
    return SubmitRecord(
        status=d.get("status", ""),
        labels=[
            SubmitLabel(
                label=lab.get("label", ""), status=lab.get("status", ""), by=parse_account(lab["by"]) if lab.get("by") else None
            )
            for lab in d.get("labels") or []
        ],
    )


def parse_change(d: dict, detailed: bool = False) -> Change:
    patch_sets = [parse_patchset(p) for p in d.get("patchSets") or []]
    current = parse_patchset(d["currentPatchSet"]) if d.get("currentPatchSet") else None
    if current is None and patch_sets:
        current = patch_sets[-1]
    if current is not None and patch_sets:
        for i, ps in enumerate(patch_sets):
            if ps.number == current.number:
                if current.files is None:
                    current.files = ps.files
                if not current.comments:
                    current.comments = ps.comments
                if not current.approvals:
                    current.approvals = ps.approvals
                patch_sets[i] = current
    messages = []
    for m in d.get("comments") or []:
        text = m.get("message", "")
        messages.append(
            ChangeMessage(
                timestamp=_int(m.get("timestamp")),
                author=parse_account(m.get("reviewer")),
                message=text,
                patch_set=message_patch_set(text),
            )
        )
    status = d.get("status", "NEW")
    return Change(
        number=_int(d.get("number")),
        change_id=d.get("id", ""),
        project=d.get("project", ""),
        branch=d.get("branch", ""),
        subject=d.get("subject", ""),
        owner=parse_account(d.get("owner")),
        status=status,
        open=bool(d.get("open", status == "NEW")),
        wip=bool(d.get("wip", False)),
        url=d.get("url", ""),
        topic=d.get("topic") or None,
        hashtags=list(d.get("hashtags") or []),
        created_on=_int(d.get("createdOn")),
        last_updated=_int(d.get("lastUpdated")),
        commit_message=d.get("commitMessage", ""),
        current=current,
        patch_sets=patch_sets,
        submit_records=[parse_submit_record(r) for r in d.get("submitRecords") or []],
        reviewers=[parse_account(a) for a in d.get("allReviewers") or []],
        messages=messages,
        depends_on=[parse_relation(r) for r in d.get("dependsOn") or []],
        needed_by=[parse_relation(r) for r in d.get("neededBy") or []],
        detailed=detailed,
    )


# --- comment threads ---------------------------------------------------------------------------


def is_ci_comment(path: str, message: str) -> bool:
    """True for patchset-level comments that only exist to trigger CI (recheck, run-selected...)."""
    if path != PATCHSET_LEVEL:
        return False
    msg = message.strip()
    return msg in CI_ONLY_MESSAGES or msg.startswith("run-selected")


def _path_order(path: str) -> tuple[int, str]:
    if path == PATCHSET_LEVEL:
        return (0, "")
    if path == COMMIT_MSG:
        return (1, "")
    if path == MERGE_LIST:
        return (2, "")
    return (3, path)


def threads_from_change(change: Change) -> list[Thread]:
    """Approximate threads from SSH query data (which lacks ids/resolution state).

    File comments on the same patchset+line are grouped; each patchset-level comment is its own
    thread because replies cannot be told apart from new comments.
    """
    sets = change.patch_sets or ([change.current] if change.current else [])
    threads: list[Thread] = []
    for ps in sets:
        groups: dict[tuple[str, int], list[Comment]] = {}
        order: list[tuple[str, int]] = []
        for c in ps.comments:
            if c.path == PATCHSET_LEVEL:
                threads.append(Thread(c.path, 0, ps.number, [c], None, is_ci_comment(c.path, c.message)))
                continue
            key = (c.path, c.line)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(c)
        for key in order:
            comments = groups[key]
            threads.append(Thread(key[0], key[1], ps.number, comments, None, False))
    threads.sort(key=lambda t: (-t.patch_set, _path_order(t.path), t.line))
    return threads


def _rest_comment(d: dict, path: str) -> Comment:
    return Comment(
        path=path,
        line=_int(d.get("line")),
        author=parse_account(d.get("author")),
        message=d.get("message", ""),
        patch_set=_int(d.get("patch_set")),
        id=d.get("id"),
        in_reply_to=d.get("in_reply_to"),
        unresolved=bool(d.get("unresolved", False)),
        updated=d.get("updated"),
        side=d.get("side"),
        range=d.get("range"),
    )


def threads_from_rest(data: dict[str, list[dict]]) -> list[Thread]:
    """Build threads from GET /changes/{id}/comments. Resolution follows the newest comment."""
    threads: list[Thread] = []
    for path, raw_comments in data.items():
        comments = {c["id"]: _rest_comment(c, path) for c in raw_comments if "id" in c}
        for group in _reply_groups(comments):
            group.sort(key=lambda c: c.updated or "")
            root = next((c for c in group if not c.in_reply_to), group[0])
            ci = all(is_ci_comment(path, c.message) for c in group)
            threads.append(
                Thread(
                    path=path,
                    line=root.line,
                    patch_set=root.patch_set,
                    comments=group,
                    unresolved=bool(group[-1].unresolved),
                    is_ci=ci,
                    side=root.side,
                    range=root.range,
                )
            )
    threads.sort(key=lambda t: (not t.unresolved, _neg_time(t.last.updated), _path_order(t.path), t.line))
    return threads


def _reply_groups(comments: dict[str, Comment]) -> list[list[Comment]]:
    """Union-find over in_reply_to links: each group is one thread."""
    parent = {cid: cid for cid in comments}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for cid, c in comments.items():
        if c.in_reply_to and c.in_reply_to in comments:
            ra, rb = find(cid), find(c.in_reply_to)
            if ra != rb:
                parent[ra] = rb
    groups: dict[str, list[Comment]] = defaultdict(list)
    for cid, c in comments.items():
        groups[find(cid)].append(c)
    return list(groups.values())


def _neg_time(ts: Optional[str]) -> str:
    # Sort newest first using an inverted string key; missing timestamps sort last.
    if not ts:
        return "\U0010ffff"
    return "".join(chr(0x10FFFF - ord(ch)) if ch.isdigit() else ch for ch in ts)


def filter_threads(threads: Iterable[Thread], show_resolved: bool, show_ci: bool) -> list[Thread]:
    """Same visibility rules as `rng comments`."""
    out: list[Thread] = []
    for t in threads:
        if t.is_ci:
            if show_ci:
                out.append(t)
            continue
        if not show_resolved and t.unresolved is False:
            continue
        if not show_ci:
            kept = [c for c in t.comments if not is_ci_comment(t.path, c.message)]
            if not kept:
                continue
            if len(kept) != len(t.comments):
                t = replace(t, comments=kept)
        out.append(t)
    return out


# --- CI (Zuul) ---------------------------------------------------------------------------------

_JOB_RE = re.compile(
    r"^- (?P<name>\S+)(?: (?P<url>https?://\S+))? : (?P<result>[A-Z][A-Z_]*)"
    r"(?: in (?P<dur>[0-9hms ]+?))?(?P<rest> \(.*\))?\s*$"
)
_BUILDSET_RE = re.compile(r"https?://\S+/buildset/\S+")
_SKIPPED_RE = re.compile(r"^Skipped (\d+) jobs?", re.IGNORECASE)
_START_RE = re.compile(r"Starting (?P<pipeline>[\w-]+) jobs:\s*(?P<url>\S+)")
_PIPELINES = ("run-selected", "slow-check", "slowcheck", "gate", "check", "post", "periodic")


def _guess_pipeline(text: str) -> str:
    low = text.lower()
    for p in _PIPELINES:
        if re.search(rf"\b{re.escape(p)}\b", low):
            return "slow-check" if p == "slowcheck" else p
    return ""


def parse_zuul_report(msg: ChangeMessage) -> Optional[CIReport]:
    lines = msg.message.split("\n")
    jobs: list[CIJob] = []
    summary = ""
    buildset = ""
    skipped = 0
    for raw in lines[1:]:
        s = raw.strip()
        if not s:
            continue
        m = _JOB_RE.match(s)
        if m:
            rest = m.group("rest") or ""
            jobs.append(
                CIJob(
                    name=m.group("name"),
                    result=m.group("result"),
                    url=m.group("url") or "",
                    duration=(m.group("dur") or "").strip(),
                    voting="non-voting" not in rest,
                )
            )
            continue
        sm = _SKIPPED_RE.match(s)
        if sm:
            skipped = int(sm.group(1))
            continue
        bm = _BUILDSET_RE.search(s)
        if bm and not buildset:
            buildset = bm.group(0)
            if s == buildset:
                continue
        if not summary:
            summary = s
    if not jobs:
        return None
    return CIReport(
        patch_set=msg.patch_set,
        timestamp=msg.timestamp,
        author=msg.author,
        summary=summary,
        votes=msg.votes_text,
        buildset_url=buildset,
        jobs=jobs,
        skipped=skipped,
        pipeline=_guess_pipeline(summary),
    )


def ci_messages(change: Change) -> tuple[list[CIReport], list[CIStart]]:
    """Extract Zuul reports and pipeline-start notices, oldest first."""
    reports: list[CIReport] = []
    starts: list[CIStart] = []
    for m in change.messages:
        sm = _START_RE.search(m.message)
        if sm:
            starts.append(CIStart(m.patch_set, m.timestamp, sm.group("pipeline"), sm.group("url")))
            continue
        rep = parse_zuul_report(m)
        if rep:
            reports.append(rep)
    return reports, starts


def latest_report(change: Change, patch_set: Optional[int] = None) -> Optional[CIReport]:
    reports, _ = ci_messages(change)
    ps = patch_set if patch_set is not None else change.ps
    for rep in reversed(reports):
        if rep.patch_set == ps:
            return rep
    return None


def ci_state(change: Change) -> tuple[str, Optional[CIReport], Optional[CIStart]]:
    """Return (state, latest report, latest start) for the current patchset.

    state is one of: none, running, success, failure.
    """
    reports, starts = ci_messages(change)
    ps = change.ps
    rep = next((r for r in reversed(reports) if r.patch_set == ps), None)
    start = next((s for s in reversed(starts) if s.patch_set == ps), None)
    if start and (rep is None or start.timestamp > rep.timestamp):
        return "running", rep, start
    if rep:
        return ("success" if rep.succeeded else "failure"), rep, start
    return "none", None, start


def parse_zuul_live(items: list[dict], base_url: str = "", tenant: str = "") -> list[CIJob]:
    jobs: list[CIJob] = []
    for item in items or []:
        for j in item.get("jobs") or []:
            url = j.get("url") or ""
            stream = ""
            if url and not url.startswith("http") and base_url:
                stream = f"{base_url.rstrip('/')}/t/{tenant}/{url}"
            elif url.startswith("http"):
                stream = url
            elapsed = j.get("elapsed_time")
            remaining = j.get("remaining_time")
            est = j.get("estimated_time")
            jobs.append(
                CIJob(
                    name=j.get("name", ""),
                    result=j.get("result"),
                    url=j.get("report_url") or "",
                    voting=bool(j.get("voting", True)),
                    elapsed_ms=int(elapsed) if elapsed is not None else None,
                    remaining_ms=int(remaining) if remaining is not None else None,
                    estimated_s=int(est) if est else None,
                    queued=bool(j.get("queued", False)),
                    waiting_status=j.get("waiting_status"),
                    pipeline=j.get("pipeline") or item.get("pipeline", ""),
                    live=True,
                    stream_url=stream,
                    start_time=str(j.get("start_time")) if j.get("start_time") else None,
                )
            )
    return jobs


def format_duration_ms(ms: Optional[int]) -> str:
    if ms is None:
        return ""
    s = int(ms / 1000)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"
