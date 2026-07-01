"""ADSUM API application entrypoint."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .admin import router as admin_router
from .analytics import router as analytics_router
from .anniversaires import router as anniversaires_router
from .anniversaires_annuaire import router as anniversaires_annuaire_router
from .audit import router as audit_router
from .auth import router as auth_router
from .collaboration import router as collaboration_router
from .comptage import comptage_public_router
from .comptage import router as comptage_router
from .config import settings
from .consentement import router as consentement_router
from .controle import router as controle_router
from .demandes import router as demandes_router
from .doublons import router as doublons_router
from .fichiers import router as fichiers_router
from .fonctions import router as fonctions_router
from .formation import router as formation_router
from .gestion import router as gestion_router
from .inscription import router as inscription_router
from .integrations import router as integrations_router
from .matrice_pays import router as matrice_pays_router
from .membres import router as membres_router
from .middleware import SecurityHeadersMiddleware
from .notifications import router as notifications_router
from .organisation import router as organisation_router
from .organisation_admin import router as organisation_admin_router
from .participation import router as participation_router
from .reference import router as reference_router
from .rgpd import router as rgpd_router
from .terminaux import router as terminaux_router
from .users import router as users_router

app = FastAPI(
    title="ADSUM API",
    version="0.1.0",
    description="ADSUM business API: authentication and member endpoints on the real PostgreSQL.",
)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(membres_router)
app.include_router(admin_router)
app.include_router(controle_router)
app.include_router(organisation_router)
app.include_router(organisation_admin_router)
app.include_router(participation_router)
app.include_router(analytics_router)
app.include_router(users_router)
app.include_router(terminaux_router)
app.include_router(audit_router)
app.include_router(comptage_router)
app.include_router(comptage_public_router)
app.include_router(collaboration_router)
app.include_router(demandes_router)
app.include_router(doublons_router)
app.include_router(fichiers_router)
app.include_router(formation_router)
app.include_router(inscription_router)
app.include_router(consentement_router)
app.include_router(matrice_pays_router)
app.include_router(reference_router)
app.include_router(rgpd_router)
app.include_router(anniversaires_router)
app.include_router(anniversaires_annuaire_router)
app.include_router(notifications_router)
app.include_router(integrations_router)
app.include_router(fonctions_router)
app.include_router(gestion_router)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "adsum-api", "version": "0.1.0"}
