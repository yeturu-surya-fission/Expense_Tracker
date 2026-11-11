"""One-off splits: HITL question -> recall -> dues -> cancel. Notifier is NullNotifier, so
'notified' assertions stay lenient by design."""
from sqlalchemy import select

from backend.db.models import Debt
from backend.db.session import session_scope


async def _debts(uid: int) -> list[Debt]:
    async with session_scope() as s:
        return list((await s.execute(select(Debt).where(Debt.debtor_id == uid))).scalars().all())


async def test_split_flow_question_recall_dues_cancel(make_user, tools_for):
    uid, _ = await make_user()
    b_uid, b_tg = await make_user("Surya", started=False)
    async with tools_for(uid) as t:
        await t["add_friend"].ainvoke({"nickname": "Surya", "telegram_id": b_tg})

        # 1. no split= -> the bot asks instead of writing anything
        question = await t["split_expense"].ainvoke({"amount": 300, "description": "dinner",
                                                     "with_friends": ["Surya"]})
        assert "Reply with" in question and "Equally" in question
        assert await _debts(b_uid) == []  # nothing written before the question is answered

        # 2. the recall carries the answer -> the split is created
        out = await t["split_expense"].ainvoke({"amount": 300, "description": "dinner",
                                                "with_friends": ["Surya"], "split": "equal"})
        assert "Split #" in out and "₹150" in out
        split_id = int(out.split("(split #")[1].split(")")[0])

        # 3. the friend owes their half
        async with tools_for(b_uid) as t2:
            dues = await t2["my_dues"].ainvoke({})
        assert "150" in dues

        # 4. cancel: everyone is told (nobody here), dues are gone
        cancelled = await t["cancel_split"].ainvoke({})
        assert f"Cancelled split #{split_id}" in cancelled
        async with tools_for(b_uid) as t2:
            dues_after = await t2["my_dues"].ainvoke({})
        assert "150" not in dues_after


async def test_split_with_unknown_friend_fails_politely(make_user, tools_for):
    uid, _ = await make_user()
    async with tools_for(uid) as t:
        out = await t["split_expense"].ainvoke({"amount": 100, "description": "x",
                                                "with_friends": ["Nobody"], "split": "equal"})
    assert "❌" in out


async def test_exact_split_takes_named_amounts(make_user, tools_for):
    uid, _ = await make_user()
    _, b_tg = await make_user("Surya", started=False)
    async with tools_for(uid) as t:
        await t["add_friend"].ainvoke({"nickname": "Surya", "telegram_id": b_tg})
        out = await t["split_expense"].ainvoke({
            "amount": 500, "description": "tickets", "with_friends": ["Surya"], "split": "exact",
            "exact_amounts": [{"name": "me", "amount": 200}, {"name": "Surya", "amount": 300}]})
    assert "₹200" in out and "₹300" in out
