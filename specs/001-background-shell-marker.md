# 001 — Background shell becomes a marker, not a state

**Status**: shipped
**Requested by**: babs
**Date**: 2026-08-09
**Scope**: the three repositories — `gtk`, `tui`, `webui`. The detection core is carried
identically by all three and guarded by `test_core_parity.py` (gtk↔tui) and
`test_detection_parity.py` (three-way), so the core part of this feature lands everywhere at once
or the guards go red.

## Problem

When a background shell outlives a turn (`!cmd`, a backgrounded Bash, a Monitor), the registry
keeps `status: shell` and the session is rendered with a dedicated **state**: `background`. That
state occupies the state column, colours the row, owns a sort bucket between `working` and `idle`,
and counts in the tray — while Claude itself has handed control back and is available.

Reported repeatedly on live sessions: a row reads "en fond" when what the user needs to know is
that Claude is idle. The shell is a *detail about* the session, not what the session *is*.

## Solution

The state column tells Claude's availability, and nothing else. A session at the prompt reads
`idle` (or `waiting`) whatever runs behind it. A discreet `⚙ sh` marker on the row says a
background shell is still alive.

`background` disappears from the state vocabulary, in the UIs and on the wire.

## Scope

- `get_session_state` returns the real state; the background shell is reported separately.
- API: `state` carries the real state, a new boolean `bg_shell` carries the shell.
- Clients read `bg_shell`; a legacy `state: "background"` from an older server is translated to
  `idle` + `bg_shell: true` so a new client never shows a state it no longer knows.
- Rendering in the three UIs: real state badge + `⚙ sh` marker next to it.
- Sorting: the `background` bucket disappears — those rows sort with `idle`
  (`waiting` > `working` > `idle`), server-side and client-side.
- Tray and status bar: the "N en fond" counter becomes a `⚙` counter, informative only — it no
  longer drives the tray colour.
- Idle duration is displayed on those rows like on any idle row.

## Out of scope

- Telling a user's `!cmd` from Claude's own shell or from a Monitor — the registry does not say.
- Detecting the background shell any other way than the registry `status: shell` (no /proc child
  inspection). Consequence, accepted: the marker disappears while Claude computes again — the
  registry flips to `busy` and carries a single status, so the shell is no longer visible anywhere.
  Recovering it would mean learning the shell's PID at the moment the registry does say `shell`,
  then following that PID in /proc: a bash child alone proves nothing, since every Bash tool call
  spawns an identical one. Deliberately not built.
- Showing what the shell is running, or how long it has been running.
- A config switch to hide the marker — added later if it ever annoys anyone.
- Any change to the `busy` reconciliation shipped in the previous lot (`fix/session-detection`).

## Acceptance criteria

- [ ] Given a registry `status: shell` and a transcript showing the turn ended, `get_session_state`
      returns `idle` (or `waiting`, per the transcript) and reports a background shell — in the
      three repositories, on identical inputs.
- [ ] `background` no longer appears as a possible state anywhere: `grep -rn "'background'"` matches
      only the marker plumbing, and `adapt_remote_row` rejects it as a *state*.
- [ ] `GET /api/sessions` returns `"state": "idle"` and `"bg_shell": true` for such a session.
- [ ] A row from an OLD server (`"state": "background"`, no `bg_shell`) renders `idle` + the marker.
- [ ] A row from a NEW server read by an old client renders `idle` with no marker and nothing
      breaks (already guaranteed by `adapt_remote_row`'s degrade-on-missing-field rule).
- [ ] Sorting: a session with a background shell sorts among the idle ones, not between `working`
      and `idle` — server-side (`detect.py`), in both clients, and in the web page's bucket
      function.
- [ ] The tray colour is driven by `waiting`/`working` only; the `⚙` count is displayed but never
      selects the icon colour.
- [ ] The idle duration is shown on a background-shell row, exactly as on any idle row.
- [ ] The three parity guards stay green and cover the new symbols/constants.

## Phases

### Phase 1 — Shared core, three repositories
- Work: `get_session_state` stops returning `background` and reports the background shell
  separately; `detect.py` emits `state` + `bg_shell`; `adapt_remote_row` reads `bg_shell` and
  translates the legacy `background` state; parity guards updated.
- **Data model impact**: none (no database in any of the three).
- **DoD**: the three suites green; `curl -s localhost:8000/api/sessions | jq '.sessions[]
  | {state, bg_shell}'` shows a real state next to `bg_shell: true` on a live shell session;
  parity guards cover the new field.

### Phase 2 — Rendering and ordering
- Work: state badge + `⚙ sh` marker in the GTK widget, the TUI and `static/index.html`; sort
  bucket removed on all four sides; tray/status-bar counters; idle duration restored on those rows.
- **Data model impact**: none.
- **DoD**: `python3 claude-watcher-gtk.py --dump` shows the real state for a live `status: shell`
  session; the bucket test in each repo asserts the new order; a screenshot of the row shows badge
  + marker + idle duration.

### Phase 3 — Vocabulary cleanup and documentation
- Work: retire `COLOR_BACKGROUND`/`tr('background')` or repurpose them for the marker; update the
  state tables in `webui/README.md` and the three `doc/ARCHITECTURE.md`; the gtk/tui READMEs.
- **Data model impact**: none.
- **DoD**: no documentation sentence describes `background` as a state; `pre-commit` green in the
  three repositories.

## Data model impact (summary)

None. No database anywhere; the only persisted artefacts are the user's INI config files, which
this feature does not touch.

## Open questions

- [ ] None.

## Decisions

- The marker sits on the DETAIL sub-line, beside the subagent count — not on the badge (the state
  line already competes for width with the project path and a remote row's label prefix) and not in
  the tool/idle cell (the idle duration keeps it, per the accepted criteria). That sub-line is
  already the place for what is true ABOUT a session rather than what it IS.
- A legacy `state: "background"` is translated client-side to `idle` + marker rather than dropped —
  a new client will read old servers for a while, and silently losing the signal is worse than one
  translation line.
- `bg_shell` is a boolean, not a shell name or PID — the registry exposes neither, and inventing a
  richer field now would freeze a shape we cannot fill.
- The tray keeps a `⚙` counter but not a colour: the colour ranks urgency, and a background shell
  is not urgent.
