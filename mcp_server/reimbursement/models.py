"""
Work reimbursements, kept apart from the person's own expenses. Nothing here is counted in summaries or budgets: it is
money the company will pay back. (The tables share the server's database; the company's policy and decisions live in the
Finance Agent's own database, which we only reach through A2A.)
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from mcp_server.db.models import MONEY, Base


class CompanyLink(Base):
    """The company this person joined (with the manager's code)."""

    __tablename__ = "company_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    company_code: Mapped[str] = mapped_column(String(32))
    company_name: Mapped[str] = mapped_column(String(120))
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WorkExpense(Base):
    """Something the person paid for work. `claim_id` is set once it has been sent in a claim."""

    __tablename__ = "work_expenses"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_work_expense_amount_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(MONEY)
    category: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(String(200))
    spent_on: Mapped[date] = mapped_column(Date)
    nights: Mapped[int | None] = mapped_column(Integer)
    days: Mapped[int | None] = mapped_column(Integer)
    has_receipt: Mapped[bool | None] = mapped_column(Boolean)
    purpose: Mapped[str | None] = mapped_column(String(200))
    preapproved: Mapped[bool | None] = mapped_column(Boolean)
    variant: Mapped[str | None] = mapped_column(String(60))
    needs_receipt: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )  # the policy wants a receipt FILE for this one
    open_questions: Mapped[list | None] = mapped_column(
        JSONB
    )  # what the finance agent still needs to know: [{key, text}]
    claim_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_claims.id", ondelete="SET NULL"), index=True
    )
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WorkReceipt(Base):
    """The receipt (photo or PDF) for one work expense. Travels with the claim so the manager can check it."""

    __tablename__ = "work_receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    expense_id: Mapped[int] = mapped_column(
        ForeignKey("work_expenses.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(120))
    content_type: Mapped[str] = mapped_column(String(60))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WorkClaim(Base):
    """A claim sent to the Finance Agent. `status` mirrors what the Finance Agent reports (see its claim lifecycle)."""

    __tablename__ = "work_claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(20), default="sending")
    remote_claim_id: Mapped[int | None] = mapped_column(Integer, index=True)
    total: Mapped[Decimal] = mapped_column(MONEY)
    approved_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    note: Mapped[str | None] = mapped_column(String(500))
    decided_items: Mapped[list | None] = mapped_column(
        JSONB
    )  # the manager's decision per expense: [{ref, claimed, approved, reason}]
    questions: Mapped[list | None] = mapped_column(
        JSONB
    )  # what the Finance Agent still needs to know (status needs_info)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PolicyChat(Base):
    """A policy conversation the Finance Agent is waiting on (an A2A task in 'input-required'): the next question continues it."""

    __tablename__ = "policy_chats"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    task_id: Mapped[str] = mapped_column(String(64))
    context_id: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
