# ruff: noqa: E501 - SQL guards and audit lines carry long literals
"""Administrable catalogue of event types, each with a UNIQUE colour.

The community manages its recurring events here instead of the three hardcoded
types. Each type carries a stable ASCII ``code``, a display ``nom`` (with accents),
a UNIQUE ``couleur`` used to distinguish events on the member calendar, an optional
description and a ``publie`` flag (controls availability in the planning dropdowns).
Creating a type without a colour auto-assigns the next unused colour; two types can
never share a colour (enforced both here and by a UNIQUE constraint). Reads require
``evenements.consulter``; writes require ``evenements.gerer``. Every change is audited.
"""
from __future__ import annotations

import colorsys
import re
import unicodedata
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import audit, db
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin/types-evenements", tags=["types-evenements"])

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

# A broad palette of visually distinct colours proposed to new types before falling
# back to a generated hue. None of these are used to force a value: the first one not
# already taken is suggested, and the operator can always override it.
_PALETTE = [
    "#2563EB", "#7C3AED", "#DB2777", "#059669", "#D97706", "#0891B2", "#DC2626", "#E11D48",
    "#0D9488", "#CA8A04", "#EA580C", "#4F46E5", "#16A34A", "#9333EA", "#B45309", "#0284C7",
    "#65A30D", "#C026D3", "#1D4ED8", "#BE123C", "#15803D", "#A21CAF", "#B91C1C", "#0369A1",
    "#4D7C0F", "#7E22CE", "#C2410C", "#0F766E", "#9D174D", "#3730A3", "#166534", "#92400E",
]


def _slug(nom: str) -> str:
    """Stable ASCII code from a display name: accents stripped, non-alphanumerics to '_'."""
    ascii_nom = unicodedata.normalize("NFKD", nom).encode("ascii", "ignore").decode("ascii")
    code = re.sub(r"[^A-Za-z0-9]+", "_", ascii_nom).strip("_").upper()
    return code or "TYPE"


def _used_colours(role: str) -> set[str]:
    return {
        str(r["couleur"]).upper()
        for r in db.fetch_all("SELECT couleur FROM type_evenement", (), role=role)
    }


def _generated_colour(used: set[str]) -> str:
    """Golden-angle hue walk so generated colours stay far apart, skipping any already used."""
    for i in range(len(used) + 1, len(used) + 361):
        hue = (i * 137.508) % 360 / 360.0
        r, g, b = colorsys.hls_to_rgb(hue, 0.45, 0.6)
        hex_colour = f"#{round(r * 255):02X}{round(g * 255):02X}{round(b * 255):02X}"
        if hex_colour not in used:
            return hex_colour
    return "#334155"


def _next_free_colour(role: str) -> str:
    used = _used_colours(role)
    for candidate in _PALETTE:
        if candidate.upper() not in used:
            return candidate
    return _generated_colour(used)


def _row_out(r: dict[str, object]) -> dict[str, object]:
    return {
        "id": str(r["id"]),
        "code": r["code"],
        "nom": r["nom"],
        "couleur": r["couleur"],
        "description": r.get("description"),
        "publie": bool(r.get("publie")),
        "ordre": int(r.get("ordre") or 0),
    }


class TypeEvenementIn(BaseModel):
    nom: str = Field(min_length=1, max_length=120)
    description: str | None = None
    # Optional: when omitted, the next unused palette colour is assigned automatically.
    couleur: str | None = None


class TypeEvenementPatch(BaseModel):
    nom: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    couleur: str | None = None
    publie: bool | None = None


def _norm_colour(couleur: str) -> str:
    couleur = couleur.strip().upper()
    if not _HEX_RE.match(couleur):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="couleur invalide (format attendu #RRGGBB)")
    return couleur


@router.get("")
def lister(user: Annotated[UserMe, Depends(require_permission("evenements.consulter"))]) -> list[dict[str, object]]:
    """List every event type (published or not), in display order."""
    return [
        _row_out(r) for r in db.fetch_all(
            "SELECT id, code, nom, couleur, description, publie, ordre FROM type_evenement ORDER BY ordre, nom ASC",
            (), role=user.role,
        )
    ]


@router.get("/couleur-suggeree")
def couleur_suggeree(user: Annotated[UserMe, Depends(require_permission("evenements.consulter"))]) -> dict[str, str]:
    """Suggest the next unused colour so a new type never collides with an existing one."""
    return {"couleur": _next_free_colour(user.role)}


@router.post("", status_code=status.HTTP_201_CREATED)
def creer(
    payload: TypeEvenementIn,
    user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))],
) -> dict[str, object]:
    """Create an event type. The colour is unique: if omitted it is auto-assigned, if
    provided it is validated and refused when already taken (clear 409, never a 500)."""
    nom = payload.nom.strip()
    if db.fetch_one("SELECT 1 FROM type_evenement WHERE lower(nom) = lower(%s)", (nom,), role=user.role):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="un type d'evenement porte deja ce nom")
    couleur = _norm_colour(payload.couleur) if payload.couleur else _next_free_colour(user.role)
    if db.fetch_one("SELECT 1 FROM type_evenement WHERE upper(couleur) = %s", (couleur,), role=user.role):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="cette couleur est deja utilisee par un autre type")
    code = _slug(nom)
    base, n = code, 2
    while db.fetch_one("SELECT 1 FROM type_evenement WHERE code = %s", (code,), role=user.role):
        code = f"{base}_{n}"
        n += 1
    ordre_row = db.fetch_one("SELECT COALESCE(max(ordre), 0) + 1 AS n FROM type_evenement", (), role=user.role)
    ordre = int((ordre_row or {}).get("n") or 0)
    try:
        created = db.execute(
            "INSERT INTO type_evenement (code, nom, couleur, description, publie, ordre) "
            "VALUES (%s, %s, %s, %s, true, %s) RETURNING id, code, nom, couleur, description, publie, ordre",
            (code, nom, couleur, (payload.description or "").strip() or None, ordre),
            role=user.role,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="type deja existant (nom ou couleur)") from exc
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="creation impossible")
    audit.log(user.id, user.role, "creation_type_evenement", "type_evenement", str(created["id"]),
              {"nom": nom, "couleur": couleur})
    return _row_out(created)


@router.patch("/{type_id}")
def modifier(
    type_id: str,
    payload: TypeEvenementPatch,
    user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))],
) -> dict[str, object]:
    """Edit a type: name, description, colour (kept unique) and publication."""
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no field to update")
    if not db.fetch_one("SELECT 1 FROM type_evenement WHERE id = %s", (type_id,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    sets: list[str] = []
    params: list[object] = []
    if "nom" in fields:
        nom = (payload.nom or "").strip()
        if not nom:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name required")
        if db.fetch_one("SELECT 1 FROM type_evenement WHERE lower(nom) = lower(%s) AND id <> %s", (nom, type_id), role=user.role):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="un type d'evenement porte deja ce nom")
        sets.append("nom = %s")
        params.append(nom)
    if "couleur" in fields and payload.couleur is not None:
        couleur = _norm_colour(payload.couleur)
        if db.fetch_one("SELECT 1 FROM type_evenement WHERE upper(couleur) = %s AND id <> %s", (couleur, type_id), role=user.role):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="cette couleur est deja utilisee par un autre type")
        sets.append("couleur = %s")
        params.append(couleur)
    if "description" in fields:
        sets.append("description = %s")
        params.append((payload.description or "").strip() or None)
    if "publie" in fields and payload.publie is not None:
        sets.append("publie = %s")
        params.append(payload.publie)
    if not sets:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="no field to update")
    params.append(type_id)
    try:
        row = db.execute(
            f"UPDATE type_evenement SET {', '.join(sets)} WHERE id = %s "
            "RETURNING id, code, nom, couleur, description, publie, ordre",
            tuple(params), role=user.role,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="nom ou couleur deja utilise") from exc
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    audit.log(user.id, user.role, "modification_type_evenement", "type_evenement", type_id, {"champs": sorted(fields)})
    return _row_out(row)


@router.delete("/{type_id}", status_code=status.HTTP_204_NO_CONTENT)
def supprimer(
    type_id: str,
    user: Annotated[UserMe, Depends(require_permission("evenements.gerer"))],
) -> None:
    """Delete a type. Events keep their history: the FK is ON DELETE SET NULL, so a
    deleted type simply detaches from its events (they fall back to the default colour)."""
    if not db.fetch_one("SELECT 1 FROM type_evenement WHERE id = %s", (type_id,), role=user.role):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    detaches = int((db.fetch_one(
        "SELECT count(*) AS n FROM evenement WHERE type_evenement_id = %s", (type_id,), role=user.role,
    ) or {}).get("n") or 0)
    db.execute("DELETE FROM type_evenement WHERE id = %s", (type_id,), role=user.role)
    audit.log(user.id, user.role, "suppression_type_evenement", "type_evenement", type_id, {"evenements_detaches": detaches})
