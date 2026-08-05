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
    # How long a connection attempt may take before it is abandoned. Applied to the
    # DSN unless the URL already carries its own value. Five seconds is generous for
    # a pooler in the same region and short enough that an unreachable database
    # produces an error the caller can act on rather than a request that never ends.
    database_connect_timeout_s: int = 5
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
    # Default sender must be an address the provider actually accepts. The ikmail
    # address is NOT a validated Brevo sender (Brevo rejects it with "sender not
    # valid"), so it must never be the fallback: use the validated single sender.
    # The live value is admin-managed in integration_config and overrides this.
    email_from: str = "saintgabrielsacerdoceroyal@gmail.com"
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
    # Optional private bucket for activity attachments (images, files). When set
    # (ADSUM_STORAGE_BUCKET_EVENEMENTS, e.g. "evenement-pieces"), attachments are
    # stored as objects and served through short-lived signed URLs instead of
    # inline data URLs, keeping base64 blobs out of the database. When empty, the
    # inline data URL behaviour is used (backward compatible, no bucket required).
    storage_bucket_evenements: str = ""
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
    # Approved template used for collaboration notifications (mention, assignment,
    # due date, publication, access request). Empty until the template is approved
    # on the WABA, in which case WhatsApp is simply skipped for those messages.
    whatsapp_template_collab: str = ""
    whatsapp_template_lang: str = "fr"
    # SMS: provider selector (empty = off). 'brevo' reuses the Brevo account key
    # (email_api_key) through the transactional SMS API; sms_sender is the
    # alphanumeric originator shown to the recipient (max 11 chars).
    sms_provider: str = ""
    sms_sender: str = "ADSUM"
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
    # Technical (applicative) super-admin accounts: 2FA is always imposed and a trusted
    # device only shortens the challenge to a 72 h (3-day) window, never more. These are
    # the most privileged accounts, so they re-verify at least every three days.
    mfa_trust_days_technique: int = 3
    mfa_baseline_enforced: bool = False
    # Staff (any role beyond a plain member) MUST use 2FA: it is mandatory for anyone
    # with access to apps beyond the member app. A plain member who has not opted in is
    # only recommended to, and is forced after this grace period (days from account
    # creation). During the grace no code is ever sent to a non-opted-in member.
    mfa_grace_jours: int = 30
    mfa_staff_obligatoire: bool = True
    # RECETTE ONLY: when true, accounts on the demo domain (@exemple.com) skip the login
    # one-time code, so throwaway demonstration profiles (which have no real inbox and no
    # Telegram) can be signed in to test each role. Default false, so it is inert unless
    # explicitly enabled (ADSUM_DEMO_LOGIN_ENABLED=1) for a recette, and it NEVER relaxes
    # 2FA for a real account: the exemption is scoped to the @exemple.com throwaway domain.
    demo_login_enabled: bool = False
    demo_login_domain: str = "@exemple.com"

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in ("production", "prod")

    @property
    def database_dsn(self) -> str:
        """The connection string, with a bounded handshake.

        A connection opened per request (see db.py on why there is no pool) means
        every request also pays a TCP and TLS handshake. Nothing bounded it: when the
        pooler was unreachable, the caller waited on the operating system default,
        which is measured in minutes. The API is deployed on a platform that kills
        the invocation long before that, so the symptom was a request that produced
        nothing and explained nothing, and the local test suite simply hung.

        A refusal after a few seconds is an answer; an unbounded wait is not. Set
        only when the caller has not already chosen a value.
        """
        dsn = self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        if not dsn or "connect_timeout" in dsn:
            # Nothing configured means nothing to append: a bare query string is not
            # a connection string, and psycopg rejects it with an error about the
            # timeout rather than about the missing URL, which sends the reader after
            # the wrong thing entirely.
            return dsn
        return f"{dsn}{'&' if '?' in dsn else '?'}connect_timeout={self.database_connect_timeout_s}"


settings = Settings()

# Fail closed at startup: the JWT secret signs access tokens and derives the OTP and
# QR keys. An empty secret would make tokens forgeable and one-time codes computable
# offline (account takeover), so the API refuses to run without it.
if not settings.jwt_secret.strip():
    raise RuntimeError("ADSUM_JWT_SECRET is required and must not be empty (fail-closed).")

# Staff 2FA is a hard security requirement, not a tunable in production: staff have
# access to apps beyond the member app, so disabling their mandatory 2FA would allow
# account takeover with a password alone. The toggle stays available outside
# production (tests, local), but the live deployment refuses to start with it off.
if settings.is_production and not settings.mfa_staff_obligatoire:
    raise RuntimeError(
        "ADSUM_MFA_STAFF_OBLIGATOIRE must not be disabled in production (fail-closed): "
        "two-factor authentication is mandatory for staff."
    )
