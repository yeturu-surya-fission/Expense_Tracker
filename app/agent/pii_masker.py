"""Reversible PII masking/pseudonymization at the external LLM boundary.

All sensitive user information (user names, Telegram usernames/IDs, friend names, group names,
emails, phone numbers, UPI IDs, bank account/card numbers, IFSC, PAN, Aadhaar) is replaced with
opaque tokens before any text or message list leaves the app to an LLM, and restored immediately
when the LLM responds.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

_RESERVED_WORDS = frozenset(
    {
        "me",
        "my",
        "mine",
        "i",
        "we",
        "us",
        "you",
        "your",
        "none",
        "all",
        "ok",
        "yes",
        "no",
        "null",
        "true",
        "false",
        "someone",
        "unknown",
    }
)

_FRIEND_CMD_RE = re.compile(
    r"(?i)\b((?:add|rename|remove)\s+friend\s+)([A-Za-z][A-Za-z0-9_.-]{1,30})(?![A-Za-z0-9._-]*@)(?:(\s+)(\d{6,15}))?"
)
_TG_ID_CONTEXT_RE = re.compile(
    r"(?i)\b((?:telegram|tg|chat|user)\s*(?:id|no)?\s*[:#-]?\s*(?:is\s+)?)(\d{6,15})\b"
)
_HEADER_PII_RE = re.compile(
    r"(?im)^((?:account\s+holder(?:\s+name)?|customer\s+name|holder\s+name|acc(?:ount)?\s+name|mailing\s+address|residential\s+address|address)\s*[:\-]\s*)(.+?)\s*$"
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_UPI_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._-]{2,}@[A-Za-z][A-Za-z0-9._-]{1,25}\b"
)
_PAN_RE = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
_IFSC_RE = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
_CARD_MASKED_RE = re.compile(
    r"(?:\b[Xx*]{4,}(?:[ -]?[Xx*]{4})*[ -]?\d{4}\b|\*{4,}[ -]?\d{4}\b)"
)
_CARD_LABELED_RE = re.compile(
    r"(?i)\b((?:card|debit\s*card|credit\s*card)\s*(?:no\.?|number|ending(?:\s+in)?)?\s*[:#-]?\s*)([Xx*\d -]{4,20}\d{4})\b"
)
_CARD_FULL_RE = re.compile(r"(?<!\.)\b(?:\d{4}[ -]?){3}\d{1,7}\b(?!\.\d)")
_LABELED_ACCOUNT_RE = re.compile(
    r"(?i)\b((?:a/c|acct?|account|cif|cust(?:omer)?\s*id|crn)\s*(?:no\.?|number|#)?\s*[:#-]?\s*)([Xx*]*\d[\dXx*-]{3,19})\b"
)
_PHONE_INTL_RE = re.compile(r"(?<!\w)\+\d{1,3}[\s-]?\d{4,5}[\s-]?\d{4,6}\b")
_PHONE_LABELED_RE = re.compile(
    r"(?i)\b((?:mob(?:ile)?|phone|ph|tel|contact)\s*(?:no\.?|number)?\s*[:#-]?\s*)(\+?\d[\d\s-]{7,14}\d)\b"
)
_PHONE_10_RE = re.compile(r"(?<!\d)(?<!\.)(?:0?[6-9]\d{9})(?!\d)(?!\.\d)")
_AADHAAR_RE = re.compile(r"(?<!\.)\b\d{4}\s\d{4}\s\d{4}\b(?!\.\d)")
_ACCOUNT_STANDALONE_RE = re.compile(r"(?<!\d)(?<!\.)(\d{9,18})(?!\d)(?!\.\d)")


def _bracket_token(kind: str, idx: int) -> str:
    return f"[{kind}_{idx}]"


class PIIMasker:
    """Maintains a consistent, reversible mapping between raw PII and safe placeholder codes."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._to_token: dict[str, str] = {}
        self._ci_to_token: dict[str, str] = {}
        self._to_original: dict[str, str] = {}
        self._int_to_original: dict[str, str] = {}
        self._named_entries: list[tuple[str, str, bool]] = []  # (raw, token, exact_case)
        self._next_synthetic_int = 880_000_001

    def _next_index(self, prefix: str) -> int:
        idx = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = idx
        return idx

    def _is_token(self, value: str) -> bool:
        return value in self._to_original or value in self._int_to_original

    def register_name(
        self,
        name: str | None,
        prefix: str = "FRIEND",
        *,
        exact_case: bool = False,
    ) -> str | None:
        """Register a named entity (user, friend, member, group) and return its opaque code."""
        if not name:
            return None
        clean = name.strip()
        if len(clean) < 2 or clean.lower() in _RESERVED_WORDS or self._is_token(clean):
            return self._to_token.get(clean)
        existing = (
            self._to_token.get(clean)
            if exact_case
            else self._ci_to_token.get(clean.lower())
        )
        if existing is not None:
            return existing
        token = f"{prefix}_{self._next_index(prefix)}"
        self._to_token[clean] = token
        if not exact_case:
            self._ci_to_token[clean.lower()] = token
        self._to_original[token] = clean
        self._named_entries.append((clean, token, exact_case))
        return token

    def register_username(self, username: str | None) -> str | None:
        """Register a Telegram @username and return its code."""
        if not username:
            return None
        bare = username.strip().lstrip("@")
        if len(bare) < 2:
            return None
        token = self.register_name(bare, "USERNAME", exact_case=False)
        if token:
            at_raw = f"@{bare}"
            at_token = f"@{token}"
            self._to_token.setdefault(at_raw, at_token)
            self._ci_to_token.setdefault(at_raw.lower(), at_token)
            self._to_original.setdefault(at_token, at_raw)
        return token

    def register_telegram_id(self, tg_id: int | str | None) -> int | None:
        """Register a numeric Telegram ID using a synthetic integer code (schema-safe for int params)."""
        if tg_id is None:
            return None
        raw = str(tg_id).strip()
        if not raw.isdigit():
            return None
        if raw in self._to_token:
            return int(self._to_token[raw])
        syn = str(self._next_synthetic_int)
        self._next_synthetic_int += 1
        self._to_token[raw] = syn
        self._to_original[syn] = raw
        self._int_to_original[syn] = raw
        return int(syn)

    def _token_for_pattern(self, kind: str, raw: str) -> str:
        if self._is_token(raw):
            return raw
        existing = self._to_token.get(raw)
        if existing is not None:
            return existing
        token = _bracket_token(kind, self._next_index(kind))
        self._to_token[raw] = token
        self._to_original[token] = raw
        return token

    def _apply_named_entities(self, text: str) -> str:
        if not self._named_entries:
            return text
        # Longest names first so "Surya Kumar" masks before "Surya".
        for raw, token, exact_case in sorted(
            self._named_entries, key=lambda e: len(e[0]), reverse=True
        ):
            flags = 0 if exact_case else re.IGNORECASE
            pat = re.compile(rf"(?<!\w){re.escape(raw)}(?!\w)", flags)
            text = pat.sub(token, text)
        return text

    def mask_text(self, text: str) -> str:
        """Replace all registered names and structured PII patterns with reversible codes."""
        if not text:
            return text

        # 1. Explicit friend commands: mask the friend name (even if new) and Telegram ID as int code.
        def _friend_cmd(m: re.Match[str]) -> str:
            prefix, name, sep, tg_id = (
                m.group(1),
                m.group(2),
                m.group(3) or "",
                m.group(4),
            )
            name_tok = (
                name
                if self._is_token(name) or name.lower() in _RESERVED_WORDS
                else (self.register_name(name, "FRIEND") or name)
            )
            id_tok = (
                str(self.register_telegram_id(tg_id)) if tg_id is not None else ""
            )
            return f"{prefix}{name_tok}{sep}{id_tok}"

        out = _FRIEND_CMD_RE.sub(_friend_cmd, text)

        # 2. Contextual Telegram / user IDs.
        def _tg_ctx(m: re.Match[str]) -> str:
            num = m.group(2)
            if self._is_token(num):
                return m.group(0)
            return f"{m.group(1)}{self.register_telegram_id(num)}"

        out = _TG_ID_CONTEXT_RE.sub(_tg_ctx, out)

        # 3. Structured PII before named entities so names inside emails/UPIs (e.g. surya@okicici)
        #    are tokenized atomically without partial substitution.
        out = _EMAIL_RE.sub(lambda m: self._token_for_pattern("EMAIL", m.group(0)), out)
        out = _UPI_RE.sub(lambda m: self._token_for_pattern("UPI", m.group(0)), out)
        out = _PAN_RE.sub(lambda m: self._token_for_pattern("PAN", m.group(0)), out)
        out = _IFSC_RE.sub(lambda m: self._token_for_pattern("IFSC", m.group(0)), out)

        # 4. Personal info in document/statement headers (Name/Address lines).
        def _header_pii(m: re.Match[str]) -> str:
            label, val = m.group(1), m.group(2)
            if self._is_token(val):
                return m.group(0)
            kind = "ADDRESS" if "address" in label.lower() else "PERSON"
            return f"{label}{self._token_for_pattern(kind, val)}"

        out = _HEADER_PII_RE.sub(_header_pii, out)

        # 5. Registered names (speaker, friends, members, group title, username).
        out = self._apply_named_entities(out)

        # 6. Card & bank account identifiers.
        out = _CARD_MASKED_RE.sub(
            lambda m: self._token_for_pattern("CARD", m.group(0)), out
        )
        out = _CARD_LABELED_RE.sub(
            lambda m: f"{m.group(1)}{self._token_for_pattern('CARD', m.group(2))}", out
        )
        out = _CARD_FULL_RE.sub(
            lambda m: (
                self._token_for_pattern("CARD", m.group(0))
                if 13 <= sum(ch.isdigit() for ch in m.group(0)) <= 19
                else m.group(0)
            ),
            out,
        )
        out = _LABELED_ACCOUNT_RE.sub(
            lambda m: f"{m.group(1)}{self._token_for_pattern('ACCOUNT', m.group(2))}",
            out,
        )

        # 7. Phone numbers & Aadhaar.
        out = _PHONE_INTL_RE.sub(
            lambda m: self._token_for_pattern("PHONE", m.group(0)), out
        )
        out = _PHONE_LABELED_RE.sub(
            lambda m: f"{m.group(1)}{self._token_for_pattern('PHONE', m.group(2))}", out
        )
        out = _PHONE_10_RE.sub(
            lambda m: (
                m.group(0)
                if self._is_token(m.group(0))
                else self._token_for_pattern("PHONE", m.group(0))
            ),
            out,
        )
        out = _AADHAAR_RE.sub(
            lambda m: self._token_for_pattern("AADHAAR", m.group(0)), out
        )

        # 8. Standalone long numeric account/reference sequences (9-18 digits).
        out = _ACCOUNT_STANDALONE_RE.sub(
            lambda m: (
                m.group(0)
                if self._is_token(m.group(1))
                else self._token_for_pattern("ACCOUNT", m.group(1))
            ),
            out,
        )
        return out

    def mask_value(self, value: Any) -> Any:
        """Recursively mask strings and registered Telegram IDs inside dicts/lists."""
        if isinstance(value, str):
            return self.mask_text(value)
        if isinstance(value, int) and not isinstance(value, bool):
            syn = self._to_token.get(str(value))
            return int(syn) if syn is not None and syn.isdigit() else value
        if isinstance(value, list):
            return [self.mask_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.mask_value(v) for k, v in value.items()}
        return value

    def unmask_text(self, text: str) -> str:
        """Restore all masked tokens inside text back to their original values."""
        if not text or not self._to_original:
            return text
        tokens = sorted(self._to_original, key=len, reverse=True)
        parts = []
        for tok in tokens:
            esc = re.escape(tok)
            if tok.startswith("[") and tok.endswith("]"):
                parts.append(esc)
            else:
                parts.append(rf"(?<!\w){esc}(?!\w)")
        pattern = re.compile("|".join(parts))
        return pattern.sub(lambda m: self._to_original[m.group(0)], text)

    def unmask_value(self, value: Any) -> Any:
        """Recursively restore masked tokens (including synthetic int Telegram IDs)."""
        if isinstance(value, int) and not isinstance(value, bool):
            orig = self._int_to_original.get(str(value))
            return int(orig) if orig is not None else value
        if isinstance(value, str):
            if value in self._int_to_original:
                return int(self._int_to_original[value])
            return self.unmask_text(value)
        if isinstance(value, list):
            return [self.unmask_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.unmask_value(v) for k, v in value.items()}
        return value

    def _mask_content(self, content: Any) -> Any:
        if isinstance(content, str):
            return self.mask_text(content)
        if isinstance(content, list):
            out = []
            for part in content:
                if isinstance(part, str):
                    out.append(self.mask_text(part))
                elif isinstance(part, dict) and "text" in part:
                    out.append({**part, "text": self.mask_text(str(part["text"]))})
                else:
                    out.append(part)
            return out
        return content

    def _unmask_content(self, content: Any) -> Any:
        if isinstance(content, str):
            return self.unmask_text(content)
        if isinstance(content, list):
            out = []
            for part in content:
                if isinstance(part, str):
                    out.append(self.unmask_text(part))
                elif isinstance(part, dict) and "text" in part:
                    out.append({**part, "text": self.unmask_text(str(part["text"]))})
                else:
                    out.append(part)
            return out
        return content

    def mask_messages(self, messages: list[AnyMessage]) -> list[AnyMessage]:
        """Return masked copies of LangChain messages without mutating stored state."""
        masked: list[AnyMessage] = []
        for msg in messages:
            if isinstance(msg, SystemMessage):
                masked.append(msg.model_copy(update={"content": self._mask_content(msg.content)}))
            elif isinstance(msg, HumanMessage):
                masked.append(msg.model_copy(update={"content": self._mask_content(msg.content)}))
            elif isinstance(msg, ToolMessage):
                masked.append(msg.model_copy(update={"content": self._mask_content(msg.content)}))
            elif isinstance(msg, AIMessage):
                masked_calls = [
                    {**c, "args": self.mask_value(c.get("args", {}))}
                    for c in (msg.tool_calls or [])
                ]
                masked.append(
                    msg.model_copy(
                        update={
                            "content": self._mask_content(msg.content),
                            "tool_calls": masked_calls,
                        }
                    )
                )
            else:
                masked.append(msg)
        return masked

    def unmask_ai_message(self, msg: AIMessage) -> AIMessage:
        """Restore masked tokens in an LLM-returned AIMessage (content and tool_calls)."""
        restored_calls = [
            {**c, "args": self.unmask_value(c.get("args", {}))}
            for c in (msg.tool_calls or [])
        ]
        return msg.model_copy(
            update={
                "content": self._unmask_content(msg.content),
                "tool_calls": restored_calls,
            }
        )


def build_chat_masker(
    *,
    speaker: str | None = None,
    username: str | None = None,
    telegram_id: int | None = None,
    group: str | None = None,
    group_chat_id: int | None = None,
    friends: list[str] | None = None,
    members: list[str] | None = None,
) -> PIIMasker:
    """Create a PIIMasker pre-seeded with chat identity, group title, and friend/member names."""
    masker = PIIMasker()
    masker.register_name(speaker, "USER", exact_case=False)
    masker.register_username(username)
    masker.register_telegram_id(telegram_id)
    if group:
        clean_group = group.strip()
        short_single = " " not in clean_group and len(clean_group) <= 6
        masker.register_name(clean_group, "GROUP", exact_case=short_single)
    if group_chat_id is not None:
        masker.register_telegram_id(abs(group_chat_id))
    for member in members or []:
        masker.register_name(member, "MEMBER", exact_case=False)
    for friend in friends or []:
        masker.register_name(friend, "FRIEND", exact_case=False)
    return masker
