"""
Everything the manager can do, with no Telegram in it: each method takes the manager's Telegram id and returns a `Reply`
(text + optional buttons). bot.py only carries messages in and out, so all of this is testable without Telegram.

The manager's chat is a small state machine: after some buttons we wait for typed text (a reason, an amount, an answer).
That waiting state lives in the database (`manager_state`), so a restart doesn't lose it.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from finance_agent import outbox
from finance_agent.claims import service as claims
from finance_agent.claims import texts
from finance_agent.config import settings
from finance_agent.db.models import Company, Employee, ManagerState, PolicyQuestion
from finance_agent.db.session import session_scope
from finance_agent.formatting import money
from finance_agent.llm import LLM
from finance_agent.policy import parser, store
from finance_agent.policy.models import Rule

Buttons = list[list[dict]]

WELCOME_NEW = "👋 Welcome! I'm the Finance Agent: I hold your company's reimbursement policy, answer your employees' questions about it, and bring you their claims to approve.\n\nFirst, what is your company called?"
HOW_TO = (
    "How this works:\n"
    "• Tell me the policy once (paste it, or send a PDF). I'll show you how I understood it, and you confirm.\n"
    "• To change it later, just say what to change: “raise the hotel cap to 8000”.\n"
    "• Employees join with your company code, ask me what's covered, and send claims. I check them against the policy and bring each one to you.\n\n"
    "/policy see the rules · /claims claims waiting for you · /paid mark approved claims as paid · /help"
)


@dataclass
class Reply:
    text: str
    buttons: Buttons | None = None


def parse_amount(text: str) -> Decimal | None:
    t = text.strip().lower().replace(",", "").replace(settings.currency_symbol, "").replace("rs.", "").replace("rs", "").replace("inr", "").strip()
    mult = Decimal(1)
    if t.endswith("k"):
        t, mult = t[:-1], Decimal(1000)
    try:
        value = Decimal(t) * mult
    except InvalidOperation:
        return None
    return value if value >= 0 else None


class ManagerService:
    def __init__(self, llm: LLM):
        self.llm = llm

    # ------------------------------------------------------------------ state
    async def _mode(self, s: AsyncSession, tg: int) -> ManagerState | None:
        return await s.get(ManagerState, tg, with_for_update=True)

    async def _set(self, s: AsyncSession, tg: int, mode: str | None, payload: dict | None = None) -> None:
        row = await s.get(ManagerState, tg)
        if mode is None:
            if row is not None:
                await s.delete(row)
        elif row is None:
            s.add(ManagerState(telegram_id=tg, mode=mode, payload=payload or {}))
        else:
            row.mode, row.payload = mode, payload or {}

    def _allowed(self, tg: int) -> bool:
        ids = {int(x) for x in settings.allowed_manager_ids.replace(" ", "").split(",") if x}
        return not ids or tg in ids

    # ------------------------------------------------------------------ commands
    async def start(self, tg: int) -> Reply:
        if not self._allowed(tg):
            return Reply("Sorry, this bot is for company managers who have been set up. Ask your administrator.")
        async with session_scope() as s:
            company = await store.company_for_manager(s, tg)
            if company is None:
                await self._set(s, tg, "awaiting_company_name")
                return Reply(WELCOME_NEW)
            policy = await store.current(s, company.id)
            return Reply(f"👋 {company.name}. Employees join with the code {company.code}.\n\n" + (f"Policy v{policy[0]} is live.\n\n" if policy else "You haven't set the policy yet: paste it here or send a PDF.\n\n") + HOW_TO)

    async def help(self, tg: int) -> Reply:
        return Reply(HOW_TO)

    async def policy(self, tg: int) -> Reply:
        async with session_scope() as s:
            company = await store.company_for_manager(s, tg)
            if company is None:
                return Reply("Send /start to set up your company first.")
            current = await store.current(s, company.id)
            if current is None:
                return Reply("There's no policy yet. Paste your reimbursement policy here (or send it as a PDF) and I'll turn it into rules for you to confirm.")
            version, rules = current
            return Reply(f"📋 {company.name} · policy v{version}\n\n{parser.rules_text(rules)}\n\nTo change something, tell me: “raise the hotel cap to 8000”, “gym is not reimbursed”, “add parking up to 300 a day”.")

    async def pending(self, tg: int) -> Reply:
        async with session_scope() as s:
            company = await store.company_for_manager(s, tg)
            if company is None:
                return Reply("Send /start to set up your company first.")
            rows = await claims.pending_for(s, company.id)
            if not rows:
                return Reply("🎉 No claims are waiting for you.")
            claim, emp = rows[0]
            more = f"\n\n({len(rows) - 1} more waiting)" if len(rows) > 1 else ""
            return Reply(texts.manager_card(claim, emp.name) + more, texts.manager_buttons(claim, emp.name))

    async def paid_list(self, tg: int) -> Reply:
        async with session_scope() as s:
            company = await store.company_for_manager(s, tg)
            if company is None:
                return Reply("Send /start to set up your company first.")
            rows = await claims.approved_unpaid(s, company.id)
            if not rows:
                return Reply("Nothing is waiting to be paid.")
            return Reply(
                "💸 Approved, not yet paid:\n" + "\n".join(f"• #{c.id} {e.name}: {money(c.approved_amount or 0)} ({c.title})" for c, e in rows),
                [[{"label": f"✔ Paid #{c.id} · {e.name.split()[0]}", "data": f"fa:paid:{c.id}"}] for c, e in rows][:10],
            )

    # ------------------------------------------------------------------ typed text
    async def text(self, tg: int, text: str) -> Reply:
        text = text.strip()
        if not self._allowed(tg):
            return await self.start(tg)
        async with session_scope() as s:
            company = await store.company_for_manager(s, tg)
            state = await self._mode(s, tg)
            mode = state.mode if state else None
            payload = dict(state.payload) if state else {}
            if company is None:
                if mode != "awaiting_company_name":
                    return Reply("Send /start to set up your company first.")
                try:
                    company = await store.create_company(s, text, tg)
                except store.PolicyError as e:
                    return Reply(f"❌ {e}")
                await self._set(s, tg, "awaiting_policy")
                return Reply(
                    f"🏢 {company.name} is set up. Your company code is {company.code}: share it with your employees so they can join.\n\n"
                    "Now tell me the reimbursement policy. Paste the text (or send a PDF). For example:\n"
                    "“Hotels up to 6000 a night with a receipt. Meals up to 1000 a day on business trips. Gym memberships are not covered.”"
                )
            if mode in ("awaiting_reject_reason", "awaiting_amount", "awaiting_question"):
                return await self._claim_input(s, company, tg, mode, payload, text)
            if mode == "awaiting_answer":
                return await self._answer_question(s, company, tg, payload, text)
            return await self._policy_text(s, company, tg, mode, payload, text)

    async def _policy_text(self, s: AsyncSession, company: Company, tg: int, mode: str | None, payload: dict, text: str) -> Reply:
        """Text about the policy: the first time it is the policy itself, afterwards it is a change to make."""
        current = await store.current(s, company.id)
        base = [Rule.model_validate(r) for r in payload["rules"]] if mode == "draft_policy" else (current[1] if current else None)
        try:
            rules = await (parser.edit_policy(self.llm, base, text) if base else parser.parse_policy(self.llm, text))
        except parser.PolicyParseError as e:
            return Reply(f"❌ {e}")
        old = current[1] if current else []
        diff = parser.diff_rules(old, rules)
        if current and not diff:
            await self._set(s, tg, None)
            return Reply("That already matches the current policy. Nothing to change.")
        note = f"Edited: {text[:200]}" if current else "Initial policy"
        await self._set(s, tg, "draft_policy", {"rules": [r.model_dump(mode="json") for r in rules], "note": note})
        shown = "\n".join(diff) if current else parser.rules_text(rules)
        head = "Here's the change I'll make:" if current else "Here's how I understood your policy:"
        return Reply(
            f"{head}\n\n{shown}\n\nSave it? (You can also tell me what to fix first.)",
            [[{"label": "✅ Save", "data": "fp:save"}, {"label": "🗑 Discard", "data": "fp:discard"}]],
        )

    async def _claim_input(self, s: AsyncSession, company: Company, tg: int, mode: str, payload: dict, text: str) -> Reply:
        claim = await claims.get(s, int(payload["claim_id"]), company_id=company.id)
        try:
            if mode == "awaiting_reject_reason":
                await claims.reject(s, claim, text)
                done = f"❌ Claim #{claim.id} rejected. I've told the employee."
            elif mode == "awaiting_amount":
                amount = parse_amount(text)
                if amount is None:
                    return Reply("Please type just the amount, e.g. 12000.")
                await claims.approve(s, claim, amount)
                done = f"✅ Claim #{claim.id} approved for {money(amount)}. I've told the employee."
            else:
                await claims.ask(s, claim, text)
                done = f"💬 Sent to the employee. I'll show you their reply and the claim again."
        except claims.ClaimError as e:
            await self._set(s, tg, None)
            return Reply(f"❌ {e}")
        await self._set(s, tg, None)
        return Reply(done)

    async def _answer_question(self, s: AsyncSession, company: Company, tg: int, payload: dict, text: str) -> Reply:
        q = await s.get(PolicyQuestion, int(payload["question_id"]))
        if q is None or q.company_id != company.id:
            await self._set(s, tg, None)
            return Reply("❌ I can't find that question.")
        emp = await s.get(Employee, q.employee_id)
        q.answer = text[:1000]
        await outbox.to_tracker(s, {
            "event": "manager_answer", "employee_ref": emp.ref, "question_id": q.id, "question": q.text, "answer": q.answer,
            "text": f"💬 Your manager answered your question:\n“{q.text}”\n\n{q.answer}",
        })
        await self._set(s, tg, None)
        return Reply(
            f"✅ Sent to {emp.name}. Should this become part of the policy, so I can answer it myself next time?",
            [[{"label": "➕ Add to policy", "data": f"fq:save:{q.id}"}, {"label": "No thanks", "data": "fp:discard"}]],
        )

    # ------------------------------------------------------------------ buttons
    async def button(self, tg: int, data: str) -> Reply:
        parts = data.split(":")
        async with session_scope() as s:
            company = await store.company_for_manager(s, tg)
            if company is None:
                return Reply("Send /start to set up your company first.")
            try:
                if parts[0] == "fp":
                    return await self._policy_button(s, company, tg, parts[1])
                if parts[0] == "fa":
                    return await self._claim_button(s, company, tg, parts[1], int(parts[2]))
                if parts[0] == "fq":
                    return await self._question_button(s, company, tg, parts[1], int(parts[2]))
            except claims.ClaimError as e:
                return Reply(f"❌ {e}")
        return Reply("That button isn't valid any more.")

    async def _policy_button(self, s: AsyncSession, company: Company, tg: int, action: str) -> Reply:
        state = await self._mode(s, tg)
        if action == "discard":
            await self._set(s, tg, None)
            return Reply("👍 Discarded. The policy is unchanged.")
        if state is None or state.mode != "draft_policy":
            return Reply("There's nothing to save any more. Tell me the policy or the change again.")
        rules = [Rule.model_validate(r) for r in state.payload["rules"]]
        version = await store.save(s, company.id, rules, state.payload.get("note"))
        await self._set(s, tg, None)
        return Reply(f"✅ Saved as policy v{version}. Employees get answers from it right away; claims already submitted keep the version they were checked against.")

    async def _claim_button(self, s: AsyncSession, company: Company, tg: int, action: str, claim_id: int) -> Reply:
        claim = await claims.get(s, claim_id, company_id=company.id)
        emp = await s.get(Employee, claim.employee_id)
        if action == "approve":
            await claims.approve(s, claim)
            return Reply(f"✅ Claim #{claim.id} approved for {money(claim.approved_amount or 0)}. I've told {emp.name}.")
        if action == "paid":
            await claims.mark_paid(s, claim)
            return Reply(f"💸 Claim #{claim.id} marked as paid. I've told {emp.name}.")
        if claim.status not in ("awaiting_manager", "info_requested"):
            return Reply(f"Claim #{claim.id} is already {claim.status.replace('_', ' ')}.")
        prompts = {
            "reject": ("awaiting_reject_reason", f"Why is claim #{claim.id} rejected? Type the reason (it goes to {emp.name})."),
            "amount": ("awaiting_amount", f"How much do you approve for claim #{claim.id}? (They claimed {money(texts.claimed_total(claim))}.) Type the amount."),
            "ask": ("awaiting_question", f"What do you want to ask {emp.name} about claim #{claim.id}?"),
        }
        if action not in prompts:
            return Reply("That button isn't valid any more.")
        mode, prompt = prompts[action]
        await self._set(s, tg, mode, {"claim_id": claim.id})
        return Reply(prompt)

    async def _question_button(self, s: AsyncSession, company: Company, tg: int, action: str, question_id: int) -> Reply:
        q = await s.get(PolicyQuestion, question_id)
        if q is None or q.company_id != company.id:
            return Reply("❌ I can't find that question.")
        if action == "answer":
            await self._set(s, tg, "awaiting_answer", {"question_id": q.id})
            return Reply(f"Your answer to “{q.text}”? Type it and I'll send it to the employee.")
        if action == "save" and q.answer:
            return await self._policy_text(s, company, tg, None, {}, f"Add this to the policy. Question: {q.text} Manager's answer: {q.answer}")
        return Reply("That button isn't valid any more.")
