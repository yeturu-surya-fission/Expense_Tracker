from mcp_server.db.session import session_scope
from mcp_server.domain import budgets, views
from mcp_server.domain.expenses import normalize_category
from mcp_server.domain.periods import PeriodError
from mcp_server.tools.registry import tool


@tool(contexts=("private",), write=True)
async def set_budget(call, category: str, monthly_limit: float) -> str:
    """Create or change a MONTHLY spending limit for a category (e.g. food 3000, clothes 2000).
    The user is alerted at 50%, 80% and 100% usage. Convert '3k' to 3000."""
    async with session_scope() as s:
        b = await budgets.set_budget(s, call.user_id, category, monthly_limit)
        spent = await budgets.category_spent(s, call.user_id, b.category)
        status = budgets.BudgetStatus(b.category, b.monthly_limit, spent)
        alert = await budgets.evaluate_alerts(s, call.user_id, b.category)
    return "\n".join(filter(None, ["🎯 Budget set", status.line(), alert]))


@tool(contexts=("private",), write=True)
async def remove_budget(call, category: str) -> str:
    """Delete the monthly limit of a category."""
    async with session_scope() as s:
        ok = await budgets.remove_budget(s, call.user_id, category)
    name = normalize_category(category)
    return f"🗑️ Removed the {name} budget." if ok else f"There is no budget for {name}."


@tool(contexts=("private",))
async def get_budgets(call, month: str | None = None) -> str:
    """Show every monthly budget with spent / remaining. Current month by default;
    pass month='YYYY-MM' (September 2026 = '2026-09') to see a past month."""
    try:
        async with session_scope() as s:
            return await views.budgets_text(s, call.user_id, month)
    except PeriodError as e:
        return f"❌ {e}"
