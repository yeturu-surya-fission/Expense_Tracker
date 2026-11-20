"""Gist 2: the model reads the policy, plain code does the sums.

Illustrative sketch of how per-day and shared (per-claim) caps are applied with exact decimals.
The real implementation is finance_agent/policy/engine.py.
"""

from decimal import Decimal


def per_day_cap(limit: Decimal, days: int) -> Decimal:
    """Lunch: max 350 a day. Two days of lunches are limited to 700."""
    return limit * days


def pay_pooled(limit: Decimal, claimed: list[Decimal]) -> list[Decimal]:
    """All ground transport combined must not exceed 5000 per trip: one limit shared by every item."""
    left, paid = limit, []
    for amount in claimed:
        take = min(amount, left)
        paid.append(take)
        left -= take
    return paid


assert per_day_cap(Decimal("350"), 2) == Decimal("700")
# a 3000 cab plus a 3500 train pays 5000 in total, not 6500
assert sum(pay_pooled(Decimal("5000"), [Decimal("3000"), Decimal("3500")])) == Decimal(
    "5000"
)
