"""Private chat: /start, menu buttons, commands and free text. Every answer comes from an MCP tool call."""
from telegram import Update
from telegram.ext import ContextTypes

from app.bot import keyboards as kb
from app.bot.base import MAX_USER_MESSAGE_CHARS, BotBase, keep_typing
from app.util.periods import month_key


class PrivateMixin(BotBase):
    async def _help_text(self, tg) -> str:
        return (await self.call(self.private(tg), "respond_to_user", kind="help")).message

    # ------------------------------------------------------------ /start (also handles invite and join links)
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        tg, msg = update.effective_user, update.effective_message
        param = context.args[0] if context.args else ""
        if param.startswith("inv_"):
            if not await self._accept_invite(tg, msg, param[4:]):
                return
        elif not await self._guard(update):
            return
        # opening a session as someone who pressed Start also makes the server queue anything that was waiting for them
        ident = self.private(tg)
        if param.startswith("join_") and param[5:].isdigit():
            res = await self.call(ident, "join_group_via_start", group_id=int(param[5:]))
            await msg.reply_text(res.message)
        else:
            await self.gateway.session_token(ident, force=True)
        await msg.reply_text(await self._help_text(tg), reply_markup=kb.main_menu())

    async def _accept_invite(self, tg, msg, inviter_tg: str) -> bool:
        """Someone opened a friend's invite link: the server connects them both (each side uses the other's Telegram name)."""
        if not inviter_tg.isdigit():
            await msg.reply_text("That invite link isn't valid.")
            return False
        res = await self.gateway.platform("platform_accept_invite", {
            "person": {"telegram_id": tg.id, "name": tg.full_name, "username": tg.username}, "inviter_telegram_id": int(inviter_tg),
        })
        await msg.reply_text(res.message.replace("❌ ", ""))
        return res.ok

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._guard(update):
            await update.effective_message.reply_text(await self._help_text(update.effective_user), reply_markup=kb.main_menu())

    # ------------------------------------------------------------ commands + menu (no LLM, instant)
    async def _direct(self, tg, message, action: str) -> None:
        ident = self.private(tg)
        if action == "summary":
            res = await self.call(ident, "ui_view", view="summary", arg="this_month")
            await message.reply_text(res.message, reply_markup=kb.summary_keyboard("this_month"))
        elif action == "daily":
            res = await self.call(ident, "ui_view", view="daily", arg=month_key())
            await message.reply_text(res.message, reply_markup=kb.daily_keyboard(month_key()))
        elif action == "trend":
            res = await self.call(ident, "ui_view", view="trend", arg="6")
            await message.reply_text(res.message, reply_markup=kb.trend_keyboard(6))
        elif action == "undo":
            res = await self.call(ident, "last_expense")
            expense_id = res.data.get("expense_id")
            await message.reply_text(res.message, reply_markup=kb.undo_confirm_keyboard(expense_id) if expense_id else None)
        elif action == "dues":
            res = await self.call(ident, "ui_view", view="dues")
            await message.reply_text(res.message, reply_markup=kb.dues_keyboard(res.data.get("dues")))
        elif action in ("recent", "budgets", "friends", "invite"):
            await message.reply_text((await self.call(ident, "ui_view", view=action)).message)

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
            reply = await self._ask(update.effective_user, msg.chat, msg.text, bot=context.bot)
        await self._send(msg, reply)
