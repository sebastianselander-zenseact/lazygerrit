"""Offline demo and test fixture.

A fake Gerrit SSH server (with Gerrit's own command-line splitting rules and a subset of the
search language), a fake REST API and a fake Zuul, all backed by a real temporary git repository
so that diffs and the local stack behave exactly like they do against the real server.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from .backend import Backend
from .config import Config, Tab
from .gerrit import GerritREST, GerritSSH, RestError, Runner
from .localgit import LocalGit
from .zuul import ZuulClient

HOST = "gerrit.example.com"
ZUUL = "https://zuul.example.com"


def gerrit_split(command_line: str) -> list[str]:
    """Port of Gerrit's CommandFactoryProvider#split: how the server tokenizes an SSH command."""
    out: list[str] = []
    in_quote = in_dbl = False
    buf: list[str] = []
    i, n = 0, len(command_line)
    while i < n:
        b = command_line[i]
        i += 1
        if b in " \t":
            if in_quote or in_dbl:
                buf.append(b)
            elif buf:
                out.append("".join(buf))
                buf = []
            continue
        if b == '"':
            if in_quote:
                buf.append(b)
            else:
                in_dbl = not in_dbl
            continue
        if b == "'":
            if in_dbl:
                buf.append(b)
            else:
                in_quote = not in_quote
            continue
        if b == "\\":
            if in_quote or i == n:
                buf.append(b)
            else:
                buf.append(command_line[i])
                i += 1
            continue
        buf.append(b)
    if buf:
        out.append("".join(buf))
    return out


# --- query language subset ---------------------------------------------------------------------


def tokenize_query(q: str) -> list[str]:
    toks: list[str] = []
    i = 0
    while i < len(q):
        c = q[i]
        if c.isspace():
            i += 1
            continue
        if c in "()":
            toks.append(c)
            i += 1
            continue
        if c == "-" and i + 1 < len(q) and q[i + 1] == "(":
            toks.append("-")
            i += 1
            continue
        j = i
        buf: list[str] = []
        while j < len(q) and not q[j].isspace() and q[j] not in "()":
            if q[j] == '"':
                k = q.find('"', j + 1)
                k = len(q) if k < 0 else k
                buf.append(q[j + 1 : k])
                j = k + 1
                continue
            buf.append(q[j])
            j += 1
        toks.append("".join(buf))
        i = j
    return toks


def parse_query(q: str) -> Any:
    toks = tokenize_query(q)

    def parse_or(pos: int) -> tuple[Any, int]:
        left, pos = parse_and(pos)
        while pos < len(toks) and toks[pos].upper() == "OR":
            right, pos = parse_and(pos + 1)
            left = ("or", left, right)
        return left, pos

    def parse_and(pos: int) -> tuple[Any, int]:
        items = []
        while pos < len(toks) and toks[pos] != ")" and toks[pos].upper() != "OR":
            if toks[pos].upper() == "AND":
                pos += 1
                continue
            node, pos = parse_unary(pos)
            items.append(node)
        return ("and", items), pos

    def parse_unary(pos: int) -> tuple[Any, int]:
        t = toks[pos]
        if t.upper() == "NOT" or t == "-":
            node, pos = parse_unary(pos + 1)
            return ("not", node), pos
        if t == "(":
            node, pos = parse_or(pos + 1)
            if pos < len(toks) and toks[pos] == ")":
                pos += 1
            return node, pos
        if t.startswith("-") and len(t) > 1:
            return ("not", ("term", t[1:])), pos + 1
        return ("term", t), pos + 1

    node, _ = parse_or(0)
    return node


# --- the fake server ---------------------------------------------------------------------------


class GerritFailure(Exception):
    pass


def _acc(name: str, username: str, email: Optional[str] = None) -> dict:
    return {"name": name, "email": email or f"{username}@example.com", "username": username}


ME = _acc("Alex Doe", "s0000001", "alex.doe@example.com")
ROBIN = _acc("Robin Reviewer", "s0000002")
KIM = _acc("Kim Coder", "s0000003")
SAM = _acc("Sam Senior", "s0000004")
ZUUL_BOT = _acc("svc00140", "svc00140", "svc00140@example.com")
CI_BOT = _acc("Gerrit user for CI Platform", "svc00596", "svc00596@example.com")
ACCOUNTS = {a["username"]: a for a in (ME, ROBIN, KIM, SAM, ZUUL_BOT, CI_BOT)}


class FakeGerritServer:
    def __init__(self, repo: Path, project: str = "demo", now: Optional[float] = None, self_user: str = "s0000001"):
        self.repo = repo
        self.project = project
        self.now = now or time.time()
        self.self_user = self_user
        self.changes: dict[int, dict] = {}
        self.lock = threading.RLock()
        self.comment_seq = 0
        self.files_cache: dict[tuple[str, str], list[dict]] = {}
        self.live: dict[tuple[int, int], dict] = {}
        self.calls: list[list[str]] = []
        self.up_to_date: set[int] = set()
        self.branch_tip: Optional[str] = None  # master on the server; it has moved on since the clone

    # -- data helpers -------------------------------------------------------------------------

    def ts(self, hours_ago: float) -> int:
        return int(self.now - hours_ago * 3600)

    def add_change(
        self,
        number: int,
        subject: str,
        owner: dict,
        revisions: Sequence[str],
        *,
        status: str = "NEW",
        wip: bool = False,
        reviewers: Sequence[dict] = (),
        attention: Sequence[str] = (),
        topic: Optional[str] = None,
        hashtags: Sequence[str] = (),
        hours_ago: float = 2,
        body: str = "",
    ) -> dict:
        change_id = "I" + format(number, "x").rjust(40, "0")
        ps_list = []
        for i, rev in enumerate(revisions, 1):
            ps_list.append(
                {
                    "number": i,
                    "revision": rev,
                    "parents": [self.git("rev-parse", rev + "^").strip()],
                    "ref": f"refs/changes/{number % 100:02d}/{number}/{i}",
                    "uploader": owner,
                    "createdOn": self.ts(hours_ago + (len(revisions) - i) * 5),
                    "author": owner,
                    "kind": "REWORK",
                    "approvals": [],
                }
            )
        ch = {
            "project": self.project,
            "branch": "master",
            "id": change_id,
            "number": number,
            "subject": subject,
            "owner": owner,
            "url": f"https://{HOST}/c/{self.project}/+/{number}",
            "commitMessage": f"{subject}\n\n{body}\n\nChange-Id: {change_id}\n"
            if body
            else f"{subject}\n\nChange-Id: {change_id}\n",
            "hashtags": list(hashtags),
            "createdOn": self.ts(hours_ago + 24),
            "lastUpdated": self.ts(hours_ago),
            "open": status == "NEW",
            "status": status,
            "_patchsets": ps_list,
            "_reviewers": [dict(r) for r in reviewers],
            "_attention": list(attention),
            "_messages": [],
            "_comments": [],
        }
        if wip:
            ch["wip"] = True
        if topic:
            ch["topic"] = topic
        for ps in ps_list:
            self.message(ch, owner, f"Uploaded patch set {ps['number']}.", hours_ago + (len(ps_list) - ps["number"]) * 5 + 0.01)
        self.changes[number] = ch
        return ch

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, check=True).stdout

    def message(self, ch: dict, who: dict, text: str, hours_ago: Optional[float] = None) -> None:
        ts = self.ts(hours_ago) if hours_ago is not None else int(time.time())
        ch["_messages"].append({"timestamp": ts, "reviewer": who, "message": text})
        ch["_messages"].sort(key=lambda m: m["timestamp"])
        ch["lastUpdated"] = max(ch["lastUpdated"], ts)

    def vote(self, ch: dict, ps: int, who: dict, label: str, value: int, hours_ago: Optional[float] = None) -> None:
        p = ch["_patchsets"][ps - 1]
        p["approvals"] = [a for a in p["approvals"] if not (a["type"] == label and a["by"]["username"] == who["username"])]
        if value != 0:
            p["approvals"].append(
                {
                    "type": label,
                    "description": label,
                    "value": str(value),
                    "grantedOn": self.ts(hours_ago) if hours_ago is not None else int(time.time()),
                    "by": who,
                }
            )
        if who["username"] not in [r["username"] for r in ch["_reviewers"]] and who["username"] != ch["owner"]["username"]:
            ch["_reviewers"].append(who)

    def comment(
        self,
        ch: dict,
        ps: int,
        who: dict,
        path: str,
        message: str,
        line: int = 0,
        unresolved: bool = False,
        in_reply_to: Optional[str] = None,
        hours_ago: Optional[float] = None,
        side: Optional[str] = None,
        range: Optional[dict] = None,
    ) -> str:
        self.comment_seq += 1
        cid = f"c{self.comment_seq:04d}"
        ts = self.ts(hours_ago) if hours_ago is not None else int(time.time())
        c = {
            "id": cid,
            "path": path,
            "patch_set": ps,
            "author": who,
            "message": message,
            "unresolved": unresolved,
            "updated": time.strftime("%Y-%m-%d %H:%M:%S.000000000", time.gmtime(ts)),
            "_ts": ts,
        }
        if line:
            c["line"] = line
        if in_reply_to:
            c["in_reply_to"] = in_reply_to
        if side:
            c["side"] = side
        if range:
            c["range"] = range
        ch["_comments"].append(c)
        return cid

    def current_ps(self, ch: dict) -> dict:
        return ch["_patchsets"][-1]

    def find_ps_by_revision(self, rev: str) -> Optional[tuple[dict, dict]]:
        for ch in self.changes.values():
            for ps in ch["_patchsets"]:
                if ps["revision"] == rev:
                    return ch, ps
        return None

    def has_unresolved(self, ch: dict) -> bool:
        threads: dict[str, dict] = {}
        by_id = {c["id"]: c for c in ch["_comments"]}

        def root(c: dict) -> str:
            while c.get("in_reply_to") in by_id:
                c = by_id[c["in_reply_to"]]
            return c["id"]

        for c in sorted(ch["_comments"], key=lambda c: c["_ts"]):
            threads[root(c)] = c
        return any(c["unresolved"] for c in threads.values())

    def files_for(self, ps: dict) -> list[dict]:
        key = (ps["parents"][0], ps["revision"])
        if key not in self.files_cache:
            out = self.git("diff", "--numstat", "-M", key[0], key[1])
            status = self.git("diff", "--name-status", "-M", key[0], key[1])
            types = {}
            for ln in status.splitlines():
                parts = ln.split("\t")
                code = parts[0][0]
                types[parts[-1]] = {"A": "ADDED", "D": "DELETED", "R": "RENAMED", "C": "COPIED"}.get(code, "MODIFIED")
            msg = self.git("show", "-s", "--format=%B", ps["revision"])
            files = [{"file": "/COMMIT_MSG", "type": "ADDED", "insertions": len(msg.strip().splitlines()) + 6, "deletions": 0}]
            for ln in out.splitlines():
                ins, dele, path = ln.split("\t", 2)
                files.append({"file": path, "type": types.get(path, "MODIFIED"), "insertions": int(ins), "deletions": -int(dele)})
            self.files_cache[key] = files
        return self.files_cache[key]

    # -- query --------------------------------------------------------------------------------

    def match(self, node: Any, ch: dict) -> bool:
        kind = node[0]
        if kind == "and":
            return all(self.match(n, ch) for n in node[1])
        if kind == "or":
            return self.match(node[1], ch) or self.match(node[2], ch)
        if kind == "not":
            return not self.match(node[1], ch)
        term = node[1]
        key, _, val = term.partition(":") if ":" in term else ("text", "", term)
        key = key.lower()
        if val == "self":
            val = self.self_user
        if key == "owner":
            return ch["owner"]["username"] == val or ch["owner"]["email"] == val
        if key == "reviewer":
            return any(r["username"] == val for r in ch["_reviewers"])
        if key == "attention":
            return val in ch["_attention"]
        if key in ("status", "is"):
            v = val.lower()
            if v == "open":
                return ch["open"]
            if v == "closed":
                return not ch["open"]
            if v == "wip":
                return bool(ch.get("wip"))
            if v == "new":
                return ch["status"] == "NEW"
            return ch["status"].lower() == v
        if key == "project":
            return ch["project"] == val
        if key == "branch":
            return ch["branch"] == val
        if key == "change":
            return str(ch["number"]) == val or ch["id"] == val
        if key == "has":
            return val == "unresolved" and self.has_unresolved(ch)
        if key == "topic":
            return ch.get("topic") == val
        if key == "hashtag":
            return val in ch["hashtags"]
        if key == "limit":
            return True
        return val.lower() in ch["subject"].lower()

    def relations(self, ch: dict) -> tuple[list[dict], list[dict]]:
        depends, needed = [], []
        cur = self.current_ps(ch)
        hit = self.find_ps_by_revision(cur["parents"][0])
        if hit:
            pch, pps = hit
            depends.append(
                {
                    "id": pch["id"],
                    "number": pch["number"],
                    "revision": pps["revision"],
                    "ref": pps["ref"],
                    "isCurrentPatchSet": pps is self.current_ps(pch),
                }
            )
        revs = {ps["revision"] for ps in ch["_patchsets"]}
        for other in self.changes.values():
            ops = self.current_ps(other)
            if other is not ch and ops["parents"][0] in revs:
                needed.append({"id": other["id"], "number": other["number"], "revision": ops["revision"], "ref": ops["ref"]})
        return depends, needed

    def submit_records(self, ch: dict) -> list[dict]:
        if not ch["open"]:
            return [{"status": "CLOSED", "labels": []}]
        approvals = self.current_ps(ch)["approvals"]

        def vals(label: str) -> list[int]:
            return [int(a["value"]) for a in approvals if a["type"] == label]

        labels = []
        cr = vals("Code-Review")
        labels.append({"label": "Code-Review", "status": "REJECT" if -2 in cr else ("OK" if 2 in cr else "NEED")})
        v = vals("Verified")
        labels.append(
            {"label": "Verified", "status": "REJECT" if any(x < 0 for x in v) else ("OK" if any(x > 0 for x in v) else "NEED")}
        )
        sr = vals("Secondary-Review")
        labels.append({"label": "Secondary-Review", "status": "OK" if 1 in sr else "MAY"})
        status = "OK" if all(lab["status"] in ("OK", "MAY") for lab in labels) else "NOT_READY"
        return [{"status": status, "labels": labels}]

    def render_ps(self, ch: dict, ps: dict, flags: set[str], current: bool) -> dict:
        d = {k: v for k, v in ps.items() if k != "approvals"}
        files = self.files_for(ps)
        d["sizeInsertions"] = sum(f["insertions"] for f in files if not f["file"].startswith("/"))
        d["sizeDeletions"] = sum(f["deletions"] for f in files if not f["file"].startswith("/"))
        if current or "--all-approvals" in flags:
            if ps["approvals"]:
                d["approvals"] = ps["approvals"]
        if "--files" in flags:
            d["files"] = files
        if "--comments" in flags:
            comments = [
                {"file": c["path"], "line": c.get("line", 0), "reviewer": c["author"], "message": c["message"]}
                for c in sorted(ch["_comments"], key=lambda c: (c["path"], c.get("line", 0), c["_ts"]))
                if c["patch_set"] == ps["number"]
            ]
            if comments:
                d["comments"] = comments
        return d

    def render(self, ch: dict, flags: set[str]) -> dict:
        d = {k: v for k, v in ch.items() if not k.startswith("_") and k != "commitMessage"}
        if "--commit-message" in flags:
            d["commitMessage"] = ch["commitMessage"]
        if "--current-patch-set" in flags:
            d["currentPatchSet"] = self.render_ps(ch, self.current_ps(ch), flags, True)
        if "--patch-sets" in flags or "--all-approvals" in flags:
            d["patchSets"] = [self.render_ps(ch, ps, flags, ps is self.current_ps(ch)) for ps in ch["_patchsets"]]
        if "--all-reviewers" in flags:
            d["allReviewers"] = ch["_reviewers"]
        if "--submit-records" in flags:
            d["submitRecords"] = self.submit_records(ch)
        if "--dependencies" in flags:
            dep, need = self.relations(ch)
            if dep:
                d["dependsOn"] = dep
            if need:
                d["neededBy"] = need
        if "--comments" in flags:
            d["comments"] = ch["_messages"]
        return d

    def query(self, args: list[str]) -> str:
        flags = {a for a in args if a.startswith("--")}
        start = 0
        rest = []
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("--start", "-S"):
                start = int(args[i + 1])
                i += 2
                continue
            if not a.startswith("-") or a.startswith("-") and ":" in a:
                rest.append(a)
            i += 1
        q = " ".join(rest)
        limit = 500
        for tok in tokenize_query(q):
            if tok.startswith("limit:"):
                limit = int(tok.split(":", 1)[1])
        node = parse_query(q)
        with self.lock:
            matched = [c for c in self.changes.values() if self.match(node, c)]
            matched.sort(key=lambda c: -c["lastUpdated"])
            page = matched[start : start + limit]
            lines = [json.dumps(self.render(c, flags)) for c in page]
        lines.append(
            json.dumps(
                {"type": "stats", "rowCount": len(page), "runTimeMilliseconds": 1, "moreChanges": len(matched) > start + limit}
            )
        )
        return "\n".join(lines) + "\n"

    # -- writes -------------------------------------------------------------------------------

    def _change_ps(self, spec: str) -> tuple[dict, int]:
        num, _, ps = spec.partition(",")
        ch = self.changes.get(int(num))
        if ch is None:
            raise GerritFailure(f'fatal: "{spec}" no such change')
        psn = int(ps) if ps else len(ch["_patchsets"])
        if psn < 1 or psn > len(ch["_patchsets"]):
            raise GerritFailure(f'fatal: "{spec}" no such patch set')
        return ch, psn

    def review(self, args: list[str], stdin: Optional[bytes]) -> str:
        message = None
        labels: dict[str, int] = {}
        spec = None
        flags = set()
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-m", "--message"):
                message = args[i + 1]
                i += 2
                continue
            if a in ("-l", "--label"):
                k, _, v = args[i + 1].partition("=")
                labels[k] = int(v)
                i += 2
                continue
            if a in ("--code-review", "--verified"):
                labels["Code-Review" if a == "--code-review" else "Verified"] = int(args[i + 1])
                i += 2
                continue
            if a in ("-n", "--notify", "-t", "--tag", "-p", "--project", "-b", "--branch"):
                i += 2
                continue
            if a.startswith("--") or a in ("-j", "-s"):
                flags.add({"-j": "--json", "-s": "--submit"}.get(a, a))
            else:
                spec = a
            i += 1
        if spec is None:
            raise GerritFailure('fatal: Argument "{COMMIT | CHANGE,PATCHSET}" is required')
        with self.lock:
            ch, psn = self._change_ps(spec)
            if "--json" in flags:
                body = json.loads((stdin or b"{}").decode("utf-8"))
                message = body.get("message", message)
                labels.update(body.get("labels") or {})
                comments = body.get("comments") or {}
                pairs = comments if isinstance(comments, list) else list(comments.items())
                for path, items in pairs:
                    for item in items:
                        unresolved = item.get("unresolved")
                        if unresolved is None:
                            parent = next((c for c in ch["_comments"] if c["id"] == item.get("in_reply_to")), None)
                            unresolved = parent["unresolved"] if parent else False
                        self.comment(
                            ch,
                            psn,
                            ACCOUNTS[self.self_user],
                            path,
                            item.get("message", ""),
                            item.get("line", 0),
                            unresolved,
                            item.get("in_reply_to"),
                            side=item.get("side"),
                            range=item.get("range"),
                        )
                if body.get("work_in_progress"):
                    ch["wip"] = True
                if body.get("ready"):
                    ch.pop("wip", None)
                n_comments = sum(len(items) for _, items in pairs)
            else:
                n_comments = 0
            if "--rebase" in flags:
                if ch["number"] in self.up_to_date:
                    raise GerritFailure("error: Change is already up to date.")
                ps = self.current_ps(ch)
                new = dict(
                    ps,
                    number=ps["number"] + 1,
                    approvals=[],
                    ref=f"refs/changes/{ch['number'] % 100:02d}/{ch['number']}/{ps['number'] + 1}",
                )
                ch["_patchsets"].append(new)
                self.message(ch, ACCOUNTS[self.self_user], f"Patch Set {new['number']}: Patch Set {ps['number']} was rebased")
                self.up_to_date.add(ch["number"])
            if "--abandon" in flags:
                ch["status"], ch["open"] = "ABANDONED", False
                self.message(ch, ACCOUNTS[self.self_user], "Abandoned" + (f"\n\n{message}" if message else ""))
                return ""
            if "--restore" in flags:
                ch["status"], ch["open"] = "NEW", True
                self.message(ch, ACCOUNTS[self.self_user], "Restored")
                return ""
            for label, value in labels.items():
                self.vote(ch, psn, ACCOUNTS[self.self_user], label, int(value))
            if message is not None or labels or n_comments:
                header = f"Patch Set {psn}:"
                if labels:
                    header += " " + " ".join(f"{k}{int(v):+d}" if int(v) else f"-{k}" for k, v in labels.items())
                text = header
                if n_comments:
                    text += f"\n\n({n_comments} comment{'s' if n_comments != 1 else ''})"
                if message:
                    text += f"\n\n{message}"
                self.message(ch, ACCOUNTS[self.self_user], text)
                if message and message.strip() in ("recheck", "slowcheck", "regate", "priocheck"):
                    pipeline = "slow-check" if message.strip() == "slowcheck" else "check"
                    self.message(
                        ch,
                        ZUUL_BOT,
                        f"Patch Set {psn}:\n\nStarting {pipeline} jobs: {ZUUL}/t/demo/status/change/{ch['number']},{psn}",
                    )
                    self.start_live(ch["number"], psn, pipeline)
        return ""

    def set_topic(self, args: list[str]) -> str:
        topic = None
        nums = []
        i = 0
        while i < len(args):
            if args[i] in ("--topic", "-t"):
                topic = args[i + 1]
                i += 2
                continue
            nums.append(args[i])
            i += 1
        with self.lock:
            for n in nums:
                ch, _ = self._change_ps(n)
                if topic:
                    ch["topic"] = topic
                else:
                    ch.pop("topic", None)
        return ""

    def set_reviewers(self, args: list[str]) -> str:
        add, remove, nums = [], [], []
        i = 0
        while i < len(args):
            if args[i] in ("-a", "--add"):
                add.append(args[i + 1])
                i += 2
                continue
            if args[i] in ("-r", "--remove"):
                remove.append(args[i + 1])
                i += 2
                continue
            if args[i] in ("-p", "--project"):
                i += 2
                continue
            nums.append(args[i])
            i += 1
        with self.lock:
            for n in nums:
                ch, _ = self._change_ps(n)
                for who in add:
                    acc = ACCOUNTS.get(who) or next((a for a in ACCOUNTS.values() if who in (a["email"], a["name"])), None)
                    if acc is None:
                        raise GerritFailure(f"error: {who} does not identify a registered user or group")
                    if acc not in ch["_reviewers"]:
                        ch["_reviewers"].append(acc)
                ch["_reviewers"] = [r for r in ch["_reviewers"] if r["username"] not in remove and r["email"] not in remove]
        return ""

    def handle(self, argv: list[str], stdin: Optional[bytes]) -> tuple[int, str, str]:
        self.calls.append(argv)
        if not argv:
            return 1, "", "fatal: no command"
        cmd, args = argv[0], argv[1:]
        try:
            if cmd == "query":
                return 0, self.query(args), ""
            if cmd == "review":
                return 0, self.review(args, stdin), ""
            if cmd == "set-topic":
                return 0, self.set_topic(args), ""
            if cmd == "set-reviewers":
                return 0, self.set_reviewers(args), ""
            if cmd == "version":
                return 0, "gerrit version 3.11.0-demo\n", ""
        except GerritFailure as e:
            return 1, "", str(e) + "\n"
        return 1, "", f"fatal: gerrit: {cmd}: not found\n"

    # -- zuul ---------------------------------------------------------------------------------

    def start_live(self, number: int, ps: int, pipeline: str = "check", started_s_ago: float = 0) -> None:
        self.live[(number, ps)] = {"pipeline": pipeline, "start": time.time() - started_s_ago}

    def zuul_status(self, number: int, ps: int) -> list[dict]:
        info = self.live.get((number, ps))
        if not info:
            return []
        elapsed = time.time() - info["start"]
        spec = [
            ("demo@zuul_dispatch", 60),
            ("demo@ubuntu_bazel_pylint", 240),
            ("demo@clang_test_and_lint", 420),
            ("demo@ubuntu_bazel_build_gcc_trunk", 900),
            ("demo@spa3_open_loop_hil_smoke_test", 2700),
            ("demo@agx_orin_capture_integration_test", 0),
        ]
        jobs = []
        offset = 0.0
        for name, est in spec:
            if est == 0:
                jobs.append(
                    {
                        "name": name,
                        "result": "SKIPPED",
                        "queued": False,
                        "elapsed_time": None,
                        "estimated_time": None,
                        "remaining_time": None,
                        "voting": True,
                        "pipeline": info["pipeline"],
                        "url": None,
                        "report_url": None,
                        "waiting_status": "dependencies: demo@zuul_dispatch",
                    }
                )
                continue
            started = elapsed - offset
            uid = f"{abs(hash((number, ps, name))) % 16**32:032x}"
            if started <= 0:
                job = {"result": None, "queued": True, "elapsed_time": None, "remaining_time": None, "waiting_status": "executor"}
            elif started < est:
                job = {
                    "result": None,
                    "queued": False,
                    "elapsed_time": int(started * 1000),
                    "remaining_time": int((est - started) * 1000),
                    "waiting_status": None,
                }
            else:
                job = {
                    "result": "SUCCESS",
                    "queued": False,
                    "elapsed_time": int(est * 1000),
                    "remaining_time": 0,
                    "waiting_status": None,
                }
            job.update(
                {
                    "name": name,
                    "estimated_time": est,
                    "voting": True,
                    "pipeline": info["pipeline"],
                    "url": f"stream/{uid}?logfile=console.log",
                    "report_url": f"{ZUUL}/t/demo/build/{uid}",
                    "uuid": uid,
                }
            )
            jobs.append(job)
            if name == "demo@zuul_dispatch":
                offset = est
        return [{"id": f"{number},{ps}", "refs": [{"id": f"{number},{ps}"}], "jobs": jobs, "active": True, "live": True}]

    # -- rest ---------------------------------------------------------------------------------

    def rest(self, method: str, path: str, body: Any) -> Any:
        parts = path.strip("/").split("/")
        with self.lock:
            if parts[:1] == ["accounts"]:
                return ACCOUNTS[self.self_user]
            if len(parts) >= 3 and parts[0] == "changes":
                ch = self.changes.get(int(parts[1]))
                if ch is None:
                    raise RestError(f"HTTP 404 for {method} {path}: Not found")
                if parts[2] == "comments" and method == "GET":
                    out: dict[str, list] = {}
                    for c in sorted(ch["_comments"], key=lambda c: c["_ts"]):
                        item = {k: v for k, v in c.items() if not k.startswith("_") and k != "path"}
                        out.setdefault(c["path"], []).append(item)
                    return out
                if parts[2] == "hashtags" and method == "POST":
                    tags = [t for t in ch["hashtags"] if t not in (body.get("remove") or [])]
                    for t in body.get("add") or []:
                        if t not in tags:
                            tags.append(t)
                    ch["hashtags"] = tags
                    return tags
        raise RestError(f"HTTP 404 for {method} {path}: Not found")


class FakeRunner(Runner):
    """Routes `ssh ... gerrit <cmd>` to the fake server; everything else (git) runs for real."""

    def __init__(self, server: FakeGerritServer, latency: float = 0.0, **kw: Any):
        super().__init__(**kw)
        self.server = server
        self.latency = latency

    def _exec(self, args, input, cwd, env, timeout):  # type: ignore[override]
        args = list(args)
        if args and args[0] == "ssh":
            if "-O" in args:
                return 0, "", ""
            idx = args.index("gerrit")
            if self.latency:
                time.sleep(self.latency)
            # ssh joins its arguments with spaces; the server splits them again.
            return self.server.handle(gerrit_split(" ".join(args[idx + 1 :])), input)
        if args[:1] == ["git"] and "review" in args[:5]:
            return 0, "Switched to a new branch 'review/demo'\n", ""
        if args[:1] == ["git"] and "ls-remote" in args:
            tip = self.server.branch_tip
            return 0, (f"{tip}\trefs/heads/master\n" if tip and "refs/heads/master" in args else ""), ""
        if args[:1] == ["git"] and "fetch" in args:
            return 0, "", ""
        if args[:1] == ["git"] and "push" in args[:5]:
            return 0, "", "remote: SUCCESS\nremote:   https://gerrit.example.com/c/demo/+/100104 [NEW]\n"
        return super()._exec(args, input, cwd, env, timeout)


class FakeREST(GerritREST):
    def __init__(self, server: FakeGerritServer, runner: Runner):
        super().__init__(f"https://{HOST}", "s0000001", "secret", runner)
        self.server = server

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        return self.server.rest(method, path, body)


class FakeZuul(ZuulClient):
    def __init__(self, server: FakeGerritServer):
        super().__init__(ZUUL, "demo")
        self.server = server

    def change_status(self, number: int, patch_set: int) -> list[dict]:
        return self.server.zuul_status(number, patch_set)


# --- demo world --------------------------------------------------------------------------------

_FILES_BASE = {
    "stringent/rapid/crop.py": "import logging\n\nLOG = logging.getLogger(__name__)\n\n\ndef crop(path, start, end):\n    data = open(path).read()\n    return data[start:end]\n",
    "stringent/rapid/spax.py": "from .crop import crop\n\n\ndef run(path):\n    trace = []\n    out = crop(path, 0, 100)\n    trace.append(out)\n    return trace\n",
    "doc/rapid.md": "# Rapid\n\nOpen loop tooling.\n",
}


def _run(repo: Path, *args: str, env: Optional[dict] = None) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env).stdout


def _commit(repo: Path, files: dict[str, str], message: str, when: int, author: dict = ME) -> str:
    for path, content in files.items():
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _run(repo, "add", "-A")
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": author["name"],
            "GIT_AUTHOR_EMAIL": author["email"],
            "GIT_COMMITTER_NAME": author["name"],
            "GIT_COMMITTER_EMAIL": author["email"],
            "GIT_AUTHOR_DATE": f"{when} +0000",
            "GIT_COMMITTER_DATE": f"{when} +0000",
        }
    )
    _run(repo, "commit", "-q", "--no-verify", "-m", message, env=env)
    return _run(repo, "rev-parse", "HEAD").strip()


def _change_id(number: int) -> str:
    return "I" + format(number, "x").rjust(40, "0")


def build_demo_world(root: Path) -> tuple[Path, FakeGerritServer]:
    repo = root / "demo"
    repo.mkdir(parents=True)
    _run(repo, "init", "-q", "-b", "master")
    _run(repo, "config", "user.name", ME["name"])
    _run(repo, "config", "user.email", ME["email"])
    _run(repo, "config", "commit.gpgsign", "false")
    _run(repo, "remote", "add", "origin", f"ssh://{ME['username']}@{HOST}:29418/demo")
    (repo / ".gitreview").write_text(f"[gerrit]\nhost={HOST}\nproject=demo\ndefaultremote=origin\n")
    now = int(time.time())
    base = _commit(repo, dict(_FILES_BASE), "Initial import", now - 86400 * 3)
    _run(repo, "update-ref", "refs/remotes/origin/master", base)

    def cm(subject: str, number: int, body: str = "") -> str:
        text = subject + ("\n\n" + body if body else "") + f"\n\nChange-Id: {_change_id(number)}\n"
        return text

    # Merged change that is still in the local history (rebased away on the server).
    files = dict(_FILES_BASE)
    files["doc/rapid.md"] += "\nSee also: dlt post processing.\n"
    merged = _commit(repo, files, cm("Bump rapiddev22", 100050), now - 86400 * 2)

    files["stringent/rapid/crop.py"] = files["stringent/rapid/crop.py"].replace(
        "    data = open(path).read()\n",
        '    LOG.info("Cropping %s", path)\n    with open(path) as fh:\n        data = fh.read()\n',
    )
    c1_ps1 = _commit(repo, files, cm("Add DLT log cropping", 100101, "First attempt."), now - 30000)
    _run(repo, "reset", "-q", "--soft", "HEAD^")
    files["stringent/rapid/crop.py"] += "\n\ndef crop_dlt(path):\n    return crop(path, 16, -16)\n"
    c1 = _commit(repo, files, cm("Add DLT log cropping", 100101, "Crops the DLT header and footer."), now - 20000)

    files["stringent/rapid/crop.py"] = files["stringent/rapid/crop.py"].replace(
        "    return data[start:end]\n",
        '    if end > len(data):\n        raise ValueError(f"cannot crop {path}")\n    return data[start:end]\n',
    )
    c2 = _commit(repo, files, cm("Raise exception if cropping fails", 100102), now - 15000)

    files["stringent/rapid/spax.py"] = files["stringent/rapid/spax.py"].replace(
        "    trace = []\n    out = crop(path, 0, 100)\n",
        "    out = crop(path, 0, 100)\n    trace = []\n",
    )
    c3_gerrit = _commit(repo, files, cm("Move declarations out one scope", 100103), now - 9000)
    _run(repo, "reset", "-q", "--soft", "HEAD^")
    files["stringent/rapid/spax.py"] += "\n\ndef dry_run(path):\n    return run(path)[:1]\n"
    _commit(repo, files, cm("Move declarations out one scope", 100103), now - 3000)

    files["stringent/rapid/spax.py"] += "\n# TODO: experiment with tracing\n"
    _commit(repo, files, "WIP: experiment with tracing\n\nChange-Id: I" + "e" * 40 + "\n", now - 600)

    # Changes by other people, based on the base commit.
    head = _run(repo, "rev-parse", "HEAD").strip()
    _run(repo, "checkout", "-q", "--detach", base)
    other = dict(_FILES_BASE)
    other["stringent/rapid/spax.py"] = other["stringent/rapid/spax.py"].replace("crop(path, 0, 100)", "crop(path, 0, 128)")
    other["tests/test_spax.py"] = (
        "from stringent.rapid.spax import run\n\n\ndef test_run(tmp_path):\n    f = tmp_path / 'x'\n    f.write_text('x' * 200)\n    assert run(f)\n"
    )
    kim = _commit(
        repo, other, cm("Fix flaky HIL smoke test", 100200, "The smoke test read past the end of the log."), now - 40000, KIM
    )
    _run(repo, "checkout", "-q", "--detach", base)
    other2 = dict(_FILES_BASE)
    other2["doc/rapid.md"] = "# Rapid\n\nOpen loop tooling for the zuul dispatcher.\n\n## Usage\n\nRun `rapid --help`.\n"
    robin = _commit(repo, other2, cm("Refactor zuul dispatcher docs", 100201), now - 50000, ROBIN)
    _run(repo, "checkout", "-q", "--detach", base)
    other3 = dict(_FILES_BASE)
    other3["doc/scenario.md"] = "# Scenario format v2\n\nDraft.\n"
    wip = _commit(repo, other3, cm("Draft: new scenario format", 100110), now - 70000)
    _run(repo, "checkout", "-q", "--detach", base)
    other4 = dict(_FILES_BASE)
    other4["doc/rapid.md"] += "\nObsolete.\n"
    abandoned = _commit(repo, other4, cm("Old approach to cropping", 100090), now - 90000)
    # master on the server got a commit after the clone, which conflicts with Kim's change.
    _run(repo, "checkout", "-q", "--detach", base)
    upstream_files = dict(_FILES_BASE)
    upstream_files["tests/test_spax.py"] = (
        "from stringent.rapid.spax import run\n\n\ndef test_empty(tmp_path):\n    f = tmp_path / 'x'\n    f.write_text('')\n    assert run(f) == ['']\n"
    )
    upstream = _commit(repo, upstream_files, "Add spax tests", now - 3600, SAM)
    _run(repo, "checkout", "-q", "-B", "review/100103", head)
    refs = [
        (c1_ps1, "c1ps1"),
        (c3_gerrit, "c3"),
        (kim, "kim"),
        (robin, "robin"),
        (wip, "wip"),
        (abandoned, "ab"),
        (upstream, "upstream"),
    ]
    for rev, name in refs:
        _run(repo, "update-ref", f"refs/demo/{name}", rev)

    s = FakeGerritServer(repo)
    s.self_user = ME["username"]
    s.branch_tip = upstream
    ch = s.add_change(100050, "Bump rapiddev22", ME, [merged], status="MERGED", hours_ago=40)
    s.vote(ch, 1, SAM, "Code-Review", 2, 41)
    s.vote(ch, 1, ZUUL_BOT, "Verified", 2, 40)
    s.message(ch, ZUUL_BOT, "Change has been successfully merged", 40)

    ch = s.add_change(
        100101, "Add DLT log cropping", ME, [c1_ps1, c1], reviewers=[ROBIN, ZUUL_BOT], hours_ago=5, topic="dlt-postprocessing"
    )
    s.vote(ch, 2, ROBIN, "Code-Review", 1, 4)
    s.vote(ch, 2, ZUUL_BOT, "Verified", 1, 4.5)
    s.message(ch, ZUUL_BOT, f"Patch Set 2:\n\nStarting check jobs: {ZUUL}/t/demo/status/change/100101,2", 5)
    s.message(ch, ZUUL_BOT, _report(2, True, now), 4.5)
    s.message(ch, ROBIN, "Patch Set 2: Code-Review+1\n\nLooks good, one nit.\n\n(1 comment)", 4)
    s.comment(ch, 2, ROBIN, "stringent/rapid/crop.py", "Nit: use a constant for the header size?", 14, True, hours_ago=4)
    s.comment(ch, 1, ROBIN, "stringent/rapid/crop.py", "Use a context manager here.", 7, True, hours_ago=20)
    first = s.comment(ch, 1, ROBIN, "/COMMIT_MSG", "Please describe what is cropped.", 8, True, hours_ago=20)
    s.comment(ch, 1, ME, "/COMMIT_MSG", "Done", 8, False, in_reply_to=first, hours_ago=6)
    s.comment(ch, 2, ME, "/PATCHSET_LEVEL", "recheck", hours_ago=5.1)

    ch = s.add_change(
        100102,
        "Raise exception if cropping fails",
        ME,
        [c2],
        reviewers=[ZUUL_BOT, CI_BOT],
        hours_ago=3,
        topic="dlt-postprocessing",
    )
    s.vote(ch, 1, ZUUL_BOT, "Verified", -1, 3)
    s.message(ch, ZUUL_BOT, f"Patch Set 1:\n\nStarting check jobs: {ZUUL}/t/demo/status/change/100102,1", 4)
    s.message(ch, CI_BOT, _dispatch(100102, 1), 3.9)
    s.message(ch, ZUUL_BOT, _report(1, False, now), 3)

    ch = s.add_change(100103, "Move declarations out one scope", ME, [c3_gerrit], reviewers=[ZUUL_BOT], hours_ago=1)
    s.message(ch, ZUUL_BOT, f"Patch Set 1:\n\nStarting check jobs: {ZUUL}/t/demo/status/change/100103,1", 0.3)
    s.start_live(100103, 1, "check", started_s_ago=600)

    ch = s.add_change(100110, "Draft: new scenario format", ME, [wip], wip=True, hours_ago=20)

    ch = s.add_change(100090, "Old approach to cropping", ME, [abandoned], status="ABANDONED", hours_ago=60)

    ch = s.add_change(
        100200,
        "Fix flaky HIL smoke test",
        KIM,
        [kim],
        reviewers=[ME, SAM, ZUUL_BOT],
        attention=[ME["username"]],
        hours_ago=2,
        hashtags=["low_risk"],
    )
    s.vote(ch, 1, SAM, "Code-Review", 1, 2.5)
    s.vote(ch, 1, ZUUL_BOT, "Verified", 1, 2.2)
    s.message(ch, ZUUL_BOT, _report(1, True, now), 2.2)
    q = s.comment(ch, 1, SAM, "stringent/rapid/spax.py", "Why 128? Please add a comment.", 6, True, hours_ago=2.4)
    s.comment(
        ch,
        1,
        KIM,
        "stringent/rapid/spax.py",
        "The header is 128 bytes on the new loggers.",
        6,
        True,
        in_reply_to=q,
        hours_ago=2.1,
    )
    s.message(ch, SAM, "Patch Set 1: Code-Review+1\n\n(1 comment)", 2.4)

    ch = s.add_change(100201, "Refactor zuul dispatcher docs", ROBIN, [robin], reviewers=[ME, ZUUL_BOT], hours_ago=8)
    s.vote(ch, 1, ZUUL_BOT, "Verified", 1, 7)
    s.vote(ch, 1, SAM, "Code-Review", 2, 6)
    s.message(ch, ZUUL_BOT, _report(1, True, now), 7)
    merge_failed = (
        "Patch Set 1:\n\nMerge Failed.\n\nThis change or one of its cross-repo dependencies was unable to be "
        "automatically merged with the current state of its repository. Please rebase the change and upload a new "
        "patchset.\nWarning:\n  Error merging demo for 100201,1"
    )
    s.message(ch, ZUUL_BOT, merge_failed, 6.5)
    return repo, s


def _report(ps: int, ok: bool, now: int) -> str:
    jobs = [
        ("demo@zuul_dispatch", "SUCCESS", "2m 44s"),
        ("demo@ubuntu_bazel_pylint", "SUCCESS", "3m 32s"),
        ("demo@clang_test_and_lint", "SUCCESS", "2m 42s"),
        ("demo@ubuntu_bazel_build_gcc_trunk", "SUCCESS", "12m 28s"),
        ("demo@spa3_open_loop_hil_smoke_test", "SUCCESS" if ok else "FAILURE", "42m 00s" if ok else "1h 27m 04s"),
    ]
    head = f"Patch Set {ps}: Verified{'+1' if ok else '-1'}\n\n"
    head += (
        "Build succeeded (check pipeline).\n"
        if ok
        else 'Check failed, check why, rebase will invalidate smart-recheck. Rerun by a comment "recheck"\n'
    )
    head += f"{ZUUL}/t/demo/buildset/{'a' * 32}\n\n"
    lines = [f"- {n} {ZUUL}/t/demo/build/{abs(hash(n)) % 16**32:032x} : {r} in {d}" for n, r, d in jobs]
    return head + "\n".join(lines) + "\nSkipped 203 jobs"


def _dispatch(number: int, ps: int) -> str:
    return (
        f"Patch Set {ps}:\n\n### Zuul dispatch summary\nDispatched `check` pipeline on change `{number},{ps}`\n\n"
        "#### Triggered jobs\n| |Job|Reason|\n|:---:|:---|:---|\n"
        "|🟦|`demo@ubuntu_bazel_pylint`|Patchset (changes matched 1 trigger rule)|\n"
        "|🟦|`demo@spa3_open_loop_hil_smoke_test`|Patchset (changes matched 1 trigger rule)|\n"
    )


def create_demo(
    root: Optional[Path] = None,
    *,
    latency: float = 0.05,
    with_rest: bool = True,
    dry_run: bool = False,
) -> tuple[Backend, Config, FakeGerritServer, Path]:
    """Build the demo world. Returns (backend, config, server, tmp root)."""
    tmp = root or Path(tempfile.mkdtemp(prefix="lazygerrit-demo-"))
    repo, server = build_demo_world(tmp)
    cfg = Config(
        repo=repo,
        host=HOST,
        port=29418,
        user=ME["username"],
        project="demo",
        branch="master",
        zuul_url=ZUUL,
        zuul_tenant="demo",
        tabs=[
            Tab("Outgoing", "owner:self status:open -is:wip project:{project}"),
            Tab("Incoming", "reviewer:self -owner:self -is:wip status:open project:{project}"),
            Tab("WIP", "owner:self is:wip status:open project:{project}"),
            Tab("Attention", "attention:self status:open"),
            Tab("Closed", "owner:self status:closed project:{project}", limit=25),
            Tab("Query", ""),
        ],
        ssh_multiplex=False,
        dry_run=dry_run,
        bot_usernames=["svc00140", "svc00596"],
    )
    runner = FakeRunner(server, latency=latency, dry_run=dry_run)
    ssh = GerritSSH(cfg.host, cfg.port, runner, user=cfg.user, multiplex=False)
    rest = FakeREST(server, runner) if with_rest else None
    git = LocalGit(repo, runner, cfg.remote, cfg.branch)
    backend = Backend(cfg, runner, ssh, rest, FakeZuul(server), git)
    return backend, cfg, server, tmp


def cleanup_demo(tmp: Path) -> None:
    shutil.rmtree(tmp, ignore_errors=True)
