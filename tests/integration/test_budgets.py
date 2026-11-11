"""Budgets: set, list, remove — plus the alert hook firing on a spend."""
from sqlalchemy import select

from backend.db.models import Budget
from backend.db.session import session_scope


async def _budgets(uid: int) -> list[Budget]:
    async with session_scope() as s:
        return list((await s.execute(select(Budget).where(Budget.user_id == uid))).scalars().all())


async def test_set_list_update_remove(make_user, tools_for):
    uid, _ = await make_user()
    async with tools_for(uid) as t:
        out = await t["set_budget"].ainvoke({"category": "food", "monthly_limit": 3000})
        assert "food" in out and ("₹3,000" in out or "3000" in out)
        listed = await t["get_budgets"].ainvoke({})
        assert "food" in listed

        await t["set_budget"].ainvoke({"category": "food", "monthly_limit": 5000})  # update, not duplicate
        rows = await _budgets(uid)
        assert len(rows) == 1 and float(rows[0].monthly_limit) == 5000.0

        out = await t["remove_budget"].ainvoke({"category": "food"})
        assert "food" in out
        assert await _budgets(uid) == []
        again = await t["remove_budget"].ainvoke({"category": "food"})
        assert isinstance(again, str)  # friendly message, not a crash


async def test_spend_against_budget_does_not_crash(make_user, tools_for):
    uid, _ = await make_user()
    async with tools_for(uid) as t:
        await t["set_budget"].ainvoke({"category": "food", "monthly_limit": 100})
        out = await t["add_expenses"].ainvoke(
            {"items": [{"description": "snack", "amount": 90, "category": "food"}]})
    assert "Spent" in out  # alert thresholds are announcements only; 90% of 100 must not break the write
