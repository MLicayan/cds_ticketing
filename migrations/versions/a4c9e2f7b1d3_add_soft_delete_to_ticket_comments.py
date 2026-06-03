"""add soft delete to ticket comments

Revision ID: a4c9e2f7b1d3
Revises: f2c4b6d8e0a1
Create Date: 2026-06-03 15:15:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "a4c9e2f7b1d3"
down_revision = "f2c4b6d8e0a1"
branch_labels = None
depends_on = None


def _add_columns_if_missing(table_name):
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns(table_name)}

    with op.batch_alter_table(table_name, schema=None) as batch_op:
        if "deleted" not in columns:
            batch_op.add_column(sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()))
        if "deleted_at" not in columns:
            batch_op.add_column(sa.Column("deleted_at", sa.DateTime(), nullable=True))


def _drop_columns_if_present(table_name):
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns(table_name)}

    with op.batch_alter_table(table_name, schema=None) as batch_op:
        if "deleted_at" in columns:
            batch_op.drop_column("deleted_at")
        if "deleted" in columns:
            batch_op.drop_column("deleted")


def upgrade():
    _add_columns_if_missing("ticket_comments")
    _add_columns_if_missing("ticket_task_comments")


def downgrade():
    _drop_columns_if_present("ticket_task_comments")
    _drop_columns_if_present("ticket_comments")
