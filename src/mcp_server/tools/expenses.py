from fastmcp import Context
from mcp_server.tools import mcp
from shared.models import ExpenseItem

from mcp_server.core import budgets, expenses
from mcp_server.core.errors import UserError
from mcp_server.core.formatting import friendly_expense
from mcp_server.core.periods import parse_when
from mcp_server.database.session import session_scope
from mcp_server.tools._common import user_id_from


@mcp.tool
async def add_expenses(items: list[ExpenseItem], ctx: Context) -> str:
    """Record one or more of the user's OWN expenses (money they spent). Put ALL expenses mentioned in the
    user's message into one call. Not for bills shared with friends: use split_expense for those."""
    uid = await user_id_from(ctx)
    try:
        async with session_scope() as s:
            created = [await expenses.add_expense(s, uid, i.amount, i.category, i.description, parse_when(i.date)) for i in items]
            alerts = await budgets.alerts_for(s, uid, created)
    except UserError as e:
        return f"❌ {e}"
    return "\n".join([*(friendly_expense(e) for e in created), *alerts])


@mcp.tool
async def undo_last_expense(ctx: Context, expense_id: int | None = None) -> str:
    """Undo/delete one of the user's own expenses. Omit expense_id to undo the most recent one."""
    try:
        async with session_scope() as s:
            e = await expenses.void_expense(s, await user_id_from(ctx), expense_id)
    except UserError as e:
        return f"❌ {e}"
    return f"↩️ Undone: {friendly_expense(e)}"
