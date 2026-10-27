from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.core import views
from app.core.periods import PeriodError
from app.db.session import session_scope
from app.tools._common import user_id_from

# How the LLM should write a period:
#   today, yesterday, this_week, last_7_days, last_30_days, this_month, last_month, this_year, all_time,
#   a month 'YYYY-MM' (e.g. '2026-09' for September 2026), a day 'YYYY-MM-DD',
#   or a range 'YYYY-MM-DD..YYYY-MM-DD'.


@tool
async def get_expenses(config: RunnableConfig, period: str = "all_time", limit: int = 10, category: str | None = None) -> str:
    """The user's expense history, newest first.
    period: today, yesterday, this_week, last_7_days, last_30_days, this_month, last_month, this_year, all_time,
    OR a month 'YYYY-MM' (September 2026 = '2026-09'), a day 'YYYY-MM-DD', or a range 'YYYY-MM-DD..YYYY-MM-DD'.
    category optional filter. limit max 50."""
    try:
        async with session_scope() as s:
            return await views.recent_text(s, user_id_from(config), period=period, limit=limit, category=category)
    except PeriodError as e:
        return f"❌ {e}"


@tool
async def spending_summary(config: RunnableConfig, period: str = "this_month") -> str:
    """Total spent and spending per category for ONE period.
    period: today, yesterday, this_week, last_7_days, last_30_days, this_month, last_month, this_year, all_time,
    OR a month 'YYYY-MM' (September 2026 = '2026-09'), a day 'YYYY-MM-DD', or a range 'YYYY-MM-DD..YYYY-MM-DD'
    (e.g. 1st to 15th October 2026 = '2026-10-01..2026-10-15')."""
    try:
        async with session_scope() as s:
            return await views.summary_text(s, user_id_from(config), period)
    except PeriodError as e:
        return f"❌ {e}"


@tool
async def monthly_trend(config: RunnableConfig, months: int = 6, category: str | None = None) -> str:
    """Month-by-month comparison of spending for the last N months (default 6, max 24), with the top categories
    per month. Use for 'trend', 'month wise', 'compare months', 'how has my food spending changed'.
    Pass `category` to see only that category month by month."""
    async with session_scope() as s:
        return await views.trend_text(s, user_id_from(config), months, category)


@tool
async def daily_breakdown(config: RunnableConfig, month: str | None = None) -> str:
    """Day-by-day spending inside one month. month = 'YYYY-MM' (default: current month).
    Use for 'day wise', 'daily spending', 'which day did I spend the most'."""
    try:
        async with session_scope() as s:
            return await views.daily_text(s, user_id_from(config), month)
    except PeriodError as e:
        return f"❌ {e}"
