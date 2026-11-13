"""Local review state that survives restarts: draft comments and per-file "reviewed" marks."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, TypeVar

from .models import COMMIT_MSG, PATCHSET_LEVEL

T = TypeVar("T")


def default_state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "lazygerrit" / "state.json"


@dataclass
class Draft:
    change: int
    patch_set: int
    path: str
    line: int = 0
    side: Optional[str] = None
    message: str = ""
    unresolved: bool = True
    in_reply_to: Optional[str] = None
    range: Optional[dict] = None
    reply_to_text: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created: float = field(default_factory=time.time)

    @property
    def is_reply(self) -> bool:
        return bool(self.in_reply_to or self.reply_to_text)

    @property
    def location(self) -> str:
        if self.path == PATCHSET_LEVEL:
            return "Patchset"
        name = "Commit message" if self.path == COMMIT_MSG else self.path
        if self.range:
            return f"{name}:{self.range.get('start_line')}-{self.range.get('end_line')}"
        return f"{name}:{self.line}" if self.line else name

    @property
    def short_location(self) -> str:
        if self.path == PATCHSET_LEVEL:
            return "Patchset"
        name = "Commit msg" if self.path == COMMIT_MSG else self.path.rsplit("/", 1)[-1]
        return f"{name}:{self.line}" if self.line else name

    def comment_input(self) -> dict:
        """CommentInput for `gerrit review --json`."""
        item: dict = {"message": self.message, "unresolved": self.unresolved}
        if self.line > 0 and self.path != PATCHSET_LEVEL:
            item["line"] = self.line
        if self.side == "PARENT":
            item["side"] = "PARENT"
        if self.range:
            item["range"] = self.range
        if self.in_reply_to:
            item["in_reply_to"] = self.in_reply_to
        return item


class StateStore:
    """Thread- and multi-process-safe JSON store. ``path`` None keeps everything in memory.

    Every change is a locked read-modify-write of the file, so several lazygerrit instances can
    share it without dropping or resurrecting each other's drafts.
    """

    def __init__(self, path: Optional[Path]):
        self.path = path
        self._lock = threading.RLock()
        self._drafts: dict[str, Draft] = {}
        self._reviewed: dict[str, list[str]] = {}
        self.load_error = ""
        self.load()

    # -- persistence ----------------------------------------------------------------------------

    def _parse(self, data: dict) -> tuple[dict[str, Draft], dict[str, list[str]]]:
        known = set(Draft.__dataclass_fields__)
        drafts: dict[str, Draft] = {}
        for raw in data.get("drafts", []):
            try:
                d = Draft(**{k: v for k, v in raw.items() if k in known})
            except TypeError:
                continue
            drafts[d.id] = d
        reviewed = {k: list(v) for k, v in (data.get("reviewed") or {}).items()}
        return drafts, reviewed

    def _read_disk(self) -> tuple[dict[str, Draft], dict[str, list[str]]]:
        assert self.path is not None
        if not self.path.exists():
            return {}, {}
        try:
            return self._parse(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, AttributeError) as e:
            backup = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            try:
                os.replace(self.path, backup)
                self.load_error = f"Could not read {self.path} ({e}); it was moved to {backup}"
            except OSError:
                self.load_error = f"Could not read {self.path}: {e}"
            return {}, {}

    def _write_disk(self, drafts: dict[str, Draft], reviewed: dict[str, list[str]]) -> None:
        assert self.path is not None
        data = {
            "version": 1,
            "drafts": [asdict(d) for d in sorted(drafts.values(), key=lambda d: d.created)],
            "reviewed": reviewed,
        }
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _transact(self, fn: Callable[[dict[str, Draft], dict[str, list[str]]], T], write: bool = True) -> T:
        with self._lock:
            if self.path is None:
                return fn(self._drafts, self._reviewed)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path.with_name(self.path.name + ".lock"), "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    drafts, reviewed = self._read_disk()
                    result = fn(drafts, reviewed)
                    if write:
                        self._write_disk(drafts, reviewed)
                    self._drafts, self._reviewed = drafts, reviewed
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
            return result

    def load(self) -> None:
        """(Re)read the file, picking up drafts written by other lazygerrit instances."""
        if self.path is None:
            return
        if not self.path.exists():
            with self._lock:
                self._drafts, self._reviewed = {}, {}
            return
        try:
            self._transact(lambda drafts, reviewed: None, write=False)
        except OSError as e:
            self.load_error = f"Could not read {self.path}: {e}"

    def snapshot(self) -> tuple[dict[str, Draft], dict[str, list[str]]]:
        """A copy to compare with after load(), to tell whether another instance changed anything."""
        with self._lock:
            return dict(self._drafts), {k: list(v) for k, v in self._reviewed.items()}

    # -- drafts ---------------------------------------------------------------------------------

    def add_draft(self, draft: Draft) -> Draft:
        self._transact(lambda drafts, reviewed: drafts.__setitem__(draft.id, draft))
        return draft

    def update_draft(self, draft_id: str, **changes: object) -> Optional[Draft]:
        def apply(drafts: dict[str, Draft], reviewed: dict[str, list[str]]) -> Optional[Draft]:
            d = drafts.get(draft_id)
            if d is not None:
                for k, v in changes.items():
                    setattr(d, k, v)
            return d

        return self._transact(apply)

    def delete_drafts(self, ids: Iterable[str]) -> int:
        wanted = list(ids)

        def apply(drafts: dict[str, Draft], reviewed: dict[str, list[str]]) -> int:
            return sum(1 for i in wanted if drafts.pop(i, None) is not None)

        return self._transact(apply) if wanted else 0

    def drafts_for(self, change: int) -> list[Draft]:
        with self._lock:
            return sorted((d for d in self._drafts.values() if d.change == change), key=lambda d: d.created)

    def draft(self, draft_id: str) -> Optional[Draft]:
        with self._lock:
            return self._drafts.get(draft_id)

    def draft_counts(self) -> dict[int, int]:
        with self._lock:
            out: dict[int, int] = {}
            for d in self._drafts.values():
                out[d.change] = out.get(d.change, 0) + 1
            return out

    # -- reviewed files -------------------------------------------------------------------------

    @staticmethod
    def _key(change: int, patch_set: int) -> str:
        return f"{change},{patch_set}"

    def is_reviewed(self, change: int, patch_set: int, path: str) -> bool:
        with self._lock:
            return path in self._reviewed.get(self._key(change, patch_set), [])

    def reviewed_paths(self, change: int, patch_set: int) -> set[str]:
        with self._lock:
            return set(self._reviewed.get(self._key(change, patch_set), []))

    def toggle_reviewed(self, change: int, patch_set: int, path: str) -> bool:
        key = self._key(change, patch_set)

        def apply(drafts: dict[str, Draft], reviewed: dict[str, list[str]]) -> bool:
            paths = reviewed.setdefault(key, [])
            if path in paths:
                paths.remove(path)
                now = False
            else:
                paths.append(path)
                now = True
            if not paths:
                reviewed.pop(key, None)
            return now

        return self._transact(apply)
