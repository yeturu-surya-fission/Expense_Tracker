"""
The whole reimbursement journey with BOTH services running (no sockets): the employee's tools call the Finance Agent over real
A2A (agent card, signed tokens, tasks), the manager decides through the manager service, and the decision comes back as a
signed webhook that becomes a message for the employee.
"""
import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from finance_agent.a2a import notify
from finance_agent.a2a.auth import TokenVerifier
from finance_agent.db.models import Notification
from finance_agent.db.session import session_scope
from finance_agent.main import create_app
from finance_agent.manager.service import ManagerService
from mcp_server.auth.keys import get_keys
from mcp_server.reimbursement import a2a_client
from mcp_server.reimbursement.webhook import sign
from tests.finance.conftest import RULES


@pytest.fixture
async def world(harness, llm):
    """Tracker (the harness) + Finance Agent, wired to each other in-process, with a manager and a policy."""
    async def keys():
        return get_keys().jwks()

    app = create_app(llm=llm, verifier=TokenVerifier(keys))
    await app.state.task_store.initialize()
    a2a_client._transport = httpx.ASGITransport(app=app)
    svc = ManagerService(llm)
    mgr = 8_800_000_000 + uuid.uuid4().int % 1_000_000
    await svc.start(mgr)
    created = await svc.text(mgr, "Acme Pvt Ltd")
    code = "FIN-" + created.text.split("FIN-")[1].split()[0].rstrip(".:,")
    llm.say({"rules": RULES})
    await svc.text(mgr, "policy text")
    await svc.button(mgr, "fp:save")

    class World:
        pass

    w = World()
    w.app, w.llm, w.svc, w.mgr, w.code, w.harness = app, llm, svc, mgr, code, harness
    yield w
    a2a_client._transport = None


async def deliver_decisions(w) -> int:
    """What the Finance Agent's background loop does: post pending webhooks to the tracker."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.harness.app)) as c:
        return await notify.deliver_once(c)


async def inbox(w, person) -> list[str]:
    res = await w.harness.platform("platform_outbox_claim", limit=50)
    mine = [m for m in res["data"]["messages"] if m["telegram_id"] == person.telegram_id]
    for m in res["data"]["messages"]:
        await w.harness.platform("platform_outbox_ack", outbox_id=m["id"], message_id=1)
    return [m["text"] for m in mine]


async def manager_cards(w) -> list[Notification]:
    async with session_scope() as s:
        return list((await s.execute(select(Notification).where(Notification.telegram_id == w.mgr).order_by(Notification.id))).scalars().all())


async def test_the_whole_journey(world):
    w, llm = world, world.llm
    kiran = await w.harness.person("Kiran Rao")

    # joins with the manager's code; a wrong code is refused
    assert "couldn't" in (await kiran.call("join_company", code="FIN-WRONG"))["text"].lower() or "don't know" in (await kiran.call("join_company", code="FIN-WRONG"))["text"]
    joined = await kiran.ok("join_company", code=w.code.lower())
    assert "Acme" in joined["text"] and joined["data"]["joined"]

    # work expenses are separate from personal spending
    await kiran.ok("add_expenses", items=[{"amount": 200, "category": "food", "description": "lunch"}])
    work = await kiran.ok("add_work_expense", items=[
        {"amount": 450, "category": "cab", "description": "cab to client office"},
        {"amount": 15000, "category": "hotel", "description": "Pune hotel"},
    ])
    cab_id, hotel_id = work["data"]["work_expense_ids"]
    summary = await kiran.ok("ui_view", view="summary", arg="this_month")
    assert "200" in summary["text"] and "15,450" not in summary["text"] and "15000" not in summary["text"]  # only the lunch is personal spending

    # asking the policy: a follow-up question, answered, continues the SAME conversation
    llm.say({"kind": "item", "rule_id": "hotel", "item": {"description": "room", "amount": 15000, "nights": None}})
    first = await kiran.ok("ask_policy", question="will I get a refund on this room, 15000 total?")
    assert first["text"] == "How many nights?" and first["data"]["waiting_for_answer"]
    llm.say({"kind": "item", "rule_id": "hotel", "item": {"description": "room", "amount": 15000, "nights": 2}})
    second = await kiran.ok("ask_policy", question="2 nights")
    assert "₹12,000 of ₹15,000" in second["text"] and not second["data"]["waiting_for_answer"]
    assert "Employee: will I get a refund" in llm.calls[-1][1] and "Employee: 2 nights" in llm.calls[-1][1]  # the finance agent remembered the first question

    # the policy doesn't cover it: offer, then ask the manager
    llm.say({"kind": "item", "rule_id": None, "item": {"description": "spa"}})
    spa = await kiran.ok("ask_policy", question="can I claim a spa day?")
    assert spa["data"]["can_escalate"] and "ask your manager" in spa["text"]
    asked = await kiran.ok("ask_manager", question="can I claim a spa day?")
    assert "sent your question" in asked["text"]
    assert any("Kiran Rao asks" in c.text for c in await manager_cards(w))

    # sending the claim needs a confirmation, then the finance agent asks for what is missing
    pending = await kiran.call("submit_claim", title="Pune trip")
    assert pending["status"] == "input_required" and "₹15,450" in pending["text"]
    sent = await kiran.ok("submit_claim", title="Pune trip", answers={"confirm": "yes"})
    claim_id = sent["data"]["claim_id"]
    assert sent["data"]["needs_info"] and "Pune hotel: How many nights?" in sent["text"]
    assert (await kiran.ok("list_work_expenses"))["data"]["waiting"] == 0  # they're in the claim now

    done = await kiran.ok("claim_answer", claim_id=claim_id, details=[{"item_id": hotel_id, "nights": 2, "has_receipt": True}])
    assert done["data"]["status"] == "awaiting_manager" and "₹12,450" in done["text"]

    # the manager sees one card with the policy's recommendation and decides
    card = [c for c in await manager_cards(w) if "Claim #" in c.text][-1]
    assert "Kiran Rao" in card.text and "Pune hotel: ₹15,000 → ₹12,000" in card.text
    remote_id = int(card.text.split("Claim #")[1].split()[0])
    approved = await w.svc.button(w.mgr, f"fa:approve:{remote_id}")
    assert "approved for ₹12,450" in approved.text

    # the decision reaches the employee as a signed webhook
    assert await deliver_decisions(w) >= 1
    messages = await inbox(w, kiran)
    assert any("approved: ₹12,450" in m and "₹3,000 of the ₹15,450" in m for m in messages)
    assert "approved" in (await kiran.ok("my_claims"))["text"] and "₹12,450" in (await kiran.ok("my_claims", claim_id=claim_id))["text"]

    # paying it is told too
    await w.svc.button(w.mgr, f"fa:paid:{remote_id}")
    await deliver_decisions(w)
    assert any("has been paid" in m for m in await inbox(w, kiran))


async def test_the_manager_can_ask_a_question_and_the_employee_answers_from_chat(world):
    w, kiran = world, await world.harness.person("Asha")
    await kiran.ok("join_company", code=w.code)
    exp = await kiran.ok("add_work_expense", items=[{"amount": 700, "category": "cab", "description": "cab"}])
    await kiran.ok("submit_claim", title="Offsite", answers={"confirm": "yes"})
    card = [c for c in await manager_cards(w) if "Asha" in c.text and "Claim #" in c.text][-1]
    remote_id = int(card.text.split("Claim #")[1].split()[0])

    await w.svc.button(w.mgr, f"fa:ask:{remote_id}")
    await w.svc.text(w.mgr, "Which client was this for?")
    await deliver_decisions(w)
    assert any("Which client was this for?" in m for m in await inbox(w, kiran))
    claim_no = (await kiran.ok("my_claims"))["text"].split("#")[1].split()[0]
    assert "your manager asked you something" in (await kiran.ok("my_claims"))["text"]

    await kiran.ok("claim_answer", claim_id=int(claim_no), text="Globex workshop")
    assert any("Globex workshop" in c.text and "Asha replied" in c.text for c in await manager_cards(w))
    assert exp["status"] == "ok"


async def test_a_manager_answer_to_a_policy_question_arrives_as_a_message(world):
    w, kiran = world, await world.harness.person("Ravi")
    await kiran.ok("join_company", code=w.code)
    await kiran.ok("ask_manager", question="Can I claim parking?")
    cards = [c for c in await manager_cards(w) if "Ravi asks" in c.text]
    qid = cards[-1].buttons[0][0]["data"].split(":")[2]
    await w.svc.button(w.mgr, f"fq:answer:{qid}")
    await w.svc.text(w.mgr, "Yes, up to 300 a day")
    await deliver_decisions(w)
    assert any("Can I claim parking?" in m and "up to 300 a day" in m for m in await inbox(w, kiran))


async def test_things_that_must_be_refused(world):
    w, llm = world, world.llm
    kiran = await w.harness.person("Nobody")
    # not in a company yet
    for tool, args in (("ask_policy", {"question": "x"}), ("submit_claim", {}), ("ask_manager", {"question": "x"})):
        r = await kiran.call(tool, **args)
        assert r["status"] == "rejected" and "company" in r["text"]
    # nothing to claim
    await kiran.ok("join_company", code=w.code)
    assert "no work expenses" in (await kiran.call("submit_claim"))["text"].lower()
    # another company's claim can't be reached with a guessed number
    assert (await kiran.call("claim_answer", claim_id=999999, text="hi"))["status"] == "rejected"
    # a work expense in a claim can't be deleted
    exp = await kiran.ok("add_work_expense", items=[{"amount": 100, "category": "cab", "description": "cab"}])
    wid = exp["data"]["work_expense_ids"][0]
    await kiran.ok("submit_claim", answers={"confirm": "yes"})
    assert "already in a claim" in (await kiran.call("remove_work_expense", expense_id=wid))["text"]


async def test_the_finance_agent_being_down_loses_nothing(world):
    w, kiran = world, await world.harness.person("Meera")
    await kiran.ok("join_company", code=w.code)
    await kiran.ok("add_work_expense", items=[{"amount": 300, "category": "cab", "description": "cab"}])

    class Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("finance is down")

    a2a_client._transport = Down()
    r = await kiran.call("submit_claim", answers={"confirm": "yes"})
    assert r["status"] == "rejected" and "isn't reachable" in r["text"]
    assert (await kiran.ok("list_work_expenses"))["data"]["waiting"] == 1  # still waiting to be claimed, ready to try again

    a2a_client._transport = httpx.ASGITransport(app=w.app)
    assert (await kiran.ok("submit_claim", answers={"confirm": "yes"}))["data"]["claim_id"]


async def test_the_webhook_only_trusts_the_finance_agent(world):
    w = world
    http = w.harness.http
    body = json.dumps({"event": "manager_answer", "employee_ref": "1", "text": "hi"}).encode()
    assert (await http.post("/a2a/webhook", content=body)).status_code == 401
    assert (await http.post("/a2a/webhook", content=body, headers={"X-Signature": "sha256=" + "0" * 64})).status_code == 401
    kiran = await w.harness.person("Hook")
    ok = json.dumps({"event": "manager_answer", "employee_ref": str(kiran.user_id), "text": "hello"}).encode()
    assert (await http.post("/a2a/webhook", content=ok, headers={"X-Signature": sign(ok)})).status_code == 200
    assert "hello" in await inbox(w, kiran)
    bad = json.dumps({"event": "nonsense", "employee_ref": str(kiran.user_id), "text": "x"}).encode()
    assert (await http.post("/a2a/webhook", content=bad, headers={"X-Signature": sign(bad)})).status_code == 400
    ghost = json.dumps({"event": "manager_answer", "employee_ref": "999999999", "text": "x"}).encode()
    assert (await http.post("/a2a/webhook", content=ghost, headers={"X-Signature": sign(ghost)})).status_code == 404


async def test_a_repeated_decision_is_told_only_once(world):
    w, kiran = world, await world.harness.person("Dup")
    await kiran.ok("join_company", code=w.code)
    await kiran.ok("add_work_expense", items=[{"amount": 500, "category": "cab", "description": "cab"}])
    await kiran.ok("submit_claim", answers={"confirm": "yes"})
    card = [c for c in await manager_cards(w) if "Dup" in c.text and "Claim #" in c.text][-1]
    remote_id = int(card.text.split("Claim #")[1].split()[0])
    await w.svc.button(w.mgr, f"fa:approve:{remote_id}")
    payload = {"event": "claim_update", "employee_ref": str(kiran.user_id), "claim_id": remote_id, "status": "approved", "approved_amount": "500.00", "text": "approved!"}
    body = json.dumps(payload).encode()
    await deliver_decisions(w)
    await inbox(w, kiran)
    again = await w.harness.http.post("/a2a/webhook", content=body, headers={"X-Signature": sign(body)})
    assert again.json().get("duplicate") and await inbox(w, kiran) == []


async def test_the_agent_card_and_the_door_are_public_and_locked_respectively(world):
    w = world
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://finance.test") as c:
        card = (await c.get("/.well-known/agent-card.json")).json()
        assert card["name"] == "Finance Agent" and {s["id"] for s in card["skills"]} >= {"ask_policy", "submit_claim"}
        assert (await c.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {}})).status_code == 401
        assert (await c.post("/a2a", json={}, headers={"Authorization": "Bearer not-a-token"})).status_code == 401
