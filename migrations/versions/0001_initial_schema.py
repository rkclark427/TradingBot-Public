"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-03 00:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sleeves",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("strategy_name", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("starting_capital", sa.Numeric(20, 10), nullable=False),
        sa.Column("current_nav", sa.Numeric(20, 10), nullable=False),
        sa.Column("high_water_mark", sa.Numeric(20, 10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parameters_json", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "sleeve_capital_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sleeve_id", sa.String(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("amount", sa.Numeric(20, 10), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["sleeve_id"], ["sleeves.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sleeve_id", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("target_weight", sa.Numeric(20, 10), nullable=False),
        sa.Column("rationale_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["sleeve_id"], ["sleeves.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "intended_orders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sleeve_id", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("qty", sa.Numeric(20, 10), nullable=False),
        sa.Column("limit_price", sa.Numeric(20, 10), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["sleeve_id"], ["sleeves.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "net_orders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("net_qty", sa.Numeric(20, 10), nullable=False),
        sa.Column("limit_price", sa.Numeric(20, 10), nullable=True),
        sa.Column("alpaca_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "intended_to_net",
        sa.Column("intended_order_id", sa.Integer(), nullable=False),
        sa.Column("net_order_id", sa.Integer(), nullable=False),
        sa.Column("allocated_qty", sa.Numeric(20, 10), nullable=False),
        sa.ForeignKeyConstraint(["intended_order_id"], ["intended_orders.id"]),
        sa.ForeignKeyConstraint(["net_order_id"], ["net_orders.id"]),
        sa.PrimaryKeyConstraint("intended_order_id", "net_order_id"),
    )

    op.create_table(
        "fills",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("net_order_id", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("qty", sa.Numeric(20, 10), nullable=False),
        sa.Column("fill_price", sa.Numeric(20, 10), nullable=False),
        sa.Column("fees", sa.Numeric(20, 10), nullable=False),
        sa.ForeignKeyConstraint(["net_order_id"], ["net_orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "sleeve_fills",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("fill_id", sa.Integer(), nullable=False),
        sa.Column("sleeve_id", sa.String(), nullable=False),
        sa.Column("allocated_qty", sa.Numeric(20, 10), nullable=False),
        sa.Column("allocated_price", sa.Numeric(20, 10), nullable=False),
        sa.ForeignKeyConstraint(["fill_id"], ["fills.id"]),
        sa.ForeignKeyConstraint(["sleeve_id"], ["sleeves.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "positions",
        sa.Column("sleeve_id", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("qty", sa.Numeric(20, 10), nullable=False),
        sa.Column("avg_cost", sa.Numeric(20, 10), nullable=False),
        sa.ForeignKeyConstraint(["sleeve_id"], ["sleeves.id"]),
        sa.PrimaryKeyConstraint("sleeve_id", "symbol"),
    )

    op.create_table(
        "sleeve_nav_snapshots",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("sleeve_id", sa.String(), nullable=False),
        sa.Column("nav", sa.Numeric(20, 10), nullable=False),
        sa.Column("cash", sa.Numeric(20, 10), nullable=False),
        sa.Column("position_value", sa.Numeric(20, 10), nullable=False),
        sa.Column("realized_pnl_today", sa.Numeric(20, 10), nullable=False),
        sa.ForeignKeyConstraint(["sleeve_id"], ["sleeves.id"]),
        sa.PrimaryKeyConstraint("date", "sleeve_id"),
    )

    op.create_table(
        "account_snapshots",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("total_equity", sa.Numeric(20, 10), nullable=False),
        sa.Column("total_cash", sa.Numeric(20, 10), nullable=False),
        sa.Column("unallocated_cash", sa.Numeric(20, 10), nullable=False),
        sa.PrimaryKeyConstraint("date", "mode"),
    )

    op.create_table(
        "risk_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(), nullable=False),
        sa.Column("sleeve_id_nullable", sa.String(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["sleeve_id_nullable"], ["sleeves.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "system_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context_json", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "asset_universe",
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("tradable", sa.Boolean(), nullable=False),
        sa.Column("fractionable", sa.Boolean(), nullable=False),
        sa.Column("marginable", sa.Boolean(), nullable=False),
        sa.Column("shortable", sa.Boolean(), nullable=False),
        sa.Column("etb", sa.Boolean(), nullable=False),
        sa.Column("last_refreshed", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("symbol"),
    )

    op.create_table(
        "account_capabilities",
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("capability_key", sa.String(), nullable=False),
        sa.Column("capability_value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_date", "capability_key"),
    )

    op.create_table(
        "heartbeat",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("heartbeat")
    op.drop_table("account_capabilities")
    op.drop_table("asset_universe")
    op.drop_table("system_events")
    op.drop_table("risk_events")
    op.drop_table("account_snapshots")
    op.drop_table("sleeve_nav_snapshots")
    op.drop_table("positions")
    op.drop_table("sleeve_fills")
    op.drop_table("fills")
    op.drop_table("intended_to_net")
    op.drop_table("net_orders")
    op.drop_table("intended_orders")
    op.drop_table("signals")
    op.drop_table("sleeve_capital_events")
    op.drop_table("sleeves")
