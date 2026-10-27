from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.core import budgets, expenses
from app.core.errors import UserError
from app.core.formatting import friendly_expense
from app.core.periods import parse_when
from app.db.session import session_scope
from app.tools._common import user_id_from


class ExpenseItem(BaseModel):
    amount: float = Field(gt=0, description="Amount spent, as a plain number (3k = 3000).")
    category: str = Field(description="One lowercase word, e.g. food, transport, clothes, bills, shopping, fuel. Prefer the user's budget categories.")
    description: str = Field(description="What it was for, e.g. 'ice cream'.")
    date: str | None = Field(default=None, description="YYYY-MM-DD, only if the user refers to a past day (e.g. 'yesterday').")


@tool
async def add_expenses(items: list[ExpenseItem], config: RunnableConfig) -> str:
    """Record one or more of the user's OWN expenses (money they spent). Put ALL expenses mentioned in the
    user's message into one call. Not for bills shared with friends: use split_expense for those."""
    items = [ExpenseItem.model_validate(i) if isinstance(i, dict) else i for i in items]
    uid = user_id_from(config)
    try:
        async with session_scope() as s:
            created = [await expenses.add_expense(s, uid, i.amount, i.category, i.description, parse_when(i.date)) for i in items]
            alerts = await budgets.alerts_for(s, uid, created)
    except UserError as e:
        return f"❌ {e}"
    return "\n".join([*(friendly_expense(e) for e in created), *alerts])


@tool
async def undo_last_expense(config: RunnableConfig, expense_id: int | None = None) -> str:
    """Undo/delete one of the user's own expenses. Omit expense_id to undo the most recent one."""
    try:
        async with session_scope() as s:
            e = await expenses.void_expense(s, user_id_from(config), expense_id)
    except UserError as e:
        return f"❌ {e}"
    return f"↩️ Undone: {friendly_expense(e)}"
