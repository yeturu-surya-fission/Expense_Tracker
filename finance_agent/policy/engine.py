"""
The maths of a policy. Pure functions: no database, no LLM, no network. Every rupee a person is told they will get
comes from here, so a model's mistake can never change an amount.

    match_rule(rules, item)      which rule covers this item (by id, category or keyword), or None
    evaluate(rule, item)         the verdict: how much is eligible, or what we still need to ask
    describe(rule)               the rule in one readable line
"""
from decimal import Decimal

from finance_agent.formatting import money, q2
from finance_agent.policy.models import Item, Question, Rule, Verdict

UNIT_WORDS = {"per_item": "per item", "per_night": "per night", "per_day": "per day", "per_month": "per month", "per_claim": "per claim"}


def _words(text: str) -> str:
    return " " + "".join(c.lower() if c.isalnum() else " " for c in text) + " "


def match_rule(rules: list[Rule], item: Item) -> Rule | None:
    """Exact id/name first, then a keyword in the category or description. Longer keywords win (so 'taxi fare' beats 'fare')."""
    label = item.category.strip().lower()
    for r in rules:
        if label and label in (r.id, r.name.lower()):
            return r
    text = _words(f"{item.category} {item.description}")
    best: tuple[int, Rule] | None = None
    for r in rules:
        for kw in [r.id.replace("_", " "), *r.keywords]:
            if kw and f" {kw.lower()} " in text and (best is None or len(kw) > best[0]):
                best = (len(kw), r)
    return best[1] if best else None


def describe(rule: Rule) -> str:
    if not rule.covered:
        return f"{rule.name}: not reimbursed" + (f" ({rule.conditions})" if rule.conditions else "")
    parts = []
    if rule.cap_amount is not None:
        parts.append(f"up to {money(rule.cap_amount)} {UNIT_WORDS[rule.cap_unit]}")
    else:
        parts.append("no limit")
    if rule.percent < 100:
        parts.append(f"{rule.percent}% reimbursed")
    if rule.receipt_over is not None:
        parts.append("receipt always needed" if rule.receipt_over == 0 else f"receipt needed above {money(rule.receipt_over)}")
    if rule.needs_preapproval:
        parts.append("needs approval before you spend")
    if rule.conditions:
        parts.append(rule.conditions)
    return f"{rule.name}: " + ", ".join(parts)


def _units(rule: Rule, item: Item) -> int | None:
    if rule.cap_unit == "per_night":
        return item.nights
    if rule.cap_unit == "per_day":
        return item.days
    return 1


def ask_for(rule: Rule, item: Item, *, strict: bool = True) -> list[Question]:
    """What we still need to know before we can give a number. `strict=False` (just asking) never asks about receipts or approvals."""
    qs: list[Question] = []
    if rule.covered and rule.cap_unit == "per_night" and item.nights is None:
        qs.append(Question(ref=item.ref, key="nights", text="How many nights?"))
    if rule.covered and rule.cap_unit == "per_day" and item.days is None:
        qs.append(Question(ref=item.ref, key="days", text="How many days?"))
    if strict and rule.covered:
        if rule.needs_preapproval and item.preapproved is None:
            qs.append(Question(ref=item.ref, key="preapproved", text="Was this approved by your manager before you spent it? (yes/no)"))
        if rule.receipt_over is not None and item.amount is not None and item.amount > rule.receipt_over and item.has_receipt is None:
            qs.append(Question(ref=item.ref, key="has_receipt", text="Do you have the receipt for this? (yes/no)"))
    return qs


def evaluate(rule: Rule | None, item: Item) -> Verdict:
    """The verdict for one claimed item."""
    if rule is None:
        return Verdict(
            ref=item.ref, status="needs_manager", eligible=Decimal(0),
            reason="No rule in the policy covers this, so your manager will decide.",
        )
    base = dict(ref=item.ref, rule_id=rule.id, rule_name=rule.name)
    if not rule.covered:
        why = f" ({rule.conditions})" if rule.conditions else ""
        return Verdict(**base, status="not_eligible", eligible=Decimal(0), reason=f"{rule.name} is not reimbursed under the policy{why}.")
    if item.amount is None or item.amount <= 0:
        return Verdict(**base, status="needs_info", questions=[Question(ref=item.ref, key="amount", text="What was the amount?")], reason="Amount missing.")

    questions = ask_for(rule, item)
    if questions:
        return Verdict(**base, status="needs_info", questions=questions, reason="More information needed.")

    amount = q2(item.amount)
    if rule.needs_preapproval and item.preapproved is False:
        return Verdict(**base, status="not_eligible", eligible=Decimal(0), reason=f"{rule.name} needs approval before spending, and this wasn't approved.")
    if rule.receipt_over is not None and amount > rule.receipt_over and item.has_receipt is False:
        need = "always" if rule.receipt_over == 0 else f"above {money(rule.receipt_over)}"
        return Verdict(**base, status="not_eligible", eligible=Decimal(0), reason=f"A receipt is required {need}, and there isn't one.")

    notes: list[str] = []
    allowed = amount
    if rule.cap_amount is not None:
        units = _units(rule, item) or 1
        cap_total = q2(rule.cap_amount * units)
        if cap_total < allowed:
            per = f"{money(rule.cap_amount)} {UNIT_WORDS[rule.cap_unit]}"
            notes.append(f"the limit is {money(cap_total)} ({units} × {per})" if units > 1 else f"the limit is {money(cap_total)} ({per})")
            allowed = cap_total
    if rule.percent < 100:
        allowed = q2(allowed * rule.percent / 100)
        notes.append(f"only {rule.percent}% is reimbursed")
    allowed = min(q2(allowed), amount)
    status = "eligible" if allowed == amount else "partial"
    reason = "Fully covered." if status == "eligible" else f"{money(allowed)} of {money(amount)}: " + ", ".join(notes) + "."
    return Verdict(**base, status=status, eligible=allowed, reason=reason)


def total(verdicts: list[Verdict]) -> Decimal:
    """What the policy would pay for everything that has a number."""
    return q2(sum((v.eligible for v in verdicts if v.eligible is not None), Decimal(0)))


def open_questions(verdicts: list[Verdict]) -> list[Question]:
    return [q for v in verdicts for q in v.questions]
