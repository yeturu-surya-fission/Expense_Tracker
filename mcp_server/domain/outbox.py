"""
Messages for people other than the caller ('you owe 750'). The server never talks to Telegram: it writes Outbox rows in the
same transaction as the data, and a platform client (the Telegram bot) claims, delivers and acknowledges them.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.db.models import Debt, Outbox, User

MAX_ATTEMPTS = 5
LEASE_SECONDS = 60
# the only columns an acknowledgement may write a Telegram message id into
REF_FIELDS = {("debts", "debtor_msg_id"), ("debts", "creditor_msg_id")}
QUEUED = 0  # stored in a message-id column while the message waits in the outbox (None = never sent / not reachable)


@dataclass(frozen=True)
class Button:
    label: str
    data: str | None = None  # callback data
    url: str | None = None


def _buttons_json(buttons: list[list[Button]] | None) -> list | None:
    if not buttons:
        return None
    return [[{"label": b.label, **({"url": b.url} if b.url else {"data": b.data})} for b in row] for row in buttons]


async def enqueue(
    session: AsyncSession, telegram_id: int, text: str, buttons: list[list[Button]] | None = None,
    ref: tuple[str, int, str] | None = None,
) -> int:
    """Queue a private message. `ref` = (table, row id, column) where the delivered message id will be stored."""
    if ref is not None and (ref[0], ref[2]) not in REF_FIELDS:
        raise ValueError(f"outbox cannot write to {ref[0]}.{ref[2]}")
    row = Outbox(
        kind="send", telegram_id=telegram_id, text=text[:4096], buttons=_buttons_json(buttons),
        ref_table=ref[0] if ref else None, ref_id=ref[1] if ref else None, ref_field=ref[2] if ref else None,
    )
    session.add(row)
    await session.flush()
    return row.id


async def retract(session: AsyncSession, telegram_id: int, message_id: int | None, ref: tuple[str, int, str] | None = None) -> None:
    """Make a message disappear: delete it if it was delivered, or cancel it if it is still waiting."""
    if message_id:
        session.add(Outbox(kind="delete", telegram_id=telegram_id, message_id=message_id))
    if message_id == QUEUED and ref is not None:
        await session.execute(
            update(Outbox).where(
                Outbox.ref_table == ref[0], Outbox.ref_id == ref[1], Outbox.ref_field == ref[2], Outbox.status.in_(("pending", "leased"))
            ).values(status="cancelled")
        )
    await session.flush()


async def claim(session: AsyncSession, limit: int = 20) -> list[Outbox]:
    """Hand the next due messages to a deliverer. A claimed message that is never acknowledged becomes due again."""
    now = func.now()  # the database's clock decides what is due (Python's clock is coarser on Windows and can lag by milliseconds)
    rows = (await session.execute(
        select(Outbox).where(
            (Outbox.status == "pending") | ((Outbox.status == "leased") & (Outbox.leased_until < now)),
            Outbox.available_at <= now,
        ).order_by(Outbox.id).limit(limit).with_for_update(skip_locked=True)
    )).scalars().all()
    for r in rows:
        r.status, r.leased_until, r.attempts = "leased", now + timedelta(seconds=LEASE_SECONDS), r.attempts + 1
    await session.flush()
    return list(rows)


async def ack(session: AsyncSession, outbox_id: int, message_id: int | None = None, error: str | None = None, blocked: bool = False) -> str:
    """
    The deliverer reports back. Success stores the Telegram message id. `blocked` = the person never pressed Start (or
    blocked the bot): we remember that, so their bills are sent the moment they do. Other errors are retried with a delay.
    """
    row = await session.get(Outbox, outbox_id, with_for_update=True)
    if row is None or row.status in ("sent", "cancelled", "blocked", "failed"):
        return row.status if row else "unknown"
    if error is None and not blocked:
        row.status, row.leased_until, row.error = "sent", None, None
        if row.kind == "send":
            row.message_id = message_id
            if row.ref_table and row.ref_id and message_id and (row.ref_table, row.ref_field) in REF_FIELDS:
                await session.execute(update(Debt).where(Debt.id == row.ref_id).values({row.ref_field: message_id}))
    elif blocked:
        row.status, row.leased_until, row.error = "blocked", None, (error or "not started")[:200]
        user = (await session.execute(select(User).where(User.telegram_id == row.telegram_id))).scalar_one_or_none()
        if user is not None:
            user.started_at = None
        if row.ref_table and row.ref_id and (row.ref_table, row.ref_field) in REF_FIELDS:
            await session.execute(update(Debt).where(Debt.id == row.ref_id).values({row.ref_field: None}))
    else:
        row.error = (error or "")[:200]
        if row.attempts >= MAX_ATTEMPTS:
            row.status, row.leased_until = "failed", None
        else:
            row.status, row.leased_until = "pending", None
            row.available_at = datetime.now(timezone.utc) + timedelta(seconds=15 * row.attempts)
    await session.flush()
    return row.status
