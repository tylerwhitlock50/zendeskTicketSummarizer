"""Postgres data layer for the scan portal.

Plain SQL via psycopg v3 with a connection pool. Tables live in the ``portal``
schema; run ``python migrations/migrate.py`` to create them. Timestamps are
stored UTC (timestamptz).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: Optional[ConnectionPool] = None


def init_pool(dsn: str) -> None:
    """Create the module-level connection pool. Safe to call once at startup."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            dsn, min_size=1, max_size=8, open=True, kwargs={"row_factory": dict_row}
        )


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def available() -> bool:
    return _pool is not None


@contextmanager
def _conn() -> Iterator[psycopg.Connection]:
    if _pool is None:
        raise RuntimeError("db.init_pool() has not been called")
    with _pool.connection() as conn:
        yield conn


def shared_secret() -> Optional[str]:
    """A random session-signing key stored in the DB, shared by every worker.

    Created on first use; returns None if the table isn't there yet (run the
    migrations) or the database can't be reached.
    """
    import secrets

    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO repair.app_secret (id, secret) VALUES (1, %s) ON CONFLICT (id) DO NOTHING",
                (secrets.token_urlsafe(48),),
            )
            row = conn.execute("SELECT secret FROM repair.app_secret WHERE id = 1").fetchone()
    except psycopg.Error:
        return None
    return row["secret"] if row else None


# ---------------------------------------------------------------- quick actions


def list_quick_actions(include_inactive: bool = False) -> list[dict[str, Any]]:
    where = "" if include_inactive else "WHERE active"
    with _conn() as conn:
        return conn.execute(
            f"SELECT id, label, comment_body, tag, position, active, public "
            f"FROM portal.quick_action {where} ORDER BY position, id"
        ).fetchall()


def get_quick_action(action_id: int) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        return conn.execute(
            "SELECT id, label, comment_body, tag, position, active, public "
            "FROM portal.quick_action WHERE id = %s",
            (action_id,),
        ).fetchone()


def create_quick_action(label: str, comment_body: str, tag: str, public: bool = True) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO portal.quick_action (label, comment_body, tag, public, position) "
            "VALUES (%s, %s, %s, %s, "
            "  (SELECT COALESCE(MAX(position), 0) + 10 FROM portal.quick_action))",
            (label, comment_body, tag, public),
        )


def update_quick_action(
    action_id: int, label: str, comment_body: str, tag: str, position: int, active: bool,
    public: bool = True,
) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE portal.quick_action "
            "SET label = %s, comment_body = %s, tag = %s, position = %s, active = %s, public = %s "
            "WHERE id = %s",
            (label, comment_body, tag, position, active, public, action_id),
        )


# ---------------------------------------------------------------- action log


def log_action(
    ticket_id: int, action_label: str, note_excerpt: str | None, tech_initials: str | None
) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO portal.action_log (ticket_id, action_label, note_excerpt, tech_initials) "
            "VALUES (%s, %s, %s, %s)",
            (ticket_id, action_label, note_excerpt, tech_initials),
        )


def recent_actions(ticket_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with _conn() as conn:
        return conn.execute(
            "SELECT action_label, note_excerpt, tech_initials, created_at "
            "FROM portal.action_log WHERE ticket_id = %s "
            "ORDER BY created_at DESC LIMIT %s",
            (ticket_id, limit),
        ).fetchall()
