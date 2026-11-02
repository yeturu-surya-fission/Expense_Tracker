"""Settings of the Telegram bot. It holds the Telegram token, the LLM key, and the credentials to reach the MCP server: nothing else."""
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
    bot_username: str = ""  # filled in at startup from Telegram

    # llm
    llm_api_key: str = ""
    llm_provider: str = "auto"
    llm_model: str = ""
    llm_temperature: float = 0.0
    google_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    groq_api_key: str = ""

    # the MCP server: the ONLY way this process reaches expense data
    mcp_server_url: str = "http://localhost:8001/mcp"
    mcp_auth_url: str = ""  # base URL of the token endpoints; defaults to the MCP URL without its /mcp path
    mcp_bot_client_id: str = "telegram-bot"
    mcp_bot_client_secret: str = ""
    mcp_timeout_seconds: float = 30.0

    # the bot's own database: only the LangGraph conversation state (chat history + paused questions)
    bot_database_url: str = "postgresql://expense:expense@localhost:5432/expense_bot"

    timezone: str = "Asia/Kolkata"  # only for the month buttons and the date in the prompt; the server does the real maths
    log_level: str = "INFO"
    outbox_poll_seconds: float = 1.0

    @field_validator("*", mode="before")
    @classmethod
    def _strip_inline_comment(cls, v):
        """Docker's env_file keeps 'VALUE   # comment' whole; drop the comment and stray quotes."""
        if isinstance(v, str):
            v = v.split(" #")[0].strip().strip("\"'").strip()
        return v

    @property
    def auth_base_url(self) -> str:
        if self.mcp_auth_url:
            return self.mcp_auth_url.rstrip("/")
        url = self.mcp_server_url.rstrip("/")
        return url[: -len("/mcp")] if url.endswith("/mcp") else url

    @property
    def checkpointer_url(self) -> str:
        """psycopg-style DSN for the LangGraph Postgres checkpointer."""
        return self.bot_database_url.replace("postgresql+asyncpg://", "postgresql://")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
