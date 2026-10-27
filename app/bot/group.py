"""
Telegram group chats.

Setup is automatic: the moment an allowed person adds the bot to a group, that group becomes an expense group. There are
no /creategroup or /join steps. Anyone in the group can talk to the bot, but ONLY by mentioning it (@bot ...), replying to
it, or using a /command, so ordinary chatter never costs an AI call or gets a reply. (Keep BotFather privacy mode ON:
then Telegram does not even deliver ordinary messages to the bot.)

Who is "in" the group: a bot cannot list members, so it learns them from the person who added it, "X joined" notices,
the adder's friends who are in the chat (checked once), and anyone who mentions it.
"""
import html

from telegram import Update
from telegram.ext import ContextTypes

from app.agent.guardrails import MAX_USER_MESSAGE_CHARS
from app.agent.runner import AgentReply
from app.bot import keyboards as kb
from app.bot.base import BotBase, keep_typing
from app.core import friends, group_flow, groups, markers, notices, views
from app.core.formatting import money
from app.core.notifier import get_notifier
from app.db.models import Group
from app.db.session import session_scope

GROUP_HELP = (
    "🧳 Everyone in this group can use me. Just mention me:\n"
    "• @{bot} I paid 1000 for petrol\n"
    "• @{bot} Surya paid 600 for ice cream, split between me and Surya\n"
    "• @{bot} split the bill   (or: settle up)\n\n"
    "Shortcuts: /group (who paid what) and /settle."
)
IN_CHAT = ("member", "administrator", "creator", "restricted")


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

    async def _group_for(self, chat, tg) -> Group | None:
        """The expense group of this chat. A chat the bot was already in gets set up on the first mention by an allowed person."""
        async with session_scope() as s:
            group = await groups.group_for_chat(s, chat.id)
        if group is not None:
            return group
        if not self.can_add_bot(tg.id):
            return None
        uid = await self._uid(tg)
        async with session_scope() as s:
            group, _ = await groups.ensure_group(s, chat.id, chat.title, uid)
            await groups.join_group(s, group.id, uid)
        return group

    # ------------------------------------------------------------ talking to the bot in a group
    async def on_group_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, tg = update.effective_message, update.effective_user
        addressed, text = self._addressed(msg, context.bot)
        if not addressed or not text or tg.is_bot:
            return  # not for us: stay quiet
        group = await self._group_for(msg.chat, tg)
        if group is None:
            return  # a chat that no allowed person ever set up
        if len(text) > MAX_USER_MESSAGE_CHARS:
            await msg.reply_text("✂️ Please keep it short, e.g. 'I paid 1000 for petrol'.")
            return
        async with keep_typing(context.bot, msg.chat_id):
            reply = await self._ask(tg, text, group_chat_id=msg.chat_id)  # speaking to the bot makes you a member
        await self._send(msg, reply, owner_telegram_id=tg.id)
        await self.announce_unreachable(msg, reply, group.id)

    async def announce_unreachable(self, message, reply: AgentReply, group_id: int) -> None:
        """Tag people the bot could not message and give them a Start button: their bill arrives the moment they press it."""
        if not reply.start_ids:
            return
        await self._tag_to_start(message, reply.start_ids, group_id)

    async def _tag_to_start(self, message, telegram_ids: list[int], group_id: int) -> None:
        async with session_scope() as s:
            users = [u for u in [await friends.upsert_user(s, i) for i in telegram_ids]]
        mentions = ", ".join(f'<a href="tg://user?id={u.telegram_id}">{html.escape(friends.display(u))}</a>' for u in users)
        link = notices.start_link(f"join_{group_id}")
        text = f"👋 {mentions}: I can't message you privately yet. Press Start below and your bill arrives right away."
        await message.reply_text(text, parse_mode="HTML", reply_markup=kb.start_bot_keyboard(link) if link else None)

    # ------------------------------------------------------------ the bot was added to / removed from a chat
    async def on_bot_membership(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        change = update.my_chat_member
        chat, adder = change.chat, change.from_user
        if chat.type not in ("group", "supergroup"):
            return
        now, before = change.new_chat_member.status, change.old_chat_member.status
        if now not in ("member", "administrator") or before in ("member", "administrator"):
            return  # removed, or only promoted / demoted
        if not self.can_add_bot(adder.id):
            try:
                await context.bot.send_message(chat.id, "Sorry, I can only be added to groups by my owner. Leaving now.")
            finally:
                await context.bot.leave_chat(chat.id)
            return
        await self._set_up_group(context.bot, chat, adder)

    async def _set_up_group(self, bot, chat, adder) -> None:
        uid = await self._uid(adder)
        async with session_scope() as s:
            group, created = await groups.ensure_group(s, chat.id, chat.title, uid)
            await groups.join_group(s, group.id, uid)
            adders_friends = await friends.list_friends(s, uid)
        for friend, user in adders_friends:  # friends of the adder who are already in this chat (best effort)
            if await get_notifier().chat_member_status(chat.id, user.telegram_id) in IN_CHAT:
                async with session_scope() as s:
                    await groups.join_group(s, group.id, user.id)
        async with session_scope() as s:
            people = ", ".join(friends.display(u) for u in await groups.members(s, group.id))
        if not created:
            return
        link = notices.start_link()
        await bot.send_message(
            chat.id,
            f"👋 Hi! I'm set up as the expense splitter for {group.name}. Nobody needs to create or join anything.\n\n"
            f"I know: {people}.\n"
            f"Anyone in this group can mention me:\n"
            f"• @{bot.username} I paid 1000 for petrol\n"
            f"• @{bot.username} split the bill  (I post who owes what and send everyone their bill privately)\n\n"
            f"If I don't know you yet, just mention me once. To get your bill privately, press Start on me.",
            reply_markup=kb.start_bot_keyboard(link) if link else None,
        )

    async def on_new_members(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        async with session_scope() as s:
            group = await groups.group_for_chat(s, msg.chat_id)
            if group is None:
                return
            for member in msg.new_chat_members or []:
                if member.is_bot:
                    continue
                user = await friends.upsert_user(s, member.id, member.full_name, member.username)
                await groups.join_group(s, group.id, user.id)

    async def on_left_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        left = msg.left_chat_member
        if left is None or left.is_bot:
            return
        async with session_scope() as s:
            group = await groups.group_for_chat(s, msg.chat_id)
            if group is not None:
                user = await friends.upsert_user(s, left.id)
                await groups.mark_left(s, group.id, user.id)

    async def on_title_change(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        async with session_scope() as s:
            await groups.rename_group(s, msg.chat_id, msg.new_chat_title)

    async def on_migrate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg.migrate_to_chat_id:  # the group became a supergroup: same expenses, new chat id
            async with session_scope() as s:
                await groups.move_group(s, msg.chat_id, msg.migrate_to_chat_id)

    # ------------------------------------------------------------ /commands in a group (no AI)
    async def group_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(GROUP_HELP.format(bot=context.bot.username))

    async def cmd_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, tg = update.effective_message, update.effective_user
        group = await self._group_for(msg.chat, tg)
        if group is None:
            return
        async with session_scope() as s:
            await groups.join_group(s, group.id, await self._uid(tg))
            text = await views.group_text(s, group.id)
        note = await group_flow.missing_members_note(msg.chat_id, group.id)
        await msg.reply_text(f"{text}\n\n{note}" if note else text)

    async def cmd_settle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, tg = update.effective_message, update.effective_user
        group = await self._group_for(msg.chat, tg)
        if group is None:
            return
        async with session_scope() as s:
            await groups.join_group(s, group.id, await self._uid(tg))
            sm = await groups.summary(s, group.id)
        if not sm.expenses:
            await msg.reply_text(f"Nothing to settle in {group.name} yet. Add an expense first.")
            return
        note = await group_flow.missing_members_note(msg.chat_id, group.id)
        text = f"Settle up {group.name}? Total {money(sm.total)}. I'll send everyone their bill privately."
        await msg.reply_text(f"{text}\n\n{note}" if note else text, reply_markup=kb.settle_keyboard(group.id))

    async def _settle_button(self, q, tg, group_id: int) -> None:
        async with session_scope() as s:
            await groups.join_group(s, group_id, await self._uid(tg))
        await q.edit_message_reply_markup(reply_markup=None)
        extracted = markers.extract(await group_flow.settle_and_notify(group_id, q.message.chat_id))
        await q.message.reply_text(extracted.text)
        if extracted.start_ids:
            await self._tag_to_start(q.message, extracted.start_ids, group_id)

    # ------------------------------------------------------------ /start group_<id> (the "Start the bot" button in a group)
    async def _join_from_start(self, message, tg, group_id: int) -> None:
        async with session_scope() as s:
            group = await groups.get_group(s, group_id)
            user = await friends.upsert_user(s, tg.id, tg.full_name, tg.username, started=True)
            await groups.join_group(s, group_id, user.id)
        await message.reply_text(f"✅ You're in {group.name}. Any bill from the group will arrive here.")
