"""Runtime settings for the agent worker, loaded from env / .env."""

from __future__ import annotations

from pydantic import AliasChoices, Field
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
        default="http://127.0.0.1:8080",
        validation_alias=AliasChoices("GATEWAY_HTTP", "VITE_GATEWAY_HTTP"),
    )
    dev_auth_token: str = Field(
        default="dev-local-insecure-token", alias="DEV_AUTH_TOKEN"
    )
    worker_concurrency: int = Field(default=2, alias="WORKER_CONCURRENCY")
    runs_dir: str = Field(default="./runs", alias="RUNS_DIR")

    # --- open-webSearch MCP (Searcher auxiliary retrieval) ----------------
    # Node CLI that speaks MCP over stdio. Defaults to the binary on PATH;
    # absolute path (e.g. /opt/homebrew/bin/open-websearch) is fine too.
    open_websearch_cmd: str = Field(
        default="open-websearch", alias="OPEN_WEBSEARCH_CMD"
    )
    # Comma-separated engine list. Tuned for Chinese competitions: Baidu/CSDN/
    # Juejin cover CUMCM/华数杯 methodology posts far better than arXiv alone;
    # Bing + DuckDuckGo give a Western-language fallback.
    open_websearch_engines: str = Field(
        default="bing,duckduckgo,baidu,csdn,juejin",
        alias="OPEN_WEBSEARCH_ENGINES",
    )
    # Hard kill-switch. When true the Searcher skips the MCP call entirely and
    # only uses arXiv — useful when Node is unavailable or CI is offline.
    open_websearch_disabled: bool = Field(
        default=False, alias="OPEN_WEBSEARCH_DISABLED"
    )

    # --- Tavily (primary web search source; M12 successor) ----------------
    # Free tier is ~1000 searches/month. When unset the Searcher silently
    # falls back to open-webSearch regardless of the user's primary choice,
    # so deploys without a key still work.
    tavily_api_key: str = Field(default="", alias="TAVILY_API_KEY")


def get_settings() -> Settings:
    """Load settings. Kept as a function so tests can override easily."""
    return Settings()
