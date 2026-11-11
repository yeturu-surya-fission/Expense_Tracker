"""Shared fixtures: the real MCP server on a free port, disposable test users, FakeLLM."""
import asyncio
import itertools
import socket
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
import uvicorn
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import delete, select

from backend.config import settings
from backend.db.models import Debt, Group, SharedExpense, User
from backend.db.session import init_db, session_scope
from backend.mcp_client.client import mcp_session
from backend.mcp_client.tools import build_tools, get_specs

_TG_BASE = 990_000_000_000  # test-user telegram ids live at/above this


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
async def mcp_url():
    """The real MCP server (401 gate, claims middleware, Postgres) on a random free port.

    It runs as a task on the pytest session loop — the app and the tests then share ONE
    event loop, so the shared async engine never sees connections from two loops.
    settings.mcp_server_url is pointed at it so mcp_session(), AgentRunner and the graph
    fallback all talk to THIS server for the whole test session."""
    from mcp_server.server import app

    await init_db()  # create_all + the post-create ALTER migrations (test users need every column)
    # self-healing: crashed runs leave their 990-range users behind (teardown never ran)
    async with session_scope() as s:
        stale = (await s.execute(select(User).where(User.telegram_id >= _TG_BASE))).scalars().all()
        stale_ids = [u.id for u in stale]
        if stale_ids:
            await s.execute(delete(Debt).where(Debt.debtor_id.in_(stale_ids) | Debt.creditor_id.in_(stale_ids)))
            await s.execute(delete(SharedExpense).where(
                SharedExpense.payer_id.in_(stale_ids) | SharedExpense.created_by.in_(stale_ids)))
            await s.execute(delete(Group).where(Group.created_by.in_(stale_ids)))
            await s.execute(delete(User).where(User.id.in_(stale_ids)))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    async with httpx.AsyncClient() as http:
        while time.monotonic() < deadline:
            try:
                if (await http.get(f"{base}/health", timeout=2)).status_code == 200:
                    break
            except Exception:
                await asyncio.sleep(0.2)
        else:
            raise RuntimeError("mcp server did not come up")
    settings.mcp_server_url = f"{base}/mcp"
    yield f"{base}/mcp"
    server.should_exit = True
    await task


_user_seq = itertools.count(990001)


@pytest.fixture
async def make_user():
    """Creates disposable users. Telegram ids start at 990_000_000_001 (outside real ranges);
    users + their groups are deleted on teardown, everything else goes via ON DELETE CASCADE."""
    created: list[int] = []

    async def _make(name: str = "Tester", started: bool = True) -> tuple[int, int]:
        tg = _TG_BASE + next(_user_seq)
        async with session_scope() as s:
            s.add(User(telegram_id=tg, name=name,
                       started_at=datetime.now(UTC) if started else None))
        async with session_scope() as s:
            user = (await s.execute(select(User).where(User.telegram_id == tg))).scalar_one()
            created.append(user.id)
            return user.id, tg

    yield _make

    async with session_scope() as s:
        # these FKs have no ON DELETE CASCADE: debts -> shared_expenses -> groups -> users
        await s.execute(delete(Debt).where(Debt.debtor_id.in_(created) | Debt.creditor_id.in_(created)))
        await s.execute(delete(SharedExpense).where(
            SharedExpense.payer_id.in_(created) | SharedExpense.created_by.in_(created)))
        await s.execute(delete(Group).where(Group.created_by.in_(created)))
        await s.execute(delete(User).where(User.id.in_(created)))


@asynccontextmanager
async def _bound_tools(user_id: int, chat_type: str = "private", group_chat_id: int | None = None):
    """The same per-run tool binding AgentRunner.chat does: one session, one token, one allow-list."""
    async with mcp_session(user_id=user_id, chat_type=chat_type, group_chat_id=group_chat_id) as client:
        specs = await get_specs(client)
        _, by_name = build_tools(specs, client, chat_type=chat_type)
        yield by_name


@pytest.fixture
def tools_for(mcp_url):
    """`async with tools_for(uid) as t:` / `tools_for(uid, 'group', -999001)` -> tools by name."""
    return _bound_tools


class FakeLLM(BaseChatModel):
    """Scripted LLM for graph tests: each invocation pops the next AIMessage. bind_tools is a
    no-op because the graph binds the run's tools itself; the fake only feeds the script."""

    replies: list
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=self.replies.pop(0))])


def tool_call(name: str, args: dict, call_id: str = "call1") -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}
