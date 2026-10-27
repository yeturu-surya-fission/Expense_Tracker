"""Groups: a Telegram group chat where several people add expenses, then settle up once."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import friends
from app.core.errors import GroupError
from app.core.splits import compute_transfers, log_user_shares
from app.db.models import Debt, Expense, Group, GroupMember, Share, SharedExpense, User


def _clean_title(title: str | None) -> str:
    return " ".join((title or "").split())[:128] or "Group"


async def ensure_group(session: AsyncSession, chat_id: int, title: str | None, creator_id: int) -> tuple[Group, bool]:
    """The expense group for a Telegram chat, created the first time the bot is added. Returns (group, created)."""
    group = await group_for_chat(session, chat_id)
    if group is not None:
        return group, False
    group = Group(chat_id=chat_id, name=_clean_title(title), created_by=creator_id)
    session.add(group)
    await session.flush()
    session.add(GroupMember(group_id=group.id, user_id=creator_id))
    await session.flush()
    return group, True


async def rename_group(session: AsyncSession, chat_id: int, title: str | None) -> None:
    group = await group_for_chat(session, chat_id)
    if group is not None and title:
        group.name = _clean_title(title)
        await session.flush()


async def move_group(session: AsyncSession, old_chat_id: int, new_chat_id: int) -> bool:
    """Telegram turns a group into a supergroup (new chat id) when it grows: keep the same expense group."""
    group = await group_for_chat(session, old_chat_id)
    if group is None:
        return False
    group.chat_id = new_chat_id
    await session.flush()
    return True


async def mark_left(session: AsyncSession, group_id: int, user_id: int) -> None:
    """They left the chat: keep their past shares, but leave them out of future 'everyone' splits."""
    row = (await session.execute(select(GroupMember).where(GroupMember.group_id == group_id, GroupMember.user_id == user_id))).scalar_one_or_none()
    if row is not None and row.left_at is None:
        row.left_at = datetime.now(timezone.utc)
        await session.flush()


async def group_for_chat(session: AsyncSession, chat_id: int) -> Group | None:
    return (await session.execute(select(Group).where(Group.chat_id == chat_id))).scalar_one_or_none()


async def get_group(session: AsyncSession, group_id: int) -> Group:
    group = await session.get(Group, group_id)
    if group is None:
        raise GroupError("I can't find that group.")
    return group


async def join_group(session: AsyncSession, group_id: int, user_id: int) -> bool:
    """True if newly added (or back after leaving), False if they were already in."""
    row = (await session.execute(select(GroupMember).where(GroupMember.group_id == group_id, GroupMember.user_id == user_id))).scalar_one_or_none()
    if row is not None:
        if row.left_at is None:
            return False
        row.left_at = None
        await session.flush()
        return True
    session.add(GroupMember(group_id=group_id, user_id=user_id))
    await session.flush()
    return True


async def members(session: AsyncSession, group_id: int) -> list[User]:
    rows = await session.execute(
        select(User).join(GroupMember, GroupMember.user_id == User.id).where(GroupMember.group_id == group_id, GroupMember.left_at.is_(None)).order_by(GroupMember.id)
    )
    return list(rows.scalars().all())


async def is_member(session: AsyncSession, group_id: int, user_id: int) -> bool:
    return (await session.execute(
        select(GroupMember.id).where(GroupMember.group_id == group_id, GroupMember.user_id == user_id, GroupMember.left_at.is_(None))
    )).first() is not None


async def is_member_anywhere(session: AsyncSession, user_id: int) -> bool:
    """Is this person in any expense group? (Group members may use the bot, even if they are not on the allow-list.)"""
    return (await session.execute(
        select(GroupMember.id).where(GroupMember.user_id == user_id, GroupMember.left_at.is_(None)).limit(1)
    )).first() is not None


async def set_default_split(session: AsyncSession, group_id: int, mode: str | None) -> None:
    (await get_group(session, group_id)).default_split = mode
    await session.flush()


async def resolve_member(session: AsyncSession, group: Group, viewer_id: int, name: str) -> User:
    """'me', the viewer's own nickname for a friend, or a member's Telegram name / @username."""
    key = (name or "").strip().lower().lstrip("@")
    if key in friends.ME_WORDS:
        return await friends.get_user(session, viewer_id)
    people = await members(session, group.id)
    nick_of = {u.id: (await friends.nickname_of(session, viewer_id, u)).lower() for u in people}

    def names(u: User) -> list[str]:
        return [n for n in (nick_of[u.id], (u.name or "").lower(), (u.username or "").lower()) if n]

    exact = [u for u in people if key in names(u)]
    if len(exact) == 1:
        return exact[0]
    pre = [u for u in people if any(n.startswith(key) or key in n.split() for n in names(u))] if key else []
    if len(pre) == 1:
        return pre[0]
    if len(pre) > 1 or len(exact) > 1:
        raise GroupError(f"'{name}' matches more than one member. Please be more specific.")
    raise GroupError(f"'{name}' isn't in {group.name}. Members: {', '.join(friends.display(u) for u in people)}.")


@dataclass
class GroupSummary:
    group: Group
    people: list[User]
    paid: dict[int, Decimal]
    share: dict[int, Decimal]
    expenses: list[tuple[SharedExpense, User]] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((e.amount for e, _ in self.expenses), Decimal(0))

    def net(self) -> dict[int, Decimal]:
        ids = set(self.paid) | set(self.share)
        return {u: self.paid.get(u, Decimal(0)) - self.share.get(u, Decimal(0)) for u in ids}


async def _open_expenses(session: AsyncSession, group_id: int, lock: bool = False) -> list[SharedExpense]:
    q = select(SharedExpense).where(SharedExpense.group_id == group_id, SharedExpense.voided_at.is_(None), SharedExpense.settled_at.is_(None)).order_by(SharedExpense.id)
    if lock:
        q = q.with_for_update()
    return list((await session.execute(q)).scalars().all())


async def summary(session: AsyncSession, group_id: int) -> GroupSummary:
    group = await get_group(session, group_id)
    people = await members(session, group_id)
    expenses = await _open_expenses(session, group_id)
    users = {u.id: u for u in people}
    paid: dict[int, Decimal] = {}
    out = []
    for e in expenses:
        paid[e.payer_id] = paid.get(e.payer_id, Decimal(0)) + e.amount
        payer = users.get(e.payer_id) or await friends.get_user(session, e.payer_id)
        out.append((e, payer))
    share: dict[int, Decimal] = {}
    if expenses:
        rows = await session.execute(select(Share.user_id, func.sum(Share.amount)).where(Share.shared_expense_id.in_([e.id for e in expenses])).group_by(Share.user_id))
        share = {uid: Decimal(total) for uid, total in rows.all()}
    return GroupSummary(group, people, paid, share, out)


@dataclass
class SettleResult:
    group: Group
    people: list[User]
    transfers: list[tuple[User, User, Decimal]]
    debts: list[Debt]
    nets: dict[int, Decimal]
    logged: dict[int, list[Expense]]  # expenses recorded right now for people who owe nothing
    total: Decimal
    count: int
    share_items: dict[int, list[tuple[str, Decimal]]] = field(default_factory=dict)
    paid_items: dict[int, list[tuple[str, Decimal]]] = field(default_factory=dict)


async def settle(session: AsyncSession, group_id: int) -> SettleResult:
    group = await get_group(session, group_id)
    expenses = await _open_expenses(session, group_id, lock=True)
    if not expenses:
        raise GroupError(f"Nothing to settle in {group.name} yet. Add an expense first.")

    paid: dict[int, Decimal] = {}
    for e in expenses:
        paid[e.payer_id] = paid.get(e.payer_id, Decimal(0)) + e.amount
    rows = await session.execute(select(Share.user_id, func.sum(Share.amount)).where(Share.shared_expense_id.in_([e.id for e in expenses])).group_by(Share.user_id))
    share = {uid: Decimal(total) for uid, total in rows.all()}
    ids = set(paid) | set(share)
    nets = {u: paid.get(u, Decimal(0)) - share.get(u, Decimal(0)) for u in ids}
    transfers = compute_transfers(nets)

    paid_items: dict[int, list[tuple[str, Decimal]]] = {}
    for e in expenses:
        paid_items.setdefault(e.payer_id, []).append((e.description, e.amount))
    share_items: dict[int, list[tuple[str, Decimal]]] = {}
    for uid, desc, amount in (await session.execute(
        select(Share.user_id, SharedExpense.description, Share.amount)
        .join(SharedExpense, SharedExpense.id == Share.shared_expense_id)
        .where(Share.shared_expense_id.in_([e.id for e in expenses])).order_by(SharedExpense.id)
    )).all():
        share_items.setdefault(uid, []).append((desc, amount))

    now = datetime.now(timezone.utc)
    for e in expenses:
        e.settled_at = now
    await session.flush()

    users = {u.id: u for u in [await friends.get_user(session, i) for i in ids]}
    debts = []
    for debtor, creditor, amount in transfers:
        d = Debt(debtor_id=debtor, creditor_id=creditor, amount=amount, description=group.name, kind="group", group_id=group_id)
        session.add(d)
        debts.append(d)
    await session.flush()

    # people who pay nobody are "complete": their share goes into their own expenses right away
    owing = {t[0] for t in transfers}
    logged = {uid: await log_user_shares(session, uid, group_id=group_id) for uid in ids if uid not in owing}
    return SettleResult(
        group, [users[i] for i in sorted(ids)], [(users[a], users[b], amt) for a, b, amt in transfers],
        debts, nets, logged, sum((e.amount for e in expenses), Decimal(0)), len(expenses), share_items, paid_items,
    )
