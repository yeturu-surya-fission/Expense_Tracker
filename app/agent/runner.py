"""
AgentRunner: the single entry point the bot uses.

One conversation thread per person per chat. If the graph is paused on a human-in-the-loop question, the next message
is treated as the answer; if it is NOT a valid answer the pending question is cancelled and the message is processed
as a fresh request.
"""
import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from app.core.choices import is_cancel, match_option

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


def message_text(msg) -> str:
    content = msg.content
    if isinstance(content, list):  # Gemini/Anthropic may return content blocks
        content = "".join(p if isinstance(p, str) else p.get("text", "") for p in content)
    return (content or "").strip()


class AgentRunner:
    def __init__(self, graph):
        self.graph = graph
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)  # single-process guard

    async def chat(
        self, *, user_id: int, thread_id: str, text: str, from_button: bool = False, context: dict | None = None
    ) -> AgentReply:
        """`context` = {"chat_type": "private" | "group", "group_chat_id": int | None}."""
        configurable = {"thread_id": thread_id, "user_id": user_id, "chat_type": "private", **(context or {})}
        config = {"configurable": configurable, "recursion_limit": 40}
        async with self._locks[thread_id]:
            pending = await self._pending_interrupt(config)

            if pending is not None:
                answer = "cancel" if is_cancel(text) and not match_option(text, pending.get("options", [])) \
                    else match_option(text, pending.get("options", []))
                if answer:
                    return await self._run(Command(resume=answer), config)
                # unrelated message: drop the stale question, then handle the message normally
                await self._run(Command(resume="cancel"), config)
                if from_button:
                    return AgentReply("That question is no longer active.")
            elif from_button:
                return AgentReply("That question is no longer active.")

            return await self._run({"messages": [HumanMessage(text)]}, config)

    async def _run(self, graph_input, config) -> AgentReply:
        result = await self.graph.ainvoke(graph_input, config)
        pending = await self._pending_interrupt(config)
        if pending is not None:
            choices = [{"id": o["id"], "label": o["label"]} for o in pending.get("options", [])] + [{"id": "cancel", "label": "✖ Cancel"}]
            return AgentReply(pending["question"], choices=choices)
        last_ai = next((m for m in reversed(result["messages"]) if isinstance(m, AIMessage)), None)
        text = (message_text(last_ai) if last_ai else "") or "Done."
        extra = last_ai.additional_kwargs if last_ai else {}
        return AgentReply(
            text, txn_ids=extra.get("txn_ids", []), split_ids=extra.get("split_ids", []), start_ids=extra.get("start_ids", [])
        )

    async def _pending_interrupt(self, config) -> dict | None:
        state = await self.graph.aget_state(config)
        for task in state.tasks:
            for itr in task.interrupts:
                return itr.value
        return None
