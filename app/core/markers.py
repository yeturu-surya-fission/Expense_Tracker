"""
Hidden markers tools put at the end of their output so the bot can attach buttons / follow-up messages.
They are stripped before the text reaches the user.

    (#12)           an expense that was just recorded    -> Undo button        (kept visible: it is the expense id)
    (split #5)      a split that was just created        -> Cancel-split button
    (start #123)    a person the bot could not message   -> "press Start" mention + button in the group
"""
import re
from dataclasses import dataclass, field

_EXPENSE = re.compile(r"\(#(\d+)\)")
_SPLIT = re.compile(r"\(split #(\d+)\)")
_START = re.compile(r"\(start #(\d+)\)")
_HIDDEN = re.compile(r"\n?\((?:split|start) #\d+\)")


@dataclass
class Extracted:
    text: str
    txn_ids: list[int] = field(default_factory=list)
    split_ids: list[int] = field(default_factory=list)
    start_ids: list[int] = field(default_factory=list)  # telegram ids of people who still need to press Start


def start_marker(telegram_id: int) -> str:
    return f"(start #{telegram_id})"


def split_marker(split_id: int) -> str:
    return f"(split #{split_id})"


def extract(text: str, *, expenses_from: str | None = None) -> Extracted:
    """Pull the markers out of text. `expenses_from` is the part of the text that came from add_expenses (if any)."""
    return Extracted(
        text=_HIDDEN.sub("", text).strip(),
        txn_ids=[int(i) for i in _EXPENSE.findall(expenses_from if expenses_from is not None else "")],
        split_ids=[int(i) for i in _SPLIT.findall(text)],
        start_ids=list(dict.fromkeys(int(i) for i in _START.findall(text))),
    )
