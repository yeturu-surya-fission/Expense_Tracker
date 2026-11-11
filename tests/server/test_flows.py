"""The expense features end to end, through MCP calls: expenses, budgets, splits, human-in-the-loop, groups, payments, outbox."""
from sqlalchemy import func, select, update

from mcp_server.db.models import Debt, Expense, Outbox
from mcp_server.db.session import session_scope
from mcp_server import jobs
from tests.harness import new_telegram_id


async def drain(h) -> list[dict]:
    """Claim and acknowledge everything waiting in the outbox (so a test starts from an empty queue); returns what was there."""
    seen = []
    while True:
        msgs = (await h.platform("platform_outbox_claim", limit=50))["data"]["messages"]
        if not msgs:
            return seen
        for m in msgs:
            seen.append(m)
            await h.platform("platform_outbox_ack", outbox_id=m["id"], message_id=1000 + m["id"])


def for_person(msgs, telegram_id):
    return [m for m in msgs if m["telegram_id"] == telegram_id and m["kind"] == "send"]


async def count_expenses(user_id: int) -> int:
    async with session_scope() as s:
        return (await s.execute(select(func.count()).select_from(Expense).where(Expense.user_id == user_id, Expense.voided_at.is_(None)))).scalar_one()


async def befriend(owner, other, nickname):
    await owner.ok("add_friend", nickname=nickname, telegram_id=other.telegram_id)


# ------------------------------------------------------------------ personal expenses and budgets
async def test_add_undo_and_summary(harness, alice):
    r = await alice.ok("add_expenses", items=[
        {"amount": 300, "category": "Food", "description": "ice cream"},
        {"amount": 1200, "category": "fuel", "description": "petrol"},
    ])
    assert len(r["data"]["expense_ids"]) == 2 and "₹300" in r["text"]
    assert "1,500" in (await alice.ok("spending_summary"))["text"]
    undone = await alice.ok("undo_last_expense")
    assert "petrol" in undone["text"]
    assert "300" in (await alice.ok("spending_summary"))["text"]
    again = await alice.call("undo_last_expense", expense_id=undone["data"]["expense_ids"][0])
    assert again["status"] == "rejected"  # already undone: a normal 'no', not a crash


async def test_budget_alerts_fire_once_per_threshold(harness, alice):
    await alice.ok("set_budget", category="food", monthly_limit=1000)
    first = await alice.ok("add_expenses", items=[{"amount": 520, "category": "food", "description": "a"}])
    assert "50%" in first["text"]
    second = await alice.ok("add_expenses", items=[{"amount": 10, "category": "food", "description": "b"}])
    assert "50%" not in second["text"]  # already warned this month
    third = await alice.ok("add_expenses", items=[{"amount": 300, "category": "food", "description": "c"}])
    assert "80%" in third["text"]


async def test_bad_input_is_a_rejection_not_an_error(harness, alice):
    r = await alice.call("set_budget", category="food", monthly_limit=-5)
    assert r["status"] in ("rejected", "error")
    r = await alice.call("spending_summary", period="not-a-period")
    assert r["status"] == "rejected" and r["text"].startswith("❌")


async def test_friends(harness, alice, bob):
    await befriend(alice, bob, "Bobby")
    assert "Bobby" in (await alice.ok("list_friends"))["text"]
    dup = await alice.call("add_friend", nickname="bobby", telegram_id=new_telegram_id())
    assert dup["status"] == "rejected"  # nickname already used by another friend
    await alice.ok("remove_friend", nickname="Bobby")
    assert "Bobby" not in (await alice.ok("list_friends"))["text"]


# ------------------------------------------------------------------ human in the loop
async def test_split_asks_first_and_writes_nothing_until_answered(harness, alice, bob):
    await befriend(alice, bob, "Bob")
    args = dict(amount=900, description="dinner", with_friends=["Bob"])

    first = await alice.call("split_expense", **args)
    assert first["status"] == "input_required"
    req = first["input_requests"][0]
    assert req["key"] == "split_mode" and {o["id"] for o in req["options"]} == {"equal", "exact"}
    # nothing committed: no expense for Alice, nothing owed by Bob
    assert await count_expenses(alice.user_id) == 0
    assert "nothing" in (await bob.ok("my_dues"))["text"].lower() or "owe" not in (await bob.ok("my_dues"))["text"].lower()

    done = await alice.call("split_expense", **args, answers={"split_mode": "equal"})
    assert done["status"] == "ok" and done["data"]["split_ids"]
    assert await count_expenses(alice.user_id) == 1  # her own ₹450 share
    assert "450" in (await bob.ok("my_dues"))["text"]


async def test_splitting_an_expense_just_logged_does_not_count_it_twice(harness, alice, bob):
    await befriend(alice, bob, "Bob")
    await alice.ok("add_expenses", items=[{"amount": 500, "category": "fuel", "description": "petrol"}])
    r = await alice.ok("split_expense", amount=500, description="petrol", category="fuel", with_friends=["Bob"], split="equal")
    assert "replaced" in r["text"]
    assert "250" in (await alice.ok("spending_summary"))["text"] and "750" not in (await alice.ok("spending_summary"))["text"]
    assert await count_expenses(alice.user_id) == 1  # only her ₹250 share

    # a different amount is a different purchase: it is kept
    await alice.ok("add_expenses", items=[{"amount": 80, "category": "food", "description": "tea"}])
    await alice.ok("split_expense", amount=500, description="movie", category="fun", with_friends=["Bob"], split="equal")
    assert await count_expenses(alice.user_id) == 3


async def test_asking_twice_with_the_same_answer_writes_once(harness, alice, bob):
    await befriend(alice, bob, "Bob")
    args = dict(amount=100, description="tea", with_friends=["Bob"], answers={"split_mode": "equal"}, idempotency_key="call-1")
    a = await alice.call("split_expense", **args)
    b = await alice.call("split_expense", **args)  # e.g. the agent re-runs the step after a crash
    assert a == b and a["status"] == "ok"
    async with session_scope() as s:
        n = (await s.execute(select(func.count()).select_from(Debt).where(Debt.creditor_id == alice.user_id))).scalar_one()
    assert n == 1


async def test_cancelling_the_question_records_nothing(harness, alice, bob):
    await befriend(alice, bob, "Bob")
    r = await alice.call("split_expense", amount=100, description="tea", with_friends=["Bob"], answers={"split_mode": "cancel"})
    assert r["status"] == "ok" and "Cancelled" in r["text"]
    assert await count_expenses(alice.user_id) == 0


async def test_an_answer_that_is_not_an_option_asks_again(harness, alice, bob):
    await befriend(alice, bob, "Bob")
    r = await alice.call("split_expense", amount=100, description="tea", with_friends=["Bob"], answers={"split_mode": "banana"})
    assert r["status"] == "input_required"


async def test_big_split_asks_two_questions_one_after_the_other(harness, alice):
    friends = [await harness.person(n) for n in ("P1", "P2", "P3")]
    for f in friends:
        await befriend(alice, f, f.name)
    args = dict(amount=4000, description="trip lunch", with_friends=["P1", "P2", "P3"])
    q1 = await alice.call("split_expense", **args)
    assert q1["input_requests"][0]["key"] == "kind"
    q2 = await alice.call("split_expense", **args, answers={"kind": "one_off"})
    assert q2["input_requests"][0]["key"] == "split_mode"
    done = await alice.call("split_expense", **args, answers={"kind": "one_off", "split_mode": "equal"})
    assert done["status"] == "ok" and "1,000" in done["text"]
    grp = await alice.call("split_expense", **args, answers={"kind": "group"})
    assert grp["status"] == "ok" and "group" in grp["text"].lower() and not grp["data"]  # just the how-to, nothing saved


async def test_exact_split_needs_amounts_and_they_must_add_up(harness, alice, bob):
    await befriend(alice, bob, "Bob")
    r = await alice.call("split_expense", amount=1000, description="x", with_friends=["Bob"], split="exact",
                         exact_amounts=[{"name": "me", "amount": 400}, {"name": "Bob", "amount": 500}])
    assert r["status"] == "rejected"
    ok = await alice.call("split_expense", amount=1000, description="x", with_friends=["Bob"], split="exact",
                          exact_amounts=[{"name": "me", "amount": 400}, {"name": "Bob", "amount": 600}])
    assert ok["status"] == "ok" and "600" in (await bob.ok("my_dues"))["text"]


# ------------------------------------------------------------------ the payment lifecycle, messages through the outbox
async def test_pay_confirm_and_reject_lifecycle_with_outbox(harness, alice, bob):
    await befriend(alice, bob, "Bob")
    await drain(harness)
    r = await alice.ok("split_expense", amount=900, description="dinner", with_friends=["Bob"], split="equal")
    assert r["data"]["start_users"] == []  # Bob pressed Start (his session was opened with started=True)

    sent = for_person(await drain(harness), bob.telegram_id)
    assert len(sent) == 1 and "Your share: ₹450" in sent[0]["text"]
    assert sent[0]["buttons"][0][0]["data"].endswith(":paid")
    debt_id = int(sent[0]["buttons"][0][0]["data"].split(":")[1])

    # the stored message id is written back by the acknowledgement
    async with session_scope() as s:
        assert (await s.get(Debt, debt_id)).debtor_msg_id == 1000 + sent[0]["id"]

    paid = await bob.ok("debt_action", debt_id=debt_id, action="paid")
    assert "added to your expenses" in paid["text"] and await count_expenses(bob.user_id) == 1
    claim = for_person(await drain(harness), alice.telegram_id)
    assert len(claim) == 1 and "says they paid you" in claim[0]["text"]
    assert [b["data"].split(":")[2] for b in claim[0]["buttons"][0]] == ["got", "notyet"]

    # only the creditor can answer
    assert (await bob.call("debt_action", debt_id=debt_id, action="got"))["status"] == "rejected"

    nope = await alice.ok("debt_action", debt_id=debt_id, action="notyet")
    assert "still unpaid" in nope["text"] and await count_expenses(bob.user_id) == 0  # expense taken out again
    back = for_person(await drain(harness), bob.telegram_id)
    assert any("haven't received" in m["text"] for m in back)

    await bob.ok("debt_action", debt_id=debt_id, action="paid")
    await drain(harness)
    done = await alice.ok("debt_action", debt_id=debt_id, action="got")
    assert "All settled" in done["text"]
    assert any("confirmed your payment" in m["text"] for m in for_person(await drain(harness), bob.telegram_id))
    assert (await bob.call("debt_action", debt_id=debt_id, action="paid"))["status"] == "rejected"  # already settled


async def test_cancelling_a_split_tells_the_friend_and_removes_their_old_message(harness, alice, bob):
    await befriend(alice, bob, "Bob")
    await drain(harness)
    r = await alice.ok("split_expense", amount=500, description="cab", with_friends=["Bob"], split="equal")
    await drain(harness)
    out = await alice.ok("cancel_split", split_id=r["data"]["split_ids"][0])
    msgs = await drain(harness)
    assert any(m["kind"] == "delete" and m["telegram_id"] == bob.telegram_id for m in msgs)
    assert any("cancelled the split" in (m["text"] or "") for m in msgs)
    assert "owe" not in (await bob.ok("my_dues"))["text"].lower() or "nothing" in (await bob.ok("my_dues"))["text"].lower()


async def test_person_who_never_pressed_start_gets_bill_when_they_do(harness, alice):
    ghost_tg = new_telegram_id()
    await alice.ok("add_friend", nickname="Ghost", telegram_id=ghost_tg)
    await drain(harness)
    r = await alice.ok("split_expense", amount=600, description="pizza", with_friends=["Ghost"], split="equal")
    assert "not notified yet" in r["text"] and r["data"]["start_users"][0]["telegram_id"] == ghost_tg
    assert for_person(await drain(harness), ghost_tg) == []  # nothing could be sent
    ghost = await harness.person("Ghost", telegram_id=ghost_tg, started=True)  # they press Start
    waiting = for_person(await drain(harness), ghost_tg)
    assert len(waiting) == 1 and "Your share: ₹300" in waiting[0]["text"]
    assert ghost.user_id


async def test_blocked_delivery_is_remembered_and_redelivered_on_start(harness, alice, bob):
    await befriend(alice, bob, "Bob")
    await drain(harness)
    await alice.ok("split_expense", amount=200, description="chai", with_friends=["Bob"], split="equal")
    msgs = (await harness.platform("platform_outbox_claim", limit=50))["data"]["messages"]
    mine = for_person(msgs, bob.telegram_id)[0]
    for m in msgs:
        if m is mine:
            await harness.platform("platform_outbox_ack", outbox_id=m["id"], blocked=True, error="forbidden")
        else:
            await harness.platform("platform_outbox_ack", outbox_id=m["id"], message_id=5)
    async with session_scope() as s:
        row = await s.get(Outbox, mine["id"])
        assert row.status == "blocked"
    # Bob is no longer reachable until he presses Start again; then the bill comes back
    assert for_person(await drain(harness), bob.telegram_id) == []
    await harness.person("Bob", telegram_id=bob.telegram_id, started=True)
    again = for_person(await drain(harness), bob.telegram_id)
    assert len(again) == 1 and "Your share" in again[0]["text"]


async def test_unacknowledged_messages_come_back_and_failures_retry(harness, alice, bob):
    await befriend(alice, bob, "Bob")
    await drain(harness)
    await alice.ok("split_expense", amount=200, description="chai", with_friends=["Bob"], split="equal")
    first = (await harness.platform("platform_outbox_claim", limit=50))["data"]["messages"]
    assert for_person(first, bob.telegram_id)
    assert (await harness.platform("platform_outbox_claim", limit=50))["data"]["messages"] == []  # leased: nobody else gets it
    async with session_scope() as s:  # the deliverer crashed: the lease runs out
        await s.execute(update(Outbox).where(Outbox.status == "leased").values(leased_until=func.now() - func.make_interval(0, 0, 0, 0, 0, 0, 1)))
    second = (await harness.platform("platform_outbox_claim", limit=50))["data"]["messages"]
    assert for_person(second, bob.telegram_id)
    for m in second:
        r = await harness.platform("platform_outbox_ack", outbox_id=m["id"], error="network down")
        assert r["data"]["status"] == "pending"  # will be retried later
    async with session_scope() as s:
        await s.execute(update(Outbox).where(Outbox.status == "pending").values(available_at=func.now()))
    await drain(harness)


async def test_reminders_are_queued_by_the_server(harness, alice, bob):
    await befriend(alice, bob, "Bob")
    await drain(harness)
    r = await alice.ok("split_expense", amount=300, description="movie", with_friends=["Bob"], split="equal")
    await drain(harness)
    async with session_scope() as s:  # make it old enough to need a nudge
        await s.execute(update(Debt).where(Debt.creditor_id == alice.user_id).values(created_at=func.now() - func.make_interval(0, 0, 0, 5)))
    assert await jobs.run_reminders_once() >= 1
    msgs = for_person(await drain(harness), bob.telegram_id)
    assert msgs and "Reminder" in msgs[0]["text"]
    assert await jobs.run_reminders_once() == 0  # not again straight away
    assert r["status"] == "ok"


# ------------------------------------------------------------------ several bills, different payers (netting)
async def test_party_night_nets_everyone_to_the_fewest_payments(harness, alice):
    surya, komal = await harness.person("Surya"), await harness.person("Komal")
    await befriend(alice, surya, "surya")
    await befriend(alice, komal, "Komal Kumar")
    await drain(harness)
    r = await alice.ok("split_bills", with_friends=["surya", "Komal Kumar"], title="party night", bills=[
        {"amount": 2500, "description": "party food", "paid_by": "me"},
        {"amount": 600, "description": "ice cream", "paid_by": "surya"},
        {"amount": 3500, "description": "bar", "paid_by": "Komal Kumar"},
    ])
    text = r["text"]
    assert "surya → Komal Kumar  ₹1,300" in text and "surya → You  ₹300" in text
    assert "2,200" in text  # equal share of 6,600 between three
    sent = await drain(harness)
    assert len(for_person(sent, surya.telegram_id)) >= 1 and any("1,300" in m["text"] for m in for_person(sent, surya.telegram_id))


# ------------------------------------------------------------------ group chats
def new_chat() -> int:
    return -1_000_000_000 - new_telegram_id() % 10**9


async def member(harness, name, chat, title=None, started=True):
    """A person in a group chat. `started`: they have also pressed Start on the bot in private, so bills can reach them."""
    tg = new_telegram_id()
    if started:
        await harness.person(name, telegram_id=tg, started=True)
    return await harness.person(name, telegram_id=tg, group_chat_id=chat, group_title=title)


async def test_group_expense_asks_once_then_remembers_equal(harness):
    chat = new_chat()
    ana = await harness.person("Ana", group_chat_id=chat, group_title="Goa Trip")
    ben = await harness.person("Ben", group_chat_id=chat)  # speaking in the group makes him a member
    q = await ana.call("add_group_expense", amount=1000, description="petrol")
    assert q["status"] == "input_required" and {o["id"] for o in q["input_requests"][0]["options"]} == {"equal", "always_equal", "exact"}
    ok = await ana.call("add_group_expense", amount=1000, description="petrol", answers={"split_mode": "always_equal"})
    assert ok["status"] == "ok" and "500" in ok["text"]
    again = await ben.call("add_group_expense", amount=300, description="snacks")
    assert again["status"] == "ok"  # the group's default is now 'equal': no question
    summary = await ana.ok("group_summary")
    assert "Goa Trip" in summary["text"] and "petrol" in summary["text"]


async def test_group_paid_by_someone_else(harness):
    chat = new_chat()
    ana = await harness.person("Ana", group_chat_id=chat, group_title="Flat")
    ben = await harness.person("Ben", group_chat_id=chat)
    r = await ana.call("add_group_expense", amount=600, description="ice cream", paid_by="Ben", split="equal")
    assert r["status"] == "ok" and "Ben paid ₹600" in r["text"]
    assert ben.user_id


async def test_settle_group_confirmation_then_bills_in_private(harness):
    chat = new_chat()
    ana = await member(harness, "Ana", chat, "Hampi")
    ben = await member(harness, "Ben", chat)
    cat = await member(harness, "Cat", chat)
    await ana.ok("add_group_expense", amount=900, description="stay", split="equal")
    await ben.ok("add_group_expense", amount=300, description="food", split="equal")
    await drain(harness)

    ask = await ana.call("settle_group")
    assert ask["status"] == "input_required" and ask["input_requests"][0]["key"] == "settle"
    assert (await ana.ok("group_summary"))["text"].count("stay") >= 1  # still open: asking changed nothing

    cancelled = await ana.call("settle_group", answers={"settle": "cancel"})
    assert cancelled["status"] == "ok" and "not settling" in cancelled["text"]

    done = await ana.call("settle_group", answers={"settle": "yes"}, idempotency_key="settle-1")
    assert done["status"] == "ok" and "Hampi settled" in done["text"]
    assert done["data"]["start_users"] == []
    sent = await drain(harness)
    # Cat owes (400 - 0): one bill; Ben owes 400-300=... everyone with something to pay gets a button
    owing = [m for m in sent if m["buttons"] and m["buttons"][0][0]["data"].endswith(":paid")]
    assert owing and all("Hampi" in m["text"] for m in owing)
    assert {m["telegram_id"] for m in owing} <= {ben.telegram_id, cat.telegram_id}
    empty = await ana.call("settle_group")
    assert "Nothing to settle" in empty["text"]


async def test_members_who_never_pressed_start_are_reported_for_tagging(harness):
    chat = new_chat()
    ana = await member(harness, "Ana", chat, "Pune")
    ghost_tg = new_telegram_id()
    await harness.platform("platform_group_event", event="members_joined", chat_id=chat, members=[{"telegram_id": ghost_tg, "name": "Ghost"}])
    await ana.ok("add_group_expense", amount=300, description="chai", split="equal")
    await drain(harness)
    done = await ana.call("settle_group", answers={"settle": "yes"})
    assert [u["telegram_id"] for u in done["data"]["start_users"]] == [ghost_tg]


async def test_missing_members_note_uses_the_platforms_member_count(harness):
    chat = new_chat()
    ana = await harness.person("Ana", group_chat_id=chat, group_title="Big group")
    await ana.ok("add_group_expense", amount=100, description="x", split="equal")
    text = (await ana.ok("group_summary", member_count=6))["text"]  # Telegram says 6 incl. the bot: we know 1 of 5
    assert "only know 1 of the 5" in text


# ------------------------------------------------------------------ group events from the platform
async def test_platform_group_events(harness):
    chat = new_chat()
    adder_tg = new_telegram_id()
    friend_tg = new_telegram_id()
    friend_owner = await harness.person("Adder", telegram_id=adder_tg)
    await friend_owner.ok("add_friend", nickname="Pal", telegram_id=friend_tg)
    ids = (await harness.platform("platform_friend_ids", telegram_id=adder_tg))["data"]["telegram_ids"]
    assert friend_tg in ids

    added = await harness.platform("platform_group_event", event="bot_added", chat_id=chat, title="  Road   trip ",
                                   actor={"telegram_id": adder_tg, "name": "Adder"}, present_telegram_ids=[friend_tg])
    d = added["data"]
    assert d["action"] == "ok" and d["created"] and d["group_name"] == "Road trip" and len(d["members"]) == 2
    again = await harness.platform("platform_group_event", event="bot_added", chat_id=chat, title="Road trip", actor={"telegram_id": adder_tg})
    assert again["data"]["created"] is False

    joiner = new_telegram_id()
    await harness.platform("platform_group_event", event="members_joined", chat_id=chat, members=[{"telegram_id": joiner, "name": "Joiner"}])
    adder = await harness.person("Adder", telegram_id=adder_tg, group_chat_id=chat)
    assert "Joiner" in (await adder.ok("agent_context"))["data"]["members"]
    await harness.platform("platform_group_event", event="member_left", chat_id=chat, actor={"telegram_id": joiner})
    assert "Joiner" not in (await adder.ok("agent_context"))["data"]["members"]
    await harness.platform("platform_group_event", event="title_changed", chat_id=chat, title="Renamed")
    assert (await adder.ok("agent_context"))["data"]["group"] == "Renamed"
    new_chat_id = new_chat()
    await harness.platform("platform_group_event", event="migrated", chat_id=chat, new_chat_id=new_chat_id)
    moved = await harness.person("Adder", telegram_id=adder_tg, group_chat_id=new_chat_id)
    assert (await moved.ok("agent_context"))["data"]["group"] == "Renamed"


async def test_bot_added_by_a_stranger_means_leave(harness, monkeypatch):
    from mcp_server.config import settings
    owner = new_telegram_id()
    monkeypatch.setattr(settings, "allowed_telegram_ids", str(owner))
    r = await harness.platform("platform_group_event", event="bot_added", chat_id=new_chat(), title="x", actor={"telegram_id": new_telegram_id()})
    assert r["data"]["action"] == "leave"


async def test_invite_link_makes_both_sides_friends(harness):
    inviter = await harness.person("Inviter")
    newcomer_tg = new_telegram_id()
    await drain(harness)
    r = await harness.platform("platform_accept_invite", person={"telegram_id": newcomer_tg, "name": "Newcomer"}, inviter_telegram_id=inviter.telegram_id)
    assert r["status"] == "ok" and "friends with Inviter" in r["text"]
    newcomer = await harness.person("Newcomer", telegram_id=newcomer_tg)
    assert "Inviter" in (await newcomer.ok("list_friends"))["text"]
    assert "Newcomer" in (await inviter.ok("list_friends"))["text"]
    assert any("joined your friends" in m["text"] for m in for_person(await drain(harness), inviter.telegram_id))
    assert (await harness.platform("platform_accept_invite", person={"telegram_id": 5, "name": "x"}, inviter_telegram_id=5))["status"] == "rejected"


# ------------------------------------------------------------------ views for the buttons and the agent
async def test_ui_views_and_agent_context(harness, alice):
    await alice.ok("set_budget", category="food", monthly_limit=500)
    await alice.ok("add_expenses", items=[{"amount": 50, "category": "food", "description": "tea"}])
    for view in ("summary", "recent", "budgets", "friends", "dues", "daily", "trend", "invite"):
        r = await alice.ok("ui_view", view=view)
        assert r["text"]
    assert "t.me/expense_test_bot?start=inv_" in (await alice.ok("ui_view", view="invite"))["text"]
    ctx = (await alice.ok("agent_context"))["data"]
    assert "food" in ctx["categories"] and ctx["speaker"] == "Alice"
    last = await alice.ok("last_expense")
    assert last["data"]["expense_id"] and "tea" in last["text"]
    undone = await alice.ok("undo_expenses", expense_ids=[last["data"]["expense_id"]])
    assert "Undone" in undone["text"]
    assert (await alice.ok("last_expense"))["data"]["expense_id"] is None


async def test_guardrail_tool_wording_comes_from_the_server(harness, alice):
    off = await alice.ok("respond_to_user", kind="off_topic", message="Sure! def fib(n): ...")
    assert "outside my job" in off["text"] and "fib" not in off["text"]
    essay = await alice.ok("respond_to_user", kind="clarify", message="long " * 200)
    assert len(essay["text"]) <= 205


async def test_dues_show_one_total_per_person(harness, alice, bob):
    await befriend(alice, bob, "Bob")
    for what, amount in (("tea", 100), ("pizza", 600), ("cab", 300)):
        await alice.ok("split_expense", amount=amount, description=what, with_friends=["Bob"], split="equal")
    owed = (await alice.ok("my_dues"))["text"]
    assert owed.count("Bob") == 1 and "₹500" in owed and "tea" not in owed and "pizza" not in owed  # 50 + 300 + 150
    owes = (await bob.ok("my_dues"))["text"]
    assert owes.count("Alice") == 1 and "₹500" in owes


# ------------------------------------------------------------------ netting what two people owe each other
async def test_what_two_friends_owe_each_other_cancels_out(harness, alice, bob):
    await befriend(alice, bob, "Bob")
    await befriend(bob, alice, "Alice")
    await drain(harness)
    await alice.ok("split_expense", amount=400, description="pizza", with_friends=["Bob"], split="equal")  # Bob owes 200
    await drain(harness)
    one = await bob.ok("split_expense", amount=200, description="juice", with_friends=["Alice"], split="equal")  # Alice owes 100
    assert "netted off" in one["text"] and "₹100" in one["text"]
    two = await bob.ok("split_expense", amount=200, description="tea", with_friends=["Alice"], split="equal")  # another 100
    assert "cancelled out" in two["text"] and "all even" in two["text"]
    assert "all clear" in (await alice.ok("my_dues"))["text"].lower() and "all clear" in (await bob.ok("my_dues"))["text"].lower()
    # both people still have their own shares in their expenses, and nothing is owed
    assert await count_expenses(alice.user_id) == 3 and await count_expenses(bob.user_id) == 3
    told = await drain(harness)
    assert any("netted off" in (m["text"] or "") for m in for_person(told, alice.telegram_id))
    assert any("netted off" in (m["text"] or "") for m in for_person(told, bob.telegram_id))


async def test_partial_netting_leaves_one_smaller_total(harness, alice, bob):
    await befriend(alice, bob, "Bob")
    await befriend(bob, alice, "Alice")
    await alice.ok("split_expense", amount=2000, description="trip", with_friends=["Bob"], split="equal")  # Bob owes 1000
    await bob.ok("split_expense", amount=200, description="pizza", with_friends=["Alice"], split="equal")  # Alice owes 100
    await bob.ok("split_expense", amount=200, description="juice", with_friends=["Alice"], split="equal")  # Alice owes 100
    owed = (await alice.ok("my_dues"))["text"]
    assert "Bob  ₹800" in owed and "You owe" not in owed
    owes = (await bob.ok("my_dues"))["text"]
    assert "Alice  ₹800" in owes and "Owed to you" not in owes


async def test_he_paid_then_i_paid_in_one_chat_cancels_out(harness, alice, bob):
    """Everything typed in ONE person's chat: 'Bob paid 100, split with me', then 'I paid 100, split with Bob'."""
    await befriend(alice, bob, "Bob")
    first = await alice.ok("split_expense", amount=100, description="lunch", with_friends=["Bob"], paid_by="Bob", split="equal")
    assert "owe" in first["text"]
    second = await alice.ok("split_expense", amount=100, description="dinner", with_friends=["Bob"], split="equal")
    assert "cancelled out" in second["text"], second["text"]
    assert "all clear" in (await alice.ok("my_dues"))["text"].lower()
    assert "all clear" in (await bob.ok("my_dues"))["text"].lower()


# ------------------------------------------------------------------ paying part of what you owe
async def test_paying_part_keeps_the_rest_due_and_the_friend_confirms(harness, alice, bob):
    await befriend(alice, bob, "Bob")
    await befriend(bob, alice, "Alice")
    for what, amount in (("tea", 100), ("pizza", 600)):  # Bob owes 50 + 300 = 350
        await alice.ok("split_expense", amount=amount, description=what, with_friends=["Bob"], split="equal")
    await drain(harness)

    over = await bob.call("pay_friend", friend="Alice", amount=500)
    assert over["status"] == "rejected" and "only owe" in over["text"]

    part = await bob.ok("pay_friend", friend="Alice", amount=100)  # cuts the second bill in two
    assert "₹100" in part["text"] and "Still to pay Alice: ₹250" in part["text"]
    mine = (await bob.ok("my_dues"))["data"]["dues"]["owe"][0]
    assert mine["total"] == "350.00" and mine["claimed"] == "100.00"  # 100 waiting for Alice, 250 still unpaid
    claim = for_person(await drain(harness), alice.telegram_id)
    assert len(claim) == 1 and "₹100" in claim[0]["text"]
    assert [b["data"].split(":")[0] for b in claim[0]["buttons"][0]] == ["recvfrom", "notyetfrom"]  # she confirms

    await alice.ok("settle_with", person_id=bob.user_id, action="received")
    after = (await bob.ok("my_dues"))["data"]["dues"]["owe"][0]
    assert after["total"] == "250.00" and after["claimed"] == "0"
    rest = await bob.ok("pay_friend", friend="Alice")  # no amount = everything left
    assert "all square" in rest["text"]
    # Bob's own expenses hold his full shares (50 + 300), counted once even though a bill was paid in two parts
    async with session_scope() as s:
        total = (await s.execute(select(func.sum(Expense.amount)).where(Expense.user_id == bob.user_id, Expense.voided_at.is_(None)))).scalar_one()
    assert total == 350


async def test_not_yet_on_a_part_payment_puts_only_that_part_back(harness, alice, bob):
    await befriend(alice, bob, "Bob")
    await befriend(bob, alice, "Alice")
    await alice.ok("split_expense", amount=600, description="pizza", with_friends=["Bob"], split="equal")  # Bob owes 300
    await bob.ok("pay_friend", friend="Alice", amount=100)
    await alice.ok("settle_with", person_id=bob.user_id, action="notyet")
    due = (await bob.ok("my_dues"))["data"]["dues"]["owe"][0]
    assert due["total"] == "300.00" and due["claimed"] == "0"
    async with session_scope() as s:
        total = (await s.execute(select(func.coalesce(func.sum(Expense.amount), 0)).where(Expense.user_id == bob.user_id, Expense.voided_at.is_(None)))).scalar_one()
    assert total == 0  # nothing was really paid, so nothing is in his expenses yet


async def test_opposite_debts_left_over_are_netted_when_dues_open(harness, alice, bob):
    await befriend(alice, bob, "Bob")
    await befriend(bob, alice, "Alice")
    await alice.ok("split_expense", amount=100, description="lunch", with_friends=["Bob"], split="equal")  # Bob owes 50
    await bob.ok("pay_friend", friend="Alice")  # Bob says he paid
    await bob.ok("split_expense", amount=100, description="dinner", with_friends=["Alice"], split="equal")  # Alice owes 50 (paid debt is in flight)
    await alice.ok("settle_with", person_id=bob.user_id, action="notyet")  # lunch is unpaid again: 50 each way
    assert "all clear" in (await alice.ok("my_dues"))["text"].lower()
    assert "all clear" in (await bob.ok("my_dues"))["text"].lower()


# ------------------------------------------------------------------ paying from the dues screen
async def test_dues_screen_pays_a_person_in_one_tap(harness, alice, bob):
    await befriend(alice, bob, "Bob")
    for what, amount in (("tea", 100), ("pizza", 600)):
        await alice.ok("split_expense", amount=amount, description=what, with_friends=["Bob"], split="equal")
    await drain(harness)
    dues = (await bob.ok("my_dues"))["data"]["dues"]
    assert len(dues["owe"]) == 1 and dues["owe"][0]["total"] == "350.00" and dues["owe"][0]["claimed"] == "0"
    pid = dues["owe"][0]["person_id"]
    paid = await bob.ok("settle_with", person_id=pid, action="pay")
    assert "₹350" in paid["text"] and await count_expenses(bob.user_id) == 2
    claim = for_person(await drain(harness), alice.telegram_id)
    assert len(claim) == 1 and "₹350" in claim[0]["text"]  # ONE message for both bills
    assert [b["data"].split(":")[0] for b in claim[0]["buttons"][0]] == ["recvfrom", "notyetfrom"]
    assert (await bob.call("settle_with", person_id=pid, action="pay"))["status"] == "rejected"  # nothing left to pay
    mine = (await alice.ok("my_dues"))["data"]["dues"]["owed"][0]
    assert mine["claimed"] == "350.00"
    refuse = await alice.ok("settle_with", person_id=bob.user_id, action="notyet")
    assert "still unpaid" in refuse["text"] and await count_expenses(bob.user_id) == 0
    await bob.ok("settle_with", person_id=pid, action="pay")
    done = await alice.ok("settle_with", person_id=bob.user_id, action="received")
    assert "All settled" in done["text"]
    assert "all clear" in (await bob.ok("my_dues"))["text"].lower()
