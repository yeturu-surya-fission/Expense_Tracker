"""Group lifecycle: membership, HITL 'always equally', summary, settle confirmation."""
from sqlalchemy import select

from backend.db.models import Group, GroupMember
from backend.db.session import session_scope


async def _make_group(uid: int, chat_id: int, name: str = "Trip") -> int:
    async with session_scope() as s:
        group = Group(chat_id=chat_id, name=name, created_by=uid)
        s.add(group)
        await s.flush()
        s.add(GroupMember(group_id=group.id, user_id=uid))
        return group.id


GROUP_CHAT = -999_000_001


async def test_group_expense_ask_then_always_equal(make_user, tools_for):
    uid, _ = await make_user()
    await _make_group(uid, GROUP_CHAT)
    async with tools_for(uid, "group", GROUP_CHAT) as t:
        # 1. no split= and no group default -> the bot asks
        question = await t["add_group_expense"].ainvoke({"amount": 600, "description": "hotel"})
        assert "Reply with" in question and "Always equally" in question

        # 2. the recall saves the default AND records the expense
        out = await t["add_group_expense"].ainvoke({"amount": 600, "description": "hotel",
                                                    "split": "always_equal"})
        assert "₹600" in out and "#" in out

        # 3. the default now holds: the next expense goes straight through
        out2 = await t["add_group_expense"].ainvoke({"amount": 300, "description": "petrol"})
        assert "₹300" in out2 and "Split equally" in out2

        summary = await t["group_summary"].ainvoke({})
        assert "hotel" in summary or "₹600" in summary


async def test_group_summary_and_settle_confirmation(make_user, tools_for):
    uid, _ = await make_user()
    await _make_group(uid, GROUP_CHAT)
    async with tools_for(uid, "group", GROUP_CHAT) as t:
        await t["add_group_expense"].ainvoke({"amount": 600, "description": "hotel", "split": "equal"})
        summary = await t["group_summary"].ainvoke({})
        assert "₹600" in summary

        # settle is a two-step HITL: first call only asks
        question = await t["settle_group"].ainvoke({})
        assert "Settle up" in question and "Reply with" in question
        # confirm=True goes through with the NullNotifier (sends nothing, marks delivered=None)
        done = await t["settle_group"].ainvoke({"confirm": True})
        assert isinstance(done, str) and done


async def test_private_context_rejected_for_group_tools(make_user, tools_for):
    uid, _ = await make_user()
    async with tools_for(uid) as t:  # private allow-list doesn't even contain group tools
        assert "add_group_expense" not in t and "group_summary" not in t
        assert "settle_group" not in t and "cancel_split" in t  # cancel_split is shared


async def test_group_chat_without_setup_is_friendly(make_user, tools_for):
    uid, _ = await make_user()
    async with tools_for(uid, "group", -888_777) as t:  # no Group row for this chat id
        out = await t["add_group_expense"].ainvoke({"amount": 10, "description": "x"})
    assert "not set up" in out
