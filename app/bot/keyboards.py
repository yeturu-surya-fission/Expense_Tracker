from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.core.periods import month_key, month_label, shift_month

# Persistent menu under the text box. Label -> action; handled WITHOUT the LLM (instant).
MENU = {
    "📊 Summary": "summary",
    "🧾 Recent": "recent",
    "🎯 Budgets": "budgets",
    "👥 Friends": "friends",
    "💳 Dues": "dues",
    "↩️ Undo last": "undo",
    "❓ Help": "help",
}

PERIODS = {"today": "Today", "this_week": "This week", "this_month": "This month", "last_month": "Last month"}


def main_menu() -> ReplyKeyboardMarkup:
    labels = list(MENU)
    rows = [[KeyboardButton(x) for x in labels[i:i + 2]] for i in range(0, len(labels), 2)]
    return ReplyKeyboardMarkup(
        rows, resize_keyboard=True, is_persistent=True,
        input_field_placeholder="e.g. spent 300 on ice cream",
    )


def choice_keyboard(choices: list[dict], owner_telegram_id: int | None = None) -> InlineKeyboardMarkup:
    """Answer buttons for a question. In a group chat the owner id is added so only the asker can answer."""
    suffix = f":{owner_telegram_id}" if owner_telegram_id else ""
    rows = [[InlineKeyboardButton(c["label"], callback_data=f"hitl:{c['id']}{suffix}")] for c in choices if c["id"] != "cancel"]
    cancel = [c for c in choices if c["id"] == "cancel"]
    if cancel:
        rows.append([InlineKeyboardButton(cancel[0]["label"], callback_data=f"hitl:cancel{suffix}")])
    return InlineKeyboardMarkup(rows)


def result_keyboard(txn_ids: list[int], split_ids: list[int]) -> InlineKeyboardMarkup | None:
    rows = []
    if txn_ids:
        rows.append([InlineKeyboardButton("↩️ Undo", callback_data="undo:" + ",".join(str(i) for i in txn_ids[:8]))])
    if split_ids:
        rows.append([InlineKeyboardButton("↩️ Cancel this split", callback_data=f"undosplit:{split_ids[0]}")])
    return InlineKeyboardMarkup(rows) if rows else None


def undo_confirm_keyboard(expense_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, undo it", callback_data=f"undo:{expense_id}"),
        InlineKeyboardButton("✖ Keep it", callback_data="keep"),
    ]])


def settle_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, settle up", callback_data=f"gsettle:{group_id}"),
        InlineKeyboardButton("✖ Not yet", callback_data="keep"),
    ]])


def start_bot_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Start the bot", url=url)]])


# ---------------------------------------------------------------- summary / daily / trend with month navigation
def _month_of(period: str) -> str:
    if period == "last_month":
        return shift_month(month_key(), -1)
    if len(period) == 7 and period[4] == "-":
        return period
    return month_key()


def summary_keyboard(period: str) -> InlineKeyboardMarkup:
    month = _month_of(period)
    nav = [InlineKeyboardButton(f"◀ {month_label(shift_month(month, -1))}", callback_data=f"sum:{shift_month(month, -1)}")]
    if month < month_key():
        nav.append(InlineKeyboardButton(f"{month_label(shift_month(month, 1))} ▶", callback_data=f"sum:{shift_month(month, 1)}"))
    quick = [InlineKeyboardButton(("● " if k == period else "") + label, callback_data=f"sum:{k}") for k, label in PERIODS.items()]
    return InlineKeyboardMarkup([
        nav, quick[:2], quick[2:],
        [InlineKeyboardButton("📅 Daily", callback_data=f"day:{month}"), InlineKeyboardButton("📈 Trend", callback_data="trend:6")],
    ])


def daily_keyboard(month: str) -> InlineKeyboardMarkup:
    nav = [InlineKeyboardButton(f"◀ {month_label(shift_month(month, -1))}", callback_data=f"day:{shift_month(month, -1)}")]
    if month < month_key():
        nav.append(InlineKeyboardButton(f"{month_label(shift_month(month, 1))} ▶", callback_data=f"day:{shift_month(month, 1)}"))
    return InlineKeyboardMarkup([nav, [
        InlineKeyboardButton("📊 Summary", callback_data=f"sum:{month}"),
        InlineKeyboardButton("📈 Trend", callback_data="trend:6"),
    ]])


def trend_keyboard(months: int) -> InlineKeyboardMarkup:
    ranges = [InlineKeyboardButton(("● " if n == months else "") + f"{n} mo", callback_data=f"trend:{n}") for n in (3, 6, 12)]
    return InlineKeyboardMarkup([ranges, [
        InlineKeyboardButton("📊 This month", callback_data="sum:this_month"),
        InlineKeyboardButton("📅 Daily", callback_data=f"day:{month_key()}"),
    ]])


# ---------------------------------------------------------------- statement import
def import_keyboard(import_id: int, n_new: int, n_maybe: int) -> InlineKeyboardMarkup:
    rows = []
    if n_new:
        rows.append([InlineKeyboardButton(f"✅ Import {n_new} new", callback_data=f"imp:{import_id}:new")])
    if n_maybe:
        rows.append([InlineKeyboardButton(f"➕ Include the {n_maybe} maybe-duplicates too", callback_data=f"imp:{import_id}:all")])
    rows.append([InlineKeyboardButton("✖ Cancel", callback_data=f"imp:{import_id}:no")])
    return InlineKeyboardMarkup(rows)


def after_import_keyboard(import_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Undo this import", callback_data=f"impundo:{import_id}")]])
