---
description: Manually refresh the Dashboard tab on the latest Tracker sheet
---

Run `hb-dashboard` to refresh the Dashboard tab on the latest Tracker.

The watcher does this automatically every 60 seconds whenever a Tracker
changes — but running manually is useful when:
- You've just edited Master Schedule statuses and want an instant refresh
- The watcher is down (check with `/watcher status`)
- You want to confirm the metric calculations are correct after a change

Steps:
1. Run `hb-dashboard`.
2. Show me the metrics line (current stage, % complete, original vs revised completion).
3. Show me the Sheet URL.
