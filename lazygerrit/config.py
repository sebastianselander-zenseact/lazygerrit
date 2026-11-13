"""Configuration: defaults tuned for gerrit.cicd.autoheim.net/src, overridable by .gitreview,
~/.config/lazygerrit/config.toml and command line flags."""

from __future__ import annotations

import configparser
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:  # Python 3.11+
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

DEFAULT_REPO = Path.home() / "git" / "src"


@dataclass
class Tab:
    name: str
    query: str
    limit: Optional[int] = None


DEFAULT_TABS = [
    Tab("Outgoing", "owner:self status:open -is:wip project:{project}"),
    Tab("Incoming", "reviewer:self -owner:self -is:wip status:open project:{project}"),
    Tab("WIP", "owner:self is:wip status:open project:{project}"),
    Tab("Attention", "attention:self status:open"),
    Tab("Closed", "owner:self status:closed project:{project}", limit=25),
    Tab("Query", ""),
]


@dataclass
class Config:
    repo: Optional[Path] = None
    host: str = "gerrit.cicd.autoheim.net"
    port: int = 29418
    user: Optional[str] = None
    project: str = "src"
    branch: str = "master"
    remote: str = "origin"
    web_url: Optional[str] = None
    zuul_url: str = "https://zuul.cicd.autoheim.net"
    zuul_tenant: str = "src"
    tabs: list[Tab] = field(default_factory=lambda: [Tab(t.name, t.query, t.limit) for t in DEFAULT_TABS])
    custom_query: str = "status:open project:{project}"
    limit: int = 50
    stack_limit: int = 40
    refresh_interval: int = 60
    zuul_refresh_interval: int = 20
    ssh_multiplex: bool = True
    ssh_options: list[str] = field(default_factory=list)
    dry_run: bool = False
    mouse: bool = True
    side_panel_width: float = 0.3333
    show_command_log: bool = True
    command_log_size: int = 8
    bot_usernames: list[str] = field(default_factory=lambda: ["svc00140", "svc00596"])
    bot_names: dict[str, str] = field(default_factory=lambda: {"svc00140": "Zuul", "svc00596": "CI Platform"})
    diff_context: int = 3
    log_reads: bool = False
    nerd_fonts: bool = False

    @property
    def base_url(self) -> str:
        return (self.web_url or f"https://{self.host}").rstrip("/")

    def change_url(self, number: int, project: Optional[str] = None) -> str:
        return f"{self.base_url}/c/{project or self.project}/+/{number}"

    def tab_query(self, tab: Tab) -> str:
        return tab.query.format(project=self.project, branch=self.branch)

    def editor(self) -> str:
        return os.environ.get("GIT_EDITOR") or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"


def git_toplevel(path: Path) -> Optional[Path]:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return Path(out.stdout.strip())


def detect_repo(explicit: Optional[str]) -> Optional[Path]:
    """--repo, else the current repo if it has a .gitreview, else ~/git/src, else the current repo."""
    if explicit:
        p = Path(explicit).expanduser().resolve()
        return git_toplevel(p) or p
    top = git_toplevel(Path.cwd())
    if top and (top / ".gitreview").exists():
        return top
    if (DEFAULT_REPO / ".gitreview").exists():
        return DEFAULT_REPO
    return top


def read_gitreview(repo: Path) -> dict[str, str]:
    path = repo / ".gitreview"
    if not path.exists():
        return {}
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error:
        return {}
    if not parser.has_section("gerrit"):
        return {}
    return dict(parser.items("gerrit"))


def ssh_user_for(host: str) -> Optional[str]:
    try:
        out = subprocess.run(["ssh", "-G", host], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    for ln in out.stdout.splitlines():
        if ln.startswith("user "):
            return ln.split(" ", 1)[1].strip()
    return None


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "lazygerrit" / "config.toml"


def _apply(cfg: Config, data: dict[str, Any]) -> None:
    for key, value in data.items():
        key = key.replace("-", "_")
        if key == "tabs" and isinstance(value, list):
            cfg.tabs = [Tab(t["name"], t.get("query", ""), t.get("limit")) for t in value if "name" in t]
            if not any(t.name == "Query" for t in cfg.tabs):
                cfg.tabs.append(Tab("Query", ""))
        elif key == "repo" and value:
            cfg.repo = Path(str(value)).expanduser()
        elif hasattr(cfg, key):
            setattr(cfg, key, value)


def load_config(
    repo_arg: Optional[str] = None,
    config_file: Optional[str] = None,
    overrides: Optional[dict[str, Any]] = None,
) -> Config:
    cfg = Config()
    path = Path(config_file).expanduser() if config_file else config_path()
    if path.exists() and tomllib is not None:
        with path.open("rb") as fh:
            _apply(cfg, tomllib.load(fh))
    cfg.repo = detect_repo(repo_arg or (str(cfg.repo) if cfg.repo else None))
    if cfg.repo:
        gr = read_gitreview(cfg.repo)
        if gr.get("host"):
            cfg.host = gr["host"]
        if gr.get("port"):
            try:
                cfg.port = int(gr["port"])
            except ValueError:
                pass
        if gr.get("project"):
            cfg.project = gr["project"].removesuffix(".git")
        if gr.get("defaultbranch"):
            cfg.branch = gr["defaultbranch"]
        if gr.get("defaultremote"):
            cfg.remote = gr["defaultremote"]
    if overrides:
        _apply(cfg, {k: v for k, v in overrides.items() if v is not None})
    if cfg.user is None:
        cfg.user = ssh_user_for(cfg.host)
    from .models import BOT_NAMES

    BOT_NAMES.update(cfg.bot_names)
    return cfg
