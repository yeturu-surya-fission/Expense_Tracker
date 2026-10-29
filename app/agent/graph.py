"""
LangGraph definition.

    START -> agent --(tool calls?)--> tools --(more pending?)--> tools
               ^                         |--(a tool errored)--> agent
               |                         +--(all ok)----------> respond -> END
    agent --(no tool calls)--> END

After the last tool finishes, `respond` builds the reply straight from the tool output (no second
LLM round-trip = much faster). Only a tool *error* goes back to the LLM so it can retry.

The tools node executes ONE tool call per superstep. That keeps every step checkpointed, so a
human-in-the-loop `interrupt()` pauses only that call and a resume never repeats a finished write.

Two chat contexts share this graph: a private chat (the user's own expenses, friends, splits) and a Telegram
group chat (only the shared-group tools). The context arrives in the run config.
"""
import logging
from typing import Annotated, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage, trim_messages
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.agent.guardrails import SCOPE_MESSAGE
from app.agent.prompts import GROUP_CHAT_CONTEXT, PRIVATE_CHAT_CONTEXT, SYSTEM_PROMPT
from app.config import settings
from app.core import budgets, friends, groups, markers
from app.core.periods import now_local
from app.db.session import session_scope
from app.tools import GROUP_TOOLS, PRIVATE_TOOLS, TOOLS_BY_NAME

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


def build_graph(llm: BaseChatModel, checkpointer: BaseCheckpointSaver | None = None):
    llm_private = llm.bind_tools(PRIVATE_TOOLS)
    llm_group = llm.bind_tools(GROUP_TOOLS)
    allowed = {"private": {t.name for t in PRIVATE_TOOLS}, "group": {t.name for t in GROUP_TOOLS}}

    async def agent_node(state: AgentState, config: RunnableConfig):
        cfg = config["configurable"]
        user_id, in_group = cfg["user_id"], cfg.get("chat_type") == "group"
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
        now = now_local()
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
        resp = await (llm_group if in_group else llm_private).ainvoke([system, *recent], config)
        if not resp.tool_calls:
            # Guardrail: the model must act through tools. Free text from it is never forwarded
            # (that is where coding / general-knowledge answers would leak out).
            log.info("guardrail: dropped free-text model reply")
            resp = AIMessage(content=SCOPE_MESSAGE)
        return {"messages": [resp]}

    async def tools_node(state: AgentState, config: RunnableConfig):
        call = pending_tool_calls(state["messages"])[0]
        context = "group" if config["configurable"].get("chat_type") == "group" else "private"
        tool = TOOLS_BY_NAME.get(call["name"])
        if tool is None or call["name"] not in allowed[context]:
            msg = ToolMessage(f"Tool {call['name']} is not available in a {context} chat.", tool_call_id=call["id"], name=call["name"], status="error")
        else:
            try:
                msg = await tool.ainvoke({**call, "type": "tool_call"}, config)
            except GraphBubbleUp:  # interrupt() - must propagate
                raise
            except Exception as e:  # let the LLM see the error and recover
                log.exception("tool %s failed", call["name"])
                msg = ToolMessage(f"Tool error: {e}", tool_call_id=call["id"], name=call["name"], status="error")
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
        extras = {"txn_ids": found.txn_ids, "split_ids": found.split_ids, "start_ids": found.start_ids}
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
