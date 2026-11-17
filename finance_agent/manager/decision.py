"""
"Different amount": the manager decides expense by expense, so the employee can be told exactly which ones were cut.
These are only the screens (text + buttons); the waiting state and the saving are in service.py.
"""
from decimal import Decimal

from finance_agent.claims import service as claims
from finance_agent.claims import texts
from finance_agent.formatting import money, q2


def item_screen(claim, idx: int, amounts: dict[str, str]) -> tuple[str, list[list[dict]]]:
    """Question for item number `idx` (0-based): pay the policy's amount, the full claimed amount, nothing, or a typed amount."""
    items = texts.items_of(claim)
    item, policy = items[idx], claims.policy_amounts(claim)
    claimed, suggested = q2(item.amount or Decimal(0)), policy.get(items[idx].ref, Decimal(0))
    verdict = next((v for v in texts.verdicts_of(claim) if v.ref == item.ref), None)
    head = f"✏️ Claim #{claim.id}: expense {idx + 1} of {len(items)}\n{texts.label(item)}: claimed {money(claimed)}"
    head += f" · policy says {money(suggested)}" + (f" ({verdict.reason})" if verdict and verdict.status not in ("eligible",) else "")
    row = [{"label": f"✅ {money(suggested)} (policy)", "data": f"fi:{claim.id}:p"}]
    if claimed != suggested:
        row.append({"label": f"💯 Full {money(claimed)}", "data": f"fi:{claim.id}:f"})
    if suggested != 0:
        row.append({"label": "❌ Nothing", "data": f"fi:{claim.id}:z"})
    return head + "\n\nHow much do you approve for this one? Tap a button, or type an amount.", [row]


def summary_screen(claim, amounts: dict[str, str]) -> tuple[str, list[list[dict]]]:
    """Everything the manager chose, before it is sent."""
    out, total = [], Decimal(0)
    for item in texts.items_of(claim):
        got = Decimal(amounts[str(item.ref)])
        total += got
        claimed = q2(item.amount or Decimal(0))
        mark = "✅" if got >= claimed else ("❌" if got == 0 else "⚠️")
        out.append(f"{mark} {texts.label(item)}: {money(claimed)} → {money(got)}")
    text = f"Claim #{claim.id}: your decision\n\n" + "\n".join(out) + f"\n\nTotal {money(total)} of {money(texts.claimed_total(claim))} claimed. Send it to the employee?"
    return text, [[{"label": f"✅ Send ({money(total)})", "data": f"fi:{claim.id}:ok"}, {"label": "↩️ Start over", "data": f"fi:{claim.id}:redo"}]]
