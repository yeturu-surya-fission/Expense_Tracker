"""Read-only queries: history and summaries of a person's own expenses."""
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.periods import Period, month_key, month_period, shift_month
from app.db.models import Expense


def _filters(user_id: int, period: Period) -> list:
    conds = [Expense.user_id == user_id, Expense.voided_at.is_(None)]
    if period.start:
        conds.append(Expense.occurred_at >= period.start)
    if period.end:
        conds.append(Expense.occurred_at < period.end)
    return conds


async def list_expenses(
    session: AsyncSession, user_id: int, *, period: Period, limit: int = 10, category: str | None = None
) -> list[Expense]:
    q = select(Expense).where(*_filters(user_id, period))
    if category:
        q = q.where(Expense.category == category.strip().lower())
    q = q.order_by(Expense.occurred_at.desc(), Expense.id.desc()).limit(max(1, min(limit, 50)))
    return list((await session.execute(q)).scalars().all())


async def summary(session: AsyncSession, user_id: int, period: Period) -> dict:
    by_cat = (
        await session.execute(
            select(Expense.category, func.sum(Expense.amount))
            .where(*_filters(user_id, period))
            .group_by(Expense.category)
            .order_by(func.sum(Expense.amount).desc())
        )
    ).all()
    return {
        "expenses_by_category": [(c or "other", Decimal(t)) for c, t in by_cat],
        "total_spent": sum((Decimal(t) for _, t in by_cat), Decimal(0)),
    }


def _local(col):
    """A timestamptz column as local wall-clock time, so months/days follow the user's timezone."""
    return func.timezone(settings.timezone, col)


async def monthly_trend(session: AsyncSession, user_id: int, *, months: int = 6, category: str | None = None) -> dict:
    """Spent per calendar month for the last `months` months (including the current one)."""
    months = max(1, min(months, 24))
    keys = [shift_month(month_key(), -i) for i in range(months - 1, -1, -1)]
    m = func.date_trunc("month", _local(Expense.occurred_at))
    q = (
        select(m, Expense.category, func.sum(Expense.amount))
        .where(
            Expense.user_id == user_id,
            Expense.voided_at.is_(None),
            Expense.occurred_at >= month_period(keys[0]).start,
            Expense.occurred_at < month_period(keys[-1]).end,
        )
        .group_by(m, Expense.category)
    )
    if category:
        q = q.where(Expense.category == category.strip().lower())
    spent = {k: Decimal(0) for k in keys}
    by_cat: dict[str, dict[str, Decimal]] = {}
    for month, cat, total in (await session.execute(q)).all():
        k = month.strftime("%Y-%m")
        if k not in spent:
            continue
        spent[k] += Decimal(total)
        by_cat.setdefault(cat or "other", {k2: Decimal(0) for k2 in keys})[k] += Decimal(total)
    return {"months": keys, "spent": spent, "by_category": by_cat}


async def daily_breakdown(session: AsyncSession, user_id: int, month: str) -> dict:
    """Spending per day inside one month."""
    p = month_period(month)
    d = func.date(_local(Expense.occurred_at))
    base = [Expense.user_id == user_id, Expense.voided_at.is_(None), Expense.occurred_at >= p.start, Expense.occurred_at < p.end]
    rows = (await session.execute(select(d, func.sum(Expense.amount), func.count()).where(*base).group_by(d).order_by(d))).all()
    top = (
        await session.execute(
            select(d, Expense.category, func.sum(Expense.amount)).where(*base).group_by(d, Expense.category).order_by(d, func.sum(Expense.amount).desc())
        )
    ).all()
    top_cat: dict[date, str] = {}
    for day, cat, _ in top:
        top_cat.setdefault(day, cat or "other")  # first row per day = its biggest category
    return {"label": p.label, "days": [(day, Decimal(total), n, top_cat.get(day, "other")) for day, total, n in rows]}
