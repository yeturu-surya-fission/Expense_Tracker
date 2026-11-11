import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from telegram import BotCommandScopeAllGroupChats

from app.agent.graph import build_graph
from app.agent.llm import get_llm
from app.agent.runner import AgentRunner
from app.api.routes import router
from app.bot.app import COMMANDS, GROUP_COMMANDS, build_telegram_app
from app.bot.delivery import OutboxDeliverer, delivery_loop
from app.config import settings
from app.mcp_client.gateway import McpGateway
from app.mcp_client.types import McpError

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # its URLs contain the bot token
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpcore2").setLevel(logging.WARNING)
log = logging.getLogger("bot")


async def load_tools(gateway: McpGateway, attempts: int = 30) -> list[dict]:
    """The MCP server may still be starting (docker compose): wait for it instead of crashing."""
    for i in range(1, attempts + 1):
        try:
            return await gateway.list_tool_definitions()
        except McpError as e:
            log.warning("waiting for the MCP server (%s/%s): %s", i, attempts, e)
            await asyncio.sleep(2)
    raise RuntimeError(f"The MCP server at {settings.mcp_server_url} is not reachable. Is it running, and are MCP_BOT_CLIENT_* set?")


@asynccontextmanager
async def lifespan(app: FastAPI):
    llm = get_llm()  # fail fast on a bad/missing key
    gateway = McpGateway(
        settings.mcp_server_url, settings.auth_base_url, settings.mcp_bot_client_id, settings.mcp_bot_client_secret,
        timeout=settings.mcp_timeout_seconds,
    )
    tool_defs = await load_tools(gateway)
    log.info("MCP server ready: %s tools available to the agent", len(tool_defs))

    # LangGraph state (conversations + paused human-in-the-loop questions) lives in the bot's own Postgres
    pool = AsyncConnectionPool(
        settings.checkpointer_url, max_size=10, open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open()
    saver = AsyncPostgresSaver(pool)
    await saver.setup()

    runner = AgentRunner(build_graph(llm, gateway, tool_defs, saver))
    tg = build_telegram_app(runner, llm, gateway)
    await tg.initialize()
    await tg.start()
    await tg.bot.set_my_commands(COMMANDS)  # the '/' menu in Telegram
    await tg.bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())  # and inside groups
    settings.bot_username = tg.bot.username or ""  # the server builds invite / start links from it
    deliverer = asyncio.create_task(delivery_loop(OutboxDeliverer(tg.bot, gateway)))  # messages the server queued for people
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

    deliverer.cancel()
    if tg.updater and tg.updater.running:
        await tg.updater.stop()
    await tg.stop()
    await tg.shutdown()
    await pool.close()
    await gateway.close()


app = FastAPI(title="Expense Tracker bot", lifespan=lifespan)
app.include_router(router)
