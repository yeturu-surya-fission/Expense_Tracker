"""
Turning a company's policy text into structured rules, and editing those rules from plain sentences.

The model proposes; the manager confirms. Nothing here is saved: callers show `rules_text(...)` / `diff_rules(...)` to the
manager first. Only validated `Rule` objects ever leave this module.
"""
import json

from pydantic import ValidationError

from finance_agent.llm import LLM, LLMError
from finance_agent.policy.engine import describe
from finance_agent.policy.models import Rule

SCHEMA = """Each rule is a JSON object:
{
  "id": "short_lowercase_slug",            // e.g. "hotel", "meals", "internet"
  "name": "Readable name",                 // e.g. "Hotel stays"
  "keywords": ["words", "that", "mean", "this"],   // e.g. ["hotel","room","stay","lodging"]; include common synonyms
  "covered": true,                         // false if the policy says it is NOT reimbursed
  "percent": 100,                          // share reimbursed, 0-100 (a 50% phone-bill rule is 50)
  "cap_amount": 6000,                      // the money limit, or null if none (a number, no currency symbol)
  "cap_unit": "per_item",                  // per_item | per_night | per_day | per_month | per_claim
  "receipt_over": 500,                     // receipt needed above this amount; 0 = always; null = never
  "needs_preapproval": false,              // true if it must be approved BEFORE spending
  "conditions": ""                         // anything only a human can check, e.g. "only during approved business travel"
}"""

SYSTEM_PARSE = f"""You turn a company's expense reimbursement policy into structured rules.
{SCHEMA}
Return {{"rules": [ ...one object per kind of expense... ]}}.
Only write down what the text actually says. If the text gives no limit, use null; do not invent limits or receipt rules.
Amounts are plain numbers (6k = 6000). Keep rules separate (flights and hotels are different rules)."""

SYSTEM_EDIT = f"""You edit a company's expense reimbursement policy. You get the CURRENT rules and an instruction from the manager.
{SCHEMA}
Return {{"rules": [ ...the COMPLETE new list of rules, including the ones that did not change... ]}}.
Change only what the instruction asks for. Keep ids of existing rules. Add a new rule if the instruction adds a new kind of expense.
If the instruction removes something, leave it out of the list."""


class PolicyParseError(ValueError):
    pass


def _validate(raw: dict) -> list[Rule]:
    items = raw.get("rules")
    if not isinstance(items, list) or not items:
        raise PolicyParseError("I couldn't find any rules in that. Please paste the policy text, or describe a rule such as 'hotels: up to 6000 a night, receipt needed'.")
    rules: list[Rule] = []
    seen: set[str] = set()
    for it in items:
        try:
            rule = Rule.model_validate(it)
        except ValidationError as e:
            raise PolicyParseError(f"One of the rules was unclear ({e.errors()[0]['loc'][0] if e.errors() else 'format'}). Please rephrase it.") from e
        base, n = rule.id, 2
        while rule.id in seen:  # two rules must not share an id
            rule.id, n = f"{base}_{n}", n + 1
        seen.add(rule.id)
        rules.append(rule)
    return rules


def _dump(rules: list[Rule]) -> str:
    return json.dumps([r.model_dump(mode="json") for r in rules], ensure_ascii=False)


async def parse_policy(llm: LLM, text: str) -> list[Rule]:
    """The rules in a pasted policy."""
    try:
        return _validate(await llm.json(SYSTEM_PARSE, text[:20000]))
    except LLMError as e:
        raise PolicyParseError(f"I couldn't read that right now ({e}). Please try again.") from e


async def edit_policy(llm: LLM, current: list[Rule], instruction: str) -> list[Rule]:
    """The current rules with one change from the manager ('raise the hotel cap to 8000')."""
    try:
        return _validate(await llm.json(SYSTEM_EDIT, f"CURRENT RULES:\n{_dump(current)}\n\nINSTRUCTION:\n{instruction[:2000]}"))
    except LLMError as e:
        raise PolicyParseError(f"I couldn't apply that right now ({e}). Please try again.") from e


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

