"""Ready-to-send text for read-only requests. Used by the LLM tools AND by the bot's no-LLM buttons."""
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import budgets, debts, friends, groups, reports
from app.core.formatting import bar, cat_icon, history_line, money, status_dot
from app.core.periods import month_key, month_label, month_period, resolve_period


async def budgets_text(s: AsyncSession, user_id: int, month: str | None = None) -> str:
    """Budget progress for the current month, or for a past month ('YYYY-MM')."""
    period = month_period(month) if month else None
    rows = await budgets.list_budget_status(s, user_id, period)
    if not rows:
        return "🎯 No budgets yet.\n\nTry saying: set a limit of 3k for food"
    lines = [f"🎯 Budgets · {period.label if period else 'this month'}\n"]
    for r in rows:
        left = f"{money(r.remaining)} left" if r.remaining >= 0 else f"over by {money(-r.remaining)}"
        lines.append(
            f"{status_dot(r.percent)} {cat_icon(r.category)} {r.category}\n"
            f"   {bar(r.percent)} {r.percent}%\n"
            f"   {money(r.spent)} of {money(r.limit)} · {left}"
        )
    return "\n".join(lines)


async def recent_text(s: AsyncSession, user_id: int, *, period: str = "all_time", limit: int = 10, category: str | None = None) -> str:
    rows = await reports.list_expenses(s, user_id, period=resolve_period(period), limit=limit, category=category)
    if not rows:
        return "🧾 No expenses found."
    return "🧾 Recent expenses\n\n" + "\n".join(history_line(e) for e in rows)


async def summary_text(s: AsyncSession, user_id: int, period: str = "this_month") -> str:
    p = resolve_period(period)
    data = await reports.summary(s, user_id, p)
    lines = [f"📊 Summary · {p.label}\n", f"🔴 Spent   {money(data['total_spent'])}"]
    total = data["total_spent"] or 1
    if data["expenses_by_category"]:
        lines.append("")
    for cat, amount in data["expenses_by_category"]:
        pct = int(amount * 100 / total)
        lines.append(f"{cat_icon(cat)} {cat:<12} {money(amount)}  ({pct}%)")
    return "\n".join(lines)


async def trend_text(s: AsyncSession, user_id: int, months: int = 6, category: str | None = None) -> str:
    data = await reports.monthly_trend(s, user_id, months=months, category=category)
    keys, spent = data["months"], data["spent"]
    title = f"📈 {category.strip().lower()} · last {len(keys)} months" if category else f"📈 Last {len(keys)} months"
    if not any(spent.values()):
        return f"{title}\n\nNo expenses in this range yet."
    top = max(spent.values()) or Decimal(1)
    lines = [title, ""]
    for k in keys:
        lines.append(f"{month_label(k)}  {bar(int(spent[k] * 100 / top), 8)} 🔴 {money(spent[k])}")
    total = sum(spent.values(), Decimal(0))
    lines += ["", f"Total spent {money(total)} · avg {money(total / len(keys))}/month"]
    if not category and data["by_category"]:
        lines += ["", "By category"]
        ranked = sorted(data["by_category"].items(), key=lambda kv: -sum(kv[1].values()))[:5]
        for cat, per_month in ranked:
            cells = " · ".join(f"{month_label(k)[:3]} {money(per_month[k])}" for k in keys[-4:])
            lines.append(f"{cat_icon(cat)} {cat}: {cells}")
    return "\n".join(lines)


async def daily_text(s: AsyncSession, user_id: int, month: str | None = None) -> str:
    month = month or month_key()
    data = await reports.daily_breakdown(s, user_id, month)
    if not data["days"]:
        return f"📅 {data['label']}\n\nNo spending recorded."
    peak = max(t for _, t, _, _ in data["days"]) or Decimal(1)
    total = sum((t for _, t, _, _ in data["days"]), Decimal(0))
    lines = [f"📅 Daily spending · {data['label']}", ""]
    for day, amount, n, cat in data["days"]:
        lines.append(f"{day:%d %a}  {bar(int(amount * 100 / peak), 8)} {money(amount)}  {cat_icon(cat)} {cat}" + (f" +{n - 1}" if n > 1 else ""))
    best = max(data["days"], key=lambda r: r[1])
    lines += ["", f"🔴 Total {money(total)} over {len(data['days'])} days · avg {money(total / len(data['days']))}/day",
              f"🔝 Biggest day: {best[0]:%d %b} ({money(best[1])})"]
    return "\n".join(lines)


# ------------------------------------------------------------------ friends, dues, groups
async def friends_text(s: AsyncSession, user_id: int) -> str:
    rows = await friends.list_friends(s, user_id)
    if not rows:
        return "👥 No friends yet.\n\nSay: add friend Surya 123456789\nor send them your invite link (/invite)."
    lines = ["👥 Your friends\n"]
    for f, u in rows:
        status = "✓ can receive messages" if u.started_at else "⏳ hasn't started the bot yet"
        lines.append(f"• {f.nickname}  ·  id {u.telegram_id}  ·  {status}")
    return "\n".join(lines)


async def dues_text(s: AsyncSession, user_id: int) -> str:
    owe, owed = await debts.dues(s, user_id)
    if not owe and not owed:
        return "💳 No pending payments. You're all clear!"
    lines = ["💳 Pending payments"]
    if owe:
        lines += ["", "You owe:"]
        for d in owe:
            who = await friends.nickname_of(s, user_id, await friends.get_user(s, d.creditor_id))
            state = "marked paid, waiting for them to confirm" if d.status == "claimed" else "unpaid"
            lines.append(f"• {who}  {money(d.amount)}  ·  {d.description}  ({state})")
    if owed:
        lines += ["", "Owed to you:"]
        for d in owed:
            who = await friends.nickname_of(s, user_id, await friends.get_user(s, d.debtor_id))
            state = "says they paid: please confirm" if d.status == "claimed" else "unpaid"
            lines.append(f"• {who}  {money(d.amount)}  ·  {d.description}  ({state})")
    return "\n".join(lines)


async def group_text(s: AsyncSession, group_id: int) -> str:
    sm = await groups.summary(s, group_id)
    names = ", ".join(friends.display(u) for u in sm.people)
    lines = [f"🧳 {sm.group.name}", f"Members: {names}"]
    if not sm.expenses:
        lines += ["", "No open expenses yet. Add one: @" + (settings_username() or "bot") + " paid 1000 for petrol"]
        return "\n".join(lines)
    lines += ["", f"Spent so far: {money(sm.total)} in {len(sm.expenses)} expense(s)", ""]
    net = sm.net()
    by_id = {u.id: u for u in sm.people}
    for uid in sorted(net, key=lambda i: friends.display(by_id[i]) if i in by_id else ""):
        if uid not in by_id:
            continue
        n = net[uid]
        verdict = "even" if n == 0 else (f"gets back {money(n)}" if n > 0 else f"owes {money(-n)}")
        lines.append(f"• {friends.display(by_id[uid])}: paid {money(sm.paid.get(uid, 0))} · share {money(sm.share.get(uid, 0))} → {verdict}")
    lines += ["", "Latest:"]
    for e, payer in sm.expenses[-5:]:
        lines.append(f"  #{e.id} {friends.display(payer)} paid {money(e.amount)} · {e.description}")
    lines.append("\nWhen you're done: settle the group to send everyone their bill.")
    return "\n".join(lines)


def settings_username() -> str:
    from app.config import settings
    return settings.bot_username


def person_breakdown(shares: list[tuple[str, Decimal]], paid: list[tuple[str, Decimal]]) -> str:
    """'share ₹70: coffee ₹20, tiffins ₹30 · paid ₹60: coffee ₹60'  - the totals AND the bills behind them, to check the maths."""
    def part(label: str, rows: list[tuple[str, Decimal]]) -> str:
        total = sum((amt for _, amt in rows), Decimal(0))
        return f"{label} {money(total)}: " + ", ".join(f"{desc} {money(amt)}" for desc, amt in rows)

    parts = [part("share", shares)] if shares else []
    if paid:
        parts.append(part("paid", paid))
    return " · ".join(parts)


def settlement_text(
    *,
    header: str,
    names: dict[int, str],
    nets: dict[int, Decimal],
    transfers: list[tuple[int, int, Decimal]],
    shares: dict[int, list[tuple[str, Decimal]]],
    paid: dict[int, list[tuple[str, Decimal]]],
    footer: str | None = None,
) -> str:
    """
    1. WHO PAYS WHOM (the few payments that settle everything), then
    2. one line per person: what they owe / get back = their share minus what they paid, with the bills in brackets.

        💸 Who pays whom
        • K Gopal → Komal Kumar  ₹10
        📋 Each person
        • K Gopal owes ₹10
           (share ₹70: coffee ₹20, tiffins ₹30, cooldrinks ₹20 · paid ₹60: coffee ₹60)
    """
    def verb(name: str, net: Decimal) -> str:
        you = name == "You"
        if net < 0:
            return f"{name} {'owe' if you else 'owes'} {money(-net)}"
        if net > 0:
            return f"{name} {'get' if you else 'gets'} back {money(net)}"
        return f"{name} {'are' if you else 'is'} all even"

    lines = [header, "", "💸 Who pays whom"]
    if transfers:
        lines += [f"• {names[a]} → {names[b]}  {money(amt)}" for a, b, amt in transfers]
    else:
        lines.append("• Nobody needs to pay anyone")
    lines += ["", "📋 Each person  (owes = share − paid)"]
    order = sorted(nets, key=lambda u: (nets[u] >= 0, nets[u]))  # people who owe first, biggest debt first
    for uid in order:
        detail = person_breakdown(shares.get(uid, []), paid.get(uid, []))
        lines.append(f"• {verb(names[uid], nets[uid])}")
        if detail:
            lines.append(f"   ({detail})")
    if footer:
        lines += ["", footer]
    return "\n".join(lines)
