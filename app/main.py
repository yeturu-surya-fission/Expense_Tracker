import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from telegram import BotCommandScopeAllGroupChats
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.agent.graph import build_graph
from app.agent.llm import get_llm
from app.agent.runner import AgentRunner
from app.api.routes import router
from app.bot.app import COMMANDS, GROUP_COMMANDS, build_telegram_app
from app.bot.notifier import TelegramNotifier
from app.bot.reminders import reminder_loop
from app.config import settings
from app.core.notifier import set_notifier
from app.db.session import engine, init_db

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # its URLs contain the bot token
log = logging.getLogger("wall")


@asynccontextmanager
async def lifespan(app: FastAPI):
    llm = get_llm()  # fail fast on a bad/missing key
    await init_db()

    # LangGraph state (conversations + paused human-in-the-loop questions) lives in Postgres too
    pool = AsyncConnectionPool(
        settings.checkpointer_url, max_size=10, open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open()
    saver = AsyncPostgresSaver(pool)
    await saver.setup()

    runner = AgentRunner(build_graph(llm, saver))
    tg = build_telegram_app(runner, llm)
    await tg.initialize()
    await tg.start()
    await tg.bot.set_my_commands(COMMANDS)  # the '/' menu in Telegram
    await tg.bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())  # and inside groups
    settings.bot_username = tg.bot.username or ""  # used to build invite / start links
    set_notifier(TelegramNotifier(tg.bot))  # how the core messages friends privately
    reminders = asyncio.create_task(reminder_loop())  # nudges about unpaid shares
    if settings.telegram_mode == "polling":
        await tg.bot.delete_webhook()
        await tg.updater.start_polling(allowed_updates=["message", "callback_query", "my_chat_member"])
        log.info("Telegram: long polling")
    else:
        await tg.bot.set_webhook(
            f"{settings.public_url.rstrip('/')}/telegram/webhook",
            secret_token=settings.telegram_webhook_secret,
            allowed_updates=["message", "callback_query", "my_chat_member"],
        )
        log.info("Telegram: webhook %s", settings.public_url)
    app.state.telegram = tg

    yield

    reminders.cancel()
    if tg.updater and tg.updater.running:
        await tg.updater.stop()
    await tg.stop()
    await tg.shutdown()
    await pool.close()
    await engine.dispose()


app = FastAPI(title="Expense Tracker", lifespan=lifespan)
app.include_router(router)
