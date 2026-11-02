from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import select

from mcp_server.config import settings
from mcp_server.db.models import Debt
from mcp_server.db.session import session_scope
from mcp_server.domain import debts as debt_logic
from mcp_server.domain import friends, group_flow, groups, notices, splits, views
from mcp_server.domain.formatting import money
from mcp_server.tools._common import Out, ask_choice
from mcp_server.tools.groups import group_context
from mcp_server.tools.registry import tool
from mcp_server.tools.splits import CANCELLED, MAKE_GROUP, ONE_OFF


class BillItem(BaseModel):
    amount: float = Field(gt=0, description="What this bill came to, as a plain number.")
    description: str = Field(description="What it was for, e.g. 'party food', 'ice cream', 'bar'.")
    category: str = Field(default="other", description="One lowercase word: food, drinks, transport, stay, entertainment...")
    paid_by: str = Field(default="me", description="Who paid THIS bill: a friend's / member's name exactly as said, or 'me'.")


@tool(contexts=("private", "group"), write=True)
async def split_bills(
    call,
    bills: list[BillItem],
    with_friends: list[str] | None = None,
    title: str = "split",
    include_me: bool = True,
    kind: str | None = None,
) -> Out | str:
    """Split SEVERAL bills that DIFFERENT people paid during the same outing (a party night: dinner paid by one friend,
    ice cream by another, the bar by a third), and work out who owes what, with the fewest payments.
    Use this ONCE for all the bills instead of calling split_expense / add_group_expense several times. Everything is
    shared equally between everyone involved. For ONE bill use split_expense (private) or add_group_expense (group).
    bills: every bill with its amount, description and who paid it ('me' for the sender).
    with_friends: the OTHER people on the outing, e.g. ['Surya', 'Komal']. In a GROUP chat leave it empty for everyone in the group.
    title: a short name for the outing, e.g. 'party night'.
    include_me: false only if the sender was not part of it.
    kind: 'one_off' or 'group' ONLY if the sender said so; otherwise null (private chat only)."""
    uid = call.user_id
    in_group = call.chat_type == "group"

    # ---- who is involved (read only)
    async with session_scope() as s:
        me = await friends.get_user(s, uid)
        if in_group:
            group = await group_context(s, call)
            everyone = await groups.members(s, group.id)
            others = [await groups.resolve_member(s, group, uid, n) for n in with_friends] if with_friends else everyone
            payers = [await groups.resolve_member(s, group, uid, b.paid_by or "me") for b in bills]
            title = title if title != "split" else group.name
        else:
            if not with_friends:
                return "❌ Tell me who was there, e.g. 'with Surya and Komal'."
            others = [await friends.resolve_user(s, uid, n) for n in with_friends]
            payers = [await friends.resolve_user(s, uid, b.paid_by or "me") for b in bills]
    people: list = []

    def add(u):
        if u.id not in {p.id for p in people}:
            people.append(u)

    if include_me:
        add(me)
    for u in [*others, *payers]:
        add(u)
    if len(people) < 2:
        return "❌ Tell me who was there, e.g. 'with Surya and Komal'."

    # ---- question for the human (private chat only; nothing is written before this)
    if not in_group and kind is None and len(people) >= settings.ask_group_min_people:
        pick = ask_choice(
            call, "kind",
            f"That's {len(people)} people. Is this a one-off split, or a trip/event you'll keep adding expenses to?",
            [ONE_OFF, MAKE_GROUP],
        )
        if pick is None:
            return CANCELLED
        if pick == "group":
            return notices.group_setup_help(title)

    # ---- do it
    specs = [
        splits.BillSpec(payer_id=payer.id, amount=Decimal(str(b.amount)), description=b.description, category=b.category,
                        participant_ids=[p.id for p in people])
        for b, payer in zip(bills, payers)
    ]
    async with session_scope() as s:
        res = await splits.create_bundle(s, creator_id=uid, title=title, bills=specs)
        debt_ids = [d.id for d in res.debts]
        first_id = res.shared[0].id
        names = {p.id: ("You" if (p.id == uid and not in_group) else
                        friends.display(p) if in_group else await friends.nickname_of(s, uid, p)) for p in people}
        my_logged = sum((e.amount for e in res.logged.get(uid, [])), Decimal(0))

    # ---- queue everyone's bill (after the data is saved)
    unreachable = []
    net_notes: list[str] = []
    if res.debts or res.logged:
        async with session_scope() as s:
            debts = list((await s.execute(select(Debt).where(Debt.id.in_(debt_ids)))).scalars().all()) if debt_ids else []
            for off in await debt_logic.offset_new(s, debts):  # people who already owed each other cancel out
                net_notes.append((await notices.notify_offset(s, off, set(debt_ids))).get(uid, ""))
            debts = [d for d in debts if d.status == "pending"]
            by_id = {p.id: p for p in people}
            transfers = [(by_id[a], by_id[b], amt) for a, b, amt in res.transfers]
            breakdown = {p.id: views.person_breakdown(res.share_items.get(p.id, []), res.paid_items.get(p.id, [])) for p in people}
            unreachable = await notices.notify_settlement(s, title, people, transfers, debts, breakdown)

    each = res.total / len(people)
    footer = ("💸 Everyone has been sent exactly who to pay, privately. They tap 'I paid' and the person owed confirms."
              if res.transfers else "Everyone paid their fair share, so nobody owes anyone anything. 🎉")
    if my_logged and not in_group:
        footer += f"\n💾 Your share, {money(my_logged)}, is saved to your expenses."
    elif uid in {a for a, _, _ in res.transfers} and not in_group:
        footer += "\nYour share is saved to your expenses when you tap 'I paid'."
    out = [views.settlement_text(
        header=f"🎉 {title} · {len(bills)} bills · total {money(res.total)} · equally among {len(people)} (about {money(each)} each)",
        names=names, nets=res.net(), transfers=res.transfers, shares=res.share_items, paid=res.paid_items, footer=footer,
    )]
    if not in_group:
        out.extend(n for n in net_notes if n)
    start_users = group_flow.people_data(unreachable) if in_group else []
    if unreachable and not in_group:
        out.append(notices.not_started_hint([friends.display(u) for u in unreachable]))
    return Out("\n".join(out), {
        "split_ids": [first_id], "start_users": start_users,
        **({"group_id": group.id} if in_group else {}),
    })
