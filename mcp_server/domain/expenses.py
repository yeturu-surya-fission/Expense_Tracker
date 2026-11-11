"""Personal expenses: the only place that creates or voids Expense rows (besides statement import)."""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.domain.errors import ExpenseError
from mcp_server.db.models import Expense

CENT = Decimal("0.01")


def to_decimal(value: Decimal | float | int | str) -> Decimal:
    return Decimal(str(value)).quantize(CENT)


def normalize_category(category: str | None) -> str:
    return (category or "other").strip().lower()[:64] or "other"


async def add_expense(
    session: AsyncSession,
    user_id: int,
    amount: Decimal | float,
    category: str | None,
    description: str | None = None,
    occurred_at: datetime | None = None,
    *,
    source: str = "personal",
    external_key: str | None = None,
    import_id: int | None = None,
) -> Expense:
    amount = to_decimal(amount)
    if amount <= 0:
        raise ExpenseError("Amount must be greater than zero.")
    cat = normalize_category(category)
    exp = Expense(
        user_id=user_id, amount=amount, category=cat,
        description=(description or "").strip()[:256] or cat,
        source=source, external_key=external_key, import_id=import_id,
    )
    if occurred_at:
        exp.occurred_at = occurred_at
    session.add(exp)
    await session.flush()
    return exp


async def void_if_just_logged(session: AsyncSession, user_id: int, amount: Decimal | float, within_minutes: int = 60) -> Expense | None:
    """
    'I paid 500 for petrol' ... then 'split it with Surya': the split is that same 500, so the plain expense
    is replaced by the split (the user's own share is logged by the split). Only an expense of the SAME amount,
    logged a moment ago and still the user's latest one, is replaced.
    """
    latest = (await session.execute(
        select(Expense).where(Expense.user_id == user_id, Expense.voided_at.is_(None), Expense.source == "personal")
        .order_by(Expense.id.desc()).limit(1).with_for_update()
    )).scalar_one_or_none()
    if latest is None or latest.amount != to_decimal(amount):
        return None
    if (datetime.now(timezone.utc) - latest.occurred_at).total_seconds() > within_minutes * 60:
        return None
    latest.voided_at = datetime.now(timezone.utc)
    await session.flush()
    return latest


async def void_expense(session: AsyncSession, user_id: int, expense_id: int | None = None) -> Expense:
    """Undo a personal expense (default: the latest one). Split shares and statement rows have their own undo."""
    q = select(Expense).where(Expense.user_id == user_id, Expense.voided_at.is_(None), Expense.source == "personal")
    q = q.where(Expense.id == expense_id) if expense_id else q.order_by(Expense.id.desc()).limit(1)
    exp = (await session.execute(q.with_for_update())).scalar_one_or_none()
    if exp is None:
        raise ExpenseError("There is no matching expense to undo.")
    exp.voided_at = datetime.now(timezone.utc)
    await session.flush()
    return exp
