import itertools

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver

from app.agent.graph import build_graph
from app.agent.runner import AgentRunner
from app.mcp_client.gateway import McpGateway
from app.mcp_client.types import Identity

_call_ids = itertools.count(1)


class ScriptedLLM(BaseChatModel):
    """Plays back pre-written AI messages so the graph can be tested without a real model."""

    script: list = []
    idx: int = 0
    seen_tools: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        self.seen_tools.append([t["name"] if isinstance(t, dict) else t.name for t in tools])
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        msg = self.script[self.idx]
        self.idx += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])


def tool_call(name: str, args: dict, id_: str | None = None) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id_ or f"call_{next(_call_ids)}", "type": "tool_call"}])


def tool_calls(*calls: tuple[str, dict]) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"call_{next(_call_ids)}", "type": "tool_call"} for n, a in calls])


@pytest.fixture(scope="session")
async def gateway(harness):
    """The bot's real MCP client, wired to the in-process server (HTTP semantics, no sockets)."""
    gw = McpGateway("http://testserver/mcp", "http://testserver", "telegram-bot", "test-bot-secret", asgi_app=harness.app)
    yield gw
    await gw.close()


@pytest.fixture(scope="session")
async def tool_defs(gateway):
    return await gateway.list_tool_definitions()


@pytest.fixture
def make_runner(gateway, tool_defs):
    def _make(*script: AIMessage) -> AgentRunner:
        return AgentRunner(build_graph(ScriptedLLM(script=list(script)), gateway, tool_defs, MemorySaver()))
    return _make


@pytest.fixture
def ident():
    from tests.harness import new_telegram_id

    def _ident(name="Kiran", *, group=None, title=None, telegram_id=None) -> Identity:
        return Identity(telegram_id=telegram_id or new_telegram_id(), name=name, chat_type="group" if group else "private",
                        group_chat_id=group, group_title=title, started=not group)
    return _ident
