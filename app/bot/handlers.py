"""BotHandlers = every handler mixed together, plus the one place that routes inline-button taps."""
import logging

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from app.bot import keyboards as kb
from app.bot.base import keep_typing
from app.bot.dues_flow import DueMixin
from app.bot.group import GroupMixin
from app.bot.private import PrivateMixin
from app.bot.statement_flow import StatementMixin

log = logging.getLogger(__name__)


class BotHandlers(PrivateMixin, GroupMixin, DueMixin, StatementMixin):
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()  # stops the button's loading spinner immediately
        tg, msg = q.from_user, q.message
        if not await self.allowed(self.ident_for(tg, msg.chat)):
            return
        kind, _, arg = q.data.partition(":")

        if kind == "hitl":  # answer to a question the bot asked: hitl:<option>[:<owner telegram id>]
            option, _, owner = arg.partition(":")
            if owner and owner != str(tg.id):
                await q.answer("This question is for someone else.", show_alert=True)
                return
            await q.edit_message_reply_markup(reply_markup=None)  # one-shot buttons
            async with keep_typing(context.bot, msg.chat_id):
                reply = await self._ask(tg, msg.chat, option, bot=context.bot, from_button=True)
            in_group = msg.chat.type != "private"
            await self._send(msg, reply, owner_telegram_id=tg.id if in_group else None)
            if in_group:
                await self.announce_unreachable(msg, reply)

        elif kind == "due":
            await self._due(q, tg, arg)

        elif kind in ("payto", "recvfrom", "notyetfrom") and arg.isdigit():
            action = {"payto": "pay", "recvfrom": "received", "notyetfrom": "notyet"}[kind]
            await self._settle_with(q, tg, action, int(arg))

        elif kind == "gsettle":
            await self._settle_button(q, tg, context.bot)

        elif kind == "undo":
            await self._undo_expenses(q, tg, [int(i) for i in arg.split(",") if i])

        elif kind == "undosplit":
            await self._undo_split(q, tg, int(arg))

        elif kind == "keep":
            await q.edit_message_text("👍 Kept.")

        elif kind == "imp":
            await self._import_action(q, tg, arg)

        elif kind == "impundo":
            await self._import_undo(q, tg, int(arg))

        elif kind in ("sum", "day", "trend"):
            await self._view(q, tg, kind, arg)

    # ------------------------------------------------------------ undo buttons
    async def _undo_expenses(self, q, tg, ids: list[int]) -> None:
        res = await self.call(self.private(tg), "undo_expenses", expense_ids=ids)
        if not res.ok:
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text("That was already undone.")
            return
        await q.edit_message_text(res.text)

    async def _undo_split(self, q, tg, split_id: int) -> None:
        res = await self.call(self.ident_for(tg, q.message.chat), "cancel_split", split_id=split_id)
        if not res.ok:
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text(res.message.replace("❌ ", "ℹ️ ", 1))
            return
        await q.edit_message_text(res.text)

    # ------------------------------------------------------------ summary / daily / trend screens that edit in place
    async def _view(self, q, tg, kind: str, arg: str) -> None:
        ident = self.private(tg)
        view = {"sum": "summary", "day": "daily", "trend": "trend"}[kind]
        res = await self.call(ident, "ui_view", view=view, arg=arg)
        if not res.ok:
            await q.message.reply_text(res.message)
            return
        markup = kb.summary_keyboard(arg) if kind == "sum" else kb.daily_keyboard(arg) if kind == "day" else kb.trend_keyboard(int(arg))
        try:
            await q.edit_message_text(res.text, reply_markup=markup)
        except BadRequest:  # same content tapped twice
            pass
