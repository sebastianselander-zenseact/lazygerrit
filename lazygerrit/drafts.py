"""Local review state that survives restarts: draft comments and per-file "reviewed" marks."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .models import COMMIT_MSG, PATCHSET_LEVEL


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
    """Thread-safe JSON-backed store. ``path`` None keeps everything in memory."""

    def __init__(self, path: Optional[Path]):
        self.path = path
        self._lock = threading.RLock()
        self._drafts: dict[str, Draft] = {}
        self._reviewed: dict[str, list[str]] = {}
        self.load_error = ""
        self.load()

    # -- persistence ----------------------------------------------------------------------------

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self.load_error = f"Could not read {self.path}: {e}"
            return
        known = set(Draft.__dataclass_fields__)
        with self._lock:
            for raw in data.get("drafts", []):
                try:
                    d = Draft(**{k: v for k, v in raw.items() if k in known})
                except TypeError:
                    continue
                self._drafts[d.id] = d
            self._reviewed = {k: list(v) for k, v in (data.get("reviewed") or {}).items()}

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            data = {
                "version": 1,
                "drafts": [asdict(d) for d in sorted(self._drafts.values(), key=lambda d: d.created)],
                "reviewed": self._reviewed,
            }
        self.path.parent.mkdir(parents=True, exist_ok=True)
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

    # -- drafts ---------------------------------------------------------------------------------

    def add_draft(self, draft: Draft) -> Draft:
        with self._lock:
            self._drafts[draft.id] = draft
        self.save()
        return draft

    def update_draft(self, draft_id: str, **changes: object) -> Optional[Draft]:
        with self._lock:
            d = self._drafts.get(draft_id)
            if d is None:
                return None
            for k, v in changes.items():
                setattr(d, k, v)
        self.save()
        return d

    def delete_drafts(self, ids: Iterable[str]) -> int:
        with self._lock:
            n = 0
            for i in ids:
                if self._drafts.pop(i, None) is not None:
                    n += 1
        if n:
            self.save()
        return n

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
        with self._lock:
            paths = self._reviewed.setdefault(self._key(change, patch_set), [])
            if path in paths:
                paths.remove(path)
                now = False
            else:
                paths.append(path)
                now = True
            if not paths:
                self._reviewed.pop(self._key(change, patch_set), None)
        self.save()
        return now
