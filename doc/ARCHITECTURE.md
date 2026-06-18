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

[features]
tray = true        # systray icon (true | false)
```

CLI flags (see the README) override these at launch. The free-drag position is
stored separately in `~/.config/claude-watcher/position.json`; if it falls
outside any connected screen (e.g. a monitor was unplugged), the widget resets to
the default corner.

## Session detection

Status comes from Claude Code's **own per-session registry** — a file Claude
maintains itself, keyed by PID and updated in real time. No hook required.

1. Claude writes `~/.claude/sessions/<pid>.json` on every state change, with a
   `status` field, the `sessionId`, and `cwd`.
2. The widget enumerates sessions by scanning `/proc/<pid>/comm` for an exact
   match on `claude`; field 22 of `/proc/<pid>/stat` gives the process
   `starttime` (in ticks).
3. **State** — read from the registry file:
   - `busy` / `shell` / `compacting` → **working**
   - `waiting` → **waiting** (Claude is blocked on a permission/notification)
   - `idle` → **idle**
   - `procStart` in the file must match the process `starttime` — a stale file
     from a recycled PID is ignored.
4. **Context % + current tool** — parsed from the transcript, located exactly via
   `sessionId` → `~/.claude/projects/<slug>/<sessionId>.jsonl`. Context % is
   input tokens / window size; the tool is the `name` of the most recent
   assistant `tool_use` block.
5. **Fallback** — if a session's Claude predates the registry, state falls back
   to the transcript's last-entry type (`assistant` → waiting, `user` → working,
   `system` → idle). This is coarser: it cannot tell a permission `waiting` from
   a finished turn.
6. Walk the process tree to find the parent terminal window (ghostty, kitty,
   alacritty, gnome-terminal…).

The terminal-title spinner is **not** used for state — only to pick the right
window when focusing a multi-window terminal.

### Why the registry instead of hooks

The earlier model installed Claude Code hooks. It couldn't track a genuine
`waiting` status: Claude fires no hook event when the user *approves* a
permission, so a long approved tool stayed stuck on `waiting` until
`PostToolUse`. The registry carries a real `waiting` status, needs no
`settings.json` changes, and works under Wayland.

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
- Sessions running an old Claude Code (no `~/.claude/sessions/<pid>.json`) fall
  back to coarser transcript-based state.
- The registry format is first-party but undocumented — its `status` enum may
  change between Claude versions (the transcript fallback covers that case).
- JSONL slug resolution (fallback path only): `cwd` → replace non-alphanum with
  `-` → match under `~/.claude/projects/`. The registry's `sessionId` bypasses
  this on the primary path.
