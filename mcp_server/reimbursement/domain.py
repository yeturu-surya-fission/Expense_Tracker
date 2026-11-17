"""
Work reimbursements: the person's work expenses, the claims they send, and what comes back. No A2A and no Telegram here:
tools.py talks to the Finance Agent and calls these functions to keep our own records.
"""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.db.models import Expense
from mcp_server.domain.errors import UserError
from mcp_server.domain.expenses import normalize_category, to_decimal
from mcp_server.domain.formatting import money
from mcp_server.domain.periods import now_local, parse_when
from mcp_server.reimbursement.models import CompanyLink, PolicyChat, WorkClaim, WorkExpense

OPEN_STATUSES = ("needs_info", "awaiting_manager", "info_requested", "approved")


class ReimbursementError(UserError):
    pass


# ------------------------------------------------------------------ company
async def link_of(session: AsyncSession, user_id: int) -> CompanyLink | None:
    return (await session.execute(select(CompanyLink).where(CompanyLink.user_id == user_id))).scalar_one_or_none()


async def require_link(session: AsyncSession, user_id: int) -> CompanyLink:
    link = await link_of(session, user_id)
    if link is None:
        raise ReimbursementError("You haven't joined a company yet. Ask your manager for the company code, then say: join company <code>.")
    return link


async def set_link(session: AsyncSession, user_id: int, code: str, name: str) -> CompanyLink:
    link = await link_of(session, user_id)
    if link is None:
        link = CompanyLink(user_id=user_id, company_code=code, company_name=name)
        session.add(link)
    else:
        link.company_code, link.company_name = code, name
    await session.flush()
    return link


# ------------------------------------------------------------------ work expenses
async def add_work_expense(
    session: AsyncSession, user_id: int, amount, category: str | None, description: str | None, date: str | None = None, *,
    nights: int | None = None, days: int | None = None, has_receipt: bool | None = None, purpose: str | None = None,
) -> WorkExpense:
    amount = to_decimal(amount)
    if amount <= 0:
        raise ReimbursementError("The amount must be more than zero.")
    when = parse_when(date) or now_local()  # parse_when gives None for today / unusable dates
    exp = WorkExpense(
        user_id=user_id, amount=amount, category=normalize_category(category), description=(description or "").strip()[:200] or normalize_category(category),
        spent_on=when.date(),
        nights=nights, days=days, has_receipt=has_receipt, purpose=(purpose or "").strip()[:200] or None,
    )
    session.add(exp)
    await session.flush()
    return exp


async def move_personal(session: AsyncSession, user_id: int, expense_id: int | None) -> WorkExpense:
    """'That cab was for work': the personal expense is taken out of the person's own spending and becomes a work expense."""
    q = select(Expense).where(Expense.user_id == user_id, Expense.voided_at.is_(None), Expense.source == "personal")
    q = q.where(Expense.id == expense_id) if expense_id else q.order_by(Expense.id.desc()).limit(1)
    personal = (await session.execute(q.with_for_update())).scalar_one_or_none()
    if personal is None:
        raise ReimbursementError("I can't find that expense to move.")
    personal.voided_at = datetime.now(timezone.utc)
    work = WorkExpense(user_id=user_id, amount=personal.amount, category=personal.category, description=personal.description, spent_on=personal.occurred_at.date())
    session.add(work)
    await session.flush()
    return work


async def unclaimed(session: AsyncSession, user_id: int) -> list[WorkExpense]:
    return list((await session.execute(
        select(WorkExpense).where(WorkExpense.user_id == user_id, WorkExpense.claim_id.is_(None), WorkExpense.voided_at.is_(None)).order_by(WorkExpense.id)
    )).scalars().all())


async def recent(session: AsyncSession, user_id: int, limit: int = 20) -> list[WorkExpense]:
    return list((await session.execute(
        select(WorkExpense).where(WorkExpense.user_id == user_id, WorkExpense.voided_at.is_(None)).order_by(WorkExpense.id.desc()).limit(limit)
    )).scalars().all())


async def remove(session: AsyncSession, user_id: int, expense_id: int) -> WorkExpense:
    exp = (await session.execute(select(WorkExpense).where(WorkExpense.id == expense_id, WorkExpense.user_id == user_id, WorkExpense.voided_at.is_(None)).with_for_update())).scalar_one_or_none()
    if exp is None:
        raise ReimbursementError("I can't find that work expense.")
    if exp.claim_id is not None:
        raise ReimbursementError("That one is already in a claim, so it can't be removed.")
    exp.voided_at = datetime.now(timezone.utc)
    await session.flush()
    return exp


def line(e: WorkExpense) -> str:
    extra = ", ".join(x for x in (f"{e.nights} night{'s' if e.nights != 1 else ''}" if e.nights else "", f"{e.days} day{'s' if e.days != 1 else ''}" if e.days else "",
                                  "receipt ✓" if e.has_receipt else ("no receipt" if e.has_receipt is False else "")) if x)
    return f"#{e.id} {e.description}: {money(e.amount)}" + (f" ({extra})" if extra else "")


# ------------------------------------------------------------------ claims
async def pick_for_claim(session: AsyncSession, user_id: int, expense_ids: list[int] | None) -> list[WorkExpense]:
    rows = await unclaimed(session, user_id)
    if expense_ids:
        rows = [r for r in rows if r.id in set(expense_ids)]
        missing = set(expense_ids) - {r.id for r in rows}
        if missing:
            raise ReimbursementError(f"I can't use work expense(s) {', '.join(f'#{i}' for i in sorted(missing))}: they don't exist or are already in a claim.")
    if not rows:
        raise ReimbursementError("You have no work expenses waiting to be claimed. Add some first, e.g. “work expense: cab 450 to the client office”.")
    return rows


async def create_claim(session: AsyncSession, user_id: int, title: str, items: list[WorkExpense]) -> WorkClaim:
    claim = WorkClaim(user_id=user_id, title=(title or "Expense claim").strip()[:160], status="sending", total=sum((i.amount for i in items), Decimal(0)))
    session.add(claim)
    await session.flush()
    for item in items:
        item.claim_id = claim.id
    await session.flush()
    return claim


def claim_items_payload(items: list[WorkExpense]) -> list[dict]:
    return [
        {
            "ref": i.id, "category": i.category, "description": i.description, "amount": str(i.amount), "date": i.spent_on.isoformat(),
            "nights": i.nights, "days": i.days, "has_receipt": i.has_receipt, "purpose": i.purpose,
        }
        for i in items
    ]


async def release_items(session: AsyncSession, claim: WorkClaim) -> None:
    """The claim never reached the Finance Agent: its items go back to the waiting list."""
    for item in (await session.execute(select(WorkExpense).where(WorkExpense.claim_id == claim.id))).scalars().all():
        item.claim_id = None
    claim.status = "failed"
    await session.flush()


async def claim_of(session: AsyncSession, user_id: int, claim_id: int) -> WorkClaim:
    claim = (await session.execute(select(WorkClaim).where(WorkClaim.id == claim_id, WorkClaim.user_id == user_id).with_for_update())).scalar_one_or_none()
    if claim is None:
        raise ReimbursementError("I can't find that claim.")
    return claim


async def claims_of(session: AsyncSession, user_id: int, limit: int = 10) -> list[WorkClaim]:
    return list((await session.execute(select(WorkClaim).where(WorkClaim.user_id == user_id, WorkClaim.status != "failed").order_by(WorkClaim.id.desc()).limit(limit))).scalars().all())


# ------------------------------------------------------------------ policy conversation that is waiting for an answer
async def open_chat(session: AsyncSession, user_id: int) -> PolicyChat | None:
    return (await session.execute(select(PolicyChat).where(PolicyChat.user_id == user_id))).scalar_one_or_none()


async def set_chat(session: AsyncSession, user_id: int, task_id: str | None, context_id: str = "") -> None:
    chat = await open_chat(session, user_id)
    if task_id is None:
        if chat is not None:
            await session.delete(chat)
    elif chat is None:
        session.add(PolicyChat(user_id=user_id, task_id=task_id, context_id=context_id))
    else:
        chat.task_id, chat.context_id = task_id, context_id
    await session.flush()


STATUS_WORDS = {
    "sending": "sending", "needs_info": "needs more details from you", "awaiting_manager": "with your manager", "info_requested": "your manager asked you something",
    "approved": "approved", "rejected": "rejected", "paid": "paid", "failed": "not sent",
}


def claim_line(c: WorkClaim) -> str:
    base = f"#{c.id} {c.title}: {money(c.total)} · {STATUS_WORDS.get(c.status, c.status)}"
    if c.status in ("approved", "paid") and c.approved_amount is not None:
        base += f" ({money(c.approved_amount)})"
    return base


async def questions_text(session: AsyncSession, claim: WorkClaim) -> str:
    """What a claim still needs, one line per question, with the name of the expense it is about."""
    names = {w.id: w.description for w in (await session.execute(select(WorkExpense).where(WorkExpense.claim_id == claim.id))).scalars().all()}
    lines = []
    for q in claim.questions or []:
        ref = int(float(q.get("ref", 0)))
        lines.append(f"• {names.get(ref, f'item #{ref}')}: {q.get('text', '')}")
    return "\n".join(lines)


async def claim_block(session: AsyncSession, c: WorkClaim) -> str:
    """A claim's line, plus what to do next when it is waiting for the person."""
    text = claim_line(c)
    if c.status == "needs_info" and c.questions:
        text += "\n" + await questions_text(session, c) + "\n   ➜ Just tell me in your own words, e.g. “1 day for all”."
    elif c.status == "info_requested":
        text += (f"\n   Your manager asks: “{c.note}”" if c.note else "\n   Your manager asked you something") + "\n   ➜ Reply and I'll pass it on."
    return text


async def waiting_claim(session: AsyncSession, user_id: int, status: str = "needs_info") -> WorkClaim | None:
    """The person's latest claim that is waiting for them (details we asked for, or an answer to the manager)."""
    return (await session.execute(
        select(WorkClaim).where(WorkClaim.user_id == user_id, WorkClaim.status == status).order_by(WorkClaim.id.desc()).limit(1).with_for_update()
    )).scalar_one_or_none()


async def report_rows(session: AsyncSession, user_id: int) -> list[dict]:
    """One row per work expense, oldest first: the details we hold, the claim it went in, and what was approved for it."""
    expenses = list((await session.execute(
        select(WorkExpense).where(WorkExpense.user_id == user_id, WorkExpense.voided_at.is_(None)).order_by(WorkExpense.spent_on, WorkExpense.id)
    )).scalars().all())
    claims = {c.id: c for c in (await session.execute(select(WorkClaim).where(WorkClaim.user_id == user_id))).scalars().all()}
    rows = []
    for e in expenses:
        c = claims.get(e.claim_id) if e.claim_id else None
        decided = next((d for d in (c.decided_items or []) if int(d.get("ref", 0)) == e.id), None) if c else None
        rows.append({
            "id": e.id, "date": e.spent_on.isoformat(), "description": e.description, "category": e.category, "amount": str(e.amount),
            "nights": e.nights, "days": e.days, "has_receipt": e.has_receipt, "purpose": e.purpose,
            "claim_no": c.id if c else None, "claim_title": c.title if c else None, "status": STATUS_WORDS.get(c.status, c.status) if c else "not claimed yet",
            "approved": decided["approved"] if decided else None, "reason": (decided or {}).get("reason") or None,
            "claim_approved": str(c.approved_amount) if c is not None and c.approved_amount is not None else None,
        })
    return rows
