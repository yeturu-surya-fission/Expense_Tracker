"""
LangGraph definition.

    START -> agent --(tool calls?)--> tools --(more pending?)--> tools
               ^                         |--(a tool errored)--> agent
               |                         +--(all ok)----------> respond -> END
    agent --(no tool calls)--> END

The tools are MCP tools living in the mcp_server process; each run gets one MCP session (one
fresh JWT) opened by the runner and published into config["configurable"]["mcp_tools" /
"mcp_by_name"]. agent_node binds that run's tool list onto the LLM; tools_node executes calls
through the run's client. Invoking the graph without a runner (tests) falls back to a one-off
session.

The tools node still executes ONE tool call per superstep — that keeps every step checkpointed
so a crashed run never replays a finished write.

After the last tool finishes, `respond` builds the reply straight from the tool output (no
second LLM round-trip = much faster). Only a tool *error* goes back to the LLM so it can retry.
Questions are ask-then-recall inside the tool loop — no graph interrupts exist anymore.

Two chat contexts share this graph: a private chat (the user's own expenses, friends, splits) and a Telegram
group chat (only the shared-group tools). The context arrives in the run config.
"""
import logging
from typing import Annotated, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage, trim_messages
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from backend.agent.guardrails import SCOPE_MESSAGE, SERVICE_OFFLINE
from backend.agent.prompts import DEGRADED_SYSTEM_PROMPT, GROUP_CHAT_CONTEXT, PRIVATE_CHAT_CONTEXT, SYSTEM_PROMPT
from backend.config import settings
from backend.core import budgets, friends, groups, markers
from backend.core.periods import now_local
from backend.db.session import session_scope
from backend.mcp_client.client import mcp_session
from backend.mcp_client.tools import build_tools, get_specs

log = logging.getLogger(__name__)
HISTORY_MESSAGES = 40



class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def pending_tool_calls(messages: list[AnyMessage]) -> list[dict]:
    """Tool calls of the latest AI message that have no ToolMessage yet."""
    for idx in range(len(messages) - 1, -1, -1):
        if isinstance(messages[idx], AIMessage):
            break
    else:
        return []
    answered = {m.tool_call_id for m in messages[idx + 1:] if isinstance(m, ToolMessage)}
    return [c for c in messages[idx].tool_calls if c["id"] not in answered]


def _last_ai(messages: list[AnyMessage]) -> AIMessage:
    return next(m for m in reversed(messages) if isinstance(m, AIMessage))


def _turn_tool_messages(messages: list[AnyMessage]) -> list[ToolMessage]:
    """ToolMessages produced since the user's latest message."""
    out: list[ToolMessage] = []
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            break
        if isinstance(m, ToolMessage):
            out.append(m)
    return out[::-1]


def _chat_type(cfg: dict) -> str:
    return cfg.get("chat_type", "private")


async def _agent_tools(cfg: dict) -> list:
    """This run's tool list for binding the LLM (schemas only — no client needed)."""
    if cfg.get("mcp_tools") is not None:  # runner path: already published for this run
        return cfg["mcp_tools"]
    async with mcp_session(user_id=cfg["user_id"], chat_type=_chat_type(cfg),
                           group_chat_id=cfg.get("group_chat_id")) as client:
        specs = await get_specs(client)
        return build_tools(specs, None, chat_type=_chat_type(cfg))[0]


async def _run_tool(tool, call: dict, name: str, context: str, config: RunnableConfig) -> ToolMessage:
    if tool is None:  # hallucinated or wrong-context tool: the LLM sees this and corrects
        return ToolMessage(f"Tool {name} is not available in a {context} chat.",
                           tool_call_id=call["id"], name=name, status="error")
    try:
        return await tool.ainvoke({**call, "type": "tool_call"}, config)
    except Exception as e:  # let the LLM see the error and recover
        log.exception("tool %s failed", name)
        return ToolMessage(f"Tool error: {e}", tool_call_id=call["id"], name=name, status="error")


async def _call_tool(call: dict, cfg: dict, config: RunnableConfig) -> ToolMessage:
    """Execute one tool call through the run's MCP client (or a one-off session in tests)."""
    name, context = call["name"], _chat_type(cfg)
    by_name = cfg.get("mcp_by_name")
    if by_name is not None:
        return await _run_tool(by_name.get(name), call, name, context, config)
    # direct graph use (tests): the one-off session must stay open until the call finishes
    async with mcp_session(user_id=cfg["user_id"], chat_type=context,
                           group_chat_id=cfg.get("group_chat_id")) as client:
        specs = await get_specs(client)
        tool = build_tools(specs, client, chat_type=context)[1].get(name)
        return await _run_tool(tool, call, name, context, config)


def build_graph(llm: BaseChatModel, checkpointer: BaseCheckpointSaver | None = None):

    async def agent_node(state: AgentState, config: RunnableConfig):
        cfg = config["configurable"]
        user_id, in_group = cfg["user_id"], _chat_type(cfg) == "group"
        degraded = bool(cfg.get("mcp_unavailable"))
        now = now_local()

        if degraded:
            system = SystemMessage(
                DEGRADED_SYSTEM_PROMPT.format(
                    today=now.strftime("%A, %Y-%m-%d"),
                    tz=settings.timezone,
                    symbol=settings.currency_symbol,
                    chat_context=("You are in a Telegram group chat." if in_group
                                  else PRIVATE_CHAT_CONTEXT),
                )
            )
        else:
            categories, friend_names, chat_context = [], [], PRIVATE_CHAT_CONTEXT
            async with session_scope() as s:
                if in_group:
                    group = await groups.group_for_chat(s, cfg.get("group_chat_id"))
                    speaker = friends.display(await friends.get_user(s, user_id))
                    if group:
                        people = ", ".join(friends.display(u) for u in await groups.members(s, group.id))
                        chat_context = GROUP_CHAT_CONTEXT.format(speaker=speaker, group=group.name, members=people or "none yet")
                    else:
                        chat_context = GROUP_CHAT_CONTEXT.format(speaker=speaker, group="(not created yet)", members="none")
                else:
                    categories = await budgets.budget_categories(s, user_id)
                    friend_names = [f.nickname for f, _ in await friends.list_friends(s, user_id)]
            system = SystemMessage(
                SYSTEM_PROMPT.format(
                    today=now.strftime("%A, %Y-%m-%d"),
                    tz=settings.timezone,
                    symbol=settings.currency_symbol,
                    categories=", ".join(categories) or "none yet",
                    friends=", ".join(friend_names) or "none yet",
                    chat_context=chat_context,
                )
            )

        recent = trim_messages(
            state["messages"], strategy="last", max_tokens=HISTORY_MESSAGES, token_counter=len,
            start_on="human", include_system=False, allow_partial=False,
        )
        tools = await _agent_tools(cfg)  # per-run binding: the graph compiles ONCE, tools bind per RUN
        model = llm.bind_tools(tools) if tools else llm
        resp = await model.ainvoke([system, *recent], config)

        if degraded:
            if resp.tool_calls:  # no tools are bound, but never execute a stray tool call
                resp = AIMessage(content=SERVICE_OFFLINE)
        elif not resp.tool_calls:
            # Guardrail: the model must act through tools. Free text from it is never forwarded
            # (that is where coding / general-knowledge answers would leak out).
            log.info("guardrail: dropped free-text model reply")
            resp = AIMessage(content=SCOPE_MESSAGE)
        return {"messages": [resp]}

    async def tools_node(state: AgentState, config: RunnableConfig):
        call = pending_tool_calls(state["messages"])[0]
        msg = await _call_tool(call, config["configurable"], config)
        return {"messages": [msg]}

    def after_agent(state: AgentState):
        return "tools" if pending_tool_calls(state["messages"]) else END

    def after_tools(state: AgentState):
        msgs = state["messages"]
        if pending_tool_calls(msgs):
            return "tools"
        latest = [m for m in _turn_tool_messages(msgs) if m.tool_call_id in {c["id"] for c in _last_ai(msgs).tool_calls}]
        return "agent" if any(m.status == "error" for m in latest) else "respond"

    async def respond_node(state: AgentState):
        tool_msgs = [m for m in _turn_tool_messages(state["messages"]) if m.status != "error"]
        lines: list[str] = []
        for m in tool_msgs:
            for line in str(m.content).splitlines():
                if line.strip():
                    if not lines or lines[-1] != line:
                        lines.append(line)
                elif lines and lines[-1].strip():  # keep single blank lines (layout)
                    lines.append("")
        raw = "\n".join(lines).strip() or "Done."
        found = markers.extract(raw, expenses_from="\n".join(str(m.content) for m in tool_msgs if m.name == "add_expenses"))
        extras = {"txn_ids": found.txn_ids, "split_ids": found.split_ids, "start_ids": found.start_ids,
                  "choices": found.choices}
        return {"messages": [AIMessage(content=found.text, additional_kwargs=extras)]}

    g = StateGraph(AgentState)
    g.add_node("agent", agent_node)
    g.add_node("tools", tools_node)
    g.add_node("respond", respond_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", after_agent, ["tools", END])
    g.add_conditional_edges("tools", after_tools, ["tools", "agent", "respond"])
    g.add_edge("respond", END)
    return g.compile(checkpointer=checkpointer)
