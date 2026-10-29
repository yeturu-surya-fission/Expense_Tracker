"""Group actions shared by the AI tools and the bot's /commands, so both behave identically."""
from sqlalchemy import select

from app.core import friends, groups, markers, notices, views
from app.core.errors import UserError
from app.core.formatting import money
from app.core.notifier import get_notifier
from app.db.models import Debt
from app.db.session import session_scope


async def missing_members_note(chat_id: int | None, group_id: int) -> str | None:
    """
    A bot cannot list who is in a group. If Telegram says the chat is bigger than the people the bot has met,
    say so, so nobody is silently left out of a split.
    """
    if chat_id is None:
        return None
    count = await get_notifier().member_count(chat_id)
    if not count:
        return None
    async with session_scope() as s:
        known = len(await groups.members(s, group_id))
    missing = count - 1 - known  # the bot itself is one of the members Telegram counts
    if missing <= 0:
        return None
    return (f"⚠️ I only know {known} of the {count - 1} people in this chat. "
            f"The other {missing} should @mention me once, so I can include them.")


async def settle_and_notify(group_id: int, chat_id: int | None = None) -> str:
    """
    Work out who owes what, save it, send everyone their bill privately (with the bills behind their total), and
    return the group-facing summary. People the bot cannot message are marked with (start #id) so the bot can
    tag them in the group with a "press Start" button.
    """
    try:
        async with session_scope() as s:
            res = await groups.settle(s, group_id)
            debt_ids = [d.id for d in res.debts]
    except UserError as e:
        return f"❌ {e}"
    breakdown = {u.id: views.person_breakdown(res.share_items.get(u.id, []), res.paid_items.get(u.id, [])) for u in res.people}
    async with session_scope() as s:
        debts = list((await s.execute(select(Debt).where(Debt.id.in_(debt_ids)))).scalars().all()) if debt_ids else []
        unreachable = await notices.notify_settlement(s, res.group.name, res.people, res.transfers, debts, breakdown)

    footer = ("💸 Everyone has been sent exactly who to pay, privately. They tap 'I paid' and the person owed confirms."
              if res.transfers else "Everyone spent equally, so nobody needs to pay anyone. 🎉 Shares are saved in each person's expenses.")
    parts = [views.settlement_text(
        header=f"✅ {res.group.name} settled · {money(res.total)} over {res.count} expense(s)",
        names={u.id: friends.display(u) for u in res.people}, nets=res.nets,
        transfers=[(a.id, b.id, amt) for a, b, amt in res.transfers],
        shares=res.share_items, paid=res.paid_items, footer=footer,
    )]
    if note := await missing_members_note(chat_id, group_id):
        parts.append(note)
    if unreachable:
        parts.append("\n".join(markers.start_marker(u.telegram_id) for u in unreachable))
    return "\n\n".join(parts)
