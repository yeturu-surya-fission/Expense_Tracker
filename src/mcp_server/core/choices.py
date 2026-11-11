"""Understanding a person's answer to a question the bot asked (a tapped button or typed words)."""
import re

CANCEL_WORDS = {"cancel", "skip", "nevermind", "stop", "no", "ignore", "forget", "abort"}


def is_cancel(text: str | None) -> bool:
    words = set(re.findall(r"[a-z]+", (text or "").lower().replace("never mind", "nevermind")))
    return bool(words & CANCEL_WORDS)


def match_option(text: str | None, options: list[dict]) -> str | None:
    """Return the id of the option the text means, or None. Options look like {id, label, words:[...]}."""
    t = (text or "").strip().lower()
    for o in options:
        if t == o["id"].lower() or t == o["label"].lower():
            return o["id"]
    words = set(re.findall(r"[a-z]+", t))
    hits = [o for o in options if words & {w.lower() for w in o.get("words", [])}]
    return hits[0]["id"] if len(hits) == 1 else None
