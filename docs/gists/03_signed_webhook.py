"""Gist 3: Finance Agent -> tracker decisions travel as an HMAC-signed webhook.

Illustrative sketch. Approval can take days, so the decision is stored and retried until the receiver accepts it,
and the receiver ignores repeats (idempotent).
"""

import hashlib
import hmac


def sign(body: bytes, secret: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def verify(body: bytes, header: str, secret: bytes) -> bool:
    return hmac.compare_digest(sign(body, secret), header)  # constant-time compare


seen: set[str] = set()


def handle(event_id: str, body: bytes, header: str, secret: bytes) -> str:
    if not verify(body, header, secret):
        return "401"
    if event_id in seen:
        return "200 duplicate ignored"
    seen.add(event_id)
    return "200 applied"
