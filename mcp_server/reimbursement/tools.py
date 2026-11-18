"""
Reimbursement tools: what the chat agent (and the /reimburse screen) can do. Work expenses are kept apart from the person's
own spending. Anything about the company's policy or a claim's decision goes to the Finance Agent over A2A; we never
guess what the policy says.

A network call to the Finance Agent never happens while a database transaction is open.
"""
from decimal import Decimal

from pydantic import BaseModel, Field

from mcp_server.db.session import session_scope
from mcp_server.domain import friends
from mcp_server.domain.formatting import money
from mcp_server.reimbursement import a2a_client, domain
from mcp_server.reimbursement.domain import ReimbursementError
from mcp_server.tools._common import Opt, Out, ask_choice
from mcp_server.tools.registry import tool

DOWN = "The finance service isn't reachable right now. Nothing was lost; please try again in a moment."


class WorkItem(BaseModel):
    amount: float = Field(gt=0, description="Amount paid, as a plain number (3k = 3000).")
    category: str = Field(description="One lowercase word: hotel, cab, flight, train, meals, internet, training, tools, parking...")
    description: str = Field(description="What it was for, e.g. 'Pune hotel' or 'cab to client office'.")
    date: str | None = Field(default=None, description="YYYY-MM-DD, only if the user names a past day.")
    nights: int | None = Field(default=None, description="Hotel stays only: how many nights, if the user said.")
    days: int | None = Field(default=None, description="Meals / daily allowances: how many days, if the user said.")
    has_receipt: bool | None = Field(default=None, description="True/False only if the user said whether they have a receipt.")
    purpose: str | None = Field(default=None, description="Why: 'client meeting in Pune', if the user said.")


class ClaimAnswer(BaseModel):
    item_id: int = Field(description="The work expense number (#id) the answer is about.")
    nights: int | None = None
    days: int | None = None
    has_receipt: bool | None = None
    preapproved: bool | None = Field(default=None, description="Whether the manager approved it before the user spent it.")
    variant: str | None = Field(default=None, description="Which case applies, e.g. the city tier, when asked 'Which applies: ...' (use the exact option offered).")
    amount: float | None = Field(default=None, gt=0)


async def _who(s, uid: int) -> str:
    return friends.display(await friends.get_user(s, uid)) or "Employee"


async def _a2a(uid: int, name: str, skill: str, payload: dict, text: str, **kw) -> a2a_client.Reply:
    try:
        return await a2a_client.call(uid, name, skill, payload, text, **kw)
    except a2a_client.FinanceUnavailable as e:
        import logging
        logging.getLogger(__name__).warning("finance agent call %s failed: %s", skill, e)
        raise ReimbursementError(DOWN) from e


# ------------------------------------------------------------------ joining
@tool(contexts=("private",), write=True)
async def join_company(call, code: str) -> Out:
    """Join the user's company for work reimbursements, using the company code their manager gave them (looks like FIN-7K2QP)."""
    async with session_scope() as s:
        name = await _who(s, call.user_id)
    reply = await _a2a(call.user_id, name, "join_company", {"code": code.strip().upper()}, f"join {code}")
    if reply.state != "completed":
        return Out(reply.text or DOMAIN_UNKNOWN, {"joined": False})
    async with session_scope() as s:
        await domain.set_link(s, call.user_id, str(reply.data.get("company", code)).upper(), str(reply.data.get("company_name", "your company")))
    return Out(reply.text + "\nYou can now add work expenses, ask what the policy covers, and send claims.", {"joined": True, "company": reply.data.get("company")})


DOMAIN_UNKNOWN = "I couldn't join that company."


# ------------------------------------------------------------------ work expenses (kept apart from personal spending)
@tool(contexts=("private",), write=True)
async def add_work_expense(call, items: list[WorkItem]) -> Out:
    """Record expenses the user paid FOR WORK and wants the company to pay back (client cab, business hotel, work internet...).
    These are kept separate from the user's personal expenses and never counted in their summaries or budgets.
    Not for personal spending (use add_expenses) or shared bills (use split_expense)."""
    async with session_scope() as s:
        rows = [
            await domain.add_work_expense(
                s, call.user_id, i.amount, i.category, i.description, i.date, nights=i.nights, days=i.days, has_receipt=i.has_receipt, purpose=i.purpose,
            ) for i in items
        ]
        ids = [r.id for r in rows]
        linked = await domain.link_of(s, call.user_id)
    await _check_rows(call.user_id, ids)  # what does the policy need for these? (receipt, nights, days...) asked now, not when the claim is sent
    async with session_scope() as s:
        rows = await domain.expenses_by_id(s, call.user_id, ids)
        lines = [domain.line(r) for r in rows]
        needs = await domain.needs_text(s, rows)
        waiting = await domain.unclaimed(s, call.user_id)
    total = sum((w.amount for w in waiting), Decimal(0))
    text = "🧾 Saved as work expense:\n" + "\n".join(lines)
    if needs:
        text += "\n\nThe company policy needs this for your claim:\n" + needs
    else:
        text += f"\n\nWaiting to be claimed: {len(waiting)} item(s), {money(total)}. Say “send my claim” when you're ready."
    return Out(text, {"work_expense_ids": ids, "needs_input": bool(needs), "joined": linked is not None})


async def _check_rows(uid: int, expense_ids: list[int]) -> None:
    """Ask the finance agent what each of these work expenses needs, and remember it. If the person hasn't joined a company, or the
    finance agent is down, nothing is asked now: the claim check at the end still asks for whatever is missing."""
    async with session_scope() as s:
        link = await domain.link_of(s, uid)
        if link is None:
            return
        name, code = await _who(s, uid), link.company_code
        payloads = {r.id: domain.item_payload(r) for r in await domain.expenses_by_id(s, uid, expense_ids)}
    results = {}
    for eid, item in payloads.items():
        try:
            reply = await _a2a(uid, name, "check_expense", {"company": code, "item": item}, "check expense")
        except ReimbursementError:
            return
        if reply.state == "completed" and reply.data.get("checked"):
            results[eid] = reply.data
    async with session_scope() as s:
        for r in await domain.expenses_by_id(s, uid, list(results)):
            domain.apply_check(r, results[r.id])


@tool(contexts=("private",), write=True)
async def expense_details(
    call, expense_id: int | None = None, nights: int | None = None, days: int | None = None, has_receipt: bool | None = None,
    preapproved: bool | None = None, variant: str | None = None,
) -> Out:
    """Give the details the policy asked for about a work expense that hasn't been claimed yet ("2 nights", "1 day", "yes it was approved",
    "Tier-1", or "no receipt" -> has_receipt=false). Applies to EVERY waiting expense that asked that question. No expense_id needed.
    The receipt itself is a photo or PDF the user sends, never typed."""
    given = {k: v for k, v in dict(nights=nights, days=days, has_receipt=has_receipt, preapproved=preapproved, variant=variant).items() if v is not None}
    if not given:
        raise ReimbursementError("Tell me the detail (nights, days, approved or not, city tier, or “no receipt”).")
    async with session_scope() as s:
        await domain.require_link(s, call.user_id)
        rows = [r for r in await domain.unclaimed(s, call.user_id) if expense_id is None or r.id == expense_id]
        have = await domain.receipt_ids(s, [r.id for r in rows])
        changed: list[int] = []
        for r in rows:
            asked = {q["key"] for q in r.open_questions or []}
            for key in domain.ANSWER_FIELDS:
                if key in given and (key in asked or expense_id is not None):
                    setattr(r, key, given[key])
                    changed.append(r.id)
            if given.get("has_receipt") is False and domain.lacks_receipt(r, have):
                r.has_receipt = False  # they have none: the claim will say so and the manager decides
                changed.append(r.id)
        if not changed:
            needs = await domain.needs_text(s, await domain.unfinished(s, call.user_id))
            if given.get("has_receipt") and needs:
                raise ReimbursementError("Please send the receipt itself, as a photo or PDF in this chat.\n" + needs)
            raise ReimbursementError("None of your waiting expenses asked for that." + (f" They need:\n{needs}" if needs else ""))
        ids = sorted(set(changed))
    await _check_rows(call.user_id, ids)  # may have more to ask now (e.g. the city tier, then nothing)
    async with session_scope() as s:
        left = await domain.needs_text(s, await domain.unfinished(s, call.user_id))
    return Out("✅ Noted." + (f" Still needed:\n{left}" if left else " That's everything. Say “send my claim” when you're ready."), {"needs_input": bool(left)})


@tool(contexts=("private",), write=True)
async def attach_receipt(call, file_base64: str, filename: str = "receipt", content_type: str = "image/jpeg", expense_id: int | None = None, if_waiting: bool = False) -> Out:
    """Keep a receipt (photo or PDF the user just sent) with a work expense. Without expense_id it goes to the latest expense
    waiting for a receipt. if_waiting=true: do nothing (attached=false) when no expense is waiting for one."""
    import base64
    import binascii
    try:
        data = base64.b64decode(file_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ReimbursementError("I couldn't read that file.") from None
    async with session_scope() as s:
        exp = await domain.attach_receipt(s, call.user_id, expense_id, filename, content_type, data)
        if exp is None:
            if if_waiting:
                return Out("", {"attached": False})
            raise ReimbursementError("None of your work expenses is waiting for a receipt. Add the expense first, e.g. “work expense: flight 3000”.")
        left = await domain.needs_text(s, await domain.unfinished(s, call.user_id))
        saved = f"📎 Receipt saved with #{exp.id} {exp.description} ({money(exp.amount)}). It goes to your manager with the claim."
    return Out(saved + (f"\n\nStill needed:\n{left}" if left else "\n\nThat's everything. Say “send my claim” when you're ready."), {"attached": True, "expense_id": exp.id, "needs_input": bool(left)})


@tool(contexts=("private",), write=True)
async def move_to_reimbursement(call, expense_id: int | None = None) -> Out:
    """The user says an expense they already logged was for work ("that cab was for work"). Moves it out of their personal
    spending into work reimbursements. Omit expense_id for their most recent expense."""
    async with session_scope() as s:
        work = await domain.move_personal(s, call.user_id, expense_id)
        text = domain.line(work)
    return Out(f"🧾 Moved to work reimbursements (and out of your personal spending):\n{text}", {"work_expense_ids": [work.id]})


@tool(contexts=("private",))
async def list_work_expenses(call) -> Out:
    """The user's work expenses waiting to be claimed, and what they have already sent."""
    async with session_scope() as s:
        waiting = await domain.unclaimed(s, call.user_id)
        claims = await domain.claims_of(s, call.user_id, 5)
        text = "Nothing is waiting to be claimed." if not waiting else "Waiting to be claimed:\n" + "\n".join(domain.line(w) for w in waiting)
        if claims:
            text += "\n\nYour claims:\n" + "\n".join([await domain.claim_block(s, c) for c in claims])
        total = sum((w.amount for w in waiting), Decimal(0))
    return Out(text, {"waiting": len(waiting), "waiting_total": str(total)})


@tool(contexts=("private",), write=True)
async def remove_work_expense(call, expense_id: int) -> Out:
    """Delete a work expense that hasn't been sent in a claim yet."""
    async with session_scope() as s:
        exp = await domain.remove(s, call.user_id, expense_id)
        text = domain.line(exp)
    return Out(f"🗑 Removed: {text}")


# ------------------------------------------------------------------ asking about the policy
@tool(contexts=("private",), write=True)
async def ask_policy(call, question: str, new_topic: bool = False) -> Out:
    """Ask the company's reimbursement policy ("will I get a refund on this room?", "what's the meal limit?"). The company's
    finance agent answers from the real policy and may ask a follow-up (city, nights, amount): pass the user's reply here as
    `question` to continue. Set new_topic=true when the user starts a DIFFERENT question instead of answering the follow-up."""
    async with session_scope() as s:
        link = await domain.require_link(s, call.user_id)
        name = await _who(s, call.user_id)
        chat = None if new_topic else await domain.open_chat(s, call.user_id)
        task_id, context_id = (chat.task_id, chat.context_id) if chat else (None, None)
        code = link.company_code
    payload = {"company": code, "question": question}
    try:
        reply = await _a2a(call.user_id, name, "ask_policy", payload, question, task_id=task_id, context_id=context_id)
    except ReimbursementError:
        if task_id is None:
            raise
        reply = await _a2a(call.user_id, name, "ask_policy", payload, question)  # the old conversation is gone: start again
    async with session_scope() as s:
        await domain.set_chat(s, call.user_id, reply.task_id if reply.state == "input_required" else None, reply.context_id)
    text = reply.text
    if reply.data.get("can_escalate"):
        text += "\n\nWant me to ask your manager about it? Just say so."
    return Out(text, {"outcome": reply.data.get("outcome"), "can_escalate": bool(reply.data.get("can_escalate")), "waiting_for_answer": reply.state == "input_required"})


@tool(contexts=("private",), write=True)
async def ask_manager(call, question: str) -> Out:
    """Send a question to the user's manager when the policy doesn't answer it and the user wants the manager's word.
    The answer arrives later as a message."""
    async with session_scope() as s:
        link = await domain.require_link(s, call.user_id)
        name = await _who(s, call.user_id)
    reply = await _a2a(call.user_id, name, "ask_manager", {"company": link.company_code, "question": question}, question)
    return Out(reply.text)


# ------------------------------------------------------------------ claims
@tool(contexts=("private",), write=True)
async def submit_claim(call, title: str | None = None, expense_ids: list[int] | None = None) -> Out:
    """Send the user's waiting work expenses (or just the listed ones) to the company as a reimbursement claim. The finance
    agent checks them against the policy, may ask for missing details (nights, receipt), then the manager decides.
    `title` = what the claim is for, e.g. 'Pune trip'."""
    async with session_scope() as s:
        link = await domain.require_link(s, call.user_id)
        try:
            items = await domain.pick_for_claim(s, call.user_id, expense_ids)
        except ReimbursementError:
            waiting = None if expense_ids else await domain.waiting_claim(s, call.user_id)
            if waiting is None:
                raise
            # nothing new to send because the expenses are ALREADY in a claim that is waiting for details: say what it needs
            return Out(
                f"ℹ️ Your expenses are already in claim #{waiting.id}. It is waiting for details from you:\n{await domain.questions_text(s, waiting)}\n\n"
                "Just tell me in your own words, e.g. “1 day for all”.", {"claim_id": waiting.id, "status": waiting.status, "needs_info": True},
            )
        have = await domain.receipt_ids(s, [i.id for i in items])
        blocked = [i for i in items if i.open_questions or domain.lacks_receipt(i, have)]
        if blocked:  # the policy's questions are asked when an expense is added: don't send a claim that is still missing them
            raise ReimbursementError("Before I can send your claim, the policy needs:\n" + await domain.needs_text(s, blocked))
        lines = [domain.line(i) for i in items]
        total = sum((i.amount for i in items), Decimal(0))
    if ask_choice(
        call, "confirm", f"Send {len(items)} item(s) totalling {money(total)} to your manager for approval?\n" + "\n".join(lines),
        [Opt("yes", "✅ Yes, send", ("yes", "send", "ok")), Opt("cancel", "✖ Not now", ("no",))],
    ) is None:
        return Out("👍 Okay, I haven't sent anything.")
    async with session_scope() as s:
        name = await _who(s, call.user_id)
        items = await domain.pick_for_claim(s, call.user_id, [i.id for i in items])
        claim = await domain.create_claim(s, call.user_id, title or "Expense claim", items)
        claim_id, payload_items = claim.id, domain.claim_items_payload(items)
        files = await domain.receipts_payload(s, [i.id for i in items])  # the receipts go to the manager with the claim
    try:
        reply = await _a2a(
            call.user_id, name, "submit_claim",
            {"company": link.company_code, "title": title or "Expense claim", "items": payload_items, "external_no": claim_id, "receipts": files}, f"claim: {title or 'expenses'}",
        )
    except ReimbursementError:
        async with session_scope() as s:
            await domain.release_items(s, await domain.claim_of(s, call.user_id, claim_id))
        raise
    async with session_scope() as s:
        claim = await domain.claim_of(s, call.user_id, claim_id)
        if reply.state == "failed" or "claim_id" not in reply.data:
            await domain.release_items(s, claim)
            return Out(reply.text or "The claim could not be sent.", {"claim_id": None})
        claim.remote_claim_id = int(reply.data["claim_id"])
        claim.status = str(reply.data.get("status", "awaiting_manager"))
        claim.questions = reply.data.get("questions") or None
    hint = (
        f"\n\nThis is claim #{claim_id}. Just tell me the details in your own words (for example “1 day for all”) and I'll pass them on."
        if reply.state == "input_required" else ""
    )
    return Out(reply.text + hint, {"claim_id": claim_id, "status": claim.status, "needs_info": reply.state == "input_required"})


@tool(contexts=("private",), write=True)
async def claim_answer(
    call, claim_id: int | None = None, days: int | None = None, nights: int | None = None, has_receipt: bool | None = None,
    preapproved: bool | None = None, variant: str | None = None, details: list[ClaimAnswer] | None = None, text: str | None = None,
) -> Out:
    """Give the finance agent what it asked for on a claim, in the user's words. Use it whenever the user answers a claim question
    like "how many days / nights?", "do you have the receipt?", "was it approved before?", "which city tier?".
    Set days / nights / has_receipt / preapproved / variant from what they said: it is applied to EVERY open question of that kind
    ("1 day for all" -> days=1). No claim_id needed: it uses the claim that is waiting for details. Use `details` only when
    different items get different answers. Use `text` (instead) to reply to a question the user's MANAGER asked."""
    given = {k: v for k, v in dict(days=days, nights=nights, has_receipt=has_receipt, preapproved=preapproved, variant=variant).items() if v is not None}
    if not given and not details and not (text or "").strip():
        raise ReimbursementError("Tell me the details (days, nights, receipt...) or your reply to the manager.")
    async with session_scope() as s:
        link = await domain.require_link(s, call.user_id)
        name = await _who(s, call.user_id)
        wants_text = bool((text or "").strip()) and not given and not details
        claim = await domain.claim_of(s, call.user_id, claim_id) if claim_id else await domain.waiting_claim(s, call.user_id, "info_requested" if wants_text else "needs_info")
        if claim is None:
            raise ReimbursementError("You don't have a claim waiting for that. Say “my claims” to see where they stand.")
        if claim.remote_claim_id is None:
            raise ReimbursementError("That claim hasn't reached the finance agent.")
        remote, claim_id, questions = claim.remote_claim_id, claim.id, list(claim.questions or [])
        open_text = await domain.questions_text(s, claim)
    payload: dict = {"company": link.company_code, "claim_id": remote}
    if wants_text:
        payload["text"] = text.strip()
    else:
        answers: dict[str, dict] = {}
        for q in questions:  # what was said applies to every question of that kind
            if q.get("key") in given:
                answers.setdefault(str(int(float(q["ref"]))), {})[q["key"]] = given[q["key"]]
        for d in details or []:
            answers.setdefault(str(d.item_id), {}).update({k: (str(Decimal(str(v))) if k == "amount" else v) for k, v in d.model_dump(exclude={"item_id"}).items() if v is not None})
        if not answers:
            raise ReimbursementError("That doesn't answer what the claim is waiting for." + (f" It needs:\n{open_text}" if open_text else ""))
        payload["answers"] = answers
    reply = await _a2a(call.user_id, name, "claim_reply", payload, text or "details")
    async with session_scope() as s:
        claim = await domain.claim_of(s, call.user_id, claim_id)
        if reply.state != "failed":
            claim.status = str(reply.data.get("status", claim.status))
            claim.questions = reply.data.get("questions") or None
    return Out(reply.text, {"claim_id": claim_id, "status": claim.status})


@tool(contexts=("private",))
async def my_claims(call, claim_id: int | None = None) -> Out:
    """The user's reimbursement claims and where each stands. With claim_id, refreshes that claim from the finance agent."""
    if claim_id is not None:
        async with session_scope() as s:
            link = await domain.require_link(s, call.user_id)
            name = await _who(s, call.user_id)
            claim = await domain.claim_of(s, call.user_id, claim_id)
            remote = claim.remote_claim_id
        if remote is not None:
            reply = await _a2a(call.user_id, name, "claim_status", {"company": link.company_code, "claim_id": remote}, "status")
            async with session_scope() as s:
                claim = await domain.claim_of(s, call.user_id, claim_id)
                if reply.state == "completed" and reply.data.get("status"):
                    claim.status = str(reply.data["status"])
                    if reply.data.get("approved_amount") is not None:
                        claim.approved_amount = Decimal(str(reply.data["approved_amount"]))
    async with session_scope() as s:
        claims = await domain.claims_of(s, call.user_id)
        text = "You haven't sent any claims yet." if not claims else "Your claims:\n" + "\n".join([await domain.claim_block(s, c) for c in claims])
    return Out(text, {"claims": len(claims)})


# ------------------------------------------------------------------ the /reimburse screen
@tool(contexts=("private",))
async def reimburse_view(call) -> Out:
    """The reimbursement home screen (no AI): company, what's waiting, claims."""
    async with session_scope() as s:
        link = await domain.link_of(s, call.user_id)
        waiting = await domain.unclaimed(s, call.user_id)
        claims = await domain.claims_of(s, call.user_id, 5)
        total = sum((w.amount for w in waiting), Decimal(0))
        head = f"🧾 Work reimbursements · {link.company_name} ({link.company_code})" if link else "🧾 Work reimbursements\nYou haven't joined a company yet: ask your manager for the company code and say “join company FIN-XXXXX”."
        parts = [head, "These are kept apart from your personal spending."]
        parts.append(f"Waiting to be claimed: {len(waiting)} item(s), {money(total)}" + ("\n" + "\n".join(domain.line(w) for w in waiting[:8]) if waiting else ""))
        if claims:
            parts.append("Claims:\n" + "\n".join([await domain.claim_block(s, c) for c in claims]))
        parts.append("Try: “work expense: cab 450 to client office”, “will I get a refund on a hotel room?”, “send my claim”.")
    return Out("\n\n".join(parts), {"linked": link is not None, "waiting": len(waiting), "claims": len(claims)})


@tool(contexts=("private",))
async def work_report(call) -> Out:
    """Everything the user logged as a WORK expense, with the claim each one is in and what was approved for it (for the /workpdf report)."""
    async with session_scope() as s:
        link = await domain.link_of(s, call.user_id)
        name = await _who(s, call.user_id)
        rows = await domain.report_rows(s, call.user_id)
    total = sum((Decimal(r["amount"]) for r in rows), Decimal(0))
    return Out(
        f"📄 Work expense report: {len(rows)} expense(s), {money(total)}.",
        {"name": name, "company": link.company_name if link else None, "rows": rows, "total": str(total)},
    )
