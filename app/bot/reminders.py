"""Background job: nudge people about payments they still owe (and creditors about payments to confirm)."""
import asyncio
import logging

from app.config import settings
from app.core import debts, notices
from app.db.session import session_scope

log = logging.getLogger(__name__)
CHECK_EVERY_SECONDS = 30 * 60


async def run_once() -> int:
    async with session_scope() as s:
        pending, claimed = await debts.debts_needing_reminder(s, settings.reminder_every_hours)
        return await notices.send_reminders(s, pending, claimed)


async def reminder_loop() -> None:
    while True:
        try:
            sent = await run_once()
            if sent:
                log.info("sent %s payment reminder(s)", sent)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reminder run failed")
        await asyncio.sleep(CHECK_EVERY_SECONDS)
