"""Helpers shared by all MCP tools.

User context: the auth middleware (pending) puts the JWT's `sub`, `chat_type`
and `group_chat_id` claims into the request's MCP state before the tool runs,
so tools read them from `ctx.get_state(...)` instead of a RunnableConfig.
"""
from dataclasses import dataclass

from fastmcp import Context

from mcp_server.core.choices import is_cancel
from mcp_server.core.markers import ask_marker


@dataclass(frozen=True)
class Opt:
    """One answer the user can pick (the words let typed replies like 'equally' work too)."""
    id: str
    label: str
    words: tuple[str, ...] = ()


async def user_id_from(ctx: Context) -> int:
    """fastmcp 4.x: Context.get_state is async, so all three accessors are awaited by tools."""
    return int(await ctx.get_state("user_id"))


async def chat_type(ctx: Context) -> str:
    return await ctx.get_state("chat_type") or "private"


async def group_chat_id(ctx: Context) -> int | None:
    value = await ctx.get_state("group_chat_id")
    return int(value) if value is not None else None


def ask_choice(question: str, options: list[Opt]) -> str:
    """
    Ask-then-recall HITL: this pauses the flow by RETURNING the question as the
    tool result. The agent shows it to the user, then calls the tool again with
    the answer filled into its own parameters (e.g. `split=` or `kind=`;
    `confirm=True` for settle_group). The answer parameter may carry typed words
    too, so `answer_id` folds them onto an option id. Nothing is written before
    the question is resolved.
    """
    labels = " / ".join(o.label for o in options)
    return (f"{question} Reply with: {labels} (or say 'cancel').\n"
            f"{ask_marker(options)}")  # hidden marker -> choice buttons + the runner's open-question flag


def answer_id(answer: str | None, options: list[Opt]) -> str | None:
    """Fold a recalled answer onto an option id, checking the option's words. None = unknown/cancelled."""
    if answer and is_cancel(answer):
        return None
    for o in options:
        if answer == o.id or answer == o.label:
            return o.id
        words = {w.lower() for w in o.words} | {o.id.lower(), o.label.lower()}
        if answer and set(answer.lower().split()) & words:
            return o.id
    return None
