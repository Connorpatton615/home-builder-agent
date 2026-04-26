"""watchers — long-running poll loops invoked by launchd.

Watchers differ from agents: agents are user-triggered ("generate this
timeline"); watchers run on an interval to react to changes Chad makes
("dashboard tab is stale, refresh it").

Phase 1 ships one watcher; Phase 2 will add a Gmail watcher and a supplier
email watcher, both following the same pattern as `dashboard.py`.
"""
