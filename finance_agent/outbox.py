"""
Everything this service says to somebody else is written down first, in the same transaction as the change that caused it,
and delivered afterwards by a background loop. So a crash or a network error can't lose a decision.

    to_manager   a Telegram message for the manager
    to_tracker   a signed webhook call to the expense tracker (decisions, questions for the employee)
"""
from sqlalchemy.ext.asyncio import AsyncSession

from finance_agent.db.models import Notification, WebhookEvent


async def to_manager(session: AsyncSession, telegram_id: int, text: str, buttons: list[list[dict]] | None = None) -> None:
    session.add(Notification(telegram_id=telegram_id, text=text[:4096], buttons=buttons))
    await session.flush()


async def to_tracker(session: AsyncSession, payload: dict) -> None:
    session.add(WebhookEvent(payload=payload))
    await session.flush()
