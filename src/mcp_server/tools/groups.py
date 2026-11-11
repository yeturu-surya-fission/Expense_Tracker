from decimal import Decimal

from fastmcp import Context

from mcp_server.tools import mcp

from mcp_server.core import friends, group_flow, groups, markers, notices, splits, views
from mcp_server.core.errors import UserError
from mcp_server.core.formatting import cat_icon, money
from mcp_server.database.session import session_scope
from mcp_server.tools._common import Opt, ask_choice, answer_id, chat_type, group_chat_id, user_id_from
from mcp_server.tools.splits import EQUAL, SPECIFY
from shared.models import PersonAmount

ALWAYS_EQUAL = Opt("always_equal", "Always equally in this group", ("always", "every", "default"))
YES_SETTLE = Opt("yes", "Yes, settle up", ("yes", "ok", "sure", "confirm", "settle", "go"))
NOT_IN_GROUP_CHAT = "❌ Group expenses are added inside the group chat. Open the group and mention me there."


async def group_context(s, uid: int, chat_type: str, group_chat_id: int | None):
    """
    (group, error) for a message sent in a group chat. Whoever talks to the bot in a group is part of that group:
    there is no 'join' step.
    """
    if chat_type != "group" or group_chat_id is None:
        return None, NOT_IN_GROUP_CHAT
    group = await groups.group_for_chat(s, group_chat_id)
    if group is None:
        return None, "❌ I'm not set up for this chat yet. Remove me from the group and add me again."
    await groups.join_group(s, group.id, uid)
    return group, None


@mcp.tool
async def create_group(name: str, ctx: Context) -> str:
    """Set up an expense group for a trip or event. The user creates a Telegram group and adds the bot: nothing else
    is needed. Use this when the user asks how to create or start a group (in a private chat), or tries to create one."""
    if await chat_type(ctx) == "group":
        return "✅ This chat is already an expense group. Just mention me with an expense, e.g. 'I paid 1000 for petrol'."
    return notices.group_setup_help(name)


@mcp.tool
async def add_group_expense(
    amount: float,
    description: str,
    ctx: Context,
    category: str = "other",
    split_between: list[str] | None = None,
    split: str | None = None,
    exact_amounts: list[PersonAmount] | None = None,
    include_me: bool = True,
    paid_by: str | None = None,
) -> str:
    """Add an expense in the group chat. It is shared between the group members.
    paid_by: a member's name, ONLY when someone OTHER than the sender paid ('Surya paid 600'); default: the sender paid.
    split_between: names of the OTHER members to share it with; leave null for ALL members.
    split: 'equal' or 'exact' ONLY if the sender said so; 'always_equal' when the sender picks 'Always equally'
    (the group's default is saved too); otherwise leave null and the bot will ask.
    exact_amounts: each member's amount when split='exact' (use 'me' for the sender).
    include_me: the sender also has a share (default true); false only if they paid purely for others.
    HUMAN QUESTION: this tool may return a question instead of doing the work. Show it to the user, then call this
    tool again with their answer in `split`, repeating every earlier answer."""
    uid = await user_id_from(ctx)
    async with session_scope() as s:
        group, err = await group_context(s, uid, await chat_type(ctx), await group_chat_id(ctx))
        if err:
            return err
        try:
            everyone = await groups.members(s, group.id)
            chosen = [await groups.resolve_member(s, group, uid, n) for n in split_between] if split_between else everyone
            payer = await groups.resolve_member(s, group, uid, paid_by) if paid_by else await friends.get_user(s, uid)
            if split_between and include_me and uid not in {u.id for u in chosen}:
                chosen = [await friends.get_user(s, uid), *chosen]  # "between me and Surya": the sender shares it too
            if split_between and payer.id not in {u.id for u in chosen}:
                chosen = [payer, *chosen]  # whoever paid is part of the split unless they were left out on purpose
            named = [(await groups.resolve_member(s, group, uid, p.name), Decimal(str(p.amount))) for p in (exact_amounts or [])]
        except UserError as e:
            return f"❌ {e}"
        group_id, default_split, group_name = group.id, group.default_split, group.name
    if split == "exact" and named:
        chosen = [u for u, _ in named]
    if not chosen:
        return "❌ Nobody to split with."

    mode = split
    if mode not in ("equal", "exact"):
        pick = answer_id(mode, [EQUAL, ALWAYS_EQUAL, SPECIFY]) if mode else None
        if pick is None:
            if default_split == "equal":
                mode = "equal"
            else:
                return ask_choice(f"How should I split {money(amount)} between {len(chosen)} people in {group_name}?",
                                  [EQUAL, ALWAYS_EQUAL, SPECIFY])
        else:
            mode = "equal"
            if pick == "always_equal":
                async with session_scope() as s:
                    await groups.set_default_split(s, group_id, "equal")
    if mode == "exact" and not named:
        return f"Okay. Tell me each person's amount, e.g. 'me 500, Surya 700'. They must add up to {money(amount)}. Nothing is recorded yet."

    try:
        async with session_scope() as s:
            total = Decimal(str(amount))
            ids = [u.id for u in chosen]
            if payer.id in ids:  # the payer is listed first (they also get any odd paisa)
                ids = [payer.id] + [i for i in ids if i != payer.id]
            so_far = (await groups.summary(s, group_id)).share  # spread odd paise fairly across the trip's expenses
            shares = splits.equal_shares(total, ids, already=so_far) if mode == "equal" else splits.exact_shares(total, {u.id: a for u, a in named})
            res = await splits.create_split(
                s, creator_id=uid, payer_id=payer.id, shares=shares, total=total, description=description,
                category=category, mode=mode, group_id=group_id,
            )
            names = {u.id: friends.display(u) for u in chosen}
            payer_name = friends.display(payer)
    except UserError as e:
        return f"❌ {e}"
    lines = [f"{cat_icon(category)} {payer_name} paid {money(amount)} for {description} · #{res.shared.id}"]
    if mode == "equal":
        each = next(iter(shares.values()))
        who = ", ".join(names.get(u, "?") for u in shares)
        lines.append(f"Split equally among {len(shares)} ({money(each)} each): {who}.")
    else:
        lines.append("Split: " + ", ".join(f"{names.get(u, '?')} {money(a)}" for u, a in shares.items()))
    lines.append(markers.split_marker(res.shared.id))
    return "\n".join(lines)


@mcp.tool
async def group_summary(ctx: Context) -> str:
    """Show the group's open expenses: who paid what, each member's share and who is owed. Group chat only."""
    async with session_scope() as s:
        group, err = await group_context(s, await user_id_from(ctx), await chat_type(ctx), await group_chat_id(ctx))
        if err:
            return err
        text = await views.group_text(s, group.id)
        group_id = group.id
    note = await group_flow.missing_members_note(await group_chat_id(ctx), group_id)
    return f"{text}\n\n{note}" if note else text


@mcp.tool
async def settle_group(ctx: Context, confirm: bool = False) -> str:
    """Settle up the group: work out who owes what and send each person their bill privately. Group chat only.
    Use when someone says settle up / split the bill / finish the trip / who owes whom.
    CONFIRMATION: the first call returns a question; show it to the user and, only when they say yes,
    call again with confirm=true (if they say no / cancel, just tell them you're not settling)."""
    async with session_scope() as s:
        group, err = await group_context(s, await user_id_from(ctx), await chat_type(ctx), await group_chat_id(ctx))
        if err:
            return err
        sm = await groups.summary(s, group.id)
        if not sm.expenses:
            return f"Nothing to settle in {group.name} yet. Add an expense first."
        group_id, group_name, total = group.id, group.name, sm.total
    note = await group_flow.missing_members_note(await group_chat_id(ctx), group_id)
    if not confirm:
        question = f"Settle up {group_name} now? Total {money(total)}. I'll send everyone their bill privately."
        if note:
            question += f"\n\n{note}"
        return ask_choice(question, [YES_SETTLE])
    return await group_flow.settle_and_notify(group_id, await group_chat_id(ctx))
