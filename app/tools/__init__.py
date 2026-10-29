"""Every tool the agent can call. Add a new tool: write it in a module, list it here."""
from app.tools.bills import split_bills
from app.tools.budgets import get_budgets, remove_budget, set_budget
from app.tools.conversation import respond_to_user
from app.tools.expenses import add_expenses, undo_last_expense
from app.tools.friends import add_friend, list_friends, remove_friend
from app.tools.groups import add_group_expense, create_group, group_summary, settle_group
from app.tools.reports import daily_breakdown, get_expenses, monthly_trend, spending_summary
from app.tools.splits import cancel_split, my_dues, split_expense

# in a private chat: everything about the user's own money and friends
PRIVATE_TOOLS = [
    respond_to_user,
    add_expenses, undo_last_expense,
    set_budget, get_budgets, remove_budget,
    get_expenses, spending_summary, monthly_trend, daily_breakdown,
    add_friend, remove_friend, list_friends,
    split_expense, split_bills, my_dues, cancel_split,
    create_group,
]

# in a Telegram group chat: only the shared-group tools (personal money stays out of the group)
GROUP_TOOLS = [
    respond_to_user,
    add_group_expense, split_bills, group_summary, settle_group, cancel_split,
]

ALL_TOOLS = list({t.name: t for t in [*PRIVATE_TOOLS, *GROUP_TOOLS]}.values())
TOOLS_BY_NAME = {t.name: t for t in ALL_TOOLS}
