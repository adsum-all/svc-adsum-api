"""ADSUM API application entrypoint."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .admin import router as admin_router
from .analytics import router as analytics_router
from .audit import router as audit_router
from .auth import router as auth_router
from .comptage import comptage_public_router
from .comptage import router as comptage_router
from .config import settings
from .controle import router as controle_router
from .membres import router as membres_router
from .organisation import router as organisation_router
from .terminaux import router as terminaux_router
from .users import router as users_router

app = FastAPI(
    title="ADSUM API",
    version="0.1.0",
    description="ADSUM business API: authentication and member endpoints on the real PostgreSQL.",
)

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
app.include_router(analytics_router)
app.include_router(users_router)
app.include_router(terminaux_router)
app.include_router(audit_router)
app.include_router(comptage_router)
app.include_router(comptage_public_router)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "adsum-api", "version": "0.1.0"}
