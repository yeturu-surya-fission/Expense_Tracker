"""Reversible PII masking/pseudonymization for the Finance Agent LLM boundary.

Masks company names, employee/manager names, emails, phone numbers, UPI IDs, bank account/card
numbers, IFSC, PAN, and Aadhaar before calling the external LLM, then restores them in responses.
Kept self-contained so finance_agent shares no imports with app or mcp_server.
"""

from __future__ import annotations

import re
from typing import Any

_RESERVED_WORDS = frozenset(
    {
        "me",
        "my",
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
        "employee",
        "manager",
        "agent",
    }
)

_HEADER_PII_RE = re.compile(
    r"(?im)^((?:account\s+holder(?:\s+name)?|customer\s+name|holder\s+name|employee\s+name|mailing\s+address|residential\s+address|address)\s*[:\-]\s*)(.+?)\s*$"
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


class FinancePIIMasker:
    """Reversible PII masker for Finance Agent LLM prompts and JSON outputs."""

    def __init__(
        self,
        *,
        company: str | None = None,
        employees: list[str] | tuple[str, ...] = (),
    ) -> None:
        self._counters: dict[str, int] = {}
        self._to_token: dict[str, str] = {}
        self._ci_to_token: dict[str, str] = {}
        self._to_original: dict[str, str] = {}
        self._named_entries: list[tuple[str, str]] = []
        if company:
            self.register_name(company, "COMPANY")
        for emp in employees:
            self.register_name(emp, "EMPLOYEE")

    def _next_index(self, prefix: str) -> int:
        idx = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = idx
        return idx

    def _is_token(self, value: str) -> bool:
        return value in self._to_original

    def register_name(self, name: str | None, prefix: str = "EMPLOYEE") -> str | None:
        if not name:
            return None
        clean = name.strip()
        if len(clean) < 2 or clean.lower() in _RESERVED_WORDS or self._is_token(clean):
            return self._to_token.get(clean)
        existing = self._ci_to_token.get(clean.lower())
        if existing is not None:
            return existing
        token = f"{prefix}_{self._next_index(prefix)}"
        self._to_token[clean] = token
        self._ci_to_token[clean.lower()] = token
        self._to_original[token] = clean
        self._named_entries.append((clean, token))
        return token

    def _token_for(self, kind: str, raw: str) -> str:
        if self._is_token(raw):
            return raw
        existing = self._to_token.get(raw)
        if existing is not None:
            return existing
        token = f"[{kind}_{self._next_index(kind)}]"
        self._to_token[raw] = token
        self._to_original[token] = raw
        return token

    def mask_text(self, text: str) -> str:
        if not text:
            return text
        out = text
        # Structured PII before named entities so emails/UPIs containing names stay atomic.
        out = _EMAIL_RE.sub(lambda m: self._token_for("EMAIL", m.group(0)), out)
        out = _UPI_RE.sub(lambda m: self._token_for("UPI", m.group(0)), out)
        out = _PAN_RE.sub(lambda m: self._token_for("PAN", m.group(0)), out)
        out = _IFSC_RE.sub(lambda m: self._token_for("IFSC", m.group(0)), out)
        out = _HEADER_PII_RE.sub(
            lambda m: (
                m.group(0)
                if self._is_token(m.group(2))
                else f"{m.group(1)}{self._token_for('ADDRESS' if 'address' in m.group(1).lower() else 'PERSON', m.group(2))}"
            ),
            out,
        )
        for raw, token in sorted(
            self._named_entries, key=lambda e: len(e[0]), reverse=True
        ):
            pat = re.compile(rf"(?<!\w){re.escape(raw)}(?!\w)", re.IGNORECASE)
            out = pat.sub(token, out)
        out = _CARD_MASKED_RE.sub(lambda m: self._token_for("CARD", m.group(0)), out)
        out = _CARD_LABELED_RE.sub(
            lambda m: f"{m.group(1)}{self._token_for('CARD', m.group(2))}", out
        )
        out = _CARD_FULL_RE.sub(
            lambda m: (
                self._token_for("CARD", m.group(0))
                if 13 <= sum(ch.isdigit() for ch in m.group(0)) <= 19
                else m.group(0)
            ),
            out,
        )
        out = _LABELED_ACCOUNT_RE.sub(
            lambda m: f"{m.group(1)}{self._token_for('ACCOUNT', m.group(2))}", out
        )
        out = _PHONE_INTL_RE.sub(lambda m: self._token_for("PHONE", m.group(0)), out)
        out = _PHONE_LABELED_RE.sub(
            lambda m: f"{m.group(1)}{self._token_for('PHONE', m.group(2))}", out
        )
        out = _PHONE_10_RE.sub(lambda m: self._token_for("PHONE", m.group(0)), out)
        out = _AADHAAR_RE.sub(lambda m: self._token_for("AADHAAR", m.group(0)), out)
        out = _ACCOUNT_STANDALONE_RE.sub(
            lambda m: self._token_for("ACCOUNT", m.group(1)), out
        )
        return out

    def unmask_text(self, text: str) -> str:
        if not text or not self._to_original:
            return text
        tokens = sorted(self._to_original, key=len, reverse=True)
        parts = [
            re.escape(t) if t.startswith("[") and t.endswith("]") else rf"(?<!\w){re.escape(t)}(?!\w)"
            for t in tokens
        ]
        pattern = re.compile("|".join(parts))
        return pattern.sub(lambda m: self._to_original[m.group(0)], text)

    def unmask_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.unmask_text(value)
        if isinstance(value, list):
            return [self.unmask_value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.unmask_value(v) for k, v in value.items()}
        return value
