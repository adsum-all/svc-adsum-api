"""Public engagement intake: capture people who express interest at public events.

Distinct from real member registration. A person scans the engagement QR and fills a
very short public form (or is entered manually / imported from Excel); they land in
``invitation_engagement`` as a lead, e-mail unique. An internal team later converts
selected leads into real member registrations (which then follow the normal flow).
The server is the only enforcement point; the public form is rate limited.
"""
# ruff: noqa: E501 - long SQL statements are kept on one line for readability
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr

from . import audit, db, ratelimit
from .fields import LineStr, ShortStr
from .permissions_rbac import require_permission
from .schemas import UserMe

public_router = APIRouter(prefix="/api/v1/public", tags=["engagement"])
router = APIRouter(prefix="/api/v1/admin/engagement", tags=["engagement"])

# Anglophone countries get the English thank-you; everyone else the French one.
_EN_PAYS = frozenset({"US", "GB", "CA", "IE", "AU", "NZ", "NG", "GH", "KE", "ZA", "UG", "TZ", "ZM", "ZW", "SL", "LR", "GM"})


class EngagementIn(BaseModel):
    email: EmailStr
    prenoms: ShortStr | None = None
    nom: ShortStr | None = None
    telephone: LineStr | None = None
    pays_indicatif: ShortStr | None = None
    pays_code: ShortStr | None = None
    evenement_id: str | None = None
    souhaite_telegram: bool = False


def _langue(pays_code: str | None) -> str:
    return "en" if (pays_code or "").strip().upper() in _EN_PAYS else "fr"


def _remercier(email: str, prenoms: str | None, pays_code: str | None) -> tuple[bool, str]:
    """Send the thank-you message (best effort). Returns (sent, channel).

    E-mail is the guaranteed channel for a lead (no member account yet, so no
    Telegram). Language follows the country. A failure never blocks the intake.
    """
    from .email_gateway import send_email

    prenom = (prenoms or "").split(" ")[0].strip() or ("dear friend" if _langue(pays_code) == "en" else "cher ami")
    if _langue(pays_code) == "en":
        subject = "Thank you for your interest"
        text = (
            f"Hello {prenom},\n\nThank you for choosing to take this step and express your wish to engage "
            "with the Sacerdoce Royal fraternity. Your interest has been received.\n\nWe will get back to you "
            "shortly with the next steps.\n\nWith our warmest regards,\nThe Sacerdoce Royal team"
        )
    else:
        subject = "Merci pour votre engagement"
        text = (
            f"Bonjour {prenom},\n\nMerci d'avoir fait ce beau choix et d'avoir exprime votre souhait de vous "
            "engager au sein de la fraternite du Sacerdoce Royal. Votre demande a bien ete recue.\n\nNous "
            "reviendrons vers vous tres prochainement avec les prochaines etapes.\n\nBien fraternellement,\n"
            "L'equipe du Sacerdoce Royal"
        )
    html = "<p>" + text.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    try:
        return send_email(email, subject, text, html)
    except Exception:  # noqa: BLE001 - a thank-you failure must never block the intake
        return False, "erreur"


def enregistrer_invitation(payload: EngagementIn, source: str, cree_par: str | None, role: str | None) -> dict[str, object]:
    """Insert one engagement lead (e-mail unique, family name uppercased), send the
    thank-you, and return the created row. Raises HTTP 409 on a duplicate e-mail so
    callers (public/manual/import) get one consistent behaviour."""
    email = str(payload.email).strip().lower()
    nom = (payload.nom or "").strip().upper() or None  # family name always uppercase
    prenoms = (payload.prenoms or "").strip() or None
    try:
        created = db.execute(
            "INSERT INTO invitation_engagement (email, prenoms, nom, telephone, pays_indicatif, pays_code, source, evenement_id, cree_par, souhaite_telegram) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (email, prenoms, nom, (payload.telephone or None), (payload.pays_indicatif or None),
             (payload.pays_code or None), source, (payload.evenement_id or None), cree_par, bool(payload.souhaite_telegram)),
            role=role,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="cette adresse e-mail est déjà enregistrée") from exc
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="enregistrement impossible")
    inv_id = str(created["id"])
    sent, canal = _remercier(email, prenoms, payload.pays_code)
    if sent:
        db.execute(
            "UPDATE invitation_engagement SET remerciement_envoye = true, remerciement_canal = %s WHERE id = %s",
            (canal, inv_id),
            role=role,
        )
    return {"id": inv_id, "email": email, "remerciement_envoye": sent}


@public_router.get("/engagement/contexte")
def engagement_contexte(evenement_id: str) -> dict[str, object]:
    """Public title of the activity a QR invitation is tied to, to show a discreet
    context on the form. Returns null on an unknown/invalid id, never an error."""
    import re

    if not re.fullmatch(r"[0-9a-fA-F-]{6,40}", evenement_id or ""):
        return {"titre": None}
    try:
        row = db.fetch_one("SELECT titre FROM evenement WHERE id = %s", (evenement_id,), role=None)
    except Exception:  # noqa: BLE001 - a malformed id must not raise on a public route
        return {"titre": None}
    return {"titre": row["titre"] if row else None}


@public_router.post("/engagement", status_code=status.HTTP_201_CREATED)
def engagement_public(payload: EngagementIn, request: Request) -> dict[str, object]:
    """Public engagement form (scanned QR). No auth; rate limited. E-mail required."""
    ratelimit.enforce(request, "public-engagement")
    # role=None: owner connection, so the public insert works while RLS stays a
    # defense in depth for authenticated paths.
    return enregistrer_invitation(payload, source="qr", cree_par=None, role=None)


@router.post("/invitations", status_code=status.HTTP_201_CREATED)
def creer_invitation_manuelle(
    payload: EngagementIn,
    user: Annotated[UserMe, Depends(require_permission("inscriptions.gerer"))],
) -> dict[str, object]:
    """Manual quick-entry by the welcome/event team (same rules as the public form)."""
    res = enregistrer_invitation(payload, source="manuel", cree_par=user.id, role=user.role)
    audit.log(user.id, user.role, "creation_invitation_engagement", "invitation_engagement", str(res["id"]), {"source": "manuel"})
    return res


@router.get("/invitations")
def list_invitations(
    user: Annotated[UserMe, Depends(require_permission("inscriptions.gerer"))],
    statut: str = "en_attente",
    evenement_id: str | None = None,
) -> list[dict[str, object]]:
    """Engagement leads at a given stage (en_attente / converti / toutes)."""
    clauses = []
    params: list[object] = []
    if statut in ("en_attente", "converti"):
        clauses.append("statut = %s")
        params.append(statut)
    if evenement_id:
        clauses.append("evenement_id = %s")
        params.append(evenement_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.fetch_all(
        "SELECT id, email, prenoms, nom, telephone, pays_indicatif, pays_code, source, statut, "
        "membre_id, remerciement_envoye, souhaite_telegram, cree_le, converti_le "
        f"FROM invitation_engagement {where} ORDER BY cree_le DESC",
        tuple(params),
        role=user.role,
    )
    return [
        {
            "id": str(r["id"]), "email": r["email"], "prenoms": r.get("prenoms"), "nom": r.get("nom"),
            "telephone": r.get("telephone"), "pays_indicatif": r.get("pays_indicatif"), "pays_code": r.get("pays_code"),
            "source": r["source"], "statut": r["statut"], "membre_id": str(r["membre_id"]) if r.get("membre_id") else None,
            "remerciement_envoye": bool(r["remerciement_envoye"]),
            "souhaite_telegram": bool(r.get("souhaite_telegram")),
            "cree_le": r["cree_le"].isoformat() if r.get("cree_le") else None,
            "converti_le": r["converti_le"].isoformat() if r.get("converti_le") else None,
        }
        for r in rows
    ]


@router.get("/dashboard")
def dashboard_engagement(
    user: Annotated[UserMe, Depends(require_permission("inscriptions.gerer"))],
    evenement_id: str | None = None,
) -> dict[str, object]:
    """Engagement indicators: totals, by channel, pending vs converted."""
    scope = "WHERE evenement_id = %s" if evenement_id else ""
    params = (evenement_id,) if evenement_id else ()
    total = db.fetch_one(f"SELECT count(*) AS n FROM invitation_engagement {scope}", params, role=user.role)
    par_source = db.fetch_all(f"SELECT source, count(*) AS n FROM invitation_engagement {scope} GROUP BY source", params, role=user.role)
    par_statut = db.fetch_all(f"SELECT statut, count(*) AS n FROM invitation_engagement {scope} GROUP BY statut", params, role=user.role)
    par_pays = db.fetch_all(
        f"SELECT COALESCE(NULLIF(pays_code, ''), 'NC') AS pays, count(*) AS n FROM invitation_engagement {scope} "
        "GROUP BY 1 ORDER BY n DESC LIMIT 8",
        params,
        role=user.role,
    )
    return {
        "total": int((total or {}).get("n", 0)),
        "par_canal": {str(r["source"]): int(r["n"]) for r in par_source},
        "par_pays": {str(r["pays"]): int(r["n"]) for r in par_pays},
        "en_attente": next((int(r["n"]) for r in par_statut if r["statut"] == "en_attente"), 0),
        "converti": next((int(r["n"]) for r in par_statut if r["statut"] == "converti"), 0),
    }


class ConvertirIn(BaseModel):
    ids: list[str]


@router.post("/convertir")
def convertir_invitations(
    payload: ConvertirIn,
    user: Annotated[UserMe, Depends(require_permission("inscriptions.administrer"))],
) -> dict[str, object]:
    """Convert selected engagement leads into real member registrations.

    Each pending lead becomes a member account (temp password sent, 'incomplet'
    stage) and is marked 'converti' so it leaves the pending queue. Already-converted
    or unknown ids are skipped. Reuses the single-registration helper."""
    from .inscription import creer_membre_inscription

    convertis: list[dict[str, str]] = []
    ignores: list[str] = []
    erreurs: list[dict[str, str]] = []
    for inv_id in payload.ids:
        row = db.fetch_one(
            "SELECT id, email, prenoms, nom FROM invitation_engagement WHERE id = %s AND statut = 'en_attente'",
            (inv_id,),
            role=user.role,
        )
        if not row:
            ignores.append(inv_id)
            continue
        try:
            r = creer_membre_inscription(str(row["email"]), row.get("prenoms"), row.get("nom"), user)
            db.execute(
                "UPDATE invitation_engagement SET statut = 'converti', membre_id = %s, converti_le = now() WHERE id = %s",
                (r["membre_id"], inv_id),
                role=user.role,
            )
            convertis.append({"id": inv_id, "matricule": str(r["matricule"])})
        except HTTPException as exc:
            if exc.status_code == 409:
                # Already a member for that e-mail: mark converted without a new account.
                db.execute("UPDATE invitation_engagement SET statut = 'converti', converti_le = now() WHERE id = %s", (inv_id,), role=user.role)
                ignores.append(inv_id)
            else:
                erreurs.append({"id": inv_id, "raison": str(exc.detail)})
    audit.log(user.id, user.role, "conversion_engagement", "invitation_engagement", None, {"convertis": len(convertis), "ignores": len(ignores)})
    return {"convertis": len(convertis), "details": convertis, "ignores": ignores, "erreurs": erreurs}
