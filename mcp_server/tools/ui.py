"""
Tools for the buttons, menus and commands of a chat client: no AI involved, the client calls them directly.
(The agent tools in the other modules are what the LLM chooses from; these are the 'instant' paths.)
"""
import base64
import binascii
import asyncio
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import select

from mcp_server.db.models import Debt, Expense
from mcp_server.db.session import session_scope
from mcp_server.domain import budgets, debts, expenses, friends, groups, notices, statements, views
from mcp_server.domain.errors import UserError
from mcp_server.domain.formatting import friendly_expense, money
from mcp_server.domain.periods import now_local
from mcp_server.tools._common import Out
from mcp_server.tools.registry import tool

VIEWS = ("summary", "recent", "budgets", "friends", "dues", "daily", "trend", "invite")


@tool(contexts=("private",))
async def ui_view(call, view: str, arg: str | None = None) -> Out:
    """A screen of the chat client. view: summary (arg = period or 'YYYY-MM'), recent, budgets, friends, dues,
    daily (arg = 'YYYY-MM'), trend (arg = number of months), invite (the user's friend-invite link)."""
    if view not in VIEWS:
        raise UserError(f"Unknown view '{view}'.")
    data: dict = {}
    async with session_scope() as s:
        uid = call.user_id
        if view == "summary":
            text = await views.summary_text(s, uid, arg or "this_month")
        elif view == "recent":
            text = await views.recent_text(s, uid, limit=10)
        elif view == "budgets":
            text = await views.budgets_text(s, uid, arg)
        elif view == "friends":
            text = await views.friends_text(s, uid)
        elif view == "dues":
            notes = await notices.net_leftovers(s, uid)
            text = "\n".join([*notes, await views.dues_text(s, uid)])
            data = {"dues": await views.dues_data(s, uid)}
        elif view == "daily":
            text = await views.daily_text(s, uid, arg)
        elif view == "trend":
            text = await views.trend_text(s, uid, int(arg or 6))
        else:
            user = await friends.get_user(s, uid)
            link = notices.start_link(f"inv_{user.telegram_id}")
            text = (f"🔗 Send this link to a friend. When they open it, you become friends automatically:\n{link}"
                    if link else "The bot link isn't ready yet. Try again in a moment.")
    return Out(text, {"view": view, "arg": arg or "", **data})


@tool(contexts=("private",))
async def last_expense(call) -> Out:
    """The user's most recent personal expense (for an 'undo?' confirmation)."""
    async with session_scope() as s:
        last = (await s.execute(
            select(Expense).where(Expense.user_id == call.user_id, Expense.voided_at.is_(None), Expense.source == "personal").order_by(Expense.id.desc()).limit(1)
        )).scalar_one_or_none()
        if last is None:
            return Out("Nothing to undo yet.", {"expense_id": None})
        return Out(f"Undo this?\n\n{friendly_expense(last)}", {"expense_id": last.id})


@tool(contexts=("private",), write=True)
async def undo_expenses(call, expense_ids: list[int]) -> Out:
    """Undo several of the user's own expenses at once (the Undo button under a reply)."""
    lines = []
    async with session_scope() as s:
        for i in expense_ids[:20]:
            lines.append(f"↩️ Undone: {friendly_expense(await expenses.void_expense(s, call.user_id, i))}")
    return Out("\n".join(lines), {"expense_ids": expense_ids})


@tool(contexts=("private",), write=True)
async def debt_action(call, debt_id: int, action: str) -> Out:
    """The payment buttons. action: 'paid' (the person who owes taps 'I paid'), 'got' (the person owed taps 'Received'),
    'notyet' (the person owed taps 'Not yet'). Messages to the other person are queued by the server."""
    uid = call.user_id
    if action == "paid":
        async with session_scope() as s:
            debt, created = await debts.claim(s, debt_id, uid)  # logs their share as THEIR expense
            alerts = await budgets.alerts_for(s, uid, created)
            creditor = await friends.get_user(s, debt.creditor_id)
            who = await friends.nickname_of(s, uid, creditor)
            amount = debt.amount
            logged = sum((e.amount for e in created), Decimal(0))
        async with session_scope() as s:  # tell the person who is owed (after the data is saved)
            await notices.notify_claim(s, await s.get(Debt, debt_id))
        text = "\n".join([f"✅ Noted: you paid {who} {money(amount)}. I've asked them to confirm.", f"💾 {money(logged)} added to your expenses.", *alerts])
        return Out(text, {"action": action, "debt_id": debt_id})
    if action == "got":
        async with session_scope() as s:
            debt = await debts.confirm(s, debt_id, uid)
            who = await friends.nickname_of(s, uid, await friends.get_user(s, debt.debtor_id))
            amount, desc = debt.amount, debt.description
        async with session_scope() as s:
            await notices.notify_confirmed(s, await s.get(Debt, debt_id))
        return Out(f"✅ Confirmed: {who} paid you {money(amount)} for {desc}. All settled!", {"action": action, "debt_id": debt_id})
    if action == "notyet":
        async with session_scope() as s:
            debt = await debts.reject(s, debt_id, uid)
            who = await friends.nickname_of(s, uid, await friends.get_user(s, debt.debtor_id))
            amount, desc = debt.amount, debt.description
        async with session_scope() as s:
            await notices.notify_rejected(s, await s.get(Debt, debt_id))
        return Out(f"👍 Okay. I told {who} it's still unpaid ({money(amount)} for {desc}).", {"action": action, "debt_id": debt_id})
    raise UserError(f"Unknown action '{action}'.")


@tool(contexts=("private",), write=True)
async def join_group_via_start(call, group_id: int) -> Out:
    """The person pressed the 'Start the bot' button a group showed them: make sure they are a member of that group."""
    async with session_scope() as s:
        group = await groups.get_group(s, group_id)
        await groups.join_group(s, group_id, call.user_id)
        name = group.name
    return Out(f"✅ You're in {name}. Any bill from the group will arrive here.", {"group_id": group_id})


@tool(contexts=("private", "group"))
async def agent_context(call) -> Out:
    """What an AI agent should know before it chooses tools: the user's budget categories and friends (private chat),
    or the group's name and members (group chat). The user's own data only."""
    async with session_scope() as s:
        speaker = friends.display(await friends.get_user(s, call.user_id))
        if call.chat_type == "group":
            group = await groups.group_for_chat(s, call.group_chat_id) if call.group_chat_id is not None else None
            members = [friends.display(u) for u in await groups.members(s, group.id)] if group else []
            return Out("", {"speaker": speaker, "group": group.name if group else None, "members": members})
        cats = await budgets.budget_categories(s, call.user_id)
        names = [f.nickname for f, _ in await friends.list_friends(s, call.user_id)]
    return Out("", {"speaker": speaker, "categories": cats, "friends": names})


# ------------------------------------------------------------------ bank statements
class StatementRowIn(BaseModel):
    date: str
    description: str
    amount: float
    type: str = Field(description="debit or credit")
    category: str = "other"
    balance_after: float | None = None


@tool(contexts=("private",))
async def statement_read_pdf(call, pdf_base64: str, password: str | None = None) -> Out:
    """Read the text out of a bank-statement PDF (sent base64-encoded). Returns the pages' text, the allowed categories
    and the user's budget categories, so the caller can turn the text into rows for statement_preview."""
    try:
        data = base64.b64decode(pdf_base64, validate=True)
    except (binascii.Error, ValueError):
        raise UserError("That file could not be read.") from None
    try:
        pages = await asyncio.to_thread(statements.extract_pdf_pages, data, password or None)
    except statements.PasswordRequired:
        return Out("🔒 This PDF is password protected. Send it again with the password as the caption, e.g.  password 12345", {"password": "required"})
    except statements.WrongPassword:
        return Out("🔒 That password didn't work. Send the PDF again with the right password as the caption.", {"password": "wrong"})
    async with session_scope() as s:
        cats = await budgets.budget_categories(s, call.user_id)
    return Out("", {"pages": pages, "categories": statements.CATEGORIES, "budget_categories": cats, "today": now_local().strftime("%Y-%m-%d")})


@tool(contexts=("private",), write=True)
async def statement_preview(call, filename: str | None, rows: list[StatementRowIn]) -> Out:
    """Check the rows read from a statement (dates, amounts, duplicates) and save them as a pending import the user
    must confirm. Nothing is added to their expenses yet."""
    async with session_scope() as s:
        items, meta = await statements.build_plan(s, call.user_id, [r.model_dump() for r in rows])
        imp = await statements.save_import(s, call.user_id, filename, items, meta)
        counts = statements.counts(imp.items)
        text, import_id = statements.preview_text(imp), imp.id
    return Out(text, {"import_id": import_id, "new": counts["new"], "maybe": counts["maybe"]})


@tool(contexts=("private",), write=True)
async def statement_apply(call, import_id: int, include_maybes: bool = False) -> Out:
    """The user confirmed: add the previewed expenses (include_maybes also adds the possible duplicates)."""
    async with session_scope() as s:
        res = await statements.apply_import(s, call.user_id, import_id, include_maybes=include_maybes)
    return Out(res.text, {"import_id": import_id})


@tool(contexts=("private",), write=True)
async def statement_cancel(call, import_id: int) -> str:
    """The user cancelled a pending statement import."""
    async with session_scope() as s:
        await statements.cancel_import(s, call.user_id, import_id)
    return "👍 Cancelled - nothing was imported."


@tool(contexts=("private",), write=True)
async def statement_undo(call, import_id: int) -> str:
    """Undo a finished statement import (all its expenses are removed)."""
    async with session_scope() as s:
        return await statements.undo_import(s, call.user_id, import_id)


@tool(contexts=("private",), write=True)
async def settle_with(call, person_id: int, action: str) -> Out:
    """The buttons of the dues screen, one per person instead of one per bill.
    action: 'pay' (I paid this person everything I owe them), 'received' (this person paid me, I confirm it),
    'notyet' (this person says they paid but I haven't received it). person_id is the id shown with the dues."""
    uid = call.user_id
    if action not in ("pay", "received", "notyet"):
        raise UserError(f"Unknown action '{action}'.")
    status, debtor_id, creditor_id = ("pending", uid, person_id) if action == "pay" else ("claimed", person_id, uid)
    async with session_scope() as s:
        rows = list((await s.execute(
            select(Debt).where(Debt.status == status, Debt.debtor_id == debtor_id, Debt.creditor_id == creditor_id).order_by(Debt.id)
        )).scalars().all())
        if not rows:
            raise UserError("There is nothing left to do with that person: it was already handled.")
        ids = [d.id for d in rows]
        other = await friends.get_user(s, person_id)
        who = await friends.nickname_of(s, uid, other)
        total = sum((d.amount for d in rows), Decimal(0))
        logged, alerts = Decimal(0), []
        created = []
        for debt_id in ids:
            if action == "pay":
                _, exps = await debts.claim(s, debt_id, uid)
                created += exps
            elif action == "received":
                await debts.confirm(s, debt_id, uid)
            else:
                await debts.reject(s, debt_id, uid)
        if action == "pay":
            alerts = await budgets.alerts_for(s, uid, created)
            logged = sum((e.amount for e in created), Decimal(0))
    async with session_scope() as s:  # tell the other person (after the data is saved)
        fresh = list((await s.execute(select(Debt).where(Debt.id.in_(ids)).order_by(Debt.id))).scalars().all())
        if action == "pay":
            await notices.notify_claim_bulk(s, fresh)
        elif action == "received":
            await notices.notify_confirmed_bulk(s, fresh)
        else:
            await notices.notify_rejected_bulk(s, fresh)
    if action == "pay":
        text = "\n".join([f"✅ Noted: you paid {who} {money(total)}. I've asked them to confirm.", f"💾 {money(logged)} added to your expenses.", *alerts])
    elif action == "received":
        text = f"✅ Confirmed: {who} paid you {money(total)}. All settled!"
    else:
        text = f"👍 Okay. I told {who} it's still unpaid ({money(total)})."
    return Out(text, {"action": action, "person_id": person_id, "debt_ids": ids})
