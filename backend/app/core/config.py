"""Application settings."""
from __future__ import annotations

from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

_CONFIG_FILE = Path(__file__).resolve()
_BACKEND_DIR = _CONFIG_FILE.parent.parent.parent
_ROOT_DIR    = _BACKEND_DIR.parent

_ROOT_ENV    = str(_ROOT_DIR   / ".env")
_BACKEND_ENV = str(_BACKEND_DIR / ".env")


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── AI / Ollama ───────────────────────────────────────────────────────────
    OLLAMA_URL: str = "http://host.docker.internal:11434"
    OLLAMA_MODEL: str = "qwen2.5:3b"

    # ── GitHub App ────────────────────────────────────────────────────────────
    GITHUB_APP_WEBHOOK_SECRET: str
    GITHUB_APP_CLIENT_ID: str = ""
    GITHUB_APP_CLIENT_SECRET: str = ""
    GITHUB_TOKEN: str = ""
    GITHUB_API_BASE: str = "https://api.github.com"

    # ── Caching ───────────────────────────────────────────────────────────────
    DIFF_CACHE_TTL: int = 7200

    # ── Security ──────────────────────────────────────────────────────────────
    SECRET_KEY: str

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"
    # "console" = coloured dev output | "json" = production JSON
    LOG_FORMAT: str = "console"

    # ── CORS ──────────────────────────────────────────────────────────────────
    CORS_ORIGINS: str = "http://localhost:3000"

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    model_config = SettingsConfigDict(
        env_file=(_ROOT_ENV, _BACKEND_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
