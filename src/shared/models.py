"""Shared Pydantic models used by both backend and MCP server.

These models define the input schemas for MCP tools and are the single source
of truth so neither backend nor mcp depends on the other for type definitions.
"""

from pydantic import BaseModel, Field


class ExpenseItem(BaseModel):
    amount: float = Field(gt=0, description="Amount spent, as a plain number (3k = 3000).")
    category: str = Field(
        description="One lowercase word, e.g. food, transport, clothes, bills, shopping, fuel. "
        "Prefer the user's budget categories."
    )
    description: str = Field(description="What it was for, e.g. 'ice cream'.")
    date: str | None = Field(
        default=None,
        description="YYYY-MM-DD, only if the user refers to a past day (e.g. 'yesterday').",
    )


class BillItem(BaseModel):
    amount: float = Field(gt=0, description="What this bill came to, as a plain number.")
    description: str = Field(
        description="What it was for, e.g. 'party food', 'ice cream', 'bar'."
    )
    category: str = Field(
        default="other",
        description="One lowercase word: food, drinks, transport, stay, entertainment...",
    )
    paid_by: str = Field(
        default="me",
        description="Who paid THIS bill: a friend's / member's name exactly as said, or 'me'.",
    )


class PersonAmount(BaseModel):
    name: str = Field(
        description="The friend's name as the user said it, or 'me'."
    )
    amount: float = Field(
        gt=0, description="That person's share in rupees, as a plain number."
    )
