"""postgres.py — direct Postgres connection helper for engine ops.

Phase A architecture (per turtle_contract_v1.md § 3): the home-builder
engine runs on the Mac Mini and connects directly to Supabase Postgres
as superuser via the standard `DATABASE_URL`. This bypasses RLS — which
is exactly what we want for engine writes — and skips the PostgREST
hop that the iOS shell uses for user-scoped reads.

Two connection paths exist in the system, and they're not interchangeable:

| Caller | URL | Auth | RLS |
|---|---|---|---|
| Engine (Mac Mini, this module) | DATABASE_URL | DB password | bypassed (superuser) |
| iOS shell backend | SUPABASE_URL via PostgREST | service-role JWT | enforced (auth.role() = 'service_role') |

Connection model (Phase A):
- Engine processes (status_updater, dashboard refresher, future workers)
  open a fresh connection per run via `connect()`. Fire-and-exit, no pool.
- Long-running watchers (inbox, dashboard) reuse a single connection
  for the duration of their tick.
- All writes use the engine's service identity (DB superuser) so RLS
  doesn't block engine ops.

Phase B (post customer #2): connection pool moves into Modal/Railway
worker dynos. Mac Mini retires. URL stays the same shape.

Required environment variables (loaded from .env via python-dotenv):
- DATABASE_URL — Supabase direct-postgres connection string
                 (format: postgresql://postgres.<ref>:<pw>@<region>.pooler.supabase.com:6543/postgres)
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row


DATABASE_URL_ENV = "DATABASE_URL"


# Load .env on import so this module works regardless of whether
# the entry point already called load_dotenv() (e.g. via claude_client).
# Idempotent — calling it twice is a no-op.
load_dotenv()


class PostgresConfigError(RuntimeError):
    """Raised when the engine can't find or open a Postgres connection."""


def _get_database_url() -> str:
    """Read DATABASE_URL from env, with a clear error if missing."""
    url = os.environ.get(DATABASE_URL_ENV)
    if not url:
        raise PostgresConfigError(
            f"Environment variable {DATABASE_URL_ENV} not set. "
            "Add it to .env or your launchd plist's EnvironmentVariables. "
            "Format: postgresql://postgres.<project-ref>:<password>@<region>.pooler.supabase.com:6543/postgres"
        )
    return url


def connect(
    *,
    autocommit: bool = False,
    application_name: str = "home-builder-engine",
) -> psycopg.Connection:
    """Open a fresh psycopg connection to Postgres.

    Args:
        autocommit:        True for read-only or fire-and-forget writes.
                           False (default) wraps the unit of work in a
                           transaction the caller controls.
        application_name:  Surfaced in `pg_stat_activity` for observability.
                           Default 'home-builder-engine'; pass a more
                           specific name (e.g. 'morning-brief') in the
                           caller for easier debugging.

    Returns:
        psycopg.Connection with `dict_row` as the default row factory.
        Caller owns lifecycle — close it when done, ideally via the
        `connection()` context manager below.
    """
    url = _get_database_url()
    # Use dict_row as default so query results are dicts keyed on column name —
    # matches the shape the adapter layer expects.
    conn = psycopg.connect(
        url,
        autocommit=autocommit,
        application_name=application_name,
        row_factory=dict_row,
    )
    return conn


@contextmanager
def connection(
    *,
    autocommit: bool = False,
    application_name: str = "home-builder-engine",
) -> Iterator[psycopg.Connection]:
    """Context-managed connection. Use this in 95% of call sites.

    Usage:
        with connection(application_name='hb-schedule') as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM home_builder.project WHERE name = %s", (name,))
                row = cur.fetchone()
    """
    conn = connect(autocommit=autocommit, application_name=application_name)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def ping() -> dict:
    """Smoke test — confirms the engine can reach Postgres and the schema is live.

    Returns a dict with diagnostics:
        - server_version (str)
        - schema_present (bool) — True if home_builder schema exists
        - tables_in_home_builder (int)

    Raises PostgresConfigError if the env var isn't set.
    Raises psycopg.OperationalError if the connection fails.
    """
    with connection(application_name="home-builder-ping") as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version() AS server_version;")
            row = cur.fetchone()
            server_version = row["server_version"] if row else "unknown"

            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.schemata "
                "WHERE schema_name = 'home_builder') AS schema_present;"
            )
            row = cur.fetchone()
            schema_present = bool(row["schema_present"]) if row else False

            tables_in_home_builder = 0
            if schema_present:
                cur.execute(
                    "SELECT count(*)::int AS n FROM information_schema.tables "
                    "WHERE table_schema = 'home_builder';"
                )
                row = cur.fetchone()
                tables_in_home_builder = row["n"] if row else 0

    return {
        "server_version": server_version,
        "schema_present": schema_present,
        "tables_in_home_builder": tables_in_home_builder,
    }
