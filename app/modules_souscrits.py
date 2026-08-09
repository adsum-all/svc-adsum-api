"""Which modules an organisation has paid for, and the refusal when it has not.

The product is sold by module and the price follows the count. The owner's rule is
explicit: **a module that has not been subscribed is not deployed, and its API refuses
access.** Hiding a button is decoration. The endpoints stay reachable to anyone who
types the address, and an organisation that dropped a module from its contract would
keep using it exactly as before, which means the platform is sold on the honour system.

Enforcement is one dependency, applied to the routes that belong to a module. It answers
402 rather than 403, because the two are different situations leading to different
actions: 403 says this account may not, 402 says this organisation has not subscribed,
and only the second is settled by a conversation with the publisher.

An empty subscription means the whole catalogue. The organisation running today names no
module, because the table did not exist until now, and reading that as "no module" would
take the platform offline the moment this shipped. Once a licence names one module it
names them all, so the absence of a row becomes a refusal rather than a silence.
"""
# ruff: noqa: E501
from __future__ import annotations

from collections.abc import Callable

from fastapi import HTTPException, status

from . import db
from . import organisation_courante as oc


def catalogue() -> list[dict[str, object]]:
    """Every module the platform can sell, with whether it is currently subscribed."""
    souscrits = souscriptions()
    return [
        {
            "code": str(r["code"]),
            "nom": str(r["nom"]),
            "description": r["description"],
            "actif": bool(r["actif"]),
            "souscrit": (not souscrits) or str(r["code"]) in souscrits,
        }
        for r in db.fetch_all("SELECT code, nom, description, actif FROM application ORDER BY ordre, nom", ())
    ]


def souscriptions() -> set[str]:
    """The module codes covered by the organisation's licence in force.

    Empty means "everything", which is the transition state described above and not a
    licence covering nothing. :func:`souscrit` is where that reading is applied, so no
    caller has to remember it.
    """
    organisation = oc.courante()
    try:
        if organisation and organisation.id:
            lignes = db.fetch_all(
                "SELECT lm.application_code FROM licence_module lm "
                "JOIN licence l ON l.id = lm.licence_id "
                "WHERE l.remplacee_le IS NULL AND l.organisation_id = %s",
                (organisation.id,),
            )
        else:
            # In transition there is exactly one organisation, so the licence in force is
            # unambiguous without needing to know which one it belongs to.
            lignes = db.fetch_all(
                "SELECT lm.application_code FROM licence_module lm "
                "JOIN licence l ON l.id = lm.licence_id WHERE l.remplacee_le IS NULL",
                (),
            )
    except Exception:  # noqa: BLE001 - an older base has no such table
        return set()
    return {str(r["application_code"]) for r in lignes}


def souscrit(code: str) -> bool:
    """Whether this module may be served. Empty subscription means the whole catalogue."""
    codes = souscriptions()
    return (not codes) or code in codes


def exiger(code: str) -> Callable[[], None]:
    """FastAPI dependency: refuse a module the organisation has not subscribed to.

    Applied to a router, it covers every route under it at once, which matters: a rule
    applied endpoint by endpoint is a rule that will be forgotten on the next endpoint.
    """

    def _dep() -> None:
        if not souscrit(code):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=(
                    f"Le module « {code} » ne fait pas partie de l'abonnement de cette organisation. "
                    "Contactez l'éditeur pour l'ajouter."
                ),
            )

    return _dep


def definir_modules(licence_id: str, codes: list[str], role: str | None = None) -> list[str]:
    """Set exactly which modules a licence covers, replacing what was there.

    Written as a whole rather than added one by one: a client's contract is a list, and
    applying it as a series of additions leaves whatever was removed still in force.
    """
    connus = {str(r["code"]) for r in db.fetch_all("SELECT code FROM application", (), role=role)}
    inconnus = sorted(set(codes) - connus)
    if inconnus:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Module inconnu : {', '.join(inconnus)}",
        )
    db.execute("DELETE FROM licence_module WHERE licence_id = %s", (licence_id,), role=role)
    for code in sorted(set(codes)):
        db.execute(
            "INSERT INTO licence_module (licence_id, application_code) VALUES (%s, %s)",
            (licence_id, code),
            role=role,
        )
    return sorted(set(codes))
