---
description: Manage the dashboard watcher (status / start / stop / tail)
argument-hint: status | start | stop | reload | tail
---

Manage the launchd-managed dashboard watcher. $ARGUMENTS picks the subcommand.

- **status** — `launchctl list | grep chadhomes` to confirm it's loaded, plus
  `tail -n 5 ~/Projects/home-builder-agent/watcher.log` to show the last
  activity.

- **start** — `launchctl load ~/Library/LaunchAgents/com.chadhomes.dashboard-watcher.plist`

- **stop** — `launchctl unload ~/Library/LaunchAgents/com.chadhomes.dashboard-watcher.plist`

- **reload** — stop, then start. Use after editing `watchers/dashboard.py`
  or anything in its import chain.

- **tail** — `tail -f ~/Projects/home-builder-agent/watcher.log` for the
  watcher's own log, AND `tail -n 20 /tmp/dashboard-watcher.stderr.log` for
  Python tracebacks if the script is crashing before our log() can fire.

Default ($ARGUMENTS empty) → run **status**.

Always confirm before running stop or reload — the watcher is
production. Surface the launchctl exit code so silent failures don't hide.
