"""Consent documents and electronic signature of the engagement.

Members read the active documents (data protection, confidentiality charter,
engagement letter, internal rules) resolved to their language, then sign them
electronically: a one-time code is sent over every enabled channel (e-mail plus
in-app, Telegram and WhatsApp through the notification engine); once the member
enters it, the signature is recorded with its request context (ip, user agent,
channels) and a proof hash, and one engagement row is written per document. A
verified signature covering all active blocking documents is what unlocks the
final registration submission.

The administration reads and edits the documents bilingually (FR/EN) and can
publish a new version, which supersedes the active one while older versions are
kept for the evidence trail.
"""
# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from . import audit, db
from .email_gateway import consume_code, generate_code, send_code, verify_code
from .inscription import _membre_ctx
from .notifications import notifier
from .permissions_rbac import require_permission
from .schemas import (
    ConsentDocOut,
    ConsentDocPublishIn,
    ConsentDocSummary,
    SignatureDemandeIn,
    SignatureVerifIn,
)

router = APIRouter(prefix="/api/v1", tags=["consentement"])


SIGNATURE_PURPOSE = "engagement"


def _lang_titre(row: dict[str, object], lang: str) -> str:
    """Resolve the title to the member's language, French being the default."""
    if lang == "en" and row.get("titre_en"):
        return str(row["titre_en"])
    return str(row["titre"])


def _lang_contenu(row: dict[str, object], lang: str) -> str:
    if lang == "en" and row.get("contenu_en"):
        return str(row["contenu_en"])
    return str(row["contenu"])


def _membre_langue(membre_id: str, role: str) -> str:
    row = db.fetch_one("SELECT langue FROM membre WHERE id = %s", (membre_id,), role=role)
    return (row["langue"] if row and row.get("langue") else "fr") or "fr"


def _membre_email(membre_id: str, role: str) -> str:
    row = db.fetch_one("SELECT email FROM membre WHERE id = %s", (membre_id,), role=role)
    if not row or not row.get("email"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="member has no e-mail address")
    return str(row["email"])


# --- Member: read the active documents --------------------------------------

@router.get("/consentements", response_model=list[ConsentDocSummary])
def list_consentements(ctx: Annotated[tuple[str, str], Depends(_membre_ctx)]) -> list[ConsentDocSummary]:
    """Active documents (summary), title resolved to the member's language."""
    membre_id, role = ctx
    lang = _membre_langue(membre_id, role)
    rows = db.fetch_all(
        "SELECT cle, version, titre, titre_en, bloquant, ordre FROM document_consentement "
        "WHERE actif = true ORDER BY ordre, cle",
        (),
        role=role,
    )
    return [
        ConsentDocSummary(
            cle=str(r["cle"]),
            version=int(r["version"]),
            titre=_lang_titre(r, lang),
            bloquant=bool(r["bloquant"]),
            ordre=int(r["ordre"]),
        )
        for r in rows
    ]


@router.get("/consentements/{cle}", response_model=ConsentDocOut)
def get_consentement(cle: str, ctx: Annotated[tuple[str, str], Depends(_membre_ctx)]) -> ConsentDocOut:
    """Full active document, title and body resolved to the member's language."""
    membre_id, role = ctx
    lang = _membre_langue(membre_id, role)
    row = db.fetch_one(
        "SELECT cle, version, titre, titre_en, contenu, contenu_en FROM document_consentement "
        "WHERE cle = %s AND actif = true",
        (cle,),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
    return ConsentDocOut(
        cle=str(row["cle"]),
        version=int(row["version"]),
        titre=_lang_titre(row, lang),
        contenu=_lang_contenu(row, lang),
    )


# --- Admin: read and publish -------------------------------------------------

@router.get("/admin/consentements")
def admin_list_consentements(user: Annotated[object, Depends(require_permission("consentements.consulter"))]) -> list[dict[str, object]]:
    """Active documents in both languages, for the administration."""
    role = user.role  # type: ignore[attr-defined]
    rows = db.fetch_all(
        "SELECT cle, version, titre, titre_en, contenu, contenu_en, bloquant, ordre, publie_le "
        "FROM document_consentement WHERE actif = true ORDER BY ordre, cle",
        (),
        role=role,
    )
    return [
        {
            "cle": str(r["cle"]),
            "version": int(r["version"]),
            "titre": r["titre"],
            "titre_en": r["titre_en"],
            "contenu": r["contenu"],
            "contenu_en": r["contenu_en"],
            "bloquant": bool(r["bloquant"]),
            "ordre": int(r["ordre"]),
            "publie_le": r["publie_le"].isoformat() if r.get("publie_le") else None,
        }
        for r in rows
    ]


@router.get("/admin/consentements/{cle}")
def admin_get_consentement(cle: str, user: Annotated[object, Depends(require_permission("consentements.consulter"))]) -> dict[str, object]:
    """Full active document in both languages, for the administration."""
    role = user.role  # type: ignore[attr-defined]
    row = db.fetch_one(
        "SELECT cle, version, titre, titre_en, contenu, contenu_en, bloquant, ordre, publie_le "
        "FROM document_consentement WHERE cle = %s AND actif = true",
        (cle,),
        role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
    return {
        "cle": str(row["cle"]),
        "version": int(row["version"]),
        "titre": row["titre"],
        "titre_en": row["titre_en"],
        "contenu": row["contenu"],
        "contenu_en": row["contenu_en"],
        "bloquant": bool(row["bloquant"]),
        "ordre": int(row["ordre"]),
        "publie_le": row["publie_le"].isoformat() if row.get("publie_le") else None,
    }


@router.post("/admin/consentements/{cle}")
def publish_consentement(cle: str, payload: ConsentDocPublishIn, user: Annotated[object, Depends(require_permission("consentements.administrer"))]) -> dict[str, object]:
    """Publish a new version of a document: it supersedes the active one.

    The previous active version is kept (actif = false) for the evidence trail,
    and the new version becomes the single active one for that key.
    """
    role = user.role  # type: ignore[attr-defined]
    actor_id = user.id  # type: ignore[attr-defined]
    row = db.fetch_one("SELECT COALESCE(MAX(version), 0) AS last FROM document_consentement WHERE cle = %s", (cle,), role=role)
    next_version = int(row["last"]) + 1 if row else 1
    db.execute("UPDATE document_consentement SET actif = false WHERE cle = %s AND actif = true", (cle,), role=role)
    created = db.execute(
        "INSERT INTO document_consentement (cle, version, titre, titre_en, contenu, contenu_en, bloquant, ordre, actif, publie_le, publie_par) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true, now(), %s) RETURNING id",
        (cle, next_version, payload.titre, payload.titre_en, payload.contenu, payload.contenu_en, payload.bloquant, payload.ordre, actor_id),
        role=role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="document not published")
    audit.log(actor_id, role, "publication_consentement", "document_consentement", cle, {"version": next_version, "bloquant": payload.bloquant})
    return {"ok": True, "cle": cle, "version": next_version}


# --- Member: electronic signature -------------------------------------------

def _client_ip(request: Request) -> str | None:
    # Delegate to the trusted resolver: the leftmost X-Forwarded-For value is
    # attacker-controlled, so a consent proof must use the platform-set hop instead.
    from .clientip import client_ip

    return client_ip(request)


@router.post("/consentements/signature/demander")
def demander_signature(payload: SignatureDemandeIn, request: Request, ctx: Annotated[tuple[str, str], Depends(_membre_ctx)]) -> dict[str, object]:
    """Open a signing event and send the one-time code over every enabled channel.

    A pending (code_verifie = false) signature_engagement row is created with the
    listed documents and the request context; the code is delivered by e-mail and,
    through the notification engine, in-app plus Telegram and WhatsApp when opted in.
    """
    membre_id, role = ctx
    email = _membre_email(membre_id, role)
    documents = [d.model_dump() for d in payload.documents]
    ip = _client_ip(request)
    user_agent = request.headers.get("user-agent")
    session_id = request.headers.get("x-session-id")
    created = db.execute(
        "INSERT INTO signature_engagement (membre_id, documents, ip, session_id, user_agent, code_verifie) "
        "VALUES (%s, %s::jsonb, %s, %s, %s, false) RETURNING id",
        (membre_id, json.dumps(documents), ip, session_id, user_agent),
        role=role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="signature not initialized")
    signature_id = str(created["id"])
    code = generate_code(email, SIGNATURE_PURPOSE)
    sent, provider = send_code(email, SIGNATURE_PURPOSE)
    canaux = notifier(membre_id, role, "engagement_code", {"code": code})
    if sent and "email" not in canaux:
        canaux = [*canaux, "email"]
    audit.log(None, role, "demande_signature_engagement", "signature_engagement", signature_id, {"canaux": canaux, "email_envoye": sent, "canal_email": provider})
    return {"ok": True, "canaux": canaux, "signature_id": signature_id}


@router.post("/consentements/signature/verifier")
def verifier_signature(payload: SignatureVerifIn, ctx: Annotated[tuple[str, str], Depends(_membre_ctx)]) -> dict[str, object]:
    """Confirm the signature with the one-time code and record every document.

    On success the pending signing event is marked verified with a proof hash, and
    one engagement row is written per document (with its signature context). This
    verified signature unlocks the final registration submission.
    """
    membre_id, role = ctx
    email = _membre_email(membre_id, role)
    if not verify_code(email, SIGNATURE_PURPOSE, payload.code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code_invalide": True})
    pending = db.fetch_one(
        "SELECT id, documents, ip FROM signature_engagement WHERE membre_id = %s AND code_verifie = false "
        "ORDER BY cree_le DESC LIMIT 1",
        (membre_id,),
        role=role,
    )
    if not pending:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"no_pending_signature": True})
    # Burn the code now (single-use). Atomic against a concurrent replay of the
    # same code: only the first request wins, the loser is rejected here.
    if not consume_code(email, SIGNATURE_PURPOSE, payload.code, ip=pending.get("ip")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code_deja_utilise": True})
    signature_id = str(pending["id"])
    documents = pending["documents"] or []
    if isinstance(documents, str):
        documents = json.loads(documents)
    ip = pending.get("ip")
    now = datetime.now(tz=UTC)
    proof = hashlib.sha256(f"{membre_id}|{json.dumps(documents, sort_keys=True)}|{now.isoformat()}".encode()).hexdigest()
    db.execute(
        "UPDATE signature_engagement SET code_verifie = true, signe_le = %s, hash_preuve = %s WHERE id = %s",
        (now, proof, signature_id),
        role=role,
    )
    for doc in documents:
        cle = str(doc.get("cle"))
        version = str(doc.get("version"))
        db.execute(
            "INSERT INTO engagement (membre_id, type, version, signe_le, hash_preuve, ip, canal, code_verifie, signature_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'electronique', true, %s)",
            (membre_id, cle, version, now, proof, ip, signature_id),
            role=role,
        )
    audit.log(None, role, "signature_engagement_verifiee", "signature_engagement", signature_id, {"documents": len(documents)})
    return {"ok": True}


@router.get("/consentements/signature/etat")
def etat_signature(ctx: Annotated[tuple[str, str], Depends(_membre_ctx)]) -> dict[str, object]:
    """Whether a verified signature covers all currently active blocking documents."""
    membre_id, role = ctx
    return {"signe": _signature_couvre_bloquants(membre_id, role)}


def _signature_couvre_bloquants(membre_id: str, role: str) -> bool:
    """True when every active blocking document has a verified engagement for its version."""
    required = db.fetch_all(
        "SELECT cle, version FROM document_consentement WHERE actif = true AND bloquant = true",
        (),
        role=role,
    )
    if not required:
        return True
    for doc in required:
        signed = db.fetch_one(
            "SELECT 1 FROM engagement WHERE membre_id = %s AND type = %s AND version = %s AND code_verifie = true LIMIT 1",
            (membre_id, str(doc["cle"]), str(doc["version"])),
            role=role,
        )
        if not signed:
            return False
    return True
