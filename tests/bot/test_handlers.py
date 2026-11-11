"""The Telegram handlers end to end: fake Telegram on one side, the real MCP server (in-process) on the other."""
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agent.graph import build_graph
from app.agent.runner import AgentRunner
from app.bot.delivery import OutboxDeliverer
from app.bot.handlers import BotHandlers
from app.config import settings as bot_settings
from mcp_server.config import settings as server_settings
from tests.bot.conftest import ScriptedLLM, tool_call
from tests.bot.fakes import (FakeBot, FakeMessage, FakeQuery, buttons, context, group_chat, private_chat, tg_user, update)
from tests.harness import new_telegram_id


@pytest.fixture(autouse=True)
def bot_username(monkeypatch):
    monkeypatch.setattr(bot_settings, "bot_username", "expense_test_bot")


@pytest.fixture
def make_handlers(gateway, tool_defs):
    def _make(*script) -> BotHandlers:
        runner = AgentRunner(build_graph(ScriptedLLM(script=list(script)), gateway, tool_defs, MemorySaver()))
        return BotHandlers(runner, ScriptedLLM(), gateway)
    return _make


async def deliver_all(gateway, bot) -> int:
    n, total = 1, 0
    while n:
        n = await OutboxDeliverer(bot, gateway).run_once()
        total += n
    return total


def private_msg(tid, text, name="Kiran"):
    user = tg_user(tid, name)
    return user, FakeMessage(text, private_chat(tid), user)


def tap(handlers, bot, user, message, data):
    q = FakeQuery(data, message, user)
    return q, handlers.on_callback(update(user, message, q), context(bot))


# ------------------------------------------------------------------ private chat
async def test_text_expense_and_undo_button(make_handlers):
    h, bot, tid = make_handlers(tool_call("add_expenses", {"items": [{"amount": 300, "category": "food", "description": "ice cream"}]})), FakeBot(), new_telegram_id()
    user, msg = private_msg(tid, "spent 300 on ice cream")
    await h.on_text(update(user, msg), context(bot))
    reply = msg.sent[-1]
    assert "₹300" in reply["text"]
    undo = [b for b in buttons(reply["markup"]) if b[0].endswith("Undo")][0]
    assert undo[1].startswith("undo:")
    bot_msg = FakeMessage(reply["text"], msg.chat, None)
    q, coro = tap(h, bot, user, bot_msg, undo[1])
    await coro
    assert "Undone" in q.edited[-1]["text"]
    q2, coro2 = tap(h, bot, user, bot_msg, undo[1])  # tapping it again
    await coro2
    assert any("already undone" in s["text"] for s in bot_msg.sent)


async def test_start_shows_help_and_menu(make_handlers):
    h, bot = make_handlers(), FakeBot()
    user, msg = private_msg(new_telegram_id(), "/start")
    await h.start(update(user, msg), context(bot))
    assert "expense tracker" in msg.sent[-1]["text"].lower() and msg.sent[-1]["markup"] is not None


async def test_strangers_are_turned_away_by_the_servers_allow_list(make_handlers, monkeypatch):
    owner = new_telegram_id()
    monkeypatch.setattr(server_settings, "allowed_telegram_ids", str(owner))
    h, bot = make_handlers(), FakeBot()
    user, msg = private_msg(new_telegram_id(), "spent 5 on tea", "Stranger")
    await h.on_text(update(user, msg), context(bot))
    assert "private bot" in msg.sent[-1]["text"]


async def test_invite_link_flow(make_handlers, harness, gateway):
    h, bot = make_handlers(), FakeBot()
    inviter_user, inv_msg = private_msg(new_telegram_id(), "/start", "Inviter")
    await h.start(update(inviter_user, inv_msg), context(bot))  # the inviter has used the bot
    link = (await h.call(h.private(inviter_user), "ui_view", view="invite")).text
    assert f"inv_{inviter_user.id}" in link
    new_user, msg = private_msg(new_telegram_id(), "/start", "Newcomer")
    await h.start(update(new_user, msg), context(bot, [f"inv_{inviter_user.id}"]))
    assert any("friends with Inviter" in s["text"] for s in msg.sent)
    await deliver_all(gateway, bot)
    assert any(m["chat_id"] == inviter_user.id and "joined your friends" in m["text"] for m in bot.sent)
    bad_user, bad = private_msg(new_telegram_id(), "/start")
    await h.start(update(bad_user, bad), context(bot, ["inv_notanumber"]))
    assert "isn't valid" in bad.sent[-1]["text"]


async def test_menu_commands_and_month_navigation(make_handlers):
    h, bot = make_handlers(tool_call("add_expenses", {"items": [{"amount": 120, "category": "food", "description": "lunch"}]})), FakeBot()
    user, msg = private_msg(new_telegram_id(), "lunch 120")
    await h.on_text(update(user, msg), context(bot))
    for text in ("/summary", "/recent", "/budgets", "/friends", "/dues", "/daily", "/trend", "/invite", "/undo"):
        _, m = private_msg(user.id, text)
        m.from_user = user
        await h.on_command(update(user, m), context(bot))
        assert m.sent and m.sent[-1]["text"], text
    _, summary = private_msg(user.id, "/summary")
    await h.on_command(update(user, summary), context(bot))
    nav = [b for b in buttons(summary.sent[-1]["markup"]) if b[1] and b[1].startswith("sum:")]
    assert nav
    shown = FakeMessage("x", summary.chat)
    q, coro = tap(h, bot, user, shown, "sum:last_month")
    await coro
    assert "Summary" in q.edited[-1]["text"]


async def test_split_and_payment_lifecycle_through_telegram(make_handlers, gateway):
    """Kiran splits a bill; Surya gets his bill in his chat, taps I paid; Kiran confirms - all via the bot, MCP and the outbox."""
    bot = FakeBot()
    surya_tid, kiran_tid = new_telegram_id(), new_telegram_id()
    h = make_handlers(tool_call("split_expense", {"amount": 900, "description": "dinner", "with_friends": ["Surya"]}))
    surya = tg_user(surya_tid, "Surya")
    await h.start(update(surya, FakeMessage("/start", private_chat(surya_tid), surya)), context(bot))  # Surya pressed Start
    kiran, _ = private_msg(kiran_tid, "")
    await h.call(h.private(kiran), "add_friend", nickname="Surya", telegram_id=surya_tid)

    msg = FakeMessage("dinner 900 split with Surya", private_chat(kiran_tid), kiran)
    await h.on_text(update(kiran, msg), context(bot))
    ask = msg.sent[-1]
    assert "How should I split" in ask["text"]
    equal = [b for b in buttons(ask["markup"]) if b[0] == "Equally"][0]
    q, coro = tap(h, bot, kiran, FakeMessage(ask["text"], msg.chat), equal[1])
    await coro
    assert q.markup_cleared  # one-shot buttons

    assert await deliver_all(gateway, bot) >= 1
    bill = [m for m in bot.sent if m["chat_id"] == surya_tid and "Your share: ₹450" in m["text"]][0]
    paid_btn = buttons(bill["markup"])[0]
    assert paid_btn[0] == "✅ I paid"

    bill_msg = FakeMessage(bill["text"], private_chat(surya_tid))
    bill_msg.chat.send_message = lambda text, **kw: _record(bill_msg, text)
    q, coro = tap(h, bot, surya, bill_msg, paid_btn[1])
    await coro
    assert bill_msg.deleted and any("you paid Kiran ₹450" in t for t in bill_msg.chat_sent)

    await deliver_all(gateway, bot)
    claim = [m for m in bot.sent if m["chat_id"] == kiran_tid and "says they paid you" in m["text"]][0]
    got = [b for b in buttons(claim["markup"]) if b[0] == "✅ Received"][0]
    q, coro = tap(h, bot, kiran, FakeMessage(claim["text"], msg.chat), got[1])
    await coro
    assert "All settled" in q.edited[-1]["text"]
    await deliver_all(gateway, bot)
    assert any(m["chat_id"] == surya_tid and "confirmed your payment" in m["text"] for m in bot.sent)


async def _record(msg, text):
    msg.__dict__.setdefault("chat_sent", []).append(text)


# ------------------------------------------------------------------ group chats
def mention(text, bot_username="expense_test_bot"):
    at = f"@{bot_username}"
    full = f"{at} {text}"
    return full, [SimpleNamespace(type="mention", offset=0, length=len(at))]


async def test_group_setup_when_the_bot_is_added(make_handlers, gateway):
    h, chat = make_handlers(), group_chat(-7_000_000 - new_telegram_id() % 10**6, "Goa Trip")
    adder, pal = tg_user(new_telegram_id(), "Adder"), new_telegram_id()
    await h.call(h.private(adder), "add_friend", nickname="Pal", telegram_id=pal)
    bot = FakeBot(chat_members={(chat.id, pal): "member"})
    change = SimpleNamespace(chat=chat, from_user=adder, new_chat_member=SimpleNamespace(status="member"), old_chat_member=SimpleNamespace(status="left"))
    await h.on_bot_membership(SimpleNamespace(my_chat_member=change), context(bot))
    welcome = bot.sent[-1]
    assert welcome["chat_id"] == chat.id and "Goa Trip" in welcome["text"] and "Adder" in welcome["text"] and f"user {pal}" in welcome["text"]  # the friend's own name is unknown until they speak
    assert buttons(welcome["markup"])[0][2] == "https://t.me/expense_test_bot"
    n = len(bot.sent)
    await h.on_bot_membership(SimpleNamespace(my_chat_member=change), context(bot))  # added again: already set up, stay quiet
    assert len(bot.sent) == n


async def test_bot_added_by_a_stranger_leaves(make_handlers, monkeypatch):
    owner = new_telegram_id()
    monkeypatch.setattr(server_settings, "allowed_telegram_ids", str(owner))
    h, chat, bot = make_handlers(), group_chat(-8_000_000 - new_telegram_id() % 10**6), FakeBot()
    change = SimpleNamespace(chat=chat, from_user=tg_user(new_telegram_id(), "Stranger"),
                             new_chat_member=SimpleNamespace(status="member"), old_chat_member=SimpleNamespace(status="left"))
    await h.on_bot_membership(SimpleNamespace(my_chat_member=change), context(bot))
    assert bot.left == [chat.id] and "only be added to groups by my owner" in bot.sent[-1]["text"]


async def test_group_mention_question_and_settle_with_start_tagging(make_handlers, gateway):
    chat = group_chat(-9_000_000 - new_telegram_id() % 10**6, "Hampi")
    ana_tid, ghost_tid = new_telegram_id(), new_telegram_id()
    ana = tg_user(ana_tid, "Ana")
    bot = FakeBot(member_count=3)
    h = make_handlers(
        tool_call("add_group_expense", {"amount": 600, "description": "stay"}),
        tool_call("settle_group", {}),
    )
    await h.start(update(ana, FakeMessage("/start", private_chat(ana_tid), ana)), context(bot))  # Ana pressed Start
    adder_change = SimpleNamespace(chat=chat, from_user=ana, new_chat_member=SimpleNamespace(status="member"), old_chat_member=SimpleNamespace(status="left"))
    await h.on_bot_membership(SimpleNamespace(my_chat_member=adder_change), context(bot))
    joined = SimpleNamespace(effective_message=FakeMessage("", chat, None, new_chat_members=[tg_user(ghost_tid, "Ghost"), tg_user(5, "SomeBot", is_bot=True)]))
    await h.on_new_members(joined, context(bot))

    text, entities = mention("I paid 600 for stay")
    msg = FakeMessage(text, chat, ana, entities)
    await h.on_group_text(update(ana, msg), context(bot))
    ask = msg.sent[-1]
    assert "How should I split" in ask["text"]
    assert all(b[1].endswith(f":{ana_tid}") for b in buttons(ask["markup"]))  # only the asker may answer
    q_other, coro = tap(h, bot, tg_user(new_telegram_id(), "Intruder"), FakeMessage("q", chat), f"hitl:equal:{ana_tid}")
    await coro
    assert q_other.alerts and "someone else" in q_other.alerts[0]  # blocked before it reaches the server (or: not allowed)
    q, coro = tap(h, bot, ana, FakeMessage(ask["text"], chat), f"hitl:equal:{ana_tid}")
    await coro

    stext, sentities = mention("settle up")
    smsg = FakeMessage(stext, chat, ana, sentities)
    await h.on_group_text(update(ana, smsg), context(bot))
    confirm = smsg.sent[-1]
    assert "Settle up Hampi" in confirm["text"]
    yes = [b for b in buttons(confirm["markup"]) if b[0] == "Yes, settle up"][0]
    q, coro = tap(h, bot, ana, FakeMessage(confirm["text"], chat), yes[1])
    await coro
    posted = q.message.sent
    assert any("Hampi settled" in s["text"] for s in posted)
    tag = [s for s in posted if "press Start" in s["text"] or "Press Start" in s["text"]]
    assert tag and f"tg://user?id={ghost_tid}" in tag[0]["text"]
    assert buttons(tag[0]["markup"])[0][2].startswith("https://t.me/expense_test_bot?start=join_")


async def test_group_commands_and_silence_without_a_mention(make_handlers):
    chat = group_chat(-9_500_000 - new_telegram_id() % 10**6, "Flat")
    ana = tg_user(new_telegram_id(), "Ana")
    h, bot = make_handlers(), FakeBot()
    setup = SimpleNamespace(chat=chat, from_user=ana, new_chat_member=SimpleNamespace(status="member"), old_chat_member=SimpleNamespace(status="left"))
    await h.on_bot_membership(SimpleNamespace(my_chat_member=setup), context(bot))
    chatter = FakeMessage("anyone free for dinner?", chat, ana)
    await h.on_group_text(update(ana, chatter), context(bot))
    assert chatter.sent == []  # ordinary chatter gets no reply and costs no AI call
    cmd = FakeMessage("/group", chat, ana)
    await h.cmd_group(update(ana, cmd), context(bot))
    assert "Flat" in cmd.sent[-1]["text"]
    cmd2 = FakeMessage("/settle", chat, ana)
    await h.cmd_settle(update(ana, cmd2), context(bot))
    assert "Nothing to settle" in cmd2.sent[-1]["text"]


async def test_unknown_chat_and_stranger_in_group_stay_silent(make_handlers, monkeypatch):
    owner = new_telegram_id()
    monkeypatch.setattr(server_settings, "allowed_telegram_ids", str(owner))
    chat = group_chat(-9_700_000 - new_telegram_id() % 10**6, "Nobody set me up")
    stranger = tg_user(new_telegram_id(), "Stranger")
    h, bot = make_handlers(), FakeBot()
    text, entities = mention("I paid 100 for tea")
    msg = FakeMessage(text, chat, stranger, entities)
    await h.on_group_text(update(stranger, msg), context(bot))
    assert msg.sent == []


# ------------------------------------------------------------------ the messages the server queues
async def test_outbox_delivery_updates_the_server_and_blocked_people_wait(make_handlers, gateway, harness):
    bot = FakeBot()
    h = make_handlers()
    owner = await harness.person("Owner")
    friend = await harness.person("Friend")
    await owner.ok("add_friend", nickname="Friend", telegram_id=friend.telegram_id)
    await deliver_all(gateway, bot)  # start from an empty queue
    bot.sent.clear()
    await owner.ok("split_expense", amount=200, description="chai", with_friends=["Friend"], split="equal")
    bot.blocked.add(friend.telegram_id)  # they blocked the bot meanwhile
    await deliver_all(gateway, bot)
    assert not [m for m in bot.sent if m["chat_id"] == friend.telegram_id]
    bot.blocked.clear()
    await harness.person("Friend", telegram_id=friend.telegram_id, started=True)  # pressed Start again
    await deliver_all(gateway, bot)
    assert [m for m in bot.sent if m["chat_id"] == friend.telegram_id and "Your share" in m["text"]]


async def test_a_message_that_was_replaced_is_deleted_from_the_chat(make_handlers, gateway, harness):
    bot = FakeBot()
    owner, friend = await harness.person("Owner"), await harness.person("Friend")
    await owner.ok("add_friend", nickname="Friend", telegram_id=friend.telegram_id)
    await deliver_all(gateway, bot)
    r = await owner.ok("split_expense", amount=200, description="chai", with_friends=["Friend"], split="equal")
    await deliver_all(gateway, bot)
    sent_id = [m["message_id"] for m in bot.sent if m["chat_id"] == friend.telegram_id and "chai" in m["text"]][-1]
    await owner.ok("cancel_split", split_id=r["data"]["split_ids"][0])
    await deliver_all(gateway, bot)
    assert (friend.telegram_id, sent_id) in bot.deleted


async def test_dues_screen_has_pay_buttons_and_updates_in_place(make_handlers, gateway):
    bot = FakeBot()
    h = make_handlers()
    kiran, surya = tg_user(new_telegram_id(), "Kiran"), tg_user(new_telegram_id(), "Surya")
    for u in (kiran, surya):
        await h.start(update(u, FakeMessage("/start", private_chat(u.id), u)), context(bot))
    await h.call(h.private(kiran), "add_friend", nickname="Surya", telegram_id=surya.id)
    for what, amount in (("tea", 100), ("pizza", 600)):
        await h.call(h.private(kiran), "split_expense", amount=amount, description=what, with_friends=["Surya"], split="equal")

    msg = FakeMessage("/dues", private_chat(surya.id), surya)
    await h.on_command(update(surya, msg), context(bot))
    shown = msg.sent[-1]
    assert shown["text"].count("Kiran") == 1 and "₹350" in shown["text"] and "tea" not in shown["text"]
    pay = [b for b in buttons(shown["markup"]) if b[0].startswith("✅ I paid Kiran")][0]
    assert pay[0] == "✅ I paid Kiran ₹350" and pay[1].startswith("payto:")

    q, coro = tap(h, bot, surya, FakeMessage(shown["text"], msg.chat), pay[1])
    await coro
    assert "you paid Kiran ₹350" in q.edited[-1]["text"] and "waiting for them to confirm" in q.edited[-1]["text"]
    assert not [b for b in buttons(q.edited[-1]["markup"]) if "I paid" in b[0]]  # nothing left to pay

    await deliver_all(gateway, bot)
    claim = [m for m in bot.sent if m["chat_id"] == kiran.id and "says they paid you" in m["text"]][0]
    got = [b for b in buttons(claim["markup"]) if b[0] == "✅ Received"][0]
    q, coro = tap(h, bot, kiran, FakeMessage(claim["text"], private_chat(kiran.id)), got[1])
    await coro
    assert "All settled" in q.edited[-1]["text"]
