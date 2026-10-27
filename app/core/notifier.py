"""
How the core talks to people other than the one chatting (friends being told 'you owe 750').

The core never imports Telegram: the bot installs a Notifier at startup (app/bot/notifier.py) and tests install a fake.
"""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Button:
    label: str
    data: str | None = None  # callback data
    url: str | None = None


class Notifier(Protocol):
    async def send(self, telegram_id: int, text: str, buttons: list[list[Button]] | None = None) -> int | None:
        """Send a private message. Returns the message id, or None if it could not be delivered."""

    async def delete(self, telegram_id: int, message_id: int) -> None: ...

    async def member_count(self, chat_id: int) -> int | None:
        """How many people (including bots) are in a group chat, or None if unknown."""

    async def chat_member_status(self, chat_id: int, telegram_id: int) -> str | None:
        """'member', 'administrator', 'creator', 'left', ... for one person in a chat, or None if unknown."""


class NullNotifier:
    async def send(self, telegram_id, text, buttons=None):
        return None

    async def delete(self, telegram_id, message_id):
        return None

    async def member_count(self, chat_id):
        return None

    async def chat_member_status(self, chat_id, telegram_id):
        return None


_current: Notifier = NullNotifier()


def set_notifier(notifier: Notifier) -> None:
    global _current
    _current = notifier


def get_notifier() -> Notifier:
    return _current
