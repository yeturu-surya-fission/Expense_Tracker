"""Private chat: /start, menu buttons, commands and free text."""
from sqlalchemy import select
from telegram import Update
from telegram import User as TgUser
from telegram.ext import ContextTypes

from app.agent.guardrails import HELP, MAX_USER_MESSAGE_CHARS
from app.bot import keyboards as kb
from app.bot.base import BotBase, keep_typing
from app.core import friends, notices, views
from app.core.errors import UserError
from app.core.formatting import friendly_expense
from app.core.notifier import get_notifier
from app.core.periods import month_key
from app.db.models import Expense
from app.db.session import session_scope


class PrivateMixin(BotBase):
    # ------------------------------------------------------------ /start (also handles invite and join links)
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        tg, msg = update.effective_user, update.effective_message
        param = context.args[0] if context.args else ""
        if param.startswith("inv_"):
            if not await self._accept_invite(tg, msg, param[4:]):
                return
        elif not await self._guard(update):
            return
        uid = await self._uid(tg, started=True)
        async with session_scope() as s:
            await notices.deliver_pending(s, uid)  # anything that was waiting for them
        if param.startswith("join_") and param[5:].isdigit():
            await self._join_from_start(msg, tg, int(param[5:]))
        await msg.reply_text(HELP, reply_markup=kb.main_menu())

    async def _accept_invite(self, tg: TgUser, msg, inviter_tg: str) -> bool:
        """Someone opened a friend's invite link: connect them both (each side uses the other's Telegram name)."""
        if not inviter_tg.isdigit() or not await self.allowed(int(inviter_tg)) or int(inviter_tg) == tg.id:
            await msg.reply_text("That invite link isn't valid.")
            return False
        inviter_name = ""
        try:
            async with session_scope() as s:
                inviter = await friends.upsert_user(s, int(inviter_tg))
                me = await friends.upsert_user(s, tg.id, tg.full_name, tg.username, started=True)
                inviter_name = friends.display(inviter)
                for owner, other in ((inviter, me), (me, inviter)):
                    if any(u.id == other.id for _, u in await friends.list_friends(s, owner.id)):
                        continue
                    nick = (friends.display(other) or "Friend")[:30]
                    try:
                        await friends.add_friend(s, owner.id, other.telegram_id, nick)
                    except UserError:  # that nickname is taken in their list: make it unique
                        await friends.add_friend(s, owner.id, other.telegram_id, f"{nick} {other.telegram_id % 1000}")
        except UserError as e:
            await msg.reply_text(f"❌ {e}")
            return False
        await get_notifier().send(int(inviter_tg), f"👥 {tg.full_name} joined your friends through your invite link. You can split bills with them now.\n(To rename them: 'add friend NewName {tg.id}')")
        await msg.reply_text(f"🤝 You're now friends with {inviter_name or 'them'}. They can split bills with you, and you'll get a message when they do.")
        return True

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._guard(update):
            await update.effective_message.reply_text(HELP, reply_markup=kb.main_menu())

    # ------------------------------------------------------------ commands + menu (no LLM, instant)
    async def _direct(self, tg: TgUser, message, action: str) -> None:
        uid = await self._uid(tg, started=True)
        async with session_scope() as s:
            if action == "summary":
                await message.reply_text(await views.summary_text(s, uid, "this_month"), reply_markup=kb.summary_keyboard("this_month"))
            elif action == "recent":
                await message.reply_text(await views.recent_text(s, uid, limit=10))
            elif action == "budgets":
                await message.reply_text(await views.budgets_text(s, uid))
            elif action == "friends":
                await message.reply_text(await views.friends_text(s, uid))
            elif action == "dues":
                await message.reply_text(await views.dues_text(s, uid))
            elif action == "daily":
                await message.reply_text(await views.daily_text(s, uid), reply_markup=kb.daily_keyboard(month_key()))
            elif action == "trend":
                await message.reply_text(await views.trend_text(s, uid, 6), reply_markup=kb.trend_keyboard(6))
            elif action == "invite":
                link = notices.start_link(f"inv_{tg.id}")
                await message.reply_text(f"🔗 Send this link to a friend. When they open it, you become friends automatically:\n{link}" if link else "The bot link isn't ready yet. Try again in a moment.")
            elif action == "undo":
                last = (await s.execute(
                    select(Expense).where(Expense.user_id == uid, Expense.voided_at.is_(None), Expense.source == "personal").order_by(Expense.id.desc()).limit(1)
                )).scalar_one_or_none()
                if last is None:
                    await message.reply_text("Nothing to undo yet.")
                else:
                    await message.reply_text(f"Undo this?\n\n{friendly_expense(last)}", reply_markup=kb.undo_confirm_keyboard(last.id))

    async def on_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/summary /recent /budgets /friends /dues /daily /trend /invite /undo"""
        if await self._guard(update):
            action = update.effective_message.text.split()[0].lstrip("/").split("@")[0]
            await self._direct(update.effective_user, update.effective_message, action)

    async def on_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        action = kb.MENU[update.effective_message.text]
        if action == "help":
            await self.help(update, context)
        else:
            await self._direct(update.effective_user, update.effective_message, action)

    # ------------------------------------------------------------ free text -> agent
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        msg = update.effective_message
        if len(msg.text) > MAX_USER_MESSAGE_CHARS:
            await msg.reply_text("✂️ That message is too long for me. Please keep it short, e.g. \"spent 200 on lunch\".")
            return
        async with keep_typing(context.bot, msg.chat_id):
            reply = await self._ask(update.effective_user, msg.text)
        await self._send(msg, reply)
