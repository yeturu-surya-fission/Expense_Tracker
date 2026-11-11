"""
Telegram group chats.

Setup is automatic: the moment an allowed person adds the bot to a group, the server turns that group into an expense group.
There are no /creategroup or /join steps. Anyone in the group can talk to the bot, but ONLY by mentioning it (@bot ...),
replying to it, or using a /command, so ordinary chatter never costs an AI call or gets a reply. (Keep BotFather privacy
mode ON: then Telegram does not even deliver ordinary messages to the bot.)

Who is "in" the group: a bot cannot list members, so the server learns them from the person who added the bot, "X joined"
notices (reported here), the adder's friends who are in the chat (checked here, once), and anyone who mentions the bot.
"""
import html
import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from app.agent.runner import AgentReply
from app.bot import keyboards as kb
from app.bot.base import MAX_USER_MESSAGE_CHARS, BotBase, keep_typing
from app.mcp_client.types import NoGroup, NotAllowed

log = logging.getLogger(__name__)

GROUP_HELP = (
    "🧳 Everyone in this group can use me. Just mention me:\n"
    "• @{bot} I paid 1000 for petrol\n"
    "• @{bot} Surya paid 600 for ice cream, split between me and Surya\n"
    "• @{bot} split the bill   (or: settle up)\n\n"
    "Commands: /summary (who paid what), /people (who is in), /settle and /help."
)
IN_CHAT = ("member", "administrator", "creator", "restricted")


def _person(tg_user) -> dict:
    return {"telegram_id": tg_user.id, "name": tg_user.full_name, "username": tg_user.username}


class GroupMixin(BotBase):
    # ------------------------------------------------------------ is this message for the bot?
    @staticmethod
    def _addressed(msg, bot) -> tuple[bool, str]:
        text = msg.text or ""
        mention = f"@{bot.username}".lower()
        mentioned = any(
            e.type == "mention" and text[e.offset:e.offset + e.length].lower() == mention for e in (msg.entities or [])
        )
        replied = bool(msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == bot.id)
        clean = text.replace(f"@{bot.username}", "").replace(f"@{bot.username.lower()}", "").strip() if mentioned else text.strip()
        return mentioned or replied, clean

    async def _group_ready(self, chat, tg) -> bool:
        """Can this person use the bot in this chat? (The server sets the group up on the first mention by an allowed person.)"""
        try:
            await self.gateway.session_token(self.in_group(tg, chat))
            return True
        except (NoGroup, NotAllowed):
            return False

    # ------------------------------------------------------------ talking to the bot in a group
    async def on_group_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, tg = update.effective_message, update.effective_user
        addressed, text = self._addressed(msg, context.bot)
        if not addressed or not text or tg.is_bot:
            return  # not for us: stay quiet
        if not await self._group_ready(msg.chat, tg):
            return  # a chat that no allowed person ever set up
        if len(text) > MAX_USER_MESSAGE_CHARS:
            await msg.reply_text("✂️ Please keep it short, e.g. 'I paid 1000 for petrol'.")
            return
        async with keep_typing(context.bot, msg.chat_id):
            reply = await self._ask(tg, msg.chat, text, bot=context.bot)  # speaking to the bot makes you a member
        await self._send(msg, reply, owner_telegram_id=tg.id)
        await self.announce_unreachable(msg, reply)

    async def announce_unreachable(self, message, reply: AgentReply) -> None:
        """Tag people the bot could not message and give them a Start button: their bill arrives the moment they press it."""
        if reply.start_users:
            await self._tag_to_start(message, reply.start_users, reply.group_id)

    async def _tag_to_start(self, message, users: list[dict], group_id: int | None) -> None:
        mentions = ", ".join(f'<a href="tg://user?id={u["telegram_id"]}">{html.escape(u["name"])}</a>' for u in users)
        link = self.start_link(f"join_{group_id}" if group_id else "")
        text = f"👋 {mentions}: I can't message you privately yet. Press Start below and your bill arrives right away."
        await message.reply_text(text, parse_mode="HTML", reply_markup=kb.start_bot_keyboard(link) if link else None)

    def start_link(self, param: str = "") -> str:
        from app.config import settings
        if not settings.bot_username:
            return ""
        return f"https://t.me/{settings.bot_username}" + (f"?start={param}" if param else "")

    # ------------------------------------------------------------ the bot was added to / removed from a chat
    async def on_bot_membership(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        change = update.my_chat_member
        chat, adder = change.chat, change.from_user
        if chat.type not in ("group", "supergroup"):
            return
        now, before = change.new_chat_member.status, change.old_chat_member.status
        if now not in ("member", "administrator") or before in ("member", "administrator"):
            return  # removed, or only promoted / demoted
        await self._set_up_group(context.bot, chat, adder)

    async def _group_event(self, event: str, chat_id: int, **fields):
        return await self.gateway.platform("platform_group_event", {"event": event, "chat_id": chat_id, **fields})

    async def _set_up_group(self, bot, chat, adder) -> None:
        res = await self._group_event("bot_added", chat.id, title=chat.title, actor=_person(adder))
        if res.data.get("action") == "leave":  # only the owner side may add the bot
            try:
                await bot.send_message(chat.id, "Sorry, I can only be added to groups by my owner. Leaving now.")
            finally:
                await bot.leave_chat(chat.id)
            return
        created = bool(res.data.get("created"))
        # friends of the adder who are already in this chat (best effort: only reliable if the bot is an admin)
        friend_ids = (await self.gateway.platform("platform_friend_ids", {"telegram_id": adder.id})).data.get("telegram_ids", [])
        present = []
        for tg_id in friend_ids:
            try:
                if (await bot.get_chat_member(chat.id, tg_id)).status in IN_CHAT:
                    present.append(tg_id)
            except TelegramError:
                pass
        if present:
            res = await self._group_event("bot_added", chat.id, title=chat.title, actor=_person(adder), present_telegram_ids=present)
        if not created:
            return
        people = ", ".join(res.data.get("members", []))
        link = self.start_link()
        await bot.send_message(
            chat.id,
            f"👋 Hi! I'm set up as the expense splitter for {res.data.get('group_name')}. Nobody needs to create or join anything.\n\n"
            f"I know: {people}.\n"
            f"Anyone in this group can mention me:\n"
            f"• @{bot.username} I paid 1000 for petrol\n"
            f"• @{bot.username} split the bill  (I post who owes what and send everyone their bill privately)\n\n"
            f"If I don't know you yet, just mention me once. To get your bill privately, press Start on me.",
            reply_markup=kb.start_bot_keyboard(link) if link else None,
        )

    async def on_new_members(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        people = [_person(m) for m in (msg.new_chat_members or []) if not m.is_bot]
        if people:
            await self._group_event("members_joined", msg.chat_id, members=people)

    async def on_left_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        left = msg.left_chat_member
        if left is not None and not left.is_bot:
            await self._group_event("member_left", msg.chat_id, actor=_person(left))

    async def on_title_change(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        await self._group_event("title_changed", msg.chat_id, title=msg.new_chat_title)

    async def on_migrate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg.migrate_to_chat_id:  # the group became a supergroup: same expenses, new chat id
            await self._group_event("migrated", msg.chat_id, new_chat_id=msg.migrate_to_chat_id)

    # ------------------------------------------------------------ /commands in a group (no AI)
    async def group_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(GROUP_HELP.format(bot=context.bot.username))

    async def cmd_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, tg = update.effective_message, update.effective_user
        if not await self._group_ready(msg.chat, tg):
            return
        count = await self.member_count(context.bot, msg.chat)
        res = await self.gateway.call(self.in_group(tg, msg.chat), "group_summary", member_count=count)
        await msg.reply_text(res.message)

    async def cmd_people(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, tg = update.effective_message, update.effective_user
        if not await self._group_ready(msg.chat, tg):
            return
        info = (await self.gateway.call(self.in_group(tg, msg.chat), "agent_context")).data
        names = info.get("members") or []
        if not names:
            await msg.reply_text("I don't know anyone in this group yet. Everyone should @mention me once.")
            return
        count = await self.member_count(context.bot, msg.chat)
        lines = [f"👥 {info.get('group') or 'This group'} · {len(names)} people", *(f"• {n}" for n in names)]
        if count and count - 1 > len(names):  # the bot is one of the chat's members
            lines += ["", f"⚠️ The chat has {count - 1} people. The other {count - 1 - len(names)} should @mention me once so I can include them."]
        text = "\n".join(lines)
        await msg.reply_text(text)

    async def cmd_settle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, tg = update.effective_message, update.effective_user
        if not await self._group_ready(msg.chat, tg):
            return
        count = await self.member_count(context.bot, msg.chat)
        res = await self.gateway.call(self.in_group(tg, msg.chat), "settle_group", member_count=count)
        # the server asks for confirmation; 'Yes' sends the same call again with the answer
        await msg.reply_text(res.message, reply_markup=kb.settle_keyboard() if res.status == "input_required" else None)

    async def _settle_button(self, q, tg, bot) -> None:
        chat = q.message.chat
        await q.edit_message_reply_markup(reply_markup=None)
        count = await self.member_count(bot, chat)
        res = await self.gateway.call(
            self.in_group(tg, chat), "settle_group", answers={"settle": "yes"}, idempotency_key=f"gsettle:{chat.id}:{q.message.message_id}",
            member_count=count,
        )
        await q.message.reply_text(res.message)
        if res.data.get("start_users"):
            await self._tag_to_start(q.message, res.data["start_users"], res.data.get("group_id"))
