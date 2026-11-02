from pydantic import BaseModel, Field

from mcp_server.db.session import session_scope
from mcp_server.domain import budgets, expenses
from mcp_server.domain.formatting import friendly_expense
from mcp_server.domain.periods import parse_when
from mcp_server.tools._common import Out
from mcp_server.tools.registry import tool


class ExpenseItem(BaseModel):
    amount: float = Field(gt=0, description="Amount spent, as a plain number (3k = 3000).")
    category: str = Field(description="One lowercase word, e.g. food, transport, clothes, bills, shopping, fuel. Prefer the user's budget categories.")
    description: str = Field(description="What it was for, e.g. 'ice cream'.")
    date: str | None = Field(default=None, description="YYYY-MM-DD, only if the user refers to a past day (e.g. 'yesterday').")


@tool(contexts=("private",), write=True)
async def add_expenses(call, items: list[ExpenseItem]) -> Out:
    """Record one or more of the user's OWN expenses (money they spent). Put ALL expenses mentioned in the
    user's message into one call. Not for bills shared with friends: use split_expense for those."""
    async with session_scope() as s:
        created = [await expenses.add_expense(s, call.user_id, i.amount, i.category, i.description, parse_when(i.date)) for i in items]
        alerts = await budgets.alerts_for(s, call.user_id, created)
        ids = [e.id for e in created]
        lines = [friendly_expense(e) for e in created]
    return Out("\n".join([*lines, *alerts]), {"expense_ids": ids})


@tool(contexts=("private",), write=True)
async def undo_last_expense(call, expense_id: int | None = None) -> Out:
    """Undo/delete one of the user's own expenses. Omit expense_id to undo the most recent one."""
    async with session_scope() as s:
        e = await expenses.void_expense(s, call.user_id, expense_id)
        text = f"↩️ Undone: {friendly_expense(e)}"
    return Out(text, {"expense_ids": [e.id]})
