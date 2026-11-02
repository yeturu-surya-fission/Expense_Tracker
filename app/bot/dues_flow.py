"""The payment buttons: the person who owes taps 'I paid', the person who is owed taps 'Received' or 'Not yet'."""
from telegram import User as TgUser

from app.bot.base import BotBase

ACTIONS = ("paid", "got", "notyet")


class DueMixin(BotBase):
    async def _due(self, q, tg: TgUser, arg: str) -> None:
        raw_id, _, action = arg.partition(":")
        if not raw_id.isdigit() or action not in ACTIONS:
            return
        res = await self.call(self.private(tg), "debt_action", debt_id=int(raw_id), action=action)
        if not res.ok:
            await q.message.reply_text(res.message.replace("❌ ", "ℹ️ ", 1))
            return
        if action == "paid":
            try:
                await q.message.delete()  # the bill disappears from their chat
            except Exception:
                await q.edit_message_reply_markup(reply_markup=None)
            await q.message.chat.send_message(res.text)
        else:
            await q.edit_message_text(res.text)
