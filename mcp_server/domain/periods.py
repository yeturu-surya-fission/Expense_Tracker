"""
Time helpers. All 'this month / today' logic uses the configured timezone.

A *period* string can be:
  today | yesterday | this_week | last_7_days | last_30_days | this_month | last_month | this_year | all_time
  YYYY-MM                      a specific month, e.g. 2026-09
  YYYY-MM-DD                   a specific day
  YYYY-MM-DD..YYYY-MM-DD       an inclusive date range
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from mcp_server.config import settings

PERIOD_NAMES = (
    "today", "yesterday", "this_week", "last_7_days", "last_30_days", "this_month",
    "last_month", "this_year", "all_time",
)
PERIOD_HELP = (
    "today, yesterday, this_week, last_7_days, last_30_days, this_month, last_month, this_year, all_time, "
    "a month 'YYYY-MM', a day 'YYYY-MM-DD', or a range 'YYYY-MM-DD..YYYY-MM-DD'"
)

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_RANGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")


class PeriodError(ValueError):
    """The period text could not be understood (message is user-facing)."""


@dataclass(frozen=True)
class Period:
    label: str
    start: datetime | None  # inclusive, UTC-aware; None = unbounded
    end: datetime | None  # exclusive


def tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def now_local() -> datetime:
    return datetime.now(tz())


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(tz())


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=tz())


def _utc(a: datetime | None, b: datetime | None) -> tuple[datetime | None, datetime | None]:
    return (a.astimezone(timezone.utc) if a else None, b.astimezone(timezone.utc) if b else None)


# ------------------------------------------------------------------ months
def month_key(ref: datetime | None = None) -> str:
    ref = ref or now_local()
    return f"{ref.year:04d}-{ref.month:02d}"


def shift_month(key: str, delta: int) -> str:
    y, m = int(key[:4]), int(key[5:7])
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def month_label(key: str) -> str:
    return date(int(key[:4]), int(key[5:7]), 1).strftime("%b %Y")


def month_period(key: str) -> Period:
    try:
        first = date(int(key[:4]), int(key[5:7]), 1)
    except ValueError:
        raise PeriodError(f"'{key}' is not a valid month (use YYYY-MM).") from None
    nxt = date(*map(int, shift_month(key, 1).split("-")), 1)
    return Period(month_label(key), *_utc(_midnight(first), _midnight(nxt)))


def current_month() -> Period:
    p = month_period(month_key())
    return Period(month_key(), p.start, p.end)


# ------------------------------------------------------------------ any period text
def resolve_period(name: str) -> Period:
    text = (name or "all_time").lower().strip()
    raw = text.replace(" ", "") if ".." in text else "_".join(text.split())  # 'this month' = this_month; '2026-10-01 .. 2026-10-15' = a range
    today = now_local().date()
    tomorrow = _midnight(today + timedelta(days=1))

    if m := _RANGE_RE.match(raw):
        try:
            a, b = date.fromisoformat(m[1]), date.fromisoformat(m[2])
        except ValueError:
            raise PeriodError("That date range isn't valid (use YYYY-MM-DD..YYYY-MM-DD).") from None
        if b < a:
            a, b = b, a
        return Period(f"{a:%d %b} - {b:%d %b %Y}", *_utc(_midnight(a), _midnight(b + timedelta(days=1))))
    if m := _DAY_RE.match(raw):
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            raise PeriodError("That date isn't valid (use YYYY-MM-DD).") from None
        return Period(f"{d:%d %b %Y}", *_utc(_midnight(d), _midnight(d + timedelta(days=1))))
    if _MONTH_RE.match(raw):
        return month_period(raw)

    if raw == "today":
        return Period("today", *_utc(_midnight(today), tomorrow))
    if raw == "yesterday":
        return Period("yesterday", *_utc(_midnight(today - timedelta(days=1)), _midnight(today)))
    if raw == "this_week":
        return Period("this week", *_utc(_midnight(today - timedelta(days=today.weekday())), tomorrow))
    if raw == "last_7_days":
        return Period("last 7 days", *_utc(_midnight(today - timedelta(days=6)), tomorrow))
    if raw == "last_30_days":
        return Period("last 30 days", *_utc(_midnight(today - timedelta(days=29)), tomorrow))
    if raw == "this_month":
        p = month_period(month_key())
        return Period(f"this month ({p.label})", p.start, p.end)
    if raw == "last_month":
        p = month_period(shift_month(month_key(), -1))
        return Period(f"last month ({p.label})", p.start, p.end)
    if raw == "this_year":
        return Period(f"{today.year}", *_utc(_midnight(date(today.year, 1, 1)), tomorrow))
    if raw in ("all_time", "all", "alltime"):
        return Period("all time", None, None)
    raise PeriodError(f"I don't understand the period '{name}'. Use: {PERIOD_HELP}.")


def parse_when(date_str: str | None) -> datetime | None:
    """'2026-10-18' -> that day (keeps current clock time for today, noon otherwise)."""
    if not date_str:
        return None
    try:
        day = datetime.strptime(date_str.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    now = now_local()
    if day == now.date():
        return None
    if day > now.date():
        return None  # no future-dated entries
    return datetime.combine(day, time(12, 0), tzinfo=tz())
