"""Monthly per-category limits and the 50% / 80% / 100% alerts."""
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.formatting import bar, money, status_dot
from app.core.errors import ExpenseError
from app.core.expenses import normalize_category, to_decimal
from app.core.periods import Period, current_month, month_key
from app.db.models import Budget, BudgetAlert, Expense


@dataclass
class BudgetStatus:
    category: str
    limit: Decimal
    spent: Decimal

    @property
    def remaining(self) -> Decimal:
        return self.limit - self.spent

    @property
    def percent(self) -> int:
        return int(self.spent * 100 / self.limit) if self.limit else 0

    def line(self) -> str:
        left = f"{money(self.remaining)} left" if self.remaining >= 0 else f"over by {money(-self.remaining)}"
        return f"{status_dot(self.percent)} {self.category}: {bar(self.percent)} {self.percent}% · {money(self.spent)} of {money(self.limit)} · {left}"


async def category_spent(session: AsyncSession, user_id: int, category: str, period: Period | None = None) -> Decimal:
    p = period or current_month()
    total = (
        await session.execute(
            select(func.coalesce(func.sum(Expense.amount), 0)).where(
                Expense.user_id == user_id,
                Expense.category == category,
                Expense.voided_at.is_(None),
                Expense.occurred_at >= p.start,
                Expense.occurred_at < p.end,
            )
        )
    ).scalar_one()
    return Decimal(total)


async def _get_budget(session: AsyncSession, user_id: int, category: str) -> Budget | None:
    return (
        await session.execute(select(Budget).where(Budget.user_id == user_id, Budget.category == category))
    ).scalar_one_or_none()


async def set_budget(session: AsyncSession, user_id: int, category: str, limit: Decimal | float) -> Budget:
    category, limit = normalize_category(category), to_decimal(limit)
    if limit <= 0:
        raise ExpenseError("A budget limit must be greater than zero.")
    budget = await _get_budget(session, user_id, category)
    if budget is None:
        budget = Budget(user_id=user_id, category=category, monthly_limit=limit)
        session.add(budget)
    else:
        budget.monthly_limit = limit
    await session.flush()
    # limit changed -> thresholds must be re-evaluated against the new number
    await session.execute(delete(BudgetAlert).where(BudgetAlert.budget_id == budget.id))
    return budget


async def remove_budget(session: AsyncSession, user_id: int, category: str) -> bool:
    budget = await _get_budget(session, user_id, normalize_category(category))
    if budget is None:
        return False
    await session.delete(budget)
    return True


async def list_budget_status(session: AsyncSession, user_id: int, period: Period | None = None) -> list[BudgetStatus]:
    budgets = (await session.execute(select(Budget).where(Budget.user_id == user_id).order_by(Budget.category))).scalars().all()
    return [BudgetStatus(b.category, b.monthly_limit, await category_spent(session, user_id, b.category, period)) for b in budgets]


async def budget_categories(session: AsyncSession, user_id: int) -> list[str]:
    return list((await session.execute(select(Budget.category).where(Budget.user_id == user_id))).scalars().all())


async def evaluate_alerts(session: AsyncSession, user_id: int, category: str) -> str | None:
    """
    Call after spending in `category`. Returns ONE alert message for the highest newly
    crossed threshold (or None). Each threshold fires once per month per budget.
    """
    category = normalize_category(category)
    budget = await _get_budget(session, user_id, category)
    if budget is None:
        return None

    status = BudgetStatus(category, budget.monthly_limit, await category_spent(session, user_id, category))
    period = month_key()
    already = set(
        (await session.execute(
            select(BudgetAlert.threshold).where(BudgetAlert.budget_id == budget.id, BudgetAlert.period == period)
        )).scalars().all()
    )
    new = [t for t in settings.alert_thresholds if status.percent >= t and t not in already]
    if not new:
        return None
    for t in new:
        await session.execute(
            pg_insert(BudgetAlert).values(budget_id=budget.id, period=period, threshold=t).on_conflict_do_nothing()
        )

    top = max(new)
    if status.spent > status.limit:
        return (f"🚨 You've EXCEEDED your {category} budget! {money(status.spent)} spent of "
                f"{money(status.limit)} ({status.percent}%), over by {money(-status.remaining)}.")
    if top >= 100:
        return f"🚨 Your {category} budget is fully used: {money(status.spent)} of {money(status.limit)}. Nothing left this month."
    return (f"⚠️ {top}% of your {category} budget is used: {money(status.spent)} of {money(status.limit)}. "
            f"{money(status.remaining)} left - spend wisely!")


async def alerts_for(session: AsyncSession, user_id: int, expenses: list[Expense]) -> list[str]:
    """Budget alerts triggered by these freshly recorded expenses (one check per category)."""
    out = []
    for category in dict.fromkeys(e.category for e in expenses):
        if (msg := await evaluate_alerts(session, user_id, category)):
            out.append(msg)
    return out
