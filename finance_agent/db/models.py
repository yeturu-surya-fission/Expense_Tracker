from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

MONEY = Numeric(14, 2)


class Base(DeclarativeBase):
    pass


class Company(Base):
    """One company = one manager + one policy. Employees join with `code`."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    manager_telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PolicyVersion(Base):
    """Every saved policy is a new row, so an old claim can be explained by the rules that applied to it."""

    __tablename__ = "policy_versions"
    __table_args__ = (UniqueConstraint("company_id", "version", name="uq_policy_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    rules: Mapped[list] = mapped_column(JSONB)
    note: Mapped[str | None] = mapped_column(String(300))  # what changed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Employee(Base):
    """A person from the expense tracker who joined a company. `ref` is their id there (we never see their other data)."""

    __tablename__ = "employees"
    __table_args__ = (UniqueConstraint("company_id", "ref", name="uq_employee"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    ref: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Claim(Base):
    """needs_info -> awaiting_manager -> approved | rejected   (info_requested: the manager asked the employee something)"""

    __tablename__ = "claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(20), default="needs_info", index=True)
    items: Mapped[list] = mapped_column(JSONB)  # what the employee claimed
    verdicts: Mapped[list] = mapped_column(JSONB)  # what the policy says about each item
    recommended: Mapped[Decimal] = mapped_column(MONEY, default=0)
    approved_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    decision_note: Mapped[str | None] = mapped_column(String(500))
    decided_items: Mapped[list | None] = mapped_column(JSONB)  # per expense: [{ref, claimed, approved, reason}] so the employee sees which ones were cut
    policy_version: Mapped[int] = mapped_column(Integer)
    external_no: Mapped[int | None] = mapped_column(Integer)  # the number the employee knows this claim by (their tracker's claim id)
    thread: Mapped[list] = mapped_column(JSONB, default=list)  # questions and answers between manager and employee
    paid: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PolicyQuestion(Base):
    """An employee asked something the policy doesn't answer, and wanted the manager's word."""

    __tablename__ = "policy_questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(String(1000))
    answer: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QaSession(Base):
    """A policy conversation that is waiting for the employee's reply (an A2A task in 'input-required')."""

    __tablename__ = "qa_sessions"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    transcript: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ManagerState(Base):
    """What the manager's chat is waiting for next ('type the reason', 'type your answer')."""

    __tablename__ = "manager_state"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mode: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Notification(Base):
    """A message waiting to be sent to the manager (written in the same transaction as the data; a loop delivers it)."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    buttons: Mapped[list | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WebhookEvent(Base):
    """A decision or message for the expense tracker, delivered by a signed webhook (retried until it is accepted)."""

    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending | sent | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
