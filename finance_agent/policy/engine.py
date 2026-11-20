"""
The maths of a policy. Pure functions: no database, no LLM, no network. Every rupee a person is told they will get
comes from here, so a model's mistake can never change an amount.

    match_rule(rules, item)        which rule covers this item (by id, category or keyword), or None
    evaluate(rule, item)           the verdict for ONE item: how much is eligible, or what we still need to ask
    apply_pools(...)               limits shared by several items ("₹5,000 per trip, all ground transport together")
    describe(rule)                 the rule in one readable line
"""

from decimal import Decimal

from finance_agent.formatting import money, q2
from finance_agent.policy.models import Item, Question, Rule, Variant, Verdict

UNIT_WORDS = {
    "per_item": "per item",
    "per_night": "per night",
    "per_day": "per day",
    "per_month": "per month (all items together)",
    "per_claim": "per trip/claim (all items together)",
}
POOLED = ("per_claim", "per_month")


def _words(text: str) -> str:
    return (
        " "
        + " ".join("".join(c.lower() if c.isalnum() else " " for c in text).split())
        + " "
    )


def _has(text_words: str, keyword: str) -> bool:
    """A whole word, singular or plural ('cigarette' matches 'cigarettes'; 'auto' does not match 'automatic')."""
    kw = _words(keyword).strip()
    if not kw:
        return False
    forms = {kw, kw + "s", kw + "es"}
    if len(kw) > 3 and kw.endswith(
        "es"
    ):  # 'movies' also matches 'movie' (and 'taxes' 'tax')
        forms.add(kw[:-2])
    if len(kw) > 3 and kw.endswith("s"):
        forms.add(kw[:-1])
    return any(f" {form} " in text_words for form in forms)


def exact_rule(rules: list[Rule], item: Item) -> Rule | None:
    """The category IS a rule's id or name (e.g. category 'lunch' for the Lunch rule)."""
    label = item.category.strip().lower()
    return next((r for r in rules if label and label in (r.id, r.name.lower())), None)


def match_rule(rules: list[Rule], item: Item) -> Rule | None:
    """
    Exact id/name first, then a keyword in the category or description. Longer keywords win (so 'taxi fare' beats 'fare').
    A rule that says "not reimbursed" beats one that pays: 'beer at dinner' is alcohol, not dinner (the manager can still override).
    """
    if (exact := exact_rule(rules, item)) is not None:
        return exact
    text = _words(f"{item.category} {item.description}")
    best: tuple[bool, int, Rule] | None = None  # (is a refusal, keyword length, rule)
    for r in rules:
        for kw in [r.id.replace("_", " "), *r.keywords]:
            if kw and _has(text, kw):
                candidate = (not r.covered, len(kw), r)
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
    return best[2] if best else None


def pick_variant(rule: Rule, item: Item) -> Variant | None:
    """Which case of the rule applies (e.g. the city tier): what the person said, or a keyword (a city name) in the item. None = unknown."""
    if not rule.variants:
        return None
    if item.variant:
        said = _words(item.variant)
        for v in rule.variants:
            if _words(v.label) == said:
                return v
    text = _words(
        f"{item.category} {item.description} {item.purpose or ''} {item.variant or ''}"
    )
    hits = [
        v for v in rule.variants if any(_has(text, k) for k in [*v.keywords, v.label])
    ]
    return hits[0] if len(hits) == 1 else None


def effective_cap(rule: Rule, item: Item) -> Decimal | None:
    v = pick_variant(rule, item)
    return (
        v.cap_amount if v is not None and v.cap_amount is not None else rule.cap_amount
    )


def describe(rule: Rule) -> str:
    if not rule.covered:
        return f"{rule.name}: not reimbursed" + (
            f" ({rule.conditions})" if rule.conditions else ""
        )
    parts = []
    if rule.variants:
        parts.append(
            "; ".join(
                f"{v.label} "
                + (
                    f"up to {money(v.cap_amount)}"
                    if v.cap_amount is not None
                    else "no limit"
                )
                for v in rule.variants
            )
            + f" {UNIT_WORDS[rule.cap_unit]}"
        )
    elif rule.cap_amount is not None:
        parts.append(f"up to {money(rule.cap_amount)} {UNIT_WORDS[rule.cap_unit]}")
    else:
        parts.append("no limit")
    if rule.percent < 100:
        parts.append(f"{rule.percent}% reimbursed")
    if rule.receipt_over is not None:
        parts.append(
            "receipt always needed"
            if rule.receipt_over == 0
            else f"receipt needed above {money(rule.receipt_over)}"
        )
    if rule.needs_preapproval:
        parts.append("needs approval before you spend")
    elif rule.preapproval_over is not None:
        parts.append(
            f"needs approval before you spend above {money(rule.preapproval_over)}"
        )
    if rule.conditions:
        parts.append(rule.conditions)
    return f"{rule.name}: " + ", ".join(parts)


def _units(rule: Rule, item: Item) -> int | None:
    if rule.cap_unit == "per_night":
        return item.nights
    if rule.cap_unit == "per_day":
        return item.days
    return 1


def _needs_preapproval(rule: Rule, item: Item) -> bool:
    return rule.needs_preapproval or (
        rule.preapproval_over is not None
        and item.amount is not None
        and item.amount > rule.preapproval_over
    )


def ask_for(rule: Rule, item: Item, *, strict: bool = True) -> list[Question]:
    """What we still need to know before we can give a number. `strict=False` (just asking) never asks about receipts or approvals."""
    qs: list[Question] = []
    if rule.covered and rule.variants and pick_variant(rule, item) is None:
        qs.append(
            Question(
                ref=item.ref,
                key="variant",
                text="Which applies: "
                + ", ".join(v.label for v in rule.variants)
                + "?",
            )
        )
    if rule.covered and rule.cap_unit == "per_night" and item.nights is None:
        qs.append(Question(ref=item.ref, key="nights", text="How many nights?"))
    if rule.covered and rule.cap_unit == "per_day" and item.days is None:
        qs.append(Question(ref=item.ref, key="days", text="How many days?"))
    if strict and rule.covered:
        if _needs_preapproval(rule, item) and item.preapproved is None:
            qs.append(
                Question(
                    ref=item.ref,
                    key="preapproved",
                    text="Was this approved by your manager before you spent it? (yes/no)",
                )
            )
        if (
            rule.receipt_over is not None
            and item.amount is not None
            and item.amount > rule.receipt_over
            and item.has_receipt is None
        ):
            qs.append(
                Question(
                    ref=item.ref,
                    key="has_receipt",
                    text="Do you have the receipt for this? (yes/no)",
                )
            )
    return qs


def evaluate(rule: Rule | None, item: Item) -> Verdict:
    """The verdict for one claimed item (limits shared with other items are applied afterwards by `apply_pools`)."""
    if rule is None:
        return Verdict(
            ref=item.ref,
            status="needs_manager",
            eligible=Decimal(0),
            reason="No rule in the policy covers this, so your manager will decide.",
        )
    base = dict(ref=item.ref, rule_id=rule.id, rule_name=rule.name)
    if not rule.covered:
        why = f" ({rule.conditions})" if rule.conditions else ""
        return Verdict(
            **base,
            status="not_eligible",
            eligible=Decimal(0),
            reason=f"{rule.name} is not reimbursed under the policy{why}.",
        )
    if item.amount is None or item.amount <= 0:
        return Verdict(
            **base,
            status="needs_info",
            questions=[
                Question(ref=item.ref, key="amount", text="What was the amount?")
            ],
            reason="Amount missing.",
        )

    questions = ask_for(rule, item)
    if questions:
        return Verdict(
            **base,
            status="needs_info",
            questions=questions,
            reason="More information needed.",
        )

    amount = q2(item.amount)
    if _needs_preapproval(rule, item) and item.preapproved is False:
        return Verdict(
            **base,
            status="not_eligible",
            eligible=Decimal(0),
            reason=f"{rule.name} needs approval before spending, and this wasn't approved.",
        )
    if (
        rule.receipt_over is not None
        and amount > rule.receipt_over
        and item.has_receipt is False
    ):
        need = (
            "always" if rule.receipt_over == 0 else f"above {money(rule.receipt_over)}"
        )
        return Verdict(
            **base,
            status="not_eligible",
            eligible=Decimal(0),
            reason=f"A receipt is required {need}, and there isn't one.",
        )

    notes: list[str] = []
    allowed = amount
    cap = effective_cap(rule, item)
    if cap is not None:
        units = _units(rule, item) or 1
        cap_total = q2(cap * units)
        if cap_total < allowed:
            variant = pick_variant(rule, item)
            per = f"{money(cap)} {UNIT_WORDS[rule.cap_unit]}" + (
                f", {variant.label}" if variant else ""
            )
            notes.append(
                f"the limit is {money(cap_total)} ({units} × {per})"
                if units > 1
                else f"the limit is {money(cap_total)} ({per})"
            )
            allowed = cap_total
    if rule.percent < 100:
        allowed = q2(allowed * rule.percent / 100)
        notes.append(f"only {rule.percent}% is reimbursed")
    allowed = min(q2(allowed), amount)
    status = "eligible" if allowed == amount else "partial"
    reason = (
        "Fully covered."
        if status == "eligible"
        else f"{money(allowed)} of {money(amount)}: " + ", ".join(notes) + "."
    )
    return Verdict(
        **base, status=status, eligible=allowed, reason=reason, check=rule.conditions
    )


def apply_pools(
    rules: list[Rule],
    items: list[Item],
    verdicts: list[Verdict],
    used_before: dict[str, Decimal] | None = None,
) -> list[Verdict]:
    """
    Limits that belong to the whole claim (`per_claim`) or the whole month (`per_month`) are shared by every item of that rule:
    '₹5,000 per trip for all ground transport together' pays 5,000 for a 3,000 cab plus a 3,500 train, not 6,500.
    `used_before` = what the person was already given under a per-month rule earlier this month.
    """
    by_id = {r.id: r for r in rules}
    by_ref = {i.ref: i for i in items}
    remaining: dict[str, Decimal] = {}
    for v in verdicts:
        rule = by_id.get(v.rule_id or "")
        if (
            rule is None
            or rule.cap_unit not in POOLED
            or v.eligible is None
            or v.status not in ("eligible", "partial")
        ):
            continue
        cap = effective_cap(rule, by_ref[v.ref])
        if cap is None:
            continue
        left = remaining.setdefault(
            rule.id,
            max(
                q2(cap) - (used_before or {}).get(rule.id, Decimal(0))
                if rule.cap_unit == "per_month"
                else q2(cap),
                Decimal(0),
            ),
        )
        give = min(v.eligible, left)
        remaining[rule.id] = left - give
        if give < v.eligible:
            what = "this month" if rule.cap_unit == "per_month" else "this claim"
            v.reason = f"{money(give)} of {money(by_ref[v.ref].amount or 0)}: the {money(cap)} limit for {rule.name.lower()} is shared by everything in {what}."
            v.eligible, v.status = q2(give), "partial"
    return verdicts


def total(verdicts: list[Verdict]) -> Decimal:
    """What the policy would pay for everything that has a number."""
    return q2(sum((v.eligible for v in verdicts if v.eligible is not None), Decimal(0)))


def open_questions(verdicts: list[Verdict]) -> list[Question]:
    return [q for v in verdicts for q in v.questions]
