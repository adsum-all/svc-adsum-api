"""ADSUM API application entrypoint."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
from .comptage import comptage_public_router
from .comptage import router as comptage_router
from .config import settings
from .consentement import router as consentement_router
from .consultations import router as consultations_router
from .controle import router as controle_router
from .demandes import router as demandes_router
from .doublons import router as doublons_router
from .email_webhook import router as email_webhook_router
from .emargement import router as emargement_router
from .engagement import public_router as engagement_public_router
from .engagement import router as engagement_router
from .engagement_import import router as engagement_import_router
from .equipes_speciales import router as equipes_speciales_router
from .evenements_series import router as evenements_series_router
from .fichiers import admin_router as fichiers_admin_router
from .fichiers import router as fichiers_router
from .fonctions import router as fonctions_router
from .formation import router as formation_router
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
from .matrice_pays import router as matrice_pays_router
from .membres import router as membres_router
from .mfa import router as mfa_router
from .middleware import (
    BodySizeLimitMiddleware,
    CorsSafeErrorBoundaryMiddleware,
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
from .pilotage import router as pilotage_router
from .reference import router as reference_router
from .retention_archivage import router as retention_archivage_router
from .rgpd import router as rgpd_router
from .sessions_membre import router as sessions_membre_router
from .sondage import cron_router as sondage_cron_router
from .sondage import router as sondage_router
from .tags import router as tags_router
from .technical_admin import router as technical_admin_router
from .telegram_liaison import router as telegram_liaison_router
from .terminaux import router as terminaux_router
from .type_evenement import router as type_evenement_router
from .users import router as users_router

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

app.include_router(auth_router)
app.include_router(mfa_router)
app.include_router(membres_router)
app.include_router(admin_router)
app.include_router(evenements_series_router)
app.include_router(controle_router)
app.include_router(organisation_router)
app.include_router(equipes_speciales_router)
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
app.include_router(pilotage_router)
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
app.include_router(collaboration_canal_router)
app.include_router(collaboration_canal_notes_router)
app.include_router(collaboration_telegram_ingest_router)
app.include_router(collaboration_canal_workflow_router)
app.include_router(collaboration_canal_config_router)
app.include_router(collaboration_corbeille_router)
app.include_router(ai_config_router)
app.include_router(collaboration_espaces_router)
app.include_router(collaboration_modeles_router)
app.include_router(collaboration_tableaux_router)
app.include_router(collaboration_cartes_router)
app.include_router(collaboration_cartes_social_router)
app.include_router(collaboration_pieces_router)
app.include_router(collaboration_checklists_router)
app.include_router(collaboration_presence_router)
app.include_router(collaboration_transverse_router)
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
app.include_router(technical_admin_router)
app.include_router(groupes_router)
app.include_router(groupes_fiche_router)
app.include_router(groupes_lecture_router)
app.include_router(emargement_router)
app.include_router(engagement_public_router)
app.include_router(email_webhook_router)
app.include_router(sessions_membre_router)
app.include_router(telegram_liaison_router)
app.include_router(engagement_router)
app.include_router(engagement_import_router)
app.include_router(sondage_router)
app.include_router(sondage_cron_router)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "adsum-api", "version": "0.1.0"}
