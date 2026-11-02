"""
Tools only a trusted platform client (the Telegram bot, with a service token) may call. They act on facts the platform
vouches for - 'this Telegram user pressed Start', 'the bot was added to chat X' - for people who have no session of their own.
"""
from pydantic import BaseModel

from mcp_server.db.session import session_scope
from mcp_server.domain import access, friends, groups, outbox
from mcp_server.domain.errors import UserError
from mcp_server.tools._common import Out
from mcp_server.tools.registry import tool


# ------------------------------------------------------------------ outbox: messages for other people
@tool(scope="platform")
async def platform_outbox_claim(call, limit: int = 20) -> Out:
    """Take the next messages waiting to be delivered to people (a friend told 'you owe 750', a reminder, a message to delete).
    Deliver each one, then report with platform_outbox_ack. An unacknowledged message comes back after 60 seconds."""
    async with session_scope() as s:
        rows = await outbox.claim(s, max(1, min(limit, 50)))
        messages = [
            {"id": r.id, "kind": r.kind, "telegram_id": r.telegram_id, "text": r.text, "buttons": r.buttons, "message_id": r.message_id}
            for r in rows
        ]
    return Out("", {"messages": messages})


@tool(scope="platform", write=True)
async def platform_outbox_ack(call, outbox_id: int, message_id: int | None = None, error: str | None = None, blocked: bool = False) -> Out:
    """Report the result of delivering one outbox message. On success pass the platform's message id. Pass blocked=true
    when the person cannot be messaged (never pressed Start / blocked the bot): their bills are kept and sent when they start."""
    async with session_scope() as s:
        status = await outbox.ack(s, outbox_id, message_id, error, blocked)
    return Out("", {"status": status})


# ------------------------------------------------------------------ people
class Person(BaseModel):
    telegram_id: int
    name: str | None = None
    username: str | None = None


@tool(scope="platform", write=True)
async def platform_accept_invite(call, person: Person, inviter_telegram_id: int) -> Out:
    """Someone opened a friend's invite link: connect them both as friends (each side uses the other's name)."""
    if inviter_telegram_id == person.telegram_id:
        raise UserError("That invite link isn't valid.")
    async with session_scope() as s:
        if not await access.allowed(s, inviter_telegram_id):
            raise UserError("That invite link isn't valid.")
        inviter = await friends.upsert_user(s, inviter_telegram_id)
        me = await friends.upsert_user(s, person.telegram_id, person.name, person.username)  # 'started' is recorded when they open a session
        inviter_name = friends.display(inviter)
        for owner, other in ((inviter, me), (me, inviter)):
            if any(u.id == other.id for _, u in await friends.list_friends(s, owner.id)):
                continue
            nick = (friends.display(other) or "Friend")[:30]
            try:
                async with s.begin_nested():
                    await friends.add_friend(s, owner.id, other.telegram_id, nick)
            except UserError:  # that nickname is taken in their list: make it unique
                await friends.add_friend(s, owner.id, other.telegram_id, f"{nick} {other.telegram_id % 1000}")
        if inviter.started_at:
            await outbox.enqueue(
                s, inviter.telegram_id,
                f"👥 {person.name or friends.display(me)} joined your friends through your invite link. You can split bills with them now.\n"
                f"(To rename them: 'add friend NewName {person.telegram_id}')",
            )
    return Out(f"🤝 You're now friends with {inviter_name or 'them'}. They can split bills with you, and you'll get a message when they do.", {"inviter": inviter_name})


@tool(scope="platform")
async def platform_friend_ids(call, telegram_id: int) -> Out:
    """Telegram ids of a person's friends (so the platform can check which of them are in a chat)."""
    async with session_scope() as s:
        user = await friends.upsert_user(s, telegram_id)
        ids = [u.telegram_id for _, u in await friends.list_friends(s, user.id)]
    return Out("", {"telegram_ids": ids})


# ------------------------------------------------------------------ group chat events
@tool(scope="platform", write=True)
async def platform_group_event(
    call,
    event: str,
    chat_id: int,
    title: str | None = None,
    actor: Person | None = None,
    members: list[Person] | None = None,
    present_telegram_ids: list[int] | None = None,
    new_chat_id: int | None = None,
) -> Out:
    """Something happened in a group chat. event: 'bot_added' (actor = who added the bot, present_telegram_ids = the
    actor's friends the platform found in the chat), 'members_joined' (members), 'member_left' (actor), 'title_changed'
    (title), 'migrated' (new_chat_id). bot_added returns data.action = 'leave' if the actor may not add the bot."""
    async with session_scope() as s:
        if event == "bot_added":
            if actor is None:
                raise UserError("bot_added needs the person who added the bot.")
            if not access.can_add_bot(actor.telegram_id):
                return Out("", {"action": "leave"})
            adder = await friends.upsert_user(s, actor.telegram_id, actor.name, actor.username)
            group, created = await groups.ensure_group(s, chat_id, title, adder.id)
            await groups.join_group(s, group.id, adder.id)
            for tg in present_telegram_ids or []:
                await groups.join_group(s, group.id, (await friends.upsert_user(s, tg)).id)
            names = [friends.display(u) for u in await groups.members(s, group.id)]
            return Out("", {"action": "ok", "created": created, "group_id": group.id, "group_name": group.name, "members": names})
        group = await groups.group_for_chat(s, chat_id)
        if group is None:
            return Out("", {"action": "ignored"})
        if event == "members_joined":
            for m in members or []:
                user = await friends.upsert_user(s, m.telegram_id, m.name, m.username)
                await groups.join_group(s, group.id, user.id)
        elif event == "member_left" and actor is not None:
            await groups.mark_left(s, group.id, (await friends.upsert_user(s, actor.telegram_id)).id)
        elif event == "title_changed":
            await groups.rename_group(s, chat_id, title)
        elif event == "migrated" and new_chat_id is not None:
            await groups.move_group(s, chat_id, new_chat_id)
        else:
            raise UserError(f"Unknown group event '{event}'.")
    return Out("", {"action": "ok"})
