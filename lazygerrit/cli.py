"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from . import __version__


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="lazygerrit",
        description=(
            "A lazygit-style terminal UI for Gerrit, tuned for the src repository on "
            "gerrit.cicd.autoheim.net (recheck, run-selected, prio_high, towers, comments, votes...)."
        ),
    )
    p.add_argument(
        "--repo", help="local clone used for diffs and the local stack (default: current repo with a .gitreview, else ~/git/src)"
    )
    p.add_argument("--config", help="config file (default: ~/.config/lazygerrit/config.toml)")
    p.add_argument("--host", help="Gerrit host (default from .gitreview)")
    p.add_argument("--port", type=int, help="Gerrit SSH port (default from .gitreview)")
    p.add_argument("--user", help="Gerrit SSH user (default from ~/.ssh/config)")
    p.add_argument("--project", help="Gerrit project (default from .gitreview)")
    p.add_argument("--branch", help="target branch (default from .gitreview, else master)")
    p.add_argument("-q", "--query", help="open the Query tab with this Gerrit search")
    p.add_argument(
        "--dry-run", action="store_true", help="log write commands (review, set-topic, push...) instead of running them"
    )
    p.add_argument("--demo", action="store_true", help="run against an offline fake Gerrit with sample data")
    p.add_argument("--no-mouse", action="store_true", help="disable mouse support")
    p.add_argument("--log-reads", action="store_true", help="also show read-only commands (queries) in the command log")
    p.add_argument("--version", action="version", version=f"lazygerrit {__version__}")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    from .ui.app import App

    tmp = None
    if args.demo:
        from .demo import cleanup_demo, create_demo

        from .config import apply_display_settings

        backend, cfg, _server, tmp = create_demo(dry_run=args.dry_run)
        apply_display_settings(cfg, args.config)
        if args.no_mouse:
            cfg.mouse = False
        backend.runner.log_reads = args.log_reads
    else:
        from .backend import Backend
        from .config import load_config
        from .gerrit import Runner

        overrides = {
            "host": args.host,
            "port": args.port,
            "user": args.user,
            "project": args.project,
            "branch": args.branch,
            "dry_run": True if args.dry_run else None,
            "mouse": False if args.no_mouse else None,
            "log_reads": True if args.log_reads else None,
        }
        cfg = load_config(args.repo, args.config, overrides)
        runner = Runner(dry_run=cfg.dry_run, log_reads=cfg.log_reads)
        backend = Backend.create(cfg, runner)
    from pathlib import Path

    from .drafts import StateStore, default_state_path

    if tmp is not None:
        state = StateStore(tmp / "state.json")
    else:
        state = StateStore(Path(cfg.state_file).expanduser() if cfg.state_file else default_state_path())
    app = App(backend, cfg, demo=args.demo, state=state)
    if args.query:
        app.custom_query = args.query
        app.custom_query_set = True
        app.p_changes.tab = app.query_tab_index()
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        backend.close()
        if tmp is not None:
            cleanup_demo(tmp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
