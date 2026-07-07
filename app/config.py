"""Settings, read from the environment only (Constitution I10, no secret in code)."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ADSUM_", extra="ignore")

    # Deployment environment. Set ADSUM_ENVIRONMENT=production on the live
    # deployment: it turns on fail-closed guards (e.g. a dedicated document
    # encryption key becomes mandatory instead of a dev-only derived fallback).
    environment: str = "development"
    # PostgreSQL connection (plain postgresql:// form), Supabase Paris in production.
    database_url: str = ""
    # JWT signing
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    # Session length. A membership app is used casually, so a short 60-minute
    # token logged people out constantly on refresh; 14 days keeps a persisted
    # session usable. Sensitive operations are still gated by single-use codes.
    access_token_minutes: int = 20160
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
    email_from: str = "saintgabrielsacerdoceroyal@ikmail.com"
    email_from_name: str = "Sacerdoce Royal"
    email_api_key: str = ""
    email_smtp_host: str = ""
    email_smtp_port: int = 587
    email_smtp_user: str = ""
    email_smtp_password: str = ""
    # Supabase Storage (S3) for member files: profile photos, identity documents,
    # signed consents. Files are private; the API mints short-lived signed URLs.
    supabase_url: str = ""
    supabase_service_key: str = ""
    storage_bucket_photos: str = "member-photos"
    storage_bucket_documents: str = "member-documents"
    # Encryption key for identity documents at rest (Fernet). Mandatory in
    # production: when environment is production the API fails closed if it is
    # unset. Outside production a key is derived from jwt_secret so local
    # development still works, without proper key separation.
    doc_encryption_key: str = ""
    # Retired keys kept for decryption during a rotation (comma-separated Fernet
    # keys). New data is always encrypted with doc_encryption_key.
    doc_encryption_keys_old: str = ""
    # Notification channels. Each channel is only attempted when configured.
    # Telegram (free): create a bot with @BotFather and set ADSUM_TELEGRAM_BOT_TOKEN.
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    telegram_api_base: str = "https://api.telegram.org"
    # WhatsApp Cloud API (Meta, paid per message): needs a verified WABA, a
    # permanent System User token, a phone number id and approved templates.
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_graph_version: str = "v22.0"
    whatsapp_template_anniversaire: str = ""
    whatsapp_template_lang: str = "fr"
    # SMS placeholder: no provider selected yet.
    sms_provider: str = ""
    # Daily cron shared secret (Vercel sends it as an Authorization bearer header).
    cron_secret: str = ""
    # Two-factor authentication. mfa_trust_days is how long a member's "trust this
    # device" choice skips the login code. mfa_baseline_enforced, when true, makes
    # the 30-day / new-device verification mandatory for EVERY member even without
    # opting in; it is off by default so 2FA is opt-in first and no member is locked
    # out on rollout, and can be switched on (ADSUM_MFA_BASELINE_ENFORCED=true) once
    # code delivery is confirmed reliable for everyone.
    mfa_trust_days: int = 30
    # Reinforced mode (member opted in via mfa_actif): a shorter trust window, so a
    # trusted device is re-verified more often. 2FA itself is always on when the
    # baseline is enforced; this is a real, honest strengthening the member controls.
    mfa_trust_days_strict: int = 7
    mfa_baseline_enforced: bool = False

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in ("production", "prod")

    @property
    def database_dsn(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


settings = Settings()

# Fail closed at startup: the JWT secret signs access tokens and derives the OTP and
# QR keys. An empty secret would make tokens forgeable and one-time codes computable
# offline (account takeover), so the API refuses to run without it.
if not settings.jwt_secret.strip():
    raise RuntimeError("ADSUM_JWT_SECRET is required and must not be empty (fail-closed).")
