"""Who may use the system. Policy lives here, in the server, so every client gets the same answer."""
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.config import settings
from mcp_server.domain import friends, groups


def can_add_bot(telegram_id: int) -> bool:
    """Only the owner side (the allow-list; everyone if it is empty) may add the bot to a group."""
    return not settings.allowed_ids or telegram_id in settings.allowed_ids


async def allowed(session: AsyncSession, telegram_id: int) -> bool:
    """
    May this person use the system in a private chat? The allow-list, friends of anybody, and anyone who is in an
    expense group (everyone in a group the owner added the bot to can use it).
    """
    if not settings.allowed_ids or telegram_id in settings.allowed_ids:
        return True
    if await friends.is_friend_of_anyone(session, telegram_id):
        return True
    user = await friends.upsert_user(session, telegram_id)
    return await groups.is_member_anywhere(session, user.id)
