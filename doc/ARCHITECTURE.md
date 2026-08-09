# Claude Code Watcher — GTK — Architecture

Technical reference for how the GTK widget detects sessions, focuses terminals,
and renders itself. For installation and usage, see the [README](../README.md).

## Configuration

All settings are editable from the in-app **Settings** screen — most users never
touch the file directly. It is written to `~/.config/claude-watcher/config.ini`
and shared with the TUI (each tool reads only the keys it understands).

```ini
[general]
lang = en          # en | fr — auto-detected from system locale if omitted

[display]
screen     = 0     # monitor index (0=first, 1=second…) — falls back to 0 if absent
width      = 320   # widget width in pixels
refresh_ms = 2000  # refresh interval in milliseconds (inotify drives instant updates; this is the fallback)
snooze_sec = 30    # snooze duration in seconds
bg_alpha   = 88    # opacity in % (20-100) — also adjustable live with Shift+scroll
sort_mode   = default  # default (state then project) | idle (state then most-recently-idle first)
idle_format = none     # idle duration shown on idle rows: none | loose (minute res, [Nd ]HH:MM) | precise ([Nd ]HH:MM:SS)

[features]
tray            = true   # systray icon (true | false)
shortcut_enable = true   # global show/hide hotkey (true | false)
show_topic      = true   # per-row session topic subtitle (true | false)
show_agents     = true   # per-row spawned-subagent count + tooltip list (true | false)
hide_daemons    = false  # hide the Claude Code background daemon rows (true | false)

[remotes]
poll_ms = 2000        # remote poll interval, separate from refresh_ms (a network round trip)

[remote:lab]          # one section per remote machine; the name is the on-screen label
url     = http://box:8000/   # the ONLY required key (webui speaks plain HTTP; see README)
token   = s3cr3t      # optional; see the README for the resolution order
enabled = true        # optional, default true; 1/yes/true/on · 0/no/false/off,
                      # anything else is refused at startup
label   = lab         # optional, defaults to the section name
```

`poll_ms` defaults to 2000 and is floored at 250.

No `[remote:*]` section and no `--remote` flag means no poll thread is started and no HTTP
request is ever made — behaviour is exactly what it was before the feature existed.
`save_config()` forces the file to mode `0600` on every write, unconditionally, because it
may hold tokens (it opens with `os.open(..., 0o600)` and chmods an existing file *before*
writing: `Path.touch(mode=…)` does not re-chmod one that already exists, which is exactly
the upgrade path). Each remote gets one daemon thread that polls sequentially and hands its
result back to the GTK loop through `GLib.idle_add` — no widget is ever touched off the
main thread, and `_refresh()` reads a cache instead of doing HTTP.

Nothing that runs in the GTK loop may raise: `GLib.timeout_add` **removes a source whose
callback raised**, so a single bad row would freeze the whole widget — local sessions
included — with the traceback on a stderr a desktop-launched widget does not have. Hence
the guarded body in `_refresh()`, and the guarded body in `RemotePoller._loop()` on the
other side, where an escaping exception would instead kill a poll thread for good (the
remote would then read `ok`, then `stale` forever). A poller whose thread is gone reports
`dead`, never a stale-looking `ok`.

Remote rows are read-only at the choke point: `kill_session()` and `focus_terminal()` both
take the session dict and return `False` on `remote` as their first statement, because a
remote pid `1234` is an unrelated *local* process `1234`. The UI never offers the
affordance either (no Focus, no Close in the context menu) and the row tooltip says why —
the same convention as daemon rows. For the same reason a remote row's `config_dir` never
becomes a local directory monitor (`local_config_dirs()`): a remote's `~/.claude` also
exists on this machine, so the naive loop would register a local monitor on a remote's
behalf.

Two clocks, deliberately: a remote's **staleness** is stamped with `time.monotonic()` (a
backward NTP step or a resumed laptop must not make a day-dead host look fresh), while a
session's `last_activity` stays on the **wall clock**, because the renderer compares it to
`time.time()` exactly as it does for a local row. The state key is named `received_mono` so
the two cannot be confused.

The whole remote block is **shared verbatim with the TUI** — same constants, same adapter,
same poller. `tests/test_core_parity.py` compares the two files symbol by symbol on the AST
(docstrings, comments and the client-naming tokens excluded) so the next drift is caught
mechanically rather than by a comment nobody reads. **The same guard is mirrored in the TUI
repo** (`CW_GTK_SCRIPT`, sibling checkout in its own CI): while it lived here only, a core
change landing as a TUI-only PR went green over there and later reddened an unrelated GTK
PR, blaming the wrong author. Both CIs check the sibling out branch-to-branch, which makes
it an ordering constraint — a core change is green only once its twin is pushed. The one declared divergence is the row
prefix: the TUI truncates a *path* from the left and must reserve the `<label>:` budget
(`session_path_cell`), while GTK prefixes the *project* in a Pango label ellipsized at END
(`session_project_markup`), where the marker survives truncation by construction.

CLI flags (`--remote NAME=URL`, `--no-local`, see the README) override these at launch.
`--dump` is local-only. The refusal lives in `main()`, **after** `resolve_remotes()`, not in
`parse_args()`: that is the only point where both sources of remotes are visible. Placed in
`parse_args()` it only ever saw `--remote`, so a `[remote:*]` section of the config file
sailed through and `dump_round()` ignored it without a word — the very silence the refusal
exists to prevent.
`--remote` merges over a matching section (its URL wins for the run, the section's other
keys survive) and is never persisted. The free-drag position is
stored separately in `~/.config/claude-watcher/position.json`; if it falls
outside any connected screen (e.g. a monitor was unplugged), the widget resets to
the default corner.

## Session detection

Status comes from one of two first-party sources, no hook required. The
per-session registry (`~/.claude/sessions/<pid>.json`) is preferred when Claude
Code writes it; otherwise state is derived from the session **transcript**
(`~/.claude/projects/<slug>/<sessionId>.jsonl`). Whether the registry file
exists depends on the Claude Code version, so the widget uses it when present
and falls back to the transcript when it is not.

Sessions running inside a Claude **worktree** (`<project>/.claude/worktrees/<name>`)
keep their transcript under the *parent project's* slug, not the worktree path.
The widget detects the marker, resolves to the parent project (so context %, topic
and idle time work), labels the row with the real project name, and adds a
`↳ WT: <name>` sub-line. When the parent transcript can't be confirmed it leaves
the raw label untouched.

A **plain git worktree** (`git worktree add`, checkout anywhere) carries no
marker in its path, so it is detected on disk instead: its root holds a `.git`
FILE pointing at `<repo>/.git/worktrees/<name>`. That file is the proof — no
transcript confirmation needed — and the row gets the same treatment (repository
name as label, `↳ WT: <name>` sub-line). Display only: unlike a Claude worktree,
its transcript lives under its OWN cwd slug, so transcript resolution is
untouched. A `.git` directory (ordinary checkout) or a `gitdir:` pointing at
`.git/modules/<name>` (submodule) is not a worktree.

1. A single `/proc` pass enumerates both sessions and subagents. Sessions are
   `/proc/<pid>/comm` exact-matching `claude`; field 22 of `/proc/<pid>/stat`
   gives the process `starttime` (in ticks). The same pass also collects
   **subagents** (see step 7).
2. **State (registry, when present)** — `~/.claude/sessions/<pid>.json` carries a
   `status` field updated in real time:
   - `busy` / `shell` / `compacting` → **working**
   - `waiting` → **waiting** (Claude is blocked on a permission/notification)
   - `idle` → **idle**
   - `procStart` in the file must match the process `starttime` — a stale file
     from a recycled PID is ignored.
   - a `shell` or `busy` status can stick after the turn actually ended. When the
     transcript shows the turn finished (`waiting`/`idle`), that status no longer
     describes reality and the transcript state is taken. `shell` additionally
     raises the `bg_shell` flag: a background shell (`!cmd`, `run_in_background`,
     a Monitor) really is still running — but that is a detail ABOUT the session,
     rendered as a `⚙ sh` marker beside the state badge, never as a state of its
     own. It does not affect ordering, the tray colour, or the idle duration:
     Claude handed control back, the session IS idle. `compacting` is not
     reconciled — it is genuine, brief foreground work.
   - only turn-END system events (`turn_duration`, `stop_hook_summary`,
     `away_summary`) prove the turn is over. Mid-turn ones (`informational`,
     `api_error`, `local_command`, `compact_boundary`, …) are skipped: reading
     them as an ended turn reconciled a working session down to background.
   - the transcript is located by `sessionId`; a session resumed from another
     directory (`claude -r`) keeps its JSONL under its ORIGINAL project, so it is
     also looked up across projects. There is no "latest .jsonl of the project"
     fallback when the id is known — that read a NEIGHBOUR session's state.
   Not every Claude Code version writes this file; when it is absent the widget
   uses the transcript fallback below.
3. **State (transcript fallback)** — used when no registry file is present.
   Derived from the most recent meaningful entry, bottom-up:
   - `assistant` → classified by `message.stop_reason`: `tool_use` / `pause_turn`
     / still-streaming (`null`) → **working**; a terminal reason (`end_turn`,
     `max_tokens`, `stop_sequence`, `refusal`) → **waiting**.
   - `user` → **working**
   - `system` → **idle**, but ONLY for a turn-END subtype (`turn_duration`,
     `stop_hook_summary`, `away_summary`). Any other subtype happens mid-turn
     and is skipped, so the scan keeps walking back to the real last event.
   This is coarser than the registry: it cannot tell a tool that is *executing*
   (working) from one *awaiting permission approval* (which also ends in an
   `assistant` `tool_use` and genuinely needs the user) — both read as
   **working**.
4. **Context % + current tool** — parsed from the transcript regardless of which
   state source is used. Context % is input tokens / window size; the tool is
   the `name` of the most recent assistant `tool_use` block. With no registry,
   the transcript is located by slugifying `cwd` (see known limitations).
5. **Session topic** (optional, `features.show_topic`) — the per-session subtitle
   that disambiguates several sessions sharing one `cwd`. Read from the
   transcript's `ai-title` event (`aiTitle`, generated by Claude), falling back to
   the last user prompt (`lastPrompt`) until a title exists. Unlike state (read
   from the transcript tail), the title sits near the top, so it is read once in
   full per file, then only the appended delta on later refreshes. The row label
   ellipsizes; hovering the row shows the full `cwd` and full topic in a tooltip.
6. Walk the process tree to find the parent terminal window (ghostty, kitty,
   alacritty, gnome-terminal…).
7. **Subagents** (`Task`-tool background agents, swarm teammates) run the
   *versioned* binary (`comm` is the version string, not `claude`), so they are
   not sessions and are never listed as focusable rows. They are matched by their
   exact `--agent-id` / `--parent-session-id` argv tokens and grouped by parent
   `sessionId`. A session that has spawned any shows an `N agents` count under its
   state badge, and the row tooltip lists each (`name`, agent type, model). This
   is optional (`features.show_agents`, on by default); when off, the detection
   scan is skipped entirely (no cmdline reads for non-`claude` processes).

The **background daemon** (`claude daemon run …`) runs the *same* `claude` binary
(so `comm` also matches `claude`) and only differs by its subcommand. It is not a
session — it has no pid-keyed registry, terminal, or transcript — so its row is
marked with a `(D)` prefix, rendered with a neutral state, and excluded from the
"Close session" action. Set `features.hide_daemons = true` to omit daemon rows
entirely.

The terminal-title spinner is **not** used for state — only to pick the right
window when focusing a multi-window terminal.

### Why the registry instead of hooks

The earlier model installed Claude Code hooks. It couldn't track a genuine
`waiting` status: Claude fires no hook event when the user *approves* a
permission, so a long approved tool stayed stuck on `waiting` until
`PostToolUse`. The registry carries a real `waiting` status, needs no
`settings.json` changes, and works under Wayland. When a Claude Code version
doesn't write the registry, the transcript fallback takes over; it recovers
most of the signal (working vs waiting) but loses the registry's ability to
flag a permission wait distinctly from a running tool.

### Instant refresh

The widget watches `~/.claude/sessions/` with inotify via `Gio.FileMonitor` —
updates appear instantly on any `<pid>.json` change, no polling delay. Polling
(`refresh_ms`) remains active as a fallback for new-process detection and
elapsed-time updates.

## Click to focus

### X11

1. **Kitty** — `kitty @ --to <socket> focus-window --match id:<id>` (precise,
   multi-tab aware)
2. `wmctrl -l -p` → find window by terminal PID → `wmctrl -ia <window_id>`
3. Fallback: `xdotool search --pid <terminal_pid> windowfocus`

### Wayland / GNOME

Cross-application window management is restricted by Wayland's security model.
GNOME 46 removed the last external API (`Shell.Eval`) that allowed it.

| Terminal | Same workspace | Different workspace |
|---|---|---|
| **Kitty** (with `allow_remote_control` + `listen_on`) | ✅ works | ❌ no workspace switch |
| Any other native Wayland terminal | ❌ nothing | ❌ nothing |
| XWayland terminal (e.g. xterm) | ✅ works | ✅ works |

The rest of the widget — overlay, status detection, snooze — is fully functional
on Wayland.

### Closing a session

Right-clicking a session row opens a per-row menu (left-click still focuses). It
always offers *Focus* and *Copy PID* (puts the `claude` PID on the clipboard). For
an **idle** session it also offers *Close session*, which — after a confirmation —
sends `SIGTERM` to the `claude` PID (clean exit, transcript flushed; never
`SIGKILL`). The terminal itself stays open. The kill is gated by the same
anti-PID-reuse guard used elsewhere: `kill_session` only fires if
`get_session_registry(pid, starttime)` still resolves (i.e. `procStart` matches),
so a recycled PID is never signalled. Active sessions (working/waiting) get no
*Close* item — to avoid interrupting a turn in progress. The row disappears on the
next scan once the process is gone.

## GTK window specifics

- `Gtk.WindowType.POPUP` + `WindowTypeHint.DOCK` → always below normal windows,
  sticky across desktops
- RGBA visual + Cairo custom background → rounded dark semi-transparent widget
- `_NET_WM_STRUT_PARTIAL` X11 property → maximized windows stop before the widget
- Header and footer are draggable — position persisted to `~/.config/claude-watcher/position.json`
- Pulse animation on the waiting dot (`_PULSE_ALPHAS`)
- Snooze: middle-click → `set_opacity(0.08)` for `snooze_sec` seconds
- Systray icon (optional): colored dot reflects global state

## Known limitations

- Fullscreen windows bypass X11 struts by design — the widget stays behind them.
- Kitty remote focus requires `allow_remote_control yes` + `listen_on` in `kitty.conf`.
- Whether `~/.claude/sessions/<pid>.json` is written depends on the Claude Code
  version; sessions without it use the coarser transcript-based state.
- Transcript state can't distinguish a tool that is *executing* from one
  *awaiting permission approval* — both end in an `assistant` `tool_use` and show
  as **working**. A permission-blocked session therefore won't light up
  **waiting** (orange); the registry used to flag this distinctly.
- The registry format is first-party but undocumented — its `status` enum may
  change between Claude versions (the transcript fallback covers that case).
- JSONL slug resolution (transcript path): `cwd` → replace non-alphanum with
  `-` → match under `~/.claude/projects/`. The registry's `sessionId`, when a
  registry file exists, bypasses this guessing.
