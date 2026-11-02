"""
LangGraph definition.

    START -> agent --(tool calls?)--> tools --(more pending?)--> tools
               ^                         |--(a tool errored)--> agent
               |                         +--(all ok)----------> respond -> END
    agent --(no tool calls)--> END

The agent never touches expense data. Every tool call is an HTTP request to the MCP server, made with a token for the
person who is chatting. When the server answers `input_required` (a question for the human), the graph pauses with
`interrupt()`; the human's answer is sent back on a fresh call of the SAME tool, so nothing was written while waiting.

After the last tool finishes, `respond` builds the reply straight from the tool output (no second LLM round-trip).
Only a tool *error* goes back to the LLM so it can retry.

The tools node executes ONE tool call per superstep. That keeps every step checkpointed, so a pause affects only that
call and a resume never repeats a finished write (the server also de-duplicates by idempotency key).
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
from langgraph.types import interrupt

from app.agent.prompts import GROUP_CHAT_CONTEXT, PRIVATE_CHAT_CONTEXT, SYSTEM_PROMPT
from app.agent.toolsets import GROUP_TOOLS, PRIVATE_TOOLS
from app.mcp_client.gateway import McpGateway
from app.mcp_client.types import Identity, ToolResult
from app.util.choices import is_cancel
from app.util.periods import now_local

log = logging.getLogger(__name__)
HISTORY_MESSAGES = 40
MAX_QUESTIONS_PER_CALL = 4  # a tool asks at most two; this only stops a misbehaving server from looping us


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


def identity_from(config: RunnableConfig) -> Identity:
    c = config["configurable"]
    return Identity(
        telegram_id=c["telegram_id"], name=c.get("name"), username=c.get("username"), chat_type=c.get("chat_type", "private"),
        group_chat_id=c.get("group_chat_id"), group_title=c.get("group_title"), started=bool(c.get("started")),
    )


def build_graph(llm: BaseChatModel, gateway: McpGateway, tool_defs: list[dict], checkpointer: BaseCheckpointSaver | None = None):
    by_name = {t["name"]: t for t in tool_defs}
    names = {"private": set(PRIVATE_TOOLS) & set(by_name), "group": set(GROUP_TOOLS) & set(by_name)}
    llm_private = llm.bind_tools([by_name[n] for n in PRIVATE_TOOLS if n in by_name])
    llm_group = llm.bind_tools([by_name[n] for n in GROUP_TOOLS if n in by_name])

    async def server_text(ident: Identity, kind: str) -> str:
        """Fixed wording (refusals, greeting) lives on the server too."""
        return (await gateway.call(ident, "respond_to_user", {"kind": kind})).message

    async def agent_node(state: AgentState, config: RunnableConfig):
        cfg = config["configurable"]
        ident, in_group = identity_from(config), cfg.get("chat_type") == "group"
        info = (await gateway.call(ident, "agent_context")).data
        categories, friend_names, chat_context = [], [], PRIVATE_CHAT_CONTEXT
        if in_group:
            people = ", ".join(info.get("members") or []) or "none yet"
            chat_context = GROUP_CHAT_CONTEXT.format(speaker=info.get("speaker", "someone"), group=info.get("group") or "(not created yet)", members=people)
        else:
            categories, friend_names = info.get("categories", []), info.get("friends", [])
        now = now_local()
        system = SystemMessage(
            SYSTEM_PROMPT.format(
                today=now.strftime("%A, %Y-%m-%d"),
                tz=str(now.tzinfo),
                symbol="₹",
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
            resp = AIMessage(content=await server_text(ident, "off_topic"))
        return {"messages": [resp]}

    async def run_tool(call: dict, ident: Identity, config: RunnableConfig, idem_base: str) -> ToolResult:
        """Call one tool; every question the server asks becomes a pause, and the answers go back on a fresh call."""
        cfg, answers = config["configurable"], {}
        for _ in range(MAX_QUESTIONS_PER_CALL + 1):
            res = await gateway.call(
                ident, call["name"], call["args"], answers=answers, idempotency_key=idem_base, member_count=cfg.get("member_count"),
            )
            if res.status != "input_required":
                return res
            req = res.input_requests[0]
            reply = interrupt({
                "type": "choice", "key": req["key"], "question": res.text or req["question"],
                "options": req["options"],
            })
            answers[req["key"]] = "cancel" if is_cancel(reply) and reply not in {o["id"] for o in req["options"]} else reply
        return ToolResult(status="error", error={"code": "too_many_questions", "message": "The server kept asking questions."})

    async def tools_node(state: AgentState, config: RunnableConfig):
        call = pending_tool_calls(state["messages"])[0]
        context = "group" if config["configurable"].get("chat_type") == "group" else "private"
        ident = identity_from(config)
        if call["name"] not in names[context]:
            msg = ToolMessage(f"Tool {call['name']} is not available in a {context} chat.", tool_call_id=call["id"], name=call["name"], status="error")
        else:
            thread = config["configurable"].get("thread_id", "")
            idem = f"{thread}:{_last_ai(state['messages']).id}:{call['id']}"
            try:
                res = await run_tool(call, ident, config, idem)
                msg = ToolMessage(
                    res.message, tool_call_id=call["id"], name=call["name"], artifact=res.data,
                    status="error" if res.status == "error" else "success",
                )
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
        data = [(m.name, m.artifact or {}) for m in tool_msgs if isinstance(m.artifact, dict)]
        extras = {
            "txn_ids": [i for n, d in data if n == "add_expenses" for i in d.get("expense_ids", [])],
            "split_ids": [i for n, d in data if n != "cancel_split" for i in d.get("split_ids", [])],
            "start_users": list({u["telegram_id"]: u for _, d in data for u in d.get("start_users", [])}.values()),
            "group_id": next((d["group_id"] for _, d in data if d.get("group_id")), None),
            "dues": next((d["dues"] for _, d in data if d.get("dues")), None),
        }
        return {"messages": [AIMessage(content="\n".join(lines).strip() or "Done.", additional_kwargs=extras)]}

    g = StateGraph(AgentState)
    g.add_node("agent", agent_node)
    g.add_node("tools", tools_node)
    g.add_node("respond", respond_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", after_agent, ["tools", END])
    g.add_conditional_edges("tools", after_tools, ["tools", "agent", "respond"])
    g.add_edge("respond", END)
    return g.compile(checkpointer=checkpointer)
