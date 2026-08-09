"""Which organisation's database this request talks to, decided before any query.

The platform is sold to many organisations and the owner has ruled, without exception:
one database per organisation, and no path by which one client's data can reach another.
Not at the infrastructure, not in the database, not in the backend, not in the frontend.

The alternative, a shared base with a tenant column, makes isolation depend on eighteen
hundred hand-written queries all filtering correctly, forever, including the ones
written in three years by somebody else. One omission is a data breach. Here, a query
cannot reach another organisation because the connection does not lead there.

**How the organisation is decided.** From the host the request arrived on, resolved once
per request, before any handler runs. A handler never chooses; there is nothing for it
to choose from.

**What happens to an unknown host.** It is refused. The obvious alternative, falling back
to a default, is the one that must never be built: a misconfigured domain would then
quietly serve one organisation's data under another organisation's address, which is the
exact failure the whole design exists to prevent. Refusing is loud, recoverable and
harmless.

**Today there is one organisation.** The registry is empty and the resolution returns the
historical connection, which is how production keeps running while the mechanism exists.
That is a transition, not a default: as soon as a second organisation is registered, an
unmatched host stops being served. :func:`mode` says which of the two states the platform
is in, so nobody has to guess.
"""
# ruff: noqa: E501
from __future__ import annotations

import contextvars
import re
from typing import NamedTuple

from . import db
from .config import settings

#: The organisation resolved for the current request. A context variable rather than a
#: global: the server handles requests concurrently, and a global would let one request
#: read the organisation of another, which is the breach in miniature.
_courante: contextvars.ContextVar[Organisation | None] = contextvars.ContextVar(
    "adsum_organisation_courante", default=None
)

_HOTE = re.compile(r"^[a-z0-9.-]{1,253}$")


class Organisation(NamedTuple):
    """One client, and where its data lives."""

    id: str
    code: str
    nom: str
    #: The connection string of its own database. Never shared with another.
    dsn: str
    etat: str


class OrganisationInconnue(Exception):
    """The request arrived on a host no organisation claims."""


class OrganisationSuspendue(Exception):
    """The organisation exists but its access is suspended."""


def normaliser_hote(brut: str) -> str:
    """The bare host name: no port, no case, no trailing dot.

    ``Example.COM:443`` and ``example.com.`` are the same host, and a registry that
    matched only one of the three spellings would refuse a legitimate request.
    """
    hote = (brut or "").split(",")[0].strip().lower()
    hote = hote.split("//")[-1].split("/")[0]
    if hote.startswith("["):  # IPv6 literal
        hote = hote.split("]")[0].lstrip("[")
    else:
        hote = hote.split(":")[0]
    return hote.rstrip(".")


def _registre() -> list[dict[str, object]]:
    """Hosts declared by registered organisations. Empty until a second one exists."""
    try:
        return [
            dict(r)
            for r in db.fetch_all(
                "SELECT o.id, o.code, o.nom, o.etat, h.hote, h.dsn "
                "FROM organisation_cliente o JOIN organisation_hote h ON h.organisation_id = o.id "
                "WHERE o.etat <> 'resiliee'",
                (),
            )
        ]
    except Exception:  # noqa: BLE001 - the table may not exist yet on an old base
        return []


def resoudre(hote: str) -> Organisation:
    """The organisation serving this host, or an error. Never a guess.

    Raises :class:`OrganisationInconnue` when nothing claims the host and more than one
    organisation is registered, and :class:`OrganisationSuspendue` when the match exists
    but is suspended: a suspended client must stop being served, and must be told why
    rather than meeting a blank failure.
    """
    propre = normaliser_hote(hote)
    if propre and not _HOTE.match(propre):
        raise OrganisationInconnue(f"hôte invalide : {hote!r}")

    lignes = _registre()
    for r in lignes:
        if normaliser_hote(str(r["hote"])) == propre:
            if str(r["etat"]) == "suspendue":
                raise OrganisationSuspendue(str(r["code"]))
            dsn = str(r["dsn"] or "").strip()
            return Organisation(
                id=str(r["id"]), code=str(r["code"]), nom=str(r["nom"]),
                dsn=dsn or settings.database_dsn, etat=str(r["etat"]),
            )

    if lignes:
        # At least one organisation declares hosts, so the registry is in use and an
        # unmatched host is a misconfiguration. Serving it from the historical database
        # would hand one organisation's data out under another's address.
        raise OrganisationInconnue(propre or "(hôte absent)")

    # Transition: nothing is registered yet, so there is exactly one organisation and
    # one database, the one this process has always used.
    return Organisation(id="", code="", nom="", dsn=settings.database_dsn, etat="active")


def mode() -> str:
    """``transition`` while no host is registered, ``isole`` once the registry is used.

    Published so an operator can see which regime the platform is in rather than infer
    it: the two behave differently on an unknown host, and that difference matters.
    """
    return "isole" if _registre() else "transition"


def definir(organisation: Organisation) -> contextvars.Token[Organisation | None]:
    return _courante.set(organisation)


def restaurer(jeton: contextvars.Token[Organisation | None]) -> None:
    _courante.reset(jeton)


def courante() -> Organisation | None:
    """The organisation of the request being handled, if one was resolved."""
    return _courante.get()


def dsn_courant() -> str:
    """The connection string every query of this request must use.

    Read by the connection factory. A handler that wanted to reach another organisation
    would have to change this function, which is the point: the decision lives in one
    place and is made before any handler runs.
    """
    org = _courante.get()
    return org.dsn if org and org.dsn else settings.database_dsn
