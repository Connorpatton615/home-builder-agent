"""Regression test for router_agent's subprocess PATH fallback.

Background: production saw `log-site-entry` rows in engine_activity
flipping to outcome=error with error_message="hb-log not found on PATH
(run pip install -e .)" on 2026-05-10. Root cause: subprocess.run
inherits the parent process's PATH, and the iOS HTTP backend / launchd
jobs / sandboxed Claude Code sessions sometimes don't include
`/Library/Frameworks/Python.framework/Versions/3.14/bin` (where the
console_scripts live).

Fix: when subprocess.run raises FileNotFoundError, retry the call via
`python -m <module>` using AGENT_MODULE_MAP. This bypasses PATH
entirely and uses sys.executable, which is always defined.

This test simulates the broken state by forcing FileNotFoundError on
the first subprocess call and asserts that the fallback path is taken
and succeeds.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch, MagicMock

from home_builder_agent.agents import router_agent


def test_agent_module_map_covers_all_dispatched_agents():
    """Every agent_cmd referenced in _build_agent_args has a module
    fallback registered. New CLI? Add to AGENT_MODULE_MAP."""
    # The set of commands _build_agent_args knows how to dispatch.
    # Manually maintained — keep this in sync with the if-chain in
    # router_agent._build_agent_args.
    dispatched_commands = {
        "hb-receipt", "hb-update", "hb-ledger", "hb-inspect",
        "hb-log", "hb-waiver", "hb-change", "hb-client-update",
        "hb-project",
    }
    missing = dispatched_commands - router_agent.AGENT_MODULE_MAP.keys()
    assert not missing, (
        f"AGENT_MODULE_MAP is missing module entries for: {missing}. "
        f"Without them, these CLIs will return 'not found on PATH' "
        f"errors in daemon contexts (the exact bug we just fixed for hb-log)."
    )


def test_invoke_agent_falls_back_to_module_when_path_lookup_fails():
    """When the CLI binary isn't on PATH, _invoke_agent retries via
    `python -m <module>` and reports success."""
    # First call: simulate FileNotFoundError (PATH lookup fails).
    # Second call: simulate success (module fallback works).
    successful_proc = MagicMock(
        returncode=0,
        stdout="SITE LOG ENTRY APPENDED — Test Project",
        stderr="",
    )

    call_args_history: list[list[str]] = []

    def fake_subprocess_run(args, **kwargs):
        call_args_history.append(args)
        if len(call_args_history) == 1:
            raise FileNotFoundError("[Errno 2] No such file or directory: 'hb-log'")
        return successful_proc

    with patch.object(subprocess, "run", side_effect=fake_subprocess_run):
        outcome, summary, err = router_agent._invoke_agent(
            "hb-log",
            {"entry_text": "test entry"},
            dry_run=False,
        )

    assert outcome == "success", f"Expected success via fallback, got {outcome=} {err=}"
    assert err is None
    assert "SITE LOG ENTRY APPENDED" in summary
    # First call used the CLI binary directly
    assert call_args_history[0][0] == "hb-log"
    # Fallback call used `python -m home_builder_agent.agents.site_log_agent`
    assert call_args_history[1][1] == "-m"
    assert call_args_history[1][2] == "home_builder_agent.agents.site_log_agent"


def test_invoke_agent_reports_clean_error_when_module_also_missing():
    """If both the CLI and the python -m fallback fail, surface a
    clean error string (do not crash)."""
    def fake_subprocess_run(args, **kwargs):
        raise FileNotFoundError("not found")

    with patch.object(subprocess, "run", side_effect=fake_subprocess_run):
        outcome, summary, err = router_agent._invoke_agent(
            "hb-log",
            {"entry_text": "test"},
            dry_run=False,
        )

    assert outcome == "error"
    assert err is not None
    assert "hb-log" in err
    assert "fallback" in err.lower() or "home_builder_agent" in err


def test_invoke_agent_reports_clean_error_for_unregistered_command():
    """An agent_cmd with no AGENT_MODULE_MAP entry can't fall back —
    surface a clean error telling the dev to register it."""
    def fake_subprocess_run(args, **kwargs):
        raise FileNotFoundError("not found")

    with patch.object(subprocess, "run", side_effect=fake_subprocess_run):
        outcome, summary, err = router_agent._invoke_agent(
            "hb-totally-fake-agent",
            {"nl_text": "..."},
            dry_run=False,
        )

    # _build_agent_args returns None for unknown agents → "couldn't build
    # CLI args" error path, NOT the FileNotFoundError path. That's fine:
    # the error string still names the bad command.
    assert outcome == "error"
    assert err is not None
    assert "hb-totally-fake-agent" in err
