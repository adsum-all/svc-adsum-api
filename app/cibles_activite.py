"""Administrable referential of activity targeting destinations (cible_activite).

The referential (migration 0147) drives which destinations the activity forms
offer and how an audience is resolved. Rules are SAFE templates only, evaluated
server-side; the admin UI can never inject a free rule:

- 'tous'      : every active member.
- 'unite'     : members attached to one organisational unit (cible_id required).
- 'fonction'  : active members holding at least one of the given catalogue
                functions (active and confirmed), deduplicated. This is the only
                template a new destination can be created with, so extending the
                targeting never requires code again (e.g. "Patriarches").
- 'attribut'  : a whitelisted member attribute (est_berger only).
- 'liste'     : explicit e-mail addresses carried by the event itself.

Stable ``code`` is the identity used by events, statistics and history; only the
``libelle`` is renamable. An inactive/archived destination disappears from new
forms but historical events keep resolving their label here.
"""
# ruff: noqa: E501
from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import audit, db
from .permissions_rbac import require_permission, require_permission_ecriture
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["cibles-activite"])

_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
# Query-string unit id: validated by shape so a malformed value is a clean 400,
# never a Postgres uuid cast error surfacing as a 500.
_UUID_QUERY_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Whitelists: the only unit tables and member attributes a rule may reference.
_UNITE_COLONNES = {
    "coordination": "coordination_id",
    "commission": "commission_id",
    "intendance": "intendance_id",
    "tribu": "tribu_id",
}
_ATTRIBUTS = {"est_berger"}


class CibleOut(BaseModel):
    code: str
    libelle: str
    description: str | None = None
    categorie: str
    type_regle: str
    # True when the form must ask for an organisational unit (cible_id).
    besoin_unite: bool = False
    ordre: int = 0
    statut: str = "actif"
    parametres: dict[str, Any] = {}


class CibleCreateIn(BaseModel):
    """Creation is restricted to the 'fonction' template: a destination built from
    catalogue functions. Everything else is structural and lives in the seed."""

    code: str
    libelle: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    fonction_cles: list[str] = Field(min_length=1, max_length=20)


class CibleUpdateIn(BaseModel):
    libelle: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    ordre: int | None = Field(default=None, ge=0, le=10000)
    statut: str | None = Field(default=None, pattern="^(actif|inactif|archive)$")
    # Only meaningful for 'fonction'-template destinations.
    fonction_cles: list[str] | None = Field(default=None, min_length=1, max_length=20)


def _row_out(r: dict[str, object]) -> CibleOut:
    params = r.get("parametres") or {}
    return CibleOut(
        code=str(r["code"]), libelle=str(r["libelle"]), description=r.get("description"),
        categorie=str(r["categorie"]), type_regle=str(r["type_regle"]),
        besoin_unite=str(r["type_regle"]) == "unite",
        ordre=int(r.get("ordre") or 0), statut=str(r["statut"]),
        parametres=params if isinstance(params, dict) else {},
    )


def _fetch(code: str, role: str | None) -> dict[str, object]:
    row = db.fetch_one(
        "SELECT code, libelle, description, categorie, type_regle, parametres, ordre, statut FROM cible_activite WHERE code = %s",
        (code,), role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="destination inconnue")
    return row


def _valider_fonction_cles(cles: list[str], role: str | None) -> list[str]:
    """Every referenced function must exist in the catalogue (typos never become
    silent empty audiences)."""
    propres = sorted({c.strip().lower() for c in cles if c and c.strip()})
    if not propres:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="au moins une fonction est requise")
    connues = {
        str(r["cle"]) for r in db.fetch_all(
            "SELECT cle FROM fonction_honorifique WHERE cle = ANY(%s)", (propres,), role=role,
        )
    }
    inconnues = [c for c in propres if c not in connues]
    if inconnues:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"fonction(s) inconnue(s) du catalogue: {', '.join(inconnues)}")
    return propres


# ---- Audience resolution (server-side only, deduplicated, active members) ----

def _predicat_unite(params: dict[str, Any], cible_id: str | None) -> tuple[str, tuple]:
    table = str(params.get("table") or "")
    colonne = _UNITE_COLONNES.get(table)
    if not colonne or not cible_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unité de ciblage invalide")
    if table == "coordination":
        # Mirror of the live engine (visibilite.CIBLE_PREDICATE): a member belongs
        # to a coordination THROUGH their intendance, never a direct column.
        return (
            "m.statut = 'actif' AND EXISTS (SELECT 1 FROM intendance i "
            "WHERE i.id = m.intendance_id AND i.coordination_id = %s)",
            (cible_id,),
        )
    return f"m.statut = 'actif' AND m.{colonne} = %s", (cible_id,)


def _predicat_attribut(params: dict[str, Any]) -> tuple[str, tuple]:
    attribut = str(params.get("attribut") or "")
    if attribut not in _ATTRIBUTS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="attribut de ciblage non autorisé")
    return f"m.statut = 'actif' AND m.{attribut} = true", ()


def _predicat_fonction(params: dict[str, Any]) -> tuple[str, tuple]:
    if params.get("toutes_fonctions"):
        # Mirror of the live 'responsables' branch: any member whose principal
        # active confirmed function is mirrored on membre.fonction_cle.
        return "m.statut = 'actif' AND m.fonction_cle IS NOT NULL", ()
    cles = [str(c) for c in (params.get("fonction_cles") or [])]
    if not cles:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="destination par fonction sans fonction configurée")
    return (
        "m.statut = 'actif' AND EXISTS (SELECT 1 FROM membre_fonction mf "
        "WHERE mf.membre_id = m.id AND mf.actif = true AND mf.confirmee = true AND lower(mf.fonction_cle) = ANY(%s))",
        (cles,),
    )


def predicat_membres(cible: dict[str, object], cible_id: str | None) -> tuple[str, tuple]:
    """SQL predicate over ``membre m`` selecting the audience of a destination.

    Safe by construction: the rule kind is constrained by the referential CHECK and
    every referenced column comes from an internal whitelist, never from the admin
    input. 'liste' resolves to no member (its audience are the event's e-mails)."""
    regle = str(cible["type_regle"])
    params_raw = cible.get("parametres") or {}
    params: dict[str, Any] = params_raw if isinstance(params_raw, dict) else {}
    if regle == "tous":
        return "m.statut = 'actif'", ()
    if regle == "unite":
        return _predicat_unite(params, cible_id)
    if regle == "attribut":
        return _predicat_attribut(params)
    if regle == "fonction":
        return _predicat_fonction(params)
    # 'liste': the audience is the ad-hoc e-mail list on the event, not members.
    return "false", ()


def cibles_actives(role: str | None) -> list[dict[str, object]]:
    return db.fetch_all(
        "SELECT code, libelle, description, categorie, type_regle, parametres, ordre, statut "
        "FROM cible_activite WHERE statut = 'actif' ORDER BY ordre, libelle",
        (), role=role,
    )


def cible_valide_pour_creation(code: str, role: str | None) -> dict[str, object] | None:
    """The destination row when it exists AND is active (usable on a NEW event)."""
    return db.fetch_one(
        "SELECT code, libelle, description, categorie, type_regle, parametres, ordre, statut "
        "FROM cible_activite WHERE code = %s AND statut = 'actif'",
        (code,), role=role,
    )


# ---- Reference endpoint (forms) ----

@router.get("/reference/cibles-activite", response_model=list[CibleOut])
def reference_cibles(user: Annotated[UserMe, Depends(require_permission("membres.self"))]) -> list[CibleOut]:
    """Active destinations, ordered, for the activity forms (back office and
    collaboration). Inactive/archived destinations never appear here."""
    return [_row_out(r) for r in cibles_actives(user.role)]


# ---- Admin endpoints (referential management) ----

@router.get("/admin/cibles-activite", response_model=list[CibleOut])
def admin_liste(user: Annotated[UserMe, Depends(require_permission("evenements.consulter"))]) -> list[CibleOut]:
    rows = db.fetch_all(
        "SELECT code, libelle, description, categorie, type_regle, parametres, ordre, statut "
        "FROM cible_activite ORDER BY ordre, libelle",
        (), role=user.role,
    )
    return [_row_out(r) for r in rows]


@router.post("/admin/cibles-activite", response_model=CibleOut, status_code=status.HTTP_201_CREATED)
def admin_creer(payload: CibleCreateIn, user: Annotated[UserMe, Depends(require_permission_ecriture("parametres.gerer"))]) -> CibleOut:
    """Create a NEW destination from the safe 'fonction' template (e.g. Patriarches
    was seeded this way). The code is stable forever and never reused: creation is
    refused if the code ever existed, even archived.

    Writes require the permission AND an admin base role, so the endpoint authority
    matches the RLS write policy of cible_activite (super_admin/admin only)."""
    code = payload.code.strip().lower()
    if not _CODE_RE.match(code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="code invalide (minuscules, chiffres, underscore, commence par une lettre)")
    if db.fetch_one("SELECT 1 FROM cible_activite WHERE code = %s", (code,), role=user.role):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="ce code de destination existe déjà (les codes ne sont jamais réutilisés)")
    cles = _valider_fonction_cles(payload.fonction_cles, user.role)
    ordre_row = db.fetch_one("SELECT COALESCE(max(ordre), 0) + 10 AS n FROM cible_activite", (), role=user.role)
    import json as _json

    db.execute(
        "INSERT INTO cible_activite (code, libelle, description, categorie, type_regle, parametres, ordre, cree_par, maj_par) "
        "VALUES (%s, %s, %s, 'responsabilite', 'fonction', %s::jsonb, %s, %s, %s)",
        (code, payload.libelle.strip(), payload.description, _json.dumps({"fonction_cles": cles}), int((ordre_row or {}).get("n") or 0), user.id, user.id),
        role=user.role,
    )
    audit.log(user.id, user.role, "creation_cible_activite", "cible_activite", code, {"libelle": payload.libelle, "fonction_cles": cles})
    return _row_out(_fetch(code, user.role))


@router.patch("/admin/cibles-activite/{code}", response_model=CibleOut)
def admin_modifier(code: str, payload: CibleUpdateIn, user: Annotated[UserMe, Depends(require_permission_ecriture("parametres.gerer"))]) -> CibleOut:
    """Rename, reorder, describe or change the lifecycle status of a destination.
    The stable code itself is immutable. For a 'fonction' destination the granted
    function set can be adjusted (validated against the catalogue)."""
    cible = _fetch(code, user.role)
    fields: dict[str, object] = {}
    if payload.libelle is not None:
        fields["libelle"] = payload.libelle.strip()
    if payload.description is not None:
        fields["description"] = payload.description
    if payload.ordre is not None:
        fields["ordre"] = payload.ordre
    if payload.statut is not None:
        fields["statut"] = payload.statut
    if payload.fonction_cles is not None:
        if str(cible["type_regle"]) != "fonction":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="seule une destination par fonction porte des fonctions")
        import json as _json

        fields["parametres"] = _json.dumps({"fonction_cles": _valider_fonction_cles(payload.fonction_cles, user.role)})
    if not fields:
        return _row_out(cible)
    sets = ", ".join(
        f"{k} = %s::jsonb" if k == "parametres" else f"{k} = %s" for k in fields
    )
    db.execute(
        f"UPDATE cible_activite SET {sets}, maj_le = now(), maj_par = %s WHERE code = %s",
        (*fields.values(), user.id, code),
        role=user.role,
    )
    audit.log(user.id, user.role, "modification_cible_activite", "cible_activite", code, {"champs": list(fields)})
    return _row_out(_fetch(code, user.role))


@router.delete("/admin/cibles-activite/{code}", status_code=status.HTTP_200_OK)
def admin_supprimer(code: str, user: Annotated[UserMe, Depends(require_permission_ecriture("parametres.gerer"))]) -> dict[str, object]:
    """Physical deletion is allowed ONLY for a destination never used by any event;
    otherwise archive it (409), so history and statistics can never break."""
    _fetch(code, user.role)
    usage = db.fetch_one("SELECT count(*) AS n FROM evenement WHERE cible_type = %s", (code,), role=user.role)
    if int((usage or {}).get("n") or 0) > 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="destination utilisée par des activités : archivez-la au lieu de la supprimer")
    db.execute("DELETE FROM cible_activite WHERE code = %s", (code,), role=user.role)
    audit.log(user.id, user.role, "suppression_cible_activite", "cible_activite", code, {})
    return {"supprime": True, "code": code}


@router.get("/admin/cibles-activite/{code}/apercu")
def admin_apercu(
    code: str,
    user: Annotated[UserMe, Depends(require_permission("evenements.consulter"))],
    cible_id: str | None = None,
) -> dict[str, object]:
    """Server-side audience preview: how many DISTINCT active members the
    destination reaches (0 for the ad-hoc e-mail list, whose audience lives on the
    event). Lets the operator detect an empty or unexpectedly large audience
    before publishing."""
    if cible_id is not None and not _UUID_QUERY_RE.match(cible_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cible_id invalide (UUID attendu)")
    cible = _fetch(code, user.role)
    predicat, params = predicat_membres(cible, cible_id)
    row = db.fetch_one(f"SELECT count(DISTINCT m.id) AS n FROM membre m WHERE {predicat}", params, role=user.role)
    return {"code": code, "libelle": cible["libelle"], "nombre": int((row or {}).get("n") or 0), "type_regle": cible["type_regle"]}
