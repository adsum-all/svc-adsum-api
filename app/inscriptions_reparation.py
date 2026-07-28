"""Find the registrations that went nowhere, and say why.

Somebody registered three weeks ago, never received anything they could act on, and
nothing in the interface showed it: the journal said the provider had accepted the
message, which is not the same as somebody receiving one. Meanwhile the same person
was being asked to confirm their attendance at activities.

This module answers, for each pending registration, the only question that matters:
what is stopping this person from getting in, and what can be done about it right
now. The diagnosis is deliberately expressed as a sentence an administrator can act
on, not a status code.
"""
# ruff: noqa: E501
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from . import audit, db
from .permissions_rbac import require_permission, require_permission_ecriture
from .schemas import UserMe

router = APIRouter(prefix="/api/v1/admin", tags=["inscription"])

# What the outbox says about the last invitation, turned into something readable.
_ETAT_ENVOI = {
    "envoye": "Invitation envoyée, en attente de livraison",
    "delivre": "Invitation livrée",
    "ouvert": "Invitation ouverte par le membre",
    "rebondi": "L'adresse a rejeté le message",
    "rejete": "Message refusé par le fournisseur",
    "echoue": "L'envoi a échoué",
    "en_cours": "Envoi en cours",
    "prepare": "Invitation préparée, pas encore partie",
}


def _diagnostic(r: dict[str, Any]) -> dict[str, Any]:
    """What blocks this registration, and what to do next.

    Ordered from the most blocking cause to the least: an address that cannot receive
    anything makes every other consideration moot.
    """
    email = str(r.get("email") or "").strip()
    statut_envoi = r.get("statut_envoi")
    connecte = r.get("dernier_login") is not None
    inscription = str(r.get("statut_inscription") or "")

    if not email or "@" not in email:
        return {"gravite": "bloquant", "cause": "adresse absente ou invalide",
                "action": "Corriger l'adresse, puis renvoyer l'invitation."}
    if statut_envoi in ("rebondi", "rejete"):
        return {"gravite": "bloquant", "cause": "l'adresse rejette les messages",
                "action": "Vérifier l'adresse auprès du membre, la corriger, puis renvoyer."}
    if statut_envoi == "echoue":
        return {"gravite": "bloquant", "cause": "l'envoi a échoué chez le fournisseur",
                "action": "Renvoyer l'invitation."}
    if not r.get("compte_id"):
        return {"gravite": "bloquant", "cause": "aucun compte de connexion",
                "action": "Renvoyer l'invitation, ce qui crée l'accès."}
    # Someone already logged in got in, whatever the ledger holds. Reporting a missing
    # trace as blocking for them would send an administrator chasing a problem that is
    # not there, and bury the ones that are.
    if statut_envoi is None and not connecte:
        return {"gravite": "bloquant", "cause": "aucune invitation tracée pour ce membre",
                "action": "Renvoyer l'invitation pour repartir sur une trace fiable."}
    if not connecte:
        return {"gravite": "attention", "cause": "invitation partie, jamais utilisée",
                "action": "Relancer le membre, ou renvoyer un nouvel accès si le délai est dépassé."}
    if inscription == "incomplet":
        return {"gravite": "attention", "cause": "connecté, mais dossier non complété",
                "action": "Relancer le membre pour qu'il termine son dossier."}
    if inscription in ("soumis", "en_revue"):
        return {"gravite": "information", "cause": "dossier soumis, en attente de revue",
                "action": "Traiter le dossier dans la revue des inscriptions."}
    if inscription == "modification_demandee":
        return {"gravite": "attention", "cause": "modification demandée, sans retour du membre",
                "action": "Relancer le membre sur la correction attendue."}
    return {"gravite": "information", "cause": "en cours", "action": "Suivre l'avancement."}


@router.get("/inscriptions/a-reparer")
def inscriptions_a_reparer(
    user: Annotated[UserMe, Depends(require_permission("inscriptions.gerer"))],
    page: int = Query(default=1, ge=1),
    taille: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    """Registrations that have not reached a usable account, with the reason for each."""
    where = "m.statut_inscription <> 'approuve' AND m.statut = 'actif'"
    colonnes = (
        "SELECT m.id, m.matricule, m.email, m.prenoms, m.nom, m.statut_inscription, m.verifie, m.cree_le, "
        "u.id AS compte_id, u.dernier_login, u.mdp_temporaire, u.mdp_expire_le, "
        "(SELECT o.statut FROM email_outbox o WHERE o.membre_id = m.id "
        "  ORDER BY o.cree_le DESC LIMIT 1) AS statut_envoi, "
        "(SELECT o.cree_le FROM email_outbox o WHERE o.membre_id = m.id "
        "  ORDER BY o.cree_le DESC LIMIT 1) AS dernier_envoi_le "
        f"FROM membre m LEFT JOIN utilisateur u ON u.membre_id = m.id WHERE {where} "
    )
    total = int((db.fetch_one(f"SELECT COUNT(*) AS n FROM membre m WHERE {where}", (), role=user.role) or {}).get("n") or 0)
    rows = db.fetch_all(
        colonnes + "ORDER BY m.cree_le DESC LIMIT %s OFFSET %s",
        (taille, (page - 1) * taille), role=user.role,
    )
    # The summary counts the whole set, never the page. A badge reading "5 to chase"
    # above a list of 10 is worse than no badge: it says the problem is half its real
    # size, and an administrator stops at the first page believing they are done.
    # The diagnosis is Python logic, so the rows are re-read under a hard ceiling;
    # ``resume_complet`` says plainly when that ceiling truncated the count.
    _PLAFOND = 1000
    toutes = db.fetch_all(colonnes + "ORDER BY m.cree_le DESC LIMIT %s", (_PLAFOND,), role=user.role)
    par_gravite: dict[str, int] = {}
    for r in toutes:
        g = _diagnostic(r)["gravite"]
        par_gravite[g] = par_gravite.get(g, 0) + 1
    items = []
    for r in rows:
        diag = _diagnostic(r)
        items.append({
            "membre_id": str(r["id"]),
            "matricule": r.get("matricule"),
            "email": r.get("email"),
            "nom": f"{r.get('prenoms') or ''} {r.get('nom') or ''}".strip() or None,
            "statut_inscription": r.get("statut_inscription"),
            "inscrit_le": r.get("cree_le"),
            "compte": bool(r.get("compte_id")),
            "connecte_au_moins_une_fois": r.get("dernier_login") is not None,
            "etat_envoi": _ETAT_ENVOI.get(str(r.get("statut_envoi")), "Aucune invitation tracée"),
            "dernier_envoi_le": r.get("dernier_envoi_le"),
            **diag,
        })
    return {"items": items, "total": total, "page": page, "taille": taille,
            "pages": max(1, (total + taille - 1) // taille), "resume": par_gravite,
            "resume_complet": len(toutes) < _PLAFOND}


@router.get("/membres/{membre_id}/envois-email")
def envois_email_membre(
    membre_id: str,
    user: Annotated[UserMe, Depends(require_permission("inscriptions.gerer"))],
) -> dict[str, Any]:
    """Every message the platform tried to send this member, and what became of it."""
    rows = db.fetch_all(
        "SELECT id, destinataire, template_code, sujet, statut, fournisseur, erreur, tentatives, "
        "cree_le, envoye_le, delivre_le, ouvert_le, echoue_le "
        "FROM email_outbox WHERE membre_id = %s ORDER BY cree_le DESC LIMIT 50",
        (membre_id,), role=user.role,
    )
    envois = []
    for r in rows:
        evenements = db.fetch_all(
            "SELECT evenement_fournisseur, statut_normalise, motif, survenu_le "
            "FROM email_delivery_event WHERE email_outbox_id = %s ORDER BY survenu_le DESC LIMIT 10",
            (str(r["id"]),), role=user.role,
        )
        envois.append({
            "id": str(r["id"]),
            "destinataire": r.get("destinataire"),
            "template": r.get("template_code"),
            "sujet": r.get("sujet"),
            "statut": r.get("statut"),
            "etat_lisible": _ETAT_ENVOI.get(str(r.get("statut")), str(r.get("statut"))),
            "fournisseur": r.get("fournisseur"),
            "erreur": r.get("erreur"),
            "tentatives": r.get("tentatives"),
            "cree_le": r.get("cree_le"),
            "envoye_le": r.get("envoye_le"),
            "delivre_le": r.get("delivre_le"),
            "ouvert_le": r.get("ouvert_le"),
            "echoue_le": r.get("echoue_le"),
            "evenements": [dict(e) for e in evenements],
        })
    return {"membre_id": membre_id, "envois": envois, "total": len(envois)}


@router.post("/inscriptions/reparer-en-masse")
def reparer_en_masse(
    user: Annotated[UserMe, Depends(require_permission_ecriture("inscriptions.administrer"))],
    membre_ids: list[str],
    apercu: bool = Query(default=True),
) -> dict[str, Any]:
    """Resend the access to several stuck registrations at once.

    Defaults to a preview: a mass send is never fired from a single click without the
    caller seeing who it will reach. The preview returns exactly the same selection the
    real run would use, so what is confirmed is what was shown.
    """
    if not membre_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="aucun membre sélectionné")
    if len(membre_ids) > 200:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="200 membres au maximum par lot")

    rows = db.fetch_all(
        "SELECT m.id, m.matricule, m.email, m.prenoms, m.nom, m.statut_inscription "
        "FROM membre m WHERE m.id = ANY(%s::uuid[]) AND m.statut = 'actif'",
        (membre_ids,), role=user.role,
    )
    destinataires = [r for r in rows if str(r.get("email") or "").strip() and "@" in str(r.get("email"))]
    ecartes = [{"membre_id": str(r["id"]), "motif": "adresse absente ou invalide"}
               for r in rows if r not in destinataires]

    if apercu:
        return {
            "apercu": True,
            "destinataires": [{"membre_id": str(r["id"]), "matricule": r.get("matricule"),
                               "email": r.get("email"),
                               "nom": f"{r.get('prenoms') or ''} {r.get('nom') or ''}".strip() or None}
                              for r in destinataires],
            "ecartes": ecartes,
            "total": len(destinataires),
        }

    from .inscription import relancer_mdp

    resultats = []
    for r in destinataires:
        try:
            res = relancer_mdp(str(r["id"]), user)
            resultats.append({"membre_id": str(r["id"]), "email": r.get("email"),
                              "envoye": bool(res.get("email_envoye")), "canal": res.get("canal_email")})
        except HTTPException as exc:
            resultats.append({"membre_id": str(r["id"]), "email": r.get("email"),
                              "envoye": False, "erreur": exc.detail})
    envoyes = sum(1 for x in resultats if x.get("envoye"))
    audit.log(user.id, user.role, "reparation_inscriptions_masse", "membre", None,
              {"demandes": len(membre_ids), "traites": len(resultats), "envoyes": envoyes,
               "ecartes": len(ecartes)})
    return {"apercu": False, "traites": len(resultats), "envoyes": envoyes,
            "ecartes": ecartes, "resultats": resultats}
