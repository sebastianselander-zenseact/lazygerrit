"""Customizations for the Zenseact `src` repository (mirrors support/tools/rng.py)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# (comment, menu key, description). Posted as review messages exactly like `rng <cmd>`.
CI_COMMANDS: list[tuple[str, str, str]] = [
    ("recheck", "r", "Re-run the check pipeline"),
    ("slowcheck", "s", "Run the slow-check pipeline"),
    ("regate", "g", "Re-enqueue the change in the gate pipeline"),
    ("priocheck", "p", "Run the check pipeline with priority"),
]

# Fallbacks for support/ci_helpers/gerrit_hashtags.py
_DEFAULT_HASHTAGS = {
    "PATCH_PRIORITY_HIGH_HASHTAG": "prio_high",
    "PATCH_CI_PRIORITY_CHECK_HASHTAG": "ci_prio_check",
    "PATCH_CI_PRIORITY_FLUSH_GATE": "ci_prio_flush_gate",
    "PATCH_LOW_RISK_HASHTAG": "low_risk",
    "ZUUL_VERBOSE_DISPATCH_HASHTAG": "zuul_verbose_dispatch",
    "ZUUL_NO_SMART_RECHECK_HASHTAG": "zuul_no_smart_recheck",
}

RUN_SELECTED_DIRS = ("src_config/premerge", "src_config/premerge/dos65")

_HASHTAG_RE = re.compile(r'^([A-Z][A-Z0-9_]*)\s*=\s*["\']([^"\']+)["\']')
_JOB_ITEM_RE = re.compile(r"^\s*-\s*['\"]?(?P<project>[\w.\-]+)@(?P<job>[\w.\-/]+)['\"]?\s*:?\s*$")


def load_hashtags(repo: Optional[Path]) -> dict[str, str]:
    tags = dict(_DEFAULT_HASHTAGS)
    if repo is None:
        return tags
    path = repo / "support" / "ci_helpers" / "gerrit_hashtags.py"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return tags
    for ln in text.splitlines():
        m = _HASHTAG_RE.match(ln.strip())
        if m:
            tags[m.group(1)] = m.group(2)
    return tags


def prio_high_hashtags(repo: Optional[Path]) -> list[str]:
    t = load_hashtags(repo)
    return [
        t["PATCH_CI_PRIORITY_CHECK_HASHTAG"],
        t["PATCH_CI_PRIORITY_FLUSH_GATE"],
        t["PATCH_PRIORITY_HIGH_HASHTAG"],
    ]


def known_hashtags(repo: Optional[Path]) -> list[str]:
    return sorted(set(load_hashtags(repo).values()))


def parse_run_selected_block(text: str) -> list[tuple[str, str]]:
    """Find (project, job) entries listed under `run-selected:` pipelines in a zuul yaml file."""
    found: list[tuple[str, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip() == "run-selected:":
            base = len(ln) - len(ln.lstrip())
            i += 1
            while i < len(lines):
                cur = lines[i]
                if cur.strip() and (len(cur) - len(cur.lstrip())) <= base:
                    break
                m = _JOB_ITEM_RE.match(cur)
                if m:
                    found.append((m.group("project"), m.group("job")))
                i += 1
            continue
        i += 1
    return found


def discover_run_selected_jobs(repo: Optional[Path]) -> list[str]:
    """Job names (without the `src@` prefix) that can be triggered with a run-selected comment."""
    if repo is None:
        return []
    jobs: set[str] = set()
    for rel in RUN_SELECTED_DIRS:
        d = repo / rel
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.yaml")):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "run-selected:" not in text:
                continue
            for _project, job in parse_run_selected_block(text):
                jobs.add(job)
    return sorted(jobs)


def run_selected_comment(project: str, jobs: list[str]) -> str:
    """Same format as `rng runselected`."""
    comment = "run-selected:"
    for job in jobs:
        name = job.split("@", 1)[1] if "@" in job else job
        comment += f"\n{project}@{name}"
    return comment
