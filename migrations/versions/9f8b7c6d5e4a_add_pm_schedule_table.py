"""Add preventive maintenance schedule table

Revision ID: 9f8b7c6d5e4a
Revises: 7d2a6e8a3c4a
Create Date: 2025-02-08
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "9f8b7c6d5e4a"
down_revision = "7d2a6e8a3c4a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "preventive_maintenance_schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("doc_no", sa.String(length=32), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instruments.id"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("task_duration", sa.String(length=50), nullable=True),
        sa.Column("assigned_engineer_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("assigned_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("tickets.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table("preventive_maintenance_schedules")
