"""
Turning a company's policy text into structured rules, and editing those rules from plain sentences.

The model proposes; the manager confirms. Nothing here is saved: callers show `rules_text(...)` / `diff_rules(...)` to the
manager first. Only validated `Rule` objects ever leave this module.
"""
import json
import logging

from pydantic import ValidationError

from finance_agent.config import settings
from finance_agent.llm import LLM, LLMError
from finance_agent.policy.engine import describe
from finance_agent.policy.models import Rule

log = logging.getLogger(__name__)

SCHEMA = """Each rule is a JSON object. Leave out any field that is just its default:
{
  "id": "short_lowercase_slug",       // e.g. "hotel", "lunch", "internet"
  "name": "Readable name",            // e.g. "Hotel stays"
  "keywords": ["words", "that", "mean", "this"],   // include common synonyms: ["hotel","room","stay","lodging"]
  "covered": true,                    // false if the policy says it is NOT reimbursed
  "percent": 100,                     // share reimbursed, 0-100 (a 50% phone-bill rule is 50)
  "cap_amount": 6000,                 // the money limit, a plain number, or null if none
  "cap_unit": "per_item",             // ONLY one of: per_item | per_night | per_day | per_month | per_claim (nothing else, even for "per year")
  "receipt_over": 500,                // receipt needed above this amount; 0 = always; null = never
  "needs_preapproval": false,         // true if it must ALWAYS be approved before spending
  "preapproval_over": 5000,           // approval before spending is needed only ABOVE this amount; null = n/a
  "variants": [                       // ONLY when the limit depends on something, e.g. the city tier:
    {"label": "Tier-1 cities", "keywords": ["mumbai", "delhi", "bengaluru"], "cap_amount": 4000},
    {"label": "Other locations", "keywords": [], "cap_amount": 2000}
  ],
  "conditions": ""                    // anything only a human can check, e.g. "only during approved business travel"
}
Choosing cap_unit:
- per_night / per_day: the limit is for each night / each day (hotels, meals).
- per_claim: ONE limit shared by everything of this kind in a whole trip or claim ("₹5,000 per trip for all ground transport combined").
- per_month: ONE limit shared by everything of this kind in a calendar month ("internet and mobile up to ₹1,000 a month").
- per_item: the limit is for each single expense (also use it for "per meal", "per ticket", "per person").
- A limit for a period we cannot track (per year, per week): use per_item and write the period in "conditions", e.g. "limit is ₹25,000 per year"."""

EXAMPLE = """Example. Policy text: "Taxis, trains and buses: ₹5,000 per trip in total. Lunch: max ₹350 a day. Hotels: Tier-1 cities ₹4,000 a night, others ₹2,000.
Equipment above ₹5,000 needs prior approval. Alcohol is never reimbursed." becomes:
{"rules": [
 {"id": "ground_transport", "name": "Ground transport", "keywords": ["taxi", "cab", "train", "bus", "auto", "metro"], "cap_amount": 5000, "cap_unit": "per_claim"},
 {"id": "lunch", "name": "Lunch", "keywords": ["lunch"], "cap_amount": 350, "cap_unit": "per_day", "conditions": "only during business travel"},
 {"id": "hotel", "name": "Hotel stays", "keywords": ["hotel", "room", "stay", "lodging"], "cap_unit": "per_night",
  "variants": [{"label": "Tier-1 cities", "keywords": ["mumbai", "delhi", "bengaluru", "chennai", "hyderabad", "kolkata", "pune"], "cap_amount": 4000},
               {"label": "Other locations", "keywords": [], "cap_amount": 2000}]},
 {"id": "equipment", "name": "Office equipment", "keywords": ["laptop", "keyboard", "monitor", "supplies"], "preapproval_over": 5000},
 {"id": "alcohol", "name": "Alcohol and tobacco", "keywords": ["alcohol", "beer", "wine", "liquor", "tobacco", "cigarette"], "covered": false}
]}"""

SYSTEM_PARSE = f"""You turn a company's expense reimbursement policy into structured rules.
{SCHEMA}
{EXAMPLE}
Return {{"rules": [ ...one object per kind of expense... ]}}.
Write a rule for EVERY kind of expense the policy names, covered or not (a "not reimbursed" list becomes rules with covered false).
Only write down what the text actually says; if it gives no limit use null. Do not invent limits. Amounts are plain numbers (6k = 6000).
Ignore parts that are not about whether or how much an expense is paid (purpose, deadlines, who approves, timelines, fraud, review dates)."""

SYSTEM_EDIT = f"""You edit a company's expense reimbursement policy. You get the CURRENT rules and an instruction from the manager.
{SCHEMA}
Return {{"rules": [ ...the COMPLETE new list of rules, including the ones that did not change... ]}}.
Change only what the instruction asks for. Keep the ids of existing rules. Add a new rule if the instruction adds a new kind of expense.
If the instruction removes something, leave it out of the list."""


class PolicyParseError(ValueError):
    """`retryable`: the same text may well work on a second try (the model was slow or unreachable), so it is worth offering a retry."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _find_rules(raw) -> list | None:
    """The list of rule objects in whatever shape the model answered with: {"rules": [...]}, a bare list, or nested one level."""
    if isinstance(raw, list):
        return raw if raw and all(isinstance(x, dict) for x in raw) else None
    if isinstance(raw, dict):
        if isinstance(raw.get("rules"), list):
            return raw["rules"]
        for value in raw.values():
            found = _find_rules(value) if isinstance(value, (list, dict)) else None
            if found and any(("id" in x or "name" in x) for x in found):
                return found
    return None


def _validate(raw) -> list[Rule]:
    items = _find_rules(raw)
    if not items:
        log.warning("no rules in the model's answer: %.300r", raw)
        raise PolicyParseError(
            "I couldn't turn that into rules. Try again (the text may need to be cleaner), or describe the limits in a few short lines, "
            "for example: “hotels up to 6000 a night, receipt needed; meals up to 1000 a day; gym is not reimbursed”.", retryable=True,
        )
    rules: list[Rule] = []
    seen: set[str] = set()
    for it in items:
        try:
            rule = Rule.model_validate(it)
        except (ValidationError, ArithmeticError, ValueError) as e:
            first = e.errors()[0]["loc"] if isinstance(e, ValidationError) and e.errors() else ()
            what = first[0] if first else "format"
            log.warning("a rule was unreadable (%s): %.200r", what, it)
            raise PolicyParseError(f"One of the rules was unclear ({what}). Please try again or rephrase that part.", retryable=True) from e
        base, n = rule.id, 2
        while rule.id in seen:  # two rules must not share an id
            rule.id, n = f"{base}_{n}", n + 1
        seen.add(rule.id)
        rules.append(rule)
    return rules


def _dump(rules: list[Rule]) -> str:
    return json.dumps([r.model_dump(mode="json", exclude_defaults=True) for r in rules], ensure_ascii=False)


async def parse_policy(llm: LLM, text: str) -> list[Rule]:
    """The rules in a pasted policy."""
    try:
        return _validate(await llm.json(SYSTEM_PARSE, text[:30000], timeout=settings.policy_timeout))
    except LLMError as e:
        raise PolicyParseError(f"I couldn't read that just now ({e}). Your text is saved: tap Try again.", retryable=True) from e


async def edit_policy(llm: LLM, current: list[Rule], instruction: str) -> list[Rule]:
    """The current rules with one change from the manager ('raise the hotel cap to 8000')."""
    try:
        return _validate(await llm.json(SYSTEM_EDIT, f"CURRENT RULES:\n{_dump(current)}\n\nINSTRUCTION:\n{instruction[:2000]}", timeout=settings.policy_timeout))
    except LLMError as e:
        raise PolicyParseError(f"I couldn't apply that just now ({e}). Your instruction is saved: tap Try again.", retryable=True) from e


def rules_text(rules: list[Rule]) -> str:
    return "\n".join(f"• {describe(r)}" for r in rules)


def _same(a: Rule, b: Rule) -> bool:
    return a.model_dump(mode="json") == b.model_dump(mode="json")


def diff_rules(old: list[Rule], new: list[Rule]) -> list[str]:
    """What changed, one line each (for the manager to confirm)."""
    before, after = {r.id: r for r in old}, {r.id: r for r in new}
    lines = [f"➕ New: {describe(r)}" for i, r in after.items() if i not in before]
    lines += [f"➖ Removed: {r.name}" for i, r in before.items() if i not in after]
    lines += [f"✏️ {describe(before[i])}\n    → {describe(r)}" for i, r in after.items() if i in before and not _same(before[i], r)]
    return lines
