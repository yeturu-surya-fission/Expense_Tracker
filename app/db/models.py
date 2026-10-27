from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

MONEY = Numeric(14, 2)


class Base(DeclarativeBase):
    pass


# ====================================================================== people
class User(Base):
    """Anyone the bot knows. `started_at` is set once they press Start in a private chat (needed to message them)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(128))
    username: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Friend(Base):
    """One side of a friendship: `owner` calls `friend` by `nickname`. Only the owner sees the nickname."""

    __tablename__ = "friends"
    __table_args__ = (UniqueConstraint("owner_id", "friend_id", name="uq_friend_pair"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    friend_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    nickname: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ====================================================================== personal expenses
class Expense(Base):
    """One thing a person spent. Rows are never deleted; undo sets `voided_at`."""

    __tablename__ = "expenses"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_expense_amount_positive"),
        Index("ix_expense_user_time", "user_id", "occurred_at"),
        Index("ix_expense_user_category", "user_id", "category"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    category: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(String(256))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(12), default="personal")  # personal | split | statement
    external_key: Mapped[str | None] = mapped_column(String(40), index=True)  # statement dedupe
    import_id: Mapped[int | None] = mapped_column(ForeignKey("statement_imports.id", ondelete="SET NULL"), index=True)


class StatementImport(Base):
    """A parsed bank-statement PDF waiting for the user's confirmation (then 'done', or 'undone')."""

    __tablename__ = "statement_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending|done|cancelled|undone
    items: Mapped[list] = mapped_column(JSONB)
    meta: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ====================================================================== budgets
class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("user_id", "category", name="uq_budget_user_category"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(String(64))
    monthly_limit: Mapped[Decimal] = mapped_column(MONEY)


class BudgetAlert(Base):
    """Remembers which thresholds were already announced this month so alerts fire once."""

    __tablename__ = "budget_alerts"
    __table_args__ = (UniqueConstraint("budget_id", "period", "threshold", name="uq_alert_once"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    budget_id: Mapped[int] = mapped_column(ForeignKey("budgets.id", ondelete="CASCADE"))
    period: Mapped[str] = mapped_column(String(7))  # "2026-10"
    threshold: Mapped[int]
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ====================================================================== groups (a Telegram group chat = one expense group)
class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)  # the Telegram group chat
    name: Mapped[str] = mapped_column(String(128))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    default_split: Mapped[str | None] = mapped_column(String(8))  # 'equal' = stop asking
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_group_member"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # set when they leave the Telegram group


# ====================================================================== shared expenses and who owes whom
class SharedExpense(Base):
    """Something one person paid for that is split between people (a quick split, or an expense inside a group)."""

    __tablename__ = "shared_expenses"
    __table_args__ = (CheckConstraint("amount > 0", name="ck_shared_amount_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)  # None = quick split
    payer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    description: Mapped[str] = mapped_column(String(256))
    category: Mapped[str] = mapped_column(String(64))
    split_mode: Mapped[str] = mapped_column(String(8))  # equal | exact
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # group expenses: set by settle-up
    bundle_id: Mapped[str | None] = mapped_column(String(32), index=True)  # bills split together and netted into few payments


class Share(Base):
    """One person's part of a shared expense. `expense_id` is set once it was recorded in their own tracker."""

    __tablename__ = "shares"
    __table_args__ = (UniqueConstraint("shared_expense_id", "user_id", name="uq_share_once"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shared_expense_id: Mapped[int] = mapped_column(ForeignKey("shared_expenses.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    expense_id: Mapped[int | None] = mapped_column(ForeignKey("expenses.id", ondelete="SET NULL"))


class Debt(Base):
    """`debtor` owes `creditor` money. pending -> claimed (debtor says paid) -> settled (creditor confirms)."""

    __tablename__ = "debts"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_debt_amount_positive"),
        Index("ix_debt_debtor_status", "debtor_id", "status"),
        Index("ix_debt_creditor_status", "creditor_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    debtor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    creditor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    description: Mapped[str] = mapped_column(String(256))
    kind: Mapped[str] = mapped_column(String(8))  # quick | group | bundle
    bundle_id: Mapped[str | None] = mapped_column(String(32), index=True)
    shared_expense_id: Mapped[int | None] = mapped_column(ForeignKey("shared_expenses.id", ondelete="CASCADE"))
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    detail: Mapped[str | None] = mapped_column(String(500))  # the bills behind the amount, shown in every message about it
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending|claimed|settled|cancelled
    expense_ids: Mapped[list] = mapped_column(JSONB, default=list)  # debtor's expenses logged when they claimed
    debtor_msg_id: Mapped[int | None] = mapped_column(BigInteger)  # last notification, so it can disappear
    creditor_msg_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
