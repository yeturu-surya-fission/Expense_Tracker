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
from app.core import expenses, friends, groups, notices, splits, views
from app.core.errors import UserError
from app.core.formatting import friendly_expense, money
from app.core.periods import PeriodError
from app.db.session import session_scope

log = logging.getLogger(__name__)


class BotHandlers(PrivateMixin, GroupMixin, DueMixin, StatementMixin):
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()  # stops the button's loading spinner immediately
        tg, msg = q.from_user, q.message
        if not await self.allowed(tg.id):
            return
        kind, _, arg = q.data.partition(":")

        if kind == "hitl":  # answer to a question the bot asked: hitl:<option>[:<owner telegram id>]
            option, _, owner = arg.partition(":")
            if owner and owner != str(tg.id):
                await q.answer("This question is for someone else.", show_alert=True)
                return
            await q.edit_message_reply_markup(reply_markup=None)  # one-shot buttons
            in_group = msg.chat.type != "private"
            async with keep_typing(context.bot, msg.chat_id):
                reply = await self._ask(tg, option, from_button=True, group_chat_id=msg.chat_id if in_group else None)
            await self._send(msg, reply, owner_telegram_id=tg.id if in_group else None)
            if in_group:
                async with session_scope() as s:
                    group = await groups.group_for_chat(s, msg.chat_id)
                if group is not None:
                    await self.announce_unreachable(msg, reply, group.id)

        elif kind == "due":
            await self._due(q, tg, arg)

        elif kind == "gsettle":
            await self._settle_button(q, tg, int(arg))

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
        uid, lines = await self._uid(tg, started=True), []
        try:
            async with session_scope() as s:
                for i in ids:
                    lines.append(f"↩️ Undone: {friendly_expense(await expenses.void_expense(s, uid, i))}")
        except UserError:
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text("That was already undone.")
            return
        await q.edit_message_text("\n".join(lines))

    async def _undo_split(self, q, tg, split_id: int) -> None:
        uid = await self._uid(tg, started=True)
        try:
            async with session_scope() as s:
                me = await friends.get_user(s, uid)
                shared, cancelled = await splits.cancel_split(s, uid, split_id)
                await notices.notify_cancelled(s, cancelled, me)
        except UserError as e:
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text(f"ℹ️ {e}")
            return
        await q.edit_message_text(f"↩️ Cancelled split #{shared.id} ({shared.description}, {money(shared.amount)}). Everyone was told.")

    # ------------------------------------------------------------ summary / daily / trend screens that edit in place
    async def _view(self, q, tg, kind: str, arg: str) -> None:
        uid = await self._uid(tg, started=True)
        try:
            async with session_scope() as s:
                if kind == "sum":
                    text, markup = await views.summary_text(s, uid, arg), kb.summary_keyboard(arg)
                elif kind == "day":
                    text, markup = await views.daily_text(s, uid, arg), kb.daily_keyboard(arg)
                else:
                    text, markup = await views.trend_text(s, uid, int(arg)), kb.trend_keyboard(int(arg))
            await q.edit_message_text(text, reply_markup=markup)
        except BadRequest:  # same content tapped twice
            pass
        except PeriodError as e:
            await q.message.reply_text(f"❌ {e}")
