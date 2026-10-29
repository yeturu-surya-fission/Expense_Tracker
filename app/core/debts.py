"""
The life of a debt:   pending --(debtor taps "I paid")--> claimed --(creditor taps "Received")--> settled
                                                           `--(creditor taps "Not yet")--> back to pending

When the debtor says they paid, their share is recorded in THEIR expenses straight away. If the creditor then says
they have not received it, that expense is voided again and the debt returns to the debtor's list.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import friends
from app.core.errors import DebtError
from app.core.splits import log_user_shares
from app.db.models import Debt, Expense, Share


async def _lock(session: AsyncSession, debt_id: int) -> Debt:
    debt = (await session.execute(select(Debt).where(Debt.id == debt_id).with_for_update())).scalar_one_or_none()
    if debt is None:
        raise DebtError("I can't find that payment any more.")
    return debt


async def claim(session: AsyncSession, debt_id: int, by_user_id: int) -> tuple[Debt, list[Expense]]:
    """The debtor says 'I paid'. Records their share as their own expense."""
    debt = await _lock(session, debt_id)
    if debt.debtor_id != by_user_id:
        raise DebtError("That payment isn't yours to mark.")
    if debt.status == "claimed":
        raise DebtError("You already marked this as paid. Waiting for the other person to confirm.")
    if debt.status == "settled":
        raise DebtError("This one is already settled.")
    if debt.status == "cancelled":
        raise DebtError("This bill was cancelled, so there is nothing to pay.")
    if debt.kind == "quick":
        expenses = await log_user_shares(session, debt.debtor_id, shared_expense_id=debt.shared_expense_id)
    elif debt.kind == "bundle":
        expenses = await log_user_shares(session, debt.debtor_id, bundle_id=debt.bundle_id)
    else:
        expenses = await log_user_shares(session, debt.debtor_id, group_id=debt.group_id)
    debt.status = "claimed"
    debt.claimed_at = datetime.now(timezone.utc)
    debt.expense_ids = [e.id for e in expenses]
    await session.flush()
    return debt, expenses


async def confirm(session: AsyncSession, debt_id: int, by_user_id: int) -> Debt:
    """The creditor says 'Received'."""
    debt = await _lock(session, debt_id)
    if debt.creditor_id != by_user_id:
        raise DebtError("Only the person who is owed can confirm this.")
    if debt.status == "settled":
        raise DebtError("You already confirmed this one.")
    if debt.status != "claimed":
        raise DebtError("They haven't said they paid yet.")
    debt.status = "settled"
    debt.settled_at = datetime.now(timezone.utc)
    await session.flush()
    return debt


async def reject(session: AsyncSession, debt_id: int, by_user_id: int) -> Debt:
    """The creditor says 'Not yet': put it back on the debtor's list and take the expense out of their records."""
    debt = await _lock(session, debt_id)
    if debt.creditor_id != by_user_id:
        raise DebtError("Only the person who is owed can do this.")
    if debt.status != "claimed":
        raise DebtError("There is nothing to reject: this one isn't marked as paid.")
    ids = list(debt.expense_ids or [])
    # A person who owes several people records their whole share once, on their first payment. If another payment of
    # theirs in the same settlement still stands, the expense stays and moves over to that payment.
    scope = (Debt.bundle_id == debt.bundle_id) if debt.kind == "bundle" else (Debt.group_id == debt.group_id)
    other = None
    if debt.kind in ("bundle", "group"):
        other = (await session.execute(
            select(Debt).where(Debt.debtor_id == debt.debtor_id, Debt.id != debt.id, Debt.status.in_(("claimed", "settled")), scope).limit(1).with_for_update()
        )).scalar_one_or_none()
    if ids and other is not None:
        other.expense_ids = list(dict.fromkeys([*(other.expense_ids or []), *ids]))
        ids = []
    if ids:
        now = datetime.now(timezone.utc)
        for exp in (await session.execute(select(Expense).where(Expense.id.in_(ids)))).scalars().all():
            exp.voided_at = now
        await session.execute(update(Share).where(Share.expense_id.in_(ids)).values(expense_id=None))
    debt.status = "pending"
    debt.claimed_at = None
    debt.expense_ids = []
    debt.creditor_msg_id = None
    await session.flush()
    return debt


async def dues(session: AsyncSession, user_id: int) -> tuple[list[Debt], list[Debt]]:
    """(what I owe, what I am owed): everything not yet settled."""
    live = ("pending", "claimed")
    owe = (await session.execute(select(Debt).where(Debt.debtor_id == user_id, Debt.status.in_(live)).order_by(Debt.id))).scalars().all()
    owed = (await session.execute(select(Debt).where(Debt.creditor_id == user_id, Debt.status.in_(live)).order_by(Debt.id))).scalars().all()
    return list(owe), list(owed)


async def debts_needing_reminder(session: AsyncSession, every_hours: int, now: datetime | None = None) -> tuple[list[Debt], list[Debt]]:
    """(unpaid debts to nudge the debtor about, 'did you receive it?' debts to nudge the creditor about)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=every_hours)
    due = or_(Debt.last_reminded_at.is_(None) & (Debt.created_at <= cutoff), Debt.last_reminded_at <= cutoff)
    pending = (await session.execute(select(Debt).where(Debt.status == "pending", due).order_by(Debt.id))).scalars().all()
    claimed = (await session.execute(select(Debt).where(Debt.status == "claimed", due).order_by(Debt.id))).scalars().all()
    return list(pending), list(claimed)


async def name_pair(session: AsyncSession, debt: Debt):
    return await friends.get_user(session, debt.debtor_id), await friends.get_user(session, debt.creditor_id)
