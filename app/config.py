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

    @property
    def database_dsn(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


settings = Settings()
