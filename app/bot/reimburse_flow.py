"""
/reimburse and its buttons: the work-reimbursement home screen. Work expenses live apart from the person's own spending, and
claims are decided by the company's finance agent; this file only shows screens and forwards taps to MCP tools.
"""
import io
from datetime import date

from telegram import User as TgUser

from app.bot import keyboards as kb
from app.bot.base import BotBase
from app.bot.work_pdf import build_pdf

ADD_HINT = (
    "➕ Just tell me, in your own words:\n"
    "• “work expense: cab 450 to the client office”\n"
    "• “claim my Pune hotel, 15000 for 2 nights”\n"
    "• or, for something you already logged: “that cab was for work”"
)
ASK_HINT = (
    "❓ Ask me anything about your company's reimbursement policy, for example:\n"
    "• “will I get a refund on a hotel room?”\n"
    "• “what's the meal limit?”\n"
    "• “can I claim a gym pass?”\n"
    "I'll check the real policy, and if it doesn't say, I can ask your manager."
)


class ReimburseMixin(BotBase):
    async def _reimburse_home(self, tg: TgUser, message) -> None:
        res = await self.call(self.private(tg), "reimburse_view")
        await message.reply_text(res.message, reply_markup=kb.reimburse_keyboard(res.data))

    async def _reimburse_command(self, tg: TgUser, message, action: str, args: list[str]) -> None:
        """/reimburse, /join CODE, /claims, /policy <question>: straight to the tools, no AI in between."""
        ident = self.private(tg)
        if action == "reimburse":
            await self._reimburse_home(tg, message)
        elif action == "join":
            if not args:
                await message.reply_text("Say /join and the code your manager gave you, for example:\n/join FIN-7K2QP")
            else:
                await message.reply_text((await self.call(ident, "join_company", code=args[0])).message)
        elif action == "claims":
            await message.reply_text((await self.call(ident, "my_claims")).message)
        elif action == "workpdf":
            res = await self.call(ident, "work_report")
            if not res.ok or not res.data.get("rows"):
                await message.reply_text(res.message if not res.ok else "You haven't logged any work expenses yet. Try: /work cab 450 to the client")
                return
            pdf = build_pdf(res.data.get("name") or tg.full_name, res.data.get("company"), res.data["rows"])
            await message.reply_document(document=io.BytesIO(pdf), filename=f"work-expenses-{date.today().isoformat()}.pdf", caption=res.message)
        elif action == "policy":
            if not args:
                await message.reply_text(ASK_HINT + "\n\nOr type it right here: /policy will I get a refund on a hotel room?")
            else:
                res = await self.call(ident, "ask_policy", question=" ".join(args))
                await message.reply_text(res.message)

    async def _reimburse(self, q, tg: TgUser, arg: str) -> None:
        ident = self.private(tg)
        if arg == "add":
            await q.message.reply_text(ADD_HINT)
        elif arg == "ask":
            await q.message.reply_text(ASK_HINT)
        elif arg == "claims":
            await q.message.reply_text((await self.call(ident, "my_claims")).message)
        elif arg == "send":
            res = await self.call(ident, "submit_claim")  # the server asks for confirmation first
            await q.message.reply_text(res.message, reply_markup=kb.send_claim_keyboard() if res.status == "input_required" else None)
        elif arg == "yes":
            await q.edit_message_reply_markup(reply_markup=None)  # one-shot
            res = await self.call(ident, "submit_claim", answers={"confirm": "yes"}, idempotency_key=f"claim:{tg.id}:{q.message.message_id}")
            await q.message.reply_text(res.message.replace("❌ ", "ℹ️ ", 1))
