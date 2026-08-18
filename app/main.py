"""ADSUM API application entrypoint."""
from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import db, modules_souscrits, organisation_courante, validation_sans_fuite
from .activites_membre import router as activites_membre_router
from .admin import router as admin_router
from .ai_config import router as ai_config_router
from .ai_tts import router as ai_tts_router
from .analytics import router as analytics_router
from .anniversaires import router as anniversaires_router
from .anniversaires_annuaire import router as anniversaires_annuaire_router
from .applications import router as applications_router
from .audit import router as audit_router
from .auth import router as auth_router
from .bibliotheque import router as bibliotheque_router
from .bibliotheque_lecture import router as bibliotheque_lecture_router
from .calendrier_institutionnel import router as calendrier_institutionnel_router
from .cibles_activite import router as cibles_activite_router
from .collaboration_canal import router as collaboration_canal_router
from .collaboration_canal_config import router as collaboration_canal_config_router
from .collaboration_canal_notes import router as collaboration_canal_notes_router
from .collaboration_canal_workflow import router as collaboration_canal_workflow_router
from .collaboration_cartes import router as collaboration_cartes_router
from .collaboration_cartes_social import router as collaboration_cartes_social_router
from .collaboration_checklists import router as collaboration_checklists_router
from .collaboration_corbeille import router as collaboration_corbeille_router
from .collaboration_espaces import router as collaboration_espaces_router
from .collaboration_modeles import router as collaboration_modeles_router
from .collaboration_pieces import router as collaboration_pieces_router
from .collaboration_presence import router as collaboration_presence_router
from .collaboration_tableaux import router as collaboration_tableaux_router
from .collaboration_telegram_ingest import router as collaboration_telegram_ingest_router
from .collaboration_transverse import router as collaboration_transverse_router
from .communication_centre import router as communication_centre_router
from .completude_profil import router as completude_profil_router
from .comptage import comptage_public_router
from .comptage import router as comptage_router
from .config import settings
from .consentement import router as consentement_router
from .console_observabilite import router as console_observabilite_router
from .console_organisations import router as console_organisations_router
from .consultations import router as consultations_router
from .controle import router as controle_router
from .demandes import router as demandes_router
from .direction_rapport import router as direction_rapport_router
from .direction_routes import router as direction_router
from .doublons import router as doublons_router
from .email_fournisseurs import router as email_fournisseurs_router
from .email_webhook import router as email_webhook_router
from .emargement import router as emargement_router
from .engagement import public_router as engagement_public_router
from .engagement import router as engagement_router
from .engagement_import import router as engagement_import_router
from .equipe_dirigeante import router as equipe_dirigeante_router
from .equipes_speciales import router as equipes_speciales_router
from .evenements_series import router as evenements_series_router
from .fichiers import admin_router as fichiers_admin_router
from .fichiers import router as fichiers_router
from .fonctions import router as fonctions_router
from .formation import router as formation_router
from .formulaire_pointage import router as formulaire_pointage_router
from .gestion import router as gestion_router
from .groupes import router as groupes_router
from .groupes_fiche import router as groupes_fiche_router
from .groupes_lecture import router as groupes_lecture_router
from .information import router as information_router
from .information_actions import router as information_actions_router
from .information_feed import router as information_feed_router
from .information_membre import router as information_membre_router
from .inscription import router as inscription_router
from .inscription_admin import router as inscription_admin_router
from .inscriptions_reparation import router as inscriptions_reparation_router
from .institutionnel import router as institutionnel_router
from .integrations import router as integrations_router
from .interim import router as interim_router
from .interne_courriel import router as interne_courriel_router
from .marque_publique import router as marque_publique_router
from .matrice_pays import router as matrice_pays_router
from .membres import router as membres_router
from .mfa import router as mfa_router
from .middleware import (
    BodySizeLimitMiddleware,
    CorsSafeErrorBoundaryMiddleware,
    OrganisationMiddleware,
    SecurityHeadersMiddleware,
)
from .modifications import router as modifications_router
from .niveaux import router as niveaux_router
from .notifications import router as notifications_router
from .notifications_centre import router as notifications_centre_router
from .organigramme import router as organigramme_router
from .organigramme_reglages import router as organigramme_reglages_router
from .organisation import router as organisation_router
from .organisation_admin import router as organisation_admin_router
from .participation import router as participation_router
from .permissions_applications_api import router as permissions_applications_router
from .pilotage import router as pilotage_router
from .pilotage_absences import router as pilotage_absences_router
from .push_api import router as push_router
from .reference import router as reference_router
from .reglages_duree import router as reglages_duree_router
from .retention_archivage import router as retention_archivage_router
from .rgpd import router as rgpd_router
from .sessions_membre import router as sessions_membre_router
from .sondage import cron_router as sondage_cron_router
from .sondage import router as sondage_router
from .supervision_tribus import router as supervision_tribus_router
from .support import router as support_router
from .support_console import router as support_console_router
from .support_entrant import router as support_entrant_router
from .tags import router as tags_router
from .technical_admin import router as technical_admin_router
from .telegram_liaison import router as telegram_liaison_router
from .terminaux import router as terminaux_router
from .type_evenement import router as type_evenement_router
from .users import router as users_router
from .vocabulaire_api import router as vocabulaire_router

app = FastAPI(
    title="ADSUM API",
    version="0.1.0",
    description="ADSUM business API: authentication and member endpoints on the real PostgreSQL.",
)

# Middleware order matters: add_middleware prepends, so the LAST call is the
# outermost wrapper. CORS must stay outermost so its header is added on the way
# out of every response. The error boundary is added FIRST so it is the innermost
# application middleware: it catches an endpoint or database exception before it
# can escape to the CORS-less server layer, and its 500 response then travels back
# out through CORS and receives the Access-Control-Allow-Origin header. Without
# this ordering an unhandled 500 reaches the browser with no CORS header and is
# misreported as a generic network error, masking the true cause.
# Resolved before the error boundary, so a handler that fails still fails inside the
# right organisation, and before CORS so a refusal still carries its header.
app.add_middleware(OrganisationMiddleware)
app.add_middleware(CorsSafeErrorBoundaryMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# One database per organisation: every query of a request goes to the database the
# middleware resolved. Installed once, here, because db.py cannot import the resolver
# without closing an import cycle.
db.installer_resolveur_dsn(organisation_courante.dsn_courant)

# A module the organisation has not subscribed to must not merely be hidden: its API
# refuses. Applied on the router rather than on each route, because a rule written
# endpoint by endpoint is a rule forgotten on the next endpoint.
_MODULE = {
    code: [Depends(modules_souscrits.exiger(code))]
    for code in ("direction", "pilotage", "collaboration", "controleur")
}

# Avant tout routeur : un 422 par défaut recopie la valeur soumise dans sa réponse,
# et sur la route de connexion cette valeur est un mot de passe en clair.
validation_sans_fuite.installer(app)

app.include_router(interne_courriel_router)
app.include_router(auth_router)
app.include_router(mfa_router)
app.include_router(membres_router)
app.include_router(admin_router)
app.include_router(evenements_series_router)
app.include_router(controle_router, dependencies=_MODULE["controleur"])
app.include_router(organisation_router)
app.include_router(equipes_speciales_router)
app.include_router(equipe_dirigeante_router)
app.include_router(supervision_tribus_router)
app.include_router(marque_publique_router)
app.include_router(vocabulaire_router)
app.include_router(reglages_duree_router)
app.include_router(bibliotheque_router)
app.include_router(bibliotheque_lecture_router)
app.include_router(organigramme_router)
app.include_router(organigramme_reglages_router)
app.include_router(interim_router)
app.include_router(information_feed_router)
app.include_router(information_membre_router)
app.include_router(information_actions_router)
app.include_router(information_router)
app.include_router(ai_tts_router)
app.include_router(activites_membre_router)
app.include_router(notifications_centre_router)
app.include_router(retention_archivage_router)
app.include_router(institutionnel_router)
app.include_router(calendrier_institutionnel_router)
app.include_router(communication_centre_router)
app.include_router(organisation_admin_router)
app.include_router(participation_router)
app.include_router(direction_router, dependencies=_MODULE["direction"])
app.include_router(direction_rapport_router, dependencies=_MODULE["direction"])
app.include_router(pilotage_router, dependencies=_MODULE["pilotage"])
app.include_router(pilotage_absences_router, dependencies=_MODULE["pilotage"])
app.include_router(consultations_router)
app.include_router(tags_router)
app.include_router(analytics_router)
app.include_router(users_router)
app.include_router(terminaux_router)
app.include_router(audit_router)
app.include_router(comptage_router)
app.include_router(comptage_public_router)
app.include_router(cibles_activite_router)
# Legacy collaboration router (app/collaboration.py) is intentionally NOT mounted:
# its /tableaux and /cartes endpoints operated on collab_tableau/collab_carte
# WITHOUT per-space membership checks (require_espace_role), bypassing the space
# isolation enforced by the space-scoped router (collaboration_tableaux.py) and
# leaking cards across spaces. The space-scoped router fully supersedes it.
app.include_router(collaboration_canal_router, dependencies=_MODULE["collaboration"])
app.include_router(collaboration_canal_notes_router, dependencies=_MODULE["collaboration"])
app.include_router(collaboration_telegram_ingest_router, dependencies=_MODULE["collaboration"])
app.include_router(collaboration_canal_workflow_router, dependencies=_MODULE["collaboration"])
app.include_router(collaboration_canal_config_router, dependencies=_MODULE["collaboration"])
app.include_router(collaboration_corbeille_router, dependencies=_MODULE["collaboration"])
app.include_router(ai_config_router)
app.include_router(collaboration_espaces_router, dependencies=_MODULE["collaboration"])
app.include_router(collaboration_modeles_router, dependencies=_MODULE["collaboration"])
app.include_router(collaboration_tableaux_router, dependencies=_MODULE["collaboration"])
app.include_router(collaboration_cartes_router, dependencies=_MODULE["collaboration"])
app.include_router(collaboration_cartes_social_router, dependencies=_MODULE["collaboration"])
app.include_router(collaboration_pieces_router, dependencies=_MODULE["collaboration"])
app.include_router(collaboration_checklists_router, dependencies=_MODULE["collaboration"])
app.include_router(collaboration_presence_router, dependencies=_MODULE["collaboration"])
app.include_router(collaboration_transverse_router, dependencies=_MODULE["collaboration"])
app.include_router(demandes_router)
app.include_router(doublons_router)
app.include_router(fichiers_router)
app.include_router(fichiers_admin_router)
app.include_router(formation_router)
app.include_router(inscriptions_reparation_router)
app.include_router(inscription_router)
app.include_router(inscription_admin_router)
app.include_router(modifications_router)
app.include_router(consentement_router)
app.include_router(matrice_pays_router)
app.include_router(reference_router)
app.include_router(type_evenement_router)
app.include_router(applications_router)
app.include_router(rgpd_router)
app.include_router(anniversaires_router)
app.include_router(anniversaires_annuaire_router)
app.include_router(notifications_router)
app.include_router(integrations_router)
app.include_router(fonctions_router)
app.include_router(niveaux_router)
app.include_router(gestion_router)
app.include_router(email_fournisseurs_router)
app.include_router(formulaire_pointage_router)
app.include_router(support_router)
app.include_router(support_entrant_router)
app.include_router(support_console_router)
app.include_router(console_observabilite_router)
app.include_router(console_organisations_router)
app.include_router(technical_admin_router)
app.include_router(groupes_router)
app.include_router(groupes_fiche_router)
app.include_router(groupes_lecture_router)
app.include_router(emargement_router)
app.include_router(engagement_public_router)
app.include_router(email_webhook_router)
app.include_router(permissions_applications_router)
app.include_router(sessions_membre_router)
app.include_router(telegram_liaison_router)
app.include_router(engagement_router)
app.include_router(engagement_import_router)
app.include_router(sondage_router)
app.include_router(sondage_cron_router)
app.include_router(push_router)
app.include_router(completude_profil_router)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "adsum-api", "version": "0.1.0"}
