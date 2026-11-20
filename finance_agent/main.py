"""The Finance Agent web app (uvicorn finance_agent.main:app): the A2A endpoint, the agent card, and the manager's Telegram bot."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from finance_agent.a2a import notify
from finance_agent.a2a.auth import TokenVerifier
from finance_agent.a2a.server import mount_a2a
from finance_agent.config import settings
from finance_agent.db.session import engine, init_db
from finance_agent.llm import LLM, LazyLLM
from finance_agent.manager.service import ManagerService

log = logging.getLogger("finance_agent")


def create_app(
    llm: LLM | None = None, verifier: TokenVerifier | None = None
) -> FastAPI:
    """`llm` and `verifier` can be replaced (tests do); by default the CodeVector model and the tracker's published keys are used."""
    llm = llm or LazyLLM()
    app = FastAPI(title="Finance Agent", docs_url=None, redoc_url=None)
    task_store = mount_a2a(app, llm, verifier)
    app.state.task_store, app.state.llm = task_store, llm

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @asynccontextmanager
    async def lifespan(_app):
        logging.basicConfig(
            level=settings.log_level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        if not settings.webhook_secret:
            raise RuntimeError(
                "FINANCE_WEBHOOK_SECRET is required (a long random string shared with the expense tracker's MCP_FINANCE_WEBHOOK_SECRET)"
            )
        await init_db()
        await task_store.initialize()
        background = [asyncio.create_task(notify.webhook_loop())]
        telegram = None
        if settings.telegram_bot_token:
            from finance_agent.manager.bot import (
                MANAGER_COMMANDS,
                build_application,
                notifier_loop,
            )

            telegram = build_application(ManagerService(llm))
            await telegram.initialize()
            await telegram.start()
            await telegram.bot.set_my_commands(
                MANAGER_COMMANDS
            )  # the "/" menu in the manager's chat
            await telegram.updater.start_polling(drop_pending_updates=False)
            background.append(asyncio.create_task(notifier_loop(telegram.bot)))
            log.info("manager bot is running")
        else:
            log.warning(
                "FINANCE_TELEGRAM_BOT_TOKEN is empty: the manager bot is OFF (claims can't reach a manager)"
            )
        try:
            yield
        finally:
            for t in background:
                t.cancel()
            if telegram is not None:
                await telegram.updater.stop()
                await telegram.stop()
                await telegram.shutdown()
            await engine.dispose()

    app.router.lifespan_context = lifespan
    return app


app = create_app()
