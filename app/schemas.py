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


class CreateMembre(BaseModel):
    """Payload to register a new member from the back office."""

    email: EmailStr
    nom: str | None = None
    prenoms: str | None = None
    telephone: str | None = None
    commission_id: str | None = None
    groupe: str | None = None
    matricule: str | None = None


class UpdateMembre(BaseModel):
    """Partial update of a member. Only provided fields are written."""

    nom: str | None = None
    prenoms: str | None = None
    telephone: str | None = None
    commission_id: str | None = None
    groupe: str | None = None
    statut: str | None = Field(default=None, pattern="^(actif|inactif|suspendu)$")
    verifie: bool | None = None


class CommissionOut(BaseModel):
    """A commission row."""

    id: str
    nom: str
    description: str | None = None


class CreateCommission(BaseModel):
    """Payload to create a commission."""

    nom: str = Field(min_length=1)
    description: str | None = None


class CreateEvenement(BaseModel):
    """Payload to create an event."""

    titre: str = Field(min_length=1)
    type: str | None = None
    volet: str = Field(default="A", pattern="^(A|B)$")
    debut: datetime
    fin: datetime | None = None
    lieu: str | None = None


class VerifyResult(BaseModel):
    """Outcome of verifying a member QR token."""

    valid: bool
    reason: str | None = None
    membre_id: str | None = None
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    key_version: int | None = None
    matricule: str | None = None
    nom: str | None = None
    prenoms: str | None = None


class CheckinRequest(BaseModel):
    """Payload to record a check-in from a QR token at an event."""

    token: str = Field(min_length=1)
    evenement_id: str = Field(min_length=1)


class CheckinMembre(BaseModel):
    """Minimal member identity returned with a check-in result."""

    id: str
    matricule: str
    nom: str | None = None
    prenoms: str | None = None


class CheckinResult(BaseModel):
    """Outcome of a successful check-in."""

    deja_present: bool
    membre: CheckinMembre
    evenement_id: str
    arrivee: datetime | None = None


class VerifyRequest(BaseModel):
    """Payload to verify a QR token without recording attendance."""

    token: str = Field(min_length=1)
