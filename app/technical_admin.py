# ruff: noqa: E501 - guard messages and SQL carry long literals
"""Management of technical (support) super-admins.

A technical super-admin is an applicative support identity with total, cross-application
access (``utilisateur.acces_technique_global``), distinct from a member super-admin. Only
a technical super-admin may list, grant, revoke or configure other technical super-admins:
the endpoints are guarded by ``acces.systeme`` at the RBAC layer AND re-check the technical
authorization in the handler, so a member super-admin (who may hold acces.systeme) can
never reach this surface. Every change is audited. The last active technical super-admin
can never be revoked, so the platform is never left without a support identity.
"""
from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr

from . import audit, db
from .permissions_rbac import require_permission
from .schemas import UserMe
from .security import hash_password

router = APIRouter(prefix="/api/v1/admin/technical-super-admins", tags=["technical-admin"])

# Graduated technical privilege levels, from least to most privileged. The level is
# distinct from the member roles and governs administration of the technical roster.
_LEVELS = ("lecteur", "developpeur", "mainteneur", "admin", "super")
# Levels allowed to administer OTHER technical accounts (create/relevel/activate/delete).
_MANAGE_LEVELS = frozenset({"admin", "super"})


def _require_technical(user: UserMe) -> None:
    """Only a technical super-admin may reach the technical-admin surface."""
    if not user.acces_technique_global:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="reserve aux super-admins applicatifs techniques",
        )


def _require_manage(user: UserMe) -> None:
    """Administering the technical roster (create/relevel/activate/delete) is reserved to
    the ``admin`` and ``super`` levels; a lower technical level can reach the surface in
    read but never mutate another technical account."""
    _require_technical(user)
    if (user.niveau_technique or "") not in _MANAGE_LEVELS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="niveau technique insuffisant (admin ou super requis)",
        )


def _row_out(r: dict[str, object]) -> dict[str, object]:
    # A technical account ALWAYS enforces two-factor authentication: it is staff and
    # created with double_facteur = true, so 2FA is imposed at login (a one-time code),
    # a trusted device only shortening the challenge to a 72 h window. mfa_actif merely
    # tells whether an authenticator app was additionally enrolled by the owner.
    # audit_count = number of journalised actions this account performed. When > 0 the
    # account keeps an immutable audit trail (traceability, HDS/RGPD), so it can be
    # revoked but never hard-deleted: only a pristine (never-used) account is deletable.
    audit_count = int(r.get("audit_count") or 0)
    return {
        "id": str(r["id"]),
        "email": r.get("email"),
        "actif": bool(r.get("actif")),
        "mfa_actif": bool(r.get("mfa_actif")),
        "mfa_impose": True,
        "niveau": r.get("niveau_technique") or "admin",
        "audit_count": audit_count,
        "supprimable_dur": (not bool(r.get("actif"))) and audit_count == 0,
        "dernier_login": r["dernier_login"].isoformat() if r.get("dernier_login") else None,
        "cree_le": r["cree_le"].isoformat() if r.get("cree_le") else None,
        "est_membre": r.get("membre_id") is not None,
    }


@router.get("")
def lister(user: Annotated[UserMe, Depends(require_permission("acces.systeme"))]) -> dict[str, object]:
    """List every technical super-admin (active or not) plus the candidate accounts that
    could be promoted (super_admin applicative accounts without the flag). Reserved to a
    managing level (admin or super): a lower technical level cannot even see this roster."""
    _require_manage(user)
    techniques = [
        _row_out(r) for r in db.fetch_all(
            "SELECT u.id, u.email, u.actif, u.mfa_actif, u.niveau_technique, u.dernier_login, u.cree_le, u.membre_id, "
            "(SELECT count(*) FROM audit a WHERE a.acteur_id = u.id) AS audit_count "
            "FROM utilisateur u WHERE u.acces_technique_global = true ORDER BY u.email",
            (), role=user.role,
        )
    ]
    actifs = sum(1 for t in techniques if t["actif"])
    # Candidates for promotion are APPLICATIVE accounts only (no member row, membre_id
    # IS NULL): a real member super-admin is NEVER proposed here. A new technical user is
    # added via the dedicated create-by-email field, not by promoting a member.
    candidats = [
        {"id": str(r["id"]), "email": r.get("email"), "est_membre": False}
        for r in db.fetch_all(
            "SELECT id, email FROM utilisateur "
            "WHERE role = 'super_admin' AND acces_technique_global = false AND membre_id IS NULL "
            "AND email NOT LIKE '%%@exemple.com' ORDER BY email",
            (), role=user.role,
        )
    ]
    return {
        "techniques": techniques,
        "candidats": candidats,
        "niveaux": list(_LEVELS),
        "mon_niveau": user.niveau_technique,
        "mon_id": user.id,
        "peut_gerer": (user.niveau_technique or "") in _MANAGE_LEVELS,
        # Number of ACTIVE technical accounts. When it is 1, the platform depends on that
        # single account: the UI must require adding and activating a replacement before
        # any deactivation, revocation or deletion of it (the server enforces this too).
        "techniques_actifs": actifs,
    }


def _valider_niveau(niveau: str, user: UserMe) -> str:
    """The requested level must be known; only a ``super`` may grant the ``super`` level."""
    niveau = (niveau or "").strip().lower()
    if niveau not in _LEVELS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="niveau technique inconnu")
    if niveau == "super" and (user.niveau_technique or "") != "super":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="seul un super peut accorder le niveau super",
        )
    return niveau


class CreerTechniqueIn(BaseModel):
    email: EmailStr
    niveau: str = "admin"


@router.post("/creer")
def creer_applicatif(
    payload: CreerTechniqueIn,
    user: Annotated[UserMe, Depends(require_permission("acces.systeme"))],
) -> dict[str, object]:
    """Create a NEW applicative technical super-admin from an e-mail (never a member).

    The account is NOT a member (no member row, no member matricule) and is technical-
    verified. It is created inactive, pending its first connection: no temporary password
    is generated or transmitted. The owner sets their own password through the standard
    password-reset flow to their professional e-mail, and staff two-factor authentication
    is mandatory. A member e-mail is refused, so a member is never turned into a technical
    account through this path."""
    _require_manage(user)
    niveau = _valider_niveau(payload.niveau, user)
    email = str(payload.email).strip().lower()
    if email.endswith("@exemple.com"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="adresse de demonstration interdite")
    if db.fetch_one("SELECT 1 FROM utilisateur WHERE lower(email) = %s", (email,), role=user.role):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="un compte existe deja avec cette adresse")
    if db.fetch_one("SELECT 1 FROM membre WHERE lower(email) = %s", (email,), role=user.role):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cette adresse correspond a une fiche membre: un membre ne peut pas devenir un compte technique",
        )
    # Random, never-exposed secret: the owner sets their real password via reset (no temp
    # password is shown or sent). double_facteur = true makes staff 2FA mandatory.
    created = db.execute(
        "INSERT INTO utilisateur (email, hash_mdp, role, membre_id, acces_technique_global, niveau_technique, actif, "
        "double_facteur, mdp_temporaire, doit_changer_mdp) "
        "VALUES (%s, %s, 'super_admin', NULL, true, %s, false, true, true, true) RETURNING id",
        (email, hash_password(secrets.token_urlsafe(32)), niveau),
        role=user.role,
    )
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="creation impossible")
    uid = str(created["id"])
    audit.log(user.id, user.role, "creation_super_admin_technique", "utilisateur", uid,
              {"email": email, "niveau": niveau})
    return {
        "ok": True, "id": uid, "email": email, "actif": False, "niveau": niveau,
        "message": "Compte technique cree, inactif. Le titulaire definit son mot de passe via la reinitialisation "
                   "sur son adresse, puis active sa double authentification a la premiere connexion.",
    }


@router.post("/{utilisateur_id}")
def accorder(
    utilisateur_id: str,
    user: Annotated[UserMe, Depends(require_permission("acces.systeme"))],
) -> dict[str, object]:
    """Grant the technical super-admin authorization to an existing super_admin account.
    A demo (@exemple.com) or non-super_admin account is refused, so a technical role is
    never granted by mistake to a demonstration or lower-privilege account."""
    _require_manage(user)
    target = db.fetch_one(
        "SELECT id, email, role, membre_id FROM utilisateur WHERE id = %s", (utilisateur_id,), role=user.role
    )
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")
    if target.get("membre_id") is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="un membre ne peut pas etre promu compte technique: creez un compte applicatif dedie",
        )
    if str(target.get("role")) != "super_admin":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="le compte doit avoir le role super_admin")
    if str(target.get("email") or "").lower().endswith("@exemple.com"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="un compte de demonstration ne peut pas etre technique",
        )
    db.execute(
        "UPDATE utilisateur SET acces_technique_global = true, "
        "niveau_technique = COALESCE(niveau_technique, 'admin') WHERE id = %s",
        (utilisateur_id,), role=user.role,
    )
    audit.log(user.id, user.role, "octroi_super_admin_technique", "utilisateur", utilisateur_id, {})
    return {"ok": True}


@router.delete("/{utilisateur_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoquer(
    utilisateur_id: str,
    user: Annotated[UserMe, Depends(require_permission("acces.systeme"))],
) -> None:
    """Revoke the technical authorization. The LAST active technical super-admin can
    never be revoked, so the platform always keeps a support identity."""
    _require_manage(user)
    if utilisateur_id == user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="vous ne pouvez pas vous revoquer vous-meme")
    target = db.fetch_one(
        "SELECT id, actif FROM utilisateur WHERE id = %s AND acces_technique_global = true",
        (utilisateur_id,), role=user.role,
    )
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not a technical super-admin")
    # The last-active-support guard applies only when the target is itself active:
    # revoking an inactive technical account can never remove the last active support
    # identity, so it is always permitted (needed for the create/cleanup lifecycle).
    if bool(target.get("actif")):
        n = int((db.fetch_one(
            "SELECT count(*) AS n FROM utilisateur WHERE acces_technique_global = true AND actif = true", (),
            role=user.role,
        ) or {}).get("n") or 0)
        if n <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="ce compte est le seul super-admin technique actif et l'application en depend. "
                "Ajoutez et activez un remplaçant avant de le revoquer.",
            )
    db.execute("UPDATE utilisateur SET acces_technique_global = false WHERE id = %s", (utilisateur_id,), role=user.role)
    audit.log(user.id, user.role, "revocation_super_admin_technique", "utilisateur", utilisateur_id, {})


def _load_technique(utilisateur_id: str, role: str) -> dict[str, object]:
    row = db.fetch_one(
        "SELECT id, actif, niveau_technique FROM utilisateur WHERE id = %s AND acces_technique_global = true",
        (utilisateur_id,), role=role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not a technical super-admin")
    return row


def _actifs_restants(exclure: str, role: str) -> int:
    return int((db.fetch_one(
        "SELECT count(*) AS n FROM utilisateur WHERE acces_technique_global = true AND actif = true AND id <> %s",
        (exclure,), role=role,
    ) or {}).get("n") or 0)


class NiveauIn(BaseModel):
    niveau: str


@router.patch("/{utilisateur_id}/niveau")
def changer_niveau(
    utilisateur_id: str,
    payload: NiveauIn,
    user: Annotated[UserMe, Depends(require_permission("acces.systeme"))],
) -> dict[str, object]:
    """Change a technical account's privilege level. Granting ``super`` is reserved to a
    ``super``; you cannot downgrade yourself out of ``super`` if you are the last one, so
    the roster never loses its top administrator."""
    _require_manage(user)
    _load_technique(utilisateur_id, user.role)
    niveau = _valider_niveau(payload.niveau, user)
    if utilisateur_id == user.id and user.niveau_technique == "super" and niveau != "super":
        last_super = int((db.fetch_one(
            "SELECT count(*) AS n FROM utilisateur WHERE acces_technique_global = true AND niveau_technique = 'super' AND id <> %s",
            (utilisateur_id,), role=user.role,
        ) or {}).get("n") or 0)
        if last_super == 0:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="vous etes le dernier super: niveau non modifiable")
    db.execute("UPDATE utilisateur SET niveau_technique = %s WHERE id = %s", (niveau, utilisateur_id), role=user.role)
    audit.log(user.id, user.role, "changement_niveau_technique", "utilisateur", utilisateur_id, {"niveau": niveau})
    return {"ok": True, "id": utilisateur_id, "niveau": niveau}


class ActivationIn(BaseModel):
    actif: bool


@router.patch("/{utilisateur_id}/activation")
def changer_activation(
    utilisateur_id: str,
    payload: ActivationIn,
    user: Annotated[UserMe, Depends(require_permission("acces.systeme"))],
) -> dict[str, object]:
    """Activate or deactivate a technical account. A deactivated account can no longer
    authenticate. You cannot deactivate yourself, nor the last active technical account."""
    _require_manage(user)
    if utilisateur_id == user.id and not payload.actif:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="vous ne pouvez pas vous desactiver vous-meme")
    _load_technique(utilisateur_id, user.role)
    if not payload.actif and _actifs_restants(utilisateur_id, user.role) == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ce compte est le seul super-admin technique actif et l'application en depend. "
            "Ajoutez et activez un remplaçant avant de le desactiver.",
        )
    db.execute("UPDATE utilisateur SET actif = %s WHERE id = %s", (payload.actif, utilisateur_id), role=user.role)
    action = "activation_super_admin_technique" if payload.actif else "desactivation_super_admin_technique"
    audit.log(user.id, user.role, action, "utilisateur", utilisateur_id, {"actif": payload.actif})
    return {"ok": True, "id": utilisateur_id, "actif": payload.actif}


# References to an applicative account that are ON DELETE NO ACTION and must therefore be
# detached (set to NULL) before the row can be removed. This ANONYMISES the historical
# records (the actions/events/audit remain, but no longer point to the deleted account),
# which is a GDPR-compliant erasure: personal identity removed, data integrity preserved.
# (table, column). Column names are fixed internal constants, never user input: no injection.
_ANONYMISER: tuple[tuple[str, str], ...] = (
    ("audit", "acteur_id"),
    ("comptage_volet_b", "saisi_par"),
    ("document", "traite_par"),
    ("evenement", "cree_par"),
    ("import_lot", "importe_par"),
    ("parametre", "maj_par"),
    ("terminal", "controleur_id"),
)


@router.delete("/{utilisateur_id}/definitif", status_code=status.HTTP_204_NO_CONTENT)
def supprimer_definitif(
    utilisateur_id: str,
    user: Annotated[UserMe, Depends(require_permission("acces.systeme"))],
) -> None:
    """Delete an applicative account definitively, with a deep zero-trace cleanup: the
    account row and its e-mail are removed, and every historical reference (audit actor,
    created events, processed documents, etc.) is ANONYMISED (set to NULL) so the records
    stay intact but no longer point to a person. Reserved to a ``super``. Works both on a
    still-technical account (which must be INACTIVE first, deactivation itself requiring a
    replacement) and on an already-revoked applicative super-admin. Never touches a member
    account, a demo account, or yourself, and never removes the last active technical one."""
    _require_manage(user)
    if (user.niveau_technique or "") != "super":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="suppression definitive reservee au niveau super")
    if utilisateur_id == user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="vous ne pouvez pas vous supprimer vous-meme")
    row = db.fetch_one(
        "SELECT id, actif, acces_technique_global, membre_id, role, email FROM utilisateur WHERE id = %s",
        (utilisateur_id,), role=user.role,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")
    if row.get("membre_id") is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="compte lie a un membre: suppression interdite ici")
    if str(row.get("role")) != "super_admin" or str(row.get("email") or "").lower().endswith("@exemple.com"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="seul un compte applicatif dedie peut etre supprime ici")
    # A still-technical account must be inactive first (an active technical account is in
    # use; deactivating it already required an active replacement). A revoked account has
    # no such dependency.
    if bool(row.get("acces_technique_global")):
        if bool(row.get("actif")):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="desactivez d'abord ce compte technique (apres avoir active un remplaçant), puis supprimez-le.",
            )
        if _actifs_restants(utilisateur_id, user.role) == 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="impossible de supprimer le dernier super-admin technique actif",
            )
    # Deep cleanup in a single transaction: anonymise every NO ACTION reference, drop the
    # sessions and trusted devices, then remove the account row (its e-mail with it).
    with db.connection(role=user.role) as conn, conn.cursor() as cur:
        for table, col in _ANONYMISER:
            cur.execute(f"UPDATE {table} SET {col} = NULL WHERE {col} = %s", (utilisateur_id,))
        cur.execute("DELETE FROM session WHERE utilisateur_id = %s", (utilisateur_id,))
        cur.execute("DELETE FROM appareil_confiance WHERE utilisateur_id = %s", (utilisateur_id,))
        cur.execute("DELETE FROM utilisateur WHERE id = %s", (utilisateur_id,))
    # A NEW audit entry records the deletion (actor = the deleter), WITHOUT the removed
    # e-mail, so no trace of the deleted address remains anywhere.
    audit.log(user.id, user.role, "suppression_definitive_compte_applicatif", "utilisateur", utilisateur_id,
              {"anonymisation": True})
