"""Duplicate / fraud detection across members.

A multi-field similarity scan compares every member pair on name, date of birth,
phone, city and address (all open-source: PostgreSQL pg_trgm trigram similarity
plus exact matches). Each signal carries a weight; the weighted score in [0, 1]
is compared to a configurable threshold stored in the parametre table. Pairs at
or above the threshold are logged in detection_doublon and can be confirmed or
dismissed. A side-by-side comparison helps the administration decide.

Reserved to staff for reads; running the scan and deciding are reserved to
admins. Every decision is audited.
"""
# ruff: noqa: E501
from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import audit, db
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin/doublons", tags=["doublons"])

# The side-by-side comparison reveals every personal field of two members at once
# (e-mail, phone, date of birth, full address): keep it to the member-managing
# roles, never a field scanner (controleur) nor read-only supervision (direction).

# Signal weights, summing to 1.0. Name and date of birth dominate; the address
# adds trigram nuance; the city is a weak corroborating signal. A second set is
# used when both members have a photo hash, so the photo can weigh in.
_WEIGHTS = {"nom": 0.35, "date_naissance": 0.25, "telephone": 0.20, "adresse": 0.12, "ville": 0.08}
_WEIGHTS_PHOTO = {"nom": 0.28, "date_naissance": 0.20, "telephone": 0.16, "adresse": 0.10, "ville": 0.06, "photo": 0.20}


def _hamming_hex(a: str, b: str) -> int:
    """Number of differing bits between two equal-length hex strings."""
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _photo_similarity(a: object, b: object) -> float | None:
    """1 - normalized Hamming distance of two 64-bit dHash hex strings, or None."""
    if not isinstance(a, str) or not isinstance(b, str) or len(a) != 16 or len(b) != 16:
        return None
    try:
        return round(1.0 - _hamming_hex(a, b) / 64.0, 3)
    except ValueError:
        return None

_DISPLAY = (
    "id, matricule, prenoms, nom, email, telephone, date_naissance, ville, pays, adresse, verifie, photo_url"
)


def _seuil(role: str) -> float:
    row = db.fetch_one("SELECT valeur FROM parametre WHERE cle = 'seuil_doublon'", (), role=role)
    try:
        return float(row["valeur"]) if row and row["valeur"] is not None else 0.6
    except (TypeError, ValueError):
        return 0.6


class SeuilIn(BaseModel):
    seuil: float


class StatutIn(BaseModel):
    statut: str  # 'confirme' | 'ignore' | 'nouveau'


def _score(row: dict[str, object]) -> tuple[float, dict[str, object]]:
    name_sim = float(row["name_sim"] or 0.0)
    addr_sim = float(row["addr_sim"] or 0.0)
    same_dob = bool(row["same_dob"])
    same_phone = bool(row["same_phone"])
    same_city = bool(row["same_city"])
    photo_sim = _photo_similarity(row.get("a_phash"), row.get("b_phash"))
    w = _WEIGHTS_PHOTO if photo_sim is not None else _WEIGHTS
    score = (
        w["nom"] * name_sim
        + w["date_naissance"] * (1.0 if same_dob else 0.0)
        + w["telephone"] * (1.0 if same_phone else 0.0)
        + w["adresse"] * addr_sim
        + w["ville"] * (1.0 if same_city else 0.0)
    )
    signaux: dict[str, object] = {
        "nom": round(name_sim, 2),
        "date_naissance": same_dob,
        "telephone": same_phone,
        "ville": same_city,
        "adresse": round(addr_sim, 2),
    }
    if photo_sim is not None:
        score += w["photo"] * photo_sim
        signaux["photo"] = photo_sim
    return round(score, 3), signaux


_SCAN_SQL = """
    WITH m AS (
        SELECT id,
               lower(trim(coalesce(prenoms, '') || ' ' || coalesce(nom, ''))) AS fullname,
               regexp_replace(coalesce(telephone, ''), '[^0-9]', '', 'g') AS phone,
               date_naissance, ville, adresse, photo_phash
        FROM membre
    )
    SELECT a.id AS a_id, b.id AS b_id,
           similarity(a.fullname, b.fullname) AS name_sim,
           (a.date_naissance IS NOT NULL AND a.date_naissance = b.date_naissance) AS same_dob,
           (length(a.phone) >= 6 AND a.phone = b.phone) AS same_phone,
           (a.ville IS NOT NULL AND lower(a.ville) = lower(b.ville)) AS same_city,
           similarity(coalesce(a.adresse, ''), coalesce(b.adresse, '')) AS addr_sim,
           a.photo_phash AS a_phash, b.photo_phash AS b_phash
    FROM m a JOIN m b ON a.id < b.id
    WHERE similarity(a.fullname, b.fullname) > 0.3
       OR (length(a.phone) >= 6 AND a.phone = b.phone)
       OR (a.date_naissance IS NOT NULL AND a.date_naissance = b.date_naissance)
       OR similarity(coalesce(a.adresse, ''), coalesce(b.adresse, '')) > 0.5
       OR (a.photo_phash IS NOT NULL AND a.photo_phash = b.photo_phash)
"""


@router.post("/scan")
def scan(user: Annotated[UserMe, Depends(require_permission("doublons.administrer"))]) -> dict[str, object]:
    """Run the similarity scan and log every pair at or above the threshold."""
    seuil = _seuil(user.role)
    rows = db.fetch_all(_SCAN_SQL, (), role=user.role)
    flagged = 0
    for r in rows:
        score, signaux = _score(r)
        if score < seuil:
            continue
        flagged += 1
        # a < b is guaranteed by the join; keep the ordering the table expects.
        db.execute(
            """
            INSERT INTO detection_doublon (membre_a, membre_b, score, signaux)
            VALUES (%s, %s, %s, %s::jsonb)
            ON CONFLICT (membre_a, membre_b)
            DO UPDATE SET score = EXCLUDED.score, signaux = EXCLUDED.signaux, detecte_le = now()
            """,
            (str(r["a_id"]), str(r["b_id"]), score, json.dumps(signaux, default=str)),
            role=user.role,
        )
    audit.log(user.id, user.role, "scan_doublons", "membre", None, {"seuil": seuil, "flagged": flagged})
    return {"ok": True, "seuil": seuil, "pairs_scanned": len(rows), "flagged": flagged}


@router.get("")
def list_detections(user: Annotated[UserMe, Depends(require_permission("doublons.consulter"))], statut: str | None = None) -> list[dict[str, object]]:
    """Logged detections, most similar first, with both members' summary."""
    where = "WHERE d.statut = %s" if statut else ""
    params: tuple[object, ...] = (statut,) if statut else ()
    rows = db.fetch_all(
        f"""
        SELECT d.id, d.score, d.signaux, d.statut, d.detecte_le,
               a.id AS a_id, a.matricule AS a_mat, a.prenoms AS a_prenoms, a.nom AS a_nom, a.verifie AS a_verifie,
               b.id AS b_id, b.matricule AS b_mat, b.prenoms AS b_prenoms, b.nom AS b_nom, b.verifie AS b_verifie
        FROM detection_doublon d
        JOIN membre a ON a.id = d.membre_a
        JOIN membre b ON b.id = d.membre_b
        {where}
        ORDER BY d.score DESC, d.detecte_le DESC
        LIMIT 200
        """,
        params,
        role=user.role,
    )
    return [
        {
            "id": str(r["id"]),
            "score": float(r["score"]),
            "signaux": r["signaux"],
            "statut": r["statut"],
            "detecte_le": r["detecte_le"].isoformat() if r["detecte_le"] else None,
            "membre_a": {"id": str(r["a_id"]), "matricule": r["a_mat"], "prenoms": r["a_prenoms"], "nom": r["a_nom"], "verifie": bool(r["a_verifie"])},
            "membre_b": {"id": str(r["b_id"]), "matricule": r["b_mat"], "prenoms": r["b_prenoms"], "nom": r["b_nom"], "verifie": bool(r["b_verifie"])},
        }
        for r in rows
    ]


@router.get("/comparaison")
def comparaison(a: str, b: str, user: Annotated[UserMe, Depends(require_permission("doublons.gerer"))]) -> dict[str, object]:
    """Side-by-side comparison of two members with per-field match flags."""
    ma = db.fetch_one(f"SELECT {_DISPLAY} FROM membre WHERE id = %s", (a,), role=user.role)
    mb = db.fetch_one(f"SELECT {_DISPLAY} FROM membre WHERE id = %s", (b,), role=user.role)
    if not ma or not mb:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    champs = ["prenoms", "nom", "email", "telephone", "date_naissance", "ville", "pays", "adresse"]
    lignes = []
    for champ in champs:
        va = ma.get(champ)
        vb = mb.get(champ)
        same = va is not None and str(va).strip().lower() == str(vb).strip().lower() if vb is not None else False
        lignes.append({"champ": champ, "a": _s(va), "b": _s(vb), "identique": bool(same)})
    return {
        "a": {"id": str(ma["id"]), "matricule": ma["matricule"], "verifie": bool(ma["verifie"]), "photo_url": ma["photo_url"]},
        "b": {"id": str(mb["id"]), "matricule": mb["matricule"], "verifie": bool(mb["verifie"]), "photo_url": mb["photo_url"]},
        "lignes": lignes,
    }


def _s(v: object) -> str | None:
    return None if v is None else str(v)


@router.post("/{detection_id}/statut")
def decider(detection_id: str, payload: StatutIn, user: Annotated[UserMe, Depends(require_permission("doublons.administrer"))]) -> dict[str, object]:
    """Confirm or dismiss a detection."""
    if payload.statut not in ("nouveau", "confirme", "ignore"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid statut")
    row = db.execute(
        "UPDATE detection_doublon SET statut = %s, decide_le = now(), decide_par = %s WHERE id = %s RETURNING id",
        (payload.statut, user.id, detection_id),
        role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="detection not found")
    audit.log(user.id, user.role, "decision_doublon", "detection_doublon", detection_id, {"statut": payload.statut})
    return {"ok": True, "statut": payload.statut}


@router.get("/seuil")
def get_seuil(user: Annotated[UserMe, Depends(require_permission("doublons.consulter"))]) -> dict[str, float]:
    return {"seuil": _seuil(user.role)}


@router.put("/seuil")
def set_seuil(payload: SeuilIn, user: Annotated[UserMe, Depends(require_permission("doublons.administrer"))]) -> dict[str, float]:
    if not 0.0 <= payload.seuil <= 1.0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="seuil must be between 0 and 1")
    db.execute(
        """
        INSERT INTO parametre (cle, valeur, categorie, description, maj_par, maj_le)
        VALUES ('seuil_doublon', %s::jsonb, 'doublon', 'Duplicate detection threshold', %s, now())
        ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur, maj_par = EXCLUDED.maj_par, maj_le = now()
        """,
        (str(payload.seuil), user.id),
        role=user.role,
    )
    audit.log(user.id, user.role, "config_seuil_doublon", "parametre", "seuil_doublon", {"seuil": payload.seuil})
    return {"seuil": payload.seuil}
