"""
A claim's life.

    submit ──► needs_info ──(employee answers)──► awaiting_manager ──► approved ──► (paid)
                                                        │  ▲    └────► rejected
                                              ask ◄─────┘  └── info_requested (the manager asked; the employee replies)

Every step that somebody else must hear about writes an outbox row (see outbox.py) in the same transaction.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_agent import outbox
from finance_agent.claims import review, texts
from finance_agent.db.models import Claim, Company, Employee
from finance_agent.formatting import money, q2
from finance_agent.llm import LLM
from finance_agent.policy import store
from finance_agent.policy.engine import open_questions, total
from finance_agent.policy.models import Item

MAX_ITEMS = 30


class ClaimError(ValueError):
    """A problem to tell the person in plain words."""


# ------------------------------------------------------------------ people
async def join(session: AsyncSession, company: Company, ref: str, name: str) -> tuple[Employee, bool]:
    emp = await employee(session, company.id, ref)
    if emp is not None:
        return emp, False
    emp = Employee(company_id=company.id, ref=ref, name=(name or "Employee")[:128])
    session.add(emp)
    await session.flush()
    await outbox.to_manager(session, company.manager_telegram_id, f"👋 {emp.name} joined {company.name}.")
    return emp, True


async def employee(session: AsyncSession, company_id: int, ref: str) -> Employee | None:
    return (await session.execute(select(Employee).where(Employee.company_id == company_id, Employee.ref == ref))).scalar_one_or_none()


async def get(session: AsyncSession, claim_id: int, *, company_id: int | None = None, employee_id: int | None = None) -> Claim:
    claim = await session.get(Claim, claim_id, with_for_update=True)
    if claim is None or (company_id is not None and claim.company_id != company_id) or (employee_id is not None and claim.employee_id != employee_id):
        raise ClaimError("I can't find that claim.")
    return claim


# ------------------------------------------------------------------ the employee side
def _parse_items(raw: list[dict]) -> list[Item]:
    if not raw:
        raise ClaimError("A claim needs at least one item.")
    if len(raw) > MAX_ITEMS:
        raise ClaimError(f"Please send at most {MAX_ITEMS} items in one claim.")
    items = []
    for n, r in enumerate(raw, 1):
        try:
            item = Item.model_validate({**r, "ref": r.get("ref") or n})
        except Exception as e:
            raise ClaimError(f"Item {n} isn't valid: {e}") from e
        if item.amount is not None and item.amount <= 0:
            raise ClaimError(f"Item {n}: the amount must be more than zero.")
        items.append(item)
    if len({i.ref for i in items}) != len(items):
        raise ClaimError("Each item needs its own reference number.")
    return items


async def _used_this_month(session: AsyncSession, claim: Claim, rules: list) -> dict[str, Decimal]:
    """What this employee was already offered this month under per-month limits (shared across their claims)."""
    monthly = {r.id for r in rules if r.cap_unit == "per_month"}
    if not monthly:
        return {}
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    others = (await session.execute(
        select(Claim).where(
            Claim.employee_id == claim.employee_id, Claim.id != claim.id, Claim.created_at >= start,
            Claim.status.in_(("awaiting_manager", "info_requested", "approved", "paid")),
        )
    )).scalars().all()
    used: dict[str, Decimal] = {}
    for other in others:
        for v in other.verdicts:
            if v.get("rule_id") in monthly and v.get("eligible") is not None:
                used[v["rule_id"]] = used.get(v["rule_id"], Decimal(0)) + Decimal(str(v["eligible"]))
    return used


async def _evaluate(session: AsyncSession, llm: LLM | None, claim: Claim) -> bool:
    """Re-check every item. Returns True if the claim now waits for the manager (False: the employee still has questions)."""
    policy = await store.current(session, claim.company_id)
    if policy is None:
        raise ClaimError("Your company hasn't set up a reimbursement policy yet.")
    version, rules = policy
    items = texts.items_of(claim)
    known = {v.ref: v.rule_id for v in texts.verdicts_of(claim)} if claim.policy_version == version else None  # same policy: keep the same matches
    used_before = await _used_this_month(session, claim, rules)
    verdicts = await review.review(llm, rules, items, known, used_before)
    claim.policy_version = version
    claim.verdicts = [v.model_dump(mode="json") for v in verdicts]
    claim.recommended = total(verdicts)
    return not open_questions(verdicts)


async def _hand_to_manager(session: AsyncSession, claim: Claim, emp: Employee, company: Company) -> None:
    claim.status = "awaiting_manager"
    await outbox.to_manager(session, company.manager_telegram_id, texts.manager_card(claim, emp.name), texts.manager_buttons(claim, emp.name))


async def submit(session: AsyncSession, llm: LLM | None, company: Company, emp: Employee, title: str, raw_items: list[dict], external_no: int | None = None) -> Claim:
    items = _parse_items(raw_items)
    claim = Claim(
        company_id=company.id, employee_id=emp.id, title=(title or "Expense claim").strip()[:160], items=[i.model_dump(mode="json") for i in items],
        verdicts=[], policy_version=0, thread=[], external_no=external_no,
    )
    session.add(claim)
    await session.flush()
    ready = await _evaluate(session, llm, claim)
    if ready:
        await _hand_to_manager(session, claim, emp, company)
    else:
        claim.status = "needs_info"
    await session.flush()
    return claim


def _coerce(key: str, value):
    if key in ("has_receipt", "preapproved"):
        if isinstance(value, str):
            return value.strip().lower() in ("yes", "y", "true", "1")
        return bool(value)
    if key in ("nights", "days"):
        n = int(Decimal(str(value)))
        if n <= 0:
            raise ValueError
        return n
    if key == "variant":
        text = str(value).strip()
        if not text:
            raise ValueError
        return text[:60]
    if key == "amount":
        n = Decimal(str(value))
        if n <= 0:
            raise ValueError
        return n
    raise KeyError(key)


async def answer(session: AsyncSession, llm: LLM | None, company: Company, emp: Employee, claim: Claim, answers: dict[int, dict]) -> Claim:
    """The employee's answers to our questions: {item ref: {nights: 2, has_receipt: true, ...}}."""
    if claim.status != "needs_info":
        raise ClaimError("That claim isn't waiting for any more information.")
    items = texts.items_of(claim)
    by_ref = {i.ref: i for i in items}
    for ref, fields in answers.items():
        item = by_ref.get(int(ref))
        if item is None:
            raise ClaimError(f"There is no item {ref} in this claim.")
        for key, value in (fields or {}).items():
            try:
                setattr(item, key, _coerce(key, value))
            except (KeyError, ValueError, InvalidOperation):
                raise ClaimError(f"I didn't understand '{key}: {value}' for item {ref}.") from None
    claim.items = [i.model_dump(mode="json") for i in items]
    if await _evaluate(session, llm, claim):
        await _hand_to_manager(session, claim, emp, company)
    await session.flush()
    return claim


async def employee_reply(session: AsyncSession, claim: Claim, emp: Employee, company: Company, text: str) -> Claim:
    """The employee answers a question the manager asked."""
    if claim.status != "info_requested":
        raise ClaimError("Your manager hasn't asked you anything about this claim.")
    claim.thread = [*claim.thread, {"from": "employee", "text": text.strip()[:1000]}]
    claim.status = "awaiting_manager"
    await outbox.to_manager(
        session, company.manager_telegram_id,
        f"💬 {emp.name} replied on claim #{claim.id}:\n“{text.strip()[:1000]}”\n\n" + texts.manager_card(claim, emp.name), texts.manager_buttons(claim, emp.name),
    )
    await session.flush()
    return claim


# ------------------------------------------------------------------ the manager side
def _announce(claim: Claim, emp: Employee) -> dict:
    return {
        "event": "claim_update", "employee_ref": emp.ref, "claim_id": claim.id, "status": claim.status,
        "approved_amount": str(claim.approved_amount) if claim.approved_amount is not None else None,
        "note": claim.decision_note, "items": claim.decided_items, "text": texts.employee_decision(claim),
    }


async def _decided(session: AsyncSession, claim: Claim, status: str, note: str | None) -> Employee:
    if claim.status not in ("awaiting_manager", "info_requested"):
        raise ClaimError(f"Claim #{claim.id} is already {claim.status.replace('_', ' ')}.")
    claim.status, claim.decision_note, claim.decided_at = status, (note or "").strip()[:500] or None, datetime.now(timezone.utc)
    return await session.get(Employee, claim.employee_id)


def policy_amounts(claim: Claim) -> dict[int, Decimal]:
    """What the policy would pay for each item (0 where it has no number)."""
    return {v.ref: q2(v.eligible or Decimal(0)) for v in texts.verdicts_of(claim)}


def _breakdown(claim: Claim, amounts: dict[int, Decimal]) -> list[dict]:
    """The decision per expense, with the reason for every one that was cut."""
    by_ref = {v.ref: v for v in texts.verdicts_of(claim)}
    rows = []
    for item in texts.items_of(claim):
        claimed, got = q2(item.amount or Decimal(0)), q2(amounts.get(item.ref, Decimal(0)))
        v = by_ref.get(item.ref)
        if got >= claimed:
            reason = ""
        elif v is not None and got == q2(v.eligible or Decimal(0)) and v.reason:
            reason = v.reason  # the policy's own reason
        else:
            reason = "Your manager decided this amount."
        rows.append({"ref": item.ref, "label": texts.label(item), "claimed": str(claimed), "approved": str(got), "reason": reason})
    return rows


async def approve(
    session: AsyncSession, claim: Claim, amount: Decimal | None = None, note: str | None = None, per_item: dict[int, Decimal] | None = None,
) -> Claim:
    """
    No amount: pay what the policy suggests, item by item. `per_item` = {item ref: amount}: the manager's own figure for each expense
    (the total is their sum). `amount` alone = just a total, with no breakdown (kept for callers that only have a total).
    """
    items = {i.ref: q2(i.amount or Decimal(0)) for i in texts.items_of(claim)}
    claimed = texts.claimed_total(claim)
    if per_item is None and amount is None:
        per_item = policy_amounts(claim)
    if per_item is not None:
        per_item = {ref: q2(per_item.get(ref, Decimal(0))) for ref in items}
        for ref, got in per_item.items():
            if got < 0 or got > items[ref]:
                raise ClaimError(f"Item {ref}: the amount must be between {money(0)} and {money(items[ref])} (what was claimed for it).")
        amount = sum(per_item.values(), Decimal(0))
    amount = q2(amount)
    if amount < 0 or amount > claimed:
        raise ClaimError(f"The amount must be between {money(0)} and {money(claimed)} (what the employee claimed).")
    emp = await _decided(session, claim, "approved", note)
    claim.decided_items = _breakdown(claim, per_item) if per_item is not None else None
    claim.approved_amount = amount
    await outbox.to_tracker(session, _announce(claim, emp))
    await session.flush()
    return claim


async def reject(session: AsyncSession, claim: Claim, reason: str | None = None) -> Claim:
    emp = await _decided(session, claim, "rejected", reason)
    claim.approved_amount = Decimal(0)
    await outbox.to_tracker(session, _announce(claim, emp))
    await session.flush()
    return claim


async def ask(session: AsyncSession, claim: Claim, question: str) -> Claim:
    if claim.status not in ("awaiting_manager", "info_requested"):
        raise ClaimError(f"Claim #{claim.id} is already {claim.status.replace('_', ' ')}.")
    emp = await session.get(Employee, claim.employee_id)
    claim.status = "info_requested"
    claim.thread = [*claim.thread, {"from": "manager", "text": question.strip()[:1000]}]
    await outbox.to_tracker(session, {
        "event": "claim_question", "employee_ref": emp.ref, "claim_id": claim.id, "question": question.strip()[:1000],
        "text": f"💬 Your manager has a question about claim #{texts.no(claim)} ({claim.title}):\n“{question.strip()[:1000]}”\n\nReply and I'll pass it on.",
    })
    await session.flush()
    return claim


async def mark_paid(session: AsyncSession, claim: Claim) -> Claim:
    if claim.status != "approved" or claim.paid:
        raise ClaimError("Only an approved claim that hasn't been paid yet can be marked as paid.")
    claim.paid, claim.status = True, "paid"
    emp = await session.get(Employee, claim.employee_id)
    await outbox.to_tracker(session, _announce(claim, emp))
    await session.flush()
    return claim


async def pending_for(session: AsyncSession, company_id: int) -> list[tuple[Claim, Employee]]:
    rows = (await session.execute(
        select(Claim, Employee).join(Employee, Employee.id == Claim.employee_id)
        .where(Claim.company_id == company_id, Claim.status.in_(("awaiting_manager", "info_requested"))).order_by(Claim.id)
    )).all()
    return [(c, e) for c, e in rows]


async def approved_unpaid(session: AsyncSession, company_id: int) -> list[tuple[Claim, Employee]]:
    rows = (await session.execute(
        select(Claim, Employee).join(Employee, Employee.id == Claim.employee_id)
        .where(Claim.company_id == company_id, Claim.status == "approved").order_by(Claim.id)
    )).all()
    return [(c, e) for c, e in rows]
