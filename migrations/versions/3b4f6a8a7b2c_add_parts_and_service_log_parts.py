"""Add parts table and service log parts association

Revision ID: 3b4f6a8a7b2c
Revises: a57003f6f4d7
Create Date: 2025-02-07
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "3b4f6a8a7b2c"
down_revision = "a57003f6f4d7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "parts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
    )
    op.create_table(
        "service_log_parts",
        sa.Column("service_log_id", sa.Integer(), sa.ForeignKey("service_logs.id"), primary_key=True),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("parts.id"), primary_key=True),
    )


def downgrade():
    op.drop_table("service_log_parts")
    op.drop_table("parts")
