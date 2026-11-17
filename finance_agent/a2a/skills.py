"""
What the Finance Agent can do for the expense tracker. Each skill takes a small JSON payload and returns a `SkillResult`;
none of this knows about the A2A protocol (executor.py turns results into task states), so it is easy to test.

    join_company   an employee joins with the company code
    ask_policy     "will I get a refund on this?"   (can ask follow-up questions: input_required)
    ask_manager    the policy doesn't say: pass the question to the manager
    submit_claim   check a claim against the policy and hand it to the manager (can ask for missing details)
    claim_reply    answer our questions (nights, receipt...) or the manager's question
    claim_status   where is my claim?
"""
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

from finance_agent import outbox
from finance_agent.claims import service as claims
from finance_agent.claims import texts
from finance_agent.db.models import Company, Employee, PolicyQuestion, QaSession
from finance_agent.db.session import session_scope
from finance_agent.formatting import money
from finance_agent.llm import LLM
from finance_agent.policy import qa, store


@dataclass
class Caller:
    ref: str  # the employee's id in the expense tracker
    name: str


@dataclass
class SkillResult:
    state: Literal["completed", "input_required", "failed"]
    text: str
    data: dict = field(default_factory=dict)


class SkillError(ValueError):
    """A problem to tell the caller in plain words (becomes a failed task)."""


async def _member(session, payload: dict, caller: Caller) -> tuple[Company, Employee]:
    code = str(payload.get("company") or "")
    company = await store.company_by_code(session, code) if code else None
    emp = await claims.employee(session, company.id, caller.ref) if company else None
    if company is None or emp is None:
        raise SkillError("You haven't joined a company yet. Join with the code your manager gave you.")
    return company, emp


def _needs_text(claim) -> str:
    questions = [q for v in texts.verdicts_of(claim) for q in v.questions]
    by_ref = {i.ref: i for i in texts.items_of(claim)}
    lines = [f"• {(by_ref[q.ref].description or by_ref[q.ref].category or f'item {q.ref}')}: {q.text}" for q in questions]
    return "To check your claim I need a little more:\n" + "\n".join(lines)


def _int_or_none(value) -> int | None:
    try:
        return int(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _claim_result(claim) -> SkillResult:
    data = {"claim_id": claim.id, "status": claim.status}
    if claim.status == "needs_info":
        qs = [q.model_dump() for v in texts.verdicts_of(claim) for q in v.questions]
        return SkillResult("input_required", _needs_text(claim), {**data, "questions": qs})
    lines = texts.lines(claim)
    text = (
        f"📨 Claim #{texts.no(claim)} ({claim.title}) is with your manager.\n\n" + "\n".join(lines) +
        f"\n\nThe policy suggests {money(claim.recommended)} of {money(texts.claimed_total(claim))}. Your manager makes the final call."
    )
    return SkillResult("completed", text, {**data, "recommended": str(claim.recommended), "claimed": str(texts.claimed_total(claim)), "lines": lines})


# ------------------------------------------------------------------ skills
async def join_company(llm: LLM, caller: Caller, payload: dict, task_id: str | None, text: str) -> SkillResult:
    code = str(payload.get("code") or "").strip()
    async with session_scope() as s:
        company = await store.company_by_code(s, code) if code else None
        if company is None:
            raise SkillError("I don't know that company code. Please check it with your manager.")
        _, created = await claims.join(s, company, caller.ref, caller.name)
        policy = await store.current(s, company.id)
        name, code = company.name, company.code
    note = "" if policy else " (Your manager hasn't published the policy yet.)"
    return SkillResult("completed", f"🏢 You're {'now part of' if created else 'already in'} {name}.{note}", {"company": code, "company_name": name, "has_policy": policy is not None})


async def ask_policy(llm: LLM, caller: Caller, payload: dict, task_id: str | None, text: str) -> SkillResult:
    question = str(payload.get("question") or text).strip()
    if not question:
        raise SkillError("What would you like to know?")
    async with session_scope() as s:
        company, emp = await _member(s, payload, caller)
        policy = await store.current(s, company.id)
        if policy is None:
            return SkillResult("completed", "Your company hasn't published a reimbursement policy yet.", {"outcome": "no_policy", "can_escalate": True})
        version, rules = policy
        session = await s.get(QaSession, task_id) if task_id else None
        if session is not None and session.employee_id != emp.id:
            session = None
        transcript = [*(session.transcript if session else []), f"Employee: {question}"]
        result = await qa.answer(llm, rules, version, transcript)
        if result.status == "need_info":
            if session is None and task_id:
                session = QaSession(task_id=task_id, employee_id=emp.id, transcript=[])
                s.add(session)
            if session is not None:
                session.transcript = [*transcript, f"Agent: {result.text}"]
            return SkillResult("input_required", result.text, {"outcome": "need_info"})
        if session is not None:
            await s.delete(session)
    return SkillResult("completed", result.text, {"outcome": result.status, "can_escalate": result.can_escalate})


async def ask_manager(llm: LLM, caller: Caller, payload: dict, task_id: str | None, text: str) -> SkillResult:
    question = str(payload.get("question") or text).strip()[:1000]
    if not question:
        raise SkillError("What would you like to ask your manager?")
    async with session_scope() as s:
        company, emp = await _member(s, payload, caller)
        q = PolicyQuestion(company_id=company.id, employee_id=emp.id, text=question)
        s.add(q)
        await s.flush()
        await outbox.to_manager(
            s, company.manager_telegram_id, f"❓ {emp.name} asks:\n“{question}”\n\nThe policy doesn't cover this. Your answer goes straight back to them.",
            [[{"label": "💬 Answer", "data": f"fq:answer:{q.id}"}]],
        )
        qid = q.id
    return SkillResult("completed", "📨 I've sent your question to your manager. I'll message you when they answer.", {"question_id": qid})


async def submit_claim(llm: LLM, caller: Caller, payload: dict, task_id: str | None, text: str) -> SkillResult:
    items = payload.get("items")
    if not isinstance(items, list):
        raise SkillError("A claim needs a list of items.")
    async with session_scope() as s:
        company, emp = await _member(s, payload, caller)
        claim = await claims.submit(s, llm, company, emp, str(payload.get("title") or "Expense claim"), items, _int_or_none(payload.get("external_no")))
        return _claim_result(claim)


async def claim_reply(llm: LLM, caller: Caller, payload: dict, task_id: str | None, text: str) -> SkillResult:
    try:
        claim_id = int(payload["claim_id"])
    except (KeyError, TypeError, ValueError):
        raise SkillError("Which claim is this about?") from None
    async with session_scope() as s:
        company, emp = await _member(s, payload, caller)
        claim = await claims.get(s, claim_id, company_id=company.id, employee_id=emp.id)
        if payload.get("answers"):
            claim = await claims.answer(s, llm, company, emp, claim, {int(k): v for k, v in payload["answers"].items()})
        elif str(payload.get("text") or "").strip():
            claim = await claims.employee_reply(s, claim, emp, company, str(payload["text"]))
            return SkillResult("completed", f"📨 Passed to your manager: claim #{texts.no(claim)} is back with them.", {"claim_id": claim.id, "status": claim.status})
        else:
            raise SkillError("Tell me the details, or your reply to the manager.")
        return _claim_result(claim)


async def claim_status(llm: LLM, caller: Caller, payload: dict, task_id: str | None, text: str) -> SkillResult:
    try:
        claim_id = int(payload["claim_id"])
    except (KeyError, TypeError, ValueError):
        raise SkillError("Which claim is this about?") from None
    async with session_scope() as s:
        company, emp = await _member(s, payload, caller)
        claim = await claims.get(s, claim_id, company_id=company.id, employee_id=emp.id)
        body = texts.employee_decision(claim)
        return SkillResult("completed", body, {"claim_id": claim.id, "status": claim.status, "approved_amount": str(claim.approved_amount) if claim.approved_amount is not None else None})


Handler = Callable[[LLM, Caller, dict, "str | None", str], Awaitable[SkillResult]]
SKILLS: dict[str, Handler] = {
    "join_company": join_company, "ask_policy": ask_policy, "ask_manager": ask_manager,
    "submit_claim": submit_claim, "claim_reply": claim_reply, "claim_status": claim_status,
}


async def run(llm: LLM, caller: Caller, payload: dict, task_id: str | None, text: str) -> SkillResult:
    """Run the skill the caller asked for. Plain-words problems become a failed result."""
    handler = SKILLS.get(str(payload.get("skill") or ""))
    if handler is None:
        return SkillResult("failed", f"I don't know that skill. I can do: {', '.join(SKILLS)}.")
    try:
        return await handler(llm, caller, payload, task_id, text)
    except (SkillError, claims.ClaimError, store.PolicyError) as e:
        return SkillResult("failed", f"❌ {e}")
