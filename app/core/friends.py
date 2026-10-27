"""Users and each person's friend list (nickname -> Telegram id)."""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import FriendError
from app.db.models import Friend, User

ME_WORDS = {"me", "i", "myself", "self", "mine"}


async def upsert_user(
    session: AsyncSession, telegram_id: int, name: str | None = None, username: str | None = None, started: bool = False
) -> User:
    user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    if user is None:
        await session.execute(
            pg_insert(User).values(telegram_id=telegram_id, name=name, username=username).on_conflict_do_nothing(index_elements=["telegram_id"])
        )
        user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one()
    if name and user.name != name:
        user.name = name
    if username and user.username != username:
        user.username = username
    if started and user.started_at is None:
        user.started_at = datetime.now(timezone.utc)
    await session.flush()
    return user


async def get_user(session: AsyncSession, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise FriendError("I don't know that user.")
    return user


def display(user: User) -> str:
    return user.name or (f"@{user.username}" if user.username else f"user {user.telegram_id}")


async def add_friend(session: AsyncSession, owner_id: int, telegram_id: int, nickname: str) -> tuple[Friend, User, bool]:
    """Returns (friend row, the friend's user, created). Re-adding the same person just renames them."""
    nickname = " ".join((nickname or "").split())[:64]
    if not nickname or nickname.lower() in ME_WORDS:
        raise FriendError("Please give your friend a name, e.g. 'add friend Surya 123456789'.")
    if not isinstance(telegram_id, int) or telegram_id <= 0:
        raise FriendError("That Telegram id doesn't look right. It is a number like 123456789 (your friend can get it from @userinfobot).")
    owner = await get_user(session, owner_id)
    if owner.telegram_id == telegram_id:
        raise FriendError("That's your own Telegram id.")

    friend_user = await upsert_user(session, telegram_id)
    existing = (await session.execute(select(Friend).where(Friend.owner_id == owner_id, Friend.friend_id == friend_user.id))).scalar_one_or_none()
    clash = (await session.execute(
        select(Friend).where(Friend.owner_id == owner_id, func.lower(Friend.nickname) == nickname.lower())
    )).scalar_one_or_none()
    if clash is not None and (existing is None or clash.id != existing.id):
        raise FriendError(f"You already have a friend called '{clash.nickname}'. Pick a different name.")
    if existing:
        existing.nickname = nickname
        await session.flush()
        return existing, friend_user, False
    row = Friend(owner_id=owner_id, friend_id=friend_user.id, nickname=nickname)
    session.add(row)
    await session.flush()
    return row, friend_user, True


async def list_friends(session: AsyncSession, owner_id: int) -> list[tuple[Friend, User]]:
    rows = await session.execute(
        select(Friend, User).join(User, User.id == Friend.friend_id).where(Friend.owner_id == owner_id).order_by(func.lower(Friend.nickname))
    )
    return [(f, u) for f, u in rows.all()]


async def remove_friend(session: AsyncSession, owner_id: int, nickname: str) -> str:
    friend, _ = await _find(session, owner_id, nickname)
    name = friend.nickname
    await session.delete(friend)
    return name


async def _find(session: AsyncSession, owner_id: int, name: str) -> tuple[Friend, User]:
    friends = await list_friends(session, owner_id)
    key = (name or "").strip().lower()
    exact = [(f, u) for f, u in friends if f.nickname.lower() == key]
    if exact:
        return exact[0]
    prefix = [(f, u) for f, u in friends if f.nickname.lower().startswith(key)] if key else []
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        raise FriendError(f"'{name}' matches more than one friend: {', '.join(f.nickname for f, _ in prefix)}. Which one?")
    raise FriendError(f"I don't have a friend called '{name}'. Add them first, e.g. 'add friend {name} 123456789'.")


async def resolve_user(session: AsyncSession, owner_id: int, name: str) -> User:
    """'me' -> the owner; otherwise a friend by nickname (exact, then unique prefix)."""
    if (name or "").strip().lower() in ME_WORDS:
        return await get_user(session, owner_id)
    return (await _find(session, owner_id, name))[1]


async def nickname_of(session: AsyncSession, viewer_id: int, other: User) -> str:
    """How `viewer` refers to `other`: their own nickname for them, else the person's Telegram name."""
    nick = (await session.execute(select(Friend.nickname).where(Friend.owner_id == viewer_id, Friend.friend_id == other.id))).scalar_one_or_none()
    return nick or display(other)


async def is_friend_of_anyone(session: AsyncSession, telegram_id: int) -> bool:
    row = (await session.execute(
        select(Friend.id).join(User, User.id == Friend.friend_id).where(User.telegram_id == telegram_id).limit(1)
    )).first()
    return row is not None
