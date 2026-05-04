# Samples

This folder holds canonical customer artifacts the agent's parsers are tested against. Drop the latest version of each file here when it changes.

## Files expected

- **`chad-spec-sheet-template.xlsx`** — Chad Lynch's master 10-tab spec sheet template. Tabs: Cost Sheet, Spec Sheet, Flooring, Fireplace & Hearth, Exterior Veneer Schedule, Shower Tile & Backsplash, Shower Doors & Bath Hardware, Interior Trim Schedule, Paint Schedule, Landscape Plan. Original is a legacy `.xls` (created 2013, last saved 2026-03-31); the parser should accept both `.xls` and `.xlsx` and recommend customers move to `.xlsx` going forward.
- **`chad-ai-help-list.xlsx`** — Chad's product brief. Two tabs: `list of list` (high-level requirements, phase durations, lead times, dashboard views, notification triggers) and `Precon Check List` (the model template — 44 items across 10 categories: Client & Contract, Plans & Engineering, Selections, Permitting, Site Prep, Subcontractors, Materials, Budget, Schedule, Meetings).

## Rules

- These files are the source-of-truth for parser fixtures. **Do not edit them in place** — re-export from Chad when the upstream artifact changes.
- Treat blank cells as the missing-data signal; the Cost Sheet uses `0` instead of blanks in places, and parsers must not conflate the two.
- Known typos in the source artifact (do not fix in the original — it's a customer artifact — but be aware when matching tab names): `Backslash` should be `Backsplash`; `Interiror Trim Schedule` should be `Interior Trim Schedule`.
