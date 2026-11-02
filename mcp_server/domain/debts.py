"""
The life of a debt:   pending --(debtor taps "I paid")--> claimed --(creditor taps "Received")--> settled
                                                           `--(creditor taps "Not yet")--> back to pending

When the debtor says they paid, their share is recorded in THEIR expenses straight away. If the creditor then says
they have not received it, that expense is voided again and the debt returns to the debtor's list.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.domain import friends
from mcp_server.domain.errors import DebtError
from mcp_server.domain.splits import log_user_shares
from mcp_server.db.models import Debt, Expense, Share


async def _lock(session: AsyncSession, debt_id: int) -> Debt:
    debt = (await session.execute(select(Debt).where(Debt.id == debt_id).with_for_update())).scalar_one_or_none()
    if debt is None:
        raise DebtError("I can't find that payment any more.")
    return debt


async def _log_for(session: AsyncSession, debt: Debt) -> list[Expense]:
    """Record the debtor's share of what this debt is for as THEIR expense."""
    if debt.kind == "quick":
        return await log_user_shares(session, debt.debtor_id, shared_expense_id=debt.shared_expense_id)
    if debt.kind == "bundle":
        return await log_user_shares(session, debt.debtor_id, bundle_id=debt.bundle_id)
    return await log_user_shares(session, debt.debtor_id, group_id=debt.group_id)


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
    expenses = await _log_for(session, debt)
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


# ---------------------------------------------------------------- netting: "you owe me 100, I owe you 100" cancels out
@dataclass
class Offset:
    """What was cancelled between two people. `settled` debts are gone; `reduced` ones remain, for a smaller amount."""
    person_a: int
    person_b: int
    amount: Decimal
    settled: list[Debt] = field(default_factory=list)
    reduced: list[Debt] = field(default_factory=list)


def _apply(debts: list[Debt], amount: Decimal) -> tuple[list[Debt], list[Debt]]:
    settled, reduced = [], []
    for d in debts:  # oldest first
        if amount <= 0:
            break
        if d.amount <= amount:
            amount -= d.amount
            settled.append(d)
        else:
            d.amount -= amount
            amount = Decimal(0)
            reduced.append(d)
    return settled, reduced


async def offset_between(session: AsyncSession, a: int, b: int) -> Offset | None:
    """
    Cancel what two people owe each other (unpaid debts only; ones already marked paid are in flight and left alone).
    A debt cancelled completely counts as settled, and its debtor's share is recorded in their expenses, exactly as if they had paid.
    """
    lo, hi = sorted((a, b))
    rows = list((await session.execute(
        select(Debt).where(Debt.status == "pending", Debt.debtor_id.in_((lo, hi)), Debt.creditor_id.in_((lo, hi))).order_by(Debt.id).with_for_update()
    )).scalars().all())
    a_owes = [d for d in rows if d.debtor_id == a and d.creditor_id == b]
    b_owes = [d for d in rows if d.debtor_id == b and d.creditor_id == a]
    cancel = min(sum((d.amount for d in a_owes), Decimal(0)), sum((d.amount for d in b_owes), Decimal(0)))
    if cancel <= 0:
        return None
    result = Offset(a, b, cancel)
    now = datetime.now(timezone.utc)
    for side in (a_owes, b_owes):
        settled, reduced = _apply(side, cancel)
        for d in settled:
            expenses = await _log_for(session, d)
            d.status, d.settled_at, d.expense_ids = "settled", now, [e.id for e in expenses]
        result.settled += settled
        result.reduced += reduced
    await session.flush()
    return result


async def offset_new(session: AsyncSession, new_debts: list[Debt]) -> list[Offset]:
    """Net every pair of people that the new debts connect against whatever they already owed each other."""
    out, seen = [], set()
    for d in new_debts:
        pair = tuple(sorted((d.debtor_id, d.creditor_id)))
        if pair in seen:
            continue
        seen.add(pair)
        if (o := await offset_between(session, *pair)) is not None:
            out.append(o)
    return out


async def pending_between(session: AsyncSession, debtor_id: int, creditor_id: int) -> Decimal:
    rows = (await session.execute(
        select(Debt.amount).where(Debt.status == "pending", Debt.debtor_id == debtor_id, Debt.creditor_id == creditor_id)
    )).scalars().all()
    return sum((Decimal(x) for x in rows), Decimal(0))
