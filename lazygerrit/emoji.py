"""Slack-style emoji: ``:shortcode:`` names (Slack's own plus a workspace's custom ones), completion
while typing, and emoji drawn so that the terminal (and tmux) agree with lazygerrit on their width."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Optional

SLACK_EMOJI_API = "https://slack.com/api/emoji.list"
CACHE_MAX_AGE = 24 * 3600
VS16 = "\ufe0f"
TONES = {f"skin-tone-{n}": chr(0x1F3FB + n - 2) for n in range(2, 7)}  # Slack's skin-tone-1 is no modifier

# Slack favourites: among suggestions that match equally well, these come first (in this order).
POPULAR = """+1 thumbsup white_check_mark heavy_check_mark eyes pray tada raised_hands joy heart 100 fire rocket clap
thinking_face sweat_smile smile slightly_smiling_face ok_hand wave muscle sob rolling_on_the_floor_laughing heart_eyes blush
x warning bug grimacing upside_down_face wink laughing face_palm shrug -1 thumbsdown star sparkles bulb handshake
partying_face sunglasses saluting_face see_no_evil boom construction hammer_and_wrench memo hourglass_flowing_sand
point_right point_up heavy_plus_sign no_entry zap mag melting_face skull exploding_head face_with_rolling_eyes
hugging_face star-struck ballot_box_with_check question exclamation heavy_minus_sign arrow_right""".split()
_POPULAR_RANK = {name: i for i, name in enumerate(POPULAR)}

_NAME = r"[a-z0-9_+'-]"
# A :shortcode: just typed: the opening colon starts the line or follows something that isn't a
# word or another colon (so "a:b:" and "x::y:" stay text, while ":+1::skin-tone-3:" works).
_TYPED = re.compile(rf"(?:^|(?<=[^\w:])):({_NAME}+):$", re.I)
_TYPING = re.compile(rf"(?:^|(?<=[^\w:])):({_NAME}{{2,}})$", re.I)
# Shortcodes in finished text: at least two characters (":x:" is too likely to be code), not
# touching words or colons on either side.
_SHORTCODE = re.compile(rf"(?<![\w:]):({_NAME}{{2,}}):(?::(skin-tone-[2-6]):)?(?![\w:])", re.I)
_CODE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.S)
# What terminals draw inconsistently: variation selector 16 (❤️ is one cell or two), skin tone
# modifiers and zero-width joiners between emoji (one glyph or several). Without them every
# terminal, tmux and ncurses agree with wcwidth().
_TERMINAL = re.compile("[\ufe0f\U0001f3fb-\U0001f3ff]|(?<=[\u2190-\U0010ffff])\u200d")


def terminal_safe(text: str) -> str:
    """Emoji in a form every terminal draws as wide as lazygerrit thinks: ❤️ → ❤, 👍🏽 → 👍, 👩‍💻 → 👩💻."""
    if text.isascii():
        return text
    return _TERMINAL.sub("", text)


@dataclass(frozen=True)
class Emoji:
    name: str
    text: Optional[str]  # None for a workspace image, which can only be written as its :name:
    tones: bool = False  # takes a skin tone
    custom: bool = False  # from the Slack workspace or the config file
    order: int = 0

    @property
    def shortcode(self) -> str:
        return f":{self.name}:"


def with_tone(text: str, tone: str) -> str:
    """An emoji with a skin tone modifier: after its first code point, replacing a VS16 there."""
    rest = text[1:]
    if rest.startswith(VS16):
        rest = rest[1:]
    return text[0] + tone + rest


@lru_cache(maxsize=1)
def standard() -> dict[str, Emoji]:
    """Slack's emoji by name, leaving out those this Python can't measure (newer than its Unicode)."""
    from .emoji_data import DATA

    out: dict[str, Emoji] = {}
    for order, row in enumerate(DATA.splitlines()):
        fields = row.split("\t")
        text = "".join(chr(int(cp, 16)) for cp in fields[0].split("-"))
        if not all(unicodedata.name(ch, None) for ch in text):
            continue
        tones = fields[2:] == ["t"]
        for name in fields[1].split():
            out[name] = Emoji(name, text, tones, False, order)
    return out


@lru_cache(maxsize=1)
def _toneable() -> dict[str, Emoji]:
    return {e.text: e for e in standard().values() if e.tones and e.text}


def _words(name: str) -> tuple[str, ...]:
    return tuple(w for w in re.split(r"[_-]", name) if w)


class EmojiIndex:
    """Looks up and completes emoji names: the config file's ``[custom_emoji]`` first, then the
    Slack workspace's custom emoji, then Slack's standard ones."""

    def __init__(
        self,
        custom: Optional[Mapping[str, Any]] = None,
        workspace: Optional[Mapping[str, str]] = None,
        skin_tone: Any = 1,
    ):
        self.problems: list[str] = []
        self.skin_tone = ""
        if skin_tone not in (None, "", 0, 1):
            if isinstance(skin_tone, int) and 2 <= skin_tone <= 6:
                self.skin_tone = TONES[f"skin-tone-{skin_tone}"]
            else:
                self.problems.append(f"emoji_skin_tone must be 1 (none) to 6, not {skin_tone!r}")
        self._custom: dict[str, str] = {}
        for name, value in (custom or {}).items():
            if isinstance(value, str) and value:
                self._custom[str(name).strip(":").lower()] = value
            else:
                self.problems.append(f"custom_emoji.{name} must be text (or alias:name), not {value!r}")
        self._workspace: dict[str, str] = {}
        self._extra: dict[str, Emoji] = {}
        self._candidates: list[tuple[str, tuple[str, ...], Emoji]] = []
        self.set_workspace(workspace or {})

    @property
    def workspace_size(self) -> int:
        return len(self._workspace)

    def set_workspace(self, workspace: Mapping[str, str]) -> None:
        self._workspace = {str(k).strip(":").lower(): str(v) for k, v in workspace.items()}
        extra: dict[str, Emoji] = {}
        for i, name in enumerate(sorted(set(self._workspace) | set(self._custom))):
            resolved = self._resolve(name)
            if resolved is not None:
                extra[name] = Emoji(name, resolved[0], resolved[1], True, 100_000 + i)
        self._extra = extra
        std = [(n, _words(n), e) for n, e in standard().items() if n not in extra and n not in TONES]
        self._candidates = [(n, _words(n), e) for n, e in extra.items()] + std

    def _resolve(self, name: str, depth: int = 0) -> Optional[tuple[Optional[str], bool]]:
        """(text, takes a tone) of a name; text None for an image. Follows alias:other names."""
        if depth > 8:
            return None
        value = self._custom.get(name)
        if value is None:
            value = self._workspace.get(name)
            if value is not None and not value.startswith("alias:"):
                return None, False  # an image (its URL)
        if value is not None:
            if value.startswith("alias:"):
                return self._resolve(value[6:].strip().strip(":").lower(), depth + 1)
            return value, False
        e = standard().get(name)
        return (e.text, e.tones) if e is not None else None

    def lookup(self, name: str) -> Optional[Emoji]:
        name = name.lower()
        return self._extra.get(name) or standard().get(name)

    def text_for(self, e: Emoji) -> str:
        """What writing this emoji inserts: its characters (in your skin tone), or :name: for an image."""
        if e.text is None:
            return e.shortcode
        if e.tones and self.skin_tone:
            return with_tone(e.text, self.skin_tone)
        return e.text

    def complete(self, query: str, limit: int = 8) -> list[Emoji]:
        """Emoji whose name (or a word of it) starts with ``query``: exact first, then favourites."""
        q = query.lower()
        if not q:
            return []
        scored = []
        for name, words, e in self._candidates:
            if name == q:
                rank = 0
            elif name.startswith(q) or any(w.startswith(q) for w in words):
                rank = 1
            elif len(q) >= 3 and q in name:
                rank = 2
            else:
                continue
            scored.append((rank, _POPULAR_RANK.get(name, len(POPULAR)), e.order, len(name), name, e))
        scored.sort(key=lambda s: s[:5])
        out: list[Emoji] = []
        seen: set[str] = set()
        for *_, e in scored:
            key = e.text if e.text is not None else e.shortcode
            if key in seen:
                continue
            seen.add(key)
            out.append(e)
            if len(out) >= limit:
                break
        return out

    def typed(self, before: str) -> Optional[tuple[int, str]]:
        """If ``before`` (the line up to the cursor) ends with a :shortcode: that was just closed,
        where it starts and what replaces it. ``:skin-tone-N:`` right after an emoji gives it that tone."""
        m = _TYPED.search(before)
        if not m:
            return None
        name = m.group(1).lower()
        if name in TONES:
            found = self._toneable_end(before[: m.start()])
            if found is None:
                return None
            start, base = found
            return start, with_tone(base, TONES[name])
        e = self.lookup(name)
        if e is None:
            return None
        return m.start(), self.text_for(e)

    def _toneable_end(self, text: str) -> Optional[tuple[int, str]]:
        """The emoji (that takes a skin tone) that ``text`` ends with: where it starts and its plain form."""
        for size in range(min(len(text), 12), 0, -1):
            tail = "".join(ch for ch in text[-size:] if ch not in TONES.values())
            for base in (tail, tail[:1] + VS16 + tail[1:]):
                if base in _toneable():
                    return len(text) - size, base
        return None

    def emojize(self, text: str) -> str:
        """Replace the :shortcodes: of finished text (e.g. from the editor) with emoji, except in `code`."""
        out: list[str] = []
        pos = 0
        for m in _CODE.finditer(text):
            out.append(self._emojize_plain(text[pos : m.start()]))
            out.append(m.group(0))
            pos = m.end()
        out.append(self._emojize_plain(text[pos:]))
        return "".join(out)

    def _emojize_plain(self, text: str) -> str:
        def sub(m: re.Match) -> str:
            e = self.lookup(m.group(1))
            if e is None or e.text is None:
                return m.group(0)
            if m.group(2) and e.tones:
                return with_tone(e.text, TONES[m.group(2).lower()])
            return self.text_for(e) + (f":{m.group(2)}:" if m.group(2) and not e.tones else "")

        return _SHORTCODE.sub(sub, text)


def typing_query(before: str) -> Optional[tuple[int, str]]:
    """If ``before`` (the line up to the cursor) ends with a :shortcode being typed (two characters
    or more), where its colon is and the name so far."""
    m = _TYPING.search(before)
    return (m.start(), m.group(1)) if m else None


# --- the Slack workspace's custom emoji ---------------------------------------------------------


class SlackError(Exception):
    pass


def parse_workspace(data: Any) -> dict[str, str]:
    """Custom emoji from Slack's emoji.list response, or a plain {name: image URL or alias:name} map."""
    if isinstance(data, dict) and isinstance(data.get("emoji"), dict):
        data = data["emoji"]
    if not isinstance(data, dict):
        raise ValueError('expected a JSON object like Slack\'s emoji.list response: {"emoji": {"name": "url or alias:name"}}')
    return {str(k).strip(":").lower(): v for k, v in data.items() if isinstance(v, str) and v}


def read_workspace_file(path: str) -> dict[str, str]:
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        return parse_workspace(json.load(fh))


def fetch_workspace(token: str, url: str = SLACK_EMOJI_API, timeout: float = 30) -> dict[str, str]:
    """The workspace's custom emoji from Slack's API (the token needs the emoji:read scope)."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "lazygerrit"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SlackError(f"Could not get the workspace's emoji from Slack: {e}") from e
    if not isinstance(data, dict) or not data.get("ok"):
        error = data.get("error") if isinstance(data, dict) else None
        hint = " (the token needs the emoji:read scope)" if error in ("missing_scope", "not_allowed_token_type") else ""
        raise SlackError(f"Slack did not list the workspace's emoji: {error or 'unexpected response'}{hint}")
    return parse_workspace(data)


def cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "lazygerrit" / "slack-emoji.json"


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def read_cache(token: str, path: Optional[Path] = None) -> tuple[Optional[dict[str, str]], float]:
    """The emoji last fetched with ``token`` (None if there are none) and their age in seconds."""
    try:
        data = json.loads((path or cache_path()).read_text(encoding="utf-8"))
        if data.get("token") != _token_id(token):
            return None, float("inf")
        return parse_workspace(data["emoji"]), time.time() - float(data["fetched"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None, float("inf")


def write_cache(token: str, emoji: Mapping[str, str], path: Optional[Path] = None) -> None:
    path = path or cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".slack-emoji-", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"token": _token_id(token), "fetched": time.time(), "emoji": dict(emoji)}, fh)
    os.replace(tmp, path)


def suggestion_label(e: Emoji, index: EmojiIndex) -> tuple[str, str, str]:
    """(emoji, :name:, note) for showing a suggestion."""
    if e.text is None:
        return " ", e.shortcode, "workspace image: posted as text"
    return index.text_for(e), e.shortcode, "custom" if e.custom else ""
