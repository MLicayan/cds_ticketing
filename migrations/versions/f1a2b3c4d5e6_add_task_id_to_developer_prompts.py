"""add task id to developer prompts

Revision ID: f1a2b3c4d5e6
Revises: e7f8a9b0c1d2
Create Date: 2026-07-14 11:40:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "f1a2b3c4d5e6"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "developer_prompts" not in tables:
        return

    columns = {column["name"] for column in inspector.get_columns("developer_prompts")}
    foreign_keys = {fk.get("name") for fk in inspector.get_foreign_keys("developer_prompts")}

    if "task_id" not in columns:
        op.add_column("developer_prompts", sa.Column("task_id", sa.Integer(), nullable=True))

    if "fk_developer_prompts_task_id_ticket_tasks" not in foreign_keys:
        op.create_foreign_key(
            "fk_developer_prompts_task_id_ticket_tasks",
            "developer_prompts",
            "ticket_tasks",
            ["task_id"],
            ["id"],
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "developer_prompts" not in tables:
        return

    foreign_keys = {fk.get("name") for fk in inspector.get_foreign_keys("developer_prompts")}
    columns = {column["name"] for column in inspector.get_columns("developer_prompts")}

    if "fk_developer_prompts_task_id_ticket_tasks" in foreign_keys:
        op.drop_constraint(
            "fk_developer_prompts_task_id_ticket_tasks",
            "developer_prompts",
            type_="foreignkey",
        )

    if "task_id" in columns:
        op.drop_column("developer_prompts", "task_id")
