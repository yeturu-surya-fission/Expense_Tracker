"""
Splitting one payment between people.

quick split  (group_id None): shares are fixed now. The payer's own share goes straight into their expenses;
                              every other person gets a Debt (they owe the payer) and logs the expense when they pay.
group expense (group_id set): only shares are stored. Debts are worked out later by groups.settle().
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import SplitError
from app.core.expenses import CENT, add_expense, normalize_category, to_decimal
from app.db.models import Debt, Expense, Share, SharedExpense


def equal_shares(total: Decimal, user_ids: list[int], already: dict[int, Decimal] | None = None) -> dict[int, Decimal]:
    """
    Equal split to the paisa: 1000 / 3 -> 333.34, 333.33, 333.33.
    The odd paise go to whoever has the SMALLEST total so far (`already`, e.g. their share of earlier bills in the
    same outing), so over several bills nobody drifts a few paise ahead. Without `already`, the first people get them.
    """
    total = to_decimal(total)
    ids = list(dict.fromkeys(user_ids))
    if not ids:
        raise SplitError("Nobody to split with.")
    base = (total / len(ids)).quantize(CENT, rounding=ROUND_DOWN)
    extra = int((total - base * len(ids)) / CENT)
    order = sorted(range(len(ids)), key=lambda i: ((already or {}).get(ids[i], Decimal(0)), i))
    bump = {ids[i] for i in order[:extra]}
    return {uid: base + (CENT if uid in bump else Decimal(0)) for uid in ids}


def exact_shares(total: Decimal, amounts: dict[int, Decimal]) -> dict[int, Decimal]:
    total = to_decimal(total)
    shares = {uid: to_decimal(a) for uid, a in amounts.items()}
    if any(a <= 0 for a in shares.values()):
        raise SplitError("Every amount must be more than zero.")
    got = sum(shares.values(), Decimal(0))
    if got != total:
        raise SplitError(f"Those amounts add up to {got} but the total is {total}.")
    return shares


def compute_transfers(net: dict[int, Decimal]) -> list[tuple[int, int, Decimal]]:
    """Fewest payments that clear everything: biggest debtor pays biggest creditor, and so on."""
    debtors = sorted(([u, -n] for u, n in net.items() if n < 0), key=lambda x: (-x[1], x[0]))
    creditors = sorted(([u, n] for u, n in net.items() if n > 0), key=lambda x: (-x[1], x[0]))
    out, i, j = [], 0, 0
    while i < len(debtors) and j < len(creditors):
        pay = min(debtors[i][1], creditors[j][1])
        out.append((debtors[i][0], creditors[j][0], pay))
        debtors[i][1] -= pay
        creditors[j][1] -= pay
        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1
    return out


@dataclass
class SplitResult:
    shared: SharedExpense
    shares: dict[int, Decimal]
    debts: list[Debt]
    logged: list[Expense]  # expenses recorded immediately (the payer's own share of a quick split)


async def create_split(
    session: AsyncSession,
    *,
    creator_id: int,
    payer_id: int,
    shares: dict[int, Decimal],
    total: Decimal,
    description: str,
    category: str,
    mode: str,
    group_id: int | None = None,
) -> SplitResult:
    total = to_decimal(total)
    if total <= 0:
        raise SplitError("The amount must be more than zero.")
    if not shares or sum(shares.values(), Decimal(0)) != total:
        raise SplitError("The shares do not add up to the total.")
    category = normalize_category(category)
    description = (description or category).strip()[:256]

    shared = SharedExpense(
        group_id=group_id, payer_id=payer_id, created_by=creator_id, amount=total,
        description=description, category=category, split_mode=mode,
    )
    session.add(shared)
    await session.flush()

    debts: list[Debt] = []
    logged: list[Expense] = []
    for uid, amount in shares.items():
        share = Share(shared_expense_id=shared.id, user_id=uid, amount=amount)
        session.add(share)
        if group_id is not None:
            continue
        if uid == payer_id:
            exp = await add_expense(session, uid, amount, category, description, source="split")
            share.expense_id = exp.id
            logged.append(exp)
        else:
            debt = Debt(debtor_id=uid, creditor_id=payer_id, amount=amount, description=description, kind="quick", shared_expense_id=shared.id)
            session.add(debt)
            debts.append(debt)
    await session.flush()
    return SplitResult(shared, shares, debts, logged)


async def log_user_shares(
    session: AsyncSession, user_id: int, *, shared_expense_id: int | None = None, group_id: int | None = None,
    bundle_id: str | None = None,
) -> list[Expense]:
    """Record a person's not-yet-recorded shares as their own expenses (called when they have paid / owe nothing)."""
    q = (
        select(Share, SharedExpense)
        .join(SharedExpense, SharedExpense.id == Share.shared_expense_id)
        .where(Share.user_id == user_id, Share.expense_id.is_(None), SharedExpense.voided_at.is_(None))
    )
    if shared_expense_id is not None:
        q = q.where(SharedExpense.id == shared_expense_id)
    elif bundle_id is not None:
        q = q.where(SharedExpense.bundle_id == bundle_id)
    else:
        q = q.where(SharedExpense.group_id == group_id, SharedExpense.settled_at.is_not(None))
    rows = (await session.execute(q.order_by(SharedExpense.id).with_for_update(of=Share))).all()
    out = []
    for share, shared in rows:
        exp = await add_expense(session, user_id, share.amount, shared.category, shared.description,
                                occurred_at=shared.created_at, source="split")
        share.expense_id = exp.id
        out.append(exp)
    await session.flush()
    return out


@dataclass
class BillSpec:
    """One bill inside a bundle: who paid, how much, and who shares it (equally)."""
    payer_id: int
    amount: Decimal
    description: str
    category: str
    participant_ids: list[int]


@dataclass
class BundleResult:
    bundle_id: str
    shared: list[SharedExpense]
    paid: dict[int, Decimal]
    share: dict[int, Decimal]
    transfers: list[tuple[int, int, Decimal]]
    debts: list[Debt]
    logged: dict[int, list[Expense]] = field(default_factory=dict)  # recorded right away for people who pay nobody
    share_items: dict[int, list[tuple[str, Decimal]]] = field(default_factory=dict)  # per person: (bill, their share)
    paid_items: dict[int, list[tuple[str, Decimal]]] = field(default_factory=dict)  # per person: (bill, what they paid)

    @property
    def total(self) -> Decimal:
        return sum((s.amount for s in self.shared), Decimal(0))

    def net(self) -> dict[int, Decimal]:
        return {u: self.paid.get(u, Decimal(0)) - self.share.get(u, Decimal(0)) for u in set(self.paid) | set(self.share)}


async def create_bundle(session: AsyncSession, *, creator_id: int, title: str, bills: list[BillSpec]) -> BundleResult:
    """
    Several bills paid by DIFFERENT people (a party night: dinner, ice cream, bar) settled together.
    Each person's total paid is compared with their total share, so only the few payments that are really needed remain.
    People who end up owing nobody have their share recorded now; people who owe record theirs when they pay.
    """
    if not bills:
        raise SplitError("There are no bills to split.")
    bundle_id = uuid.uuid4().hex
    title = (title or "split").strip()[:200]
    shared_rows: list[SharedExpense] = []
    paid: dict[int, Decimal] = {}
    share: dict[int, Decimal] = {}
    share_items: dict[int, list[tuple[str, Decimal]]] = {}
    paid_items: dict[int, list[tuple[str, Decimal]]] = {}
    for bill in bills:
        total = to_decimal(bill.amount)
        if total <= 0:
            raise SplitError("Every bill must be more than zero.")
        shares = equal_shares(total, bill.participant_ids, already=share)
        shared = SharedExpense(
            group_id=None, payer_id=bill.payer_id, created_by=creator_id, amount=total, bundle_id=bundle_id,
            description=(bill.description or bill.category).strip()[:256], category=normalize_category(bill.category), split_mode="equal",
        )
        session.add(shared)
        await session.flush()
        shared_rows.append(shared)
        paid[bill.payer_id] = paid.get(bill.payer_id, Decimal(0)) + total
        paid_items.setdefault(bill.payer_id, []).append((shared.description, total))
        for uid, amount in shares.items():
            session.add(Share(shared_expense_id=shared.id, user_id=uid, amount=amount))
            share[uid] = share.get(uid, Decimal(0)) + amount
            share_items.setdefault(uid, []).append((shared.description, amount))
    await session.flush()

    ids = set(paid) | set(share)
    nets = {u: paid.get(u, Decimal(0)) - share.get(u, Decimal(0)) for u in ids}
    transfers = compute_transfers(nets)
    debts = []
    for debtor, creditor, amount in transfers:
        d = Debt(debtor_id=debtor, creditor_id=creditor, amount=amount, description=title, kind="bundle", bundle_id=bundle_id)
        session.add(d)
        debts.append(d)
    await session.flush()
    owing = {t[0] for t in transfers}
    logged = {uid: await log_user_shares(session, uid, bundle_id=bundle_id) for uid in ids if uid not in owing}
    return BundleResult(bundle_id, shared_rows, paid, share, transfers, debts, logged, share_items, paid_items)


async def cancel_split(session: AsyncSession, user_id: int, split_id: int | None = None) -> tuple[SharedExpense, list[Debt]]:
    """Cancel a split you created, as long as nobody has paid yet (and a group expense is not settled yet)."""
    q = select(SharedExpense).where(SharedExpense.voided_at.is_(None), SharedExpense.created_by == user_id)
    q = q.where(SharedExpense.id == split_id) if split_id else q.order_by(SharedExpense.id.desc()).limit(1)
    shared = (await session.execute(q.with_for_update())).scalar_one_or_none()
    if shared is None:
        raise SplitError("I couldn't find a split of yours to cancel.")
    if shared.settled_at is not None:
        raise SplitError("That group expense is already part of a settlement, so it can't be cancelled.")
    siblings = [shared]
    if shared.bundle_id:  # bills split together are cancelled together
        siblings = list((await session.execute(select(SharedExpense).where(SharedExpense.bundle_id == shared.bundle_id, SharedExpense.voided_at.is_(None)))).scalars().all())
        debt_filter = Debt.bundle_id == shared.bundle_id
    else:
        debt_filter = Debt.shared_expense_id == shared.id
    debts = list((await session.execute(select(Debt).where(debt_filter).with_for_update())).scalars().all())
    if any(d.status in ("claimed", "settled") for d in debts):
        raise SplitError("Someone has already paid their share, so this split can't be cancelled.")

    now = datetime.now(timezone.utc)
    for d in debts:
        d.status = "cancelled"
    shares = (await session.execute(select(Share).where(Share.shared_expense_id.in_([x.id for x in siblings])))).scalars().all()
    for share in shares:
        if share.expense_id:
            exp = await session.get(Expense, share.expense_id)
            if exp is not None and exp.voided_at is None:
                exp.voided_at = now
    for x in siblings:
        x.voided_at = now
    await session.flush()
    return shared, debts
