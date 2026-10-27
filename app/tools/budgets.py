from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.core import budgets, views
from app.core.errors import UserError
from app.core.expenses import normalize_category
from app.core.periods import PeriodError
from app.db.session import session_scope
from app.tools._common import user_id_from


@tool
async def set_budget(category: str, monthly_limit: float, config: RunnableConfig) -> str:
    """Create or change a MONTHLY spending limit for a category (e.g. food 3000, clothes 2000).
    The user is alerted at 50%, 80% and 100% usage. Convert '3k' to 3000."""
    uid = user_id_from(config)
    try:
        async with session_scope() as s:
            b = await budgets.set_budget(s, uid, category, monthly_limit)
            spent = await budgets.category_spent(s, uid, b.category)
            status = budgets.BudgetStatus(b.category, b.monthly_limit, spent)
            alert = await budgets.evaluate_alerts(s, uid, b.category)
    except UserError as e:
        return f"❌ {e}"
    return "\n".join(filter(None, ["🎯 Budget set", status.line(), alert]))


@tool
async def remove_budget(category: str, config: RunnableConfig) -> str:
    """Delete the monthly limit of a category."""
    async with session_scope() as s:
        ok = await budgets.remove_budget(s, user_id_from(config), category)
    name = normalize_category(category)
    return f"🗑️ Removed the {name} budget." if ok else f"There is no budget for {name}."


@tool
async def get_budgets(config: RunnableConfig, month: str | None = None) -> str:
    """Show every monthly budget with spent / remaining. Current month by default;
    pass month='YYYY-MM' (September 2026 = '2026-09') to see a past month."""
    try:
        async with session_scope() as s:
            return await views.budgets_text(s, user_id_from(config), month)
    except PeriodError as e:
        return f"❌ {e}"
