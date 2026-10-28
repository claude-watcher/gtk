# 005 — Hide Agent SDK sessions (setting)

**Status**: shipped
**Requested by**: babs
**Date**: 2026-10-28
**Repos**: gtk, tui, webui (one change, three PRs)

## Problem

Tools built on the Agent SDK (T3 Code, custom SDK scripts) run real, long-lived `claude` sessions.
Someone who drives them from that tool's own UI does not want them in the watcher: on a T3 user's
machine they outnumber the terminal sessions (11 of 16 rows observed live, 2026-10-28) and bury the
sessions the watcher exists for. Others want them listed (spec 003 makes T3 rows clickable). No
setting covers it today.

## Solution

A setting, on by default, hides every session started through the Agent SDK, local and remote alike,
in the GTK widget, the TUI and the webui page. Turned off, the list is as today (and 003's T3 deep
link applies again).

The source of truth is the session's `CLAUDE_CODE_ENTRYPOINT`, set by the `claude` binary itself
(observed live: T3 sessions `sdk-ts`, VS Code panel `claude-vscode`, terminal sessions absent or
`cli`). The webui server reports that value raw; only clients interpret it.

## Scope

- Every session row carries `entrypoint`: the raw `CLAUDE_CODE_ENTRYPOINT` of its process, `None`
  when absent or unreadable.
  - gtk / tui: `scan_local_sessions`, from the env it already reads (no extra I/O).
  - webui: `/api/sessions` rows gain `"entrypoint"`, from the env `_scan_sessions` already reads.
  - gtk / tui `adapt_remote_row`: copies it, bounded like every other remote string.
- Setting `hide_sdk_sessions` (default `true`), applied after the local/remote merge, next to
  `hide_daemons`:
  - gtk: `[features] hide_sdk_sessions`, settings-dialog checkbox.
  - tui: `[features] hide_sdk_sessions`, `--show-sdk-sessions` flag (overrides it to off for the
    run), Settings switch.
  - webui page: an `sdk` checkbox in the page prefs (unchecked by default), client-side like
    `daemons`.
- Rule (clients only): hidden when `entrypoint` starts with `sdk-`.
- Docs: READMEs (EN/FR), ARCHITECTURE, webui README (`/api/sessions` field) and AGENTS.md.

## Out of scope

- Any filtering on the webui server, or in `scan_proc`.
- A T3-only rule: `t3 serve` sessions carry no T3-specific marker (only T3 desktop has
  `CHROME_DESKTOP`).
- Hiding by other entrypoints (`claude-vscode`, `claude-desktop`, `mcp`…).
- Showing the entrypoint in the UI.

## Acceptance criteria

- [x] **AC1** Given a local `claude` whose env has `CLAUDE_CODE_ENTRYPOINT=sdk-ts`, when the clients
  scan, then its row carries `entrypoint == "sdk-ts"`; without the variable (or env unreadable),
  `entrypoint is None`.
- [x] **AC2** `/api/sessions` rows carry `entrypoint` with the raw value or `null`, for every row
  (daemon included); the server never drops a row because of it.
- [x] **AC3** A remote row's `entrypoint` survives `adapt_remote_row` (non-string, overlong or
  missing → `None`, the row is kept); gtk and tui stay identical (`test_core_parity.py` green).
- [x] **AC4** With `hide_sdk_sessions = true` (the default, also when the key is missing from the
  config), rows whose `entrypoint` starts with `sdk-` are absent
  from the list, local and remote alike; rows with `None`, `cli`, `claude-vscode` stay.
- [x] **AC5** With the setting off, the row list is the same as before this feature.
- [x] **AC6** The setting persists in `[features]` and is editable from each client's settings UI
  (gtk dialog, tui Settings + `--show-sdk-sessions`); toggling it takes effect on the next refresh.
- [x] **AC7** The webui page has an `sdk` checkbox (unchecked by default), persisted with its other
  prefs; unchecked, `sdk-*` rows are not rendered and the count reflects what is shown.

## Phases

### Phase 1 — webui: report the raw entrypoint, page checkbox
- Work: `entrypoint` on every `_scan_sessions` row; page pref `sdk`; tests on a fake `proc_root`;
  README / AGENTS.md.
- **Covers**: AC2, AC7
- **Data model impact**: none (one new JSON field, additive)
- **DoD**: `uv run pytest` + `pre-commit run --all-files` green; live `/api/sessions` shows
  `"entrypoint": "sdk-ts"` on a T3 session.

### Phase 2 — gtk + tui (paired)
- Work: `entrypoint` on local rows; `adapt_remote_row` copies it; `hide_sdk_sessions` setting, flag
  and UI; filter after the merge; tests (byte-identical test file in both clients).
- **Covers**: AC1, AC3, AC4, AC5, AC6
- **Data model impact**: none (one config key, additive)
- **DoD**: both suites green with the sibling present, `pre-commit run --all-files` green; live: with
  the setting on, T3 rows disappear from both clients (local and through a webui remote), off they
  come back.

## Data model impact (summary)

None. One additive JSON field (`entrypoint`), one additive config key (`hide_sdk_sessions`).

## Open questions

- (none)

## Decisions

- Scope = every `sdk-*` entrypoint, T3 included — chosen by the user; `t3 serve` has no T3-specific
  marker.
- Default on — chosen by the user. Existing configs without the key hide SDK sessions after the
  upgrade; 003's T3 deep link only matters with the setting off.
- tui flag is `--show-sdk-sessions`: with a default of on, a `--hide-…` flag could not change anything.
- Raw value on the wire, rule in the clients — requested by the user: the server reports facts, each
  client applies its own setting, and a future rule needs no server change.
- Env over argv (`--input-format stream-json`) — the VS Code panel shares that argv and would need an
  exception; `CLAUDE_CODE_ENTRYPOINT` names the launcher directly.
- Filter after the local/remote merge, like `hide_daemons` — filtering only the local scan would leave
  remote SDK rows visible.
- Webui page gets the checkbox too — it already mirrors `hide_daemons` client-side.
- Hidden rows do not count — same as hidden daemons.
- A daemon row is governed by the daemon setting alone, whatever its `entrypoint` — the daemon is not
  a session, the SDK setting is about sessions.
- Spec lives in `gtk/specs/` only; tui/webui PRs link to it (as 004).
