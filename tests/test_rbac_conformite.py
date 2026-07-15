"""Conformity test (O4): the permission mapping reproduces the legacy roles.

For every role-gated endpoint, the roles holding its mapped permission MUST equal
the roles the endpoint admitted under require_roles (frozen real matrix, Phase A).
Any divergence fails the build: proves iso-behaviour and catches a lost gate.
"""
# ruff: noqa: E501
from __future__ import annotations

from app.permissions_data import CATALOGUE, ENDPOINT_PERMISSION, ROLE_PERMISSIONS

ROLES = ["membre", "controleur", "gestionnaire", "direction", "admin", "super_admin"]

REAL_ROLESET: dict[str, list[str]] = {
    'GET /api/v1/admin/ai/catalogue': ['admin', 'super_admin'],
    'GET /api/v1/admin/ai/providers': ['admin', 'super_admin'],
    'POST /api/v1/admin/ai/providers': ['admin', 'super_admin'],
    'PATCH /api/v1/admin/ai/providers/{provider_id}': ['admin', 'super_admin'],
    'POST /api/v1/admin/ai/providers/{provider_id}/activer': ['admin', 'super_admin'],
    'DELETE /api/v1/admin/ai/providers/{provider_id}': ['admin', 'super_admin'],
    'POST /api/v1/admin/ai/providers/{provider_id}/test': ['admin', 'super_admin'],
    'GET /api/v1/collaboration/canal': ['admin', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/collaboration/canal/compteur': ['admin', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/collaboration/modeles-catalogue': ['admin', 'direction', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/canal': ['admin', 'gestionnaire', 'super_admin'],
    'PATCH /api/v1/collaboration/canal/{message_id}': ['admin', 'gestionnaire', 'super_admin'],
    'DELETE /api/v1/collaboration/canal/{message_id}': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/canal/{message_id}/pieces': ['admin', 'gestionnaire', 'super_admin'],
    'DELETE /api/v1/collaboration/canal/pieces/{piece_id}': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/canal/{message_id}/notes': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/canal/notes/{note_id}/transcrire': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/canal/notes/{note_id}/rediger': ['admin', 'gestionnaire', 'super_admin'],
    'PATCH /api/v1/collaboration/canal/notes/{note_id}': ['admin', 'gestionnaire', 'super_admin'],
    'DELETE /api/v1/collaboration/canal/notes/{note_id}': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/canal/{message_id}/reponses': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/canal/{message_id}/creer-carte': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/canal/telegram/importer': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/canal/{message_id}/prise-en-charge': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/canal/{message_id}/affecter': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/canal/{message_id}/charger-tableau': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/canal/{message_id}/restituer': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/canal/{message_id}/cloturer': ['admin', 'gestionnaire', 'super_admin'],
    'DELETE /api/v1/collaboration/espaces/{espace_id}': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/espaces/{espace_id}/restaurer': ['admin', 'gestionnaire', 'super_admin'],
    'DELETE /api/v1/collaboration/tableaux-espace/{tableau_id}': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/tableaux-espace/{tableau_id}/restaurer': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/collaboration/corbeille': ['admin', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/collaboration/reglages': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'PUT /api/v1/collaboration/reglages': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/collaboration/espaces/{espace_id}/emetteurs': ['admin', 'direction', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/espaces/{espace_id}/canal/demander-config': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/collaboration/bot-demandes': ['admin', 'super_admin'],
    'PUT /api/v1/admin/collaboration/espaces/{espace_id}/bot': ['super_admin'],
    'POST /api/v1/collaboration/espaces/{espace_id}/emetteurs': ['admin', 'gestionnaire', 'super_admin'],
    'DELETE /api/v1/collaboration/espaces/{espace_id}/emetteurs/{utilisateur_id}': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/collaboration/canal/{message_id}/workflow': ['admin', 'direction', 'gestionnaire', 'super_admin'],
    'DELETE /api/v1/admin/fonctions/{cle}': ['admin', 'gestionnaire', 'super_admin'],
    'DELETE /api/v1/admin/membres/{membre_id}': ['admin', 'super_admin'],
    'DELETE /api/v1/admin/membres/{membre_id}/fonctions/{fonction_id}': ['admin', 'gestionnaire', 'super_admin'],
    'DELETE /api/v1/admin/membres/{membre_id}/groupes/{appartenance_id}': ['admin', 'super_admin'],
    'DELETE /api/v1/admin/niveaux-engagement/{cle}': ['admin', 'gestionnaire', 'super_admin'],
    'DELETE /api/v1/admin/organisation/{entity}/{item_id}': ['admin', 'super_admin'],
    'GET /api/v1/admin/annuaire/pays': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/attestations': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/audit': ['admin', 'super_admin'],
    'GET /api/v1/admin/bergers': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/catalogue-acces': ['admin', 'super_admin'],
    'GET /api/v1/admin/catalogue-permissions': ['admin', 'super_admin'],
    'GET /api/v1/admin/commissions': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/comptage/{evenement_id}': ['admin', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/consentements': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/consentements/{cle}': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/coordinations': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/deblocage/elements': ['admin', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/demandes': ['admin', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/demandes/{demande_id}': ['admin', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/demandes/{demande_id}/modifications': ['admin', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/demandes/{demande_id}/photo-pending': ['admin', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/documents/{document_id}/content': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/documents/{document_id}/url': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/doublons': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/doublons/comparaison': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/doublons/seuil': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/evenements': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/evenements/{evenement_id}/participation-stats': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/evenements/{evenement_id}/questionnaire': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/evenements/{evenement_id}/reponses': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/fonctions': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/groupes': ['admin', 'super_admin'],
    'GET /api/v1/admin/inscriptions': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/engagement/dashboard': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/engagement/invitations': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/engagement/template.xlsx': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/engagement/convertir': ['admin', 'super_admin'],
    'POST /api/v1/admin/engagement/import': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/engagement/invitations': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/inscriptions/compteurs': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/inscriptions/{membre_id}/corrections': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/inscriptions/{membre_id}/dossier': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/integrations': ['admin', 'super_admin'],
    'GET /api/v1/admin/integrations/statut': ['admin', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/intendances': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/membres': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/membres/{membre_id}': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/membres/{membre_id}/acces-effectif': ['admin', 'super_admin'],
    'GET /api/v1/admin/membres/{membre_id}/connexions': ['admin', 'super_admin'],
    'GET /api/v1/admin/membres/{membre_id}/fonctions': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/membres/{membre_id}/gouvernance': ['admin', 'super_admin'],
    'GET /api/v1/admin/membres/{membre_id}/groupes': ['admin', 'super_admin'],
    'GET /api/v1/admin/membres/{membre_id}/participation-analytique': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/membres/{membre_id}/photo-url': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/modeles/anniversaire': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/niveaux-engagement': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/notifications/echecs': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/notifications/types': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/parametres/questionnaire-fenetre': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/parametres/sondage-pointage': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'PUT /api/v1/admin/parametres/sondage-pointage': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/notifications/declencher-sondages': ['admin', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/participation/global': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/pays-signature': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/perimetres-disponibles': ['admin', 'super_admin'],
    'GET /api/v1/admin/sous-commissions': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/statistiques': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/tags/demandes': ['admin', 'super_admin'],
    'GET /api/v1/admin/terminaux': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/tribus': ['admin', 'controleur', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/admin/utilisateurs': ['admin', 'super_admin'],
    'GET /api/v1/collaboration/cartes/{carte_id}/commentaires': ['admin', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/collaboration/tableaux': ['admin', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/collaboration/tableaux/{tableau_id}': ['admin', 'direction', 'gestionnaire', 'super_admin'],
    'GET /api/v1/controle/evenements': ['admin', 'controleur', 'gestionnaire', 'super_admin'],
    'GET /api/v1/controle/membres': ['admin', 'controleur', 'gestionnaire', 'super_admin'],
    'PATCH /api/v1/admin/demandes/{demande_id}': ['admin', 'gestionnaire', 'super_admin'],
    'PATCH /api/v1/admin/evenements/{evenement_id}/session': ['admin', 'gestionnaire', 'super_admin'],
    'PATCH /api/v1/admin/membres/{membre_id}': ['admin', 'super_admin'],
    'PATCH /api/v1/admin/membres/{membre_id}/fonctions/{fonction_id}': ['admin', 'gestionnaire', 'super_admin'],
    'PATCH /api/v1/admin/organisation/{entity}/{item_id}': ['admin', 'super_admin'],
    'PATCH /api/v1/admin/organisation/{entity}/{item_id}/publication': ['admin', 'super_admin'],
    'PATCH /api/v1/admin/pays-signature/{code}': ['admin', 'gestionnaire', 'super_admin'],
    'PATCH /api/v1/admin/terminaux/{terminal_id}': ['admin', 'super_admin'],
    'PATCH /api/v1/admin/utilisateurs/{utilisateur_id}': ['super_admin'],
    'PATCH /api/v1/collaboration/cartes/{carte_id}': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/anniversaires/declencher': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/attestations/{attestation_id}/valider': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/commissions': ['admin', 'super_admin'],
    'POST /api/v1/admin/comptage': ['admin', 'controleur', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/consentements/{cle}': ['admin', 'super_admin'],
    'POST /api/v1/admin/coordinations': ['admin', 'super_admin'],
    'POST /api/v1/admin/demandes/{demande_id}/demander-piece': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/demandes/{demande_id}/messages': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/demandes/{demande_id}/modifications/decision': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/demandes/{demande_id}/prendre-en-charge': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/doublons/scan': ['admin', 'super_admin'],
    'POST /api/v1/admin/doublons/{detection_id}/statut': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/evenements': ['admin', 'gestionnaire', 'super_admin'],
    'PUT /api/v1/admin/evenements/{evenement_id}': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/evenements/{evenement_id}/annuler': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/evenements/{evenement_id}/reactiver': ['admin', 'gestionnaire', 'super_admin'],
    'DELETE /api/v1/admin/evenements/{evenement_id}': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/evenements/{evenement_id}/sondage': ['admin', 'direction', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/evenements/{evenement_id}/test-diffusion': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/fonctions': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/groupes': ['super_admin'],
    'PATCH /api/v1/admin/groupes/{groupe_id}': ['super_admin'],
    'DELETE /api/v1/admin/groupes/{groupe_id}': ['super_admin'],
    'POST /api/v1/admin/inscriptions/membre': ['admin', 'super_admin'],
    'POST /api/v1/admin/inscriptions/membres-lot': ['admin', 'super_admin'],
    'POST /api/v1/admin/inscriptions/{membre_id}/decision': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/inscriptions/{membre_id}/relancer-mdp': ['admin', 'super_admin'],
    'POST /api/v1/admin/integrations/test-email': ['admin', 'super_admin'],
    'POST /api/v1/admin/intendances': ['admin', 'super_admin'],
    'POST /api/v1/admin/membres': ['admin', 'super_admin'],
    'POST /api/v1/admin/membres/{membre_id}/bloquer': ['admin', 'super_admin'],
    'POST /api/v1/admin/membres/{membre_id}/debloquer': ['admin', 'super_admin'],
    'POST /api/v1/admin/membres/{membre_id}/demander-document': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/membres/{membre_id}/fonctions': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/membres/{membre_id}/groupes': ['admin', 'super_admin'],
    'POST /api/v1/admin/niveaux-engagement': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/notifications/declencher-quotidien': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/notifications/echecs/{echec_id}/resolu': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/admin/sous-commissions': ['admin', 'super_admin'],
    'POST /api/v1/admin/tags': ['admin', 'super_admin'],
    'POST /api/v1/admin/tags/demandes/{demande_id}/decision': ['admin', 'super_admin'],
    'POST /api/v1/admin/terminaux': ['admin', 'super_admin'],
    'POST /api/v1/admin/utilisateurs': ['super_admin'],
    'POST /api/v1/admin/utilisateurs/lot': ['super_admin'],
    'POST /api/v1/collaboration/cartes': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/cartes/{carte_id}/commentaires': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/cartes/{carte_id}/publier': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/collaboration/tableaux': ['admin', 'gestionnaire', 'super_admin'],
    'POST /api/v1/controle/checkin': ['admin', 'controleur', 'gestionnaire', 'super_admin'],
    'POST /api/v1/controle/checkin-manuel': ['admin', 'controleur', 'gestionnaire', 'super_admin'],
    'POST /api/v1/controle/checkout': ['admin', 'controleur', 'gestionnaire', 'super_admin'],
    'POST /api/v1/controle/verify': ['admin', 'controleur', 'gestionnaire', 'super_admin'],
    'PUT /api/v1/admin/coordinations/{item_id}': ['admin', 'super_admin'],
    'PUT /api/v1/admin/doublons/seuil': ['admin', 'super_admin'],
    'PUT /api/v1/admin/evenements/{evenement_id}/questionnaire': ['admin', 'gestionnaire', 'super_admin'],
    'PUT /api/v1/admin/fonctions/{cle}': ['admin', 'gestionnaire', 'super_admin'],
    'PUT /api/v1/admin/integrations/{cle}': ['admin', 'super_admin'],
    'PUT /api/v1/admin/intendances/{item_id}': ['admin', 'super_admin'],
    'PUT /api/v1/admin/membres/{membre_id}/fonction': ['admin', 'gestionnaire', 'super_admin'],
    'PUT /api/v1/admin/modeles/anniversaire': ['admin', 'gestionnaire', 'super_admin'],
    'PUT /api/v1/admin/niveaux-engagement/{cle}': ['admin', 'gestionnaire', 'super_admin'],
    'PUT /api/v1/admin/notifications/types/{cle}': ['admin', 'gestionnaire', 'super_admin'],
    'PUT /api/v1/admin/parametres/questionnaire-fenetre': ['admin', 'gestionnaire', 'super_admin'],
    'PUT /api/v1/admin/tribus/{tribu_id}/patriarche': ['admin', 'super_admin'],
    'PUT /api/v1/membres/me/langue': ['admin', 'controleur', 'direction', 'gestionnaire', 'membre', 'super_admin'],
}


def _roles_holding(permission: str) -> set[str]:
    return {role for role in ROLES if permission in ROLE_PERMISSIONS.get(role, frozenset())}


def test_every_role_gated_endpoint_has_a_permission() -> None:
    manquants = [ep for ep in REAL_ROLESET if ep not in ENDPOINT_PERMISSION]
    assert not manquants, f"endpoints sans permission mappee: {manquants}"


def test_permission_mapping_is_iso_behaviour() -> None:
    ecarts = []
    for ep, roles in REAL_ROLESET.items():
        perm = ENDPOINT_PERMISSION.get(ep)
        if perm is None:
            continue
        holders = _roles_holding(perm)
        if holders != set(roles):
            ecarts.append((ep, perm, sorted(roles), sorted(holders)))
    assert not ecarts, f"ecarts d'iso-comportement: {ecarts}"


def test_role_permissions_within_catalogue() -> None:
    for role, perms in ROLE_PERMISSIONS.items():
        inconnues = [p for p in perms if p not in CATALOGUE]
        assert not inconnues, f"{role} refere hors catalogue: {inconnues}"


# --- Live-code conformity: scan the actual backend, not just the seed data. ---

def test_live_code_admits_exactly_the_phase_a_roles() -> None:
    """Every role-gated endpoint, switched or not, must admit its Phase A role-set.

    Switchover safety net: a wrong permission on a switched endpoint or a lost gate
    makes the live-scanned admitted roles diverge from the frozen matrix.
    """
    from tests.rbac_live_scan import scan

    live = scan()
    ecarts = []
    for ep, roles in REAL_ROLESET.items():
        entry = live.get(ep)
        if entry is None:
            ecarts.append((ep, "ABSENT du code live", sorted(roles), None))
        elif entry["admis"] != sorted(roles):
            ecarts.append((ep, entry["kind"], sorted(roles), entry["admis"]))
    assert not ecarts, f"ecarts code-live vs matrice Phase A: {ecarts}"


def test_switched_endpoints_use_the_mapped_permission() -> None:
    """A switched endpoint must carry EXACTLY the permission the mapping assigns."""
    from tests.rbac_live_scan import scan

    mauvais = []
    for ep, entry in scan().items():
        if entry["kind"] == "perm":
            attendu = ENDPOINT_PERMISSION.get(ep)
            if entry["cle"] != attendu:
                mauvais.append((ep, entry["cle"], attendu))
    assert not mauvais, f"permission erronee sur endpoint bascule (endpoint, mis, attendu): {mauvais}"


def test_no_unresolved_gate_in_live_code() -> None:
    """No endpoint should end up with an unresolved authorization dependency."""
    from tests.rbac_live_scan import scan

    unresolved = [ep for ep, e in scan().items() if str(e["kind"]).startswith("alias:") or e["kind"] == "?"]
    assert not unresolved, f"gates non resolues dans le code live: {unresolved}"
