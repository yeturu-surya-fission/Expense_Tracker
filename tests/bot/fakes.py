"""Stand-ins for Telegram objects, just enough for the handlers: they record what the bot says instead of sending it."""
import itertools
from types import SimpleNamespace

from telegram.error import Forbidden

_mid = itertools.count(100)


class FakeMessage:
    def __init__(self, text="", chat=None, user=None, entities=None, reply_to=None, **extra):
        self.text, self.chat, self.from_user, self.entities, self.reply_to_message = text, chat, user, entities or [], reply_to
        self.chat_id = chat.id if chat else None
        self.message_id = next(_mid)
        self.sent: list[dict] = []
        self.deleted = False
        self.__dict__.update(extra)

    async def reply_text(self, text, reply_markup=None, **kw):
        m = FakeMessage(text, self.chat)
        m.reply_markup, m.kw = reply_markup, kw
        self.sent.append({"text": text, "markup": reply_markup, **kw})
        return m

    async def edit_text(self, text, reply_markup=None, **kw):
        self.text = text
        self.sent.append({"text": text, "markup": reply_markup, "edit": True})

    async def delete(self):
        self.deleted = True


class FakeQuery:
    def __init__(self, data, message, user):
        self.data, self.message, self.from_user = data, message, user
        self.alerts, self.edited, self.markup_cleared = [], [], False

    async def answer(self, text=None, show_alert=False):
        if text:
            self.alerts.append(text)

    async def edit_message_text(self, text, reply_markup=None):
        self.edited.append({"text": text, "markup": reply_markup})
        self.message.text = text

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_cleared = reply_markup is None


class FakeBot:
    """The Telegram Bot API: records sends/deletes; `blocked` ids raise Forbidden like a person who never pressed Start."""

    def __init__(self, username="expense_test_bot", member_count=None, chat_members=None):
        self.username, self.id = username, 999
        self.sent: list[dict] = []
        self.deleted: list[tuple] = []
        self.left: list[int] = []
        self.blocked: set[int] = set()
        self.member_count = member_count
        self.chat_members = chat_members or {}  # {(chat_id, user_id): status}

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if chat_id in self.blocked:
            raise Forbidden("bot can't initiate conversation with a user")
        mid = next(_mid)
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup, "message_id": mid, **kw})
        return SimpleNamespace(message_id=mid)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def leave_chat(self, chat_id):
        self.left.append(chat_id)

    async def send_chat_action(self, chat_id, action):
        return None

    async def get_chat_member_count(self, chat_id):
        return self.member_count

    async def get_chat_member(self, chat_id, user_id):
        status = self.chat_members.get((chat_id, user_id))
        if status is None:
            from telegram.error import BadRequest
            raise BadRequest("user not found")
        return SimpleNamespace(status=status)


def tg_user(tid, name="Kiran", username=None, is_bot=False):
    return SimpleNamespace(id=tid, full_name=name, username=username, is_bot=is_bot)


def private_chat(tid):
    return SimpleNamespace(id=tid, type="private", title=None)


def group_chat(cid, title="Trip"):
    return SimpleNamespace(id=cid, type="supergroup", title=title)


def buttons(markup) -> list[tuple[str, str | None, str | None]]:
    """[(label, callback_data, url)] of an inline keyboard, flattened."""
    if markup is None:
        return []
    return [(b.text, b.callback_data, b.url) for row in markup.inline_keyboard for b in row]


def update(user=None, message=None, query=None, **extra):
    return SimpleNamespace(effective_user=user, effective_message=message, callback_query=query, **extra)


def context(bot, args=None):
    return SimpleNamespace(bot=bot, args=args or [])
