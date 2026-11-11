from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # telegram
    telegram_bot_token: str = ""
    telegram_mode: Literal["polling", "webhook"] = "polling"
    public_url: str = ""
    telegram_webhook_secret: str = ""
    allowed_telegram_ids: str = ""
    bot_username: str = ""  # filled in at startup from Telegram

    # codevector (the only LLM gateway)
    codevector_api_key: str = ""
    codevector_base_url: str = ""
    codevector_model: str = ""
    llm_temperature: float = 0.0

    # database (a NEW database: the old cash/bank schema is not reused)
    database_url: str = "postgresql+asyncpg://expense:expense@localhost:5432/expense_tracker"

    # mcp client (the backend agent talks to the MCP server over Streamable HTTP)
    mcp_server_url: str = "http://localhost:9000/mcp"

    # mcp auth (backend signs tokens, mcp server verifies them)
    mcp_private_key: str = ""  # base64 Ed25519 private key — backend process only
    mcp_public_key: str = ""   # base64 Ed25519 public key — mcp server process only

    # behaviour
    currency_symbol: str = "₹"
    timezone: str = "Asia/Kolkata"
    budget_alert_thresholds: str = "50,80,100"
    reminder_every_hours: int = 48  # how often an unpaid share is re-sent to the person who owes it
    ask_group_min_people: int = 4  # a split with this many people asks "one-off or create a group?"
    log_level: str = "INFO"

    @field_validator("*", mode="before")
    @classmethod
    def _strip_inline_comment(cls, v):
        """Docker's env_file keeps 'VALUE   # comment' whole; drop the comment and stray quotes."""
        if isinstance(v, str):
            v = v.split(" #")[0].strip().strip("\"'").strip()
        return v

    @property
    def allowed_ids(self) -> set[int]:
        return {int(x) for x in self.allowed_telegram_ids.replace(" ", "").split(",") if x}

    @property
    def alert_thresholds(self) -> list[int]:
        return sorted({int(x) for x in self.budget_alert_thresholds.split(",") if x.strip()})

    @property
    def checkpointer_url(self) -> str:
        """psycopg-style DSN for the LangGraph Postgres checkpointer."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
