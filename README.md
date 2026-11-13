THIS IS 100% CLANKED. USE AT YOUR OWN RISK

# lazygerrit

A [lazygit](https://github.com/jesseduffield/lazygit)-style terminal UI for Gerrit, tuned for the
`src` repository on `gerrit.cicd.autoheim.net` (`~/git/src`).

It does everything `support/tools/rng.py` does (recheck, slowcheck, regate, priocheck,
run-selected, prio_high, rebase, towers, topics, hashtags, reviewers, votes, links and
comments), and adds dashboards, diffs with inline comments, replies, live Zuul status and a
view of your local stack.

```
╭─[1]─Status─────────────────────────────────╮╭─Change 100102──────────────────────────────────────────────────────────────────────────────╮
│✓ demo → review/100103  s0000001@gerrit.exam││100102 Raise exception if cropping fails                                                    ▐
╰────────────────────────────────────────────╯│                                                                                            ▐
╭─[2]─Outgoing - Inc - WIP - Att - Clo - Que─╮│Status     NEW · not ready                                                                  ▐
│100103    ·  ·   Move declarations out one …││Owner      Alex Doe <alex.doe@example.com>                                                  ▐
│100102 ✗  ·  ·   Raise exception if croppin…││Updated    3h ago (created 1d ago)                                                          ▐
│100101 ✓ +1  · ● Add DLT log cropping       ││Project    demo · master                                                                    ▐
│                                            ││Topic      dlt-postprocessing                                                               ▐
│                                            ││Patch set  1 of 1 · rework · +2 -0 · Alex Doe 3h ago                                        ▐
│                                            ││Commit     7c29ae89b714  refs/changes/02/100102/1                                           ▐
│                                            ││URL        https://gerrit.example.com/c/demo/+/100102                                       ▐
│                                            ││                                                                                            ▐
│                                            ││Labels · not ready                                                                          ▐
│                                            ││  ○ Code-Review       need                                                                  ▐
│                                            ││  ✗ Verified          -1 Zuul                                                               ▐
╰─────────────────────────────────────2 of 3─╯│  · Secondary-Review  may                                                                   ▐
╭─[3]─Local stack (origin/master..HEAD)──────╮│                                                                                            ▐
│c6df943 + WIP: experiment with tracing      ││✗ CI · check · failed · ps1 · 3h ago                                                        ▐
│c719cca ↑ 100103    · Move declarations out…││  Check failed, check why, rebase will invalidate smart-recheck. Rerun by a comment "recheck▐
│7c29ae8 ✓ 100102 ✗  · Raise exception if cr…││"                                                                                           │
│9c904da ✓ 100101 ✓ +1 Add DLT log cropping  ││  ✗ spa3_open_loop_hil_smoke_test  FAILURE 1h 27m 04s                                       │
│dcc0995 M 100050 ✓ +2 Bump rapiddev22       ││  ✓ 4 passed · 203 skipped                                                                  │
╰─────────────────────────────────────1 of 5─╯│  https://zuul.example.com/t/demo/buildset/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa                 │
╭─[4]─Files - CI · 100102,1──────────────────╮│                                                                                            │
│A Commit message                            ││Relation chain                                                                              │
│M stringent/rapid/crop.py              +2 -0││  ↑ 100103    · Move declarations out one scope                                             │
│                                            │╰────────────────────────────────────────────────────────────────────────────────────────────╯
│                                            │╭─Command log────────────────────────────────────────────────────────────────────────────────╮
╰─────────────────────────────────────1 of 2─╯│10:26:50 You can hide/focus this panel by pressing '@'                                      │
╭─[5]─Comments - Messages · 100102,1─────────╮│                                                                                            │
│No comments                                 ││                                                                                            │
╰────────────────────────────────────────────╯╰────────────────────────────────────────────────────────────────────────────────────────────╯
Recheck/CI: r | Vote: a | Comment: c | Rebase: b | Copy: y | Open: o | Actions: x | Search: s | Keybindings: ?        DEMO  lazygerrit 0.1.0
```

## Getting started

Requires Python 3.10+ and nothing else: it is pure standard library (`curses`).

```sh
# run straight from the checkout
~/git/lazygerrit/bin/lazygerrit

# or install it as a command
uv tool install --editable ~/git/lazygerrit      # or: pipx install -e ~/git/lazygerrit
lazygerrit
```

With no arguments it uses the repository you are in if it has a `.gitreview`, otherwise
`~/git/src`. Host, port, project, branch and remote come from `.gitreview`
(`gerrit.cicd.autoheim.net:29418`, `src`, `master`, `origin`) and the SSH user from
`~/.ssh/config`. All Gerrit traffic goes over the same SSH access that `rng` and `git review`
use. One multiplexed connection (ControlMaster) is shared by all commands, so the UI stays snappy.

Useful flags:

| Flag | |
|---|---|
| `--dry-run` | write commands are shown in the command log but never sent. Good for trying things out. |
| `--demo` | offline playground with a fake Gerrit, fake Zuul and a throw-away git repo |
| `-q 'topic:foo'` | open the Query tab with a search |
| `--repo PATH` | use another clone for diffs and the local stack |
| `--log-reads` | also show queries in the command log |

## Layout

Like lazygit: side panels on the left (jump with `1`-`5`, cycle with `h`/`l`/`tab`, switch tabs
with `[`/`]`). The main view on the right shows whatever is selected, and the command log
underneath shows every command that changed something on Gerrit.

| Panel | Shows |
|---|---|
| `[1] Status` | connection, project → local branch. The main view has a dashboard with counts, the legend and setup hints. |
| `[2] Changes` | tabs **Outgoing** (`owner:self -is:wip`), **Incoming** (`reviewer:self -owner:self`), **WIP**, **Attention** (`attention:self`), **Closed**, **Query** (press `s`). |
| `[3] Local stack` | your commits on top of `origin/master`, matched to Gerrit by Change-Id: `✓` in sync, `↑` differs (push needed), `↓` older patch set, `+` not on Gerrit, `M` merged. |
| `[4] Files - CI` | files of the current patch set (diffs with inline comments), and CI: live Zuul jobs with progress plus the latest Zuul reports, parsed into jobs. |
| `[5] Comments - Messages` | comment threads (unresolved first; `f` toggles resolved and CI comments, using the same rules as `rng comments`) and the change messages. |

List rows: `✓`/`✗` Verified, then Code-Review and Secondary-Review (`+2`, `-1`, `·` = none),
`●` unresolved comments, owner initials on other people's changes.

## Keybindings

Press `?` anywhere for the full, context-sensitive list. You can run an action straight from that menu.

**Global**: `q` quit · `?` keybindings · `1-5` panels · `h`/`l`/`tab` next/prev panel · `[`/`]` tabs ·
`R` refresh · `/` filter list or search main view · `J`/`K`, `pgup`/`pgdn` scroll main view ·
`+`/`_` screen mode · `@` command log · `:` run any `gerrit` SSH command · `ctrl+z` suspend · `esc` back

**Lists**: `j`/`k` · `,`/`.` page · `<`/`>` (`g`/`G`) top/bottom · `v` or `shift+↑/↓` range select.
Actions apply to every selected change.

**Changes** (also in the stack, files and comments panels):

| Key | Action | rng equivalent |
|---|---|---|
| `r` | CI menu: `r` recheck, `s` slowcheck, `g` regate, `p` priocheck, `R` run-selected picker (jobs from `src_config/premerge`, failed jobs pre-selected), `h` prio_high, `t` recheck tower | `recheck`, `slowcheck`, `regate`, `priocheck`, `runselected`, `prio_high`, `recheck_tower` |
| `a` | vote: `a` Code-Review +2, `1` +1, `0`, `n` -1, `N` -2, `s` Secondary-Review +1, `S` 0, `x` -1, `l` any label | `codereview`, `secondaryreview` |
| `c` / `C` | patchset-level comment (multi-line prompt / `$EDITOR`) | |
| `b` | rebase menu: rebase, rebase + recheck, rebase tower, rebase tower + recheck | `rebase [--recheck]`, `rebase_tower` |
| `t` / `T` | set topic (empty clears it) / on the whole tower | `topic`, `topic_tower` |
| `#` | hashtags: add, remove, add to tower, prio_high | `hashtag`, `hashtag_tower` |
| `i` / `I` | add reviewers (tab completes people seen in your changes) / remove one | `reviewer` |
| `w` | toggle work in progress / ready | |
| `D` | abandon / restore | |
| `y` | copy: number, URL, `N: subject \| URL`, markdown link, tower as markdown list, Change-Id, SHA, ref, fetch command… | `link`, `link_tower`, `change_number` |
| `o` | open in the browser (change, file at line, job, comment) | |
| `space` | check out locally with `git review -d` (refuses with a dirty tree) | |
| `s` | custom Gerrit search in the Query tab | `list` |
| `x` | menu with all of the above | |

"Tower" means the selected change and all open changes below it, processed bottom-first, the same
as rng's `*_tower` commands.

Every write targets the change's *current* patch set, which is queried right before sending, just
like rng. With several changes selected, they are processed bottom of the relation chain first.
Votes are the exception: if a newer patch set appeared since lazygerrit showed you the change, the
vote is refused and you are asked to refresh first, so you never approve code you haven't seen.
If posting a comment fails, the prompt reopens with your text.

**Local stack**: `enter` files of the change (or the local diff), `P` push menu (`git push origin
HEAD:refs/for/master`, a selected commit, `%wip`, `%ready`, `%topic=`, `%hashtag=`, `%r=`).

**Files**: `enter` focuses the diff. In the diff, `j`/`k` move a line cursor, `c` comments on that
line (deleted lines comment on the base side), `r` replies to the thread under the cursor, `{`/`}`
change the context, `e` opens your editor at the line and `H`/`L` scroll sideways.
**CI**: `f` cycles the job filter, `o` opens the build page, `r` → `J` run-selected for exactly that job.

**Comments**: `r` reply, `d` reply "Done" and resolve, `A` "Ack", `u` reply and toggle resolved,
`f` filter, `e` open the commented file in `$EDITOR`. **Messages**: `f` hides/shows CI bot
messages.

Text prompts: `enter` submits, `alt+enter`/`ctrl+j` inserts a newline, pasting multi-line text
works, `ctrl+o` continues in `$EDITOR`, `tab` completes.

## Hashtags, prio_high and exact comment threads (REST)

Gerrit only exposes hashtags and comment thread metadata (ids, resolved state) over its REST API.
`rng` uses `requests`, which reads `~/.netrc`, and lazygerrit does the same. Create an HTTP password
at <https://gerrit.cicd.autoheim.net/settings/#HTTPCredentials> and add:

```
machine gerrit.cicd.autoheim.net login <your s-number> password <http-password>
```

(or export `LAZYGERRIT_HTTP_USER` / `LAZYGERRIT_HTTP_PASSWORD`). Without it everything else works.
Hashtag/prio_high actions explain how to enable them. Comment threads are rebuilt from the SSH
data (grouped by file and line, resolution unknown), and replies are placed at the same line instead
of being threaded.

## Configuration

Optional `~/.config/lazygerrit/config.toml`:

```toml
refresh_interval = 60        # seconds between background refreshes (0 = off)
zuul_refresh_interval = 20   # live CI polling for the selected change
limit = 50                   # changes per tab
side_panel_width = 0.3333
mouse = true
dry_run = false
diff_context = 3

[bot_names]                  # friendly names for CI accounts
svc00140 = "Zuul"
svc00596 = "CI Platform"

[[tabs]]
name = "Outgoing"
query = "owner:self status:open -is:wip project:{project}"

[[tabs]]
name = "Reviewing"
query = "reviewer:self -owner:self status:open project:{project} -is:reviewed"
limit = 30
```

## Development

```sh
python3 -m unittest discover -s tests -t .   # 70+ tests, no network needed
./bin/lazygerrit --demo                       # play with the UI offline
ruff check lazygerrit tests
```

The tests drive the real UI headlessly (virtual canvas + key events) against
`lazygerrit/demo.py`. That fake Gerrit server splits SSH command lines with a port of Gerrit's own
argument splitter and evaluates a subset of the search language, so quoting, JSON review input and
queries are exercised end to end.

Code map: `gerrit.py` (SSH/REST transport, command log, dry-run) · `backend.py` (queries) ·
`operations.py` (rng semantics) · `srcrepo.py` (src specifics: CI commands, run-selected jobs,
priority hashtags) · `parsing.py` (Gerrit JSON, threads, Zuul reports) · `ui/` (curses UI).
