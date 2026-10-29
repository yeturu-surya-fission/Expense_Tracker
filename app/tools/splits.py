from decimal import Decimal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import settings
from app.core import budgets, friends, markers, notices, splits, views
from app.core.errors import UserError
from app.core.formatting import cat_icon, money
from app.db.models import Debt
from app.db.session import session_scope
from app.tools._common import Opt, ask_choice, user_id_from

EQUAL = Opt("equal", "Equally", ("equal", "equally", "same", "even", "evenly", "half"))
SPECIFY = Opt("exact", "I'll tell the amounts", ("amount", "amounts", "specify", "custom", "unequal", "different", "exact"))
ONE_OFF = Opt("one_off", "One-off split", ("oneoff", "once", "single", "simple", "dinner"))
MAKE_GROUP = Opt("group", "Create a group", ("group", "trip", "event", "create"))


class PersonAmount(BaseModel):
    name: str = Field(description="The friend's name as the user said it, or 'me'.")
    amount: float = Field(gt=0, description="That person's share in rupees, as a plain number.")


@tool
async def split_expense(
    amount: float,
    description: str,
    with_friends: list[str],
    config: RunnableConfig,
    category: str = "other",
    paid_by: str | None = None,
    split: str | None = None,
    exact_amounts: list[PersonAmount] | None = None,
    include_me: bool = True,
    kind: str | None = None,
) -> str:
    """Split ONE payment between the user and their friends (a dinner, a taxi, tickets...). Each friend is told
    what they owe and can tap 'I paid'. Use the friends' names exactly as the user said them.
    amount: the TOTAL paid. with_friends: names of the other people, e.g. ['Surya', 'Ravi'].
    paid_by: who paid, only if it was NOT the user (a friend's name); default is the user.
    split: 'equal' or 'exact' ONLY if the user said how to split; otherwise leave null and the bot will ask.
    exact_amounts: each person's amount when split='exact' (use 'me' for the user).
    include_me: false only if the user is paying for others and has no share themselves.
    kind: 'one_off' or 'group' ONLY if the user said (e.g. 'for our trip' = 'group'); otherwise null."""
    uid = user_id_from(config)

    # ---- who is involved (read only)
    async with session_scope() as s:
        try:
            me = await friends.get_user(s, uid)
            others = [await friends.resolve_user(s, uid, n) for n in with_friends]
            payer = await friends.resolve_user(s, uid, paid_by) if paid_by else me
            named = [(await friends.resolve_user(s, uid, p.name), Decimal(str(p.amount))) for p in (exact_amounts or [])]
        except UserError as e:
            return f"❌ {e}"

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
            f"That's {len(people)} people. Is this a one-off split, or a trip/event you'll keep adding expenses to?",
            [ONE_OFF, MAKE_GROUP],
        )
        if pick is None:
            return "👍 Cancelled - nothing was recorded."
        if pick == "group":
            return notices.group_setup_help()
    mode = split
    if mode not in ("equal", "exact"):
        pick = ask_choice(f"How should I split {money(amount)} between {len(people)} people?", [EQUAL, SPECIFY])
        if pick is None:
            return "👍 Cancelled - nothing was recorded."
        mode = pick
    if mode == "exact" and not named:
        example = f"me 1000, {others[0].name or 'Surya'} 2000" if others else "me 1000, Surya 2000"
        return f"Okay. Tell me each person's amount, e.g. '{example}'. They must add up to {money(amount)}. Nothing is recorded yet."

    # ---- do it
    try:
        async with session_scope() as s:
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
    except UserError as e:
        return f"❌ {e}"

    # ---- tell the friends (after the data is saved)
    delivered: dict[int, bool] = {}
    debtor_to_debt: dict[int, int] = {}
    if debt_ids:
        async with session_scope() as s:
            debts = list((await s.execute(select(Debt).where(Debt.id.in_(debt_ids)))).scalars().all())
            delivered = await notices.notify_new_debts(s, debts)
            debtor_to_debt = {d.debtor_id: d.id for d in debts}

    who_paid = "you" if payer.id == uid else await _nick(uid, payer)
    how = "equally" if mode == "equal" else "by the amounts you gave"
    out = [f"{cat_icon(category)} Split #{split_id} · {description} {money(amount)} · paid by {who_paid} · {how}"]
    unreachable = []
    for p, share, nick in lines_info:
        if p.id == payer.id:
            out.append(f"• {nick}: {money(share)} (saved to {'your' if p.id == uid else 'their'} expenses)")
            continue
        sent = delivered.get(debtor_to_debt.get(p.id, -1), False)
        verb = "owe" if nick == "You" else "owes"
        out.append(f"• {nick} {verb} {money(share)} " + ("✓ notified" if sent or p.id == uid else "⚠ not notified yet"))
        if not sent and p.id != uid:
            unreachable.append(nick)
    out.append(markers.split_marker(split_id))
    out.extend(alerts)
    if unreachable:
        out.append(notices.not_started_hint(unreachable))
    return "\n".join(out)


async def _nick(uid: int, other) -> str:
    async with session_scope() as s:
        return await friends.nickname_of(s, uid, other)


@tool
async def my_dues(config: RunnableConfig) -> str:
    """Show who the user owes money to and who owes the user money (split bills and group settlements)."""
    async with session_scope() as s:
        return await views.dues_text(s, user_id_from(config))


@tool
async def cancel_split(config: RunnableConfig, split_id: int | None = None) -> str:
    """Cancel a split the user created, if nobody has paid their share yet. Omit split_id for the latest one."""
    uid = user_id_from(config)
    try:
        async with session_scope() as s:
            me = await friends.get_user(s, uid)
            shared, debts = await splits.cancel_split(s, uid, split_id)
            await notices.notify_cancelled(s, debts, me)
    except UserError as e:
        return f"❌ {e}"
    return f"↩️ Cancelled split #{shared.id} ({shared.description}, {money(shared.amount)}). Everyone was told."
