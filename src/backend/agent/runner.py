"""
AgentRunner: the single entry point the bot uses.

One conversation thread per person per chat. Each chat() opens its own MCP session — one fresh
JWT that dies with the run — and publishes the bound tools into the graph config.

Questions are ask-then-recall inside the LLM loop, so there is no interrupt machinery here
anymore. The runner only remembers whether the last reply of a thread carried choice buttons;
a button press on a thread with no open question is politely refused instead of being fed to
the model as a phantom answer. Non-answer text to an open question is handled by the LLM loop
itself (it re-asks or treats the message as a fresh request).
"""
import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage

from backend.mcp_client.client import mcp_session
from backend.mcp_client.tools import build_tools, get_specs

log = logging.getLogger(__name__)


@dataclass
class AgentReply:
    text: str
    choices: list[dict] = field(default_factory=list)  # non-empty => ask the user to pick one: [{"id","label"}]
    txn_ids: list[int] = field(default_factory=list)  # expenses just recorded (Undo button)
    split_ids: list[int] = field(default_factory=list)  # splits just created (Cancel-split button)
    start_ids: list[int] = field(default_factory=list)  # telegram ids of people who still need to press Start


def is_rate_limit(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "ratelimit" in text or "resource_exhausted" in text or "429" in text or "quota" in text


def is_mcp_unreachable(exc: BaseException) -> bool:
    """True when the MCP server could not be reached (as opposed to a tool or LLM error)."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(
        marker in text
        for marker in (
            "connecterror",
            "connecttimeout",
            "connection attempts failed",
            "failed to connect",
            "connection refused",
        )
    )


def message_text(msg) -> str:
    content = msg.content
    if isinstance(content, list):  # OpenAI-compatible gateways may return content blocks
        content = "".join(p if isinstance(p, str) else p.get("text", "") for p in content)
    return (content or "").strip()


class AgentRunner:
    def __init__(self, graph):
        self.graph = graph
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)  # single-process guard
        self._open_questions: set[str] = set()  # threads whose last reply showed choice buttons

    async def chat(
        self, *, user_id: int, thread_id: str, text: str, from_button: bool = False, context: dict | None = None
    ) -> AgentReply:
        """`context` = {"chat_type": "private" | "group", "group_chat_id": int | None}."""
        configurable = {"thread_id": thread_id, "user_id": user_id, "chat_type": "private", **(context or {})}
        config = {"configurable": configurable, "recursion_limit": 40}
        if from_button and thread_id not in self._open_questions:
            return AgentReply("That question is no longer active.")
        async with self._locks[thread_id]:
            mcp_ready = False
            try:
                async with mcp_session(
                    user_id=user_id,
                    chat_type=configurable["chat_type"],
                    group_chat_id=configurable.get("group_chat_id"),
                ) as client:
                    specs = await get_specs(client)
                    configurable["mcp_tools"], configurable["mcp_by_name"] = build_tools(
                        specs, client, chat_type=configurable["chat_type"]
                    )
                    mcp_ready = True
                    reply = await self._run({"messages": [HumanMessage(text)]}, config)
            except Exception as exc:
                if mcp_ready or not is_mcp_unreachable(exc):
                    raise
                log.warning(
                    "MCP unavailable for user %s; answering without expense tools: %s",
                    user_id, exc,
                )
                configurable["mcp_tools"] = []
                configurable["mcp_by_name"] = {}
                configurable["mcp_unavailable"] = True
                reply = await self._run({"messages": [HumanMessage(text)]}, config)
        if reply.choices:
            self._open_questions.add(thread_id)
        else:
            self._open_questions.discard(thread_id)
        return reply

    async def _run(self, graph_input, config) -> AgentReply:
        result = await self.graph.ainvoke(graph_input, config)
        last_ai = next((m for m in reversed(result["messages"]) if isinstance(m, AIMessage)), None)
        text = (message_text(last_ai) if last_ai else "") or "Done."
        extra = last_ai.additional_kwargs if last_ai else {}
        return AgentReply(
            text, choices=extra.get("choices", []), txn_ids=extra.get("txn_ids", []),
            split_ids=extra.get("split_ids", []), start_ids=extra.get("start_ids", [])
        )
