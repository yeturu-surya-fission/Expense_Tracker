"""The manager's Telegram bot: carries messages to `ManagerService` and its replies back. No decisions are made here."""
import asyncio
import io
import logging
import re

from sqlalchemy import select
from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from finance_agent.config import settings
from finance_agent.db.models import Notification
from finance_agent.db.session import session_scope
from finance_agent.manager.service import ManagerService, Reply

log = logging.getLogger(__name__)
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 30000

MANAGER_COMMANDS = [
    BotCommand("setpolicy", "📋 Give (or replace) the reimbursement policy"),
    BotCommand("editpolicy", "✏️ Change part of the policy"),
    BotCommand("policy", "📖 See the current rules"),
    BotCommand("claims", "📨 Claims waiting for your decision"),
    BotCommand("paid", "💸 Mark approved claims as paid"),
    BotCommand("employees", "👥 Who has joined"),
    BotCommand("code", "🔑 Your company code"),
    BotCommand("history", "🕘 Earlier policy versions"),
    BotCommand("done", "✅ Finish a policy sent in several messages"),
    BotCommand("retry", "🔁 Try the last policy step again"),
    BotCommand("cancel", "✖ Stop what I'm waiting for"),
    BotCommand("help", "❓ How this works"),
]


def markup(buttons: list[list[dict]] | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(b["label"], callback_data=b["data"]) for b in row] for row in buttons])


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)


class ManagerBot:
    def __init__(self, service: ManagerService):
        self.service = service

    async def _send(self, message, reply: Reply) -> None:
        await message.reply_text(reply.text, reply_markup=markup(reply.buttons))

    @staticmethod
    def _progress(message):
        async def say(text: str) -> None:
            await message.reply_text(text)
        return say

    def _command(self, name: str, *, text_after: bool = False, slow: bool = False):
        """A /command. `text_after`: whatever follows the command is passed on (/setpolicy <the policy>). `slow`: tell the manager we're working."""
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            msg = update.effective_message
            kwargs = {}
            if text_after:
                kwargs["text"] = (msg.text.split(None, 1)[1] if len((msg.text or "").split(None, 1)) > 1 else "")[:MAX_TEXT_CHARS]
            if slow:
                kwargs["progress"] = self._progress(msg)
            await self._send(msg, await getattr(self.service, name)(update.effective_user.id, **kwargs))
        return handler

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        await self._send(msg, await self.service.text(update.effective_user.id, msg.text[:MAX_TEXT_CHARS], self._progress(msg)))

    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, doc = update.effective_message, update.effective_message.document
        if doc.file_size and doc.file_size > MAX_FILE_BYTES:
            await msg.reply_text("That file is too big. Please send a policy under 5 MB, or paste the text.")
            return
        data = bytes(await (await doc.get_file()).download_as_bytearray())
        try:
            text = pdf_text(data) if (doc.mime_type == "application/pdf" or (doc.file_name or "").lower().endswith(".pdf")) else data.decode("utf-8", "replace")
        except Exception:
            await msg.reply_text("I couldn't read that file. Please paste the policy text instead.")
            return
        if not text.strip():
            await msg.reply_text("I couldn't find any text in that file (a scanned PDF?). Please paste the policy text instead.")
            return
        await self._send(msg, await self.service.document(update.effective_user.id, text[:MAX_TEXT_CHARS], self._progress(msg)))

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        reply = await self.service.button(q.from_user.id, q.data, self._progress(q.message))
        try:
            await q.edit_message_reply_markup(reply_markup=None)  # buttons are one-shot
        except TelegramError:
            pass
        await self._send(q.message, reply)


def build_application(service: ManagerService) -> Application:
    token = settings.telegram_bot_token.strip().strip("\"'")
    if not re.fullmatch(r"\d+:[\w-]{30,}", token):
        raise RuntimeError("FINANCE_TELEGRAM_BOT_TOKEN is missing or malformed. Create a second bot with @BotFather for the manager and put its token in .env")
    bot = ManagerBot(service)
    app = Application.builder().token(token).concurrent_updates(True).build()
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", bot._command("start"), filters=private))
    app.add_handler(CommandHandler("help", bot._command("help"), filters=private))
    app.add_handler(CommandHandler("setpolicy", bot._command("set_policy", text_after=True, slow=True), filters=private))
    app.add_handler(CommandHandler("editpolicy", bot._command("edit_policy", text_after=True, slow=True), filters=private))
    app.add_handler(CommandHandler("done", bot._command("done", slow=True), filters=private))
    app.add_handler(CommandHandler("retry", bot._command("retry", slow=True), filters=private))
    app.add_handler(CommandHandler("cancel", bot._command("cancel"), filters=private))
    app.add_handler(CommandHandler("policy", bot._command("policy"), filters=private))
    app.add_handler(CommandHandler("history", bot._command("history"), filters=private))
    app.add_handler(CommandHandler("employees", bot._command("employees"), filters=private))
    app.add_handler(CommandHandler("code", bot._command("code"), filters=private))
    app.add_handler(CommandHandler("claims", bot._command("pending"), filters=private))
    app.add_handler(CommandHandler("paid", bot._command("paid_list"), filters=private))
    app.add_handler(MessageHandler(private & filters.Document.ALL, bot.on_document))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, bot.on_text))
    app.add_handler(CallbackQueryHandler(bot.on_callback))
    return app


async def notifier_loop(bot: Bot) -> None:
    """Delivers what the service wrote for the manager (new claims, employee questions, replies)."""
    while True:
        try:
            await deliver_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("manager notifications failed")
        await asyncio.sleep(2)


async def deliver_once(bot: Bot) -> int:
    async with session_scope() as s:
        rows = list((await s.execute(
            select(Notification).where(Notification.status == "pending").order_by(Notification.id).limit(20).with_for_update(skip_locked=True)
        )).scalars().all())
        for n in rows:
            try:
                await bot.send_message(n.telegram_id, n.text, reply_markup=markup(n.buttons))
                n.status = "sent"
            except TelegramError as e:
                n.attempts += 1
                n.status = "failed" if n.attempts >= 5 else "pending"
                log.warning("could not message manager %s: %s", n.telegram_id, e)
        return len(rows)
