"""conversation_store.py — Ask-tab conversation memory in SQLite.

Implements the persistence layer for the iOS Ask tab's `conversation_id`
plumbing per the 2026-05-09 ADR ("Ask Tab Conversation Memory via
Server-Side SQLite") and the VERTICAL_HANDOFF__ask_memory_attachments
brief.

Storage shape (matches the user-instructed schema, not the broader
handoff sketch — the prompt narrowed scope, no image_refs column in v1):

    conversations(
        id TEXT PRIMARY KEY,                  -- UUID from iOS
        user_id TEXT NOT NULL,                -- auth.users.id (Supabase JWT subject)
        project_id TEXT,                      -- nullable
        created_at TEXT NOT NULL,             -- ISO8601 UTC
        last_turn_at TEXT NOT NULL,           -- ISO8601 UTC
        rolling_summary TEXT NOT NULL DEFAULT ''
    )

    messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        turn_idx INTEGER NOT NULL,            -- monotonic per-conversation
        role TEXT NOT NULL CHECK (role IN ('user','assistant')),
        content TEXT NOT NULL,                -- final text only; no SSE deltas
        tool_log_json TEXT,                   -- JSON array; nullable
        actions_taken_json TEXT,              -- JSON array of strings; nullable
        cost_usd REAL,                        -- per-turn $ cost; nullable for user rows
        created_at TEXT NOT NULL              -- ISO8601 UTC
    )

DB file lives at ``~/Projects/home-builder-agent/.ask_conversations.db``
by default. The path is resolvable via the ``ASK_CONVERSATIONS_DB`` env
var so tests can point it at a tmp dir.

Pruning policy (load-bearing per CTO ADR — token bloat is the #1 risk):
on each `append_message` write, if the conversation now has more than
20 message rows, run `prune` which:
  1. selects messages older than the most recent 8
  2. asks Sonnet for a one-sentence summary of those messages,
     concatenated with the existing rolling_summary if any
  3. writes the new summary back to conversations.rolling_summary
  4. deletes those older messages

This bounds context window and per-turn cost. Older history is gone
from SQLite; the rolling_summary captures the gist.

Concurrency: SQLite with WAL gives single-writer/multi-reader. For
Phase A, the shell-backend is single-process (uvicorn workers=1 in
the launchd plist) so we don't need finer locking. WAL is enabled on
each connection.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

DEFAULT_DB_FILENAME = ".ask_conversations.db"


def _default_db_path() -> Path:
    """Return the default SQLite path: ~/Projects/home-builder-agent/.ask_conversations.db.

    Prefers the package-root parent (so a ``pip install -e .`` install
    points at the repo). Falls back to $HOME if the package layout
    isn't recognizable.
    """
    here = Path(__file__).resolve()
    # home_builder_agent/agents/conversation_store.py → repo root is two up
    repo_root = here.parents[2]
    if (repo_root / "pyproject.toml").exists():
        return repo_root / DEFAULT_DB_FILENAME
    return Path.home() / "Projects" / "home-builder-agent" / DEFAULT_DB_FILENAME


def db_path() -> Path:
    """Resolve the active DB path. ``ASK_CONVERSATIONS_DB`` env wins for tests."""
    env = os.environ.get("ASK_CONVERSATIONS_DB")
    if env:
        return Path(env)
    return _default_db_path()


# ---------------------------------------------------------------------------
# Connection + schema
# ---------------------------------------------------------------------------

# Per-thread connection cache. SQLite connections aren't safe to share
# across threads by default; the FastAPI app may dispatch chad_turn_stream
# via asyncio.to_thread which can land on different threads across calls.
_thread_local = threading.local()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), detect_types=sqlite3.PARSE_DECLTYPES, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Yield a per-thread, schema-initialized SQLite connection."""
    path = db_path()
    cached = getattr(_thread_local, "conn", None)
    cached_path = getattr(_thread_local, "path", None)
    if cached is None or str(cached_path) != str(path):
        # Path changed (or first call on this thread) — open a fresh conn.
        if cached is not None:
            try:
                cached.close()
            except Exception:
                pass
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(path)
        _ensure_schema(conn)
        _thread_local.conn = conn
        _thread_local.path = path
        cached = conn
    try:
        yield cached
    except Exception:
        cached.rollback()
        raise


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables idempotently. Cheap on every connection open."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id              TEXT PRIMARY KEY,
            user_id         TEXT NOT NULL,
            project_id      TEXT,
            created_at      TEXT NOT NULL,
            last_turn_at    TEXT NOT NULL,
            rolling_summary TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS messages (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id     TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            turn_idx            INTEGER NOT NULL,
            role                TEXT NOT NULL CHECK (role IN ('user','assistant')),
            content             TEXT NOT NULL,
            tool_log_json       TEXT,
            actions_taken_json  TEXT,
            cost_usd            REAL,
            created_at          TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_messages_by_conversation
            ON messages (conversation_id, turn_idx ASC);
        """
    )
    conn.commit()


def init_db() -> None:
    """Idempotent table creation. Useful for tests + first-run scripts."""
    with get_conn() as _:
        # _ensure_schema is called inside get_conn(); explicit init is
        # just the side effect of opening a connection.
        pass


# ---------------------------------------------------------------------------
# Smoke-test fallbacks
# ---------------------------------------------------------------------------

# Per CLAUDE.md: smoke-test bypass writes the canonical fixture user_id
# when the route can't resolve a real auth.users.id. Conversation rows
# need a non-null user_id; this is the v1 stand-in until JWT lands.
SMOKE_USER_ID = "11111111-1111-4111-a111-111111111111"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_or_create(
    conversation_id: str,
    user_id: str | None = None,
    project_id: str | None = None,
) -> dict:
    """Look up a conversation by id; create lazily if it doesn't exist.

    Returns a dict with keys ``id``, ``user_id``, ``project_id``,
    ``created_at``, ``last_turn_at``, ``rolling_summary``.

    ``user_id`` falls back to the smoke-test fixture if not supplied —
    this keeps the route working before JWT auth lands without a NOT
    NULL violation. Once JWT enforcement is live, the route should
    always supply a real user_id.
    """
    if not conversation_id:
        raise ValueError("conversation_id is required")
    user_id = user_id or SMOKE_USER_ID
    now = _now_iso()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, user_id, project_id, created_at, last_turn_at, rolling_summary "
            "FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if row is not None:
            return dict(row)
        conn.execute(
            "INSERT INTO conversations (id, user_id, project_id, created_at, last_turn_at, rolling_summary) "
            "VALUES (?, ?, ?, ?, ?, '')",
            (conversation_id, user_id, project_id, now, now),
        )
        conn.commit()
        return {
            "id": conversation_id,
            "user_id": user_id,
            "project_id": project_id,
            "created_at": now,
            "last_turn_at": now,
            "rolling_summary": "",
        }


def append_message(
    conversation_id: str,
    role: str,
    content: str,
    *,
    tool_log: list | None = None,
    actions_taken: list | None = None,
    cost_usd: float | None = None,
    user_id: str | None = None,
    project_id: str | None = None,
) -> int:
    """Persist a single message turn. Returns the new turn_idx.

    Auto-creates the conversation row if it doesn't exist (route can be
    careless about pre-creating). Triggers ``prune`` on the conversation
    if the post-write row count exceeds the prune threshold.

    Returns the turn_idx so callers can correlate the row.
    """
    if role not in ("user", "assistant"):
        raise ValueError(f"role must be 'user' or 'assistant', got {role!r}")
    get_or_create(conversation_id, user_id=user_id, project_id=project_id)
    now = _now_iso()
    tool_log_json = json.dumps(tool_log) if tool_log else None
    actions_taken_json = json.dumps(actions_taken) if actions_taken else None

    with get_conn() as conn:
        # Allocate next turn_idx.
        last_idx_row = conn.execute(
            "SELECT COALESCE(MAX(turn_idx), -1) AS last_idx FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        turn_idx = (last_idx_row["last_idx"] if last_idx_row else -1) + 1
        conn.execute(
            "INSERT INTO messages (conversation_id, turn_idx, role, content, "
            "tool_log_json, actions_taken_json, cost_usd, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                turn_idx,
                role,
                content,
                tool_log_json,
                actions_taken_json,
                cost_usd,
                now,
            ),
        )
        conn.execute(
            "UPDATE conversations SET last_turn_at = ? WHERE id = ?",
            (now, conversation_id),
        )
        conn.commit()

    # Prune AFTER the row is committed so we never lose data on a
    # summary-call failure.
    try:
        prune(conversation_id)
    except Exception as e:
        # Pruning is best-effort. If Sonnet fails we keep all history
        # and log to stderr; next write retries.
        print(
            f"[conversation_store] prune failed for {conversation_id}: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )

    return turn_idx


def load_recent_turns(conversation_id: str, n: int = 8) -> list[dict]:
    """Return the last ``n`` messages for ``conversation_id`` in chronological order.

    Returns an empty list if the conversation doesn't exist or has no
    messages. Each row is a dict with keys ``turn_idx``, ``role``,
    ``content``, ``tool_log``, ``actions_taken``, ``cost_usd``,
    ``created_at`` (tool_log + actions_taken are decoded from JSON).
    """
    if not conversation_id:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT turn_idx, role, content, tool_log_json, actions_taken_json, "
            "cost_usd, created_at "
            "FROM messages WHERE conversation_id = ? "
            "ORDER BY turn_idx DESC LIMIT ?",
            (conversation_id, n),
        ).fetchall()
    # rows are newest-first; reverse so the caller gets chronological order.
    rows = list(reversed(rows))
    return [
        {
            "turn_idx": r["turn_idx"],
            "role": r["role"],
            "content": r["content"],
            "tool_log": json.loads(r["tool_log_json"]) if r["tool_log_json"] else [],
            "actions_taken": (
                json.loads(r["actions_taken_json"]) if r["actions_taken_json"] else []
            ),
            "cost_usd": r["cost_usd"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def update_rolling_summary(conversation_id: str, summary: str) -> None:
    """Replace the rolling summary on an existing conversation row."""
    if not conversation_id:
        raise ValueError("conversation_id is required")
    with get_conn() as conn:
        conn.execute(
            "UPDATE conversations SET rolling_summary = ? WHERE id = ?",
            (summary, conversation_id),
        )
        conn.commit()


def get_summary(conversation_id: str) -> str:
    """Return the rolling summary for a conversation, or empty string."""
    if not conversation_id:
        return ""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT rolling_summary FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
    if row is None:
        return ""
    return row["rolling_summary"] or ""


def message_count(conversation_id: str) -> int:
    """Count total messages for a conversation. Used by tests + prune."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
    return int(row["n"]) if row else 0


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

# Threshold: prune when this many messages exist. Keep the most recent
# KEEP_RECENT in SQLite + the context window; everything older is
# summarized then deleted. Sized so load_recent_turns(n=16) survives a
# prune — KEEP_RECENT must be ≥ the largest `n` any caller requests.
PRUNE_THRESHOLD = 30
KEEP_RECENT = 16

# Sonnet model id for summarization. Mirrors core/claude_client + config.
_SUMMARY_MODEL = "claude-sonnet-4-6"
_SUMMARY_MAX_TOKENS = 256


def prune(conversation_id: str) -> bool:
    """If the conversation exceeds PRUNE_THRESHOLD messages, summarize-and-delete.

    Selects everything older than the most recent KEEP_RECENT, asks
    Sonnet to fold those into the existing rolling summary in one
    sentence, writes the new summary, deletes the old rows. Returns
    True if pruning ran, False otherwise.

    On any Sonnet failure this raises (caller swallows + logs); we never
    delete rows without a successful summary in hand.
    """
    if message_count(conversation_id) <= PRUNE_THRESHOLD:
        return False

    with get_conn() as conn:
        # Fetch existing summary + the rows we're about to evict.
        summary_row = conn.execute(
            "SELECT rolling_summary FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        existing_summary = (summary_row["rolling_summary"] if summary_row else "") or ""

        # Find the cutoff turn_idx — the (count - KEEP_RECENT)th oldest.
        # Anything strictly less than that turn_idx gets evicted.
        evict_rows = conn.execute(
            "SELECT id, turn_idx, role, content FROM messages "
            "WHERE conversation_id = ? "
            "ORDER BY turn_idx DESC "
            "LIMIT -1 OFFSET ?",
            (conversation_id, KEEP_RECENT),
        ).fetchall()

    if not evict_rows:
        return False

    # rows are newest-first via the OFFSET trick; reverse for prompt clarity
    evict_rows = list(reversed(evict_rows))
    evict_ids = [r["id"] for r in evict_rows]

    new_summary = _summarize_evicted(existing_summary, evict_rows)

    with get_conn() as conn:
        conn.execute(
            "UPDATE conversations SET rolling_summary = ? WHERE id = ?",
            (new_summary, conversation_id),
        )
        # Bulk-delete the rows we just absorbed.
        placeholders = ",".join("?" * len(evict_ids))
        conn.execute(
            f"DELETE FROM messages WHERE id IN ({placeholders})",
            evict_ids,
        )
        conn.commit()
    return True


def _summarize_evicted(existing_summary: str, evict_rows: list[Any]) -> str:
    """Fold ``evict_rows`` into ``existing_summary`` via Sonnet. One sentence."""
    # Lazy import so the module imports cheaply when summarization isn't
    # exercised (CLI smoke tests, schema-only callers).
    from home_builder_agent.core.claude_client import make_client

    client = make_client()

    transcript_lines: list[str] = []
    for r in evict_rows:
        role = r["role"] if isinstance(r, sqlite3.Row) else r.get("role")
        content = r["content"] if isinstance(r, sqlite3.Row) else r.get("content")
        # Truncate any individual turn that's pathologically long so the
        # prompt itself doesn't bloat. 600 chars is enough for a useful
        # one-sentence summary line.
        content = (content or "").strip()
        if len(content) > 600:
            content = content[:600] + " […]"
        transcript_lines.append(f"{role.upper()}: {content}")

    transcript = "\n".join(transcript_lines)

    if existing_summary:
        prompt = (
            "You are maintaining a single-sentence rolling summary of an "
            "ongoing conversation between Chad (a custom-home builder) "
            "and his AI assistant.\n\n"
            f"Existing summary: {existing_summary}\n\n"
            "New conversation turns to fold in:\n"
            f"{transcript}\n\n"
            "Write an updated single-sentence summary that captures what "
            "has been decided, asked, or accomplished across the existing "
            "summary AND the new turns. Stay under 200 tokens. No "
            "preamble — output just the sentence."
        )
    else:
        prompt = (
            "You are summarizing the early portion of a conversation "
            "between Chad (a custom-home builder) and his AI assistant.\n\n"
            f"Conversation turns to summarize:\n{transcript}\n\n"
            "Write a single sentence that captures what has been decided, "
            "asked, or accomplished. Stay under 200 tokens. No preamble — "
            "output just the sentence."
        )

    response = client.messages.create(
        model=_SUMMARY_MODEL,
        max_tokens=_SUMMARY_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    summary = "".join(parts).strip()
    # Defense: if the model returned multiple sentences, keep the first.
    return summary or existing_summary
