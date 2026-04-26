---
description: Generate a construction timeline + Tracker sheet from a project spec
argument-hint: [spec-filename]
---

Run the timeline generator agent on the spec named in $ARGUMENTS (default to
`pelican_point.md` if none given).

Steps:
1. Verify `.env` exists and has `ANTHROPIC_API_KEY` set; if missing, stop and tell me.
2. Verify the spec file exists in the Drive folder under `PROJECT SPECS/`. If it doesn't, run `hb-timeline --list` and show me what's available.
3. Run `hb-timeline $ARGUMENTS` (or with no args if $ARGUMENTS is empty).
4. After the run completes, show me:
   - The Doc URL and Sheet URL from the output
   - Total cost line
   - Whether the JSON block parsed cleanly (count of phases/tasks/orders)
5. If the run errored, surface the error and suggest the most likely fix
   (auth issue → re-run, KB file missing → check WORKSPACE path, etc.).

Don't summarize the timeline content itself — Chad will read the Doc directly.
