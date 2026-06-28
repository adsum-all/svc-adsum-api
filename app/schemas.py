"""Request and response models (Pydantic)."""
from __future__ import annotations

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
