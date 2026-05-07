"""add strategy_state table

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-07 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "strategy_state",
        sa.Column("sleeve_id", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("entry_price", sa.Numeric(20, 10), nullable=False),
        sa.Column("days_held", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("highest_close_since_entry", sa.Numeric(20, 10), nullable=False),
        sa.Column("last_session_date", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(["sleeve_id"], ["sleeves.id"]),
        sa.PrimaryKeyConstraint("sleeve_id", "symbol"),
    )


def downgrade() -> None:
    op.drop_table("strategy_state")
