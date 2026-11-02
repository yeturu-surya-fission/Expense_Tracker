"""Bank statement PDFs (private chat only): upload -> preview -> confirm -> expenses added. The server holds the rules."""
import asyncio
import base64
import logging
import re

from telegram import Update
from telegram import User as TgUser
from telegram.ext import ContextTypes

from app.agent import statement_parser
from app.bot import keyboards as kb
from app.bot.base import BotBase, keep_typing

log = logging.getLogger(__name__)
MAX_PDF_BYTES = 10 * 1024 * 1024
STATEMENT_TIMEOUT = 180


class StatementMixin(BotBase):
    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        msg, doc = update.effective_message, update.effective_message.document
        is_pdf = doc.mime_type == "application/pdf" or (doc.file_name or "").lower().endswith(".pdf")
        if not is_pdf:
            await msg.reply_text("📄 I can only read PDF bank statements. Please send a PDF.")
            return
        if doc.file_size and doc.file_size > MAX_PDF_BYTES:
            await msg.reply_text("📄 That PDF is bigger than 10 MB. Please send a smaller one.")
            return

        pw = re.search(r"(?i)pass(?:word)?\s*[:=]?\s*(\S+)", msg.caption or "")
        status = await msg.reply_text("📄 Got your statement - reading it. This can take up to a minute…")
        ident = self.private(update.effective_user)
        try:
            async with keep_typing(context.bot, msg.chat_id):
                data = bytes(await (await doc.get_file()).download_as_bytearray())
                read = await self.call(ident, "statement_read_pdf", pdf_base64=base64.b64encode(data).decode(), password=pw.group(1) if pw else None)
                if not read.ok:
                    await status.edit_text(read.message)
                    return
                if read.data.get("password"):
                    await status.edit_text(read.text)
                    return
                rows = await asyncio.wait_for(
                    statement_parser.parse_pages(
                        self.llm, read.data["pages"], categories=read.data["categories"], today=read.data["today"],
                        user_categories=read.data.get("budget_categories"),
                    ),
                    STATEMENT_TIMEOUT,
                )
                prev = await self.call(ident, "statement_preview", filename=doc.file_name, rows=rows)
            if not prev.ok:
                await status.edit_text(prev.message)
                return
            await status.edit_text(prev.text, reply_markup=kb.import_keyboard(prev.data["import_id"], prev.data["new"], prev.data["maybe"]))
        except statement_parser.StatementError as e:
            await status.edit_text(f"❌ {e}")
        except asyncio.TimeoutError:
            await status.edit_text("⏱️ Reading the statement took too long. Please try again, or send a shorter period.")
        except Exception:
            log.exception("statement import failed for telegram user %s", update.effective_user.id)
            await status.edit_text("😵 Sorry, I couldn't process that statement. Please try again.")

    async def on_other_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._guard(update):
            await update.effective_message.reply_text("📄 I can only read PDF bank statements. Please send a PDF.")

    async def _import_action(self, q, tg: TgUser, arg: str) -> None:
        raw_id, _, mode = arg.partition(":")
        ident, import_id = self.private(tg), int(raw_id)
        if mode == "no":
            res = await self.call(ident, "statement_cancel", import_id=import_id)
            await q.edit_message_text(res.message.replace("❌ ", "ℹ️ ", 1))
            return
        res = await self.gateway.call(ident, "statement_apply", {"import_id": import_id, "include_maybes": mode == "all"})
        if not res.ok:
            await q.edit_message_text(res.message.replace("❌ ", "ℹ️ ", 1))
            return
        await q.edit_message_text(res.text, reply_markup=kb.after_import_keyboard(import_id))

    async def _import_undo(self, q, tg: TgUser, import_id: int) -> None:
        res = await self.call(self.private(tg), "statement_undo", import_id=import_id)
        await q.edit_message_text(res.message.replace("❌ ", "ℹ️ ", 1))
