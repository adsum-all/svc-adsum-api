"""Member data modification lifecycle: the single admin-opened cycle.

One cohesive domain, kept out of inscription.py and demandes.py so both stay
within the size policy:

- the member's single, idempotent submission (text fields AND a staged
  replacement photo, submitted once, the whole unlock consumed atomically);
- the administration's final validation (commit the fields, promote the staged
  photo to the live photo, or reject and discard).

The open cycle is the source of truth: a request in 'attente_membre' with
unlocked fields. The status flip to 'en_validation' is the atomic lock, so no
refresh, double click, extra tab or direct API retry can resubmit.
"""
from __future__ import annotations

import json
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from . import db, identite
from .auth import current_user
from .demandes import _notify_ticket, _system_message, require_lecture, require_staff
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["modifications"])

# Fields a member may self-edit once the administration unlocks them.
_EDITABLE_FIELDS = {
    "prenoms", "nom", "telephone", "indicatif_telephone", "date_naissance",
    "naissance_annee_visible", "genre", "pays", "region", "ville", "adresse",
    "adresse_complement", "commission_id", "intendance_id", "tribu_id", "groupe",
    "profession", "niveau_etudes", "situation_matrimoniale", "type_mariage",
    "baptise", "confirme", "premiere_communion", "type_membre", "fonction_cle",
}

# Defense-in-depth whitelist: only these member columns can be committed from a
# pending proposal, even though the submission already filtered the input.
_COMMITTABLE_FIELDS = frozenset(
    {
        "prenoms", "nom", "telephone", "date_naissance", "genre", "pays", "ville",
        "commission_id", "intendance_id", "tribu_id", "groupe", "profession",
        "niveau_etudes", "situation_matrimoniale", "type_mariage",
        "baptise", "confirme", "premiere_communion",
    }
)


def _membre_ctx(user: Annotated[UserMe, Depends(current_user)]) -> tuple[str, str]:
    if not user.membre_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account is not linked to a member")
    return user.membre_id, user.role


# --- Member: the single submission ----------------------------------------

class SoumettreModif(BaseModel):
    """Payload of the single member submission: the edited text fields, and a
    hint that a replacement photo was staged (correctness never depends on the
    hint, only the confirmation wording does)."""

    champs: dict[str, str] = {}
    inclure_photo: bool = False


@router.post("/membres/me/modifications/soumettre")
def soumettre_modifications(
    payload: SoumettreModif, ctx: Annotated[tuple[str, str], Depends(_membre_ctx)]
) -> dict[str, object]:
    """One and only business submission for an admin-opened modification cycle.

    Gathers the edited text fields AND any staged replacement photo into a single
    proposal, consumes the whole unlock and moves the request to 'en_validation'.
    Idempotent: a refresh, a double click, a second tab or a direct API retry all
    find the cycle already submitted and get a clean 409."""
    membre_id, role = ctx
    return soumettre_cycle(membre_id, role, dict(payload.champs), payload.inclure_photo)


def soumettre_cycle(
    membre_id: str, role: str, fields: dict[str, object], inclure_photo: bool
) -> dict[str, object]:
    """Single, idempotent submission of an admin-opened modification cycle.

    The open cycle is the member's request in 'attente_membre' together with the
    unlocked fields. The status flip to 'en_validation' is the atomic lock: only
    the first caller flips it, so any replay hits a request that is no longer
    'attente_membre' and is rejected. The whole unlock is then consumed, so
    nothing stays editable until the administration opens a new cycle. The
    ``inclure_photo`` hint only shapes the confirmation wording; a staged photo is
    part of the cycle whenever one exists and 'photo_identite' was unlocked."""
    etat = db.fetch_one(
        "SELECT coalesce(champs_deverrouilles, '{}') AS deverrouilles, photo_pending_url "
        "FROM membre WHERE id = %s",
        (membre_id,),
        role=role,
    )
    if not etat:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="member not found")
    unlocked = set(etat.get("deverrouilles") or [])
    photo_incluse = bool(etat.get("photo_pending_url")) and "photo_identite" in unlocked
    cycle = db.fetch_one(
        "SELECT id FROM demande WHERE membre_id = %s AND statut = 'attente_membre' "
        "ORDER BY maj_le DESC LIMIT 1",
        (membre_id,),
        role=role,
    )
    if not unlocked or not cycle:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Aucun cycle de modification ouvert. L'administration doit débloquer les éléments à corriger.",
        )
    demande_id = str(cycle["id"])
    fields = {k: v for k, v in fields.items() if k in _EDITABLE_FIELDS}
    forbidden = [k for k in fields if k not in unlocked]
    if forbidden:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"locked_fields": forbidden})
    if not fields and not photo_incluse:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Aucune modification à soumettre.")
    # ATOMIC LOCK: flip the cycle. A single statement, its own transaction: only
    # the first caller gets a row back, every replay gets none and is refused.
    locked = db.execute(
        "UPDATE demande SET statut = 'en_validation', echeance_reponse = NULL, maj_le = now() "
        "WHERE id = %s AND statut = 'attente_membre' RETURNING id",
        (demande_id,),
        role=role,
    )
    if not locked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cette demande a déjà été soumise. Une seule soumission est possible par déblocage.",
        )
    if fields:
        before = db.fetch_one(f"SELECT {', '.join(fields)} FROM membre WHERE id = %s", (membre_id,), role=role) or {}
        try:
            db.execute(
                "INSERT INTO modification_membre (membre_id, demande_id, valeurs, valeurs_avant) "
                "VALUES (%s, %s, %s::jsonb, %s::jsonb)",
                (membre_id, demande_id, json.dumps(fields, default=str), json.dumps(dict(before), default=str)),
                role=role,
            )
        except psycopg.errors.UniqueViolation as exc:  # pragma: no cover - guarded by the atomic flip above
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Cette demande a déjà été soumise."
            ) from exc
    # Consume the whole unlock: no field stays editable until a new admin cycle.
    db.execute("UPDATE membre SET champs_deverrouilles = '{}' WHERE id = %s", (membre_id,), role=role)
    quoi = []
    if fields:
        quoi.append("informations")
    if photo_incluse:
        quoi.append("photo d'identité")
    detail = " et ".join(quoi) if quoi else "modifications"
    _system_message(
        demande_id, role,
        f"Le membre a soumis ses {detail} pour validation. En attente de validation finale par l'administration.",
    )
    _notify_ticket(
        demande_id, role, "Modification soumise",
        f"Vos {detail} ont été transmises. Elles seront enregistrées après validation par l'administration.",
    )
    return {"ok": True, "pending_validation": True, "champs": list(fields), "photo": photo_incluse}


# --- Administration: final validation --------------------------------------

class ModifDecision(BaseModel):
    decision: str  # 'valider' | 'rejeter'


@router.get("/admin/demandes/{demande_id}/modifications")
def admin_demande_modifications(
    demande_id: str, user: Annotated[UserMe, Depends(require_lecture)]
) -> list[dict[str, object]]:
    """Pending and past member modifications attached to this request, as a diff."""
    rows = db.fetch_all(
        "SELECT id, valeurs, valeurs_avant, statut, propose_le, decide_le "
        "FROM modification_membre WHERE demande_id = %s ORDER BY propose_le DESC",
        (demande_id,),
        role=user.role,
    )
    out: list[dict[str, object]] = []
    for r in rows:
        valeurs = r["valeurs"] or {}
        avant = r["valeurs_avant"] or {}
        diff = [{"champ": k, "avant": avant.get(k), "apres": v} for k, v in valeurs.items()]
        out.append(
            {
                "id": str(r["id"]),
                "statut": r["statut"],
                "propose_le": r["propose_le"].isoformat() if r["propose_le"] else None,
                "decide_le": r["decide_le"].isoformat() if r["decide_le"] else None,
                "diff": diff,
            }
        )
    return out


@router.get("/admin/demandes/{demande_id}/photo-pending")
def admin_photo_pending(demande_id: str, user: Annotated[UserMe, Depends(require_lecture)]) -> dict[str, object]:
    """Signed preview of a replacement photo the member staged on this request,
    with its focal point, so the administration reviews the actual new photo,
    framed the way it will be shown, before validating."""
    from . import storage
    from .config import settings

    owner = db.fetch_one(
        "SELECT m.photo_pending_url AS p, m.photo_pending_focus_x AS fx, m.photo_pending_focus_y AS fy "
        "FROM demande d JOIN membre m ON m.id = d.membre_id WHERE d.id = %s",
        (demande_id,),
        role=user.role,
    )
    path = (owner or {}).get("p")
    if not path:
        return {"url": None, "focus_x": None, "focus_y": None}
    try:
        url: str | None = storage.signed_download_url(settings.storage_bucket_photos, str(path))
    except storage.StorageError:
        url = None
    return {"url": url, "focus_x": (owner or {}).get("fx"), "focus_y": (owner or {}).get("fy")}


@router.post("/admin/demandes/{demande_id}/modifications/decision")
def admin_decide_modification(
    demande_id: str, payload: ModifDecision, user: Annotated[UserMe, Depends(require_staff)]
) -> dict[str, object]:
    """Final validation of the member's single modification submission.

    A submission bundles the edited text fields (if any) AND a staged replacement
    photo (if any); this decides on the whole bundle at once, and also handles a
    photo-only submission (no modification_membre row). On 'valider' the fields
    are committed and the staged photo is promoted to the live photo (copied onto
    the stable live path, the staging object is removed). On 'rejeter' nothing is
    committed and the staged photo is discarded. Either way the unlock is cleared
    and the member is notified.
    """
    if payload.decision not in ("valider", "rejeter"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="decision must be valider or rejeter")
    dem = db.fetch_one("SELECT membre_id FROM demande WHERE id = %s", (demande_id,), role=user.role)
    if not dem:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    membre_id = str(dem["membre_id"])
    mod = db.fetch_one(
        "SELECT id, valeurs FROM modification_membre "
        "WHERE demande_id = %s AND statut = 'en_attente' ORDER BY propose_le DESC LIMIT 1",
        (demande_id,),
        role=user.role,
    )
    membre = db.fetch_one("SELECT photo_pending_url FROM membre WHERE id = %s", (membre_id,), role=user.role)
    photo_pending = (membre or {}).get("photo_pending_url")
    if not mod and not photo_pending:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no pending modification")
    if payload.decision == "valider":
        applied: dict[str, object] = {}
        if mod:
            applied = {k: v for k, v in (mod["valeurs"] or {}).items() if k in _COMMITTABLE_FIELDS}
            # Normalise the civil identity on commit: family name uppercase, given
            # names title case (single source of truth in app/identite.py).
            if applied.get("nom") is not None:
                applied["nom"] = identite.normaliser_nom(str(applied["nom"]))
            if applied.get("prenoms") is not None:
                applied["prenoms"] = identite.normaliser_prenoms(str(applied["prenoms"]))
            if applied:
                sets = ", ".join(f"{k} = %s" for k in applied)
                db.execute(f"UPDATE membre SET {sets} WHERE id = %s", (*applied.values(), membre_id), role=user.role)
            db.execute(
                "UPDATE modification_membre SET statut = 'validee', decide_le = now(), decide_par = %s WHERE id = %s",
                (user.id, str(mod["id"])),
                role=user.role,
            )
        photo_promue = _promouvoir_photo_pending(membre_id, str(photo_pending), user.role) if photo_pending else False
        db.execute("UPDATE membre SET champs_deverrouilles = '{}' WHERE id = %s", (membre_id,), role=user.role)
        db.execute("UPDATE demande SET statut = 'resolue', maj_le = now() WHERE id = %s", (demande_id,), role=user.role)
        _notify_member(
            demande_id, user.role, "Modification validée",
            "Votre modification a été validée et enregistrée.",
        )
        return {"ok": True, "statut": "validee", "champs": list(applied), "photo": photo_promue}
    if mod:
        db.execute(
            "UPDATE modification_membre SET statut = 'rejetee', decide_le = now(), decide_par = %s WHERE id = %s",
            (user.id, str(mod["id"])),
            role=user.role,
        )
    if photo_pending:
        from . import storage
        from .config import settings

        db.execute(
            "UPDATE membre SET photo_pending_url = NULL, photo_pending_phash = NULL, "
            "photo_pending_focus_x = NULL, photo_pending_focus_y = NULL WHERE id = %s",
            (membre_id,),
            role=user.role,
        )
        storage.delete_object(settings.storage_bucket_photos, str(photo_pending))
    db.execute("UPDATE membre SET champs_deverrouilles = '{}' WHERE id = %s", (membre_id,), role=user.role)
    db.execute("UPDATE demande SET statut = 'refusee', maj_le = now() WHERE id = %s", (demande_id,), role=user.role)
    _notify_member(
        demande_id, user.role, "Modification refusée",
        "Votre modification n'a pas été validée. Contactez l'administration.",
    )
    return {"ok": True, "statut": "rejetee"}


def _promouvoir_photo_pending(membre_id: str, pending_path: str, role: str) -> bool:
    """Promote a staged replacement photo to the live photo. The live photo lives
    at a stable path ({membre}/photo.jpg); the staged bytes are copied onto it and
    the staging object removed, so the stable path invariant holds for the next
    replacement. Returns True on success, False if the staged object is gone."""
    from . import storage
    from .config import settings

    live_path = f"{membre_id}/photo.jpg"
    try:
        data = storage.download_bytes(settings.storage_bucket_photos, pending_path)
        storage.upload_bytes(settings.storage_bucket_photos, live_path, data, "image/jpeg")
    except storage.StorageError:
        return False
    storage.delete_object(settings.storage_bucket_photos, pending_path)
    db.execute(
        "UPDATE membre SET photo_url = %s, photo_phash = photo_pending_phash, "
        "photo_focus_x = photo_pending_focus_x, photo_focus_y = photo_pending_focus_y, "
        "photo_pending_url = NULL, photo_pending_phash = NULL, "
        "photo_pending_focus_x = NULL, photo_pending_focus_y = NULL WHERE id = %s",
        (live_path, membre_id),
        role=role,
    )
    return True


def _notify_member(demande_id: str, role: str, titre: str, corps: str) -> None:
    row = db.fetch_one("SELECT membre_id FROM demande WHERE id = %s", (demande_id,), role=role)
    if not row:
        return
    db.execute(
        "INSERT INTO notification (membre_id, type, titre, corps, lu, cree_le) "
        "VALUES (%s, 'demande', %s, %s, false, now())",
        (str(row["membre_id"]), titre, corps),
        role=role,
    )
