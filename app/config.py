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
    # Encryption key for identity documents at rest (Fernet). If unset, a key is
    # derived from jwt_secret so encryption still works; set a dedicated key
    # (ADSUM_DOC_ENCRYPTION_KEY) in production for proper key separation.
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

    @property
    def database_dsn(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


settings = Settings()
