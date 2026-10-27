"""Shared helpers for every handler: who is allowed, who is this person in our database, asking the agent."""
import asyncio
import logging
from contextlib import asynccontextmanager

from telegram import User as TgUser
from telegram.constants import ChatAction

from app.agent.runner import AgentReply, AgentRunner, is_rate_limit
from app.bot import keyboards as kb
from app.config import settings
from app.core import friends, groups
from app.db.session import session_scope

log = logging.getLogger(__name__)
REPLY_TIMEOUT = 60  # hard cap on one message, whatever the LLM does


@asynccontextmanager
async def keep_typing(bot, chat_id: int):
    """Telegram's 'typing…' fades after ~5s, so refresh it until the answer is ready."""
    async def loop():
        while True:
            try:
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(4)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()


class BotBase:
    def __init__(self, runner: AgentRunner, llm):
        self.runner = runner
        self.llm = llm  # used by the statement importer
        self._users: dict[int, tuple[int, bool]] = {}  # telegram id -> (db id, has started the bot)

    # ---- who may use the bot
    @staticmethod
    def can_add_bot(telegram_id: int) -> bool:
        """Only the owner side (the allow-list; everyone if it is empty) may add the bot to a group. Others make it leave."""
        return not settings.allowed_ids or telegram_id in settings.allowed_ids

    @staticmethod
    async def allowed(telegram_id: int) -> bool:
        """
        May this person use the bot in a private chat? The allow-list, friends of a listed person, and anyone who is
        in an expense group (everyone in a group the owner added the bot to can use it).
        """
        if not settings.allowed_ids or telegram_id in settings.allowed_ids:
            return True
        async with session_scope() as s:
            if await friends.is_friend_of_anyone(s, telegram_id):
                return True
            user = await friends.upsert_user(s, telegram_id)
            return await groups.is_member_anywhere(s, user.id)

    async def _guard(self, update) -> bool:
        if await self.allowed(update.effective_user.id):
            return True
        target = update.effective_message or (update.callback_query and update.callback_query.message)
        if target:
            await target.reply_text("Sorry, this is a private bot. 🔒 Ask a friend who uses it to add you.")
        return False

    # ---- who is this person in our database
    async def _uid(self, tg: TgUser, started: bool = False) -> int:
        hit = self._users.get(tg.id)
        if hit and (hit[1] or not started):
            return hit[0]
        async with session_scope() as s:
            user = await friends.upsert_user(s, tg.id, tg.full_name, tg.username, started=started)
            self._users[tg.id] = (user.id, user.started_at is not None)
        return user.id

    # ---- asking the agent
    async def _ask(
        self, tg: TgUser, text: str, *, from_button: bool = False, group_chat_id: int | None = None
    ) -> AgentReply:
        in_group = group_chat_id is not None
        uid = await self._uid(tg, started=not in_group)
        thread = f"tg-g{group_chat_id}-{tg.id}" if in_group else f"tg-{tg.id}"
        context = {"chat_type": "group", "group_chat_id": group_chat_id} if in_group else {"chat_type": "private"}
        try:
            return await asyncio.wait_for(
                self.runner.chat(user_id=uid, thread_id=thread, text=text, from_button=from_button, context=context),
                timeout=REPLY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.error("agent timed out for telegram user %s", tg.id)
            return AgentReply("⏱️ The AI took too long to answer. Please send it again.")
        except Exception as e:
            if is_rate_limit(e):
                log.warning("LLM rate limit hit for telegram user %s", tg.id)
                return AgentReply("⏳ The AI is a bit busy right now (rate limit). Please try again in about a minute.")
            log.exception("agent failed for telegram user %s", tg.id)
            return AgentReply("😵 Sorry, my brain hiccuped (AI or database error). Please try again in a moment.")

    @staticmethod
    async def _send(message, reply: AgentReply, owner_telegram_id: int | None = None) -> None:
        """Send an agent reply with the right buttons. In group chats, answer buttons are tied to the person who asked."""
        if reply.choices:
            markup = kb.choice_keyboard(reply.choices, owner_telegram_id)
        else:
            markup = kb.result_keyboard(reply.txn_ids, reply.split_ids)
        await message.reply_text(reply.text[:4000], reply_markup=markup)
