"""The graph with a scripted FakeLLM: guardrail, tool roundtrip, error retry, group allow-list,
marker plumbing — all through the live MCP server like production."""
import pytest

from backend.agent.graph import build_graph
from backend.agent.runner import AgentRunner
from backend.agent.guardrails import SCOPE_MESSAGE
from tests.conftest import FakeLLM, tool_call
from langchain_core.messages import AIMessage


@pytest.fixture(autouse=True)
def _live_mcp(mcp_url):
    """Every graph test runs against the live test server (and after its init_db migrations)."""


@pytest.fixture
def runner(make_user):
    def _make(replies: list) -> tuple[AgentRunner, FakeLLM]:
        llm = FakeLLM(replies=replies)
        graph = build_graph(llm)
        return AgentRunner(graph), llm
    return _make


async def test_tool_roundtrip_reaches_mcp_and_responds(make_user, runner):
    uid, _ = await make_user()
    r, llm = runner([AIMessage(content="", tool_calls=[tool_call("respond_to_user", {"kind": "help"})])])
    reply = await r.chat(user_id=uid, thread_id=f"t{uid}", text="help me")
    assert "expense tracker" in reply.text.lower()
    assert llm.calls == 1
    assert reply.choices == [] and reply.txn_ids == []


async def test_guardrail_drops_free_text(make_user, runner):
    uid, _ = await make_user()
    r, llm = runner([AIMessage(content="Sure, let me explain how python works")])
    reply = await r.chat(user_id=uid, thread_id=f"t{uid}", text="write me code")
    assert reply.text == SCOPE_MESSAGE
    assert llm.calls == 1  # no second round-trip: the guardrail answers


async def test_tool_error_sends_the_agent_back(make_user, runner):
    uid, _ = await make_user()
    r, llm = runner([
        AIMessage(content="", tool_calls=[tool_call("no_such_tool", {}, "c1")]),
        AIMessage(content="", tool_calls=[tool_call("respond_to_user", {"kind": "help"}, "c2")]),
    ])
    reply = await r.chat(user_id=uid, thread_id=f"t{uid}", text="do a thing")
    assert "expense tracker" in reply.text.lower()
    assert llm.calls == 2  # error ToolMessage went back to the LLM for the retry


async def test_group_chat_rejects_private_tools(make_user, runner):
    uid, _ = await make_user()
    r, llm = runner([
        AIMessage(content="", tool_calls=[tool_call("add_expenses", {"items": []}, "c1")]),
        AIMessage(content="", tool_calls=[tool_call("respond_to_user", {"kind": "help"}, "c2")]),
    ])
    reply = await r.chat(user_id=uid, thread_id=f"t{uid}", text="spent 5",
                         context={"chat_type": "group", "group_chat_id": -777})
    assert "expense tracker" in reply.text.lower()
    assert llm.calls == 2  # the rejected tool produced an error ToolMessage and the loop retried


async def test_txn_markers_reach_the_reply(make_user, runner):
    uid, _ = await make_user()
    r, _ = runner([AIMessage(content="", tool_calls=[tool_call(
        "add_expenses", {"items": [{"description": "coffee", "amount": 12, "category": "food"}]}, "c1")])])
    reply = await r.chat(user_id=uid, thread_id=f"t{uid}", text="spent 12 on coffee")
    assert reply.txn_ids, "respond_node must lift (#id) markers into txn_ids for the Undo button"
    assert "(split #" not in reply.text and "(start #" not in reply.text  # those ARE hidden; (#id) stays (Undo button)
