"""Bank statement PDFs (private chat only): upload -> preview -> confirm -> expenses added."""
import asyncio
import logging
import re

from telegram import Update
from telegram import User as TgUser
from telegram.ext import ContextTypes

from app.agent import statement_parser
from app.bot import keyboards as kb
from app.bot.base import BotBase, keep_typing
from app.core import budgets, statements
from app.core.errors import UserError
from app.db.session import session_scope

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
        uid = await self._uid(update.effective_user, started=True)
        try:
            async with keep_typing(context.bot, msg.chat_id):
                data = bytes(await (await doc.get_file()).download_as_bytearray())
                pages = await asyncio.to_thread(statements.extract_pdf_pages, data, pw.group(1) if pw else None)
                async with session_scope() as s:
                    cats = await budgets.budget_categories(s, uid)
                rows = await asyncio.wait_for(statement_parser.parse_pages(self.llm, pages, cats), STATEMENT_TIMEOUT)
                async with session_scope() as s:
                    items, meta = await statements.build_plan(s, uid, rows)
                    imp = await statements.save_import(s, uid, doc.file_name, items, meta)
            c = statements.counts(imp.items)
            await status.edit_text(statements.preview_text(imp), reply_markup=kb.import_keyboard(imp.id, c["new"], c["maybe"]))
        except statements.PasswordRequired:
            await status.edit_text("🔒 This PDF is password protected. Send it again with the password as the caption, e.g.  password 12345")
        except statements.WrongPassword:
            await status.edit_text("🔒 That password didn't work. Send the PDF again with the right password as the caption.")
        except UserError as e:
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
        uid = await self._uid(tg, started=True)
        try:
            async with session_scope() as s:
                if mode == "no":
                    await statements.cancel_import(s, uid, int(raw_id))
                    await q.edit_message_text("👍 Cancelled - nothing was imported.")
                    return
                res = await statements.apply_import(s, uid, int(raw_id), include_maybes=(mode == "all"))
        except UserError as e:
            await q.edit_message_text(f"ℹ️ {e}")
            return
        await q.edit_message_text(res.text, reply_markup=kb.after_import_keyboard(int(raw_id)))

    async def _import_undo(self, q, tg: TgUser, import_id: int) -> None:
        try:
            async with session_scope() as s:
                text = await statements.undo_import(s, await self._uid(tg, started=True), import_id)
        except UserError as e:
            await q.edit_message_text(f"ℹ️ {e}")
            return
        await q.edit_message_text(text)
