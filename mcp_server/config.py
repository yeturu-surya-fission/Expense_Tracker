"""Settings of the MCP server. It has its own database and its own secrets; it knows nothing about Telegram tokens or LLM keys."""
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "mcp_server/.env"), env_prefix="MCP_", extra="ignore")

    env: Literal["dev", "production"] = "dev"
    log_level: str = "INFO"

    # database (owned by the MCP server alone)
    database_url: str = "postgresql+asyncpg://expense:expense@localhost:5432/expense_mcp"

    # who we are on the network
    host: str = "0.0.0.0"
    port: int = 8001
    public_url: str = "http://localhost:8001"  # base URL clients use; the MCP endpoint is <public_url>/mcp
    allowed_origins: str = ""  # comma list of browser origins allowed to call us (MCP spec: Origin must be validated)
    allowed_hosts: str = ""  # extra Host header values accepted besides public_url's host, localhost and the compose service name

    # tokens
    jwt_issuer: str = ""  # defaults to public_url
    jwt_audience: str = ""  # defaults to <public_url>/mcp (RFC 8707 resource indicator)
    jwt_private_key: str = ""  # PEM (RSA). Empty in dev => a throw-away key is generated at start-up
    jwt_private_key_file: str = ""
    jwt_extra_public_keys: str = ""  # PEMs separated by '|' of OLD keys, still published in the JWKS while tokens signed by them live
    service_token_ttl_minutes: int = 10
    session_token_ttl_minutes: int = 30

    # first-party clients that may ask for a service token (client_credentials). The Telegram bot is one.
    bot_client_id: str = "telegram-bot"
    bot_client_secret: str = ""

    # abuse protection
    rate_limit_per_minute: int = 120  # per user (or per service client)
    max_request_bytes: int = 15 * 1024 * 1024  # a statement PDF travels base64 inside a request

    # behaviour (same meaning as before the split)
    # (these read MCP_<NAME> first, then the plain <NAME> from the shared .env, e.g. MCP_TIMEZONE or TIMEZONE)
    allowed_telegram_ids: str = Field("", validation_alias=AliasChoices("MCP_ALLOWED_TELEGRAM_IDS", "ALLOWED_TELEGRAM_IDS"))
    bot_username: str = ""  # the bot also tells us at login; used to build start / invite links
    currency_symbol: str = Field("₹", validation_alias=AliasChoices("MCP_CURRENCY_SYMBOL", "CURRENCY_SYMBOL"))
    timezone: str = Field("Asia/Kolkata", validation_alias=AliasChoices("MCP_TIMEZONE", "TIMEZONE"))
    budget_alert_thresholds: str = Field("50,80,100", validation_alias=AliasChoices("MCP_BUDGET_ALERT_THRESHOLDS", "BUDGET_ALERT_THRESHOLDS"))
    reminder_every_hours: int = Field(48, validation_alias=AliasChoices("MCP_REMINDER_EVERY_HOURS", "REMINDER_EVERY_HOURS"))
    ask_group_min_people: int = Field(4, validation_alias=AliasChoices("MCP_ASK_GROUP_MIN_PEOPLE", "ASK_GROUP_MIN_PEOPLE"))
    reminders_enabled: bool = True
    auto_create_tables: bool = True  # dev convenience; production runs `alembic upgrade head` instead (set to false)
    audit_retention_days: int = 90

    @field_validator("*", mode="before")
    @classmethod
    def _strip_inline_comment(cls, v):
        """Docker's env_file keeps 'VALUE   # comment' whole; drop the comment and stray quotes (never touch PEM text)."""
        if isinstance(v, str) and "BEGIN " not in v:
            v = v.split(" #")[0].strip().strip("\"'").strip()
        return v

    @property
    def issuer(self) -> str:
        return (self.jwt_issuer or self.public_url).rstrip("/")

    @property
    def resource_url(self) -> str:
        return (self.jwt_audience or f"{self.public_url.rstrip('/')}/mcp").rstrip("/")

    @property
    def allowed_ids(self) -> set[int]:
        return {int(x) for x in self.allowed_telegram_ids.replace(" ", "").split(",") if x}

    @property
    def alert_thresholds(self) -> list[int]:
        return sorted({int(x) for x in self.budget_alert_thresholds.split(",") if x.strip()})

    @property
    def hosts(self) -> list[str]:
        from urllib.parse import urlparse
        own = urlparse(self.public_url).netloc
        base = [own, "localhost:*", "127.0.0.1:*", "mcp:*", "mcp_server:*"] + ([own.split(":")[0]] if own else [])
        return list(dict.fromkeys([*base, *[h.strip() for h in self.allowed_hosts.split(",") if h.strip()]]))

    @property
    def origins(self) -> set[str]:
        return {o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
