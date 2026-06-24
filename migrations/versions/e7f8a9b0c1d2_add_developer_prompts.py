"""add developer prompts

Revision ID: e7f8a9b0c1d2
Revises: c9d0e1f2a3b4
Create Date: 2026-06-24 12:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "e7f8a9b0c1d2"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "developer_prompts" not in tables:
        op.create_table(
            "developer_prompts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("created_by_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_developer_prompts_created_by_id", "developer_prompts", ["created_by_id"], unique=False)

    if "developer_prompt_responses" not in tables:
        op.create_table(
            "developer_prompt_responses",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("prompt_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("response_status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("responded_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["prompt_id"], ["developer_prompts.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("prompt_id", "user_id", name="uq_developer_prompt_response_prompt_user"),
        )
        op.create_index("ix_developer_prompt_responses_prompt_id", "developer_prompt_responses", ["prompt_id"], unique=False)
        op.create_index("ix_developer_prompt_responses_user_id", "developer_prompt_responses", ["user_id"], unique=False)


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "developer_prompt_responses" in tables:
        op.drop_index("ix_developer_prompt_responses_user_id", table_name="developer_prompt_responses")
        op.drop_index("ix_developer_prompt_responses_prompt_id", table_name="developer_prompt_responses")
        op.drop_table("developer_prompt_responses")

    if "developer_prompts" in tables:
        op.drop_index("ix_developer_prompts_created_by_id", table_name="developer_prompts")
        op.drop_table("developer_prompts")
