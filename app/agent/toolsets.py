"""Which of the server's tools the LLM is shown in each kind of chat. (The server enforces the same split on its side.)"""

# in a private chat: everything about the user's own money and friends
PRIVATE_TOOLS = [
    "respond_to_user",
    "add_expenses", "undo_last_expense",
    "set_budget", "get_budgets", "remove_budget",
    "get_expenses", "spending_summary", "monthly_trend", "daily_breakdown",
    "add_friend", "remove_friend", "list_friends",
    "split_expense", "split_bills", "my_dues", "pay_friend", "cancel_split",
    "create_group",
    # work reimbursements (kept apart from personal spending; the company's finance agent decides)
    "join_company", "add_work_expense", "move_to_reimbursement", "list_work_expenses", "remove_work_expense",
    "ask_policy", "ask_manager", "submit_claim", "claim_answer", "my_claims", "expense_details",
]

# in a Telegram group chat: only the shared-group tools (personal money stays out of the group)
GROUP_TOOLS = [
    "respond_to_user",
    "add_group_expense", "split_bills", "group_summary", "settle_group", "cancel_split",
]
