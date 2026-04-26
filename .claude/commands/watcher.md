---
description: Manage the dashboard or inbox watcher (status / start / stop / reload / tail)
argument-hint: [dashboard|inbox|all] [status|start|stop|reload|tail]
---

Manage one or both launchd-managed watchers.

**Format:** `$ARGUMENTS` is `[target] [subcommand]`
- `target` is `dashboard`, `inbox`, or `all` (default when omitted)
- `subcommand` is one of the actions below (default: `status`)

Examples: `/watcher` → status of both; `/watcher inbox stop` → stop inbox watcher.

---

### Subcommands

- **status** — `launchctl list | grep chadhomes` (shows both), then tail the
  last 5 lines of the relevant log(s):
  - dashboard → `~/Projects/home-builder-agent/watcher.log`
  - inbox → `~/Projects/home-builder-agent/inbox_watcher.log`

- **start**
  - dashboard → `launchctl load ~/Library/LaunchAgents/com.chadhomes.dashboard-watcher.plist`
  - inbox → `launchctl load ~/Library/LaunchAgents/com.chadhomes.inbox-watcher.plist`

- **stop**
  - dashboard → `launchctl unload ~/Library/LaunchAgents/com.chadhomes.dashboard-watcher.plist`
  - inbox → `launchctl unload ~/Library/LaunchAgents/com.chadhomes.inbox-watcher.plist`

- **reload** — stop then start. Use after editing a watcher's source file or
  anything in its import chain.

- **tail** — `tail -f` on the relevant log(s), plus `tail -n 20` on the
  stderr log for Python tracebacks if the script crashes before log() fires:
  - dashboard stderr → `/tmp/dashboard-watcher.stderr.log`
  - inbox stderr → `/tmp/inbox-watcher.stderr.log`

---

Default (`$ARGUMENTS` empty) → **status of both watchers**.

Always confirm before running **stop** or **reload** on the dashboard watcher —
it is production and has been running reliably. Surface launchctl exit codes
so silent failures don't hide.
