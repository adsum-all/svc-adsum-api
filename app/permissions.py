"""Permission taxonomy and human-readable explanation of what each role grants.

The enforcement boundary in ADSUM is (role x perimeter): every endpoint is gated
by ``require_roles`` and, for scoped access, by the perimeter predicate. This
module makes that boundary EXPLAINABLE and REVIEWABLE for administrators: it maps
each platform role to the atomic capabilities it grants, with a French label, a
description, a risk level and a scope semantics, so an admin sees precisely what a
role can do, where, and how dangerous it is, before granting it.

It is documentation-grade truth about the model, kept in sync with the real
``require_roles`` usage; it is not a second enforcement path (there is one source
of enforcement, the endpoints). Deny-by-default: a capability not listed for a
role is not granted.
"""
# ruff: noqa: E501 - the catalogue carries long human-readable description lines
from __future__ import annotations

# risque: "faible" | "moyen" | "eleve" | "critique"
# portee: "self" | "scope" | "global" (what the capability reads/acts upon)
CAPABILITIES: dict[str, dict[str, str]] = {
    "self.read": {"libelle": "Voir son propre espace", "description": "Consulter son profil, sa carte, ses activités et ses notifications.", "risque": "faible", "portee": "self"},
    "participation.declare": {"libelle": "Déclarer sa présence", "description": "Répondre au questionnaire de présence de ses activités.", "risque": "faible", "portee": "self"},
    "consultation.answer": {"libelle": "Répondre aux sondages", "description": "Répondre aux consultations qui lui sont adressées.", "risque": "faible", "portee": "self"},
    "scan.read": {"libelle": "Scanner les présences", "description": "Scanner les QR des membres pour enregistrer les présences sur le terrain.", "risque": "moyen", "portee": "scope"},
    "presence.record": {"libelle": "Enregistrer une présence", "description": "Marquer un membre présent lors d'un contrôle.", "risque": "moyen", "portee": "scope"},
    "member.read.basic": {"libelle": "Voir les membres (fiche de base)", "description": "Consulter nom, matricule et statut des membres de son périmètre.", "risque": "moyen", "portee": "scope"},
    "member.read.sensitive": {"libelle": "Voir les données sensibles", "description": "Accéder aux coordonnées, à la date de naissance et aux informations personnelles.", "risque": "eleve", "portee": "scope"},
    "member.read.documents": {"libelle": "Voir les documents d'identité", "description": "Consulter les pièces jointes et documents d'un membre.", "risque": "eleve", "portee": "scope"},
    "member.create": {"libelle": "Créer / inscrire un membre", "description": "Enregistrer un nouveau membre et créer son dossier.", "risque": "moyen", "portee": "scope"},
    "member.update": {"libelle": "Modifier un dossier membre", "description": "Éditer les informations d'un membre de son périmètre.", "risque": "eleve", "portee": "scope"},
    "member.approve": {"libelle": "Valider les demandes", "description": "Approuver ou rejeter les inscriptions et modifications.", "risque": "eleve", "portee": "scope"},
    "member.block": {"libelle": "Bloquer / débloquer un compte", "description": "Suspendre ou réactiver l'accès d'un membre.", "risque": "eleve", "portee": "scope"},
    "member.export": {"libelle": "Exporter des membres", "description": "Extraire une liste de membres (CSV). Action tracée et bornée au périmètre.", "risque": "eleve", "portee": "scope"},
    "analytics.read": {"libelle": "Voir les statistiques", "description": "Consulter les tableaux de bord et indicateurs consolidés (lecture).", "risque": "moyen", "portee": "scope"},
    "activity.manage": {"libelle": "Gérer les activités", "description": "Créer et gérer les activités de son périmètre.", "risque": "moyen", "portee": "scope"},
    "consultation.manage": {"libelle": "Gérer les sondages", "description": "Créer, ouvrir, clore les consultations et voir les résultats.", "risque": "moyen", "portee": "scope"},
    "access.assign": {"libelle": "Attribuer des accès", "description": "Ajouter ou retirer des membres des groupes d'accès. Pouvoir sensible.", "risque": "critique", "portee": "global"},
    "group.manage": {"libelle": "Gérer les groupes et l'organisation", "description": "Gérer coordinations, intendances, commissions, tribus et groupes.", "risque": "eleve", "portee": "global"},
    "user.manage": {"libelle": "Gérer les comptes", "description": "Créer, activer, désactiver des comptes d'accès.", "risque": "eleve", "portee": "global"},
    "audit.read": {"libelle": "Voir le journal d'audit", "description": "Consulter la traçabilité des actions sensibles.", "risque": "moyen", "portee": "global"},
    "system.superadmin": {"libelle": "Administration système", "description": "Rotation des clés, paramètres critiques, tous pouvoirs. À réserver au strict minimum.", "risque": "critique", "portee": "global"},
}

# Each role's atomic capabilities, cumulative by seniority. Kept in sync with the
# real require_roles gates. A scoped grant applies these within the perimeter only.
ROLE_CAPS: dict[str, list[str]] = {
    "membre": ["self.read", "participation.declare", "consultation.answer"],
    "controleur": ["self.read", "scan.read", "presence.record"],
    "gestionnaire": [
        "self.read", "member.read.basic", "member.read.sensitive", "member.read.documents",
        "member.create", "member.update", "member.approve", "member.block", "member.export",
        "activity.manage", "consultation.manage",
    ],
    "direction": ["self.read", "member.read.basic", "analytics.read", "member.export", "activity.manage", "consultation.manage"],
    "admin": [
        "self.read", "member.read.basic", "member.read.sensitive", "member.read.documents",
        "member.create", "member.update", "member.approve", "member.block", "member.export",
        "analytics.read", "activity.manage", "consultation.manage",
        "access.assign", "group.manage", "user.manage", "audit.read",
    ],
    "super_admin": sorted(CAPABILITIES.keys()),
}

_RISK_ORDER = {"faible": 0, "moyen": 1, "eleve": 2, "critique": 3}

ROLE_LABELS = {
    "membre": "Membre", "controleur": "Contrôle / scan", "gestionnaire": "Gestion des membres",
    "direction": "Direction (lecture consolidée)", "admin": "Administration", "super_admin": "Super-administration",
}


def role_risque(role: str) -> str:
    """The highest risk level any of the role's capabilities carries."""
    caps = ROLE_CAPS.get(role, [])
    if not caps:
        return "faible"
    return max((CAPABILITIES[c]["risque"] for c in caps), key=lambda r: _RISK_ORDER.get(r, 0))


def expliquer_role(role: str) -> dict[str, object]:
    """A reviewable description of what a role grants, for the admin UI."""
    caps = ROLE_CAPS.get(role, [])
    return {
        "role": role,
        "libelle": ROLE_LABELS.get(role, role),
        "risque": role_risque(role),
        "capabilities": [{"cle": c, **CAPABILITIES[c]} for c in caps],
    }


def catalogue() -> list[dict[str, object]]:
    """The full role -> capabilities catalogue, ordered by seniority."""
    ordre = ["membre", "controleur", "gestionnaire", "direction", "admin", "super_admin"]
    return [expliquer_role(r) for r in ordre]
