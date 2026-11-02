"""Shared helpers for every handler: who is this person, may they use the system, asking the agent, calling MCP tools."""
import asyncio
import logging
from contextlib import asynccontextmanager

from telegram import User as TgUser
from telegram.constants import ChatAction
from telegram.error import TelegramError

from app.agent.runner import AgentReply, AgentRunner, is_rate_limit
from app.bot import keyboards as kb
from app.mcp_client.gateway import McpGateway
from app.mcp_client.types import Identity, McpError, NoGroup, NotAllowed, ToolResult

log = logging.getLogger(__name__)
REPLY_TIMEOUT = 60  # hard cap on one message, whatever the LLM does
MAX_USER_MESSAGE_CHARS = 1000


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
    def __init__(self, runner: AgentRunner, llm, gateway: McpGateway):
        self.runner = runner
        self.llm = llm  # used by the statement importer
        self.gateway = gateway

    # ---- who is this, and where
    @staticmethod
    def private(tg: TgUser) -> Identity:
        """Writing to the bot in private means the bot can write back (the person pressed Start)."""
        return Identity(telegram_id=tg.id, name=tg.full_name, username=tg.username, started=True)

    @staticmethod
    def in_group(tg: TgUser, chat) -> Identity:
        return Identity(telegram_id=tg.id, name=tg.full_name, username=tg.username, chat_type="group",
                        group_chat_id=chat.id, group_title=chat.title)

    @classmethod
    def ident_for(cls, tg: TgUser, chat) -> Identity:
        return cls.private(tg) if chat.type == "private" else cls.in_group(tg, chat)

    # ---- may they use it? (the server decides; a refused session is the answer)
    async def allowed(self, ident: Identity) -> bool:
        try:
            await self.gateway.session_token(ident)
            return True
        except (NotAllowed, NoGroup):
            return False

    async def _guard(self, update) -> bool:
        tg = update.effective_user
        if await self.allowed(self.private(tg)):
            return True
        target = update.effective_message or (update.callback_query and update.callback_query.message)
        if target:
            await target.reply_text("Sorry, this is a private bot. 🔒 Ask a friend who uses it to add you.")
        return False

    # ---- calling the server
    async def call(self, ident: Identity, tool: str, **args) -> ToolResult:
        return await self.gateway.call(ident, tool, args)

    @staticmethod
    def shown(res: ToolResult) -> str:
        """The text of a result for a human; a refusal ('❌ ...') of a button action reads as information."""
        return res.message

    # ---- asking the agent
    @staticmethod
    async def member_count(bot, chat) -> int | None:
        if chat.type == "private":
            return None
        try:
            return await bot.get_chat_member_count(chat.id)
        except TelegramError:
            return None

    async def _ask(self, tg: TgUser, chat, text: str, *, bot=None, from_button: bool = False) -> AgentReply:
        ident = self.ident_for(tg, chat)
        in_group = ident.chat_type == "group"
        thread = f"tg-g{chat.id}-{tg.id}" if in_group else f"tg-{tg.id}"
        count = await self.member_count(bot, chat) if (in_group and bot is not None) else None
        try:
            return await asyncio.wait_for(
                self.runner.chat(identity=ident, thread_id=thread, text=text, from_button=from_button, member_count=count),
                timeout=REPLY_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.error("agent timed out for telegram user %s", tg.id)
            return AgentReply("⏱️ The AI took too long to answer. Please send it again.")
        except McpError:
            log.exception("expense service unavailable for telegram user %s", tg.id)
            return AgentReply("🔌 I can't reach the expense service right now. Please try again in a moment.")
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
