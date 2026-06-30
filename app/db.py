"""Database access against the real PostgreSQL.

Connections are served from a persistent pool that survives across warm
invocations, so a query does not pay a fresh TCP+TLS+pooler handshake every
time (this was the dominant per-request latency). Each query still runs in a
transaction where the caller role is set as the transaction-local session
variable ``adsum.role``, which activates the per-role RLS policies (ADR-0002)
and never leaks to another request that reuses the same pooled connection.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

_pool: ConnectionPool | None = None


def _get_pool() -> ConnectionPool:
    """Lazily build the process-wide connection pool.

    ``min_size=0`` keeps cold start cheap (no connection opened at import);
    warm requests reuse the small set of open connections (``max_size``),
    which removes the per-request connection handshake.
    """
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_dsn,
            min_size=0,
            max_size=4,
            max_idle=120,
            timeout=10,
            # prepare_threshold=None disables prepared statements, required for
            # the Supabase pooler in transaction mode (PgBouncer).
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            open=True,
        )
    return _pool


@contextmanager
def connection(role: str | None = None) -> Iterator[psycopg.Connection]:
    pool = _get_pool()
    with pool.connection() as conn:
        if role:
            with conn.cursor() as cur:
                # Transaction-local so the role never leaks to another request
                # that reuses the same pooled connection.
                cur.execute("SELECT set_config('adsum.role', %s, true)", (role,))
        yield conn
        # pool.connection() commits on clean exit and rolls back on exception.


def fetch_one(sql: str, params: tuple[Any, ...], role: str | None = None) -> dict[str, Any] | None:
    with connection(role) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def fetch_all(sql: str, params: tuple[Any, ...], role: str | None = None) -> list[dict[str, Any]]:
    with connection(role) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def get_user_by_email(email: str) -> dict[str, Any] | None:
    return fetch_one(
        "SELECT id, email, hash_mdp, role, membre_id, actif FROM utilisateur WHERE email = %s",
        (email,),
    )


def get_user_by_id(user_id: str, role: str) -> dict[str, Any] | None:
    return fetch_one(
        "SELECT id, email, role, membre_id, actif FROM utilisateur WHERE id = %s",
        (user_id,),
        role=role,
    )


def execute(sql: str, params: tuple[Any, ...], role: str | None = None) -> dict[str, Any] | None:
    """Run a write statement and return its RETURNING row, if any."""
    with connection(role) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return None
        return cur.fetchone()
