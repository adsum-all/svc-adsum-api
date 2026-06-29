"""Database access against the real PostgreSQL.

Each query runs in a transaction where the caller role is set as the session
variable adsum.role, which activates the per-role RLS policies (ADR-0002).
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .config import settings


@contextmanager
def connection(role: str | None = None) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(settings.database_dsn, row_factory=dict_row)
    try:
        if role:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('adsum.role', %s, false)", (role,))
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
