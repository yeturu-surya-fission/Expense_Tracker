"""What a reimbursement policy is made of: rules, and the items people claim. Plain data, no logic."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

CapUnit = Literal["per_item", "per_night", "per_day", "per_month", "per_claim"]

# other ways a limit's period gets written: the closest unit we can check
UNIT_ALIASES = {
    "per_trip": "per_claim",
    "per_journey": "per_claim",
    "per_travel": "per_claim",
    "per_report": "per_claim",
    "per_stay": "per_item",
    "per_expense": "per_item",
    "per_ticket": "per_item",
    "per_meal": "per_item",
    "per_person": "per_item",
    "per_purchase": "per_item",
    "per_receipt": "per_item",
    "per_transaction": "per_item",
    "per_occasion": "per_item",
    "per_visit": "per_item",
    "per_unit": "per_item",
    "per_monthly": "per_month",
    "monthly": "per_month",
    "per_daily": "per_day",
    "daily": "per_day",
    "per_nightly": "per_night",
}


def to_money(value):
    """Models write money as 4000, "4000", "₹4,000", "4k": all become a number (None stays None)."""
    if value is None or isinstance(value, (int, float, Decimal)):
        return value
    text = (
        str(value)
        .strip()
        .lower()
        .replace(",", "")
        .replace("₹", "")
        .replace("rs.", "")
        .replace("rs", "")
        .replace("inr", "")
        .strip()
    )
    if text in ("", "null", "none", "no limit"):
        return None
    mult = 1000 if text.endswith("k") else 1
    return Decimal(text[:-1] if mult == 1000 else text) * mult


class Variant(BaseModel):
    """One version of a rule that has different limits, e.g. hotels in Tier-1 / Tier-2 / other cities."""

    label: str = Field(description="e.g. 'Tier-1 cities'")
    keywords: list[str] = Field(
        default_factory=list,
        description="words that pick this variant, e.g. city names ['mumbai','delhi','bengaluru']",
    )
    cap_amount: Decimal | None = Field(
        default=None, ge=0, description="this variant's limit (same unit as the rule)"
    )

    _money = field_validator("cap_amount", mode="before")(lambda cls, v: to_money(v))

    @field_validator("keywords", mode="before")
    @classmethod
    def _words(cls, v):
        return [str(w).strip().lower() for w in (v or []) if str(w).strip()]


class Rule(BaseModel):
    """One line of the policy, e.g. 'hotels: up to 6000 a night, receipt needed'."""

    id: str = Field(description="short lowercase slug, e.g. 'hotel'")
    name: str = Field(description="human name, e.g. 'Hotel stays'")
    keywords: list[str] = Field(
        default_factory=list,
        description="words that mean this rule, e.g. ['hotel','room','stay','lodging']",
    )
    covered: bool = True
    percent: int = Field(
        default=100,
        ge=0,
        le=100,
        description="share of the (capped) amount that is reimbursed",
    )
    cap_amount: Decimal | None = Field(
        default=None, ge=0, description="the limit, in money; None = no limit"
    )
    cap_unit: CapUnit = "per_item"
    receipt_over: Decimal | None = Field(
        default=None,
        ge=0,
        description="a receipt is required when the amount is above this; 0 = always; None = never",
    )
    needs_preapproval: bool = False
    preapproval_over: Decimal | None = Field(
        default=None,
        ge=0,
        description="approval before spending is needed above this amount",
    )
    variants: list[Variant] = Field(
        default_factory=list,
        description="when the limit depends on something (city tier...): one variant per case",
    )
    conditions: str = Field(
        default="",
        description="rules that only a human can check, e.g. 'only during approved business travel'",
    )

    _money = field_validator(
        "cap_amount", "receipt_over", "preapproval_over", mode="before"
    )(lambda cls, v: to_money(v))

    @model_validator(mode="before")
    @classmethod
    def _unit(cls, data):
        """A model may write a period we can't track ("per_year", "per_person"). Never fail the whole policy over it: use the closest
        unit we have and keep the original wording as a note for the manager, who sees every claim anyway."""
        if not isinstance(data, dict):
            return data
        raw = (
            str(data.get("cap_unit") or "per_item")
            .strip()
            .lower()
            .replace(" ", "_")
            .replace("-", "_")
        )
        raw = raw if raw.startswith("per_") else "per_" + raw
        known = raw in ("per_item", "per_night", "per_day", "per_month", "per_claim")
        unit = raw if known else UNIT_ALIASES.get(raw, "per_item")
        if not known and raw not in UNIT_ALIASES:
            note = f"limit is {raw.replace('_', ' ')}"
            data = {
                **data,
                "conditions": f"{data.get('conditions') or ''}; {note}".strip("; "),
            }
        return {**data, "cap_unit": unit}

    @field_validator("percent", mode="before")
    @classmethod
    def _percent(cls, v):
        return (
            int(Decimal(str(v).replace("%", "").strip()))
            if v not in (None, "")
            else 100
        )

    @field_validator("id", mode="before")
    @classmethod
    def _slug(cls, v):
        return (
            "".join(c if c.isalnum() else "_" for c in str(v).strip().lower()).strip(
                "_"
            )
            or "other"
        )

    @field_validator("keywords", mode="before")
    @classmethod
    def _words(cls, v):
        return [str(w).strip().lower() for w in (v or []) if str(w).strip()]


class Item(BaseModel):
    """One thing someone wants to be reimbursed for (or asks about)."""

    ref: int = 0
    category: str = ""
    description: str = ""
    amount: Decimal | None = None
    nights: int | None = None
    days: int | None = None
    has_receipt: bool | None = None
    preapproved: bool | None = None
    variant: str | None = None  # which case of a rule applies (e.g. the city tier)
    date: str | None = None
    purpose: str | None = None


Status = Literal["eligible", "partial", "not_eligible", "needs_info", "needs_manager"]


class Question(BaseModel):
    ref: int
    key: str  # nights | days | has_receipt | preapproved | amount | variant
    text: str


class Verdict(BaseModel):
    """What the policy says about one item."""

    ref: int
    rule_id: str | None = None
    rule_name: str | None = None
    status: Status
    eligible: Decimal | None = None  # None until the questions are answered
    reason: str = ""
    check: str = ""  # a condition of the rule that only a person can verify ("only during business travel")
    questions: list[Question] = Field(default_factory=list)
