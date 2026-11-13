THIS IS 100% CLANKED. USE AT YOUR OWN RISK

# lazygerrit

A [lazygit](https://github.com/jesseduffield/lazygit)-style terminal UI for Gerrit, tuned for the
`src` repository on `gerrit.cicd.autoheim.net` (`~/git/src`).

It does everything `support/tools/rng.py` does (recheck, slowcheck, regate, priocheck,
run-selected, prio_high, rebase, towers, topics, hashtags, reviewers, votes, links and
comments), and adds dashboards, code review (diffs with intraline highlighting, inline comments,
drafts published Gerrit-style in one go, patch set comparison), live Zuul status and a view of
your local stack.

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
| `[4] Files - CI` | files of the patch set you are reviewing (`✓` reviewed, `✎` your drafts, `●` unresolved / `○` other comments), and CI: live Zuul jobs with progress plus the latest Zuul reports, parsed into jobs. |
| `[5] Comments - Messages` | your drafts, then comment threads (unresolved first; `f` toggles resolved and CI comments, using the same rules as `rng comments`) and the change messages. |

List rows: `✓`/`✗` Verified, then Code-Review and Secondary-Review (`+2`, `-1`, `·` = none),
`●` unresolved comments, `✎N` your unpublished drafts, owner initials on other people's changes.

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
| `p` | review dialog: publish all your drafts together with votes and a message | |
| `c` / `C` | reply to the change: review dialog with the message focused (or `$EDITOR`). With several changes selected: post the same comment on all of them | |
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
Votes are the exception: if the change has a newer patch set than the one you looked at (the
newest patch set whose diff you opened, or the one shown when you voted without opening a diff),
the vote is refused, so you never approve code you haven't seen. The review dialog warns about this
before you send.
If posting a comment fails, the prompt reopens with your text.

**Local stack**: `enter` files of the change (or the local diff), `P` push menu (`git push origin
HEAD:refs/for/master`, a selected commit, `%wip`, `%ready`, `%topic=`, `%hashtag=`, `%r=`).

**CI**: `f` cycles the job filter, `o` opens the build page, `r` → `J` run-selected for exactly that job.
**Messages**: `f` hides/shows CI bot messages.

**Merge conflicts**: a red `CONFLICT` tag marks open changes that cannot be merged into their branch
(Gerrit's "Merge Conflict"), and the change view lists the conflicting files. This Gerrit does not
report mergeability over SSH, so lazygerrit checks it: patch sets that are in your clone are merged
into the branch as it is on Gerrit right now, with `git merge-tree` writing to a scratch directory.
Your working tree, refs and object store are left alone, apart from fetching the branch's new
commits (without moving `origin/master`). For changes that are not in your clone, Zuul's latest
"Merge Failed" report is used instead. The branch on Gerrit is looked up at most every
`merge_check_interval` seconds (`R` looks again). This needs git 2.38 or newer.

### Code review

Comments work like in the Gerrit web UI: everything you write is a **draft** until you publish.
Drafts are stored in `~/.local/state/lazygerrit/state.json`, so they survive restarts.
`p` opens the review dialog. Set Code-Review / Secondary-Review with `←`/`→`, write an optional
message (posted as a resolved patchset-level comment) and press `enter`. All drafts, votes and the
message go out together, so reviewers get one notification. Drafts on older patch sets are posted
on those patch sets. If publishing fails, the dialog reopens with your message and votes. While a
review is being sent, that change's drafts can't be edited and it can't be published twice.

Several lazygerrit instances can run at the same time: every change to the state file is a locked
read-modify-write, and drafts written by another instance show up on the next refresh (`R`). An
unreadable state file is moved aside to `state.json.corrupt-<time>` instead of being overwritten.

1. Pick a change and press `4` (or `enter`) for its files. `P` views another patch set, `B` compares
   against an older patch set instead of the parent ("what changed since I last looked"). Like in
   Gerrit, a comparison lists only files that one of the two patch sets changes itself; files that
   only differ because of a rebase are left out. Comments on the left side go to the older patch set.
2. `enter` opens the diff: line numbers, syntax highlighting, word-level highlights of what changed
   in a line, existing comments inline, and a note when other patch sets have comments on the file.
3. Review and publish with the keys below.

| Diff view key | |
|---|---|
| `j`/`k`, `J`/`K`, `g`/`G` | move the line cursor / scroll / top, bottom |
| `]` / `[` | next / previous hunk |
| `}` / `{` | next / previous comment thread (including drafts) |
| `.` / `,` | next / previous file |
| `c` | draft comment on the line (deleted lines comment on the old side). On a draft: edit it. On a thread: reply |
| `v` then `c` | select lines and comment on the range |
| `r` / `d` / `A` / `u` | reply to the thread under the cursor / reply "Done" (resolves) / "Ack" / reply and toggle resolved |
| `D` | delete the draft under the cursor |
| `space` | mark the file reviewed and go to the next one |
| `z` | toggle whole-file view |
| `\|` | switch the diff view: review view, delta, difftastic, … (see [Diff tools](#diff-tools-delta-difftastic-)) |
| `P` / `B` | patch set / base to compare against |
| `p` | publish drafts with a vote and message |
| `e` / `o` | open the file in `$EDITOR` / in the browser at that line |
| `esc` | back to the file list |

**Files panel**: `space` toggles reviewed, `P`/`B`/`z`/`|` as above, `e` edits the file.
**Comments panel**: `enter` jumps to the comment in the diff (switching patch set if needed),
`r`/`d`/`A`/`u` create draft replies, `c` edits a draft, `D` deletes it, `f` filters, `e` opens the
file in `$EDITOR`.

Text prompts and the review dialog: `enter` submits, `alt+enter`/`ctrl+j` inserts a newline,
pasting multi-line text works, `ctrl+o` continues in `$EDITOR`, `tab` completes / moves between
fields. In the review dialog, typing always goes to the message, so text can never change a vote.

**Syntax highlighting**: when [delta](https://github.com/dandavison/delta) is installed, the review
view is syntax highlighted by `delta --color-only`, a mode that only adds colors and keeps the diff
line for line, so comments and commenting work as usual. Your delta settings apply (e.g.
`syntax-theme` in the `[delta]` section of `~/.gitconfig`). Set `syntax_highlight = "off"` to turn it
off, or to another command that only adds colors (like `diff-highlight`). If the command fails or
changes the diff's lines, a message says so and diffs are shown without it.

### Diff tools (delta, difftastic, …)

Diffs can be shown with your favourite diff tool instead of the review view, configured like
lazygit's [`git.diffRenderers`](https://github.com/jesseduffield/lazygit/blob/master/docs/Custom_DiffRenderers.md).
Entries copied from your lazygit config work as they are (`colorArg` and the other field names
are accepted). For example, in `~/.config/lazygerrit/config.toml`:

```toml
[[diff_renderers]]                 # type = "stdinFilter" (the default): git's diff is piped through it
command = "delta --paging=never"

[[diff_renderers]]                 # git runs it as its external diff program
type = "extDiff"
command = "difft --color=always --background={{colorScheme}} --context={{diffContext}}"

[[diff_renderers]]                 # git's own diff with extra arguments
type = "rawGit"
args = ["--color-words"]
name = "color-words"
```

- Diffs open with the first entry; `|` switches to the next one. The review view (lazygerrit's own,
  which shows and adds comments) is always in the cycle, at the end unless you place it yourself
  with `{ type = "builtin" }`, e.g. first to keep it as the default.
- A diff tool cannot show Gerrit comments, so a line on top tells how many threads and drafts the
  file has. `c`, `r`, `v` and the other comment and hunk/thread keys switch to the review view on the
  same line (press the key again to act); `|` goes back. Jumping to a comment from the Comments
  panel always uses the review view. Leaving the diff returns to your diff tool.
- The tool runs in a hidden terminal the size of the diff view, so it sizes and colors its output
  as usual (no `--width` needed). `{{width}}`, `{{columnWidth}}` (one side of a side-by-side
  view), `{{diffContext}}` (3, or everything when `z` shows whole files) and `{{colorScheme}}`
  (`dark`/`light`, from `color_scheme` or `$COLORFGBG`) can be used in commands, as can simple
  conditions like `{{if gt .width 160}}--side-by-side{{end}}`.
- Commit messages are rendered too. `--demo` uses the diff tools of your config file, so you can
  try them there.

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
idle_pause = 300             # stop background refreshes after this long without input (0 = never)
limit = 50                   # changes per tab
side_panel_width = 0.3333
mouse = true
dry_run = false
diff_context = 3
state_file = "~/.local/state/lazygerrit/state.json"   # drafts and reviewed marks
color_scheme = "auto"        # dark/light background, for {{colorScheme}} in diff_renderers
syntax_highlight = "auto"    # review view: delta --color-only if delta is installed; "off"; or a command
merge_check_interval = 600   # seconds between looking up the branch on Gerrit for merge checks (0 = off)

[[diff_renderers]]           # optional diff tools, see "Diff tools" above
command = "delta --paging=never"

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

### Load on Gerrit

Everything goes over one multiplexed SSH connection. Startup runs about a dozen small queries (one
per tab, the local stack and the selected change). After that lazygerrit refreshes only the visible
tab, the local stack and the selected change, every `refresh_interval` seconds. It waits until the
cursor has rested for 120 ms before loading a change and caches change details for 45 s. While CI
jobs are running it polls Zuul, not Gerrit, every `zuul_refresh_interval` seconds. After
`idle_pause` seconds without a key press all background refreshing stops, and it resumes on the
next key. The only expensive query (`--patch-sets` together with `--files`) is never used. Merge
checks add a `git ls-remote` every `merge_check_interval` seconds, plus a fetch when the branch moved.

## Development

```sh
python3 -m unittest discover -s tests -t .   # 120+ tests, no network needed
./bin/lazygerrit --demo                       # play with the UI offline
ruff check lazygerrit tests
```

The tests drive the real UI headlessly (virtual canvas + key events) against
`lazygerrit/demo.py`. That fake Gerrit server splits SSH command lines with a port of Gerrit's own
argument splitter and evaluates a subset of the search language, so quoting, JSON review input and
queries are exercised end to end.

Code map: `gerrit.py` (SSH/REST transport, command log, dry-run) · `backend.py` (queries) ·
`operations.py` (rng semantics, publishing reviews) · `drafts.py` (drafts, reviewed marks) ·
`ui/review.py` (code review) · `srcrepo.py` (src specifics: CI commands, run-selected jobs,
priority hashtags) · `parsing.py` (Gerrit JSON, threads, Zuul reports) · `ui/` (curses UI).
