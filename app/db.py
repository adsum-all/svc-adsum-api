"""Database access against the real PostgreSQL.

Each query runs in a transaction where the caller role is set as the session
variable adsum.role, which activates the per-role RLS policies (ADR-0002).

Note on connection handling: a persistent connection pool was tried but does not
survive Vercel's serverless freeze/thaw model (frozen maintenance threads, the
pooler closes idle connections), which produced invocation failures. On
serverless we therefore open a short-lived connection per request. The proper
warm-pool optimisation belongs on a persistent host (VM or managed service);
moving the API there is the way to remove the per-request connection handshake.
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
                # Transaction-local so the role never leaks to another request that
                # reuses the same pooled connection (Supabase transaction pooler).
                cur.execute("SELECT set_config('adsum.role', %s, true)", (role,))
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
    # Case-insensitive so every auth flow resolves the SAME account for a given address
    # (aligned with get_user_by_identifier and the lower(email) unique index). Without this,
    # two rows differing only by letter case could resolve differently between flows.
    return fetch_one(
        "SELECT id, email, hash_mdp, role, membre_id, actif, "
        "mdp_temporaire, mdp_expire_le, doit_changer_mdp, "
        "mfa_actif, mfa_canal, cree_le "
        "FROM utilisateur WHERE lower(email) = lower(%s)",
        (email,),
    )


def get_user_by_identifier(ident: str) -> dict[str, Any] | None:
    """Resolve the account from an identifier that may be the e-mail, the ADSUM
    matricule, or the member code. E-mail is matched case-insensitively; matricule and
    member code are stored upper-case. Returns the same columns as
    :func:`get_user_by_email` so the login flow is identical whichever method is used.
    """
    ident = (ident or "").strip()
    if not ident:
        return None
    # Deterministic priority e-mail > matricule > member code. A member can set
    # their own member code once a field is unlocked; without this ordering a code
    # colliding with another member's matricule could make a bare LIMIT 1 resolve
    # to the wrong row (the legitimate matricule owner would then fail to sign in).
    # Ranking the matricule match above the code match guarantees the higher-trust
    # identifier always wins, so a colliding code can never hijack a matricule login.
    return fetch_one(
        "SELECT u.id, u.email, u.hash_mdp, u.role, u.membre_id, u.actif, "
        "u.mdp_temporaire, u.mdp_expire_le, u.doit_changer_mdp, u.mfa_actif, u.mfa_canal, u.cree_le, "
        "u.acces_technique_global "
        "FROM utilisateur u LEFT JOIN membre m ON m.id = u.membre_id "
        "WHERE lower(u.email) = lower(%s) OR upper(m.matricule) = upper(%s) "
        "OR (m.code_membre IS NOT NULL AND m.code_membre <> '' AND upper(m.code_membre) = upper(%s)) "
        "ORDER BY (lower(u.email) = lower(%s)) DESC, (upper(m.matricule) = upper(%s)) DESC, "
        "(m.code_membre IS NOT NULL AND upper(m.code_membre) = upper(%s)) DESC "
        "LIMIT 1",
        (ident, ident, ident, ident, ident, ident),
    )


def get_user_by_id(user_id: str, role: str) -> dict[str, Any] | None:
    return fetch_one(
        "SELECT id, email, role, membre_id, actif, acces_technique_global, niveau_technique "
        "FROM utilisateur WHERE id = %s",
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
