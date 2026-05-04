"""add current_cash to sleeves

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-04 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sleeves",
        sa.Column("current_cash", sa.Numeric(20, 10), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("sleeves", "current_cash")
