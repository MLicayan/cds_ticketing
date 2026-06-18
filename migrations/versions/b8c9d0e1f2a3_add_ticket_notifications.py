"""add ticket notifications

Revision ID: b8c9d0e1f2a3
Revises: a4c9e2f7b1d3
Create Date: 2026-06-18 10:05:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "b8c9d0e1f2a3"
down_revision = "a4c9e2f7b1d3"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "ticket_notifications" in tables:
        return

    op.create_table(
        "ticket_notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("recipient_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("notification_type", sa.String(length=50), nullable=False, server_default="ticket_created"),
        sa.Column("comment_preview", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["recipient_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["ticket_tasks.id"]),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ticket_notifications_recipient_id", "ticket_notifications", ["recipient_id"], unique=False)
    op.create_index("ix_ticket_notifications_task_id", "ticket_notifications", ["task_id"], unique=False)
    op.create_index("ix_ticket_notifications_ticket_id", "ticket_notifications", ["ticket_id"], unique=False)


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "ticket_notifications" not in tables:
        return

    op.drop_index("ix_ticket_notifications_task_id", table_name="ticket_notifications")
    op.drop_index("ix_ticket_notifications_ticket_id", table_name="ticket_notifications")
    op.drop_index("ix_ticket_notifications_recipient_id", table_name="ticket_notifications")
    op.drop_table("ticket_notifications")
