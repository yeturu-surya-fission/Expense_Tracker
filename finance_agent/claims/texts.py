"""The words used about a claim: for the employee, and for the manager's approval card."""

from decimal import Decimal

from finance_agent.db.models import Claim
from finance_agent.formatting import money, q2
from finance_agent.policy.models import Item, Verdict

ICON = {
    "eligible": "✅",
    "partial": "⚠️",
    "not_eligible": "❌",
    "needs_info": "❓",
    "needs_manager": "❓",
}


def no(claim: Claim) -> int:
    """The number the EMPLOYEE knows this claim by (their expense tracker's number). The manager's cards use the Finance Agent's own number."""
    return claim.external_no or claim.id


def items_of(claim: Claim) -> list[Item]:
    return [Item.model_validate(i) for i in claim.items]


def verdicts_of(claim: Claim) -> list[Verdict]:
    return [Verdict.model_validate(v) for v in claim.verdicts]


def claimed_total(claim: Claim) -> Decimal:
    return q2(sum((i.amount or Decimal(0) for i in items_of(claim)), Decimal(0)))


def label(item: Item) -> str:
    return (item.description or item.category or f"item {item.ref}").strip()[:50]


def lines(claim: Claim) -> list[str]:
    by_ref = {v.ref: v for v in verdicts_of(claim)}
    out = []
    for item in items_of(claim):
        v = by_ref.get(item.ref)
        got = money(v.eligible) if v and v.eligible is not None else "?"
        out.append(
            f"{ICON.get(v.status, '•') if v else '•'} {label(item)}: {money(item.amount or 0)} → {got}"
            + (f" — {v.reason}" if v and v.status not in ("eligible",) else "")
            + (
                f" (check: {v.check[:90]})"
                if v and v.check and v.status in ("eligible", "partial")
                else ""
            )
        )
    return out


def manager_card(claim: Claim, employee_name: str) -> str:
    return (
        f"📨 Claim #{claim.id} · {employee_name} · {claim.title}\n\n"
        + "\n".join(lines(claim))
        + f"\n\nPolicy suggests {money(claim.recommended)} of {money(claimed_total(claim))} claimed. (Policy v{claim.policy_version})"
    )


def decided_line(d: dict) -> str:
    """One expense in the final decision: what was claimed, what is paid, and why not all of it."""
    claimed, got = Decimal(d["claimed"]), Decimal(d["approved"])
    if got >= claimed:
        return f"✅ {d['label']}: {money(got)}"
    if got == 0:
        return f"❌ {d['label']}: not refunded ({money(claimed)})" + (
            f" — {d['reason']}" if d.get("reason") else ""
        )
    return f"⚠️ {d['label']}: {money(got)} of {money(claimed)}" + (
        f" — {d['reason']}" if d.get("reason") else ""
    )


def employee_decision(claim: Claim) -> str:
    total = claimed_total(claim)
    if claim.status == "approved":
        diff = total - (claim.approved_amount or 0)
        text = (
            f"✅ Claim #{no(claim)} ({claim.title}) approved: {money(claim.approved_amount or 0)} of the {money(total)} you claimed."
            if diff > 0
            else f"✅ Claim #{no(claim)} ({claim.title}) approved: {money(claim.approved_amount or 0)}."
        )
        if claim.decided_items:
            text += "\n\n" + "\n".join(decided_line(d) for d in claim.decided_items)
        elif diff > 0:
            text += f" {money(diff)} of it isn't covered."
        if claim.decision_note:
            text += f"\nNote from your manager: {claim.decision_note}"
        return text
    if claim.status == "rejected":
        return f"❌ Claim #{no(claim)} ({claim.title}) was rejected." + (
            f"\nReason: {claim.decision_note}" if claim.decision_note else ""
        )
    if claim.status == "paid":
        return f"💸 Claim #{no(claim)} ({claim.title}) has been paid: {money(claim.approved_amount or 0)}."
    return f"Claim #{no(claim)} ({claim.title}) is {claim.status.replace('_', ' ')}."


def manager_buttons(claim: Claim, employee_name: str) -> list[list[dict]]:
    return [
        [
            {
                "label": f"✅ Approve {money(claim.recommended)}",
                "data": f"fa:approve:{claim.id}",
            },
            {"label": "❌ Reject", "data": f"fa:reject:{claim.id}"},
        ],
        [
            {"label": "✏️ Different amount", "data": f"fa:amount:{claim.id}"},
            {
                "label": f"💬 Ask {employee_name.split()[0]}",
                "data": f"fa:ask:{claim.id}",
            },
        ],
    ]
