"""Request and response models (Pydantic)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


class UserMe(BaseModel):
    id: str
    email: EmailStr
    role: str
    membre_id: str | None = None


class MembreProfile(BaseModel):
    """Public profile of the authenticated member."""

    id: str
    matricule: str
    email: EmailStr
    nom: str | None = None
    prenoms: str | None = None
    telephone: str | None = None
    groupe: str | None = None
    photo_url: str | None = None
    statut: str
    verifie: bool
    commission: str | None = None


class EvenementOut(BaseModel):
    """A scheduled event visible to the member."""

    id: str
    titre: str
    type: str | None = None
    volet: str
    debut: datetime
    fin: datetime | None = None
    lieu: str | None = None
    session_ouverte: bool


class PresenceOut(BaseModel):
    """One attendance record of the member, with its event title."""

    evenement_id: str
    evenement_titre: str
    debut: datetime | None = None
    arrivee: datetime | None = None
    depart: datetime | None = None
    methode: str | None = None


class QrToken(BaseModel):
    """A signed, short-lived QR token the member shows for check-in."""

    token: str
    membre_id: str
    issued_at: datetime
    expires_at: datetime
    key_version: int
