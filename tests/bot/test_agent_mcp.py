"""The bot's agent drives the MCP server only through MCP-client HTTP calls: tools, questions to the human, guardrails."""
import asyncio

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import func, select

from app.agent.graph import GROUP_TOOLS, PRIVATE_TOOLS
from app.mcp_client.types import McpError, NoGroup, NotAllowed
from mcp_server.config import settings as server_settings
from mcp_server.db.models import Debt, Expense
from mcp_server.db.session import session_scope
from tests.bot.conftest import tool_call
from tests.harness import new_telegram_id


async def server_side(harness, telegram_id, name="X"):
    """A fresh session for the same Telegram person, to check what the server really holds."""
    return await harness.person(name, telegram_id=telegram_id)


# ------------------------------------------------------------------ the tools the LLM is shown
async def test_every_tool_the_bot_uses_exists_on_the_server(tool_defs):
    names = {t["name"] for t in tool_defs}
    assert set(PRIVATE_TOOLS) <= names and set(GROUP_TOOLS) <= names
    assert not any(n.startswith("platform_") for n in names)  # the LLM never sees platform tools


async def test_tool_schemas_are_flat_and_hide_protocol_arguments(tool_defs):
    for t in tool_defs:
        blob = str(t["parameters"])
        assert "$ref" not in blob and "anyOf" not in blob and "null" not in blob, t["name"]
        assert not ({"answers", "idempotency_key", "member_count"} & set(t["parameters"]["properties"])), t["name"]
        assert t["description"] and "Protocol arguments" not in t["description"]
    split = next(t for t in tool_defs if t["name"] == "split_expense")
    assert split["parameters"]["required"] == ["amount", "description", "with_friends"]
    assert split["parameters"]["properties"]["exact_amounts"]["items"]["properties"]["amount"]["type"] == "number"


# ------------------------------------------------------------------ a normal request
async def test_expense_through_the_agent(harness, make_runner, ident):
    me = ident()
    runner = make_runner(tool_call("add_expenses", {"items": [{"amount": 300, "category": "food", "description": "ice cream"}]}))
    reply = await runner.chat(identity=me, thread_id=f"t{me.telegram_id}", text="spent 300 on ice cream")
    assert "₹300" in reply.text and len(reply.txn_ids) == 1 and not reply.choices
    check = await server_side(harness, me.telegram_id)
    assert "ice cream" in (await check.ok("get_expenses"))["text"]


async def test_two_tools_in_one_message(harness, make_runner, ident):
    from tests.bot.conftest import tool_calls
    me = ident()
    runner = make_runner(tool_calls(
        ("add_expenses", {"items": [{"amount": 300, "category": "food", "description": "ice cream"}]}),
        ("set_budget", {"category": "food", "monthly_limit": 3000}),
    ))
    reply = await runner.chat(identity=me, thread_id=f"t{me.telegram_id}", text="spent 300 on ice cream and set a food limit of 3k")
    assert "₹300" in reply.text and "Budget set" in reply.text


# ------------------------------------------------------------------ human in the loop
async def test_question_pauses_the_graph_and_nothing_is_written(harness, make_runner, ident):
    me, bob = ident("Kiran"), ident("Bob")
    friend = await harness.person("Bob", telegram_id=bob.telegram_id)
    owner = await harness.person("Kiran", telegram_id=me.telegram_id)
    await owner.ok("add_friend", nickname="Bob", telegram_id=bob.telegram_id)
    runner = make_runner(tool_call("split_expense", {"amount": 900, "description": "dinner", "with_friends": ["Bob"]}))

    thread = f"t{me.telegram_id}"
    ask = await runner.chat(identity=me, thread_id=thread, text="dinner 900 split with Bob")
    assert [c["id"] for c in ask.choices] == ["equal", "exact", "cancel"]
    assert "How should I split" in ask.text
    async with session_scope() as s:  # nothing committed while the human is thinking
        assert (await s.execute(select(func.count()).select_from(Expense).where(Expense.user_id == owner.user_id))).scalar_one() == 0
        assert (await s.execute(select(func.count()).select_from(Debt).where(Debt.creditor_id == owner.user_id))).scalar_one() == 0

    done = await runner.chat(identity=me, thread_id=thread, text="equal", from_button=True)
    assert done.split_ids and "Bob owes ₹450" in done.text
    async with session_scope() as s:  # written exactly once, although the tool step ran twice (once per round)
        assert (await s.execute(select(func.count()).select_from(Debt).where(Debt.creditor_id == owner.user_id))).scalar_one() == 1
    assert friend.user_id


async def test_typed_answers_work_like_buttons(harness, make_runner, ident):
    me = ident()
    owner = await harness.person("Kiran", telegram_id=me.telegram_id)
    bob = await harness.person("Bob")
    await owner.ok("add_friend", nickname="Bob", telegram_id=bob.telegram_id)
    runner = make_runner(tool_call("split_expense", {"amount": 100, "description": "tea", "with_friends": ["Bob"]}))
    thread = f"t{me.telegram_id}"
    await runner.chat(identity=me, thread_id=thread, text="tea 100 with Bob")
    done = await runner.chat(identity=me, thread_id=thread, text="split it equally please")
    assert done.split_ids


async def test_cancel_answer_records_nothing(harness, make_runner, ident):
    me = ident()
    owner = await harness.person("Kiran", telegram_id=me.telegram_id)
    bob = await harness.person("Bob")
    await owner.ok("add_friend", nickname="Bob", telegram_id=bob.telegram_id)
    runner = make_runner(tool_call("split_expense", {"amount": 100, "description": "tea", "with_friends": ["Bob"]}))
    thread = f"t{me.telegram_id}"
    await runner.chat(identity=me, thread_id=thread, text="tea 100 with Bob")
    done = await runner.chat(identity=me, thread_id=thread, text="cancel")
    assert "Cancelled" in done.text and not done.split_ids
    assert "owe" not in (await bob.ok("my_dues"))["text"].lower() or "nothing" in (await bob.ok("my_dues"))["text"].lower()


async def test_unrelated_message_drops_the_question_and_is_handled_normally(harness, make_runner, ident):
    me = ident()
    owner = await harness.person("Kiran", telegram_id=me.telegram_id)
    bob = await harness.person("Bob")
    await owner.ok("add_friend", nickname="Bob", telegram_id=bob.telegram_id)
    runner = make_runner(
        tool_call("split_expense", {"amount": 100, "description": "tea", "with_friends": ["Bob"]}),
        tool_call("add_expenses", {"items": [{"amount": 50, "category": "food", "description": "snack"}]}),
    )
    thread = f"t{me.telegram_id}"
    await runner.chat(identity=me, thread_id=thread, text="tea 100 with Bob")
    reply = await runner.chat(identity=me, thread_id=thread, text="spent 50 on a snack")
    assert "₹50" in reply.text and not reply.choices
    assert "snack" in (await owner.ok("get_expenses"))["text"] and "tea" not in (await owner.ok("get_expenses"))["text"]


async def test_two_questions_in_a_row(harness, make_runner, ident):
    me = ident()
    owner = await harness.person("Kiran", telegram_id=me.telegram_id)
    for n in ("P1", "P2", "P3"):
        p = await harness.person(n)
        await owner.ok("add_friend", nickname=n, telegram_id=p.telegram_id)
    runner = make_runner(tool_call("split_expense", {"amount": 4000, "description": "lunch", "with_friends": ["P1", "P2", "P3"]}))
    thread = f"t{me.telegram_id}"
    q1 = await runner.chat(identity=me, thread_id=thread, text="lunch 4000 with P1 P2 P3")
    assert "one-off split" in q1.text.lower()
    q2 = await runner.chat(identity=me, thread_id=thread, text="one_off", from_button=True)
    assert "How should I split" in q2.text
    done = await runner.chat(identity=me, thread_id=thread, text="equal", from_button=True)
    assert "1,000" in done.text and done.split_ids


# ------------------------------------------------------------------ guardrails
async def test_free_text_from_the_model_is_replaced_by_the_servers_refusal(make_runner, ident):
    me = ident()
    reply = await make_runner(AIMessage("The capital of France is Paris.")).chat(identity=me, thread_id=f"t{me.telegram_id}", text="capital of france?")
    assert "outside my job" in reply.text and "Paris" not in reply.text


async def test_greeting_and_clarify_come_from_the_server(make_runner, ident):
    me = ident()
    runner = make_runner(
        tool_call("respond_to_user", {"kind": "greeting"}),
        tool_call("respond_to_user", {"kind": "clarify", "message": "How much did you spend?"}),
    )
    assert "Hi!" in (await runner.chat(identity=me, thread_id=f"t{me.telegram_id}", text="hi")).text
    assert (await runner.chat(identity=me, thread_id=f"t{me.telegram_id}", text="lunch")).text == "❓ How much did you spend?"


async def test_a_tool_outside_the_chat_type_is_refused_and_the_model_retries(make_runner, ident):
    group = ident("Ana", group=-4_000_000 - new_telegram_id() % 10**6, title="Trip")
    runner = make_runner(
        tool_call("add_expenses", {"items": [{"amount": 5, "category": "food", "description": "x"}]}),  # personal money in a group
        tool_call("respond_to_user", {"kind": "clarify", "message": "Say it as a group expense"}),
    )
    reply = await runner.chat(identity=group, thread_id=f"g{group.telegram_id}", text="spent 5")
    assert reply.text == "❓ Say it as a group expense"


async def test_group_chat_only_sees_group_tools(make_runner, ident, tool_defs):
    from tests.bot.conftest import ScriptedLLM
    from app.agent.graph import build_graph
    from langgraph.checkpoint.memory import MemorySaver
    llm = ScriptedLLM(script=[], seen_tools=[])
    build_graph(llm, None, tool_defs, MemorySaver())
    private, group = llm.seen_tools
    assert "add_expenses" in private and "add_expenses" not in group
    assert "settle_group" in group and "settle_group" not in private


# ------------------------------------------------------------------ group chats through the agent
async def test_group_expense_and_settle_through_the_agent(harness, make_runner, ident):
    chat = -5_000_000 - new_telegram_id() % 10**6
    ana = ident("Ana", group=chat, title="Hampi")
    ben = ident("Ben", group=chat)
    ghost = new_telegram_id()
    await harness.person("Ana", telegram_id=ana.telegram_id, started=True)  # Ana pressed Start before; Ghost never did
    await harness.person("Ana", telegram_id=ana.telegram_id, group_chat_id=chat, group_title="Hampi")  # the chat exists once she speaks in it
    await harness.platform("platform_group_event", event="members_joined", chat_id=chat, members=[{"telegram_id": ghost, "name": "Ghost"}])
    runner = make_runner(
        tool_call("add_group_expense", {"amount": 900, "description": "stay"}),
        tool_call("settle_group", {}),
    )
    thread = f"g{chat}-{ana.telegram_id}"
    ask = await runner.chat(identity=ana, thread_id=thread, text="I paid 900 for stay", member_count=4)
    assert [c["id"] for c in ask.choices][:3] == ["equal", "always_equal", "exact"]
    done = await runner.chat(identity=ana, thread_id=thread, text="always_equal", from_button=True, member_count=4)
    assert "paid ₹900" in done.text and done.split_ids
    confirm = await runner.chat(identity=ana, thread_id=thread + "s", text="settle up", member_count=4)
    assert [c["id"] for c in confirm.choices] == ["yes", "cancel"] and "I only know" in confirm.text  # Telegram says 3 people + bot
    settled = await runner.chat(identity=ana, thread_id=thread + "s", text="yes", from_button=True, member_count=4)
    assert "Hampi settled" in settled.text
    assert [u["telegram_id"] for u in settled.start_users] == [ghost] and settled.group_id


# ------------------------------------------------------------------ the MCP client itself
async def test_expired_or_revoked_session_is_renewed_transparently(harness, gateway, ident):
    me = ident()
    first = await gateway.call(me, "list_friends")
    assert first.ok
    token = await gateway.session_token(me)
    await harness.http.post("/auth/revoke", json={"token": token}, headers={"Authorization": f"Bearer {harness.service}"})
    again = await gateway.call(me, "list_friends")  # the old token is dead: the client gets a new one and retries once
    assert again.ok and await gateway.session_token(me) != token


async def test_wrong_client_secret_is_an_error(harness):
    from app.mcp_client.gateway import McpGateway
    bad = McpGateway("http://testserver/mcp", "http://testserver", "telegram-bot", "wrong", asgi_app=harness.app)
    with pytest.raises(McpError):
        await bad.call(Identity_(1), "list_friends")
    await bad.close()


def Identity_(tg):
    from app.mcp_client.types import Identity
    return Identity(telegram_id=tg, started=True)


async def test_refused_people_raise_specific_errors(harness, gateway, ident, monkeypatch):
    owner = new_telegram_id()
    monkeypatch.setattr(server_settings, "allowed_telegram_ids", str(owner))
    with pytest.raises(NotAllowed):
        await gateway.session_token(ident("Stranger"))
    with pytest.raises(NoGroup):
        await gateway.session_token(ident("Stranger", group=-6_000_000 - new_telegram_id() % 10**6, title="x"))


async def test_concurrent_calls_never_mix_identities(harness, gateway, ident):
    people = []
    for i in range(6):
        p = ident(f"Person{i}")
        people.append(p)
        h = await harness.person(f"Person{i}", telegram_id=p.telegram_id)
        await h.ok("add_expenses", items=[{"amount": 100 + i, "category": "food", "description": f"unique-{i}"}])

    async def look(i):
        res = await gateway.call(people[i], "get_expenses")
        return i, res.text

    results = await asyncio.gather(*(look(i % 6) for i in range(36)))
    for i, text in results:
        assert f"unique-{i}" in text and all(f"unique-{j}" not in text for j in range(6) if j != i)


async def test_unreachable_server_is_a_clean_error():
    from app.mcp_client.gateway import McpGateway
    gw = McpGateway("http://127.0.0.1:9/mcp", "http://127.0.0.1:9", "telegram-bot", "x", timeout=2)
    with pytest.raises(McpError):
        await gw.call(Identity_(5), "list_friends")
    await gw.close()
