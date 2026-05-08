"""Update service_log_parts to store part details

Revision ID: b1c4c8f2d7a8
Revises: 9f8b7c6d5e4a
Create Date: 2025-02-08
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b1c4c8f2d7a8"
down_revision = "9f8b7c6d5e4a"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("service_log_parts")
    op.create_table(
        "service_log_parts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("service_log_id", sa.Integer(), sa.ForeignKey("service_logs.id"), nullable=False),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("parts.id"), nullable=True),
        sa.Column("part_no", sa.String(length=255), nullable=True),
        sa.Column("qty", sa.Numeric(12, 2), nullable=True),
        sa.Column("price", sa.Numeric(12, 2), nullable=True),
        sa.Column("total", sa.Numeric(12, 2), nullable=True),
        sa.Column("under_warranty", sa.Boolean(), nullable=True, server_default=sa.text("0")),
    )


def downgrade():
    op.drop_table("service_log_parts")
    op.create_table(
        "service_log_parts",
        sa.Column("service_log_id", sa.Integer(), sa.ForeignKey("service_logs.id"), primary_key=True),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("parts.id"), primary_key=True),
    )
