"""Read-only access views: permission catalogue, matrix and reviewable effective access.

Split out of app.groupes to keep each file focused and under the size threshold.
These endpoints never mutate; they explain the model to administrators. The single
enforcement path stays require_permission on every route.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from . import db
from .groupes import _PORTEE_TABLES, _effective_role
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin", tags=["groupes"])

_ORDRE_ROLES = ("membre", "controleur", "gestionnaire", "direction", "admin", "super_admin")


@router.get("/catalogue-acces")
def catalogue_acces(user: Annotated[UserMe, Depends(require_permission("acces.administrer"))]) -> dict[str, object]:
    """The role -> capabilities catalogue with labels, descriptions and risk levels.

    Powers the pedagogical admin UI: it explains, for every platform role, exactly
    what it lets a person do, on which scope, and how sensitive it is, so access is
    granted knowingly and never by broad guesswork.
    """
    from . import permissions

    return {"roles": permissions.catalogue()}


def _matrice_permissions() -> dict[str, object]:
    """The atomic permission catalogue and the role -> permissions matrix.

    Pure data (from :mod:`app.permissions_data`), so the read-only matrix shown in
    the back office is derived from the very mapping the server enforces, never a
    hand-kept copy that could drift. Permissions are grouped by domain and each
    role lists exactly the keys it holds.
    """
    from . import permissions_data

    details = permissions_data.PERMISSION_DETAILS
    permissions = [
        {"cle": cle, "domaine": meta["domaine"], "libelle": meta["libelle"],
         "risque": meta["risque"], "portee": meta["portee"],
         "description": details.get(cle, {}).get("description", ""),
         "limite": details.get(cle, {}).get("limite", "")}
        for cle, meta in sorted(permissions_data.CATALOGUE.items())
    ]
    domaines = sorted({meta["domaine"] for meta in permissions_data.CATALOGUE.values()})
    roles = [
        {"role": role, "permissions": sorted(permissions_data.permissions_du_role(role))}
        for role in _ORDRE_ROLES
    ]
    return {"permissions": permissions, "domaines": domaines, "roles": roles}


def _groupes_specialises(role: str) -> list[dict[str, object]]:
    """The permission-mode access groups with the atomic permissions each grants.

    Reads ``groupe_acces`` (mode = 'permissions') joined with ``groupe_permission``.
    Both belong to migrations 0075/0076, the same hard dependency as
    ``require_permission`` itself, so the code and the schema always deploy
    together and no failure is masked here.
    """
    rows = db.fetch_all(
        "SELECT g.id, g.cle, g.libelle, g.description, g.actif, "
        "COALESCE(array_agg(gp.permission ORDER BY gp.permission) "
        "FILTER (WHERE gp.permission IS NOT NULL), '{}') AS permissions "
        "FROM groupe_acces g "
        "LEFT JOIN groupe_permission gp ON gp.groupe_id = g.id "
        "WHERE g.mode = 'permissions' "
        "GROUP BY g.id, g.cle, g.libelle, g.description, g.actif "
        "ORDER BY g.libelle ASC",
        (),
        role=role,
    )
    return [
        {"id": str(r["id"]), "cle": r["cle"], "libelle": r["libelle"],
         "description": r.get("description"), "actif": bool(r["actif"]),
         "permissions": list(r.get("permissions") or [])}
        for r in rows
    ]


@router.get("/catalogue-permissions")
def catalogue_permissions(
    user: Annotated[UserMe, Depends(require_permission("acces.administrer"))]
) -> dict[str, object]:
    """The granular permission catalogue, the role matrix and the specialized groups.

    Powers the read-only access matrix in the back office: which atomic permission
    each role holds, and which permissions each specialized (permission-mode) group
    grants. The UI is a mirror; the server dependency ``require_permission`` remains
    the only enforcement.
    """
    matrice = _matrice_permissions()
    matrice["groupes_specialises"] = _groupes_specialises(user.role)
    return matrice


@router.get("/membres/{membre_id}/acces-effectif")
def acces_effectif(membre_id: str, user: Annotated[UserMe, Depends(require_permission("acces.administrer"))]) -> dict[str, object]:
    """A reviewable explanation of a member's effective access, with warnings.

    Lists the global role, every scoped membership with its perimeter, the atomic
    capabilities each grants, and safety warnings (broad global power, sensitive
    capabilities), so an admin can review who can see and do what, and where.
    """
    from . import permissions

    eff = _effective_role(membre_id, user.role)
    rows = db.fetch_all(
        "SELECT g.role_accorde, mg.portee_type, mg.portee_id, "
        "COALESCE(pc.nom, pin.nom, pk.nom, pt.nom) AS portee_libelle "
        "FROM membre_groupe mg JOIN groupe_acces g ON g.id = mg.groupe_id "
        "LEFT JOIN coordination pc ON mg.portee_type = 'coordination' AND pc.id = mg.portee_id "
        "LEFT JOIN intendance pin ON mg.portee_type = 'intendance' AND pin.id = mg.portee_id "
        "LEFT JOIN commission pk ON mg.portee_type = 'commission' AND pk.id = mg.portee_id "
        "LEFT JOIN tribu pt ON mg.portee_type = 'tribu' AND pt.id = mg.portee_id "
        "WHERE mg.membre_id = %s AND g.actif = true ORDER BY g.role_accorde DESC",
        (membre_id,),
        role=user.role,
    )
    acces = []
    warnings: list[str] = []
    for r in rows:
        role_accorde = str(r["role_accorde"])
        portee_type = str(r["portee_type"])
        explication = permissions.expliquer_role(role_accorde)
        acces.append({
            "role": role_accorde,
            "role_libelle": explication["libelle"],
            "risque": explication["risque"],
            "portee_type": portee_type,
            "portee_libelle": r.get("portee_libelle"),
            "portee_texte": "toute la base" if portee_type == "global" else (r.get("portee_libelle") or portee_type),
            "capabilities": explication["capabilities"],
        })
        if portee_type == "global" and role_accorde in ("admin", "super_admin"):
            warnings.append(f"Accès {explication['libelle']} GLOBAL : pouvoir très large sur toute la base. À réserver au strict nécessaire.")
        if role_accorde == "super_admin":
            warnings.append("Rôle super-administration : tous les pouvoirs système. Séparation des tâches recommandée.")
    if eff == "membre" and acces:
        warnings.append("Accès uniquement scopés : aucune visibilité globale (comportement attendu, hermétique).")
    return {
        "membre_id": membre_id,
        "role_global_effectif": eff,
        "risque_global": permissions.role_risque(eff),
        "acces": acces,
        "avertissements": warnings,
    }


@router.get("/perimetres-disponibles")
def perimetres_disponibles(user: Annotated[UserMe, Depends(require_permission("acces.administrer"))]) -> dict[str, object]:
    """The organisational units that a scoped group can be attached to."""
    out: dict[str, object] = {}
    for cle, table in _PORTEE_TABLES.items():
        rows = db.fetch_all(f"SELECT id, nom FROM {table} ORDER BY nom ASC", (), role=user.role)
        out[cle] = [{"id": str(r["id"]), "nom": r["nom"]} for r in rows]
    return out
