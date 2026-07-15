"""Admin registration lists: one queue per lifecycle stage, plus stage counts.

Split out of app.inscription to keep files focused and under the size threshold.
Everyone in the registration flow is surfaced at their exact stage, so the account
manager never sifts a mixed queue.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr

from . import db
from .membre_filters import exclusion_technique
from .permissions_rbac import require_permission
from .schemas import UserMe

router = APIRouter(prefix="/api/v1", tags=["inscription"])


class CompteMembreIn(BaseModel):
    email: EmailStr
    prenoms: str | None = None
    nom: str | None = None


@router.post("/admin/inscriptions/membre", status_code=status.HTTP_201_CREATED)
def creer_compte_membre(
    payload: CompteMembreIn,
    user: Annotated[UserMe, Depends(require_permission("inscriptions.administrer"))],
) -> dict[str, object]:
    from .inscription import creer_membre_inscription

    return creer_membre_inscription(str(payload.email), payload.prenoms, payload.nom, user)

# Registration lifecycle filters, so the admin sees each stage in its own list and
# never has to sift a mixed queue. Default keeps the historical 'to validate' set.
_FILTRES_INSCRIPTION: dict[str, tuple[str, ...]] = {
    "en_cours": ("incomplet",),
    "recus": ("soumis",),
    "a_valider": ("soumis", "en_revue", "modification_demandee"),
    "validees": ("approuve",),
    "refusees": ("refuse",),
    "toutes": ("incomplet", "soumis", "en_revue", "modification_demandee", "approuve", "refuse"),
}

_LIFECYCLE_LABEL: dict[str, str] = {
    "incomplet": "Compte créé, en attente du formulaire",
    "soumis": "Formulaire reçu, à examiner",
    "en_revue": "En cours d'examen",
    "modification_demandee": "Correction demandée",
    "approuve": "Validée",
    "refuse": "Refusée",
}


@router.get("/admin/inscriptions")
def list_inscriptions(
    user: Annotated[UserMe, Depends(require_permission("inscriptions.gerer"))],
    filtre: str = "a_valider",
) -> list[dict[str, object]]:
    """Registrations at a given lifecycle stage. ``filtre`` selects the stage
    (en_cours, recus, a_valider, validees, refusees, toutes); the default keeps the
    historical 'to validate' queue so existing callers are unaffected."""
    statuts = _FILTRES_INSCRIPTION.get(filtre, _FILTRES_INSCRIPTION["a_valider"])
    placeholders = ", ".join(["%s"] * len(statuts))
    rows = db.fetch_all(
        "SELECT id, matricule, prenoms, nom, email, statut_inscription, soumis_le, decision_le, "
        "(SELECT count(*) FROM document d WHERE d.membre_id = membre.id) AS nb_documents "
        f"FROM membre WHERE statut_inscription IN ({placeholders}) AND {exclusion_technique('membre')} "
        "ORDER BY COALESCE(soumis_le, decision_le) DESC NULLS LAST, matricule DESC",
        tuple(statuts),
        role=user.role,
    )
    return [
        {
            "id": str(r["id"]),
            "matricule": r["matricule"],
            "nom": f"{r['prenoms'] or ''} {r['nom'] or ''}".strip(),
            "email": r["email"],
            "statut": r["statut_inscription"],
            "statut_libelle": _LIFECYCLE_LABEL.get(str(r["statut_inscription"]), str(r["statut_inscription"])),
            "soumis_le": r["soumis_le"].isoformat() if r["soumis_le"] else None,
            "decision_le": r["decision_le"].isoformat() if r.get("decision_le") else None,
            "nb_documents": int(r["nb_documents"]),
        }
        for r in rows
    ]


class MembreLotItem(BaseModel):
    email: EmailStr
    prenoms: str | None = None
    nom: str | None = None


class MembresLotIn(BaseModel):
    membres: list[MembreLotItem]


@router.post("/admin/inscriptions/membres-lot")
def creer_membres_lot(
    payload: MembresLotIn,
    user: Annotated[UserMe, Depends(require_permission("inscriptions.administrer"))],
) -> dict[str, object]:
    """Create several member registrations at once, each getting its access.

    Duplicate e-mails (already registered, or repeated in the batch) are skipped and
    reported, never a hard failure, so one bad row does not abort the whole import.
    Every created member starts at the 'incomplet' stage like the single creation.
    """
    from .inscription import creer_membre_inscription

    crees: list[dict[str, str]] = []
    doublons: list[str] = []
    erreurs: list[dict[str, str]] = []
    vus: set[str] = set()
    for m in payload.membres:
        email = str(m.email).strip().lower()
        if not email or email in vus:
            doublons.append(email)
            continue
        vus.add(email)
        try:
            r = creer_membre_inscription(email, (m.prenoms or "").strip() or None, (m.nom or "").strip() or None, user)
            crees.append({"email": email, "matricule": str(r["matricule"])})
        except HTTPException as exc:
            if exc.status_code == 409:
                doublons.append(email)
            else:
                erreurs.append({"email": email, "raison": str(exc.detail)})
    return {"crees": len(crees), "details_crees": crees, "doublons": doublons, "erreurs": erreurs}


@router.get("/admin/inscriptions/compteurs")
def compteurs_inscriptions(
    user: Annotated[UserMe, Depends(require_permission("inscriptions.gerer"))],
) -> dict[str, int]:
    """One count per lifecycle stage, to badge the registration tabs at a glance."""
    rows = db.fetch_all(
        "SELECT statut_inscription AS s, count(*) AS n FROM membre "
        f"WHERE statut_inscription IS NOT NULL AND {exclusion_technique('membre')} GROUP BY statut_inscription",
        (),
        role=user.role,
    )
    par_statut = {str(r["s"]): int(r["n"]) for r in rows}
    return {
        filtre: sum(par_statut.get(s, 0) for s in statuts)
        for filtre, statuts in _FILTRES_INSCRIPTION.items()
    }
