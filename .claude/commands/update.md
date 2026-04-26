---
description: Apply a natural-language status update to the current project
argument-hint: <update text>
---

Run the status updater agent with the message in $ARGUMENTS.

Steps:
1. Run `hb-update "$ARGUMENTS"`.
2. Show me the parsed change (phase #, type, magnitude, new status).
3. Show me the cascade summary (how many phases shifted, original vs revised completion).
4. Show me the Chad-voice summary the agent generated.
5. If the cascade affected fewer phases than I'd expect, double-check the dependency graph in the Master Schedule's Dependencies column — that's the source of truth for what cascades.

Useful update phrasings:
- `"Phase 3 pushed 1 week — I-joist lead time"`
- `"Foundation done"`
- `"Started framing"`
- `"Phase 5 finished 3 days early"`
- `"Phase 4 blocked — waiting on county inspection"`
