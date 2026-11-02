from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import select

from mcp_server.config import settings
from mcp_server.db.models import Debt
from mcp_server.db.session import session_scope
from mcp_server.domain import budgets, expenses, friends, notices, splits, views
from mcp_server.domain import debts as debt_logic
from mcp_server.domain.formatting import cat_icon, money
from mcp_server.tools._common import Opt, Out, ask_choice
from mcp_server.tools.registry import tool

EQUAL = Opt("equal", "Equally", ("equal", "equally", "same", "even", "evenly", "half"))
SPECIFY = Opt("exact", "I'll tell the amounts", ("amount", "amounts", "specify", "custom", "unequal", "different", "exact"))
ONE_OFF = Opt("one_off", "One-off split", ("oneoff", "once", "single", "simple", "dinner"))
MAKE_GROUP = Opt("group", "Create a group", ("group", "trip", "event", "create"))
CANCELLED = "👍 Cancelled - nothing was recorded."


class PersonAmount(BaseModel):
    name: str = Field(description="The friend's name as the user said it, or 'me'.")
    amount: float = Field(gt=0, description="That person's share in rupees, as a plain number.")


@tool(contexts=("private",), write=True)
async def split_expense(
    call,
    amount: float,
    description: str,
    with_friends: list[str],
    category: str = "other",
    paid_by: str | None = None,
    split: str | None = None,
    exact_amounts: list[PersonAmount] | None = None,
    include_me: bool = True,
    kind: str | None = None,
) -> Out | str:
    """Split ONE payment between the user and their friends (a dinner, a taxi, tickets...). Each friend is told
    what they owe and can tap 'I paid'. Use the friends' names exactly as the user said them.
    amount: the TOTAL paid. with_friends: names of the other people, e.g. ['Surya', 'Ravi'].
    paid_by: who paid, only if it was NOT the user (a friend's name); default is the user.
    split: 'equal' or 'exact' ONLY if the user said how to split; otherwise leave null and the human will be asked.
    exact_amounts: each person's amount when split='exact' (use 'me' for the user).
    include_me: false only if the user is paying for others and has no share themselves.
    kind: 'one_off' or 'group' ONLY if the user said (e.g. 'for our trip' = 'group'); otherwise null."""
    uid = call.user_id

    # ---- who is involved (read only)
    async with session_scope() as s:
        me = await friends.get_user(s, uid)
        others = [await friends.resolve_user(s, uid, n) for n in with_friends]
        payer = await friends.resolve_user(s, uid, paid_by) if paid_by else me
        named = [(await friends.resolve_user(s, uid, p.name), Decimal(str(p.amount))) for p in (exact_amounts or [])]

    people: list = []

    def add(u):
        if u.id not in {p.id for p in people}:
            people.append(u)

    if payer.id != me.id or include_me:
        add(payer)  # the payer is listed first (they also get any odd paisa)
    if include_me:
        add(me)
    for u in others:
        add(u)
    if split == "exact" and named:
        people = []
        for u, _ in named:
            add(u)
    if len([p for p in people if p.id != payer.id]) == 0:
        return "❌ Tell me who to split with, e.g. 'split with Surya and Ravi'."

    # ---- questions for the human (nothing is written before these)
    if kind is None and len(people) >= settings.ask_group_min_people:
        pick = ask_choice(
            call, "kind",
            f"That's {len(people)} people. Is this a one-off split, or a trip/event you'll keep adding expenses to?",
            [ONE_OFF, MAKE_GROUP],
        )
        if pick is None:
            return CANCELLED
        if pick == "group":
            return notices.group_setup_help()
    mode = split
    if mode not in ("equal", "exact"):
        pick = ask_choice(call, "split_mode", f"How should I split {money(amount)} between {len(people)} people?", [EQUAL, SPECIFY])
        if pick is None:
            return CANCELLED
        mode = pick
    if mode == "exact" and not named:
        example = f"me 1000, {others[0].name or 'Surya'} 2000" if others else "me 1000, Surya 2000"
        return f"Okay. Tell me each person's amount, e.g. '{example}'. They must add up to {money(amount)}. Nothing is recorded yet."

    # ---- do it
    async with session_scope() as s:
        replaced = await expenses.void_if_just_logged(s, uid, amount) if payer.id == uid else None
        shares = splits.equal_shares(Decimal(str(amount)), [p.id for p in people]) if mode == "equal" \
            else splits.exact_shares(Decimal(str(amount)), {u.id: a for u, a in named})
        res = await splits.create_split(
            s, creator_id=uid, payer_id=payer.id, shares=shares, total=Decimal(str(amount)),
            description=description, category=category, mode=mode,
        )
        alerts = await budgets.alerts_for(s, uid, res.logged) if payer.id == uid else []
        debt_ids = [d.id for d in res.debts]
        split_id = res.shared.id
        lines_info = []
        for p in people:
            lines_info.append((p, shares[p.id], await friends.nickname_of(s, uid, p) if p.id != uid else "You"))
        who_paid = "you" if payer.id == uid else await friends.nickname_of(s, uid, payer)

    # ---- tell the friends (queued after the data is saved)
    delivered: dict[int, bool] = {}
    debtor_to_debt: dict[int, int] = {}
    netted: dict[int, tuple[str, Decimal]] = {}  # debt id -> ("settled" | "reduced", amount now)
    net_notes: list[str] = []
    if debt_ids:
        async with session_scope() as s:
            debts = list((await s.execute(select(Debt).where(Debt.id.in_(debt_ids)))).scalars().all())
            # people who already owed each other cancel out (a friend's bill for you, your bill for them)
            for off in await debt_logic.offset_new(s, debts):
                net_notes.append((await notices.notify_offset(s, off, set(debt_ids))).get(uid, ""))
            for d in debts:
                if d.status == "settled":
                    netted[d.id] = ("settled", Decimal(0))
            pending = [d for d in debts if d.status == "pending"]
            delivered = await notices.notify_new_debts(s, pending)
            for d in pending:
                if d.amount != next(sh for p_, sh, _n in lines_info if p_.id == d.debtor_id):
                    netted[d.id] = ("reduced", d.amount)
            debtor_to_debt = {d.debtor_id: d.id for d in debts}

    how = "equally" if mode == "equal" else "by the amounts you gave"
    out = [f"{cat_icon(category)} Split #{split_id} · {description} {money(amount)} · paid by {who_paid} · {how}"]
    if replaced:
        out.append(f"♻️ Your plain expense #{replaced.id} ({money(replaced.amount)}) is replaced by this split, so it isn't counted twice.")
    unreachable = []
    for p, share, nick in lines_info:
        if p.id == payer.id:
            out.append(f"• {nick}: {money(share)} (saved to {'your' if p.id == uid else 'their'} expenses)")
            continue
        debt_id = debtor_to_debt.get(p.id, -1)
        verb = "owe" if nick == "You" else "owes"
        how_netted = netted.get(debt_id)
        if how_netted and how_netted[0] == "settled":
            out.append(f"• {nick} {verb} {money(share)} · cancelled out by what is owed the other way ✓")
            continue
        sent = delivered.get(debt_id, False)
        extra = f" (now {money(how_netted[1])} after netting)" if how_netted else ""
        out.append(f"• {nick} {verb} {money(share)}{extra} " + ("✓ notified" if sent or p.id == uid else "⚠ not notified yet"))
        if not sent and p.id != uid:
            unreachable.append((p, nick))
    out.extend(n for n in net_notes if n)
    out.extend(alerts)
    if unreachable:
        out.append(notices.not_started_hint([nick for _, nick in unreachable]))
    return Out("\n".join(out), {
        "split_ids": [split_id],
        "start_users": [{"telegram_id": p.telegram_id, "name": nick} for p, nick in unreachable],
    })


@tool(contexts=("private",))
async def my_dues(call) -> Out:
    """Show who the user owes money to and who owes the user money (split bills and group settlements), one total per person."""
    async with session_scope() as s:
        return Out(await views.dues_text(s, call.user_id), {"dues": await views.dues_data(s, call.user_id)})


@tool(contexts=("private", "group"), write=True)
async def cancel_split(call, split_id: int | None = None) -> Out:
    """Cancel a split the user created, if nobody has paid their share yet. Omit split_id for the latest one."""
    async with session_scope() as s:
        me = await friends.get_user(s, call.user_id)
        shared, debts = await splits.cancel_split(s, call.user_id, split_id)
        await notices.notify_cancelled(s, debts, me)
        text = f"↩️ Cancelled split #{shared.id} ({shared.description}, {money(shared.amount)}). Everyone was told."
    return Out(text, {"split_ids": [shared.id]})
