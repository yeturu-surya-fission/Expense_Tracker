"""Telegram implementation of the core's Notifier: how a friend gets 'you owe 750' in their private chat."""
import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, TelegramError

from app.core.notifier import Button

log = logging.getLogger(__name__)


def to_markup(buttons: list[list[Button]] | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(b.label, url=b.url) if b.url else InlineKeyboardButton(b.label, callback_data=b.data) for b in row]
        for row in buttons
    ])


class TelegramNotifier:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def send(self, telegram_id: int, text: str, buttons: list[list[Button]] | None = None) -> int | None:
        try:
            msg = await self.bot.send_message(telegram_id, text, reply_markup=to_markup(buttons))
            return msg.message_id
        except Forbidden:  # the person never pressed Start, or blocked the bot
            log.info("cannot message %s: bot not started / blocked", telegram_id)
        except TelegramError as e:
            log.warning("sending to %s failed: %s", telegram_id, e)
        return None

    async def member_count(self, chat_id: int) -> int | None:
        try:
            return await self.bot.get_chat_member_count(chat_id)
        except TelegramError:
            return None

    async def chat_member_status(self, chat_id: int, telegram_id: int) -> str | None:
        try:
            return (await self.bot.get_chat_member(chat_id, telegram_id)).status
        except TelegramError:  # not guaranteed to work unless the bot is an admin of the chat
            return None

    async def delete(self, telegram_id: int, message_id: int) -> None:
        try:
            await self.bot.delete_message(telegram_id, message_id)
        except TelegramError:
            pass  # already deleted or too old: nothing to do
