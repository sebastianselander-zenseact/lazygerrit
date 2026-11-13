"""Content rendering: list rows and main-view bodies."""

from __future__ import annotations

import calendar
import difflib
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Optional, Sequence

from .. import __version__
from ..diff import DiffRow, LineRef
from ..drafts import Draft
from ..models import (
    Account,
    Change,
    ChangeMessage,
    CIJob,
    CIReport,
    FileInfo,
    LocalCommit,
    Thread,
)
from ..parsing import ci_state, format_duration_ms, is_ci_comment
from . import theme
from .text import (
    DEFAULT_COLOR,
    PLAIN,
    Line,
    Style,
    ansi_to_lines,
    drop_first_char,
    line_width,
    pad_str,
    plain,
    sanitize,
    strip_ansi,
    text_width,
    truncate_str,
    wrap,
)

VOTE_GLYPH_OK = "✓"
VOTE_GLYPH_BAD = "✗"


def now() -> float:
    return time.time()


def rel_time(ts: Optional[float], ref: Optional[float] = None) -> str:
    if not ts:
        return ""
    d = max(0, (ref or now()) - ts)
    if d < 60:
        return f"{int(d)}s"
    if d < 3600:
        return f"{int(d // 60)}m"
    if d < 86400:
        return f"{int(d // 3600)}h"
    if d < 86400 * 14:
        return f"{int(d // 86400)}d"
    if d < 86400 * 60:
        return f"{int(d // (86400 * 7))}w"
    if d < 86400 * 365:
        return f"{int(d // (86400 * 30))}mo"
    return f"{int(d // (86400 * 365))}y"


def rel_ago(ts: Optional[float]) -> str:
    r = rel_time(ts)
    return f"{r} ago" if r else ""


def gerrit_time(s: Optional[str]) -> Optional[float]:
    """Parse REST timestamps like '2026-11-11 14:48:30.000000000' (UTC)."""
    if not s:
        return None
    try:
        return float(calendar.timegm(time.strptime(s[:19], "%Y-%m-%d %H:%M:%S")))
    except ValueError:
        return None


def comment_time(c: Any) -> Optional[float]:
    return gerrit_time(getattr(c, "updated", None))


def vote_style(value: int) -> Style:
    if value >= 2:
        return theme.OK_BOLD
    if value > 0:
        return theme.OK
    if value <= -2:
        return theme.BAD_BOLD
    return theme.BAD


def vote_span(value: Optional[int], width: int = 2) -> tuple[str, Style]:
    if value is None:
        return ("·".rjust(width), theme.DIM)
    return (f"{value:+d}".rjust(width), vote_style(value))


def verified_span(value: Optional[int]) -> tuple[str, Style]:
    if value is None:
        return (" ", PLAIN)
    if value > 0:
        return (VOTE_GLYPH_OK, theme.OK_BOLD if value >= 2 else theme.OK)
    return (VOTE_GLYPH_BAD, theme.BAD_BOLD if value <= -2 else theme.BAD)


def status_tag(ch: Change) -> Optional[tuple[str, Style]]:
    if ch.is_merged:
        return ("MERGED ", theme.MAGIC)
    if ch.is_abandoned:
        return ("ABANDONED ", theme.BAD)
    if ch.wip:
        return ("WIP ", theme.MAGIC)
    return None


def change_row(ch: Change, width: int, *, show_owner: bool, hide_wip: bool = False, drafts: int = 0) -> Line:
    parts: Line = [(str(ch.number), theme.NUMBER), (" ", PLAIN)]
    parts.append(verified_span(ch.label_value("Verified")))
    parts.append((" ", PLAIN))
    parts.append(vote_span(ch.label_value("Code-Review")))
    parts.append((" ", PLAIN))
    parts.append(vote_span(ch.label_value("Secondary-Review")))
    parts.append((" ", PLAIN))
    parts.append(("●", theme.WARN) if ch.has_unresolved else (" ", PLAIN))
    parts.append((" ", PLAIN))
    if show_owner:
        parts.append((ch.owner.initials.ljust(2), theme.author_style(ch.owner.username or ch.owner.name)))
        parts.append((" ", PLAIN))
    if drafts:
        parts.append((f"✎{drafts} ", theme.MAGIC))
    tag = status_tag(ch)
    if tag and not (hide_wip and ch.wip and not ch.is_merged and not ch.is_abandoned):
        parts.append(tag)
    parts.append((sanitize(ch.subject), Style(dim=True) if ch.is_abandoned else PLAIN))
    return parts


SYNC_GLYPHS = {
    "synced": ("✓", theme.OK, theme.OK),
    "modified": ("↑", theme.WARN, theme.WARN),
    "outdated": ("↓", theme.INFO, theme.INFO),
    "not-pushed": ("+", theme.BAD, theme.BAD),
    "merged": ("M", theme.MAGIC, theme.MAGIC),
    "abandoned": ("A", theme.DIM, theme.DIM),
}

SYNC_TEXT = {
    "synced": "in sync with the current patch set on Gerrit",
    "modified": "differs from Gerrit — push to upload a new patch set",
    "outdated": "is an older patch set — Gerrit has a newer one",
    "not-pushed": "is not on Gerrit yet",
    "merged": "is merged on Gerrit",
    "abandoned": "belongs to an abandoned change",
}


def stack_row(c: LocalCommit, width: int) -> Line:
    state = c.sync_state
    glyph, gstyle, hstyle = SYNC_GLYPHS[state]
    parts: Line = [(c.short, hstyle), (" ", PLAIN), (glyph, gstyle), (" ", PLAIN)]
    if c.change is not None:
        parts.append((str(c.change.number), theme.NUMBER))
        parts.append((" ", PLAIN))
        parts.append(verified_span(c.change.label_value("Verified")))
        parts.append((" ", PLAIN))
        parts.append(vote_span(c.change.label_value("Code-Review")))
        parts.append((" ", PLAIN))
    parts.append((sanitize(c.subject), PLAIN))
    return parts


FILE_STATUS_STYLE = {
    "A": theme.OK,
    "M": theme.WARN,
    "D": theme.BAD,
    "R": theme.INFO,
    "C": theme.INFO,
    "W": theme.MAGIC,
}


def file_row(f: FileInfo, width: int, comments: int = 0, unresolved: int = 0, drafts: int = 0, reviewed: bool = False) -> Line:
    st = f.status_char
    parts: Line = [("✓" if reviewed else " ", theme.OK), (st, FILE_STATUS_STYLE.get(st, PLAIN)), (" ", PLAIN)]
    right: Line = []
    if drafts:
        right.append((f"✎{drafts} ", theme.MAGIC))
    if unresolved:
        right.append((f"●{unresolved} ", theme.WARN))
    if comments - unresolved > 0:
        right.append((f"○{comments - unresolved} ", theme.DIM))
    if not f.is_magic:
        right += [(f"+{f.insertions}", theme.OK), (" ", PLAIN), (f"-{f.deletions}", theme.BAD)]
    right_w = sum(text_width(t) for t, _ in right)
    avail = max(4, width - 3 - right_w - 1)
    path_style = theme.DIM if reviewed else PLAIN
    if f.is_magic:
        shown = truncate_str(f.display_path, avail)
        parts.append((shown, theme.MAGIC))
        used = text_width(shown)
    else:
        path = f.path
        if text_width(path) > avail:
            path = "…" + path[-(avail - 1) :]
        if "/" in path:
            d, base = path.rsplit("/", 1)
            parts += [(d + "/", theme.DIM), (base, path_style)]
        else:
            parts.append((path, path_style))
        used = text_width(path)
    gap = max(1, width - 3 - used - right_w)
    parts.append((" " * gap, PLAIN))
    parts += right
    return parts


@dataclass
class CIHeader:
    title: str
    state: str
    url: str = ""
    report: Optional[CIReport] = None
    live: bool = False
    jobs: list[CIJob] = field(default_factory=list)
    timestamp: int = 0


CI_GLYPH = {
    "success": ("✓", theme.OK),
    "failure": ("✗", theme.BAD),
    "running": ("●", theme.WARN),
    "queued": ("◌", theme.DIM),
    "skipped": ("-", theme.DIM),
    "canceled": ("⊘", theme.DIM),
    "unknown": ("?", theme.DIM),
    "none": ("·", theme.DIM),
}


def progress_bar(p: float, width: int = 10) -> str:
    filled = int(round(p * width))
    return "█" * filled + "░" * (width - filled)


def job_right(j: CIJob) -> Line:
    st = j.state
    if st == "running":
        p = j.progress
        el = format_duration_ms(j.elapsed_ms)
        if p is not None:
            return [(progress_bar(p, 8), theme.WARN), (f" {int(p * 100):>3}%", theme.WARN)]
        return [(el, theme.WARN)]
    if st == "queued":
        return [(j.waiting_status or "queued", theme.DIM)]
    if j.duration:
        return [(j.duration, theme.DIM)]
    if j.live and j.elapsed_ms:
        return [(format_duration_ms(j.elapsed_ms), theme.DIM)]
    return []


def ci_row(item: Any, width: int) -> Line:
    if isinstance(item, CIHeader):
        glyph, st = CI_GLYPH.get(item.state, ("·", theme.DIM))
        parts: Line = [(glyph, st), (" ", PLAIN), (item.title, theme.BOLD)]
        if item.timestamp:
            parts.append((f" · {rel_ago(item.timestamp)}", theme.DIM))
        return parts
    j: CIJob = item
    glyph, st = CI_GLYPH.get(j.state, ("?", theme.DIM))
    right = job_right(j)
    if not j.voting:
        right = [("non-voting ", theme.DIM)] + right
    right_w = sum(text_width(t) for t, _ in right)
    name = truncate_str(j.short_name, max(4, width - 6 - right_w))
    gap = max(1, width - 4 - text_width(name) - right_w)
    return [("  ", PLAIN), (glyph, st), (" ", PLAIN), (name, PLAIN), (" " * gap, PLAIN)] + right


def thread_glyph(t: Thread) -> tuple[str, Style]:
    if t.is_ci:
        return ("⚙", theme.DIM)
    if t.unresolved is None:
        return ("·", theme.DIM)
    return ("●", theme.WARN) if t.unresolved else ("✓", theme.OK)


def first_line(text: str) -> str:
    for ln in text.splitlines():
        if ln.strip():
            return sanitize(ln.strip())
    return ""


def thread_row(t: Thread, width: int) -> Line:
    glyph = thread_glyph(t)
    last = t.last
    parts: Line = [glyph, (" ", PLAIN), (f"ps{t.patch_set} ", theme.DIM), (t.short_location, theme.INFO), (" ", PLAIN)]
    parts.append((last.author.short_name, theme.author_style(last.author.username or last.author.name)))
    if len(t.comments) > 1:
        parts.append((f" ({len(t.comments)})", theme.DIM))
    parts.append((": ", theme.DIM))
    parts.append((first_line(t.root.message if len(t.comments) == 1 else last.message), PLAIN))
    return parts


def draft_row(d: Draft, width: int) -> Line:
    parts: Line = [("✎", theme.MAGIC), (" ", PLAIN), (f"ps{d.patch_set} ", theme.DIM), (d.short_location, theme.INFO)]
    parts.append((" draft", theme.MAGIC))
    if d.is_reply:
        parts.append((" reply", theme.DIM))
    parts += [(": ", theme.DIM), (first_line(d.message), PLAIN)]
    return parts


def draft_view(d: Draft, width: int, thread: Optional[Thread] = None) -> list[Line]:
    state = "unresolved" if d.unresolved else "resolved"
    out: list[Line] = [
        [
            ("✎ ", theme.MAGIC),
            ("Draft", theme.MAGIC),
            (" on ", PLAIN),
            (d.location, theme.BOLD),
            (f" · patch set {d.patch_set} · {state}", theme.DIM),
        ],
        [("Not published yet: press p to publish your drafts (with an optional vote and message).", theme.DIM)],
        [],
    ]
    if thread is not None:
        out.append([("In reply to", theme.HEADING)])
        for c in thread.comments:
            out += comment_block(c, width, indent="  ")
        out.append([])
    elif d.reply_to_text:
        out.append([("In reply to: ", theme.DIM), (d.reply_to_text, PLAIN)])
        out.append([])
    out += wrap_lines(d.message, width, PLAIN, "  ")
    out.append([])
    out.append(
        [
            ("c", theme.KEY),
            (" edit · ", theme.DIM),
            ("u", theme.KEY),
            (" toggle resolved · ", theme.DIM),
            ("D", theme.KEY),
            (" delete · ", theme.DIM),
            ("enter", theme.KEY),
            (" show in code", theme.DIM),
        ]
    )
    return out


def message_row(m: ChangeMessage, width: int, bots: Sequence[str] = ()) -> Line:
    bot = m.author.is_bot(tuple(bots))
    author_style = theme.DIM if bot else theme.author_style(m.author.username or m.author.name)
    parts: Line = [(rel_time(m.timestamp).rjust(3), theme.DIM), (" ", PLAIN)]
    parts.append((f"ps{m.patch_set}" if m.patch_set else "   ", theme.DIM))
    parts.append((" ", PLAIN))
    parts.append((pad_str(m.author.short_name, 13), author_style))
    parts.append((" ", PLAIN))
    votes = m.votes_text
    if votes:
        for v in votes.split():
            style = theme.BAD if "-" in v[1:] else theme.OK
            if v.startswith("-"):
                style = theme.DIM
            parts.append((v + " ", style))
    parts.append((first_line(m.body) or first_line(m.message), theme.DIM if bot else PLAIN))
    return parts


# --- main view bodies --------------------------------------------------------------------------


def heading(text: str) -> Line:
    return [(text, theme.HEADING)]


def kv(label: str, value: Any, width: int = 11) -> Line:
    val: Line = [(str(value), PLAIN)] if isinstance(value, str) else list(value)
    return [(label.ljust(width), theme.LABEL)] + val


def wrap_lines(text: str, width: int, style: Style = PLAIN, indent: str = "") -> list[Line]:
    out: list[Line] = []
    for ln in wrap(sanitize_keep_newlines(text), max(10, width - len(indent))):
        out.append([(indent + ln, style)])
    return out


def sanitize_keep_newlines(text: str) -> str:
    return "\n".join(sanitize(ln) for ln in text.replace("\r\n", "\n").split("\n"))


SUBMIT_GLYPH = {
    "OK": ("✓", theme.OK),
    "REJECT": ("✗", theme.BAD),
    "NEED": ("○", theme.WARN),
    "MAY": ("·", theme.DIM),
    "IMPOSSIBLE": ("!", theme.BAD),
}


def account_line(a: Account) -> str:
    if a.name and a.email:
        return f"{a.name} <{a.email}>"
    return a.display


def labels_section(ch: Change) -> list[Line]:
    out: list[Line] = []
    rec = ch.submit_records[0] if ch.submit_records else None
    statuses = {lab.label: lab.status for lab in rec.labels} if rec else {}
    title = "Labels"
    if rec:
        title += f" · {rec.status.replace('_', ' ').lower()}"
    out.append(heading(title))
    labels = ch.labels() or ["Code-Review", "Verified"]
    w = max(text_width(n) for n in labels) + 2
    for label in labels:
        st = statuses.get(label, "")
        glyph, gst = SUBMIT_GLYPH.get(st, (" ", PLAIN))
        parts: Line = [("  ", PLAIN), (glyph, gst), (" ", PLAIN), (label.ljust(w), PLAIN)]
        votes = sorted(ch.votes(label), key=lambda a: -abs(a.value))
        if votes:
            for i, a in enumerate(votes):
                if i:
                    parts.append((", ", theme.DIM))
                parts.append((f"{a.value:+d}", vote_style(a.value)))
                parts.append((f" {a.by.display}", PLAIN))
        elif st:
            parts.append((st.lower(), theme.DIM))
        out.append(parts)
    return out


def ci_section(ch: Change, live: Optional[list[CIJob]], zuul_status_url: str = "") -> list[Line]:
    out: list[Line] = []
    state, rep, start = ci_state(ch)
    live_jobs = [j for j in (live or []) if j.state != "skipped"]
    if live_jobs:
        running = [j for j in live_jobs if j.state in ("running", "queued")]
        pipelines = sorted({j.pipeline for j in live_jobs if j.pipeline})
        done = sum(1 for j in live_jobs if j.state == "success")
        failed = [j for j in live_jobs if j.state == "failure"]
        title = f"CI · {'/'.join(pipelines) or 'zuul'} · running"
        out.append([("● ", theme.WARN), (title, theme.HEADING), (f" · {done}/{len(live_jobs)} done", theme.DIM)])
        for j in failed:
            out.append([("  ", PLAIN), ("✗ ", theme.BAD), (j.short_name, PLAIN)])
        for j in running[:8]:
            out.append([("  ", PLAIN), CI_GLYPH[j.state], (" ", PLAIN), (j.short_name, PLAIN), ("  ", PLAIN)] + job_right(j))
        if len(running) > 8:
            out.append([(f"  … {len(running) - 8} more", theme.DIM)])
        if zuul_status_url:
            out.append([("  ", PLAIN), (zuul_status_url, theme.LINK)])
        if rep is None:
            return out
        out.append([])
    if rep is not None:
        ok = rep.succeeded
        glyph, gst = ("✓", theme.OK) if ok else ("✗", theme.BAD)
        title = f"CI · {rep.pipeline or 'zuul'} · {'passed' if ok else 'failed'}"
        out.append([(glyph + " ", gst), (title, theme.HEADING), (f" · ps{rep.patch_set} · {rel_ago(rep.timestamp)}", theme.DIM)])
        if rep.summary:
            out.append([("  ", PLAIN), (truncate_str(rep.summary, 200), theme.DIM)])
        failed = rep.failed_jobs
        for j in failed:
            out.append(
                [("  ", PLAIN), ("✗ ", theme.BAD), (j.short_name, PLAIN), (f"  {j.result} {j.duration}".rstrip(), theme.BAD)]
            )
        passed = sum(1 for j in rep.jobs if j.state == "success")
        summary = f"  ✓ {passed} passed"
        if rep.skipped:
            summary += f" · {rep.skipped} skipped"
        out.append([(summary, theme.OK if passed else theme.DIM)])
        if rep.buildset_url:
            out.append([("  ", PLAIN), (rep.buildset_url, theme.LINK)])
    elif state == "running" and start is not None:
        out.append([("● ", theme.WARN), (f"CI · {start.pipeline} · started {rel_ago(start.timestamp)}", theme.HEADING)])
        out.append([("  ", PLAIN), (start.url, theme.LINK)])
    elif not live_jobs:
        out.append([("· ", theme.DIM), ("CI · no results for this patch set", theme.DIM)])
    return out


def relation_section(ch: Change, chain: Optional[list[Change]]) -> list[Line]:
    if not chain or len(chain) <= 1:
        if not ch.depends_on and not ch.needed_by:
            return []
    out: list[Line] = [heading("Relation chain")]
    if chain:
        idx = next((i for i, c in enumerate(chain) if c.number == ch.number), -1)
        for i, c in enumerate(chain):
            if i < idx:
                arrow = ("↑ ", theme.DIM)
            elif i == idx:
                arrow = ("▶ ", theme.OK_BOLD)
            else:
                arrow = ("↓ ", theme.DIM)
            parts: Line = [("  ", PLAIN), arrow, (str(c.number), theme.NUMBER), (" ", PLAIN)]
            parts.append(verified_span(c.label_value("Verified")))
            parts.append((" ", PLAIN))
            parts.append(vote_span(c.label_value("Code-Review")))
            parts.append((" ", PLAIN))
            tag = status_tag(c)
            if tag:
                parts.append(tag)
            parts.append((sanitize(c.subject), theme.BOLD if i == idx else PLAIN))
            out.append(parts)
    else:
        for r in ch.needed_by:
            out.append([("  ↑ ", theme.DIM), (str(r.number), theme.NUMBER), (" needed by", theme.DIM)])
        out.append([("  ▶ ", theme.OK_BOLD), (str(ch.number), theme.NUMBER), (" " + ch.subject, theme.BOLD)])
        for r in ch.depends_on:
            note = "" if r.is_current is not False else " (outdated patch set)"
            out.append([("  ↓ ", theme.DIM), (str(r.number), theme.NUMBER), (" depends on" + note, theme.DIM)])
    return out


def change_details(
    ch: Change,
    *,
    base_url: str,
    width: int,
    chain: Optional[list[Change]] = None,
    threads: Optional[tuple[list[Thread], bool]] = None,
    live: Optional[list[CIJob]] = None,
    zuul_status_url: str = "",
    loading: bool = False,
) -> list[Line]:
    out: list[Line] = []
    head: Line = [(str(ch.number), theme.NUMBER), (" ", PLAIN), (sanitize(ch.subject), theme.BOLD)]
    out.append(head)
    out.append([])
    status = ch.status
    if ch.wip and ch.open:
        status = "WIP"
    st_style = theme.MAGIC if status in ("MERGED", "WIP") else (theme.BAD if status == "ABANDONED" else theme.OK)
    status_parts: Line = [(status, st_style)]
    if ch.submit_status() and ch.open:
        status_parts.append((f" · {ch.submit_status().replace('_', ' ').lower()}", theme.DIM))
    out.append(kv("Status", status_parts))
    out.append(kv("Owner", account_line(ch.owner)))
    upd: Line = [(rel_ago(ch.last_updated), PLAIN)]
    if ch.created_on:
        upd.append((f" (created {rel_ago(ch.created_on)})", theme.DIM))
    out.append(kv("Updated", upd))
    out.append(kv("Project", f"{ch.project} · {ch.branch}"))
    if ch.topic:
        out.append(kv("Topic", [(ch.topic, theme.INFO)]))
    if ch.hashtags:
        out.append(kv("Hashtags", [(" ".join(f"#{h}" for h in ch.hashtags), theme.MAGIC)]))
    if ch.current:
        ps = ch.current
        total = len(ch.patch_sets) if ch.patch_sets else ps.number
        ps_parts: Line = [(f"{ps.number}", theme.BOLD), (f" of {total}", theme.DIM)]
        if ps.kind:
            ps_parts.append((f" · {ps.kind.replace('_', ' ').lower()}", theme.DIM))
        if ps.size_insertions or ps.size_deletions:
            ps_parts += [
                (" · ", theme.DIM),
                (f"+{ps.size_insertions}", theme.OK),
                (" ", PLAIN),
                (f"-{ps.size_deletions}", theme.BAD),
            ]
        if ps.uploader.display != "?":
            ps_parts.append((f" · {ps.uploader.display} {rel_ago(ps.created_on)}", theme.DIM))
        out.append(kv("Patch set", ps_parts))
        if ps.revision:
            out.append(kv("Commit", [(ps.revision[:12], theme.HASH), (f"  {ps.ref}", theme.DIM)]))
    out.append(kv("URL", [(f"{base_url}/c/{ch.project}/+/{ch.number}", theme.LINK)]))
    humans = [r for r in ch.reviewers if r.username != ch.owner.username and not r.is_bot()]
    if humans:
        out.append(kv("Reviewers", ", ".join(r.display for r in humans)))
    out.append([])
    out += labels_section(ch)
    out.append([])
    if ch.detailed:
        out += ci_section(ch, live, zuul_status_url)
        out.append([])
    rel = relation_section(ch, chain) if ch.detailed else []
    if rel:
        out += rel
        out.append([])
    if threads is not None:
        ths, exact = threads
        human = [t for t in ths if not t.is_ci]
        if exact:
            unres = sum(1 for t in human if t.unresolved)
            text = f"{unres} unresolved · {len(human) - unres} resolved"
            out.append([("Comments  ", theme.LABEL), (text, theme.WARN if unres else theme.DIM)])
        else:
            text = f"{len(human)} comment threads"
            out.append([("Comments  ", theme.LABEL), (text, PLAIN), (" (resolution needs REST access)", theme.DIM)])
        out.append([])
    if ch.commit_message:
        out.append(heading("Commit message"))
        for ln in sanitize_keep_newlines(ch.commit_message.rstrip()).split("\n"):
            style = theme.DIM if ln.startswith(("Change-Id:", "Signed-off-by:", "Jira issue:", "Solves CHIP:")) else PLAIN
            for part in wrap(ln, max(10, width - 2)) or [""]:
                out.append([("  " + part, style)])
    if loading:
        out.append([])
        out.append([("Loading details…", theme.DIM)])
    return out


def commit_details(c: LocalCommit, width: int) -> list[Line]:
    state = c.sync_state
    glyph, gst, hst = SYNC_GLYPHS[state]
    out: list[Line] = [[(c.short, hst), (" ", PLAIN), (sanitize(c.subject), theme.BOLD)], []]
    out.append(kv("Commit", [(c.sha, theme.HASH)]))
    out.append(kv("Author", f"{c.author} · {rel_ago(c.timestamp)}"))
    if c.change_id:
        out.append(kv("Change-Id", [(c.change_id, theme.DIM)]))
    text = SYNC_TEXT[state]
    if c.change is not None:
        text = f"{c.change.number} — this commit {text}"
        if state == "outdated":
            ps = c.change.ps_for_revision(c.sha)
            text += f" (local is patch set {ps.number if ps else '?'}, current is {c.change.ps})"
    else:
        text = f"This commit {text}" + (" — push with P" if c.change_id else " (no Change-Id)")
    out.append(kv("Gerrit", [(glyph + " ", gst), (text, PLAIN)]))
    out.append([])
    body = c.body.strip("\n")
    if body:
        out.append(heading("Message"))
        for ln in sanitize_keep_newlines(body).split("\n"):
            for part in wrap(ln, max(10, width - 2)) or [""]:
                out.append([("  " + part, theme.DIM if ln.startswith("Change-Id:") else PLAIN)])
    return out


def comment_block(c: Any, width: int, indent: str = "", show_ps: bool = False) -> list[Line]:
    author = c.author
    head: Line = [(indent, PLAIN), (author.display, theme.author_style(author.username or author.name))]
    when = comment_time(c)
    if when:
        head.append((f" · {rel_ago(when)}", theme.DIM))
    if show_ps:
        head.append((f" · ps{c.patch_set}", theme.DIM))
    if c.unresolved is True:
        head.append((" · unresolved", theme.WARN))
    out = [head]
    out += wrap_lines(c.message, width, PLAIN, indent + "  ")
    return out


def thread_view(
    t: Thread,
    *,
    width: int,
    context: Optional[list[tuple[int, str]]] = None,
    exact: bool = True,
) -> list[Line]:
    glyph = thread_glyph(t)
    state = (
        "CI" if t.is_ci else ("unresolved" if t.unresolved else ("resolved" if t.unresolved is False else "resolution unknown"))
    )
    out: list[Line] = [[glyph, (" ", PLAIN), (t.location, theme.BOLD), (f" · patch set {t.patch_set} · {state}", theme.DIM)]]
    if t.side == "PARENT":
        out.append([("  on the base (left side) of the diff", theme.DIM)])
    out.append([])
    if context:
        nw = max(len(str(n)) for n, _ in context)
        for n, text in context:
            mark = n == t.line
            out.append(
                [
                    (f"{n:>{nw}} ", theme.WARN if mark else theme.DIM),
                    ("│ ", theme.DIM),
                    (sanitize(text), theme.BOLD if mark else PLAIN),
                ]
            )
        out.append([])
    for c in t.comments:
        out += comment_block(c, width)
        out.append([])
    if not exact:
        out.append([("Threads are approximated from SSH data. Add an HTTP password to ~/.netrc for exact", theme.DIM)])
        out.append([("threads, resolution state and threaded replies.", theme.DIM)])
    return out


def message_view(m: ChangeMessage, width: int) -> list[Line]:
    out: list[Line] = [
        [(m.author.display, theme.author_style(m.author.username or m.author.name)), (f" · {rel_ago(m.timestamp)}", theme.DIM)]
        + ([(f" · patch set {m.patch_set}", theme.DIM)] if m.patch_set else [])
    ]
    if m.votes_text:
        out.append([(m.votes_text, theme.BOLD)])
    out.append([])
    out += wrap_lines(m.body, width)
    return out


def job_view(item: Any, width: int) -> list[Line]:
    if isinstance(item, CIHeader):
        glyph, st = CI_GLYPH.get(item.state, ("·", theme.DIM))
        out: list[Line] = [[(glyph + " ", st), (item.title, theme.BOLD)]]
        if item.timestamp:
            out.append([(rel_ago(item.timestamp), theme.DIM)])
        if item.url:
            out.append(kv("URL", [(item.url, theme.LINK)]))
        if item.report:
            r = item.report
            out.append([])
            if r.votes:
                out.append(kv("Votes", r.votes))
            if r.summary:
                out += wrap_lines(r.summary, width)
            out.append([])
            counts: dict[str, int] = {}
            for j in r.jobs:
                counts[j.state] = counts.get(j.state, 0) + 1
            out.append(
                kv(
                    "Jobs",
                    ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + (f", {r.skipped} skipped" if r.skipped else ""),
                )
            )
        elif item.jobs:
            counts = {}
            for j in item.jobs:
                counts[j.state] = counts.get(j.state, 0) + 1
            out.append([])
            out.append(kv("Jobs", ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))))
        return out
    j: CIJob = item
    glyph, st = CI_GLYPH.get(j.state, ("?", theme.DIM))
    out = [[(glyph + " ", st), (j.name, theme.BOLD)], []]
    out.append(kv("Result", [(j.result or j.state, st)]))
    if j.pipeline:
        out.append(kv("Pipeline", j.pipeline))
    if j.duration:
        out.append(kv("Duration", j.duration))
    if j.live:
        if j.elapsed_ms is not None:
            out.append(kv("Elapsed", format_duration_ms(j.elapsed_ms)))
        if j.remaining_ms:
            out.append(kv("Remaining", format_duration_ms(j.remaining_ms)))
        if j.estimated_s:
            out.append(kv("Estimated", format_duration_ms(j.estimated_s * 1000)))
        if j.progress is not None and j.state == "running":
            out.append(kv("Progress", [(progress_bar(j.progress, 30), theme.WARN), (f" {int(j.progress * 100)}%", theme.WARN)]))
        if j.waiting_status:
            out.append(kv("Waiting", j.waiting_status))
    out.append(kv("Voting", "yes" if j.voting else "no"))
    if j.url:
        out.append(kv("Build", [(j.url, theme.LINK)]))
    if j.stream_url and j.state in ("running", "queued"):
        out.append(kv("Log", [(j.stream_url, theme.LINK)]))
    out.append([])
    out.append(
        [("o", theme.KEY), (" open in browser · ", theme.DIM), ("r", theme.KEY), (" CI menu (run-selected this job)", theme.DIM)]
    )
    return out


LOGO = [
    " _                                _ _   ",
    "| |__ _ _____  _ __ _ ___ _ _ _ _(_) |_ ",
    "| / _` |_ / || / _` / -_) '_| '_| |  _|",
    "|_\\__,_/__|\\_, \\__, \\___|_| |_| |_|\\__|",
    "           |__/|___/                    ",
]


def status_dashboard(
    *,
    host: str,
    port: int,
    user: Optional[str],
    project: str,
    branch: str,
    repo: Optional[str],
    local_branch: str,
    server_version: str,
    connected: Optional[bool],
    conn_error: str,
    rest: Optional[str],
    rest_error: str,
    zuul: str,
    dry_run: bool,
    demo: bool,
    counts: list[tuple[str, Optional[int]]],
    base_url: str,
    drafts: Sequence[tuple[int, int]] = (),
) -> list[Line]:
    out: list[Line] = [[(ln, theme.OK)] for ln in LOGO]
    out.append([(f"  lazygerrit {__version__} — a lazygit-style terminal UI for Gerrit", theme.DIM)])
    out.append([])
    if connected is None:
        conn: Line = [("connecting…", theme.DIM)]
    elif connected:
        conn = [("✓ connected", theme.OK), (f" · Gerrit {server_version}", theme.DIM)]
    else:
        conn = [("✗ " + (conn_error.splitlines()[0] if conn_error else "not connected"), theme.BAD)]
    who = f"{user}@" if user else ""
    out.append(kv("Gerrit", [(f"ssh://{who}{host}:{port}", PLAIN), ("  ", PLAIN)] + conn, 10))
    out.append(kv("Web", [(base_url, theme.LINK)], 10))
    out.append(kv("Project", f"{project} · {branch}", 10))
    if repo:
        out.append(kv("Repo", [(repo, PLAIN), (f"  ({local_branch})", theme.DIM)], 10))
    else:
        out.append(kv("Repo", [("no local repository (diffs and the local stack are unavailable)", theme.DIM)], 10))
    if rest:
        out.append(kv("REST", [(f"✓ authenticated as {rest}", theme.OK)], 10))
    else:
        out.append(
            kv(
                "REST",
                [("not configured", theme.WARN), (" — hashtags/prio_high and exact comment threads need it", theme.DIM)],
                10,
            )
        )
        for ln in rest_error.splitlines():
            out.append([("          " + ln, theme.BAD)])
        out.append([("          Create an HTTP password at ", theme.DIM), (f"{base_url}/settings/#HTTPCredentials", theme.LINK)])
        out.append([(f"          and add `machine {host} login <user> password <http-password>` to ~/.netrc", theme.DIM)])
    out.append(kv("Zuul", [(zuul, theme.LINK)] if zuul else [("disabled", theme.DIM)], 10))
    if dry_run:
        out.append(
            kv("Mode", [("DRY RUN", theme.DRY_RUN), (" — write commands are only logged, nothing is sent", theme.DIM)], 10)
        )
    if demo:
        out.append(kv("Mode", [("DEMO", theme.DRY_RUN), (" — offline fake Gerrit with sample data", theme.DIM)], 10))
    out.append([])
    out.append(heading("Dashboard"))
    for name, n in counts:
        out.append([("  ", PLAIN), (name.ljust(10), PLAIN), (str(n) if n is not None else "…", theme.BOLD if n else theme.DIM)])
    if drafts:
        out.append([])
        out.append(heading("Unpublished drafts"))
        for n, k in drafts:
            out.append([("  ", PLAIN), (str(n), theme.NUMBER), (f"  ✎ {k} draft{'s' if k != 1 else ''}", theme.MAGIC)])
        out.append([("  Select the change and press p to publish them.", theme.DIM)])
    out.append([])
    out.append(heading("Legend"))
    out.append(
        [
            ("  ", PLAIN),
            ("✓", theme.OK),
            ("/", theme.DIM),
            ("✗", theme.BAD),
            ("  Verified (CI)   ", PLAIN),
            ("+2", theme.OK_BOLD),
            (" ", PLAIN),
            ("-1", theme.BAD),
            ("  Code-Review, Secondary-Review   ", PLAIN),
            ("●", theme.WARN),
            ("  unresolved comments", PLAIN),
        ]
    )
    out.append(
        [
            ("  Local stack: ", PLAIN),
            ("✓", theme.OK),
            (" in sync  ", PLAIN),
            ("↑", theme.WARN),
            (" differs (push)  ", PLAIN),
            ("↓", theme.INFO),
            (" older patch set  ", PLAIN),
            ("+", theme.BAD),
            (" not on Gerrit  ", PLAIN),
            ("M", theme.MAGIC),
            (" merged", PLAIN),
        ]
    )
    out.append([])
    out.append(heading("Getting around"))
    tips = [
        ("1-5 h/l tab", "switch panels"),
        ("[ ]", "switch tabs"),
        ("enter", "drill down (files, diffs, threads)"),
        ("r", "CI: recheck, slowcheck, regate, run-selected, prio_high"),
        ("a", "vote (Code-Review, Secondary-Review)"),
        ("c / C", "reply to a change (review dialog / $EDITOR)"),
        ("p", "publish your drafts with a vote and message"),
        ("4 enter", "review diffs: c comments a line, ]/[ hunks, ./, files"),
        ("x", "all actions for the selected change"),
        ("y / o", "copy (links, change number…) / open in browser"),
        ("v", "select a range of changes (actions apply to all)"),
        ("?", "all keybindings for the current panel"),
    ]
    for key, desc in tips:
        out.append([("  ", PLAIN), (key.ljust(12), theme.KEY), (desc, PLAIN)])
    return out


def synth_commit_msg(ch: Change, ps: Any, subjects: Optional[dict[str, str]] = None) -> str:
    """Gerrit's /COMMIT_MSG layout built from query data (used when the commit is not local)."""
    subjects = subjects or {}
    when = time.strftime("%a %b %d %H:%M:%S %Y %z", time.localtime(ps.created_on)) if ps.created_on else ""
    lines = []
    parents = ps.parents or []
    for i, p in enumerate(parents):
        label = "Parent:     " if len(parents) == 1 else ("Merge Of:   " if i == 0 else "            ")
        lines.append(f"{label}{p[:8]} ({subjects.get(p, '…')})")
    author, committer = ps.author, ps.uploader
    lines.append(f"Author:     {author.name} <{author.email}>")
    lines.append(f"AuthorDate: {when}")
    lines.append(f"Commit:     {committer.name} <{committer.email}>")
    lines.append(f"CommitDate: {when}")
    lines.append("")
    return "\n".join(lines) + "\n" + ch.commit_message.rstrip("\n") + "\n"


_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]")
ADD_EMPH = Style(fg=2, bg=22)
DEL_EMPH = Style(fg=1, bg=52)


def intraline(old: str, new: str) -> Optional[tuple[list[tuple[int, int]], list[tuple[int, int]]]]:
    """Changed character ranges of a replaced line pair (word granularity); None if too different."""
    if len(old) > 500 or len(new) > 500:
        return None
    a, b = _TOKEN_RE.findall(old), _TOKEN_RE.findall(new)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if sm.ratio() < 0.35:
        return None

    def offsets(tokens: list[str]) -> list[int]:
        pos = [0]
        for t in tokens:
            pos.append(pos[-1] + len(t))
        return pos

    pa, pb = offsets(a), offsets(b)
    ra: list[tuple[int, int]] = []
    rb: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            ra.append((pa[i1], pa[i2]))
        if j2 > j1:
            rb.append((pb[j1], pb[j2]))
    return ra, rb


def highlight_rows(rows: Sequence[DiffRow], diff: str, colored: str) -> dict[int, tuple[Line, Optional[Style]]]:
    """Colors for the code of ``rows`` from ``colored``, which is the unified ``diff`` they were parsed
    from passed through a highlighter that keeps every line (e.g. ``delta --color-only``).

    Returns {row index: (the code's spans without the +/- marker, background of the whole line)}.
    Lines whose text the highlighter changed are left out, so they are shown as usual.
    """
    raw_lines = diff.split("\n")
    out_lines = colored.split("\n")
    styles: dict[int, tuple[Line, Optional[Style]]] = {}
    if diff.rstrip("\n").count("\n") != strip_ansi(colored).rstrip("\n").count("\n"):
        return styles  # not line for line: matching lines up would be guesswork
    for i, r in enumerate(rows):
        if r.kind not in ("add", "del", "ctx") or not 0 <= r.src < min(len(raw_lines), len(out_lines)):
            continue
        raw, out = raw_lines[r.src], out_lines[r.src]
        if raw[:1] not in ("+", "-", " "):
            continue
        body = (ansi_to_lines(drop_first_char(out)) or [[]])[0]
        if plain(body) != sanitize(raw[1:]):
            continue
        head = (ansi_to_lines(out) or [[]])[0]
        bg = head[0][1].bg if head else DEFAULT_COLOR
        styles[i] = (body, Style(bg=bg) if bg != DEFAULT_COLOR else None)
    return styles


def emphasize(text: str, ranges: Sequence[tuple[int, int]], base: Style, emph: Style) -> Line:
    out: Line = []
    pos = 0
    for start, end in ranges:
        if start > pos:
            out.append((text[pos:start], base))
        if end > start:
            out.append((text[start:end], emph))
        pos = max(pos, end)
    if pos < len(text):
        out.append((text[pos:], base))
    return out


def _intraline_ranges(texts: list[str], rows: list[DiffRow]) -> dict[int, list[tuple[int, int]]]:
    """Pair runs of deleted lines with the added lines that follow them and diff each pair."""
    out: dict[int, list[tuple[int, int]]] = {}
    i = 0
    n = len(rows)
    while i < n:
        if rows[i].kind != "del":
            i += 1
            continue
        d0 = i
        while i < n and rows[i].kind == "del":
            i += 1
        a0 = i
        while i < n and rows[i].kind == "add":
            i += 1
        dels, adds = list(range(d0, a0)), list(range(a0, i))
        if not adds or len(dels) > 60 or len(adds) > 60:
            continue
        for di, ai in zip(dels, adds, strict=False):
            res = intraline(texts[di], texts[ai])
            if res is None:
                continue
            ra, rb = res
            if ra:
                out[di] = ra
            if rb:
                out[ai] = rb
    return out


def diff_lines(
    path: str,
    rows: list[DiffRow],
    threads: Sequence[Thread],
    width: int,
    *,
    old_path: Optional[str] = None,
    threads_old: Sequence[Thread] = (),
    drafts: Sequence[Draft] = (),
    new_ps: int = 0,
    base_ps: Optional[int] = None,
    notes: Sequence[Line] = (),
    styled: Optional[dict[int, tuple[Line, Optional[Style]]]] = None,
) -> tuple[list[Line], list[Any]]:
    """Render diff rows with line numbers, intraline highlights, comment threads and drafts.

    ``styled`` (from highlight_rows) replaces the coloring of code lines, e.g. with syntax highlighting.

    ``threads`` belong to ``new_ps`` (REVISION side on the right, PARENT side on the left when the
    base is the parent commit); ``threads_old`` belong to ``base_ps`` and are shown on the left.
    Returns (lines, meta); meta holds a LineRef for code lines, ("hunk", i) for hunk headers and
    ("thread", t) / ("draft", d) for comment lines.
    """
    nums = [r.old or 0 for r in rows] + [r.new or 0 for r in rows] + [1]
    nw = len(str(max(nums)))
    out: list[Line] = []
    meta: list[Any] = []
    new_anchor: dict[int, list[Any]] = {}
    old_anchor: dict[int, list[Any]] = {}
    file_level: list[Any] = []

    def place(item: Any, line: int, old_side: bool) -> None:
        if line <= 0:
            file_level.append(item)
        else:
            (old_anchor if old_side else new_anchor).setdefault(line, []).append(item)

    for t in threads:
        place(t, t.line, t.side == "PARENT")
    for t in threads_old:
        place(t, t.line, True)
    for d in drafts:
        old_side = (d.patch_set == new_ps and d.side == "PARENT") or (base_ps is not None and d.patch_set == base_ps)
        place(d, d.line, old_side)
    placed: set[int] = set()
    box_w = max(20, width - nw * 2 - 6)
    indent = " " * (nw * 2 + 2)

    def add_items(items: list[Any]) -> None:
        items = sorted(items, key=lambda it: 1 if isinstance(it, Draft) else 0)
        for it in items:
            if id(it) in placed:
                continue
            placed.add(id(it))
            if isinstance(it, Draft):
                bar = ("▌ ", theme.MAGIC)
                head: Line = [(indent, PLAIN), bar, ("✎ draft", theme.MAGIC)]
                if it.is_reply:
                    head.append((" reply", theme.DIM))
                head.append((" · unresolved" if it.unresolved else " · resolved", theme.DIM))
                out.append(head)
                meta.append(("draft", it))
                for ln in wrap(sanitize_keep_newlines(it.message), box_w):
                    out.append([(indent, PLAIN), bar, (ln, PLAIN)])
                    meta.append(("draft", it))
                continue
            t: Thread = it
            glyph, gst = thread_glyph(t)
            bar_style = gst if t.unresolved is not False else theme.DIM
            for ci, c in enumerate(t.comments):
                head = [
                    (indent, PLAIN),
                    ("▌ ", bar_style),
                    (c.author.display, theme.author_style(c.author.username or c.author.name)),
                ]
                when = comment_time(c)
                if when:
                    head.append((f" · {rel_ago(when)}", theme.DIM))
                if ci == len(t.comments) - 1 and t.unresolved is not None:
                    head.append((" · unresolved" if t.unresolved else " · resolved", theme.WARN if t.unresolved else theme.DIM))
                out.append(head)
                meta.append(("thread", t))
                for ln in wrap(sanitize_keep_newlines(c.message), box_w):
                    out.append([(indent, PLAIN), ("▌ ", bar_style), (ln, PLAIN)])
                    meta.append(("thread", t))

    for note in notes:
        out.append(list(note))
        meta.append(None)
    if notes:
        out.append([])
        meta.append(None)
    if file_level:
        out.append([("File comments", theme.HEADING)])
        meta.append(None)
        add_items(file_level)
        out.append([])
        meta.append(None)

    texts = [sanitize(r.text) for r in rows]
    emph = _intraline_ranges(texts, rows)
    hunk_no = 0
    for idx, r in enumerate(rows):
        text = texts[idx]
        if r.kind == "meta":
            out.append([(text, theme.BOLD)])
            meta.append(None)
        elif r.kind == "hunk":
            out.append([(text, theme.INFO)])
            meta.append(("hunk", hunk_no))
            hunk_no += 1
        elif r.kind == "nonl":
            out.append([(r.text, theme.DIM)])
            meta.append(None)
        else:
            o = str(r.old) if r.old is not None else ""
            n = str(r.new) if r.new is not None else ""
            gutter = (f"{o:>{nw}} {n:>{nw}} ", theme.DIM)
            hl = styled.get(idx) if styled else None
            fill = hl[1] if hl else None
            if r.kind == "add":
                body = hl[0] if hl else emphasize(text, emph.get(idx, []), theme.OK, ADD_EMPH)
                ln = [gutter, ("+", replace(theme.OK, bg=fill.bg) if fill else theme.OK)] + body
                meta.append(LineRef(path, r.new or 0, None, new_ps, len(text)))
            elif r.kind == "del":
                body = hl[0] if hl else emphasize(text, emph.get(idx, []), theme.BAD, DEL_EMPH)
                ln = [gutter, ("-", replace(theme.BAD, bg=fill.bg) if fill else theme.BAD)] + body
                if base_ps is not None:
                    # Left side of a patch set comparison: that file as it is in the older patch set.
                    meta.append(LineRef(old_path or path, r.old or 0, None, base_ps, len(text)))
                else:
                    meta.append(LineRef(path, r.old or 0, "PARENT", new_ps, len(text)))
            else:
                ln = [gutter, (" ", PLAIN)] + hl[0] if hl else [gutter, (" " + text, PLAIN)]
                meta.append(LineRef(path, r.new or 0, None, new_ps, len(text)))
            if fill is not None and line_width(ln) < width:
                ln.append((" " * (width - line_width(ln)), fill))  # like the terminal's erase-to-end-of-line
            out.append(ln)
            if r.new is not None and r.kind in ("add", "ctx") and r.new in new_anchor:
                add_items(new_anchor[r.new])
            if r.old is not None and r.kind in ("del", "ctx") and r.old in old_anchor:
                add_items(old_anchor[r.old])
    rest = [it for items in list(new_anchor.values()) + list(old_anchor.values()) for it in items if id(it) not in placed]
    if rest:
        out.append([])
        meta.append(None)
        out.append([("Comments outside the shown diff context (z shows the full file)", theme.HEADING)])
        meta.append(None)
        for it in rest:
            out.append([(f"line {it.line}", theme.INFO)])
            meta.append(("draft", it) if isinstance(it, Draft) else ("thread", it))
            add_items([it])
    if not rows:
        out.append([("(no textual changes)", theme.DIM)])
        meta.append(None)
    return out, meta


def diff_code_text(ln: Line) -> str:
    """The code of a line rendered by diff_lines, without the line numbers and the +/- marker."""
    return plain(ln[1:])[1:]


def map_row(probe: tuple[str, float], lines: Sequence[Line], meta: Optional[Sequence[Any]] = None) -> int:
    """The row of ``lines`` that best matches ``probe`` (text, relative position) taken from another
    view of the same diff, to keep the cursor on the same code when switching views.

    With ``meta`` (diff_lines' output) only code lines are candidates, and they match when their code
    appears in the probe; otherwise any line matches that contains the probe. Ties go to the line
    closest to the same relative position, which is also the fallback.
    """
    text, ratio = probe
    n = len(lines)
    if not n:
        return 0
    want = " ".join(text.split())
    best: Optional[tuple[tuple[bool, int, float], int]] = None
    for i, ln in enumerate(lines):
        pos = abs(i / max(1, n - 1) - ratio)
        if meta is not None:
            if i >= len(meta) or not isinstance(meta[i], LineRef):
                continue
            code = " ".join(diff_code_text(ln).split())
            hit = len(code) >= 4 and code in want
            score = (not hit, -len(code) if hit else 0, pos)
        else:
            hit = len(want) >= 4 and want in " ".join(plain(ln).split())
            score = (not hit, 0, pos)
        if best is None or score < best[0]:
            best = (score, i)
    return best[1] if best else round(ratio * (n - 1))


def cmdlog_lines(entries: Sequence[Any], width: int) -> list[Line]:
    out: list[Line] = []
    for e in entries:
        when = time.strftime("%H:%M:%S", time.localtime(e.timestamp))
        if e.status == "info":
            out.append([(when + " ", theme.DIM), (e.title, PLAIN)])
            continue
        style = {"ok": theme.OK, "error": theme.BAD, "dry-run": theme.WARN}.get(e.status, PLAIN)
        label = {"ok": "", "error": " (failed)", "dry-run": " (dry run)"}.get(e.status, "")
        out.append([(when + " ", theme.DIM), (e.title, theme.BOLD), (label, style)])
        out.append([("  " + e.display, theme.INFO)])
        if e.status == "error" and e.output:
            for ln in e.output.splitlines()[:3]:
                out.append([("  " + sanitize(ln), theme.BAD)])
    return out


def is_ci_thread_comment(t: Thread) -> bool:
    return all(is_ci_comment(t.path, c.message) for c in t.comments)
