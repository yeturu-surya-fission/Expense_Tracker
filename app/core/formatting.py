"""Pure text rendering. Everything the user reads is built here so tools and bot look the same."""
from decimal import Decimal

from app.config import settings
from app.core.periods import to_local
from app.db.models import Expense

CATEGORY_ICON = {
    "food": "🍔", "groceries": "🛒", "transport": "🚌", "travel": "✈️", "fuel": "⛽",
    "clothes": "👕", "shopping": "🛍️", "bills": "🧾", "rent": "🏠", "entertainment": "🎬",
    "health": "💊", "education": "📚", "insurance": "🛡️", "emi": "💳", "investment": "📈",
    "gift": "🎁", "dining": "🍽️", "stay": "🏨", "drinks": "🍻",
}


def money(value: Decimal | float | int) -> str:
    d = Decimal(value).quantize(Decimal("0.01"))
    text = f"{abs(d):,.2f}"
    if text.endswith(".00"):
        text = text[:-3]
    sign = "-" if d < 0 else ""
    return f"{sign}{settings.currency_symbol}{text}"


def cat_icon(category: str | None) -> str:
    return CATEGORY_ICON.get((category or "").lower(), "🏷️")


def bar(percent: int, width: int = 10) -> str:
    filled = min(width, round(width * percent / 100))
    return "▓" * filled + "░" * (width - filled)


def status_dot(percent: int) -> str:
    return "🟢" if percent < 50 else "🟡" if percent < 80 else "🟠" if percent < 100 else "🔴"


def friendly_expense(e: Expense) -> str:
    """One confirmation line for a freshly recorded expense."""
    label = e.description or e.category
    return f"🔴 Spent {money(e.amount)} on {label} {cat_icon(e.category)}  (#{e.id})"


def history_line(e: Expense) -> str:
    when = to_local(e.occurred_at).strftime("%d %b")
    what = " · ".join(x for x in (e.category, e.description if e.description != e.category else None) if x)
    tag = {"split": "  [split share]", "statement": "  [statement]"}.get(e.source, "")
    void = "  ❌ undone" if e.voided_at else ""
    return f"{when}  🔴 {money(e.amount)}  {what}{tag}  #{e.id}{void}"
