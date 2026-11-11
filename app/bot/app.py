import logging
import re

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, MessageHandler, filters

from app.agent.runner import AgentRunner
from app.bot.handlers import BotHandlers
from app.bot.keyboards import MENU
from app.config import settings
from app.mcp_client.gateway import McpGateway
from app.mcp_client.types import McpError

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand("summary", "📊 Spending summary (browse months)"),
    BotCommand("recent", "🧾 Latest expenses"),
    BotCommand("budgets", "🎯 Budget progress"),
    BotCommand("friends", "👥 My friends"),
    BotCommand("dues", "💳 Who owes whom"),
    BotCommand("invite", "🔗 Invite link for a friend"),
    BotCommand("daily", "📅 Day-by-day spending"),
    BotCommand("trend", "📈 Month-by-month trend"),
    BotCommand("undo", "↩️ Undo last expense"),
    BotCommand("help", "❓ What can I say?"),
]


GROUP_COMMANDS = [
    BotCommand("group", "🧳 Who paid what and who is owed"),
    BotCommand("settle", "✅ Settle up the group"),
    BotCommand("help", "❓ How to use me here"),
]


def build_telegram_app(runner: AgentRunner, llm, gateway: McpGateway) -> Application:
    token = settings.telegram_bot_token.strip().strip("\"'")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty. Create a bot with @BotFather and put the token in .env")
    if not re.fullmatch(r"\d+:[\w-]{30,}", token):
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN looks malformed. It must be '<digits>:<secret>', e.g. 1234567890:AAE... "
            "Copy the full token from @BotFather (/mybots -> API Token)."
        )
    h = BotHandlers(runner, llm, gateway)
    private, groups = filters.ChatType.PRIVATE, filters.ChatType.GROUPS
    # concurrent_updates: one user's slow LLM call must not block everybody else
    app = Application.builder().token(token).concurrent_updates(True).build()

    # --- private chat
    app.add_handler(CommandHandler("start", h.start, filters=private))
    app.add_handler(CommandHandler("help", h.help, filters=private))
    app.add_handler(CommandHandler(["summary", "recent", "budgets", "friends", "dues", "invite", "daily", "trend", "undo"], h.on_command, filters=private))
    menu_regex = "^(" + "|".join(re.escape(label) for label in MENU) + ")$"
    app.add_handler(MessageHandler(private & filters.Regex(menu_regex), h.on_menu))
    app.add_handler(MessageHandler(private & filters.Document.ALL, h.on_document))
    app.add_handler(MessageHandler(private & filters.PHOTO, h.on_other_file))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, h.on_text))

    # --- group chats: set up when the bot is added; answers only when mentioned, replied to, or given a /command
    app.add_handler(ChatMemberHandler(h.on_bot_membership, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(groups & filters.StatusUpdate.NEW_CHAT_MEMBERS, h.on_new_members))
    app.add_handler(MessageHandler(groups & filters.StatusUpdate.LEFT_CHAT_MEMBER, h.on_left_member))
    app.add_handler(MessageHandler(groups & filters.StatusUpdate.NEW_CHAT_TITLE, h.on_title_change))
    app.add_handler(MessageHandler(groups & filters.StatusUpdate.MIGRATE, h.on_migrate))
    app.add_handler(CommandHandler(["start", "help"], h.group_help, filters=groups))
    app.add_handler(CommandHandler("group", h.cmd_group, filters=groups))
    app.add_handler(CommandHandler("settle", h.cmd_settle, filters=groups))
    app.add_handler(MessageHandler(groups & filters.TEXT & ~filters.COMMAND, h.on_group_text))

    # --- buttons everywhere
    app.add_handler(CallbackQueryHandler(h.on_callback))
    app.add_error_handler(on_error)
    return app


async def on_error(update, context) -> None:
    """Anything a handler did not catch: tell the person instead of staying silent."""
    log.error("handler failed", exc_info=context.error)
    text = ("🔌 I can't reach the expense service right now. Please try again in a moment."
            if isinstance(context.error, McpError) else "😵 Sorry, something went wrong. Please try again.")
    target = getattr(update, "effective_message", None) or (getattr(update, "callback_query", None) and update.callback_query.message)
    if target is not None:
        try:
            await target.reply_text(text)
        except Exception:
            pass
