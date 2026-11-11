"""Expense tools end-to-end through the live MCP server and the real Postgres."""
from sqlalchemy import select

from backend.db.models import Expense
from backend.db.session import session_scope


async def _rows(uid: int) -> list[Expense]:
    async with session_scope() as s:
        return list((await s.execute(
            select(Expense).where(Expense.user_id == uid).order_by(Expense.id))).scalars().all())


async def test_add_and_verify_row(make_user, tools_for):
    uid, _ = await make_user()
    async with tools_for(uid) as t:
        out = await t["add_expenses"].ainvoke(
            {"items": [{"description": "coffee", "amount": 10, "category": "food"}]})
    assert "Spent" in out and "₹10" in out and "#" in out
    rows = await _rows(uid)
    assert len(rows) == 1
    assert float(rows[0].amount) == 10.0
    assert rows[0].category == "food"
    assert rows[0].voided_at is None


async def test_undo_voids_and_second_undo_fails_cleanly(make_user, tools_for):
    uid, _ = await make_user()
    async with tools_for(uid) as t:
        await t["add_expenses"].ainvoke(
            {"items": [{"description": "coffee", "amount": 10, "category": "food"}]})
        out = await t["undo_last_expense"].ainvoke({})
        out2 = await t["undo_last_expense"].ainvoke({})
    assert "Undone" in out
    assert "no matching expense" in out2.lower() or "nothing" in out2.lower()
    assert all(r.voided_at is not None for r in await _rows(uid))


async def test_reports_read_what_was_written(make_user, tools_for):
    uid, _ = await make_user()
    async with tools_for(uid) as t:
        await t["add_expenses"].ainvoke(
            {"items": [{"description": "bus", "amount": 25, "category": "transport"}]})
        summary = await t["spending_summary"].ainvoke({})
        listed = await t["get_expenses"].ainvoke({})
        daily = await t["daily_breakdown"].ainvoke({})
        trend = await t["monthly_trend"].ainvoke({})
    assert "₹25" in summary
    assert "bus" in listed
    assert "₹25" in daily
    assert trend  # any non-empty month table


async def test_amount_must_be_positive(make_user, tools_for):
    uid, _ = await make_user()
    async with tools_for(uid) as t:
        try:
            await t["add_expenses"].ainvoke(
                {"items": [{"description": "x", "amount": -5, "category": "food"}]})
            raised = False
        except RuntimeError:
            raised = True  # server-side validation error -> is_error -> bridge raises
    assert raised
    assert await _rows(uid) == []
