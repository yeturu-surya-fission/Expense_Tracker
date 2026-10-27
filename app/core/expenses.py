"""Personal expenses: the only place that creates or voids Expense rows (besides statement import)."""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ExpenseError
from app.db.models import Expense

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
