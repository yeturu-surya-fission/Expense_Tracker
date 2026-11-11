"""When the MCP server is down, the LLM still answers; expense actions report offline."""
from langchain_core.messages import AIMessage

from backend.agent.graph import build_graph
from backend.agent.runner import AgentRunner, is_mcp_unreachable
from tests.conftest import FakeLLM


def test_mcp_connect_errors_are_recognized():
    assert is_mcp_unreachable(RuntimeError("Client failed to connect: All connection attempts failed"))
    assert is_mcp_unreachable(RuntimeError("httpx2.ConnectError: All connection attempts failed"))
    assert not is_mcp_unreachable(RuntimeError("tool failed"))


async def test_mcp_down_still_answers_free_text(monkeypatch):
    def unreachable(**_kwargs):
        raise RuntimeError("Client failed to connect: All connection attempts failed")

    monkeypatch.setattr("backend.agent.runner.mcp_session", unreachable)
    llm = FakeLLM(replies=[AIMessage(content="4")])
    runner = AgentRunner(build_graph(llm))

    reply = await runner.chat(user_id=1, thread_id="degraded-math", text="What is 2 + 2?")

    assert reply.text == "4"
    assert llm.calls == 1


async def test_mcp_down_does_not_execute_tool_calls(monkeypatch):
    def unreachable(**_kwargs):
        raise RuntimeError("Client failed to connect: All connection attempts failed")

    monkeypatch.setattr("backend.agent.runner.mcp_session", unreachable)
    llm = FakeLLM(replies=[AIMessage(
        content="",
        tool_calls=[{"name": "spending_summary", "args": {}, "id": "c1", "type": "tool_call"}],
    )])
    runner = AgentRunner(build_graph(llm))

    reply = await runner.chat(user_id=1, thread_id="degraded-tool", text="how much did I spend?")

    assert "temporarily offline" in reply.text.lower()
    assert llm.calls == 1
