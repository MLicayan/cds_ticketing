"""add developer prompt tasks table

Revision ID: a1b2c3d4e5f6
Revises: f1a2b3c4d5e6
Create Date: 2026-07-14 12:25:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "a1b2c3d4e5f6"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "developer_prompt_tasks" not in tables:
        op.create_table(
            "developer_prompt_tasks",
            sa.Column("prompt_id", sa.Integer(), nullable=False),
            sa.Column("task_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["prompt_id"], ["developer_prompts.id"]),
            sa.ForeignKeyConstraint(["task_id"], ["ticket_tasks.id"]),
            sa.PrimaryKeyConstraint("prompt_id", "task_id"),
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "developer_prompt_tasks" in tables:
        op.drop_table("developer_prompt_tasks")
