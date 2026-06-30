"""Settings, read from the environment only (Constitution I10, no secret in code)."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ADSUM_", extra="ignore")

    # PostgreSQL connection (plain postgresql:// form), Supabase Paris in production.
    database_url: str = ""
    # JWT signing
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60
    cors_origins: str = "*"
    # QR check-in token signing (Ed25519 private seed, base64url of 32 bytes)
    qr_signing_key: str = ""
    # Optional Ed25519 public key (base64url of 32 bytes) for verify-only terminals
    # that hold no private seed. When empty, the public key is derived from
    # qr_signing_key.
    qr_public_key: str = ""
    qr_key_version: int = 1
    qr_ttl_seconds: int = 90
    # E-mail gateway, provider switchable by the business at any time (no lock-in).
    # ADSUM_EMAIL_PROVIDER: console | smtp | brevo | resend
    email_provider: str = "console"
    email_from: str = "no-reply@sacerdoceroyal.info"
    email_api_key: str = ""
    email_smtp_host: str = ""
    email_smtp_port: int = 587
    email_smtp_user: str = ""
    email_smtp_password: str = ""

    @property
    def database_dsn(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


settings = Settings()
