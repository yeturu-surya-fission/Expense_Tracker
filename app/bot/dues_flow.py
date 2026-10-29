"""The payment buttons: the person who owes taps 'I paid', the person who is owed taps 'Received' or 'Not yet'."""
from decimal import Decimal

from telegram import User as TgUser

from app.bot.base import BotBase
from app.core import budgets, debts, friends, notices
from app.core.errors import UserError
from app.core.formatting import money
from app.db.models import Debt
from app.db.session import session_scope


class DueMixin(BotBase):
    async def _due(self, q, tg: TgUser, arg: str) -> None:
        raw_id, _, action = arg.partition(":")
        if not raw_id.isdigit() or action not in ("paid", "got", "notyet"):
            return
        debt_id, uid = int(raw_id), await self._uid(tg, started=True)
        try:
            if action == "paid":
                await self._paid(q, uid, debt_id)
            elif action == "got":
                await self._received(q, uid, debt_id)
            else:
                await self._not_yet(q, uid, debt_id)
        except UserError as e:
            await q.message.reply_text(f"ℹ️ {e}")

    async def _paid(self, q, uid: int, debt_id: int) -> None:
        async with session_scope() as s:
            debt, expenses = await debts.claim(s, debt_id, uid)  # logs their share as THEIR expense
            alerts = await budgets.alerts_for(s, uid, expenses)
            creditor = await friends.get_user(s, debt.creditor_id)
            who = await friends.nickname_of(s, uid, creditor)
            amount = debt.amount
            logged = sum((e.amount for e in expenses), Decimal(0))
        async with session_scope() as s:  # tell the person who is owed (after the data is saved)
            await notices.notify_claim(s, await s.get(Debt, debt_id))
        try:
            await q.message.delete()  # the bill disappears from their chat
        except Exception:
            await q.edit_message_reply_markup(reply_markup=None)
        lines = [f"✅ Noted: you paid {who} {money(amount)}. I've asked them to confirm.", f"💾 {money(logged)} added to your expenses."]
        await q.message.chat.send_message("\n".join([*lines, *alerts]))

    async def _received(self, q, uid: int, debt_id: int) -> None:
        async with session_scope() as s:
            debt = await debts.confirm(s, debt_id, uid)
            debtor = await friends.get_user(s, debt.debtor_id)
            who = await friends.nickname_of(s, uid, debtor)
        async with session_scope() as s:
            await notices.notify_confirmed(s, await s.get(Debt, debt_id))
        await q.edit_message_text(f"✅ Confirmed: {who} paid you {money(debt.amount)} for {debt.description}. All settled!")

    async def _not_yet(self, q, uid: int, debt_id: int) -> None:
        async with session_scope() as s:
            debt = await debts.reject(s, debt_id, uid)
            debtor = await friends.get_user(s, debt.debtor_id)
            who = await friends.nickname_of(s, uid, debtor)
        async with session_scope() as s:
            await notices.notify_rejected(s, await s.get(Debt, debt_id))
        await q.edit_message_text(f"👍 Okay. I told {who} it's still unpaid ({money(debt.amount)} for {debt.description}).")
