import asyncio
import os
import sys

if sys.platform == "win32":  # psycopg async (the bot's checkpointer) cannot run on the default Proactor loop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# The MCP server reads its settings when imported, so the test configuration is set first.
os.environ["MCP_DATABASE_URL"] = os.environ.get("TEST_MCP_DATABASE_URL", "postgresql+asyncpg://expense:expense@localhost:5432/expense_mcp_test")
os.environ["MCP_BOT_CLIENT_SECRET"] = "test-bot-secret"
os.environ["MCP_PUBLIC_URL"] = "http://testserver"
os.environ["MCP_REMINDERS_ENABLED"] = "false"
os.environ["MCP_ENV"] = "dev"
os.environ["MCP_ALLOWED_TELEGRAM_IDS"] = ""
os.environ.update({"MCP_CURRENCY_SYMBOL": "₹", "MCP_TIMEZONE": "Asia/Kolkata", "MCP_BUDGET_ALERT_THRESHOLDS": "50,80,100",
                   "MCP_REMINDER_EVERY_HOURS": "48", "MCP_ASK_GROUP_MIN_PEOPLE": "4"})  # whatever the developer's own .env says

import pytest  # noqa: E402

from tests.harness import Harness  # noqa: E402


@pytest.fixture(scope="session")
async def harness():
    from mcp_server.server import create_app

    h = Harness(create_app())
    await h.start("test-bot-secret")
    yield h
    await h.stop()


@pytest.fixture
async def alice(harness):
    return await harness.person("Alice")


@pytest.fixture
async def bob(harness):
    return await harness.person("Bob")
