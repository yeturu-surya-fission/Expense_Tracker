"""
A real company policy (tiered hotel caps, a ground-transport limit shared by a whole trip, meal limits per meal, a monthly
internet cap, approval above ₹5,000, a list of things never reimbursed), the manager's commands, and the failure modes of
reading a policy with a language model.
"""
import itertools
from decimal import Decimal as D

import pytest

from finance_agent.claims import service as claims
from finance_agent.config import settings
from finance_agent.db.session import session_scope
from finance_agent.llm import LLMError
from finance_agent.manager.service import SPLIT_LIMIT, ManagerService
from finance_agent.policy import parser, store
from finance_agent.policy.engine import apply_pools, describe, evaluate, match_rule, pick_variant
from finance_agent.policy.models import Item, Rule

_ids = itertools.count(9_300_000_000)

TIER1 = ["mumbai", "delhi", "bengaluru", "bangalore", "chennai", "hyderabad", "kolkata", "pune", "ahmedabad"]
TIER2 = ["jaipur", "lucknow", "indore", "kochi", "coimbatore", "nagpur"]

# what a good reading of the policy looks like
POLICY = [
    {"id": "flights", "name": "Flights", "keywords": ["flight", "airfare"], "conditions": "economy class only"},
    {"id": "ground_transport", "name": "Ground transport", "keywords": ["cab", "taxi", "auto", "train", "bus", "metro", "fuel", "petrol"], "cap_amount": 5000, "cap_unit": "per_claim"},
    {"id": "breakfast", "name": "Breakfast", "keywords": ["breakfast"], "cap_amount": 200, "cap_unit": "per_day", "conditions": "only during approved business travel"},
    {"id": "lunch", "name": "Lunch", "keywords": ["lunch"], "cap_amount": 350, "cap_unit": "per_day", "conditions": "only during approved business travel"},
    {"id": "dinner", "name": "Dinner", "keywords": ["dinner"], "cap_amount": 450, "cap_unit": "per_day", "conditions": "only during approved business travel"},
    {"id": "hotel", "name": "Hotel stays", "keywords": ["hotel", "room", "stay", "lodging"], "cap_unit": "per_night", "receipt_over": 0,
     "variants": [{"label": "Tier-1 cities", "keywords": TIER1, "cap_amount": "₹4,000"},
                  {"label": "Tier-2 cities", "keywords": TIER2, "cap_amount": 3000},
                  {"label": "Other locations", "keywords": [], "cap_amount": "2k"}]},
    {"id": "internet_mobile", "name": "Internet and mobile", "keywords": ["internet", "wifi", "mobile", "phone", "broadband"], "cap_amount": 1000, "cap_unit": "per_month", "receipt_over": 0},
    {"id": "office_equipment", "name": "Office equipment and supplies", "keywords": ["laptop", "keyboard", "monitor", "stationery", "supplies"], "preapproval_over": 5000},
    {"id": "personal", "name": "Personal shopping and entertainment", "keywords": ["shopping", "movie", "entertainment"], "covered": False},
    {"id": "alcohol_tobacco", "name": "Alcohol and tobacco", "keywords": ["alcohol", "beer", "wine", "liquor", "tobacco", "cigarette"], "covered": False},
    {"id": "fines", "name": "Traffic fines", "keywords": ["fine", "challan", "penalty"], "covered": False},
]
RULES = [Rule.model_validate(r) for r in POLICY]
BY_ID = {r.id: r for r in RULES}


def item(ref=1, **kw) -> Item:
    return Item(ref=ref, **kw)


def run(*items: Item, used_before=None):
    verdicts = [evaluate(match_rule(RULES, i), i) for i in items]
    return apply_pools(RULES, list(items), verdicts, used_before)


# ------------------------------------------------------------------ the numbers in this policy
def test_the_ground_transport_limit_is_shared_by_the_whole_trip():
    cab, train = item(1, category="cab", description="cab", amount=D(3000)), item(2, category="train", description="train to Pune", amount=D(3500))
    a, b = run(cab, train)
    assert (a.eligible, b.eligible) == (D("3000.00"), D("2000.00"))  # 5,000 in total, not 6,500
    assert b.status == "partial" and "shared by everything in this claim" in b.reason


def test_a_trip_under_the_shared_limit_is_paid_in_full():
    a, b = run(item(1, category="cab", description="cab", amount=D(1200)), item(2, category="bus", description="bus", amount=D(800)))
    assert (a.eligible, b.eligible) == (D("1200.00"), D("800.00"))


def test_hotel_limits_follow_the_city_tier():
    pune = evaluate(BY_ID["hotel"], item(category="hotel", description="Pune hotel", amount=D(15000), nights=2, has_receipt=True))
    assert pick_variant(BY_ID["hotel"], item(description="hotel in Pune")).label == "Tier-1 cities"
    assert (pune.status, pune.eligible) == ("partial", D("8000.00")) and "Tier-1 cities" in pune.reason  # 2 × 4,000
    jaipur = evaluate(BY_ID["hotel"], item(category="hotel", description="hotel in Jaipur", amount=D(7000), nights=2, has_receipt=True))
    assert jaipur.eligible == D("6000.00")  # 2 × 3,000
    other = evaluate(BY_ID["hotel"], item(category="hotel", description="hotel in Shillong", amount=D(7000), nights=2, has_receipt=True, variant="Other locations"))
    assert other.eligible == D("4000.00")  # 2 × 2,000


def test_an_unknown_city_is_asked_about_not_guessed():
    v = evaluate(BY_ID["hotel"], item(category="hotel", description="hotel in Shillong", amount=D(7000), nights=2, has_receipt=True))
    assert v.status == "needs_info" and v.questions[0].key == "variant"
    assert "Tier-1 cities, Tier-2 cities, Other locations" in v.questions[0].text
    said = evaluate(BY_ID["hotel"], item(category="hotel", description="hotel in Shillong", amount=D(7000), nights=2, has_receipt=True, variant="tier-2 cities"))
    assert said.eligible == D("6000.00")  # matched case-insensitively


def test_hotels_always_need_a_receipt_in_this_policy():
    assert {q.key for q in evaluate(BY_ID["hotel"], item(category="hotel", description="Pune hotel", amount=D(100), nights=1)).questions} == {"has_receipt"}


def test_meals_are_capped_per_meal_per_day():
    assert evaluate(BY_ID["lunch"], item(category="lunch", description="lunch", amount=D(900), days=2)).eligible == D("700.00")  # 2 × 350
    assert evaluate(BY_ID["dinner"], item(category="dinner", description="dinner", amount=D(300), days=1)).eligible == D("300.00")
    assert evaluate(BY_ID["breakfast"], item(category="breakfast", description="breakfast", amount=D(500), days=1)).eligible == D("200.00")


def test_the_monthly_internet_limit_is_shared_across_the_month():
    first = run(item(category="internet", description="home wifi", amount=D(800), has_receipt=True), used_before={"internet_mobile": D(0)})[0]
    assert first.eligible == D("800.00")
    second = run(item(category="internet", description="mobile bill", amount=D(600), has_receipt=True), used_before={"internet_mobile": D(800)})[0]
    assert second.eligible == D("200.00") and "this month" in second.reason  # only ₹200 of the ₹1,000 is left
    spent = run(item(category="internet", description="mobile bill", amount=D(600), has_receipt=True), used_before={"internet_mobile": D(1000)})[0]
    assert spent.eligible == D("0.00")


def test_equipment_needs_approval_only_above_five_thousand():
    assert evaluate(BY_ID["office_equipment"], item(category="keyboard", description="keyboard", amount=D(3000))).eligible == D("3000.00")  # no question asked
    big = evaluate(BY_ID["office_equipment"], item(category="laptop", description="laptop", amount=D(60000)))
    assert big.status == "needs_info" and big.questions[0].key == "preapproved"
    assert evaluate(BY_ID["office_equipment"], item(category="laptop", description="laptop", amount=D(60000), preapproved=False)).status == "not_eligible"
    assert evaluate(BY_ID["office_equipment"], item(category="laptop", description="laptop", amount=D(60000), preapproved=True)).eligible == D("60000.00")


@pytest.mark.parametrize("what", ["beer at dinner", "traffic challan", "movie tickets", "cigarettes"])
def test_what_the_policy_never_reimburses_is_refused(what):
    v = run(item(category="other", description=what, amount=D(500)))[0]
    assert v.status == "not_eligible" and v.eligible == D(0)


def test_money_written_any_way_is_understood_and_variants_are_described():
    hotel = BY_ID["hotel"]
    assert [v.cap_amount for v in hotel.variants] == [D(4000), D(3000), D(2000)]
    assert describe(hotel) == "Hotel stays: Tier-1 cities up to ₹4,000; Tier-2 cities up to ₹3,000; Other locations up to ₹2,000 per night, receipt always needed"
    assert "shared" in describe(BY_ID["ground_transport"]) or "all items together" in describe(BY_ID["ground_transport"])


# ------------------------------------------------------------------ a whole claim, through the claim service
async def company():
    async with session_scope() as s:
        c = await store.create_company(s, "Acme", next(_ids))
        await store.save(s, c.id, RULES, "test")
        e, _ = await claims.join(s, c, f"u{next(_ids)}", "Kiran Rao")
        return c, e


async def test_a_trip_claim_with_this_policy(llm):
    c, e = await company()
    items = [
        {"ref": 1, "category": "train", "description": "train to Pune", "amount": "3500"},
        {"ref": 2, "category": "cab", "description": "cabs in Pune", "amount": "3000"},
        {"ref": 3, "category": "hotel", "description": "Pune hotel", "amount": "15000", "nights": 2, "has_receipt": True},
        {"ref": 4, "category": "lunch", "description": "client lunch", "amount": "900", "days": 2},
        {"ref": 5, "category": "drinks", "description": "beer with the team", "amount": "700"},
    ]
    async with session_scope() as s:
        claim = await claims.submit(s, llm, c, e, "Pune trip", items)
        got = {v["ref"]: v for v in claim.verdicts}
        assert claim.status == "awaiting_manager"
        assert D(got[1]["eligible"]) == D("3500.00") and D(got[2]["eligible"]) == D("1500.00")  # 5,000 shared by the two ground items
        assert D(got[3]["eligible"]) == D("8000.00") and D(got[4]["eligible"]) == D("700.00") and D(got[5]["eligible"]) == D(0)
        assert claim.recommended == D("13700.00")


async def test_the_monthly_limit_counts_what_the_same_person_was_already_offered(llm):
    c, e = await company()
    async with session_scope() as s:
        first = await claims.submit(s, llm, c, e, "Wifi", [{"ref": 1, "category": "internet", "description": "wifi", "amount": "800", "has_receipt": True}])
        assert first.recommended == D("800.00")
    async with session_scope() as s:
        second = await claims.submit(s, llm, c, e, "Mobile", [{"ref": 1, "category": "internet", "description": "mobile", "amount": "600", "has_receipt": True}])
        assert second.recommended == D("200.00")
    other, _ = None, None
    async with session_scope() as s:
        c2 = await store.create_company(s, "Beta", next(_ids))
        await store.save(s, c2.id, RULES, "t")
        e2, _ = await claims.join(s, c2, "someone-else", "Asha")
        third = await claims.submit(s, llm, c2, e2, "Mobile", [{"ref": 1, "category": "internet", "description": "mobile", "amount": "600", "has_receipt": True}])
        assert third.recommended == D("600.00")  # another person's limit is their own


async def test_the_employee_is_asked_which_city_tier_and_the_answer_is_used(llm):
    c, e = await company()
    async with session_scope() as s:
        claim = await claims.submit(s, llm, c, e, "Trip", [{"ref": 1, "category": "hotel", "description": "hotel in Shillong", "amount": "7000", "nights": 2, "has_receipt": True}])
        cid = claim.id
        assert claim.status == "needs_info" and claim.verdicts[0]["questions"][0]["key"] == "variant"
    async with session_scope() as s:
        claim = await claims.get(s, cid)
        await claims.answer(s, llm, c, e, claim, {1: {"variant": "Other locations"}})
        assert claim.status == "awaiting_manager" and claim.recommended == D("4000.00")


# ------------------------------------------------------------------ reading a policy: the shapes models answer with
def test_models_answer_in_different_shapes_and_all_are_understood():
    two = [{"id": "hotel", "name": "Hotels", "cap_amount": "₹4,000", "percent": "80%"}, {"id": "gym", "name": "Gym", "covered": False}]
    for shape in (two, {"rules": two}, {"policy": {"rules": two}}, {"expense_rules": two}):
        rules = parser._validate(shape)
        assert [r.id for r in rules] == ["hotel", "gym"] and rules[0].cap_amount == D(4000) and rules[0].percent == 80


@pytest.mark.parametrize("junk", [{}, {"rules": []}, {"message": "sorry"}, [], {"rules": ["not a rule"]}])
def test_an_answer_with_no_rules_is_a_retryable_plain_message(junk):
    with pytest.raises(parser.PolicyParseError) as e:
        parser._validate(junk)
    assert e.value.retryable and ("couldn't turn that into rules" in str(e.value) or "rules was unclear" in str(e.value))


# ------------------------------------------------------------------ the manager's commands
async def manager(llm, name="Acme Co"):
    svc, mgr = ManagerService(llm), next(_ids)
    await svc.start(mgr)
    created = await svc.text(mgr, name)
    return svc, mgr, "FIN-" + created.text.split("FIN-")[1].split()[0].rstrip(".:,")


async def test_setpolicy_then_paste_reads_it_with_a_progress_message_and_a_long_timeout(llm):
    svc, mgr, _ = await manager(llm)
    said = []

    async def progress(t):
        said.append(t)

    llm.say({"rules": POLICY})
    draft = await svc.text(mgr, "Employee Reimbursement Policy ... (the pasted policy)", progress)
    assert said and "Reading your policy" in said[0]
    assert llm.timeouts[-1] == settings.policy_timeout  # reading a whole policy is allowed longer than a quick answer
    assert "Hotel stays: Tier-1 cities up to ₹4,000" in draft.text and "Ground transport: up to ₹5,000 per trip/claim" in draft.text
    assert [b["data"] for b in draft.buttons[0]] == ["fp:save", "fp:discard"]
    assert "policy v1" in (await svc.button(mgr, "fp:save")).text


async def test_a_long_policy_split_by_telegram_is_collected_then_read_once(llm):
    svc, mgr, _ = await manager(llm)
    part1, part2 = "A" * SPLIT_LIMIT, "tail of the policy"
    assert "Got part 1" in (await svc.text(mgr, part1)).text  # nothing is sent to the model yet
    assert "Got part 2" in (await svc.text(mgr, part2)).text
    assert llm.calls == []
    llm.say({"rules": POLICY})
    draft = await svc.done(mgr)
    assert "Hotel stays" in draft.text
    assert part1 in llm.calls[-1][1] and part2 in llm.calls[-1][1] and llm.calls[-1][1].index(part1) < llm.calls[-1][1].index(part2)  # in order, as one text
    assert "nothing to finish" in (await svc.done(mgr)).text.lower()


async def test_setpolicy_command_with_and_without_text_and_replacing_a_policy(llm):
    svc, mgr, _ = await manager(llm)
    assert "Send the policy now" in (await svc.set_policy(mgr)).text or "Send the policy now" in (await svc.set_policy(mgr)).text.replace("📥 ", "")
    llm.say({"rules": POLICY[:3]})
    first = await svc.text(mgr, "flights, ground transport and breakfast only")
    await svc.button(mgr, "fp:save")
    assert "Flights" in first.text

    again = await svc.set_policy(mgr)
    assert "replace your current policy" in again.text
    llm.say({"rules": POLICY})
    replaced = await svc.text(mgr, "the full policy")
    assert replaced.text.startswith("Here's what would change in your policy") and "➕ New: Hotel stays" in replaced.text
    assert "policy v2" in (await svc.button(mgr, "fp:save")).text

    llm.say({"rules": POLICY})  # inline: /setpolicy <text>
    same = await svc.set_policy(mgr, "the full policy again")
    assert "Nothing to change" in same.text


async def test_editpolicy_needs_a_policy_and_then_changes_only_what_was_asked(llm):
    svc, mgr, _ = await manager(llm)
    assert "setpolicy" in (await svc.edit_policy(mgr)).text
    llm.say({"rules": POLICY})
    await svc.text(mgr, "policy")
    await svc.button(mgr, "fp:save")
    assert "What should change" in (await svc.edit_policy(mgr)).text
    changed = [dict(r) for r in POLICY]
    changed[1] = {**changed[1], "cap_amount": 6000}
    llm.say({"rules": changed})
    edit = await svc.text(mgr, "raise the ground transport limit to 6000")
    assert "up to ₹5,000 per trip/claim (all items together)" in edit.text and "up to ₹6,000" in edit.text
    assert "CURRENT RULES" in llm.calls[-1][1] and "raise the ground transport limit" in llm.calls[-1][1]
    assert "policy v2" in (await svc.button(mgr, "fp:save")).text


async def test_a_failed_read_keeps_the_text_and_try_again_works(llm):
    svc, mgr, _ = await manager(llm)
    llm.say(LLMError("the language model is not reachable (ReadTimeout)"))
    failed = await svc.text(mgr, "my whole policy text")
    assert "couldn't read that just now" in failed.text and "Try again" in failed.text
    assert [b["data"] for b in failed.buttons[0]] == ["fp:retry", "fp:discard"]
    llm.say({"rules": POLICY})
    ok = await svc.button(mgr, "fp:retry")  # no need to paste it again
    assert "Hotel stays" in ok.text and "my whole policy text" in llm.calls[-1][1]
    assert "nothing to retry" in (await svc.retry(mgr)).text.lower()

    llm.say({"policy_summary": "I could not find any rules"})  # an answer without rules is retryable too
    nope = await svc.text(mgr, "second policy text")
    assert "couldn't turn that into rules" in nope.text and nope.buttons[0][0]["data"] == "fp:retry"
    llm.say({"rules": POLICY})
    assert "Here's" in (await svc.retry(mgr)).text


async def test_cancel_clears_whatever_is_pending_and_a_pdf_is_one_whole_policy(llm):
    svc, mgr, _ = await manager(llm)
    await svc.text(mgr, "A" * SPLIT_LIMIT)
    assert "Cancelled" in (await svc.cancel(mgr)).text and "Nothing to cancel" in (await svc.cancel(mgr)).text
    llm.say({"rules": POLICY})
    draft = await svc.document(mgr, "B" * (SPLIT_LIMIT + 500))  # a file longer than a message is not 'part 1 of more'
    assert "Hotel stays" in draft.text and llm.calls[-1][1].startswith("B")


async def test_info_commands(llm):
    svc, mgr, code = await manager(llm)
    assert code in (await svc.code(mgr)).text and "No policy has been saved" in (await svc.history(mgr)).text
    assert "Nobody has joined" in (await svc.employees(mgr)).text
    llm.say({"rules": POLICY})
    await svc.text(mgr, "policy")
    await svc.button(mgr, "fp:save")
    hist = (await svc.history(mgr)).text
    assert "v1" in hist and "Initial policy" in hist
    async with session_scope() as s:
        company = await store.company_by_code(s, code)
        await claims.join(s, company, "e1", "Kiran Rao")
        await claims.join(s, company, "e2", "Asha Menon")
    emps = (await svc.employees(mgr)).text
    assert "2 employee(s)" in emps and "Kiran Rao" in emps and "Asha Menon" in emps
    stranger = ManagerService(llm)
    for method in ("code", "policy", "history", "employees", "pending", "paid_list"):
        assert "/start" in (await getattr(stranger, method)(next(_ids))).text  # someone who never set up a company


# ------------------------------------------------------------------ rules as the REAL model (CodeVector) read the pasted policy
def _real_rules():
    import json
    from pathlib import Path
    return [Rule.model_validate(r) for r in json.loads((Path(__file__).parent / "fixtures" / "real_model_rules.json").read_text(encoding="utf-8"))]


def test_what_the_real_model_extracted_gives_the_right_numbers():
    rules = _real_rules()
    run_real = lambda *items: apply_pools(rules, list(items), [evaluate(match_rule(rules, i), i) for i in items])
    cab, train = run_real(item(1, category="cab", description="cab", amount=D(3000)), item(2, category="train", description="train", amount=D(3500)))
    assert (cab.eligible, train.eligible) == (D("3000.00"), D("2000.00"))  # one ₹5,000 limit for the whole trip
    hotel = item(category="hotel", description="Pune hotel", amount=D(15000), nights=2, has_receipt=True)
    asked = evaluate(match_rule(rules, hotel), hotel)
    assert asked.status == "needs_info" and asked.questions[0].key == "variant"  # the policy doesn't say which cities are Tier-1: ask, don't guess
    answered = evaluate(match_rule(rules, hotel), hotel.model_copy(update={"variant": "Tier-1 cities"}))
    assert answered.eligible == D("8000.00")
    assert evaluate(match_rule(rules, item(category="lunch", description="lunch")), item(category="lunch", description="lunch", amount=D(900), days=2)).eligible == D("700.00")
    for thing in ("beer with the team", "traffic challan", "movie tickets", "family dinner for my spouse"):
        v = run_real(item(category="other", description=thing, amount=D(500)))[0]
        assert v.status == "not_eligible", thing
    laptop = item(category="laptop", description="laptop", amount=D(60000))
    assert evaluate(match_rule(rules, laptop), laptop).questions[0].key == "preapproved"


async def test_a_question_about_a_shared_limit_says_it_is_shared(llm):
    from finance_agent.policy import qa
    llm.say({"kind": "item", "rule_id": "ground_transport", "item": {"description": "cab", "amount": 3000}})
    r = await qa.answer(llm, RULES, 1, ["Employee: can I claim a 3000 cab?"])
    assert r.status == "answer" and "shared by everything of this kind in the trip" in r.text
    llm.say({"kind": "item", "rule_id": "hotel", "item": {"description": "hotel in Pune", "amount": 15000, "nights": 2, "variant": "Tier-1 cities"}})
    pune = await qa.answer(llm, RULES, 1, ["Employee: hotel in Pune, 2 nights, 15000?"])
    assert "₹8,000 of ₹15,000" in pune.text and "shared" not in pune.text  # per-night limits are not pooled
    llm.say({"kind": "item", "rule_id": "hotel", "item": {"description": "hotel in Shillong", "amount": 7000, "nights": 2}})
    unknown = await qa.answer(llm, RULES, 1, ["Employee: hotel somewhere, 2 nights, 7000?"])
    assert unknown.status == "need_info" and "Which applies: Tier-1 cities, Tier-2 cities, Other locations?" in unknown.text


# ------------------------------------------------------------------ a word in the category alone is only a hint
async def test_the_description_decides_and_a_category_word_alone_is_confirmed(llm):
    from finance_agent.claims.review import rule_for
    # 'food' (picked by the chat assistant) is a keyword of the meals rule, but 'sweets for an event' is not a meal
    sweets = item(category="food", description="sweets for the business event", amount=D(500))
    meals_like = [Rule.model_validate({"id": "meals", "name": "Meals", "keywords": ["food", "meal"], "cap_amount": 1000, "cap_unit": "per_day"})]
    llm.say({"rule_id": None})
    assert await rule_for(llm, meals_like, sweets) is None  # the model says no rule covers it: the manager decides
    assert len(llm.calls) == 1
    # a description that really is a meal needs no question at all
    assert (await rule_for(llm, meals_like, item(category="food", description="team meal", amount=D(300)))).id == "meals"
    assert len(llm.calls) == 1
    # the model is unavailable: the category hint is used rather than nothing
    llm.say(LLMError("down"))
    assert (await rule_for(llm, meals_like, sweets)).id == "meals"
    # a category that IS the rule's name is exact, no question either
    assert (await rule_for(llm, meals_like, item(category="meals", description="x", amount=D(1)))).id == "meals"
    assert len(llm.calls) == 2
