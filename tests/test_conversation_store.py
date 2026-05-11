"""Tests for home_builder_agent.agents.conversation_store.

Covers:
  - schema creation is idempotent
  - get_or_create lazy-creates + returns existing
  - append_message round-trips fields + allocates monotonic turn_idx
  - load_recent_turns returns the last N in chronological order
  - rolling_summary read/write
  - prune triggers exactly when message_count > PRUNE_THRESHOLD,
    folds evicted rows into the rolling_summary via Sonnet, deletes
    them, and leaves KEEP_RECENT untouched
  - prune is a no-op below the threshold
  - prune Sonnet failures are surfaced (caller swallows; here we test
    the explicit prune() raise path)

Sonnet is mocked at the module-level (`_summarize_evicted`) so tests
run offline.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure each test gets its own DB file. autouse fixture below re-points
# ASK_CONVERSATIONS_DB at a tmp path *before* the module is touched.


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_ask.db"
    monkeypatch.setenv("ASK_CONVERSATIONS_DB", str(db_file))
    # Reload the module's thread-local cache so a stale connection from
    # a previous test (different DB path) doesn't get reused.
    from home_builder_agent.agents import conversation_store as cs
    if hasattr(cs._thread_local, "conn"):
        try:
            cs._thread_local.conn.close()
        except Exception:
            pass
        del cs._thread_local.conn
    if hasattr(cs._thread_local, "path"):
        del cs._thread_local.path
    yield db_file


@pytest.fixture
def store():
    from home_builder_agent.agents import conversation_store as cs
    return cs


# ---------------------------------------------------------------------------
# Schema + connection
# ---------------------------------------------------------------------------

def test_init_db_creates_tables(store, isolated_db):
    """init_db creates both tables; running twice is a no-op."""
    store.init_db()
    store.init_db()
    with store.get_conn() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('conversations', 'messages')"
        ).fetchall()
    names = sorted(r["name"] for r in rows)
    assert names == ["conversations", "messages"]


def test_db_path_uses_env_override(monkeypatch, tmp_path):
    """ASK_CONVERSATIONS_DB env wins over the default path."""
    target = tmp_path / "custom.db"
    monkeypatch.setenv("ASK_CONVERSATIONS_DB", str(target))
    from home_builder_agent.agents import conversation_store as cs
    assert cs.db_path() == target


# ---------------------------------------------------------------------------
# get_or_create + append_message
# ---------------------------------------------------------------------------

def test_get_or_create_new_conversation(store):
    convo = store.get_or_create("conv-1", user_id="u-1", project_id="p-1")
    assert convo["id"] == "conv-1"
    assert convo["user_id"] == "u-1"
    assert convo["project_id"] == "p-1"
    assert convo["rolling_summary"] == ""
    assert convo["created_at"] == convo["last_turn_at"]


def test_get_or_create_returns_existing(store):
    first = store.get_or_create("conv-2", user_id="u-1", project_id="p-1")
    # Second call with different user_id should NOT overwrite — returns
    # the existing row as-is.
    second = store.get_or_create("conv-2", user_id="other-user")
    assert second["user_id"] == "u-1"
    assert second["created_at"] == first["created_at"]


def test_get_or_create_uses_smoke_user_when_missing(store):
    convo = store.get_or_create("conv-3")
    assert convo["user_id"] == store.SMOKE_USER_ID


def test_get_or_create_requires_id(store):
    with pytest.raises(ValueError):
        store.get_or_create("")


def test_append_message_round_trip(store):
    store.append_message(
        "conv-4",
        "user",
        "what's pending in my queue?",
        user_id="u-1",
        project_id="p-1",
    )
    store.append_message(
        "conv-4",
        "assistant",
        "Three drafts on Whitfield, two on Pelican.",
        tool_log=[{"name": "list_pending_drafts", "duration_ms": 42}],
        actions_taken=[],
        cost_usd=0.012,
    )
    rows = store.load_recent_turns("conv-4")
    assert len(rows) == 2
    assert rows[0]["role"] == "user"
    assert rows[0]["content"] == "what's pending in my queue?"
    assert rows[0]["turn_idx"] == 0
    assert rows[1]["role"] == "assistant"
    assert rows[1]["turn_idx"] == 1
    assert rows[1]["tool_log"] == [{"name": "list_pending_drafts", "duration_ms": 42}]
    assert rows[1]["actions_taken"] == []
    assert rows[1]["cost_usd"] == pytest.approx(0.012)


def test_append_message_rejects_unknown_role(store):
    with pytest.raises(ValueError):
        store.append_message("conv-5", "system", "nope")


def test_append_message_allocates_monotonic_turn_idx(store):
    for i in range(5):
        store.append_message("conv-6", "user", f"q{i}")
    rows = store.load_recent_turns("conv-6", n=10)
    indices = [r["turn_idx"] for r in rows]
    assert indices == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# load_recent_turns
# ---------------------------------------------------------------------------

def test_load_recent_turns_returns_chronological_window(store):
    for i in range(12):
        # Disable prune for these by keeping below threshold = 20.
        store.append_message("conv-7", "user", f"u{i}")
    rows = store.load_recent_turns("conv-7", n=4)
    contents = [r["content"] for r in rows]
    assert contents == ["u8", "u9", "u10", "u11"]


def test_load_recent_turns_unknown_id_returns_empty(store):
    assert store.load_recent_turns("does-not-exist") == []


# ---------------------------------------------------------------------------
# Rolling summary
# ---------------------------------------------------------------------------

def test_rolling_summary_read_write(store):
    store.get_or_create("conv-8")
    assert store.get_summary("conv-8") == ""
    store.update_rolling_summary("conv-8", "Chad asked about framing dates.")
    assert store.get_summary("conv-8") == "Chad asked about framing dates."


def test_get_summary_unknown_id_returns_empty(store):
    assert store.get_summary("missing") == ""


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def test_prune_no_op_below_threshold(store):
    for i in range(store.PRUNE_THRESHOLD):  # exactly at threshold, not over
        store.append_message("conv-9", "user", f"u{i}")
    with patch.object(store, "_summarize_evicted") as mock_summary:
        ran = store.prune("conv-9")
    assert ran is False
    mock_summary.assert_not_called()
    assert store.message_count("conv-9") == store.PRUNE_THRESHOLD


def test_prune_summarizes_and_deletes_when_over_threshold(store):
    """Beyond PRUNE_THRESHOLD, prune evicts rows older than KEEP_RECENT
    and folds them into the rolling summary.

    Assertions are written in terms of the module constants so they
    stay correct if PRUNE_THRESHOLD / KEEP_RECENT get retuned (they
    were bumped 20→30 / 8→16 in commit 0dc09d9 to enlarge the memory
    retention window).
    """
    total_to_seed = store.PRUNE_THRESHOLD + 5

    # Disable the auto-prune triggered by append_message so we can drive
    # prune() explicitly (and not pay a Sonnet call mid-loop). We do this
    # by patching prune to a no-op for the seeding phase, then unpatching.
    with patch.object(store, "prune", return_value=False):
        for i in range(total_to_seed):
            role = "user" if i % 2 == 0 else "assistant"
            store.append_message("conv-10", role, f"msg-{i}")
    assert store.message_count("conv-10") == total_to_seed

    # Now run prune with a mocked summary call.
    with patch.object(store, "_summarize_evicted", return_value="folded summary"):
        ran = store.prune("conv-10")

    assert ran is True
    # KEEP_RECENT survive, the rest are gone.
    assert store.message_count("conv-10") == store.KEEP_RECENT
    surviving = store.load_recent_turns("conv-10", n=store.KEEP_RECENT)
    surviving_indices = [r["turn_idx"] for r in surviving]
    # The most recent KEEP_RECENT turn indices survive — turn_idx is
    # 0-indexed by append_message, so indices total_to_seed-KEEP_RECENT
    # through total_to_seed-1 (inclusive).
    expected_first = total_to_seed - store.KEEP_RECENT
    assert surviving_indices == list(range(expected_first, total_to_seed))
    # Summary was written.
    assert store.get_summary("conv-10") == "folded summary"


def test_prune_passes_existing_summary_to_summarizer(store):
    """When a rolling summary already exists, prune folds it in via
    _summarize_evicted's first arg."""
    with patch.object(store, "prune", return_value=False):
        for i in range(store.PRUNE_THRESHOLD + 3):
            store.append_message("conv-11", "user", f"u{i}")
    store.update_rolling_summary("conv-11", "Earlier: framing slipped a week.")

    captured = {}

    def fake_summary(existing, evict_rows):
        captured["existing"] = existing
        captured["evicted_count"] = len(evict_rows)
        return "merged summary"

    with patch.object(store, "_summarize_evicted", side_effect=fake_summary):
        store.prune("conv-11")

    assert captured["existing"] == "Earlier: framing slipped a week."
    # Evicted = total - KEEP_RECENT
    assert captured["evicted_count"] == store.PRUNE_THRESHOLD + 3 - store.KEEP_RECENT
    assert store.get_summary("conv-11") == "merged summary"


def test_append_message_triggers_prune_on_overflow(store):
    """Crossing PRUNE_THRESHOLD via append_message kicks prune
    automatically (Sonnet mocked)."""
    with patch.object(store, "_summarize_evicted", return_value="auto summary"):
        # Seed up to threshold without crossing.
        for i in range(store.PRUNE_THRESHOLD):
            store.append_message("conv-12", "user", f"u{i}")
        assert store.message_count("conv-12") == store.PRUNE_THRESHOLD
        assert store.get_summary("conv-12") == ""

        # One more push triggers prune.
        store.append_message("conv-12", "user", f"u{store.PRUNE_THRESHOLD}")

    # Post-prune we have KEEP_RECENT messages and a non-empty summary.
    assert store.message_count("conv-12") == store.KEEP_RECENT
    assert store.get_summary("conv-12") == "auto summary"


def test_append_message_swallows_prune_failure(store, capsys):
    """If Sonnet fails mid-prune, append_message must not raise — the
    caller's response is more important than the cleanup."""
    for i in range(store.PRUNE_THRESHOLD):
        store.append_message("conv-13", "user", f"u{i}")

    def boom(*args, **kwargs):
        raise RuntimeError("Sonnet exploded")

    with patch.object(store, "_summarize_evicted", side_effect=boom):
        # This should NOT raise.
        store.append_message("conv-13", "user", "trigger prune")

    captured = capsys.readouterr()
    assert "prune failed" in captured.err
    # Rows still all there because the prune transaction never committed.
    assert store.message_count("conv-13") == store.PRUNE_THRESHOLD + 1


# ---------------------------------------------------------------------------
# Helper-level integration: prior_turns_as_messages
# ---------------------------------------------------------------------------

def test_prior_turns_as_messages_strips_metadata():
    """The helper that bridges store rows to Anthropic-shaped messages
    should drop tool_log/actions_taken/cost — only role + content."""
    from home_builder_agent.agents.chad_agent import _prior_turns_as_messages

    rows = [
        {
            "turn_idx": 0,
            "role": "user",
            "content": "what's framing?",
            "tool_log": [],
            "actions_taken": [],
            "cost_usd": None,
            "created_at": "2026-05-09T00:00:00+00:00",
        },
        {
            "turn_idx": 1,
            "role": "assistant",
            "content": "Framing's at 60%, started Tuesday.",
            "tool_log": [{"name": "ask_chad"}],
            "actions_taken": [],
            "cost_usd": 0.05,
            "created_at": "2026-05-09T00:00:01+00:00",
        },
        {
            # Empty content rows are dropped.
            "turn_idx": 2,
            "role": "user",
            "content": "",
            "tool_log": [],
            "actions_taken": [],
            "cost_usd": None,
            "created_at": "2026-05-09T00:00:02+00:00",
        },
    ]
    out = _prior_turns_as_messages(rows)
    assert out == [
        {"role": "user", "content": "what's framing?"},
        {"role": "assistant", "content": "Framing's at 60%, started Tuesday."},
    ]


def test_system_with_summary_prefixes():
    from home_builder_agent.agents.chad_agent import _system_with_summary

    assert _system_with_summary("BASE", None) == "BASE"
    assert _system_with_summary("BASE", "") == "BASE"
    out = _system_with_summary("BASE", "Earlier: framing pushed.")
    assert "Earlier conversation context" in out
    assert "Earlier: framing pushed." in out
    assert out.endswith("BASE")
