"""make task parent optional

Revision ID: f2c4b6d8e0a1
Revises: d1a2b3c4e5f6
Create Date: 2026-05-25 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "f2c4b6d8e0a1"
down_revision = "d1a2b3c4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("ticket_tasks", schema=None) as batch_op:
        batch_op.alter_column(
            "ticket_id",
            existing_type=sa.Integer(),
            nullable=True,
        )


def downgrade():
    with op.batch_alter_table("ticket_tasks", schema=None) as batch_op:
        batch_op.alter_column(
            "ticket_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
