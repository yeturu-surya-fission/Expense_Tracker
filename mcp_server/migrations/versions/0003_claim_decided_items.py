"""per-expense decisions on work claims

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-17 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('work_claims', sa.Column('decided_items', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('work_claims', 'decided_items')
