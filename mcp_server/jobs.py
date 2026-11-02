"""Background work inside the MCP server: payment reminders (written to the outbox) and housekeeping."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from mcp_server.config import settings
from mcp_server.db.models import AuditLog, IdempotencyRecord, Outbox, RevokedToken
from mcp_server.db.session import session_scope
from mcp_server.domain import debts, notices

log = logging.getLogger(__name__)
CHECK_EVERY_SECONDS = 30 * 60


async def run_reminders_once() -> int:
    async with session_scope() as s:
        pending, claimed = await debts.debts_needing_reminder(s, settings.reminder_every_hours)
        return await notices.send_reminders(s, pending, claimed)


async def cleanup_once(now: datetime | None = None) -> None:
    """Forget what is no longer needed: expired revocations, old idempotency keys, delivered messages, old audit rows."""
    now = now or datetime.now(timezone.utc)
    async with session_scope() as s:
        await s.execute(delete(RevokedToken).where(RevokedToken.expires_at < now))
        await s.execute(delete(IdempotencyRecord).where(IdempotencyRecord.created_at < now - timedelta(days=2)))
        await s.execute(delete(Outbox).where(Outbox.status.in_(("sent", "cancelled", "blocked", "failed")), Outbox.created_at < now - timedelta(days=7)))
        await s.execute(delete(AuditLog).where(AuditLog.at < now - timedelta(days=settings.audit_retention_days)))


async def reminder_loop() -> None:
    while True:
        try:
            sent = await run_reminders_once()
            if sent:
                log.info("queued %s payment reminder(s)", sent)
            await cleanup_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background job failed")
        await asyncio.sleep(CHECK_EVERY_SECONDS)
