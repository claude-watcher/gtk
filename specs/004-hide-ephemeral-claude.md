# 004 — Hide ephemeral headless claude processes

**Status**: shipped
**Requested by**: Andy Trouvé
**Date**: 2026-10-19
**Repos**: gtk, tui, webui (one change, three PRs)

## Problem

Some tools spawn short-lived, headless `claude` processes that are not sessions: T3 Code does it for
thread title generation / provider probes. They show up for a few seconds in every client as an
extra row — a duplicate of the project being worked on, or a row in `~/.t3/userdata` — then vanish.
The list flickers and the rows are useless: no transcript, nothing to focus, nothing to watch.

Observed live (2026-10-19), all children of the T3 server:

```
claude --output-format stream-json --verbose --input-format stream-json
       --setting-sources=user,project,local --strict-mcp-config --permission-mode default
       --no-session-persistence --settings {"disableAllHooks":true}
```

Real T3 sessions carry `--session-id=<uuid>` and never `--no-session-persistence`.

## Solution

A non-interactive `claude` process started with `--no-session-persistence` is not listed, in any
client nor in the webui feed. Everything else is listed exactly as today.

## Scope

- Filter in `scan_proc` of the three repos:
  - `gtk/claude-watcher-gtk.py` and `tui/claude-watcher-tui.py` — identical code (AST parity, docstrings/comments excluded,
    guarded by `test_core_parity.py`).
  - `webui/detect.py` — own copy (`scan_proc` diverges by design server-side), same rule.
- Tests in each repo.

## Out of scope

- Other heuristics (`-p` / `--print` one-shots, short-lived processes, cwd-based rules): `-p` runs
  do persist a transcript and can be long jobs the user wants to see.
- A setting to show them again.
- Filtering subagents (`--agent-id`) — unchanged.

## Acceptance criteria

- [x] **AC1** Given a `claude` process (comm `claude`) whose argv contains the exact token
  `--no-session-persistence` AND a non-interactive token (`-p`, `--print`, `--output-format[=…]`,
  `--input-format[=…]`), when the scan runs, then it yields no session row (gtk, tui, webui `/api`
  feed).
- [x] **AC2** Given a `claude` process without that token (interactive, `-p`, T3 session with
  `--session-id`, daemon), or with it but interactive (`claude --no-session-persistence`: the flag
  is ignored without `--print`, the session persists a transcript), when the scan runs, then its row is unchanged.
- [x] **AC3** Exact-token match on the NUL-split argv: the string inside a larger argument (e.g. in
  `--settings {...}` or a prompt) does not hide the process.
- [x] **AC4** Unreadable cmdline (exit/exec race) keeps today's behaviour: the process is listed
  (a session is never lost over a read failure).
- [x] **AC5** gtk and tui `scan_proc` stay identical code (AST parity, docstrings/comments
  excluded): `test_core_parity.py` green in both repos
  with the sibling present.

## Phases

### Phase 1 — gtk + tui (paired)
- Work: add the filter to `scan_proc` in both clients, identical; tests in both repos (fake
  `/proc` entries or monkeypatched `Path` reads, per each repo's existing test style).
- **Covers**: AC1, AC2, AC3, AC4, AC5
- **Data model impact**: none
- **DoD**: in each repo, full test suite green with the sibling script present
  (`CW_TUI_SCRIPT` / gtk equivalent) and `pre-commit run --all-files` green; live check — trigger a
  T3 title generation, no flashing row in either client.

### Phase 2 — webui
- Work: same rule in `webui/detect.py::scan_proc`; test with the injectable `proc_root`.
- **Covers**: AC1, AC2, AC3, AC4
- **Data model impact**: none
- **DoD**: webui test suite + its pre-commit green; `/api` feed never lists a
  `--no-session-persistence` process (test with fake `proc_root`).

## Data model impact (summary)

None.

## Open questions

- (none)

## Decisions

- Rule = argv token `--no-session-persistence`, nothing T3-specific — because it states the
  property that matters (no transcript ⇒ no session to watch) and covers any tool doing the same.
- Non-interactive token required too — `claude --help`: `--no-session-persistence` "only works with
  --print"; an interactive session given the flag still writes a transcript and must stay listed
  (review finding, phase 1). T3 passes `--output-format stream-json` without `-p`, hence the
  format flags count as non-interactive.
- Checked before the daemon test, reusing the argv already read for it — no extra I/O per tick.
- No setting to show them — nothing useful to display; add one later if a use case appears.
- Same branch name in the three repos (`fix/hide-ephemeral-claude`) — the CI parity job resolves
  the sibling by branch name.
- Spec lives in `gtk/specs/` only; tui/webui PRs link to it.
- Implementation branches off `main` once 003 (`feat/t3-thread-deeplink`) is merged; otherwise off
  that branch to keep `specs/README.md` consistent.
