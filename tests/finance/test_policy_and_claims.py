"""Policy reading, policy questions, claims and the manager's side: everything except the A2A wiring (see test_a2a.py)."""
import itertools
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from finance_agent.claims import service as claims
from finance_agent.db.models import Claim, Notification, WebhookEvent
from finance_agent.db.session import session_scope
from finance_agent.llm import LLMError
from finance_agent.manager.service import ManagerService, parse_amount
from finance_agent.policy import parser, qa, store
from finance_agent.policy.models import Rule
from tests.finance.conftest import RULES

_ids = itertools.count(9_100_000_000)
RULES_ = [Rule.model_validate(r) for r in RULES]


async def company_with_policy(rules=RULES):
    async with session_scope() as s:
        company = await store.create_company(s, "Acme", next(_ids))
        await store.save(s, company.id, [Rule.model_validate(r) for r in rules], "test")
        emp, _ = await claims.join(s, company, f"u{next(_ids)}", "Kiran Rao")
        return company, emp


async def outgoing(kind):
    async with session_scope() as s:
        if kind == "manager":
            return [n for n in (await s.execute(select(Notification).order_by(Notification.id))).scalars().all()]
        return [e.payload for e in (await s.execute(select(WebhookEvent).order_by(WebhookEvent.id))).scalars().all()]


# ------------------------------------------------------------------ reading a policy
async def test_a_pasted_policy_becomes_validated_rules_with_unique_ids(llm):
    llm.say({"rules": [
        {"id": "Hotel Stays!", "name": "Hotels", "cap_amount": "6000", "cap_unit": "per_night", "keywords": ["Room", " stay "]},
        {"id": "hotel_stays", "name": "Hostels", "percent": 80},
    ]})
    rules = await parser.parse_policy(llm, "Hotels up to 6000 a night. Hostels 80%.")
    assert [r.id for r in rules] == ["hotel_stays", "hotel_stays_2"]
    assert rules[0].cap_amount == D(6000) and rules[0].keywords == ["room", "stay"]


@pytest.mark.parametrize("answer", [{"rules": []}, {"nothing": 1}, {"rules": [{"id": "x", "name": "X", "percent": 900}]}, LLMError("down")])
async def test_an_unreadable_policy_gives_a_plain_message(llm, answer):
    llm.say(answer)
    with pytest.raises(parser.PolicyParseError):
        await parser.parse_policy(llm, "blah")


async def test_an_edit_shows_exactly_what_changed(llm):
    edited = [dict(r) for r in RULES]
    edited[0] = {**edited[0], "cap_amount": 8000}
    edited.append({"id": "parking", "name": "Parking", "cap_amount": 300, "cap_unit": "per_day"})
    edited = [r for r in edited if r["id"] != "gym"]
    llm.say({"rules": edited})
    new = await parser.edit_policy(llm, RULES_, "raise hotels to 8000, add parking, drop gym")
    diff = parser.diff_rules(RULES_, new)
    assert any("up to ₹6,000 per night" in d and "up to ₹8,000 per night" in d for d in diff)
    assert any(d.startswith("➕ New: Parking") for d in diff)
    assert any(d == "➖ Removed: Gym membership" for d in diff)
    assert len(diff) == 3


# ------------------------------------------------------------------ policy questions
async def test_will_i_get_a_refund_on_this_room(llm):
    llm.say({"kind": "item", "rule_id": "hotel", "item": {"description": "hotel room", "amount": 15000, "nights": 2}})
    r = await qa.answer(llm, RULES_, 3, ["Employee: will I get a refund for a room, 7500 a night for 2 nights?"])
    assert r.status == "answer"
    assert "₹12,000 of ₹15,000" in r.text and len(r.text.splitlines()) <= 2 and "Rule:" not in r.text


async def test_missing_details_are_asked_for_not_guessed(llm):
    llm.say({"kind": "item", "rule_id": "hotel", "item": {"description": "room", "amount": 7500, "nights": None}})
    r = await qa.answer(llm, RULES_, 1, ["Employee: refund on this room?"])
    assert (r.status, r.text) == ("need_info", "How many nights?")


async def test_a_capped_rule_without_an_amount_asks_for_the_amount(llm):
    llm.say({"kind": "item", "rule_id": "meals", "item": {"days": 1}})
    assert (await qa.answer(llm, RULES_, 1, ["Employee: can I claim lunch?"])).text == "How much is it?"


async def test_an_uncapped_rule_is_simply_confirmed(llm):
    llm.say({"kind": "item", "rule_id": "local_travel", "item": {"description": "cab"}})
    r = await qa.answer(llm, RULES_, 1, ["Employee: is a cab covered?"])
    assert r.status == "answer" and "Local travel is covered" in r.text


async def test_something_not_in_the_policy_offers_the_manager(llm):
    llm.say({"kind": "item", "rule_id": None, "item": {"description": "spa"}}, {"kind": "not_covered"}, {"kind": "item", "rule_id": "invented_rule", "item": {}})
    for _ in range(3):
        r = await qa.answer(llm, RULES_, 1, ["Employee: can I claim a spa day?"])
        assert (r.status, r.can_escalate) == ("not_covered", True)


async def test_a_rule_that_says_no_is_an_answer_not_an_escalation(llm):
    llm.say({"kind": "item", "rule_id": "gym", "item": {"description": "gym pass", "amount": 1500}})
    r = await qa.answer(llm, RULES_, 1, ["Employee: gym pass?"])
    assert r.status == "answer" and r.text.startswith("❌") and not r.can_escalate


async def test_a_general_question_is_answered_from_the_rules_and_a_model_outage_is_survivable(llm):
    llm.say({"kind": "general", "answer": "The hotel cap is ₹6,000 a night (Hotel stays)."}, LLMError("down"))
    assert "₹6,000 a night" in (await qa.answer(llm, RULES_, 2, ["Employee: hotel cap?"])).text
    r = await qa.answer(llm, RULES_, 2, ["Employee: hotel cap?"])
    assert r.status == "need_info" and "try again" not in r.text.lower() or "ask again" in r.text.lower()


# ------------------------------------------------------------------ claims
async def test_a_claim_is_checked_asked_about_then_handed_to_the_manager(llm):
    company, emp = await company_with_policy()
    llm.say({"rule_id": None})  # 'spa day' matches no keyword; the model agrees nothing covers it
    items = [
        {"ref": 1, "category": "travel", "description": "cab to client", "amount": "450"},
        {"ref": 2, "category": "hotel", "description": "Pune hotel", "amount": "15000"},
        {"ref": 3, "category": "wellness", "description": "spa day", "amount": "2000"},
    ]
    heard_before = len(await outgoing("manager"))
    async with session_scope() as s:
        claim = await claims.submit(s, llm, company, emp, "Pune trip", items)
        cid = claim.id
        assert claim.status == "needs_info"  # the hotel needs nights and a receipt answer
        assert {(q["key"]) for v in claim.verdicts for q in v["questions"]} == {"nights", "has_receipt"}
    assert len(await outgoing("manager")) == heard_before  # the manager hears nothing until the claim is complete

    async with session_scope() as s:
        claim = await claims.get(s, cid)
        await claims.answer(s, llm, company, emp, claim, {2: {"nights": 2, "has_receipt": "yes"}})
        assert claim.status == "awaiting_manager"
        assert claim.recommended == D("12450.00")  # cab 450 + hotel capped at 2 x 6000; the spa is the manager's call
    card = (await outgoing("manager"))[-1]  # (read after the commit)
    assert "Claim #" in card.text and "Kiran Rao" in card.text and "₹12,450" in card.text and "spa day" in card.text
    assert [b["data"].split(":")[1] for row in card.buttons for b in row] == ["approve", "reject", "amount", "ask"]


async def test_bad_claims_are_refused_in_plain_words(llm):
    company, emp = await company_with_policy()
    async with session_scope() as s:
        for items, why in (([], "at least one item"), ([{"category": "cab", "amount": "-5"}], "more than zero"), ([{"ref": 1, "amount": 1}, {"ref": 1, "amount": 2}], "own reference")):
            with pytest.raises(claims.ClaimError, match=why):
                await claims.submit(s, llm, company, emp, "x", items)
    async with session_scope() as s:
        empty = await store.create_company(s, "No Policy Ltd", next(_ids))
        e2, _ = await claims.join(s, empty, "z1", "Z")
        with pytest.raises(claims.ClaimError, match="hasn't set up"):
            await claims.submit(s, llm, empty, e2, "x", [{"category": "cab", "amount": 5}])


async def test_a_claim_only_ever_belongs_to_its_own_company_and_employee(llm):
    a, emp_a = await company_with_policy()
    b, emp_b = await company_with_policy()
    async with session_scope() as s:
        claim = await claims.submit(s, llm, a, emp_a, "x", [{"category": "cab", "description": "cab", "amount": "100"}])
        cid = claim.id
    async with session_scope() as s:
        with pytest.raises(claims.ClaimError, match="can't find"):
            await claims.get(s, cid, company_id=b.id)  # another company's manager
        with pytest.raises(claims.ClaimError, match="can't find"):
            await claims.get(s, cid, employee_id=emp_b.id)  # another employee


# ------------------------------------------------------------------ the manager
async def test_the_manager_sets_up_the_company_and_policy_then_edits_it(llm):
    svc, mgr = ManagerService(llm), next(_ids)
    assert "company called" in (await svc.start(mgr)).text.lower() or "called" in (await svc.start(mgr)).text
    created = await svc.text(mgr, "Acme Pvt Ltd")
    assert "FIN-" in created.text
    code = "FIN-" + created.text.split("FIN-")[1].split()[0].rstrip(".:,")

    llm.say({"rules": RULES})
    draft = await svc.text(mgr, "Hotels 6000 a night with receipt. Meals 1000 a day. Gym not covered.")
    assert "Hotel stays: up to ₹6,000 per night" in draft.text and [b["data"] for b in draft.buttons[0]] == ["fp:save", "fp:discard"]
    assert (await svc.policy(mgr)).text.startswith("There's no policy yet")  # nothing is live until the manager confirms

    assert "policy v1" in (await svc.button(mgr, "fp:save")).text
    assert "Hotel stays" in (await svc.policy(mgr)).text

    changed = [dict(r) for r in RULES]
    changed[0]["cap_amount"] = 8000
    llm.say({"rules": changed})
    chat = await svc.text(mgr, "ok thanks, what next?")  # a stray message never changes the policy, and the model is not even asked
    assert "nothing was changed" in chat.text and "/editpolicy" in chat.text
    edit = await svc.edit_policy(mgr, "raise the hotel cap to 8000")
    assert "→" in edit.text and "₹8,000" in edit.text
    assert "policy v2" in (await svc.button(mgr, "fp:save")).text

    llm.say({"rules": changed})  # the model changed nothing
    assert "Nothing to change" in (await svc.edit_policy(mgr, "make the hotel cap 8000")).text
    async with session_scope() as s:
        company = await store.company_by_code(s, code)
        assert [v.version for v in await store.history(s, company.id)] == [2, 1]  # old versions are kept


async def test_discarding_a_draft_changes_nothing_and_a_stale_save_is_harmless(llm):
    svc, mgr = ManagerService(llm), next(_ids)
    await svc.start(mgr)
    await svc.text(mgr, "Beta Co")
    llm.say({"rules": RULES})
    await svc.text(mgr, "policy text")
    assert "Discarded" in (await svc.button(mgr, "fp:discard")).text
    assert "nothing to save" in (await svc.button(mgr, "fp:save")).text.lower()


async def test_approve_reject_different_amount_ask_and_paid(llm):
    svc, mgr = ManagerService(llm), next(_ids)
    await svc.start(mgr)
    created = await svc.text(mgr, "Gamma Co")
    code = "FIN-" + created.text.split("FIN-")[1].split()[0].rstrip(".:,")
    llm.say({"rules": RULES})
    await svc.text(mgr, "policy")
    await svc.button(mgr, "fp:save")
    async with session_scope() as s:
        company = await store.company_by_code(s, code)
        emp, _ = await claims.join(s, company, "emp-1", "Kiran Rao")
        ids = []
        for amount in ("1000", "2000", "3000"):
            c = await claims.submit(s, llm, company, emp, f"Trip {amount}", [{"category": "cab", "description": "cab", "amount": amount}])
            ids.append(c.id)
    a, b, c = ids

    approved = await svc.button(mgr, f"fa:approve:{a}")
    assert "approved for ₹1,000" in approved.text
    assert "already approved" in (await svc.button(mgr, f"fa:approve:{a}")).text  # one decision per claim

    assert "Why is claim" in (await svc.button(mgr, f"fa:reject:{b}")).text
    assert "rejected" in (await svc.text(mgr, "Not a business trip")).text

    assert "How much do you approve" in (await svc.button(mgr, f"fa:amount:{c}")).text
    assert "type just the amount" in (await svc.text(mgr, "lots")).text.lower()
    assert "claimed ₹3,000" in (await svc.text(mgr, "9999")).text or "only claimed" in (await svc.text(mgr, "9999")).text
    summary = await svc.text(mgr, "2,500")  # one expense: the summary comes next
    assert "₹2,500" in summary.text and summary.buttons
    assert "approved for ₹2,500" in (await svc.button(mgr, f"fi:{c}:ok")).text
    async with session_scope() as s:
        claim = await claims.get(s, c)
        with pytest.raises(claims.ClaimError, match="between"):
            await claims.approve(s, claim, D(9999))  # more than they claimed: refused (and nothing changes)

    paid = await svc.paid_list(mgr)
    assert "Kiran Rao: ₹1,000" in paid.text and "Kiran Rao: ₹2,500" in paid.text
    assert "marked as paid" in (await svc.button(mgr, f"fa:paid:{a}")).text

    events = [e for e in await outgoing("tracker") if e.get("claim_id") in ids and e["employee_ref"] == "emp-1"]
    by = {(e["claim_id"], e["status"]): e for e in events}
    assert by[(a, "approved")]["approved_amount"] == "1000.00" and "approved" in by[(a, "approved")]["text"]
    assert by[(b, "rejected")]["note"] == "Not a business trip" and "Not a business trip" in by[(b, "rejected")]["text"]
    assert by[(c, "approved")]["approved_amount"] == "2500.00" and "₹2,500 of the ₹3,000" in by[(c, "approved")]["text"] and "cab: ₹2,500 of ₹3,000" in by[(c, "approved")]["text"]
    assert (a, "paid") in by


async def test_the_manager_can_ask_the_employee_and_the_reply_comes_back_as_a_card(llm):
    svc, mgr = ManagerService(llm), next(_ids)
    await svc.start(mgr)
    code = "FIN-" + (await svc.text(mgr, "Delta Co")).text.split("FIN-")[1].split()[0].rstrip(".:,")
    llm.say({"rules": RULES})
    await svc.text(mgr, "policy")
    await svc.button(mgr, "fp:save")
    async with session_scope() as s:
        company = await store.company_by_code(s, code)
        emp, _ = await claims.join(s, company, "emp-2", "Asha Menon")
        cid = (await claims.submit(s, llm, company, emp, "Offsite", [{"category": "cab", "description": "cab", "amount": "700"}])).id

    await svc.button(mgr, f"fa:ask:{cid}")
    assert "sent to the employee" in (await svc.text(mgr, "Which client was this for?")).text.lower()
    asked = [e for e in await outgoing("tracker") if e.get("claim_id") == cid and e["event"] == "claim_question"]
    assert asked and "Which client was this for?" in asked[0]["text"]

    async with session_scope() as s:
        claim = await claims.get(s, cid)
        assert claim.status == "info_requested"
        await claims.employee_reply(s, claim, emp, company, "Globex, the Monday workshop")
        assert claim.status == "awaiting_manager"
    card = (await outgoing("manager"))[-1]
    assert "Asha Menon replied" in card.text and "Globex" in card.text and card.buttons  # the decision buttons are back


async def test_a_policy_question_the_manager_answers_goes_back_and_can_become_a_rule(llm):
    from finance_agent.a2a import skills
    svc, mgr = ManagerService(llm), next(_ids)
    await svc.start(mgr)
    code = "FIN-" + (await svc.text(mgr, "Eps Co")).text.split("FIN-")[1].split()[0].rstrip(".:,")
    llm.say({"rules": RULES})
    await svc.text(mgr, "policy")
    await svc.button(mgr, "fp:save")
    async with session_scope() as s:
        company = await store.company_by_code(s, code)
        await claims.join(s, company, "emp-3", "Ravi")
    res = await skills.run(llm, skills.Caller("emp-3", "Ravi"), {"skill": "ask_manager", "company": code, "question": "Can I claim a spa day?"}, None, "")
    assert res.state == "completed" and res.data["question_id"]
    qid = res.data["question_id"]
    note = (await outgoing("manager"))[-1]
    assert "Ravi asks" in note.text and note.buttons[0][0]["data"] == f"fq:answer:{qid}"

    await svc.button(mgr, f"fq:answer:{qid}")
    done = await svc.text(mgr, "Yes, up to 2000 a month, with approval")
    assert "Add to policy" in done.buttons[0][0]["label"]
    answers = [e for e in await outgoing("tracker") if e.get("question_id") == qid]
    assert answers[0]["event"] == "manager_answer" and "up to 2000" in answers[0]["text"] and answers[0]["employee_ref"] == "emp-3"

    wellness = RULES + [{"id": "spa", "name": "Spa", "keywords": ["spa"], "cap_amount": 2000, "cap_unit": "per_month", "needs_preapproval": True}]
    llm.say({"rules": wellness})
    draft = await svc.button(mgr, f"fq:save:{qid}")
    assert "New: Spa" in draft.text
    assert "Can I claim a spa day?" in llm.calls[-1][1] and "up to 2000 a month" in llm.calls[-1][1]


def test_amounts_the_manager_types():
    assert parse_amount("12000") == D(12000) and parse_amount("₹7,500") == D(7500) and parse_amount("7.5k") == D(7500)
    assert parse_amount("lots") is None and parse_amount("-5") is None


async def test_only_allowed_people_can_become_managers(llm, monkeypatch):
    from finance_agent.config import settings
    monkeypatch.setattr(settings, "allowed_manager_ids", "111,222")
    svc = ManagerService(llm)
    assert "Sorry" in (await svc.start(333)).text and "Sorry" in (await svc.text(333, "My Company")).text
    assert "company called" in (await svc.start(111)).text or "called" in (await svc.start(111)).text
    async with session_scope() as s:
        assert (await s.execute(select(Claim))).scalars().all() is not None


async def test_a_manual_amount_is_decided_expense_by_expense_and_the_employee_sees_which_were_cut(llm):
    svc, mgr = ManagerService(llm), next(_ids)
    await svc.start(mgr)
    code = "FIN-" + (await svc.text(mgr, "Epsilon Co")).text.split("FIN-")[1].split()[0].rstrip(".:,")
    llm.say({"rules": RULES})
    await svc.text(mgr, "policy")
    await svc.button(mgr, "fp:save")
    async with session_scope() as s:
        company = await store.company_by_code(s, code)
        emp, _ = await claims.join(s, company, "emp-9", "Ravi Kumar")
        cid = (await claims.submit(s, llm, company, emp, "Client visit", [
            {"category": "cab", "description": "cab", "amount": "3000"},
            {"category": "cab", "description": "cab again", "amount": "2500"},
            {"category": "cab", "description": "late cab", "amount": "2000"},
        ])).id

    first = await svc.button(mgr, f"fa:amount:{cid}")  # asks about each expense in turn
    assert "expense 1 of 3" in first.text and any(b["data"] == f"fi:{cid}:p" for b in first.buttons[0])
    second = await svc.button(mgr, f"fi:{cid}:f")  # full amount for the first
    assert "expense 2 of 3" in second.text
    third = await svc.text(mgr, "2000")  # typed: pays 2,000 of 2,500
    assert "expense 3 of 3" in third.text
    summary = await svc.button(mgr, f"fi:{cid}:z")  # nothing for the last
    assert "₹7,000 of ₹7,500" not in summary.text and "Total ₹5,000 of ₹7,500" in summary.text
    done = await svc.button(mgr, f"fi:{cid}:ok")
    assert "approved for ₹5,000" in done.text

    event = [e for e in await outgoing("tracker") if e.get("claim_id") == cid and e["status"] == "approved"][0]
    text = event["text"]
    assert "approved: ₹5,000 of the ₹7,500 you claimed" in text
    assert "✅ cab: ₹3,000" in text and "⚠️ cab again: ₹2,000 of ₹2,500" in text and "❌ late cab: not refunded (₹2,000)" in text
    async with session_scope() as s:
        claim = await claims.get(s, cid)
        assert [d["approved"] for d in claim.decided_items] == ["3000.00", "2000.00", "0.00"]


async def test_the_manager_cannot_approve_more_than_was_claimed_for_one_expense(llm):
    svc, mgr = ManagerService(llm), next(_ids)
    await svc.start(mgr)
    code = "FIN-" + (await svc.text(mgr, "Zeta Co")).text.split("FIN-")[1].split()[0].rstrip(".:,")
    llm.say({"rules": RULES})
    await svc.text(mgr, "policy")
    await svc.button(mgr, "fp:save")
    async with session_scope() as s:
        company = await store.company_by_code(s, code)
        emp, _ = await claims.join(s, company, "emp-10", "Sana Iqbal")
        cid = (await claims.submit(s, llm, company, emp, "One cab", [{"category": "cab", "description": "cab", "amount": "700"}])).id
    await svc.button(mgr, f"fa:amount:{cid}")
    assert "only claimed ₹700" in (await svc.text(mgr, "900")).text


def test_an_unknown_limit_period_never_fails_the_whole_policy():
    from finance_agent.policy.models import Rule
    year = Rule.model_validate({"id": "training", "name": "Training", "cap_amount": 25000, "cap_unit": "per_year"})
    assert year.cap_unit == "per_item" and "per year" in year.conditions  # kept as a note for the manager
    assert Rule.model_validate({"id": "x", "name": "X", "cap_unit": "per trip"}).cap_unit == "per_claim"
    assert Rule.model_validate({"id": "x", "name": "X", "cap_unit": "per person"}).cap_unit == "per_item"
    assert Rule.model_validate({"id": "x", "name": "X"}).cap_unit == "per_item" and Rule.model_validate({"id": "x", "name": "X", "cap_unit": None}).cap_unit == "per_item"
