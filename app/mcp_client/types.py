"""The few types the bot shares with the MCP client layer."""
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Identity:
    """Who the bot is acting for, and where. The MCP server turns this into a session token (the bot never forges one)."""
    telegram_id: int
    name: str | None = None
    username: str | None = None
    chat_type: str = "private"  # private | group
    group_chat_id: int | None = None
    group_title: str | None = None
    started: bool = False  # True when the person wrote to the bot in a private chat (so it can message them back)

    @property
    def key(self) -> tuple:
        return (self.telegram_id, self.chat_type, self.group_chat_id, self.started)


@dataclass
class ToolResult:
    """What every MCP tool returns (see mcp_server/tools/registry.py)."""
    status: str  # ok | input_required | rejected | error
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    input_requests: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, str] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def message(self) -> str:
        """Text to show a human, whatever the status."""
        if self.status == "error":
            return (self.error or {}).get("message") or "Something went wrong."
        return self.text


class McpError(Exception):
    """The MCP server could not be reached or answered with something unusable."""


class NotAllowed(Exception):
    """The server refused to open a session: this person may not use the system."""


class NoGroup(Exception):
    """The server refused to open a group session: this chat is not set up and this person may not set it up."""
