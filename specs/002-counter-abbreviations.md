# 002 — Header counters shrink instead of being cut

**Status**: shipped
**Requested by**: babs
**Date**: 2026-08-09
**Scope**: `gtk` and `tui` (the header/status line). The server has no such line.

## Problem

The header counters (`3 attente · 1 travaille · 12 total`) are a Pango label with
`ellipsize=END`. When the widget is narrow, the line is simply cut: the user loses the *last*
counters — the total among them — while the truncated ones stay long. Spec 001 adds a `⚙` counter,
which makes the line longer and the truncation more frequent.

## Solution

Pick the richest wording that FITS the space actually available, instead of writing one wording and
letting it be cut. Three levels, from full words to bare numbers; the first that fits wins.

```
3 attente · 1 travaille · 2 ⚙ · 12 total     (level 0)
3 att · 1 trav · 2⚙ · 12                     (level 1)
3/1/2⚙/12                                    (level 2)
```

Measure before drawing (`create_pango_layout(txt).get_pixel_size()` in GTK, cell width in the TUI)
rather than reacting to `is_ellipsized()` after allocation: no feedback loop, and the choice becomes
a pure function that both clients share and the parity guard covers.

## Scope

- A pure `counts_segments(waiting, working, bg_shell, total, level)` shared by both clients,
  returning `(text, colour)` segments so each client keeps its own markup.
- Level selection against the width the label was actually allocated.
- The tray tooltip and the row tooltips keep the full wording — they are not width-constrained.

## Out of scope

- The remotes bar and the session rows (they already ellipsise per-cell, and their content is a
  path or a name, not a countable).
- A user setting to force a level.
- Reflowing the header onto two lines.

## Acceptance criteria

- [ ] `counts_segments` is identical in both clients and covered by the parity guard.
- [ ] Given a budget that fits level 0, level 0 is chosen; shrink the budget and level 1 then 2 are
      chosen in turn; below the smallest, level 2 is kept (never an empty line).
- [ ] The chosen text never exceeds the budget when any level fits it.
- [ ] Counters at zero are omitted at every level (no `0 attente`).
- [ ] The tray tooltip keeps full words whatever the header level.

## Phases

### Phase 1 — Fitting counters
- Work: the pure function, the level picker, wiring in both headers, tests.
- **Data model impact**: none.
- **DoD**: the three suites green; a unit test drives the three levels through a fake measure; the
  widget narrowed by hand shows `3/1/12` instead of a cut line.

## Data model impact (summary)

None.

## Open questions

- [ ] None.

## Decisions

- Level 2 uses `/` separators and bare numbers rather than emoji glyphs — emoji width is
  double-width in most terminals and would defeat the TUI's cell measurement.
- The level is recomputed on each refresh from the last allocation; no `size-allocate` handler, so
  no relayout feedback loop. One refresh (2 s by default) to converge after a resize.
- The budget is the HEADER's width minus its other children's natural widths, never the counters
  label's own allocation: `ellipsize=END` gives that label a near-zero minimum, so its allocation
  measures the text already written rather than the room available, and the densest level latched.
- The first refresh does not measure: the window has not yet taken the width of its rows (they
  arrive with that very refresh), so the header reads narrow and the widget opened on bare numbers.
