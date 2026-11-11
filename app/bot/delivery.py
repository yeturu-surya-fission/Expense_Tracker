"""
Delivers the messages the MCP server queues for people ('you owe 750', reminders, 'that split was cancelled').

The server never talks to Telegram: it writes an outbox. This loop claims the next messages with a platform tool, sends or
deletes them in Telegram, and reports each result back (message id on success; `blocked` if the person never pressed Start).
"""
import asyncio
import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, TelegramError

from app.config import settings
from app.mcp_client.gateway import McpGateway
from app.mcp_client.types import McpError

log = logging.getLogger(__name__)
BATCH = 20


def to_markup(buttons: list[list[dict]] | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(b["label"], url=b["url"]) if b.get("url") else InlineKeyboardButton(b["label"], callback_data=b["data"]) for b in row]
        for row in buttons
    ])


class OutboxDeliverer:
    def __init__(self, bot: Bot, gateway: McpGateway):
        self.bot, self.gateway = bot, gateway

    async def _ack(self, outbox_id: int, **fields) -> None:
        await self.gateway.platform("platform_outbox_ack", {"outbox_id": outbox_id, **fields})

    async def deliver(self, m: dict) -> None:
        if m["kind"] == "delete":
            try:
                await self.bot.delete_message(m["telegram_id"], m["message_id"])
            except TelegramError:
                pass  # already deleted or too old: nothing to do
            await self._ack(m["id"])
            return
        try:
            sent = await self.bot.send_message(m["telegram_id"], m["text"], reply_markup=to_markup(m.get("buttons")))
        except Forbidden:  # the person never pressed Start, or blocked the bot
            log.info("cannot message %s: bot not started / blocked", m["telegram_id"])
            self.gateway.forget_sessions(m["telegram_id"])  # their next message re-opens a session as 'started'
            await self._ack(m["id"], blocked=True, error="forbidden")
        except TelegramError as e:
            log.warning("sending to %s failed: %s", m["telegram_id"], e)
            await self._ack(m["id"], error=str(e)[:150])
        else:
            await self._ack(m["id"], message_id=sent.message_id)

    async def run_once(self) -> int:
        res = await self.gateway.platform("platform_outbox_claim", {"limit": BATCH})
        messages = res.data.get("messages", [])
        for m in messages:
            await self.deliver(m)
        return len(messages)


async def delivery_loop(deliverer: OutboxDeliverer) -> None:
    delay = settings.outbox_poll_seconds
    while True:
        try:
            handled = await deliverer.run_once()
            delay = 0 if handled >= BATCH else settings.outbox_poll_seconds
        except asyncio.CancelledError:
            raise
        except McpError as e:
            log.warning("outbox: %s", e)
            delay = min(max(delay * 2, 2), 30)
        except Exception:
            log.exception("outbox delivery failed")
            delay = 5
        await asyncio.sleep(delay)
