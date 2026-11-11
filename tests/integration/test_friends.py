"""Friends: add, list, remove (and removing an unknown one fails politely)."""
from sqlalchemy import select

from backend.db.models import Friend
from backend.db.session import session_scope


async def test_add_list_remove(make_user, tools_for):
    uid, _ = await make_user()
    _, tg_b = await make_user("Surya")
    _, tg_c = await make_user("Ravi")
    async with tools_for(uid) as t:
        out = await t["add_friend"].ainvoke({"nickname": "Surya", "telegram_id": tg_b})
        assert "Surya" in out
        await t["add_friend"].ainvoke({"nickname": "Ravi", "telegram_id": tg_c})
        listed = await t["list_friends"].ainvoke({})
        assert "Surya" in listed and "Ravi" in listed
        await t["remove_friend"].ainvoke({"nickname": "Ravi"})
        after = await t["list_friends"].ainvoke({})
        assert "Ravi" not in after and "Surya" in after


async def test_remove_unknown_friend_is_friendly(make_user, tools_for):
    uid, _ = await make_user()
    async with tools_for(uid) as t:
        out = await t["remove_friend"].ainvoke({"nickname": "Ghost"})
    assert "❌" in out and "Ghost" in out  # a friendly explanation, not a crash


async def test_friend_rows_are_scoped_to_the_owner(make_user, tools_for):
    owner, _ = await make_user()
    _, tg_b = await make_user("Surya")
    other, _ = await make_user("SomeoneElse")
    async with tools_for(owner) as t:
        await t["add_friend"].ainvoke({"nickname": "Surya", "telegram_id": tg_b})
    async with session_scope() as s:
        rows = list((await s.execute(select(Friend).where(Friend.owner_id == owner))).scalars().all())
    assert len(rows) == 1
    async with tools_for(other) as t:
        listed = await t["list_friends"].ainvoke({})
    async with session_scope() as s:
        other_rows = list((await s.execute(select(Friend).where(Friend.owner_id == other))).scalars().all())
    assert other_rows == []  # the help text may mention examples; only real rows matter
