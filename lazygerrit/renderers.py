"""Diff renderers: show diffs with delta, difftastic, ``git diff --color-words``, … instead of the
review view. They are configured like lazygit's ``git.diffRenderers``:

* ``stdinFilter`` (the default): git's diff is piped through ``command`` (delta, diff-so-fancy, ydiff)
* ``extDiff``: git runs ``command`` as its external diff program (difftastic)
* ``rawGit``: git's own colored diff with extra ``args`` (``--color-words``)
* ``builtin``: lazygerrit's review view, which shows and adds comments
"""

from __future__ import annotations

import json
import operator
import os
import re
import shlex
import shutil
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, Sequence

if TYPE_CHECKING:
    from .localgit import LocalGit

_TYPES = {"stdinfilter": "stdinFilter", "extdiff": "extDiff", "rawgit": "rawGit", "builtin": "builtin"}


class RendererError(Exception):
    pass


@dataclass
class DiffRenderer:
    type: str = "stdinFilter"
    command: str = ""
    name: str = ""
    color_arg: str = "always"
    args: list[str] = field(default_factory=list)

    @property
    def builtin(self) -> bool:
        return self.type == "builtin"

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        if self.builtin:
            return "review view"
        if self.type == "rawGit":
            return " ".join(["git", *self.args])
        words = self.command.split()
        return os.path.basename(words[0]) if words else "diff.external"


BUILTIN = DiffRenderer(type="builtin")


def _norm(key: str) -> str:
    return key.replace("_", "").replace("-", "").lower()


def parse_renderers(raw: Any) -> tuple[list[DiffRenderer], list[str]]:
    """Renderers from the config file (lazygit's field names work too). Returns (renderers, problems)."""
    if isinstance(raw, (dict, str)):
        raw = [raw]
    if not isinstance(raw, list):
        return [], ["diff_renderers must be a list of [[diff_renderers]] tables"]
    out: list[DiffRenderer] = []
    problems: list[str] = []
    for i, item in enumerate(raw, 1):
        where = f"diff_renderers entry {i}"
        if isinstance(item, str):
            item = {"command": item}
        if not isinstance(item, dict):
            problems.append(f"{where} must be a table with a command")
            continue
        data = {_norm(str(k)): v for k, v in item.items()}
        unknown = sorted(set(data) - {"type", "command", "name", "colorarg", "args"})
        if unknown:
            problems.append(f"{where}: unknown setting {', '.join(unknown)} (known: type, command, name, colorArg, args)")
        kind = _TYPES.get(_norm(str(data.get("type") or "stdinFilter")))
        if kind is None:
            problems.append(f"{where}: unknown type {data.get('type')!r} (use stdinFilter, extDiff, rawGit or builtin)")
            continue
        args = data.get("args") or []
        if isinstance(args, str):
            args = shlex.split(args)
        color = str(data.get("colorarg") or "always")
        if color not in ("always", "never"):
            problems.append(f"{where}: colorArg must be always or never")
            color = "always"
        r = DiffRenderer(kind, str(data.get("command") or "").strip(), str(data.get("name") or ""), color, [str(a) for a in args])
        if kind == "stdinFilter" and not r.command:
            problems.append(f"{where}: a stdinFilter renderer needs a command, e.g. delta --paging=never")
            continue
        out.append(r)
    return out, problems


def detect_color_scheme(setting: str = "auto", env: Optional[dict] = None) -> str:
    """``dark`` or ``light``: the setting, else what $COLORFGBG says about the background, else dark."""
    if setting in ("dark", "light"):
        return setting
    fgbg = (env if env is not None else os.environ).get("COLORFGBG", "")
    try:
        bg = int(fgbg.rsplit(";", 1)[-1])
    except ValueError:
        return "dark"
    return "light" if bg == 7 or 9 <= bg <= 15 else "dark"


def template_values(width: int, color_scheme: str, context: int) -> dict[str, Any]:
    """The variables a renderer command can use (the same as in lazygit)."""
    return {"width": width, "colorScheme": color_scheme, "columnWidth": width // 2 - 6, "diffContext": context}


# -- lazygit's command templates: {{width}}, {{.colorScheme}}, {{if gt .width 160}}…{{else}}…{{end}} --

_ACTION_RE = re.compile(r"\{\{(-\s)?\s*(.*?)\s*(\s-)?\}\}", re.S)
_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|`[^`]*`|[()]|[^\s()]+')


def _truthy(v: Any) -> bool:
    return bool(v)


def _compare(op: Callable[[Any, Any], bool]) -> Callable[[list[Any]], Any]:
    def run(args: list[Any]) -> Any:
        if len(args) != 2:
            raise RendererError("comparisons take two values, e.g. {{if gt .width 160}}")
        a, b = args
        if isinstance(a, bool) or isinstance(b, bool) or type(a) is not type(b):
            if op not in (operator.eq, operator.ne):
                raise RendererError(f"cannot compare {a!r} with {b!r}")
        return op(a, b)

    return run


def _and(args: list[Any]) -> Any:
    for a in args[:-1]:
        if not _truthy(a):
            return a
    return args[-1] if args else True


def _or(args: list[Any]) -> Any:
    for a in args[:-1]:
        if _truthy(a):
            return a
    return args[-1] if args else False


def _not(args: list[Any]) -> Any:
    if len(args) != 1:
        raise RendererError("not takes one value")
    return not _truthy(args[0])


_FUNCS: dict[str, Callable[[list[Any]], Any]] = {
    "eq": _compare(operator.eq),
    "ne": _compare(operator.ne),
    "lt": _compare(operator.lt),
    "le": _compare(operator.le),
    "gt": _compare(operator.gt),
    "ge": _compare(operator.ge),
    "and": _and,
    "or": _or,
    "not": _not,
}


def _parse_terms(tokens: list[str], i: int) -> tuple[list[Any], int]:
    terms: list[Any] = []
    while i < len(tokens) and tokens[i] != ")":
        if tokens[i] == "(":
            sub, i = _parse_terms(tokens, i + 1)
            if i >= len(tokens):
                raise RendererError("missing ) in template")
            terms.append(sub)
        else:
            terms.append(tokens[i])
        i += 1
    return terms, i


def _eval_term(t: Any, values: dict[str, Any]) -> Any:
    if isinstance(t, list):
        return _eval_terms(t, values)
    if t.startswith('"'):
        try:
            return json.loads(t)
        except ValueError:
            raise RendererError(f"bad string {t} in template") from None
    if t.startswith("`"):
        return t[1:-1]
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if t in ("true", "false"):
        return t == "true"
    name = t[1:] if t.startswith(".") else t
    if name in values:
        return values[name]
    known = ", ".join("{{" + k + "}}" for k in values)
    raise RendererError(f"unknown template variable {t} (available: {known})")


def _eval_terms(terms: list[Any], values: dict[str, Any]) -> Any:
    if not terms:
        raise RendererError("empty {{ }} in template")
    head = terms[0]
    if isinstance(head, str) and head in _FUNCS:
        return _FUNCS[head]([_eval_term(t, values) for t in terms[1:]])
    if len(terms) > 1:
        raise RendererError(f"cannot evaluate {{{{{' '.join(str(t) for t in terms)}}}}}")
    return _eval_term(head, values)


def _eval(expr: str, values: dict[str, Any]) -> Any:
    tokens = _TOKEN_RE.findall(expr)
    terms, i = _parse_terms(tokens, 0)
    if i < len(tokens):
        raise RendererError(f"unbalanced ) in {{{{{expr}}}}}")
    return _eval_terms(terms, values)


def _format(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def expand_command(command: str, values: dict[str, Any]) -> str:
    """Resolve lazygit's template syntax in a renderer command: variables and if/else/end."""
    parts: list[tuple[str, str]] = []
    pos = 0
    trim_next = False
    for m in _ACTION_RE.finditer(command):
        text = command[pos : m.start()]
        if trim_next:
            text = text.lstrip()
        if m.group(1):
            text = text.rstrip()
        parts.append(("text", text))
        parts.append(("action", m.group(2)))
        pos = m.end()
        trim_next = bool(m.group(3))
    tail = command[pos:]
    parts.append(("text", tail.lstrip() if trim_next else tail))
    out, i, stop = _run(parts, 0, values, True)
    if stop is not None:
        raise RendererError(f"{{{{{stop}}}}} without {{{{if}}}} in {command!r}")
    return out


def _run(parts: list[tuple[str, str]], i: int, values: dict[str, Any], active: bool) -> tuple[str, int, Optional[str]]:
    """Expand until an {{else…}}/{{end}} (returned as the third value) or the end."""
    out: list[str] = []
    while i < len(parts):
        kind, text = parts[i]
        i += 1
        if kind == "text":
            if active:
                out.append(text)
            continue
        head = text.split(None, 1)[0] if text else ""
        if head in ("end", "else"):
            return "".join(out), i, text
        if head.startswith("/*"):
            continue
        if head != "if":
            if active:
                out.append(_format(_eval(text, values)))
            continue
        cond: Optional[str] = text[2:].strip()
        taken = False
        while True:
            take = active and not taken and (cond is None or _truthy(_eval(cond, values)))
            body, i, stop = _run(parts, i, values, take)
            if take:
                out.append(body)
                taken = True
            if stop is None:
                raise RendererError("{{if}} without {{end}}")
            if stop == "end":
                break
            if cond is None:
                raise RendererError("{{else}} after {{else}}")
            rest = stop[4:].strip()
            if rest.startswith("if ") or rest == "if":
                cond = rest[2:].strip()
            elif rest:
                raise RendererError(f"unsupported {{{{{stop}}}}}")
            else:
                cond = None
    return "".join(out), i, None


# -- syntax highlighting for the review view ----------------------------------------------------

DEFAULT_HIGHLIGHTER = "delta --color-only --paging=never"


def syntax_command(setting: Any) -> Optional[str]:
    """The command that colors the review view's diffs: it must keep every line of the diff as it
    is (only adding colors), like ``delta --color-only``. ``auto`` uses delta when it is installed."""
    text = str(setting).strip().lower() if setting is not None else "off"
    if setting is True or text in ("auto", "on", "true", "yes"):
        return DEFAULT_HIGHLIGHTER if shutil.which("delta") else None
    if setting is False or text in ("", "off", "none", "false", "no"):
        return None
    return str(setting).strip()


def run_highlighter(git: "LocalGit", command: str, diff: str, *, color_scheme: str, timeout: float = 30) -> tuple[int, str]:
    """Pass a unified diff through the highlighter. Returns (status, colored diff)."""
    env = _env(git, 200, 50, own_external=False)
    res = git.runner.run_tty(
        ["sh", "-c", command],
        size=(200, 50),
        input=diff.encode(),
        cwd=str(git.repo) if git.repo else None,
        env=env,
        timeout=timeout,
        dark=color_scheme != "light",
        title="Syntax highlighting",
        display=command,
    )
    return res.returncode, res.stdout


# -- running renderers ------------------------------------------------------------------------


def _env(git: "LocalGit", width: int, height: int, own_external: bool) -> dict:
    env = dict(git.env())
    env.update(COLUMNS=str(width), LINES=str(height), PAGER="cat", GIT_PAGER="cat", DELTA_PAGER="cat", BAT_PAGER="cat")
    # lazygerrit draws with 256 colors: without COLORTERM=truecolor, delta and friends use their own
    # 256-color palette instead of 24-bit colors that would have to be approximated (badly, for the
    # dark green/red backgrounds of added/removed lines).
    env.pop("COLORTERM", None)
    if env.get("TERM", "dumb") == "dumb":
        env["TERM"] = "xterm-256color"
    if own_external:
        env.pop("GIT_EXTERNAL_DIFF", None)  # it would win over our diff.external
    return env


def render(
    git: "LocalGit",
    r: DiffRenderer,
    *,
    base: str = "",
    rev: str = "",
    paths: Sequence[str] = (),
    texts: Optional[tuple[str, str, str]] = None,
    width: int,
    height: int,
    context: int,
    color_scheme: str,
    timeout: float = 60,
) -> tuple[int, str]:
    """Render the diff of ``paths`` between commits ``base`` and ``rev``, or of ``texts`` (name, old,
    new), with ``r``. Returns (status, output); a non-zero status means the renderer failed and the
    output usually says why."""
    command = expand_command(r.command, template_values(width, color_scheme, context)) if r.command else ""
    env = _env(git, width, height, own_external=r.type == "extDiff" and bool(command))
    size = (width, height)
    dark = color_scheme != "light"
    title = f"Diff with {r.label}"
    with tempfile.TemporaryDirectory(prefix="lazygerrit-diff-") if texts else nullcontext() as tmp:
        if texts is not None and tmp is not None:
            name, old, new = texts
            for side, text in (("a", old), ("b", new)):
                p = Path(tmp, side, name)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
            cwd = tmp
            what = ["--no-index", "--no-prefix", f"a/{name}", f"b/{name}"]
        else:
            cwd = str(git.repo)
            what = ["--find-renames", base, rev, "--", *paths]
        ok = (0, 1)  # git diff --no-index exits with 1 when there are differences
        if r.type == "extDiff":
            cmd = ["git", "--no-pager"] + (["-c", f"diff.external={command}"] if command else [])
            cmd += ["diff", "--ext-diff", f"-U{context}", *what]
            res = git.runner.run_tty(
                cmd, size=size, cwd=cwd, env=env, timeout=timeout, ok=ok, dark=dark, title=title, display=shlex.join(cmd[1:])
            )
            return (0 if res.returncode in ok else res.returncode), res.stdout
        color = "always" if r.type == "rawGit" else r.color_arg
        args = r.args if r.type == "rawGit" else []
        cmd = ["git", "--no-pager", "diff", "--no-ext-diff", f"--color={color}", f"-U{context}", *args, *what]
        diff = git.runner.run(cmd, cwd=cwd, env=env, timeout=timeout, check=False, title=title, display=shlex.join(cmd[1:]))
        if diff.returncode not in ok:
            return diff.returncode, diff.stderr or diff.stdout
        if r.type == "rawGit":
            return 0, diff.stdout
        res = git.runner.run_tty(
            ["sh", "-c", command],
            size=size,
            input=diff.stdout.encode(),
            cwd=cwd,
            env=env,
            timeout=timeout,
            dark=dark,
            title=title,
            display=command,
        )
        return res.returncode, res.stdout
