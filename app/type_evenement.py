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


def _hex_to_lab(hex_colour: str) -> tuple[float, float, float]:
    """Perceptual CIELAB coordinates of an #RRGGBB colour (sRGB -> linear -> XYZ -> Lab).

    Working in Lab lets us measure how visually different two colours are, instead of
    comparing raw hex strings, so a suggested colour is really far from every used one."""
    h = hex_colour.lstrip("#")
    rgb = [int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    r, g, b = lin
    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _delta_e(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """CIE76 perceptual distance between two Lab colours (a larger value = more distinct)."""
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def _candidate_pool() -> list[str]:
    """The fixed palette plus a golden-angle spread of generated hues at a readable
    lightness and chroma, so there is always a large set to pick the most distinct from."""
    pool = list(_PALETTE)
    for i in range(72):
        hue = (i * 137.508) % 360 / 360.0
        r, g, b = colorsys.hls_to_rgb(hue, 0.45, 0.6)
        pool.append(f"#{round(r * 255):02X}{round(g * 255):02X}{round(b * 255):02X}")
    return pool


def _next_free_colour(role: str, avoid: set[str] | None = None) -> str:
    """Suggest the colour that is the MOST perceptually distinct from every colour already
    used by a type, staying in a readable lightness band (never too dark or too pale). The
    optional ``avoid`` set (e.g. the colour currently shown) is excluded so asking again
    yields a different distinct colour. Falls back to the first palette colour if the pool
    is exhausted."""
    used = _used_colours(role)
    avoid_upper = {c.upper() for c in (avoid or set())}
    used_labs = [_hex_to_lab(c) for c in used if _HEX_RE.match(c)]
    best: str | None = None
    best_score = -1.0
    for cand in _candidate_pool():
        cu = cand.upper()
        if cu in used or cu in avoid_upper:
            continue
        lab = _hex_to_lab(cu)
        # Readability band: skip colours too dark or too light for a legible calendar chip.
        if lab[0] < 32 or lab[0] > 72:
            continue
        score = min((_delta_e(lab, u) for u in used_labs), default=1000.0)
        if score > best_score:
            best_score = score
            best = cu
    if best:
        return best
    # Pool exhausted (every readable candidate already taken or avoided): return the first
    # palette colour not in the avoid set, else the first palette colour.
    for cand in _PALETTE:
        if cand.upper() not in avoid_upper:
            return cand
    return _PALETTE[0]


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
def couleur_suggeree(
    user: Annotated[UserMe, Depends(require_permission("evenements.consulter"))],
    eviter: str | None = None,
) -> dict[str, str]:
    """Suggest the colour most perceptually distinct from every colour already used by a
    type. ``eviter`` (the colour currently shown) is excluded so a repeated request yields a
    different distinct colour, powering the "generate another distinct colour" action."""
    avoid = {eviter.strip().upper()} if eviter and _HEX_RE.match(eviter.strip().upper()) else None
    return {"couleur": _next_free_colour(user.role, avoid=avoid)}


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
