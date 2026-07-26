"""Who may receive what, decided in one place.

A member record is created with ``statut = 'actif'`` the moment an administrator
registers someone, long before that person has completed their file, had it
validated, or even opened the invitation. Every mass send filtered on that single
column, so a brand-new registrant landed in the audience of attendance surveys,
birthday digests and weekly recaps while still waiting to be able to log in. People
were being asked to confirm their presence at activities before their account
existed for them.

The rule this module enforces: being registered is not being active. Onboarding
messages reach someone who is merely registered; everything operational waits until
the account can actually be used.

The decision lives here and nowhere else. Copying the condition into each screen is
exactly how the two definitions drifted apart in the first place.
"""
from __future__ import annotations

from typing import Any

from . import db

# Notification kinds that belong to onboarding: they are the ONLY ones a member may
# receive before their account is usable, because their whole purpose is to get them
# there. Everything else is operational and waits.
TYPES_ONBOARDING = frozenset({
    "compte_cree",
    "inscription_recue",
    "inscription_validee",
    "inscription_refusee",
    "inscription_modification",
    "dossier_incomplet",
    "relance_inscription",
    "mot_de_passe_temporaire",
    "reinitialisation_mot_de_passe",
    "code_verification",
    "engagement_signature",
    "telegram_liaison",
})

# Kinds that carry a security purpose: never withheld, whatever the state of the
# account. Refusing a password reset to someone locked out would be absurd.
TYPES_SECURITE = frozenset({
    "code_verification",
    "reinitialisation_mot_de_passe",
    "connexion_inhabituelle",
    "mot_de_passe_change",
})

# SQL predicate over an aliased ``membre`` row, for recipient-selection queries that
# work in bulk. Kept in step with :func:`etat_membre` below, which decides the same
# thing for a single member.
def predicat_operationnel(alias: str = "m") -> str:
    """A member who may receive operational messages, as a SQL condition.

    The two columns are the schema's own vocabulary: ``statut`` is one of actif,
    inactif, suspendu, archive, and ``statut_inscription`` one of incomplet, soumis,
    en_revue, approuve, refuse, modification_demandee. Only ``approuve`` means the
    file was validated, and ``verifie`` records the identity check. Both are required:
    on this base every approved member is also verified, so demanding both excludes
    nobody who is genuinely in, while refusing everyone who is merely registered.

    Deliberately expressed on the member alone so it can be dropped into any audience
    query without forcing a join: the account state is checked separately by
    :func:`etat_membre` when a single recipient is evaluated.
    """
    return (
        f"{alias}.statut = 'actif' "
        f"AND coalesce({alias}.verifie, false) = true "
        f"AND {alias}.statut_inscription = 'approuve'"
    )


def etat_membre(membre_id: str, role: str | None = None) -> dict[str, Any]:
    """The full eligibility picture of one member, with the reason when refused.

    Returns the member state, the account state, whether operational messages are
    allowed, and a readable reason. The reason matters as much as the verdict: a
    blocked notification that says nothing is indistinguishable from a bug, which is
    how this problem stayed invisible for weeks.
    """
    row = db.fetch_one(
        "SELECT m.id, m.statut, m.verifie, m.statut_inscription, m.email, "
        "u.id AS compte_id, u.actif AS compte_actif, u.mdp_temporaire, u.dernier_login "
        "FROM membre m LEFT JOIN utilisateur u ON u.membre_id = m.id WHERE m.id = %s",
        (membre_id,), role=role,
    )
    if not row:
        return {"operationnel": False, "raison": "membre inconnu", "code": "membre_inconnu"}

    statut = str(row.get("statut") or "")
    inscription = str(row.get("statut_inscription") or "")
    verifie = bool(row.get("verifie"))
    compte_actif = bool(row.get("compte_actif"))
    jamais_connecte = row.get("dernier_login") is None

    if statut in ("suspendu", "desactive", "archive"):
        return {"operationnel": False, "raison": f"fiche membre {statut}", "code": "membre_" + statut}
    if statut != "actif":
        libelle = f"fiche membre au statut {statut or 'inconnu'}"
        return {"operationnel": False, "raison": libelle, "code": "membre_inactif"}
    # A validated member without a login account is still a member of the organisation:
    # they are told about the activities they belong to. Only an account that exists and
    # was deliberately disabled cuts the operational messages.
    if row.get("compte_id") and not compte_actif:
        return {"operationnel": False, "raison": "compte désactivé", "code": "compte_inactif"}
    if inscription != "approuve":
        libelles = {
            "incomplet": "dossier d'inscription incomplet",
            "soumis": "dossier soumis, en attente de revue",
            "en_revue": "dossier en cours de revue",
            "refuse": "dossier refusé",
            "modification_demandee": "modification demandée sur le dossier",
        }
        return {"operationnel": False,
                "raison": libelles.get(inscription, f"inscription au statut {inscription or 'inconnu'}"),
                "code": "inscription_" + (inscription or "inconnu")}
    if not verifie:
        return {"operationnel": False, "raison": "identité non vérifiée", "code": "identite_non_verifiee"}
    if jamais_connecte and bool(row.get("mdp_temporaire")):
        return {"operationnel": False, "raison": "invitation jamais utilisée", "code": "invitation_non_consommee"}
    return {"operationnel": True, "raison": "compte actif et dossier approuvé", "code": "actif"}


def autorise(membre_id: str, type_cle: str, role: str | None = None) -> tuple[bool, str]:
    """May this member receive this kind of notification? Returns (autorisé, raison).

    Security and onboarding messages pass whatever the state, because withholding
    them is what would leave someone stuck. Everything operational waits for an
    account that can actually be used.
    """
    if type_cle in TYPES_SECURITE or type_cle in TYPES_ONBOARDING:
        return True, "message d'accueil ou de sécurité, toujours autorisé"
    etat = etat_membre(membre_id, role)
    if etat["operationnel"]:
        return True, etat["raison"]
    return False, etat["raison"]
