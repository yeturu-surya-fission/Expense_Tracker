"""Helpers shared by all tools."""
from dataclasses import dataclass

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from app.core.choices import is_cancel


@dataclass(frozen=True)
class Opt:
    """One answer the user can pick (shown as a button; the words let typed replies like 'equally' work too)."""
    id: str
    label: str
    words: tuple[str, ...] = ()


def user_id_from(config: RunnableConfig) -> int:
    return config["configurable"]["user_id"]


def chat_type(config: RunnableConfig) -> str:
    return config["configurable"].get("chat_type", "private")


def group_chat_id(config: RunnableConfig) -> int | None:
    return config["configurable"].get("group_chat_id")


def ask_choice(question: str, options: list[Opt]) -> str | None:
    """
    Human-in-the-loop: pause the graph and ask the user to pick one option.
    Returns the chosen option id, or None if they cancelled.

    The AgentRunner screens the reply (button or typed words) before resuming, so only an option id or 'cancel' arrives here.
    NOTE: interrupt() re-runs the tool from the top on resume, so call it BEFORE any database write.
    """
    answer = interrupt({
        "type": "choice",
        "question": question,
        "options": [{"id": o.id, "label": o.label, "words": list(o.words)} for o in options],
    })
    if is_cancel(answer):
        return None
    return answer if answer in {o.id for o in options} else None
