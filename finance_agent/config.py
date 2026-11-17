"""Settings of the Finance Agent. It has its own database, its own Telegram bot (for the manager) and its own secrets."""
from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "finance_agent/.env"), env_prefix="FINANCE_", extra="ignore")

    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://expense:expense@localhost:5432/expense_finance"
    auto_create_tables: bool = True

    # where we are on the network
    host: str = "0.0.0.0"
    port: int = 8002
    public_url: str = "http://localhost:8002"  # the A2A endpoint is <public_url>/a2a, the agent card <public_url>/.well-known/agent-card.json

    # the manager's Telegram bot (a different bot from the employees' expense bot)
    telegram_bot_token: str = ""
    telegram_mode: str = "polling"
    allowed_manager_ids: str = ""  # comma list of Telegram ids that may set up a company; empty = anyone who messages the bot

    # who may call us: the expense tracker signs a short-lived token for every call; we check it against its public keys
    trusted_issuer: str = "http://mcp:8001"
    trusted_jwks_url: str = "http://mcp:8001/.well-known/jwks.json"

    # how we tell the expense tracker about decisions (a signed webhook)
    tracker_webhook_url: str = "http://mcp:8001/a2a/webhook"
    webhook_secret: str = ""

    # the LLM that reads policies (same CodeVector gateway the expense bot uses)
    codevector_api_key: str = Field("", validation_alias=AliasChoices("FINANCE_CODEVECTOR_API_KEY", "CODEVECTOR_API_KEY"))
    codevector_model: str = Field("dev-model-alpha", validation_alias=AliasChoices("FINANCE_CODEVECTOR_MODEL", "CODEVECTOR_MODEL"))
    codevector_base_url: str = Field(
        "https://coding-gateway.fissionlabs.com/gateway/openai/v1", validation_alias=AliasChoices("FINANCE_CODEVECTOR_BASE_URL", "CODEVECTOR_BASE_URL")
    )
    llm_timeout: int = 45  # a quick answer (a question, one item)
    policy_timeout: int = 150  # reading or editing a whole policy writes a lot of JSON: it is allowed longer

    currency_symbol: str = Field("₹", validation_alias=AliasChoices("FINANCE_CURRENCY_SYMBOL", "CURRENCY_SYMBOL"))

    @field_validator("*", mode="before")
    @classmethod
    def _strip_inline_comment(cls, v):
        if isinstance(v, str) and "BEGIN " not in v:
            v = v.split(" #")[0].strip().strip("\"'").strip()
        return v

    @property
    def audience(self) -> str:
        return self.public_url.rstrip("/") + "/a2a"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
