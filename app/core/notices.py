"""Every message the bot sends to someone other than the person who is chatting. Wording lives here."""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import friends
from app.core.formatting import money
from app.core.notifier import Button, get_notifier
from app.db.models import Debt, Group, User


def start_link(param: str = "") -> str:
    if not settings.bot_username:
        return ""
    return f"https://t.me/{settings.bot_username}" + (f"?start={param}" if param else "")


def not_started_hint(user_names: list[str]) -> str:
    link = start_link()
    who = ", ".join(user_names)
    return f"⚠️ {who} hasn't started the bot yet, so I couldn't message them. Ask them to open {link or 'the bot'} and press Start. I'll deliver it then."


async def _send(user: User, text: str, buttons: list[list[Button]] | None = None) -> int | None:
    if user.started_at is None:
        return None
    return await get_notifier().send(user.telegram_id, text, buttons)


def _pay_buttons(debt_id: int) -> list[list[Button]]:
    return [[Button("✅ I paid", data=f"due:{debt_id}:paid")]]


def _receive_buttons(debt_id: int) -> list[list[Button]]:
    return [[Button("✅ Received", data=f"due:{debt_id}:got"), Button("✖ Not yet", data=f"due:{debt_id}:notyet")]]


async def _debt_text(session: AsyncSession, debt: Debt, creditor: User, reminder: bool = False) -> str:
    who = await friends.nickname_of(session, debt.debtor_id, creditor)
    head = "⏰ Reminder: " if reminder else ""
    note = f"\n({debt.detail})" if debt.detail else ""
    if debt.kind != "quick":
        return f"{head}🧾 {debt.description} is settled up.\nYou owe {who} {money(debt.amount)}.{note}"
    return f"{head}💸 {who} paid for {debt.description}.\nYour share: {money(debt.amount)}.{note}"


async def notify_new_debts(session: AsyncSession, debts: list[Debt], extras: dict[int, str] | None = None) -> dict[int, bool]:
    """Tell each debtor what they owe. `extras` = {debtor id: bracket text to show under the amount}. Returns {debt id: delivered}."""
    out = {}
    for d in debts:
        debtor, creditor = await friends.get_user(session, d.debtor_id), await friends.get_user(session, d.creditor_id)
        if extras and extras.get(d.debtor_id):
            detail = extras[d.debtor_id]
            d.detail = detail if len(detail) <= 500 else detail[:497] + "..."  # remembered for late delivery and reminders
        msg_id = await _send(debtor, await _debt_text(session, d, creditor), _pay_buttons(d.id))
        if msg_id:
            d.debtor_msg_id = msg_id
        out[d.id] = bool(msg_id)
    await session.flush()
    return out


async def notify_claim(session: AsyncSession, debt: Debt) -> bool:
    """Tell the creditor that the debtor says they paid."""
    debtor, creditor = await friends.get_user(session, debt.debtor_id), await friends.get_user(session, debt.creditor_id)
    who = await friends.nickname_of(session, debt.creditor_id, debtor)
    text = f"💰 {who} says they paid you {money(debt.amount)} for {debt.description}.\nDid you receive it?"
    msg_id = await _send(creditor, text, _receive_buttons(debt.id))
    if msg_id:
        debt.creditor_msg_id = msg_id
    await session.flush()
    return bool(msg_id)


async def notify_confirmed(session: AsyncSession, debt: Debt) -> None:
    debtor, creditor = await friends.get_user(session, debt.debtor_id), await friends.get_user(session, debt.creditor_id)
    who = await friends.nickname_of(session, debt.debtor_id, creditor)
    await _send(debtor, f"🎉 {who} confirmed your payment of {money(debt.amount)} for {debt.description}. All settled!")


async def notify_rejected(session: AsyncSession, debt: Debt) -> None:
    debtor, creditor = await friends.get_user(session, debt.debtor_id), await friends.get_user(session, debt.creditor_id)
    who = await friends.nickname_of(session, debt.debtor_id, creditor)
    text = f"⚠️ {who} says they haven't received {money(debt.amount)} for {debt.description} yet.\nIt's back on your list, and I took it out of your expenses."
    msg_id = await _send(debtor, text, _pay_buttons(debt.id))
    if msg_id:
        debt.debtor_msg_id = msg_id
    await session.flush()


async def notify_cancelled(session: AsyncSession, debts: list[Debt], by: User) -> None:
    for d in debts:
        debtor = await friends.get_user(session, d.debtor_id)
        if d.debtor_msg_id and debtor.started_at:
            await get_notifier().delete(debtor.telegram_id, d.debtor_msg_id)
        who = await friends.nickname_of(session, d.debtor_id, by)
        await _send(debtor, f"↩️ {who} cancelled the split for {d.description}. You don't owe {money(d.amount)} any more.")


async def notify_group_settlement(session: AsyncSession, group: Group, people: list[User], transfers: list[tuple[User, User, Decimal]], debts: list[Debt]) -> list[str]:
    return await notify_settlement(session, group.name, people, transfers, debts)


async def notify_settlement(
    session: AsyncSession, title: str, people: list[User], transfers: list[tuple[User, User, Decimal]], debts: list[Debt],
    breakdown: dict[int, str] | None = None,
) -> list[User]:
    """
    Send everyone their private message for a settled-up group or party: the people who owe get their bill, the people
    owed get a summary, everyone gets the bills behind their total. Returns the people who could NOT be messaged
    (they never pressed Start): they get their bill the moment they do.
    """
    breakdown = breakdown or {}
    unreachable: dict[int, User] = {}
    debtor_ids = {t[0].id for t in transfers}
    await notify_new_debts(session, debts, breakdown)
    for d in debts:
        if not d.debtor_msg_id:
            user = await friends.get_user(session, d.debtor_id)
            unreachable[user.id] = user
    for person in people:
        if person.id in debtor_ids:
            continue
        owed = [(debtor, amt) for debtor, creditor, amt in transfers if creditor.id == person.id]
        if owed:
            lines = [f"• {await friends.nickname_of(session, person.id, debtor)} owes you {money(amt)}" for debtor, amt in owed]
            text = f"🧾 {title} is settled up. You are owed:\n" + "\n".join(lines) + "\nI'll ask you to confirm when they pay."
        else:
            text = f"🧾 {title} is settled up. You're all even, so nobody needs to pay you. Your share is saved in your expenses."
        if breakdown.get(person.id):
            text += f"\n({breakdown[person.id]})"
        if not await _send(person, text):
            unreachable[person.id] = person
    return list(unreachable.values())


async def send_reminders(session: AsyncSession, pending: list[Debt], claimed: list[Debt]) -> int:
    """Re-send unpaid shares to debtors and 'did you receive it?' to creditors. Returns how many went out."""
    sent = 0
    now = datetime.now(timezone.utc)
    for d in pending:
        debtor, creditor = await friends.get_user(session, d.debtor_id), await friends.get_user(session, d.creditor_id)
        if d.debtor_msg_id and debtor.started_at:
            await get_notifier().delete(debtor.telegram_id, d.debtor_msg_id)
        msg_id = await _send(debtor, await _debt_text(session, d, creditor, reminder=True), _pay_buttons(d.id))
        if msg_id:
            d.debtor_msg_id, d.last_reminded_at, sent = msg_id, now, sent + 1
    for d in claimed:
        debtor, creditor = await friends.get_user(session, d.debtor_id), await friends.get_user(session, d.creditor_id)
        who = await friends.nickname_of(session, d.creditor_id, debtor)
        text = f"⏰ Reminder: {who} said they paid you {money(d.amount)} for {d.description}. Did you receive it?"
        if d.creditor_msg_id and creditor.started_at:
            await get_notifier().delete(creditor.telegram_id, d.creditor_msg_id)
        msg_id = await _send(creditor, text, _receive_buttons(d.id))
        if msg_id:
            d.creditor_msg_id, d.last_reminded_at, sent = msg_id, now, sent + 1
    await session.flush()
    return sent


async def deliver_pending(session: AsyncSession, user_id: int) -> int:
    """A friend just pressed Start: send them everything that was waiting for them."""
    sent = 0
    waiting_debts = (await session.execute(select(Debt).where(Debt.debtor_id == user_id, Debt.status == "pending", Debt.debtor_msg_id.is_(None)))).scalars().all()
    if waiting_debts:
        sent += sum((await notify_new_debts(session, list(waiting_debts))).values())
    waiting_claims = (await session.execute(select(Debt).where(Debt.creditor_id == user_id, Debt.status == "claimed", Debt.creditor_msg_id.is_(None)))).scalars().all()
    for d in waiting_claims:
        sent += int(await notify_claim(session, d))
    return sent


def group_setup_help(name: str | None = None) -> str:
    """How to start a trip/event group (a bot cannot create Telegram groups, so the user does that part)."""
    bot = settings.bot_username or "me"
    link = f"https://t.me/{settings.bot_username}?startgroup=true" if settings.bot_username else ""
    return "\n".join([
        "🧳 Trips and groups are simple:",
        "1. Create a Telegram group and add your friends.",
        f"2. Add me to the group{' (' + link + ')' if link else ''}. That's all: I set myself up.",
        f"3. Anyone in the group can now mention me, e.g. '@{bot} I paid 1000 for petrol'.",
        f"4. When you're done, say '@{bot} split the bill'. I post who owes what and send each person their bill privately.",
    ])
