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


_RISK_ORDER = {"faible": 0, "moyen": 1, "eleve": 2, "critique": 3}
_ROLE_LIBELLES = {
    "membre": "Membre",
    "controleur": "Contrôle",
    "gestionnaire": "Gestion des membres",
    "direction": "Direction",
    "admin": "Administration",
    "super_admin": "Super-administration",
}


def _capabilities_de(permissions_cles: list[str]) -> list[dict[str, str]]:
    """Reviewable capability entries derived from the ENFORCED catalogue
    (permissions_data.CATALOGUE + PERMISSION_DETAILS), never a parallel copy."""
    from . import permissions_data

    details = permissions_data.PERMISSION_DETAILS
    out = []
    for cle in sorted(permissions_cles):
        meta = permissions_data.CATALOGUE.get(cle)
        if not meta:
            continue
        out.append({
            "cle": cle,
            "libelle": meta["libelle"],
            "description": details.get(cle, {}).get("description", ""),
            "risque": meta["risque"],
            "portee": meta["portee"],
        })
    return out


def _risque_max(capabilities: list[dict[str, str]]) -> str:
    if not capabilities:
        return "faible"
    return max((c["risque"] for c in capabilities), key=lambda r: _RISK_ORDER.get(r, 0))


def _entree_acces(r: dict[str, object], role_appel: str) -> tuple[dict[str, object], list[str]]:
    """One reviewable access entry (plus its warnings) for a single membership row,
    with capabilities derived from the enforced catalogue."""
    from . import permissions_data

    role_accorde = str(r["role_accorde"])
    portee_type = str(r["portee_type"])
    mode = str(r.get("mode") or "role")
    if mode == "permissions":
        perms = [
            str(p["permission"]) for p in db.fetch_all(
                "SELECT permission FROM groupe_permission WHERE groupe_id = %s",
                (str(r["groupe_id"]),), role=role_appel,
            )
        ]
        caps = _capabilities_de(perms)
        libelle = f"Groupe de permissions : {r['groupe_libelle']}"
    else:
        caps = _capabilities_de(sorted(permissions_data.permissions_du_role(role_accorde)))
        libelle = _ROLE_LIBELLES.get(role_accorde, role_accorde)
    entree = {
        "role": role_accorde,
        "role_libelle": libelle,
        "risque": _risque_max(caps),
        "portee_type": portee_type,
        "portee_libelle": r.get("portee_libelle"),
        "portee_texte": "toute la base" if portee_type == "global" else (r.get("portee_libelle") or portee_type),
        "capabilities": caps,
    }
    alertes: list[str] = []
    if portee_type == "global" and role_accorde in ("admin", "super_admin"):
        alertes.append(f"Accès {libelle} GLOBAL : pouvoir très large sur toute la base. À réserver au strict nécessaire.")
    if role_accorde == "super_admin":
        alertes.append("Rôle super-administration : tous les pouvoirs système. Séparation des tâches recommandée.")
    return entree, alertes


@router.get("/membres/{membre_id}/acces-effectif")
def acces_effectif(membre_id: str, user: Annotated[UserMe, Depends(require_permission("acces.administrer"))]) -> dict[str, object]:
    """A reviewable explanation of a member's effective access, with warnings.

    Every capability shown here is derived from the SAME mapping the server
    enforces (permissions_data.ROLE_PERMISSIONS / groupe_permission), so the
    review can never promise an action the server would refuse, nor hide one it
    would allow. Permission-mode groups list their own granted permissions.
    """
    from . import permissions_data

    eff = _effective_role(membre_id, user.role)
    rows = db.fetch_all(
        "SELECT g.id AS groupe_id, g.role_accorde, g.mode, g.libelle AS groupe_libelle, "
        "mg.portee_type, mg.portee_id, "
        "COALESCE(pc.nom, pin.nom, pk.nom, pt.nom) AS portee_libelle "
        "FROM membre_groupe mg JOIN groupe_acces g ON g.id = mg.groupe_id "
        "LEFT JOIN coordination pc ON mg.portee_type = 'coordination' AND pc.id = mg.portee_id "
        "LEFT JOIN intendance pin ON mg.portee_type = 'intendance' AND pin.id = mg.portee_id "
        "LEFT JOIN commission pk ON mg.portee_type = 'commission' AND pk.id = mg.portee_id "
        "LEFT JOIN tribu pt ON mg.portee_type = 'tribu' AND pt.id = mg.portee_id "
        "WHERE mg.membre_id = %s AND mg.actif = true AND g.actif = true ORDER BY g.role_accorde DESC",
        (membre_id,),
        role=user.role,
    )
    acces = []
    warnings: list[str] = []
    a_portee_globale = False
    for r in rows:
        entree, alertes = _entree_acces(r, user.role)
        acces.append(entree)
        warnings.extend(alertes)
        if entree["portee_type"] == "global":
            a_portee_globale = True
    # "Only scoped" is only true when NO membership is global: a global
    # permission-mode group keeps role 'membre' yet grants global permissions.
    if eff == "membre" and acces and not a_portee_globale:
        warnings.append("Accès uniquement scopés : aucune visibilité globale (comportement attendu, hermétique).")
    caps_eff = _capabilities_de(sorted(permissions_data.permissions_du_role(eff)))
    return {
        "membre_id": membre_id,
        "role_global_effectif": eff,
        "risque_global": _risque_max(caps_eff),
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
