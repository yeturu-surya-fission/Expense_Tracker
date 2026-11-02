"""
What every tool shares: who is calling (`Call`), how a tool asks the human something (`ask_choice` -> `NeedInput`), and
how it answers (`Out`).

Human in the loop, without server-side sessions: a tool that needs an answer *raises NeedInput before it has written
anything*. The server turns that into an `input_required` result (question + options). The client asks the human and calls
the SAME tool again with the same arguments plus `answers={key: chosen option}`. Nothing is committed until every
question has an answer, and a second call with the same answers does the work exactly once (idempotency key).
"""
from dataclasses import dataclass, field
from typing import Any

from mcp_server.domain.choices import is_cancel


@dataclass(frozen=True)
class Opt:
    """One answer the human can pick (a button; `words` let a typed reply like 'equally' work too)."""
    id: str
    label: str
    words: tuple[str, ...] = ()


@dataclass
class Call:
    """The verified identity and context of one tool call. Built from the access token, never from tool arguments."""
    user_id: int
    subject: str  # 'user:12'
    actor: str | None  # the client acting for the user ('telegram-bot')
    chat_type: str = "private"  # private | group
    group_chat_id: int | None = None
    answers: dict[str, str] = field(default_factory=dict)
    member_count: int | None = None  # advisory, reported by the client (how many people Telegram says are in the group)
    tool: str = ""


@dataclass
class Out:
    """A successful result: text for the human plus structured data for the client (ids for Undo buttons, people to tag...)."""
    text: str
    data: dict[str, Any] = field(default_factory=dict)


class NeedInput(Exception):
    """Raised before any write: the human must pick an option first."""

    def __init__(self, key: str, question: str, options: list[Opt]):
        super().__init__(question)
        self.key, self.question, self.options = key, question, options

    def as_request(self) -> dict:
        return {
            "key": self.key,
            "type": "choice",
            "question": self.question,
            "options": [{"id": o.id, "label": o.label, "words": list(o.words)} for o in self.options],
        }


def ask_choice(call: Call, key: str, question: str, options: list[Opt]) -> str | None:
    """
    The human's answer for `key`: an option id, or None if they cancelled. Raises NeedInput when there is no answer yet
    (or the answer is not one of the options), so the caller has not written anything when it is raised.
    """
    if key in call.answers:
        value = call.answers[key]
        if is_cancel(value):
            return None
        if value in {o.id for o in options}:
            return value
    raise NeedInput(key, question, options)
