"""Calendar helpers the chat client needs for its month buttons (the server does the real period maths)."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import settings


def now_local() -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def month_key(ref: datetime | None = None) -> str:
    ref = ref or now_local()
    return f"{ref.year:04d}-{ref.month:02d}"


def shift_month(key: str, delta: int) -> str:
    y, m = int(key[:4]), int(key[5:7])
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def month_label(key: str) -> str:
    return date(int(key[:4]), int(key[5:7]), 1).strftime("%b %Y")
