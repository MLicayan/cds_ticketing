"""allow app service logs without instrument

Revision ID: a9c1d2e3f4b5
Revises: f1b2c3d4e5f6
Create Date: 2026-05-09 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "a9c1d2e3f4b5"
down_revision = "f1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("service_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "instrument_id",
            existing_type=mysql.INTEGER(display_width=11),
            nullable=True,
        )


def downgrade():
    with op.batch_alter_table("service_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "instrument_id",
            existing_type=mysql.INTEGER(display_width=11),
            nullable=False,
        )
