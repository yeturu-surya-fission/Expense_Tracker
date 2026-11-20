"""work expenses: policy questions asked up front, and receipt files

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "work_expenses", sa.Column("preapproved", sa.Boolean(), nullable=True)
    )
    op.add_column(
        "work_expenses", sa.Column("variant", sa.String(length=60), nullable=True)
    )
    op.add_column(
        "work_expenses",
        sa.Column(
            "needs_receipt", sa.Boolean(), server_default="false", nullable=False
        ),
    )
    op.add_column(
        "work_expenses",
        sa.Column(
            "open_questions", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    op.create_table(
        "work_receipts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("expense_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=120), nullable=False),
        sa.Column("content_type", sa.String(length=60), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["expense_id"], ["work_expenses.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_work_receipts_expense_id"),
        "work_receipts",
        ["expense_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_work_receipts_user_id"), "work_receipts", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_table("work_receipts")
    op.drop_column("work_expenses", "open_questions")
    op.drop_column("work_expenses", "needs_receipt")
    op.drop_column("work_expenses", "variant")
    op.drop_column("work_expenses", "preapproved")
