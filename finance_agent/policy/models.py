"""What a reimbursement policy is made of: rules, and the items people claim. Plain data, no logic."""
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

CapUnit = Literal["per_item", "per_night", "per_day", "per_month", "per_claim"]


class Rule(BaseModel):
    """One line of the policy, e.g. 'hotels: up to 6000 a night, receipt needed'."""

    id: str = Field(description="short lowercase slug, e.g. 'hotel'")
    name: str = Field(description="human name, e.g. 'Hotel stays'")
    keywords: list[str] = Field(default_factory=list, description="words that mean this rule, e.g. ['hotel','room','stay','lodging']")
    covered: bool = True
    percent: int = Field(default=100, ge=0, le=100, description="share of the (capped) amount that is reimbursed")
    cap_amount: Decimal | None = Field(default=None, ge=0, description="the limit, in money; None = no limit")
    cap_unit: CapUnit = "per_item"
    receipt_over: Decimal | None = Field(default=None, ge=0, description="a receipt is required when the amount is above this; 0 = always; None = never")
    needs_preapproval: bool = False
    conditions: str = Field(default="", description="rules that only a human can check, e.g. 'only during approved business travel'")

    @field_validator("id", mode="before")
    @classmethod
    def _slug(cls, v):
        return "".join(c if c.isalnum() else "_" for c in str(v).strip().lower()).strip("_") or "other"

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
    date: str | None = None
    purpose: str | None = None


Status = Literal["eligible", "partial", "not_eligible", "needs_info", "needs_manager"]


class Question(BaseModel):
    ref: int
    key: str  # nights | days | has_receipt | preapproved | amount
    text: str


class Verdict(BaseModel):
    """What the policy says about one item."""

    ref: int
    rule_id: str | None = None
    rule_name: str | None = None
    status: Status
    eligible: Decimal | None = None  # None until the questions are answered
    reason: str = ""
    questions: list[Question] = Field(default_factory=list)
