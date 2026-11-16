from decimal import ROUND_HALF_UP, Decimal

from finance_agent.config import settings


def q2(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def money(value) -> str:
    d = q2(value)
    n = f"{d:,.0f}" if d == d.to_integral_value() else f"{d:,.2f}"
    return f"{settings.currency_symbol}{n}"
