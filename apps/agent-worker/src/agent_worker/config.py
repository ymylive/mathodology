"""Runtime settings for the agent worker, loaded from env / .env."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Worker configuration.

    Env file lookup tries a couple of plausible locations so the worker runs
    the same whether invoked from the repo root or from `apps/agent-worker`.
    """

    model_config = SettingsConfigDict(
        env_file=["../.env", "../../.env", ".env"],
        env_file_encoding="utf-8",
        extra="ignore",
    )

    redis_url: str = Field(default="redis://127.0.0.1:6379/0", alias="REDIS_URL")
    gateway_http: str = Field(
        default="http://127.0.0.1:8080", alias="VITE_GATEWAY_HTTP"
    )
    dev_auth_token: str = Field(
        default="dev-local-insecure-token", alias="DEV_AUTH_TOKEN"
    )
    worker_concurrency: int = Field(default=2, alias="WORKER_CONCURRENCY")


def get_settings() -> Settings:
    """Load settings. Kept as a function so tests can override easily."""
    return Settings()
