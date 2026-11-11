"""Which of the server's tools the LLM is shown in each kind of chat. (The server enforces the same split on its side.)"""

# in a private chat: everything about the user's own money and friends
PRIVATE_TOOLS = [
    "respond_to_user",
    "add_expenses", "undo_last_expense",
    "set_budget", "get_budgets", "remove_budget",
    "get_expenses", "spending_summary", "monthly_trend", "daily_breakdown",
    "add_friend", "remove_friend", "list_friends",
    "split_expense", "split_bills", "my_dues", "cancel_split",
    "create_group",
]

# in a Telegram group chat: only the shared-group tools (personal money stays out of the group)
GROUP_TOOLS = [
    "respond_to_user",
    "add_group_expense", "split_bills", "group_summary", "settle_group", "cancel_split",
]
