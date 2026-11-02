"""Group actions shared by the tools, so every client behaves identically."""
from sqlalchemy import select

from mcp_server.db.models import Debt, User
from mcp_server.db.session import session_scope
from mcp_server.domain import friends, groups, notices, views
from mcp_server.domain.errors import UserError
from mcp_server.domain.formatting import money


async def missing_members_note(member_count: int | None, group_id: int) -> str | None:
    """
    A bot cannot list who is in a group. If the chat platform says the chat is bigger than the people we have met,
    say so, so nobody is silently left out of a split. `member_count` is reported by the client (it includes the bot).
    """
    if not member_count:
        return None
    async with session_scope() as s:
        known = len(await groups.members(s, group_id))
    missing = member_count - 1 - known  # the bot itself is one of the members the platform counts
    if missing <= 0:
        return None
    return (f"⚠️ I only know {known} of the {member_count - 1} people in this chat. "
            f"The other {missing} should @mention me once, so I can include them.")


async def settle_and_notify(group_id: int, member_count: int | None = None) -> tuple[str, list[User]]:
    """
    Work out who owes what, save it, queue everyone's bill (with the bills behind their total), and return the
    group-facing summary plus the people who could not be messaged (they never pressed Start).
    """
    try:
        async with session_scope() as s:
            res = await groups.settle(s, group_id)
            debt_ids = [d.id for d in res.debts]
    except UserError as e:
        return f"❌ {e}", []
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
    if note := await missing_members_note(member_count, group_id):
        parts.append(note)
    return "\n\n".join(parts), unreachable


def people_data(users: list[User]) -> list[dict]:
    """People a client should tag with a 'press Start' button: {telegram_id, name}."""
    return [{"telegram_id": u.telegram_id, "name": friends.display(u)} for u in users]
