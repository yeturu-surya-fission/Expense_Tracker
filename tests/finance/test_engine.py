"""The policy maths. Every amount an employee is told comes from here, so these are exact."""
from decimal import Decimal as D

import pytest

from finance_agent.policy.engine import describe, evaluate, match_rule, open_questions, total
from finance_agent.policy.models import Item, Rule
from tests.finance.conftest import RULES

RULES_ = [Rule.model_validate(r) for r in RULES]
BY_ID = {r.id: r for r in RULES_}


def item(**kw) -> Item:
    return Item(ref=kw.pop("ref", 1), **kw)


def verdict(rule_id: str, **kw):
    return evaluate(BY_ID[rule_id], item(**kw))


def test_a_hotel_over_the_nightly_cap_is_paid_up_to_the_cap():
    v = verdict("hotel", amount=D(15000), nights=2, has_receipt=True)
    assert (v.status, v.eligible) == ("partial", D("12000.00"))
    assert "2 × ₹6,000 per night" in v.reason


def test_a_hotel_within_the_cap_is_paid_in_full():
    v = verdict("hotel", amount=D(10000), nights=2, has_receipt=True)
    assert (v.status, v.eligible) == ("eligible", D("10000.00"))


def test_a_hotel_needs_the_number_of_nights_and_a_receipt_before_we_can_say():
    v = verdict("hotel", amount=D(15000))
    assert v.status == "needs_info" and v.eligible is None
    assert {q.key for q in v.questions} == {"nights", "has_receipt"}


def test_no_receipt_above_the_threshold_means_not_eligible_but_small_amounts_need_none():
    assert verdict("hotel", amount=D(900), nights=1, has_receipt=False).status == "not_eligible"
    small = Rule(id="parking", name="Parking", receipt_over=D(500))
    assert evaluate(small, item(amount=D(300))).status == "eligible"  # under the threshold: no receipt question, no refusal


def test_meals_are_capped_per_day():
    v = verdict("meals", amount=D(1400), days=1)
    assert (v.status, v.eligible) == ("partial", D("1000.00"))
    assert verdict("meals", amount=D(2500), days=3).eligible == D("2500.00")


def test_a_percentage_rule_pays_that_share():
    v = verdict("internet", amount=D(1000))
    assert (v.status, v.eligible) == ("partial", D("500.00"))
    assert "50%" in v.reason


def test_percentage_applies_after_the_cap():
    rule = Rule(id="phone", name="Phone", cap_amount=D(800), percent=50)
    assert evaluate(rule, item(amount=D(1000))).eligible == D("400.00")


def test_a_rule_that_is_not_covered_pays_nothing():
    v = verdict("gym", amount=D(1500))
    assert (v.status, v.eligible) == ("not_eligible", D(0))


def test_preapproval_is_asked_and_enforced():
    assert verdict("training", amount=D(8000)).questions[0].key == "preapproved"
    assert verdict("training", amount=D(8000), preapproved=False).status == "not_eligible"
    assert verdict("training", amount=D(8000), preapproved=True).eligible == D("8000.00")


def test_an_item_no_rule_covers_goes_to_the_manager():
    v = evaluate(None, item(amount=D(500), description="spa day"))
    assert v.status == "needs_manager" and v.eligible == D(0)


def test_missing_or_zero_amount_is_asked_for():
    assert verdict("local_travel").questions[0].key == "amount"


def test_money_never_drifts_on_awkward_numbers():
    rule = Rule(id="x", name="X", percent=33)
    assert evaluate(rule, item(amount=D("100.10"))).eligible == D("33.03")  # 33.033 rounded to paise


def test_matching_by_id_name_keyword_and_longest_keyword():
    assert match_rule(RULES_, item(category="hotel")).id == "hotel"
    assert match_rule(RULES_, item(category="Hotel stays")).id == "hotel"
    assert match_rule(RULES_, item(category="travel", description="Uber cab to client office")).id == "local_travel"
    assert match_rule(RULES_, item(category="other", description="team lunch with client")).id == "meals"
    assert match_rule(RULES_, item(category="spa", description="massage")) is None
    # keywords match whole words only: 'auto' must not match 'automatic'
    assert match_rule(RULES_, item(category="x", description="automatic renewal")) is None


def test_totals_and_open_questions():
    vs = [verdict("hotel", ref=1, amount=D(15000), nights=2, has_receipt=True), verdict("meals", ref=2, amount=D(900)), verdict("local_travel", ref=3, amount=D(450))]
    assert total(vs) == D("12000.00") + D("450.00")  # the meal still needs 'days', so it has no number yet
    assert [q.key for q in open_questions(vs)] == ["days"]


def test_every_rule_can_be_described_in_words():
    assert describe(BY_ID["hotel"]) == "Hotel stays: up to ₹6,000 per night, receipt needed above ₹500"
    assert describe(BY_ID["gym"]) == "Gym membership: not reimbursed"
    assert "approval before you spend" in describe(BY_ID["training"])


@pytest.mark.parametrize("bad", [{"percent": 150}, {"cap_amount": -1}])
def test_nonsense_rules_are_rejected(bad):
    with pytest.raises(Exception):
        Rule.model_validate({"id": "x", "name": "X", **bad})
