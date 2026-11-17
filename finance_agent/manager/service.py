"""
Everything the manager can do, with no Telegram in it: each method takes the manager's Telegram id and returns a `Reply`
(text + optional buttons). bot.py only carries messages in and out, so all of this is testable without Telegram.

The manager's chat is a small state machine: after some commands or buttons we wait for typed text (the policy, a reason, an
amount, an answer). That waiting state lives in the database (`manager_state`), so a restart doesn't lose it.

Reading a policy can take a minute, so the language model is never called while a database transaction is open: we read what
we need, let go of the database, ask the model, then write the result.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finance_agent import outbox
from finance_agent.claims import service as claims
from finance_agent.claims import texts
from finance_agent.config import settings
from finance_agent.db.models import Company, Employee, ManagerState, PolicyQuestion
from finance_agent.db.session import session_scope
from finance_agent.formatting import money
from finance_agent.llm import LLM
from finance_agent.manager import decision
from finance_agent.policy import parser, store
from finance_agent.policy.models import Rule

Buttons = list[list[dict]]
Progress = Callable[[str], Awaitable[None]] | None

# Telegram cuts a long message at 4096 characters and delivers the rest as a separate message: a paste this long is probably only the first part
SPLIT_LIMIT = 4000

WELCOME_NEW = "👋 Welcome! I'm the Finance Agent: I hold your company's reimbursement policy, answer your employees' questions about it, and bring you their claims to approve.\n\nFirst, what is your company called?"
HOW_TO = (
    "How this works:\n"
    "• Give me the policy once: /setpolicy, then paste it (or send a PDF). I show you how I understood it; you confirm.\n"
    "• To change it later: /editpolicy, or just say what to change: “raise the hotel cap to 8000”.\n"
    "• Employees join with your company code, ask me what's covered, and send claims. I check each claim against the policy and bring it to you.\n\n"
    "Commands:\n"
    "/setpolicy give (or replace) the policy\n"
    "/editpolicy change part of it\n"
    "/policy see the current rules · /history earlier versions\n"
    "/claims claims waiting for you · /paid mark approved claims as paid\n"
    "/employees who has joined · /code your company code\n"
    "/done finish a long policy sent in several messages · /retry try again · /cancel stop what I'm waiting for"
)
SEND_POLICY = (
    "📥 Send the policy now. Paste the text, or send it as a PDF or text file.\n"
    "If your text is long and Telegram splits it into several messages, send all the parts and then type /done.\n"
    "(/cancel to stop.)"
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

    async def _company(self, s: AsyncSession, tg: int) -> Company | None:
        return await store.company_for_manager(s, tg)

    NO_COMPANY = "Send /start to set up your company first."
    CHAT_HINT = (
        "I only change the policy when you ask with a command, so nothing was changed.\n"
        "• /editpolicy: change part of it\n• /setpolicy: give a new one\n• /policy: see it\n• /claims: claims waiting for you\n• /help: all commands"
    )
    NO_POLICY_HINT = "There's no policy yet. Type /setpolicy and then send it."

    # ------------------------------------------------------------------ simple commands
    async def start(self, tg: int) -> Reply:
        if not self._allowed(tg):
            return Reply("Sorry, this bot is for company managers who have been set up. Ask your administrator.")
        async with session_scope() as s:
            company = await self._company(s, tg)
            if company is None:
                await self._set(s, tg, "awaiting_company_name")
                return Reply(WELCOME_NEW)
            policy = await store.current(s, company.id)
            return Reply(
                f"👋 {company.name}. Employees join with the code {company.code}.\n\n"
                + (f"Policy v{policy[0]} is live.\n\n" if policy else "You haven't set the policy yet: type /setpolicy.\n\n") + HOW_TO
            )

    async def help(self, tg: int) -> Reply:
        return Reply(HOW_TO)

    async def code(self, tg: int) -> Reply:
        async with session_scope() as s:
            company = await self._company(s, tg)
            return Reply(f"🏢 {company.name}\nCompany code: {company.code}\nShare it so your employees can join (they say “join company {company.code}” or /join {company.code}).") if company else Reply(self.NO_COMPANY)

    async def policy(self, tg: int) -> Reply:
        async with session_scope() as s:
            company = await self._company(s, tg)
            if company is None:
                return Reply(self.NO_COMPANY)
            current = await store.current(s, company.id)
            if current is None:
                return Reply("There's no policy yet. Type /setpolicy and paste your reimbursement policy (or send a PDF).")
            version, rules = current
            return Reply(f"📋 {company.name} · policy v{version}\n\n{parser.rules_text(rules)}\n\n/editpolicy to change something, /setpolicy to replace it all, /history for earlier versions.")

    async def history(self, tg: int) -> Reply:
        async with session_scope() as s:
            company = await self._company(s, tg)
            if company is None:
                return Reply(self.NO_COMPANY)
            versions = await store.history(s, company.id, 10)
            if not versions:
                return Reply("No policy has been saved yet.")
            return Reply("🕘 Policy versions (newest first):\n" + "\n".join(f"• v{v.version} · {v.created_at:%d %b %Y %H:%M} · {v.note or '—'}" for v in versions))

    async def employees(self, tg: int) -> Reply:
        async with session_scope() as s:
            company = await self._company(s, tg)
            if company is None:
                return Reply(self.NO_COMPANY)
            rows = (await s.execute(select(Employee).where(Employee.company_id == company.id).order_by(Employee.id))).scalars().all()
            if not rows:
                return Reply(f"Nobody has joined yet. Share your company code: {company.code}")
            return Reply(f"👥 {company.name} · {len(rows)} employee(s):\n" + "\n".join(f"• {e.name} (joined {e.joined_at:%d %b %Y})" for e in rows))

    async def pending(self, tg: int) -> Reply:
        async with session_scope() as s:
            company = await self._company(s, tg)
            if company is None:
                return Reply(self.NO_COMPANY)
            rows = await claims.pending_for(s, company.id)
            if not rows:
                return Reply("🎉 No claims are waiting for you.")
            claim, emp = rows[0]
            more = f"\n\n({len(rows) - 1} more waiting)" if len(rows) > 1 else ""
            return Reply(texts.manager_card(claim, emp.name) + more, texts.manager_buttons(claim, emp.name))

    async def paid_list(self, tg: int) -> Reply:
        async with session_scope() as s:
            company = await self._company(s, tg)
            if company is None:
                return Reply(self.NO_COMPANY)
            rows = await claims.approved_unpaid(s, company.id)
            if not rows:
                return Reply("Nothing is waiting to be paid.")
            return Reply(
                "💸 Approved, not yet paid:\n" + "\n".join(f"• #{c.id} {e.name}: {money(c.approved_amount or 0)} ({c.title})" for c, e in rows),
                [[{"label": f"✔ Paid #{c.id} · {e.name.split()[0]}", "data": f"fa:paid:{c.id}"}] for c, e in rows][:10],
            )

    async def cancel(self, tg: int) -> Reply:
        async with session_scope() as s:
            state = await self._mode(s, tg)
            if state is None:
                return Reply("Nothing to cancel.")
            await self._set(s, tg, None)
            return Reply("👍 Cancelled. Nothing was changed.")

    # ------------------------------------------------------------------ giving and changing the policy
    async def set_policy(self, tg: int, text: str = "", progress: Progress = None) -> Reply:
        """/setpolicy: the policy follows (in this message, or the next ones)."""
        async with session_scope() as s:
            company = await self._company(s, tg)
            if company is None:
                return Reply(self.NO_COMPANY)
            has_policy = await store.current(s, company.id) is not None
            if not text.strip():
                await self._set(s, tg, "awaiting_policy", {"replace": has_policy})
                return Reply(SEND_POLICY + ("\n\nThis will replace your current policy (you'll see what changes before it is saved)." if has_policy else ""))
            if len(text) >= SPLIT_LIMIT:
                await self._set(s, tg, "collecting_policy", {"parts": [text], "replace": has_policy})
                return Reply(self._collecting_reply(1, text))
        return await self._run_policy(company.id, tg, kind="parse", text=text, replace=has_policy, progress=progress)

    async def edit_policy(self, tg: int, text: str = "", progress: Progress = None) -> Reply:
        """/editpolicy: change part of the current policy."""
        async with session_scope() as s:
            company = await self._company(s, tg)
            if company is None:
                return Reply(self.NO_COMPANY)
            if await store.current(s, company.id) is None:
                return Reply("There's no policy to edit yet. Type /setpolicy first.")
            if not text.strip():
                await self._set(s, tg, "awaiting_edit")
                return Reply("✏️ What should change? For example:\n• raise the hotel cap to 8000\n• gym is not reimbursed\n• add parking up to 300 a day\n(/cancel to stop.)")
        return await self._run_policy(company.id, tg, kind="edit", text=text, replace=False, progress=progress)

    async def done(self, tg: int, progress: Progress = None) -> Reply:
        """/done: everything the manager sent in several messages is one policy."""
        async with session_scope() as s:
            company = await self._company(s, tg)
            state = await self._mode(s, tg)
            if company is None:
                return Reply(self.NO_COMPANY)
            if state is None or state.mode != "collecting_policy":
                return Reply("There's nothing to finish. Type /setpolicy to give me the policy.")
            text, replace = "\n".join(state.payload["parts"]), bool(state.payload.get("replace"))
            await self._set(s, tg, None)
        return await self._run_policy(company.id, tg, kind="parse", text=text, replace=replace, progress=progress)

    async def retry(self, tg: int, progress: Progress = None) -> Reply:
        """/retry (or the Try again button): run the last policy text or change again, after a failure."""
        async with session_scope() as s:
            company = await self._company(s, tg)
            state = await self._mode(s, tg)
            if company is None:
                return Reply(self.NO_COMPANY)
            if state is None or state.mode != "retry_policy":
                return Reply("There's nothing to retry.")
            p = dict(state.payload)
        return await self._run_policy(
            company.id, tg, kind=p["kind"], text=p["text"], replace=bool(p.get("replace")),
            base=[Rule.model_validate(r) for r in p["base"]] if p.get("base") else None, progress=progress,
        )

    async def document(self, tg: int, text: str, progress: Progress = None) -> Reply:
        """A PDF or text file: always one complete policy (no waiting for more parts)."""
        async with session_scope() as s:
            company = await self._company(s, tg)
            if company is None:
                return Reply(self.NO_COMPANY)
            state = await self._mode(s, tg)
            has_policy = await store.current(s, company.id) is not None
            if state is not None and state.mode in ("collecting_policy", "awaiting_policy", "awaiting_edit", "retry_policy"):
                await self._set(s, tg, None)
        return await self._run_policy(company.id, tg, kind="parse", text=text, replace=has_policy, progress=progress)

    @staticmethod
    def _collecting_reply(n: int, text: str) -> str:
        return f"📥 Got part {n} ({len(text):,} characters). Send the next part, or type /done when that's everything. (/cancel to stop.)"

    async def _run_policy(
        self, company_id: int, tg: int, *, kind: str, text: str, replace: bool, base: list[Rule] | None = None, progress: Progress = None,
    ) -> Reply:
        """Ask the model to read the policy (kind='parse') or apply a change (kind='edit'), then show the manager what it understood."""
        async with session_scope() as s:
            current = await store.current(s, company_id)
        old = current[1] if current else []
        if progress is not None:
            await progress("⏳ Reading your policy… this can take up to a minute." if kind == "parse" else "⏳ Working out the change…")
        try:  # (no database transaction is open while we wait for the model)
            rules = await (parser.edit_policy(self.llm, base or old, text) if kind == "edit" else parser.parse_policy(self.llm, text))
        except parser.PolicyParseError as e:
            if not e.retryable:
                return Reply(f"❌ {e}")
            async with session_scope() as s:
                await self._set(s, tg, "retry_policy", {
                    "text": text[:30000], "kind": kind, "replace": replace, "base": [r.model_dump(mode="json") for r in base] if base else None,
                })
            return Reply(f"❌ {e}", [[{"label": "🔁 Try again", "data": "fp:retry"}, {"label": "✖ Cancel", "data": "fp:discard"}]])
        diff = parser.diff_rules(old, rules)
        async with session_scope() as s:
            if current and not diff:
                await self._set(s, tg, None)
                return Reply("That already matches the current policy. Nothing to change.")
            note = f"Edited: {text[:200]}" if kind == "edit" else ("Replaced the policy" if current else "Initial policy")
            await self._set(s, tg, "draft_policy", {"rules": [r.model_dump(mode="json") for r in rules], "note": note})
        if kind == "edit" or current:
            shown, head = "\n".join(diff), ("Here's the change I'll make:" if kind == "edit" else "Here's what would change in your policy:")
        else:
            shown, head = parser.rules_text(rules), "Here's how I understood your policy:"
        return Reply(
            f"{head}\n\n{shown}\n\nSave it? (You can also tell me what to fix first, or /cancel.)",
            [[{"label": "✅ Save", "data": "fp:save"}, {"label": "🗑 Discard", "data": "fp:discard"}]],
        )

    # ------------------------------------------------------------------ typed text
    async def text(self, tg: int, text: str, progress: Progress = None) -> Reply:
        text = text.strip()
        if not self._allowed(tg):
            return await self.start(tg)
        async with session_scope() as s:
            company = await self._company(s, tg)
            state = await self._mode(s, tg)
            mode = state.mode if state else None
            payload = dict(state.payload) if state else {}
            if company is None:
                if mode != "awaiting_company_name":
                    return Reply(self.NO_COMPANY)
                try:
                    company = await store.create_company(s, text, tg)
                except store.PolicyError as e:
                    return Reply(f"❌ {e}")
                await self._set(s, tg, "awaiting_policy", {"replace": False})
                return Reply(
                    f"🏢 {company.name} is set up. Your company code is {company.code}: share it with your employees so they can join.\n\n"
                    f"Now give me the reimbursement policy.\n{SEND_POLICY}"
                )
            if mode in ("awaiting_reject_reason", "awaiting_question"):
                return await self._claim_input(s, company, tg, mode, payload, text)
            if mode == "awaiting_items":
                return await self._item_amount(s, company, tg, payload, parse_amount(text))
            if mode == "awaiting_answer":
                return await self._answer_question(s, company, tg, payload, text)
            if mode == "collecting_policy":
                parts = [*payload["parts"], text]
                await self._set(s, tg, "collecting_policy", {**payload, "parts": parts})
                return Reply(self._collecting_reply(len(parts), text))
            current = await store.current(s, company.id)
            if mode not in ("draft_policy", "awaiting_edit", "awaiting_policy", "retry_policy"):
                # The manager is only chatting. The policy changes ONLY through /setpolicy or /editpolicy (and then Save), never from a stray message.
                return Reply(self.CHAT_HINT if current else self.NO_POLICY_HINT)
            if mode == "draft_policy":  # the manager is correcting the draft they were just shown
                job = dict(kind="edit", base=[Rule.model_validate(r) for r in payload["rules"]], replace=False)
            elif mode == "awaiting_edit":
                job = dict(kind="edit", base=None, replace=False)
            else:  # the policy itself (first time, /setpolicy, or after a failed try)
                replace = bool(payload.get("replace", current is not None))
                if len(text) >= SPLIT_LIMIT:
                    await self._set(s, tg, "collecting_policy", {"parts": [text], "replace": replace})
                    return Reply(self._collecting_reply(1, text))
                job = dict(kind="parse", base=None, replace=replace)
            if mode in ("awaiting_policy", "awaiting_edit"):
                await self._set(s, tg, None)
        return await self._run_policy(company.id, tg, text=text, progress=progress, **job)

    async def _claim_input(self, s: AsyncSession, company: Company, tg: int, mode: str, payload: dict, text: str) -> Reply:
        claim = await claims.get(s, int(payload["claim_id"]), company_id=company.id)
        try:
            if mode == "awaiting_reject_reason":
                await claims.reject(s, claim, text)
                done = f"❌ Claim #{claim.id} rejected. I've told the employee."
            else:
                await claims.ask(s, claim, text)
                done = "💬 Sent to the employee. I'll show you their reply and the claim again."
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
    async def button(self, tg: int, data: str, progress: Progress = None) -> Reply:
        parts = data.split(":")
        if parts[0] == "fp" and parts[1] == "retry":
            return await self.retry(tg, progress)
        job = None
        async with session_scope() as s:
            company = await self._company(s, tg)
            if company is None:
                return Reply(self.NO_COMPANY)
            try:
                if parts[0] == "fp":
                    return await self._policy_button(s, company, tg, parts[1])
                if parts[0] == "fa":
                    return await self._claim_button(s, company, tg, parts[1], int(parts[2]))
                if parts[0] == "fi":
                    return await self._items_button(s, company, tg, int(parts[1]), parts[2])
                if parts[0] == "fq":
                    reply, job = await self._question_button(s, company, tg, parts[1], int(parts[2]))
                    if job is None:
                        return reply
            except claims.ClaimError as e:
                return Reply(f"❌ {e}")
        if job is not None:  # "add this answer to the policy": the model reads it, outside the transaction
            return await self._run_policy(company.id, tg, kind="edit", text=job, replace=False, progress=progress)
        return Reply("That button isn't valid any more.")

    async def _policy_button(self, s: AsyncSession, company: Company, tg: int, action: str) -> Reply:
        state = await self._mode(s, tg)
        if action == "discard":
            await self._set(s, tg, None)
            return Reply("👍 Discarded. The policy is unchanged.")
        if action != "save" or state is None or state.mode != "draft_policy":
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
        if action == "amount":  # own figures: go through the expenses one by one
            return await self._start_items(s, tg, claim)
        prompts = {
            "reject": ("awaiting_reject_reason", f"Why is claim #{claim.id} rejected? Type the reason (it goes to {emp.name})."),
            "ask": ("awaiting_question", f"What do you want to ask {emp.name} about claim #{claim.id}?"),
        }
        if action not in prompts:
            return Reply("That button isn't valid any more.")
        mode, prompt = prompts[action]
        await self._set(s, tg, mode, {"claim_id": claim.id})
        return Reply(prompt)

    # ------------------------------------------------------------------ own amounts, expense by expense
    async def _start_items(self, s: AsyncSession, tg: int, claim) -> Reply:
        await self._set(s, tg, "awaiting_items", {"claim_id": claim.id, "idx": 0, "amounts": {}})
        return Reply(*decision.item_screen(claim, 0, {}))

    async def _item_amount(self, s: AsyncSession, company: Company, tg: int, payload: dict, amount: Decimal | None) -> Reply:
        """The manager's figure (tapped or typed) for the current expense; then the next expense, or the summary."""
        claim = await claims.get(s, int(payload["claim_id"]), company_id=company.id)
        items = texts.items_of(claim)
        idx, amounts = int(payload["idx"]), dict(payload["amounts"])
        if idx >= len(items):
            return Reply(*decision.summary_screen(claim, amounts))
        if amount is None:
            return Reply("Please tap a button, or type just the amount for this expense, e.g. 450.")
        claimed = items[idx].amount or Decimal(0)
        if amount > claimed:
            return Reply(f"They only claimed {money(claimed)} for this expense. Type {money(claimed)} or less.")
        amounts[str(items[idx].ref)] = str(amount)
        idx += 1
        await self._set(s, tg, "awaiting_items", {"claim_id": claim.id, "idx": idx, "amounts": amounts})
        return Reply(*(decision.item_screen(claim, idx, amounts) if idx < len(items) else decision.summary_screen(claim, amounts)))

    async def _items_button(self, s: AsyncSession, company: Company, tg: int, claim_id: int, choice: str) -> Reply:
        state = await self._mode(s, tg)
        if state is None or state.mode != "awaiting_items" or int(state.payload["claim_id"]) != claim_id:
            return Reply("That screen isn't open any more. Tap “Different amount” on the claim again.")
        claim = await claims.get(s, claim_id, company_id=company.id)
        if claim.status not in ("awaiting_manager", "info_requested"):
            await self._set(s, tg, None)
            return Reply(f"Claim #{claim.id} is already {claim.status.replace('_', ' ')}.")
        payload, items = dict(state.payload), texts.items_of(claim)
        if choice == "redo":
            return await self._start_items(s, tg, claim)
        if choice == "ok":
            if int(payload["idx"]) < len(items):
                return Reply("Please go through all the expenses first.")
            emp = await s.get(Employee, claim.employee_id)
            await claims.approve(s, claim, per_item={int(ref): Decimal(v) for ref, v in payload["amounts"].items()})
            await self._set(s, tg, None)
            return Reply(f"✅ Claim #{claim.id} approved for {money(claim.approved_amount or 0)}. I've told {emp.name} which expenses were paid and which were not.")
        idx = int(payload["idx"])
        if idx >= len(items):
            return Reply(*decision.summary_screen(claim, payload["amounts"]))
        item = items[idx]
        amount = {"p": claims.policy_amounts(claim).get(item.ref, Decimal(0)), "f": item.amount or Decimal(0), "z": Decimal(0)}.get(choice)
        if amount is None:
            return Reply("That button isn't valid any more.")
        return await self._item_amount(s, company, tg, payload, amount)

    async def _question_button(self, s: AsyncSession, company: Company, tg: int, action: str, question_id: int) -> tuple[Reply, str | None]:
        q = await s.get(PolicyQuestion, question_id)
        if q is None or q.company_id != company.id:
            return Reply("❌ I can't find that question."), None
        if action == "answer":
            await self._set(s, tg, "awaiting_answer", {"question_id": q.id})
            return Reply(f"Your answer to “{q.text}”? Type it and I'll send it to the employee."), None
        if action == "save" and q.answer:
            return Reply(""), f"Add this to the policy. Question: {q.text} Manager's answer: {q.answer}"
        return Reply("That button isn't valid any more."), None
