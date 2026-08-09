# Claude Code Watcher — GTK

> [Version française](README_FR.md)

A GTK3 desktop widget for Ubuntu that monitors all running Claude Code sessions on your machine and displays them in a persistent overlay — similar to a Conky system monitor.

<p align="center">
  <img src="doc/demo-en.gif" alt="Claude Code Watcher GTK widget tracking several sessions and switching to a two-column layout" width="720">
</p>

## Features

- Detects all active Claude Code sessions automatically
- Shows each session's status in **real time**:
  - **Waiting** (orange) — Claude replied, waiting for your input
  - **Working** (amber) — Claude is processing your message, with tool name
  - **Idle** (green) — session paused
- A `⚙ sh` marker beside the badge when a background shell outlived the turn
  (`!cmd`, a backgrounded Bash, a Monitor) — Claude is available, something still runs
- Context window usage (`ctx%`) shown when available
- Git **worktree** sessions resolved to their real project, tagged `↳ WT: <name>`
- Spawned subagent count per session (`N agents`), with each agent detailed in the row tooltip — toggle off in settings
- Background daemon shown as a non-focusable `(D)` row (hideable in settings)
- Click a session row to focus its terminal window
- Right-click a session row for its menu (focus, copy the PID, or close an idle session — sends `SIGTERM`)
- Right-click the header for the global context menu (show/hide, snooze, settings, quit)
- Middle-click to snooze/wake (fades the widget for a configurable duration)
- **Shift + mouse wheel** adjusts opacity live
- Mouse wheel on the title bar — or the ▾/▸ chevron — rolls the widget up/down
- Multi-column layout for many sessions, with a configurable max height and a scrollbar beyond it
- Configurable global hotkey (default `<Ctrl><Alt>q`) to start keyboard navigation
- Drag the header or footer to reposition freely — position is remembered across restarts
- Systray icon with global status indicator
- Footer shows the installed version with an update indicator (green = up to date, red = a newer release is available)
- Language auto-detected from system locale (`fr` / `en`)
- **Remote machines** — sessions from other hosts running `claude-watcher-webui`, merged into the same list and marked `<name>:<project>` (read-only; see [Remote sessions](#remote-sessions))

> [!NOTE]
> Click-to-focus is limited on GNOME Wayland. The rest of the widget works
> normally. See [`doc/ARCHITECTURE.md`](doc/ARCHITECTURE.md#click-to-focus) for the details.

## Requirements

- Ubuntu / Debian (X11 or Wayland/GNOME)
- Python 3 (`/usr/bin/python3`)
- GTK3 + GObject introspection libraries

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-wnck-3.0 gir1.2-appindicator3-0.1 wmctrl xdotool
```

Optional — required for Kitty terminal focus:
- `allow_remote_control yes` + `listen_on unix:/tmp/kitty` in `kitty.conf`

## Install

```bash
curl -fsSL https://github.com/claude-watcher/gtk/releases/latest/download/install.sh | bash
```

Pin a specific version instead of the latest:

```bash
curl -fsSL https://github.com/claude-watcher/gtk/releases/download/v1.4.0/install.sh | bash
```

To **upgrade**, just re-run the `latest` one-liner.

The installer will:
1. Install missing apt dependencies
2. Install the script to `~/.local/share/claude-watcher/`
3. Write `~/.config/claude-watcher/config.ini` (skipped if it already exists)
4. Add an app-menu launcher and register autostart so the widget launches at login

To **uninstall** (removes the script and desktop entries; keeps your config):

```bash
./install.sh --uninstall
```

<details>
<summary>From a local clone (development)</summary>

```bash
git clone https://github.com/claude-watcher/gtk
cd gtk
./install.sh          # installs the checked-out script, no download
```
</details>

> **No hook to install:** status comes from Claude Code's own session files,
> so there's nothing to add to your `settings.json`.

> **Note:** Must use `/usr/bin/python3`, not a Homebrew/pyenv Python — those don't
> have access to system GTK bindings.

## Usage

The widget starts automatically after install. To launch it manually, use the
**Claude Code Watcher** app-menu entry, or:

```bash
/usr/bin/python3 ~/.local/share/claude-watcher/claude-watcher &
```

It starts anchored to the **bottom-right corner** of the configured screen. Drag
the header or footer bar to move it freely — the position is saved and restored on next launch.

All settings are editable from the **Settings** screen (right-click → Settings) —
no need to touch a config file by hand.

### CLI overrides

```
--screen N          monitor index
--corner CORNER     bottom-right | bottom-left | top-right | top-left
--x PX --y PX       absolute position (disables corner anchor)
--margin-x PX       horizontal margin from corner
--margin-y PX       vertical margin from corner
--no-tray           disable systray icon
--list-screens      print detected monitors and exit
--settings          open the Settings window on launch
--remote NAME=URL   watch a machine running claude-watcher-webui (repeatable)
--no-local          only show remote sessions (no local /proc scan)
```

## Remote sessions

Point the widget at other machines running
[`claude-watcher-webui`](https://github.com/claude-watcher/webui) and their sessions
appear in the same list, marked `<name>:<project>` (the scp convention). Remote rows are
**read-only**: no focus, no close — the right-click menu offers neither. A remote that
stops answering is marked stale with the age of its data, and every configured remote
shows up in the footer status area with its health — `lab ok 3` (reachable) is never
confused with `lab down`. The Settings window lists them read-only under **Remotes**,
disabled ones greyed out.

### On the remote machine first

There is a server half, and it is not optional:

1. Install and **run** [`claude-watcher-webui`](https://github.com/claude-watcher/webui)
   on that host — the widget is only a consumer of its `GET /api/sessions`.
2. webui defaults to `APP_HOST=127.0.0.1`, so out of the box it is reachable **only from
   the machine itself**. To watch it from elsewhere, either bind it wider or tunnel to it
   (see below).
3. Binding a non-loopback `APP_HOST` (e.g. `0.0.0.0`) with **no** `APP_AUTH_TOKEN` is
   **refused at startup** — set a token, or opt in explicitly with
   `APP_ALLOW_INSECURE_BIND=true`. That token is the one you give the widget.

> **webui speaks plain HTTP.** It terminates no TLS (there is no `ssl_certfile` knob), so
> `https://box:8000/` does **not** work against it — the connection fails with
> `SSL: RECORD_LAYER_FAILURE`. Use `http://`, or put a reverse proxy (nginx, Caddy,
> Traefik) in front of it and point the widget at the proxy's `https://` URL.

The safest shape needs no proxy and keeps the token off the wire — an SSH tunnel to a
loopback URL:

```bash
ssh -N -L 8001:127.0.0.1:8000 box &          # webui stays bound to 127.0.0.1 on `box`
/usr/bin/python3 ~/.local/share/claude-watcher/claude-watcher --remote lab=http://127.0.0.1:8001
```

### Declaring remotes

Persistent remotes live in `~/.config/claude-watcher/config.ini` (shared with the TUI, so
you declare them once for both):

```ini
[remotes]
poll_ms = 2000              # remote poll interval, separate from refresh_ms.
                            # Default 2000, floored at 250 — below that you are
                            # hammering the host, not watching it.

[remote:lab]
url = http://box:8000/      # the ONLY required key; a section without it is ignored
token = s3cr3t
enabled = true              # 1/yes/true/on · 0/no/false/off. Anything else is
                            # refused at startup rather than defaulting to "on"
label = lab                 # optional, defaults to the section name
```

The file is forced to mode `0600` whenever the widget writes it, because it may hold
tokens. If you create or edit it by hand, `chmod 600 ~/.config/claude-watcher/config.ini`
yourself — nothing re-chmods a file the widget never wrote.

For a one-off look at a machine, use the flag — it is never written to the config file:

```bash
/usr/bin/python3 ~/.local/share/claude-watcher/claude-watcher --remote lab=http://box:8000
/usr/bin/python3 ~/.local/share/claude-watcher/claude-watcher --remote lab=http://remote:s3cr3t@box:8000/
CW_REMOTE_TOKEN_LAB=s3cr3t /usr/bin/python3 ~/.local/share/claude-watcher/claude-watcher --remote lab=http://box:8000
```

Token resolution order, first match wins:

1. the URL's userinfo — `https://remote:<token>@host/` (the token is the **password**;
   `https://<token>@host/` with no colon works too)
2. `CW_REMOTE_TOKEN_<NAME>` — the name uppercased, non-alphanumerics replaced by `_`
   (`--remote my-lab=…` → `CW_REMOTE_TOKEN_MY_LAB`)
3. the `token` key of a matching `[remote:<name>]` section
4. none — the remote is polled unauthenticated

However it is resolved, the token is sent as an `X-API-Key` **header** and never as a
query parameter — webui accepts the token in a header only (`X-API-Key`,
`Authorization: Bearer`, `Authorization: Basic`), and it logs `query_params` on every
request, so a token in the URL would be both rejected and written to the server's log in
clear. A query you pass in the remote URL is still forwarded untouched — the widget does
not rewrite your URL, and a reverse proxy may need its own parameters — but it will not
authenticate you, and it is masked everywhere the widget displays it.

> **The token must be ASCII.** HTTP header values are latin-1, so a token outside that
> range would authenticate as a different string; webui refuses such a token at startup
> rather than serving unexplained 401s.

> **A token passed in `--remote` is visible to every user on the machine** via
> `/proc/<pid>/cmdline`, which is world-readable (`-r--r--r--`), while
> `/proc/<pid>/environ` is owner-only (`-r--------`). On a shared host, use
> `CW_REMOTE_TOKEN_<NAME>` or the config file (`0600`) instead.

> **A token sent to an `http://` remote travels in clear**, and the widget will not stop
> you. Use an SSH tunnel to a loopback URL, or a reverse proxy terminating `https://`
> (certificates are then verified, with no option to disable it).

Only `http` and `https` URLs are polled: a scheme-less `--remote lab=box` or a `file://`
typo is reported as an error on that remote instead of being fetched.

### Failure modes, and what the widget does about them

| Situation | Behaviour |
|---|---|
| Slow or hung host | 5 s connect/read timeout **and** a 5 s total read budget; one thread per remote, so only that host is delayed |
| Huge response | read capped at 4 MiB, poll recorded as failed |
| Repeated failures | exponential backoff, capped at 60 s |
| HTTP 401 / 403 | shown as an auth error, retried no sooner than every 5 min |
| Redirects | **not followed** — a 302 would replay your token to the redirect target |
| Over 500 sessions | truncated, and the status area says `lab ok 500/612` |
| First poll still in flight | `lab starting`, not `lab down` |
| Poll thread gone | `lab poller stopped` — never a stale-looking `ok` |

Remotes are read at startup: adding or removing one means restarting the widget. Pointing
a remote at your own machine with the local scan on lists every session twice — once bare,
once prefixed; that is a configuration choice, not a bug.

`--dump` is a **local** diagnostic (it prints the raw registry/JSONL values behind each
state, which only exist on this machine): combining it with `--no-local`, or with any
*enabled* remote — whether declared with `--remote` or by a `[remote:<name>]` section of
the config file — is refused rather than silently ignored. A remote left at
`enabled = false` is never polled, so it does not block the diagnostic.

## How it works

For the technical details — session detection, click-to-focus internals, GTK
window specifics, and known limitations — see [`doc/ARCHITECTURE.md`](doc/ARCHITECTURE.md).
