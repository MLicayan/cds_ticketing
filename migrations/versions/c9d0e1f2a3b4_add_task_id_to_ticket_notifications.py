"""add task id to ticket notifications

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-06-18 10:28:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "ticket_notifications" not in tables:
        return

    columns = {column["name"] for column in inspector.get_columns("ticket_notifications")}

    with op.batch_alter_table("ticket_notifications", schema=None) as batch_op:
        if "comment_preview" not in columns:
            batch_op.add_column(sa.Column("comment_preview", sa.Text(), nullable=True))

        if "task_id" not in columns:
            batch_op.add_column(sa.Column("task_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                "fk_ticket_notifications_task",
                "ticket_tasks",
                ["task_id"],
                ["id"],
            )
            batch_op.create_index("ix_ticket_notifications_task_id", ["task_id"], unique=False)

        if "ticket_id" in columns:
            batch_op.alter_column(
                "ticket_id",
                existing_type=sa.Integer(),
                nullable=True,
            )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "ticket_notifications" not in tables:
        return

    columns = {column["name"] for column in inspector.get_columns("ticket_notifications")}

    with op.batch_alter_table("ticket_notifications", schema=None) as batch_op:
        if "comment_preview" in columns:
            batch_op.drop_column("comment_preview")

        if "task_id" in columns:
            batch_op.drop_index("ix_ticket_notifications_task_id")
            batch_op.drop_constraint("fk_ticket_notifications_task", type_="foreignkey")
            batch_op.drop_column("task_id")

        if "ticket_id" in columns:
            batch_op.alter_column(
                "ticket_id",
                existing_type=sa.Integer(),
                nullable=False,
            )
