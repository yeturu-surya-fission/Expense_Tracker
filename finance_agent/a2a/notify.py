"""
Telling the expense tracker what happened to a claim: a signed webhook (HMAC-SHA256 of the body, like GitHub webhooks).
Events wait in the `webhook_events` table until the tracker accepts them, so a restart or an outage loses nothing.
"""
import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select

from finance_agent.config import settings
from finance_agent.db.models import WebhookEvent
from finance_agent.db.session import session_scope

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 8
POLL_SECONDS = 3


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def deliver_once(client: httpx.AsyncClient | None = None) -> int:
    """Send every due event. An event the tracker rejects or can't be reached for is retried later (back-off), then given up on."""
    async with session_scope() as s:
        rows = list((await s.execute(
            select(WebhookEvent).where(WebhookEvent.status == "pending", WebhookEvent.available_at <= func.now()).order_by(WebhookEvent.id).limit(20).with_for_update(skip_locked=True)
        )).scalars().all())
        owns = client is None
        client = client or httpx.AsyncClient(timeout=10)
        try:
            for ev in rows:
                body = json.dumps(ev.payload, ensure_ascii=False, separators=(",", ":")).encode()
                try:
                    r = await client.post(settings.tracker_webhook_url, content=body, headers={"Content-Type": "application/json", "X-Signature": sign(body, settings.webhook_secret)})
                    ok = r.status_code < 300
                    if not ok:
                        log.warning("tracker refused webhook %s: HTTP %s", ev.id, r.status_code)
                except httpx.HTTPError as e:
                    ok = False
                    log.warning("tracker unreachable for webhook %s: %s", ev.id, type(e).__name__)
                ev.attempts += 1
                if ok:
                    ev.status = "sent"
                elif ev.attempts >= MAX_ATTEMPTS:
                    ev.status = "failed"
                else:
                    ev.available_at = datetime.now(timezone.utc) + timedelta(seconds=min(30 * 2 ** ev.attempts, 3600))
        finally:
            if owns:
                await client.aclose()
        return len(rows)


async def webhook_loop() -> None:
    while True:
        try:
            await deliver_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("webhook delivery failed")
        await asyncio.sleep(POLL_SECONDS)
