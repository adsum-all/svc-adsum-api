"""Shared SQL predicate that keeps technical (applicative) identities out of every
member surface.

A technical super-admin is never a member: it has no member row and must never appear
in the directory, the validation queues, the dashboard counts or the duplicate detector.
This predicate excludes any member row tied to a technical account, whether by the
``membre_id`` link OR merely by sharing its e-mail (a stray member row carrying a
technical account's address, such as the founder's, must never surface). The alias is a
fixed internal constant, never user input, so it cannot be used for injection.
"""
from __future__ import annotations


def exclusion_technique(alias: str = "membre") -> str:
    """Return a boolean SQL predicate (for a WHERE/AND clause) that is TRUE only for a
    member row that is NOT a technical identity. ``alias`` is the member table alias used
    in the surrounding query (e.g. ``m`` or ``membre``)."""
    return (
        "NOT EXISTS (SELECT 1 FROM utilisateur u WHERE u.acces_technique_global = true "
        f"AND (u.membre_id = {alias}.id OR ({alias}.email IS NOT NULL AND lower(u.email) = lower({alias}.email))))"
    )
