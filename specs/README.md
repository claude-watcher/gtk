# Specs

One file per feature, written before the code. `NNN-slug.md`, statuses:
`draft` → `approved` → `in progress` → `shipped`.

Specs whose scope spans several repositories of the project (`gtk`, `tui`, `webui`) live here and
say so in their header — the detection core is carried identically by the three and guarded by the
parity tests, so it cannot be changed in one repository alone.

| # | Feature | Status |
|---|---------|--------|
| [001](001-background-shell-marker.md) | Background shell becomes a marker, not a state | shipped |
| [002](002-counter-abbreviations.md) | Header counters shrink instead of being cut | shipped |
