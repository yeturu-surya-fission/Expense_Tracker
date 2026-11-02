"""A question to the human survives a bot restart: the paused state lives in the bot's own Postgres, the answer goes to MCP."""
import os

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import func, select

from app.agent.graph import build_graph
from app.agent.runner import AgentRunner
from mcp_server.db.models import Debt
from mcp_server.db.session import session_scope
from tests.bot.conftest import ScriptedLLM, tool_call

BOT_DSN = os.environ.get("TEST_BOT_DATABASE_URL", "postgresql://expense:expense@localhost:5432/expense_bot_test")


@pytest.fixture
async def saver():
    pool = AsyncConnectionPool(BOT_DSN, max_size=4, open=False, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
    await pool.open()
    s = AsyncPostgresSaver(pool)
    await s.setup()
    yield s
    await pool.close()


async def test_paused_question_survives_a_restart(harness, gateway, tool_defs, saver, ident):
    me = ident("Kiran")
    owner = await harness.person("Kiran", telegram_id=me.telegram_id)
    bob = await harness.person("Bob")
    await owner.ok("add_friend", nickname="Bob", telegram_id=bob.telegram_id)
    script = [tool_call("split_expense", {"amount": 900, "description": "dinner", "with_friends": ["Bob"]})]
    thread = f"persist-{me.telegram_id}"

    before = AgentRunner(build_graph(ScriptedLLM(script=list(script)), gateway, tool_defs, saver))
    ask = await before.chat(identity=me, thread_id=thread, text="dinner 900 split with Bob")
    assert ask.choices and "How should I split" in ask.text

    # "restart": a brand-new runner and graph, same database, and an LLM that must not even be needed
    after = AgentRunner(build_graph(ScriptedLLM(script=[]), gateway, tool_defs, saver))
    done = await after.chat(identity=me, thread_id=thread, text="equal", from_button=True)
    assert done.split_ids and "Bob owes ₹450" in done.text
    async with session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(Debt).where(Debt.creditor_id == owner.user_id))).scalar_one() == 1
