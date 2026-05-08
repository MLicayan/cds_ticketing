"""Add LIS fields to instruments and LIS logs table

Revision ID: 1b8dc29b2c3c
Revises: 04d75dd6a11a
Create Date: 2025-02-07
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "1b8dc29b2c3c"
down_revision = "04d75dd6a11a"
branch_labels = None
depends_on = None


def upgrade():
    # Add LIS metadata columns to instruments
    with op.batch_alter_table("instruments") as batch_op:
        batch_op.add_column(sa.Column("lis_status", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("lis_protocol", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("lis_last_active_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("lis_last_sent_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("lis_last_received_at", sa.DateTime(), nullable=True))

    # Create LIS logs table
    op.create_table(
        "lis_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instruments.id"), nullable=False),
        sa.Column("direction", sa.String(length=10)),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table("lis_logs")
    with op.batch_alter_table("instruments") as batch_op:
        batch_op.drop_column("lis_last_received_at")
        batch_op.drop_column("lis_last_sent_at")
        batch_op.drop_column("lis_last_active_at")
        batch_op.drop_column("lis_protocol")
        batch_op.drop_column("lis_status")
