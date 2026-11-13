"""Gerrit operations with the same semantics as support/tools/rng.py in the src repo."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Optional, Sequence

from .backend import Backend
from .drafts import Draft
from .gerrit import CommandError
from .models import PATCHSET_LEVEL, Change, Thread
from .srcrepo import run_selected_comment

ALREADY_UP_TO_DATE = (
    "Change is already up to date",
    "Change is already based on the latest patch set of the dependent change",
)

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


@dataclass(frozen=True)
class Target:
    number: int
    patch_set: int
    project: str
    subject: str = ""

    @classmethod
    def of(cls, change: Change) -> "Target":
        return cls(change.number, change.ps, change.project, change.subject)

    @property
    def ref(self) -> str:
        return f"{self.number},{self.patch_set}"


class StaleChange(Exception):
    pass


def order_bottom_up(changes: Sequence[Change]) -> list[Change]:
    """Order changes so that a change comes after the selected change it depends on."""
    numbers = {c.number for c in changes}
    deps = {c.number: {r.number for r in c.depends_on if r.number in numbers} for c in changes}
    out: list[Change] = []
    done: set[int] = set()
    pending = list(changes)
    while pending:
        ready = [c for c in pending if deps[c.number] <= done]
        if not ready:  # cycle; cannot happen with real relation chains
            out += pending
            break
        for c in ready:
            out.append(c)
            done.add(c.number)
            pending.remove(c)
    return out


def resolve(be: Backend, changes: Sequence[Change], strict: bool = False, seen: Optional[dict[int, int]] = None) -> list[Target]:
    """Targets for the *current* patch set of each change, queried right before writing (as rng
    does), ordered bottom of the relation chain first.

    With ``strict`` (votes) a newer patch set than the one shown is an error instead of being
    silently targeted, so nobody approves code they have not seen. ``seen`` maps change numbers
    to the newest patch set whose diff was opened; voting on a newer one is refused too.
    """
    fresh: list[Change] = []
    for c in changes:
        cur = be.light(c.number) or c
        if strict and c.ps and cur.ps != c.ps:
            raise StaleChange(
                f"{c.number} has a new patch set ({cur.ps}) since it was loaded here ({c.ps}).\n"
                "Press R to refresh, have a look at the new patch set, then vote again."
            )
        looked = (seen or {}).get(c.number)
        if strict and looked and looked < cur.ps:
            raise StaleChange(
                f"{c.number} is at patch set {cur.ps}, but the newest diff you opened is of patch set {looked}.\n"
                f"Open a diff of patch set {cur.ps}, then vote again."
            )
        fresh.append(cur)
    return [Target.of(c) for c in order_bottom_up(fresh)]


def comment_map(comments: dict[str, list[dict]]) -> list:
    """Comments in the array-of-pairs form rng uses for `gerrit review --json`."""
    return [[path, items] for path, items in comments.items()]


def comment_command(be: Backend, t: Target, command: str) -> str:
    """`rng recheck|slowcheck|regate|priocheck`: post the command as the review message."""
    be.ssh.review(t.number, t.patch_set, message=command, title=f"{command.capitalize()} {t.ref}")
    return f"Posted '{command}' on {t.ref}"


def patchset_comment(be: Backend, t: Target, text: str, unresolved: bool = False, title: str = "") -> str:
    """A patchset-level comment, posted like rng's run_patchset_level_comment_command."""
    item: dict = {"message": text}
    if unresolved:
        item["unresolved"] = True
    be.ssh.review(
        t.number,
        t.patch_set,
        json_input={"comments": comment_map({PATCHSET_LEVEL: [item]})},
        title=title or f"Comment on {t.ref}",
    )
    return f"Commented on {t.ref}"


def inline_comment(
    be: Backend,
    t: Target,
    path: str,
    line: int,
    text: str,
    side: Optional[str] = None,
    unresolved: bool = True,
) -> str:
    item: dict = {"message": text, "unresolved": unresolved}
    if line > 0:
        item["line"] = line
    if side == "PARENT":
        item["side"] = "PARENT"
    be.ssh.review(t.number, t.patch_set, json_input={"comments": comment_map({path: [item]})}, title=f"Comment on {path}")
    loc = f"{path}:{line}" if line else path
    return f"Commented on {loc} in {t.ref}"


def reply(be: Backend, number: int, thread: Thread, text: str, resolve: Optional[bool] = None) -> str:
    """Reply to a thread on the patchset it lives on.

    With REST data the reply is threaded (in_reply_to); otherwise a comment is placed at the
    same location. ``resolve`` None keeps the thread's current state.
    """
    if resolve is None:
        unresolved = bool(thread.unresolved) if thread.unresolved is not None else False
    else:
        unresolved = not resolve
    item: dict = {"message": text, "unresolved": unresolved}
    if thread.last.id:
        item["in_reply_to"] = thread.last.id
    if thread.line and not thread.is_patchset_level:
        item["line"] = thread.line
    if thread.range:
        item["range"] = thread.range
    if thread.side == "PARENT":
        item["side"] = "PARENT"
    be.ssh.review(
        number,
        thread.patch_set,
        json_input={"comments": comment_map({thread.path: [item]})},
        title=f"Reply on {number},{thread.patch_set}",
    )
    return f"Replied on {thread.location}"


def run_selected(be: Backend, t: Target, jobs: Sequence[str]) -> str:
    comment = run_selected_comment(t.project, list(jobs))
    patchset_comment(be, t, comment, title=f"Run-selected on {t.ref}")
    return f"Requested {len(jobs)} job(s) on {t.ref}"


def prio_high(be: Backend, t: Target, reason: str, hashtags: Sequence[str]) -> str:
    """`rng prio_high`: add priority hashtags, then explain why in a patchset-level comment."""
    be.set_hashtags(t.number, add=hashtags)
    patchset_comment(be, t, f"prio_high reason: {reason}", title=f"prio_high on {t.ref}")
    return f"Marked {t.ref} as prio_high"


def current_patch_set(be: Backend, number: int, fallback: int) -> int:
    if be.runner.dry_run:
        return fallback
    try:
        ch = be.light(number)
    except CommandError:
        return fallback
    return ch.ps if ch and ch.ps else fallback


def rebase(be: Backend, t: Target, recheck: bool = False) -> str:
    """`rng rebase [--recheck]` (server-side rebase)."""
    try:
        be.ssh.review(t.number, t.patch_set, rebase=True, title=f"Rebase {t.ref}")
    except CommandError as e:
        if any(p in e.message for p in ALREADY_UP_TO_DATE):
            return f"{t.number}: rebase not required, change is up to date"
        raise
    if recheck:
        new_ps = current_patch_set(be, t.number, t.patch_set + 1)
        comment_command(be, replace(t, patch_set=new_ps), "recheck")
        return f"Rebased and rechecked {t.number}"
    return f"Rebased {t.number}"


def vote(be: Backend, t: Target, label: str, value: int) -> str:
    be.ssh.review(t.number, t.patch_set, labels={label: value}, title=f"{label} {value:+d} on {t.ref}")
    return f"Voted {label}{value:+d} on {t.ref}"


def set_topic(be: Backend, t: Target, topic: Optional[str]) -> str:
    be.ssh.set_topic(t.number, topic, title=f"Set topic on {t.number}")
    return f"Topic of {t.number} set to '{topic}'" if topic else f"Cleared topic of {t.number}"


def add_reviewers(be: Backend, t: Target, reviewers: Sequence[str]) -> str:
    be.ssh.set_reviewers(t.number, add=reviewers, title=f"Add reviewers to {t.number}")
    return f"Added {', '.join(reviewers)} to {t.number}"


def remove_reviewers(be: Backend, t: Target, reviewers: Sequence[str]) -> str:
    be.ssh.set_reviewers(t.number, remove=reviewers, title=f"Remove reviewers from {t.number}")
    return f"Removed {', '.join(reviewers)} from {t.number}"


def hashtags(be: Backend, t: Target, add: Sequence[str] = (), remove: Sequence[str] = ()) -> str:
    be.set_hashtags(t.number, add=add, remove=remove)
    parts = []
    if add:
        parts.append("added " + ", ".join(add))
    if remove:
        parts.append("removed " + ", ".join(remove))
    return f"Hashtags on {t.number}: " + "; ".join(parts)


def set_wip(be: Backend, t: Target, wip: bool) -> str:
    body = {"work_in_progress": True} if wip else {"ready": True}
    be.ssh.review(t.number, t.patch_set, json_input=body, title=("Mark WIP " if wip else "Mark ready ") + t.ref)
    return f"{t.number} is now {'work in progress' if wip else 'ready for review'}"


def abandon(be: Backend, t: Target, message: Optional[str] = None) -> str:
    be.ssh.review(t.number, t.patch_set, abandon=True, message=message or None, title=f"Abandon {t.number}")
    return f"Abandoned {t.number}"


def restore(be: Backend, t: Target) -> str:
    be.ssh.review(t.number, t.patch_set, restore=True, title=f"Restore {t.number}")
    return f"Restored {t.number}"


class PartialPublish(Exception):
    """Some drafts were published before a later `gerrit review` call failed."""

    def __init__(self, published: list[str], error: BaseException):
        self.published = published
        self.error = error
        super().__init__(str(error))


def _drafts_by_path(drafts: Sequence[Draft]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for d in drafts:
        grouped.setdefault(d.path, []).append(d.comment_input())
    return grouped


def publish_review(
    be: Backend,
    change: Change,
    drafts: Sequence[Draft],
    labels: Optional[dict[str, int]] = None,
    message: Optional[str] = None,
    shown_ps: Optional[int] = None,
    published: Optional[list[str]] = None,
) -> str:
    """Publish draft comments, votes and a message like Gerrit's reply dialog.

    Comments are attached to the patch set they were written on, so drafts on older patch sets
    are sent with one `gerrit review` per patch set; votes and the message go to the current
    patch set together with its drafts (a single notification in the common case). Votes are
    refused when the change got a patch set newer than ``shown_ps``. Ids of published drafts are
    appended to ``published`` as soon as each call succeeds.
    """
    done: list[str] = published if published is not None else []
    current = be.light(change.number) or change
    if labels and shown_ps and current.ps != shown_ps:
        raise StaleChange(
            f"{change.number} is at patch set {current.ps}, but you looked at patch set {shown_ps}.\n"
            f"Your drafts are kept. Press R to refresh if needed, open a diff of patch set {current.ps}, "
            "then publish again."
        )
    by_ps: dict[int, list[Draft]] = {}
    for d in drafts:
        by_ps.setdefault(d.patch_set, []).append(d)
    n = change.number
    try:
        for ps in sorted(by_ps):
            if ps == current.ps:
                continue
            group = by_ps[ps]
            be.ssh.review(
                n,
                ps,
                json_input={"comments": comment_map(_drafts_by_path(group))},
                title=f"Publish {len(group)} draft(s) on {n},{ps}",
            )
            done.extend(d.id for d in group)
        body: dict = {}
        mine = by_ps.get(current.ps, [])
        comments = _drafts_by_path(mine)
        if message:
            # Like Gerrit's reply dialog: the message becomes a (resolved) patchset-level comment.
            comments.setdefault(PATCHSET_LEVEL, []).insert(0, {"message": message, "unresolved": False})
        if comments:
            body["comments"] = comment_map(comments)
        if labels:
            body["labels"] = dict(labels)
        if body:
            be.ssh.review(n, current.ps, json_input=body, title=f"Publish review on {n},{current.ps}")
            done.extend(d.id for d in mine)
    except Exception as e:
        if done:
            raise PartialPublish(list(done), e) from e
        raise
    parts = []
    if drafts:
        parts.append(f"{len(drafts)} comment{'s' if len(drafts) != 1 else ''}")
    for label, value in (labels or {}).items():
        parts.append(f"{label}{value:+d}" if value else f"{label} 0")
    if message:
        parts.append("message")
    return f"Published {', '.join(parts) or 'review'} on {n}"


def apply_bottom_up(targets: Sequence[Target], fn: Callable[[Target], str], progress: Progress = _noop) -> list[str]:
    """Apply ``fn`` to targets bottom first (targets are given top first, like rng towers)."""
    results = []
    ordered = list(reversed(targets))
    for i, t in enumerate(ordered, 1):
        progress(f"[{i}/{len(ordered)}] {t.number}")
        results.append(fn(t))
    return results


def tower_targets(be: Backend, change: Change, border: Optional[str] = None) -> list[Target]:
    return [Target.of(c) for c in be.tower(change, border)]


def link(cfg_url: Callable[[int, str], str], t: Target, short: bool = False) -> str:
    """`rng link`: '<number>: <subject> | <url>'."""
    url = cfg_url(t.number, t.project)
    return url if short else f"{t.number}: {t.subject} | {url}"


def link_tower(cfg_url: Callable[[int, str], str], targets: Sequence[Target], short: bool = False) -> str:
    """`rng link_tower`: markdown list, bottom first."""
    return "\n".join(f"- {link(cfg_url, t, short)}" for t in reversed(targets))
