"""add task comment replies

Revision ID: c1a2b3d4e5f6
Revises: b7e9c2d4a6f1
Create Date: 2026-05-12 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "c1a2b3d4e5f6"
down_revision = "b7e9c2d4a6f1"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("ticket_task_comments")}

    if "parent_comment_id" not in columns:
        with op.batch_alter_table("ticket_task_comments", schema=None) as batch_op:
            batch_op.add_column(sa.Column("parent_comment_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                "fk_ticket_task_comments_parent_comment_id",
                "ticket_task_comments",
                ["parent_comment_id"],
                ["id"],
            )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("ticket_task_comments")}

    if "parent_comment_id" in columns:
        with op.batch_alter_table("ticket_task_comments", schema=None) as batch_op:
            batch_op.drop_constraint("fk_ticket_task_comments_parent_comment_id", type_="foreignkey")
            batch_op.drop_column("parent_comment_id")
