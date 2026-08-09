"""Client organisations and their licences, from the publisher's side.

The platform is built to be sold and nothing knew it. There was no customer, no
contract, no expiry, no way to suspend an unpaid account or grant a free one: the
commercial reality lived in somebody's memory.

The rules that matter, and why they are rules rather than conventions.

**A suspension always names a reason and an author.** A client locked out deserves
better than "the system did it", and whoever locked them out must be identifiable.
The database refuses a suspension without both.

**A licence is superseded, never edited.** Granting a new one stamps the previous as
replaced and keeps it. An organisation that disputes what it was promised is settled
by reading a row rather than by remembering a conversation.

**One licence in force at a time.** Two overlapping licences make "what are they
entitled to" unanswerable, which is the only question this exists to answer. Enforced
by a partial unique index, so two simultaneous grants cannot both succeed.

What is deliberately absent: any way to reach into an organisation's data. This module
knows a customer's name, contract and state. It cannot list their members, and the
console that calls it has no screen that could.
"""
# ruff: noqa: E501
from __future__ import annotations

import re
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field

from . import audit, db
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/support/console/organisations", tags=["support-console"])

ETATS = ("evaluation", "active", "suspendue", "resiliee")
_CODE = re.compile(r"^[a-z][a-z0-9-]{2,39}$")


def _licence_out(r: dict[str, object]) -> dict[str, object]:
    fin = r.get("fin")
    return {
        "id": str(r["id"]),
        "formule": r["formule"],
        "membres_inclus": r.get("membres_inclus"),
        "debut": r["debut"].isoformat() if r.get("debut") else None,
        "fin": fin.isoformat() if fin else None,
        "gracieuse": bool(r.get("gracieuse")),
        "motif": r.get("motif"),
        "remplacee_le": r["remplacee_le"].isoformat() if r.get("remplacee_le") else None,
        # Computed here rather than in the browser: an expiry that each client works
        # out for itself eventually disagrees with the server about who is licensed.
        "expiree": bool(fin and fin < date.today()),
        "jours_restants": (fin - date.today()).days if fin else None,
    }


def _org_out(r: dict[str, object], licence: dict[str, object] | None) -> dict[str, object]:
    return {
        "id": str(r["id"]),
        "code": r["code"],
        "nom": r["nom"],
        "pays": r.get("pays"),
        "ville": r.get("ville"),
        "contact_nom": r.get("contact_nom"),
        "contact_email": r.get("contact_email"),
        "contact_telephone": r.get("contact_telephone"),
        "etat": r["etat"],
        "suspendue_motif": r.get("suspendue_motif"),
        "suspendue_le": r["suspendue_le"].isoformat() if r.get("suspendue_le") else None,
        "note": r.get("note"),
        "cree_le": r["cree_le"].isoformat() if r.get("cree_le") else None,
        "licence": licence,
    }


def _licence_en_vigueur(organisation_id: str, role: str | None) -> dict[str, object] | None:
    r = db.fetch_one(
        "SELECT * FROM licence WHERE organisation_id = %s AND remplacee_le IS NULL",
        (organisation_id,),
        role=role,
    )
    return _licence_out(dict(r)) if r else None


@router.get("")
def lister(
    user: Annotated[UserMe, Depends(require_permission("support.traiter"))],
    etat: str = Query(default="", description="Filtre sur un état, vide pour tous"),
) -> dict[str, object]:
    """Every customer, with the licence currently in force.

    Sorted by state then name so what needs attention (suspended, in evaluation) is not
    buried under what is running fine.
    """
    conditions: list[str] = []
    params: list[object] = []
    if etat:
        if etat not in ETATS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"état inconnu : {etat}")
        conditions.append("etat = %s")
        params.append(etat)
    ou = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = db.fetch_all(
        "SELECT * FROM organisation_cliente" + ou
        + " ORDER BY CASE etat WHEN 'suspendue' THEN 0 WHEN 'evaluation' THEN 1 WHEN 'active' THEN 2 ELSE 3 END, nom",
        tuple(params),
        role=user.role,
    )
    organisations = [_org_out(dict(r), _licence_en_vigueur(str(r["id"]), user.role)) for r in rows]
    return {
        "total": len(organisations),
        "par_etat": {
            e: sum(1 for o in organisations if o["etat"] == e) for e in ETATS
        },
        "organisations": organisations,
    }


class OrganisationIn(BaseModel):
    code: str = Field(min_length=3, max_length=40)
    nom: str = Field(min_length=2, max_length=160)
    pays: str = Field(default="", max_length=80)
    ville: str = Field(default="", max_length=80)
    contact_nom: str = Field(default="", max_length=120)
    contact_email: EmailStr | None = None
    contact_telephone: str = Field(default="", max_length=40)
    note: str = Field(default="", max_length=2000)


@router.post("", status_code=status.HTTP_201_CREATED)
def creer(
    payload: OrganisationIn,
    user: Annotated[UserMe, Depends(require_permission("support.traiter"))],
) -> dict[str, object]:
    """Record a customer. It starts in evaluation, never active.

    Defaulting to active would grant access before anyone decided to, which is exactly
    the mistake a licence layer exists to prevent.
    """
    code = payload.code.strip().lower()
    if not _CODE.match(code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Le code doit faire 3 à 40 caractères, en minuscules, chiffres et tirets (exemple : paroisse-saint-jean).",
        )
    if db.fetch_one("SELECT id FROM organisation_cliente WHERE code = %s", (code,), role=user.role):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Le code {code} est déjà pris.")
    cree = db.execute(
        "INSERT INTO organisation_cliente (code, nom, pays, ville, contact_nom, contact_email, contact_telephone, note, etat) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'evaluation') RETURNING id",
        (code, payload.nom.strip(), payload.pays.strip() or None, payload.ville.strip() or None,
         payload.contact_nom.strip() or None, str(payload.contact_email) if payload.contact_email else None,
         payload.contact_telephone.strip() or None, payload.note.strip() or None),
        role=user.role,
    )
    if not cree:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Création impossible.")
    audit.log(user.id, user.role, "creer_organisation_cliente", "organisation_cliente", str(cree["id"]), {"code": code})
    return {"ok": True, "id": str(cree["id"]), "code": code}


class EtatIn(BaseModel):
    etat: str
    #: Required to suspend. The database refuses a suspension without one.
    motif: str = Field(default="", max_length=500)


@router.patch("/{organisation_id}/etat")
def changer_etat(
    organisation_id: str,
    payload: EtatIn,
    user: Annotated[UserMe, Depends(require_permission("support.traiter"))],
) -> dict[str, object]:
    """Activate, suspend, or terminate.

    Suspending demands a reason, and it is the reason a client will be told. Writing
    "impayé" here is what makes the lock-out explicable when they call.
    """
    if payload.etat not in ETATS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"état inconnu : {payload.etat}")
    org = db.fetch_one("SELECT id, etat, nom FROM organisation_cliente WHERE id = %s", (organisation_id,), role=user.role)
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation introuvable.")

    motif = payload.motif.strip()
    if payload.etat == "suspendue" and not motif:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Une suspension doit indiquer son motif : c'est ce que le client s'entendra répondre.",
        )
    db.execute(
        "UPDATE organisation_cliente SET etat = %s, "
        "  suspendue_motif = CASE WHEN %s = 'suspendue' THEN %s ELSE NULL END, "
        "  suspendue_par = CASE WHEN %s = 'suspendue' THEN %s::uuid ELSE NULL END, "
        "  suspendue_le = CASE WHEN %s = 'suspendue' THEN now() ELSE NULL END, "
        "  maj_le = now() WHERE id = %s",
        (payload.etat, payload.etat, motif or None, payload.etat, user.id, payload.etat, organisation_id),
        role=user.role,
    )
    audit.log(
        user.id, user.role, "changer_etat_organisation", "organisation_cliente", organisation_id,
        {"avant": org["etat"], "apres": payload.etat, "motif": motif},
    )
    return {"ok": True, "etat": payload.etat}


class LicenceIn(BaseModel):
    formule: str = Field(min_length=2, max_length=60)
    membres_inclus: int | None = Field(default=None, gt=0, le=1_000_000)
    debut: date
    fin: date | None = None
    gracieuse: bool = False
    motif: str = Field(default="", max_length=500)


@router.post("/{organisation_id}/licences", status_code=status.HTTP_201_CREATED)
def accorder_licence(
    organisation_id: str,
    payload: LicenceIn,
    user: Annotated[UserMe, Depends(require_permission("support.traiter"))],
) -> dict[str, object]:
    """Grant a licence, superseding the one in force.

    Both statements are one transaction at the database level, and the partial unique
    index is the real guarantee: if two grants race, one of them fails rather than both
    landing and leaving the organisation with two entitlements.
    """
    org = db.fetch_one("SELECT id FROM organisation_cliente WHERE id = %s", (organisation_id,), role=user.role)
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation introuvable.")
    if payload.fin and payload.fin < payload.debut:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="La fin d'une licence ne peut précéder son début.")
    if payload.gracieuse and not payload.motif.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Une licence gracieuse doit dire pourquoi elle l'est : sans motif, personne ne saura s'il faut la renouveler.",
        )

    db.execute(
        "UPDATE licence SET remplacee_le = now() WHERE organisation_id = %s AND remplacee_le IS NULL",
        (organisation_id,),
        role=user.role,
    )
    cree = db.execute(
        "INSERT INTO licence (organisation_id, formule, membres_inclus, debut, fin, gracieuse, motif, accordee_par) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (organisation_id, payload.formule.strip(), payload.membres_inclus, payload.debut, payload.fin,
         payload.gracieuse, payload.motif.strip() or None, user.id),
        role=user.role,
    )
    if not cree:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Attribution impossible.")
    audit.log(
        user.id, user.role, "accorder_licence", "licence", str(cree["id"]),
        {"organisation": organisation_id, "formule": payload.formule.strip(), "gracieuse": payload.gracieuse},
    )
    return {"ok": True, "id": str(cree["id"])}


@router.get("/{organisation_id}/licences")
def historique_licences(
    organisation_id: str,
    user: Annotated[UserMe, Depends(require_permission("support.traiter"))],
) -> list[dict[str, object]]:
    """Every licence ever granted, newest first. Superseded ones are kept on purpose."""
    return [
        _licence_out(dict(r))
        for r in db.fetch_all(
            "SELECT * FROM licence WHERE organisation_id = %s ORDER BY cree_le DESC",
            (organisation_id,),
            role=user.role,
        )
    ]
