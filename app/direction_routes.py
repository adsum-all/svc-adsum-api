"""HTTP surface of the direction dashboard.

Every endpoint here takes the same filter set and reads the same consolidation, so a
screen sets its filters once and every panel narrows together. That uniformity is the
point: a dashboard where the header says "third quarter" and one chart quietly shows
the whole year is worse than one with no filters at all.

All of it is read-only and gated on ``statistiques.consulter``. The direction steers
from aggregates; managing members is the back office's job and stays there.

An unknown dimension or filter is answered with 422 and the list of accepted keys,
never with an unfiltered result. Silently widening the population under a heading
that says otherwise is the one failure mode a filtered dashboard cannot have.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from . import direction_analyse as analyse
from . import direction_membres, direction_referentiels
from . import direction_pilotage as pilotage
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/direction", tags=["direction"])

_Lecteur = Annotated[UserMe, Depends(require_permission("statistiques.consulter"))]

# The nominative follow-up is gated apart from the aggregates, on the permission that
# already governs reading members. Statistics and names are different disclosures and
# an installation must be able to grant one without the other.
_LecteurNominatif = Annotated[UserMe, Depends(require_permission("membres.consulter"))]


def _filtres(
    coordination: str | None, intendance: str | None, commission: str | None,
    tribu: str | None, volet: str | None, evenement: str | None, pays: str | None,
    genre: str | None, type_membre: str | None, depuis: str | None, jusqu_a: str | None,
) -> dict[str, Any]:
    """Collect the query filters into the shape the analytical layer expects."""
    return {
        "coordination": coordination, "intendance": intendance, "commission": commission,
        "tribu": tribu, "volet": volet, "evenement": evenement, "pays": pays,
        "genre": genre, "type_membre": type_membre, "depuis": depuis, "jusqu_a": jusqu_a,
    }


#: The filter query parameters, declared once and reused by every endpoint below.
#: Written as a dependency rather than repeated per route so a filter can never exist
#: on one panel and be missing from another on the same screen.
def filtres_communs(
    coordination: str | None = Query(default=None, description="Identifiant de coordination"),
    intendance: str | None = Query(default=None, description="Identifiant d'intendance"),
    commission: str | None = Query(default=None, description="Identifiant de commission"),
    tribu: str | None = Query(default=None, description="Identifiant de tribu"),
    volet: str | None = Query(default=None, description="Volet de l'activité"),
    evenement: str | None = Query(default=None, description="Identifiant d'activité"),
    pays: str | None = Query(default=None),
    genre: str | None = Query(default=None),
    type_membre: str | None = Query(default=None),
    depuis: str | None = Query(default=None, description="Date de début, format ISO"),
    jusqu_a: str | None = Query(default=None, description="Date de fin, format ISO"),
    donnees: str | None = Query(
        default=None,
        description="toutes (défaut), reelles, ou demonstration",
    ),
) -> dict[str, Any]:
    base = _filtres(
        coordination, intendance, commission, tribu, volet, evenement,
        pays, genre, type_membre, depuis, jusqu_a,
    )
    base["donnees"] = donnees
    return base


_Filtres = Annotated[dict[str, Any], Depends(filtres_communs)]


def _refuser(err: analyse.DimensionInconnue) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=(
            f"Paramètre inconnu : {err}. "
            f"Dimensions acceptées : {', '.join(sorted(analyse.DIMENSIONS))}. "
            f"Filtres acceptés : {', '.join(analyse.filtres_disponibles())}."
        ),
    )


@router.get("/dimensions")
def dimensions(user: _Lecteur) -> dict[str, Any]:
    """The axes and filters the screens may offer.

    Served rather than hard-coded in the front so a dimension added here appears in
    the pickers without a second deployment, and so the two can never disagree about
    what the platform actually computes.
    """
    return {
        "dimensions": analyse.dimensions_disponibles(),
        "filtres": analyse.filtres_disponibles(),
        "mesures": ["presents", "participants", "absents", "total"],
    }


@router.get("/referentiels")
def referentiels(user: _Lecteur) -> dict[str, Any]:
    """The lists the filter bar offers, served under the direction's own permission.

    One round trip instead of four calls to administration endpoints, one of which
    required a member-management permission the direction does not hold and answered
    403 on every page load.
    """
    return direction_referentiels.referentiels(user.role)


@router.get("/regles-calcul")
def regles_calcul(user: _Lecteur, filtres: _Filtres) -> dict[str, Any]:
    """Every figure the platform publishes, with its definition, its formula and its audit.

    Served so an organisation can check the arithmetic instead of being asked to trust
    it. The same filters apply as everywhere else, so the table describes the selection
    on screen and not the whole base.
    """
    try:
        return pilotage.regles_calcul(filtres, user.role)
    except analyse.DimensionInconnue as err:
        raise _refuser(err) from err


@router.get("/synthese")
def synthese(user: _Lecteur, filtres: _Filtres) -> dict[str, Any]:
    """Headline figures for the current filter set."""
    try:
        return pilotage.synthese(filtres, user.role)
    except analyse.DimensionInconnue as err:
        raise _refuser(err) from err


@router.get("/comparaison")
def comparaison(user: _Lecteur, filtres: _Filtres) -> dict[str, Any]:
    """This period against the preceding one of the same length."""
    try:
        return direction_referentiels.comparaison(filtres, user.role)
    except analyse.DimensionInconnue as err:
        raise _refuser(err) from err


@router.get("/repartition")
def repartition(
    user: _Lecteur, filtres: _Filtres,
    dimension: str = Query(description="Axe de regroupement"),
) -> list[dict[str, Any]]:
    """Attendance broken down along one axis."""
    try:
        return analyse.repartition(dimension, filtres, user.role)
    except analyse.DimensionInconnue as err:
        raise _refuser(err) from err


@router.get("/croisement")
def croisement(
    user: _Lecteur, filtres: _Filtres,
    lignes: str = Query(description="Axe des lignes"),
    colonnes: str = Query(description="Axe des colonnes"),
    mesure: str = Query(default="presents"),
) -> dict[str, Any]:
    """A cross-tab matrix with its margins."""
    try:
        return analyse.tableau_croise(lignes, colonnes, filtres, user.role, mesure)
    except analyse.DimensionInconnue as err:
        raise _refuser(err) from err


@router.get("/arborescence")
def arborescence(user: _Lecteur, filtres: _Filtres) -> list[dict[str, Any]]:
    """The organisation as a tree, with attendance at every node."""
    try:
        return pilotage.arborescence(filtres, user.role)
    except analyse.DimensionInconnue as err:
        raise _refuser(err) from err


@router.get("/serie")
def serie(user: _Lecteur, filtres: _Filtres) -> list[dict[str, Any]]:
    """Attendance activity by activity, chronologically."""
    try:
        return pilotage.serie_temporelle(filtres, user.role)
    except analyse.DimensionInconnue as err:
        raise _refuser(err) from err


@router.get("/assiduite")
def assiduite(user: _Lecteur, filtres: _Filtres) -> dict[str, Any]:
    """Members grouped by how often they attend."""
    try:
        return pilotage.cohortes_assiduite(filtres, user.role)
    except analyse.DimensionInconnue as err:
        raise _refuser(err) from err


@router.get("/suivi-membres")
def suivi_membres(
    user: _LecteurNominatif, filtres: _Filtres,
    tri: str = Query(default="taux", description="taux, taux_desc, nom, recent, ancien"),
    limite: int = Query(default=100, ge=1, le=500),
    decalage: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Named members with their attendance, restricted to the steering fields.

    Deliberately not a copy of the back-office directory: the response carries the
    whitelist it honoured in ``champs_exposes``, so what the direction can see about a
    person is verifiable from the payload itself.
    """
    try:
        return direction_membres.suivi_assiduite(filtres, user.role, tri, limite, decalage)
    except analyse.DimensionInconnue as err:
        raise _refuser(err) from err
