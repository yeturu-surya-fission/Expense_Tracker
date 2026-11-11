"""AgentRunner: button flag UX, per-run session publishing, thread serialization."""
import pytest

from backend.agent.runner import AgentRunner
from backend.mcp_client.tools import PRIVATE_TOOL_NAMES, GROUP_TOOL_NAMES
from tests.conftest import FakeLLM, tool_call
from langchain_core.messages import AIMessage

from backend.agent.graph import build_graph


@pytest.fixture(autouse=True)
def _live_mcp(mcp_url):
    """Every runner test points at the live test server."""


def _runner(replies: list) -> AgentRunner:
    return AgentRunner(build_graph(FakeLLM(replies=replies)))


def _help_script() -> list:
    return [AIMessage(content="", tool_calls=[tool_call("respond_to_user", {"kind": "help"})])]


async def test_button_with_no_open_question_is_refused(make_user):
    uid, _ = await make_user()
    r = _runner([])  # the LLM is never reached
    reply = await r.chat(user_id=uid, thread_id=f"t{uid}", text="1", from_button=True)
    assert reply.text == "That question is no longer active."


async def test_button_after_a_question_flows_through(make_user):
    uid, _ = await make_user()
    b_uid, _ = await make_user("Surya", started=False)
    from backend.db.models import Friend
    from backend.db.session import session_scope
    async with session_scope() as s:
        s.add(Friend(owner_id=uid, friend_id=b_uid, nickname="Surya"))
    r = _runner([
        AIMessage(content="", tool_calls=[tool_call(
            "split_expense", {"amount": 300, "description": "dinner", "with_friends": ["Surya"]}, "c1")]),
        AIMessage(content="", tool_calls=[tool_call(
            "split_expense", {"amount": 300, "description": "dinner", "with_friends": ["Surya"],
                              "split": "equal"}, "c2")]),
    ])
    reply = await r.chat(user_id=uid, thread_id=f"t{uid}", text="split 300 for dinner")
    assert "Reply with" in reply.text and reply.choices, "a question must come back with choice buttons"
    assert f"t{uid}" in r._open_questions
    reply2 = await r.chat(user_id=uid, thread_id=f"t{uid}", text="equally", from_button=True)
    assert "Split #" in reply2.text  # the tapped answer drove the recall, not a refusal
    assert f"t{uid}" not in r._open_questions


async def test_chat_publishes_this_runs_tools_into_config(make_user, mcp_url):
    """The contract between runner and graph: one session, one allow-list, published per run."""
    uid, _ = await make_user()
    seen: list = []
    real_chat = AgentRunner.chat

    async def spy(self, *, user_id, thread_id, text, from_button=False, context=None):
        from backend.mcp_client.client import mcp_session
        from backend.mcp_client.tools import get_specs, build_tools
        async with mcp_session(user_id=user_id, chat_type="private") as client:
            tools, by_name = build_tools(await get_specs(client), client, chat_type="private")
            seen.append({t.name for t in tools})
        return await real_chat(self, user_id=user_id, thread_id=thread_id, text=text,
                               from_button=from_button, context=context)

    r = _runner(_help_script())
    r.chat = spy.__get__(r)
    await r.chat(user_id=uid, thread_id=f"t{uid}", text="hi")
    assert seen[-1] == PRIVATE_TOOL_NAMES


async def test_two_chats_get_fresh_sessions(make_user):
    """Nothing is cached between messages: the second chat must work with its own token."""
    uid, _ = await make_user()
    r = _runner([*_help_script(), *_help_script()])
    first = await r.chat(user_id=uid, thread_id=f"t{uid}", text="hi")
    second = await r.chat(user_id=uid, thread_id=f"t{uid}", text="again")
    assert "expense tracker" in first.text.lower()
    assert "expense tracker" in second.text.lower()