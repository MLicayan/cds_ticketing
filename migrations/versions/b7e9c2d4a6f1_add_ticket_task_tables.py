"""add ticket task tables

Revision ID: b7e9c2d4a6f1
Revises: a9c1d2e3f4b5
Create Date: 2026-05-09 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "b7e9c2d4a6f1"
down_revision = "a9c1d2e3f4b5"
branch_labels = None
depends_on = None


ticket_priority = sa.Enum("NOT_SET", "low", "medium", "high", "critical", name="ticketpriority")
ticket_status = sa.Enum("open", "in_progress", "on_hold", "resolved", "reopened", "closed", "cancelled", name="ticketstatus")


def upgrade():
    inspector = sa.inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())

    if "ticket_tasks" not in existing_tables:
        op.create_table(
            "ticket_tasks",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("task_no", sa.String(length=32), nullable=False),
            sa.Column("ticket_id", sa.Integer(), nullable=False),
            sa.Column("client_id", sa.Integer(), nullable=False),
            sa.Column("instrument_id", sa.Integer(), nullable=True),
            sa.Column("app_id", sa.Integer(), nullable=True),
            sa.Column("ticket_for", sa.String(length=20), nullable=True),
            sa.Column("reported_by_id", sa.Integer(), nullable=False),
            sa.Column("assigned_engineer_id", sa.Integer(), nullable=True),
            sa.Column("assigned_by_id", sa.Integer(), nullable=True),
            sa.Column("priority", ticket_priority, nullable=False),
            sa.Column("subject", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("status", ticket_status, nullable=True),
            sa.Column("kanban_bucket", sa.String(length=20), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("closed_at", sa.DateTime(), nullable=True),
            sa.Column("started_date", sa.Date(), nullable=True),
            sa.Column("is_working", sa.Boolean(), nullable=True),
            sa.Column("date_needed", sa.DateTime(), nullable=True),
            sa.Column("target_date", sa.Date(), nullable=True),
            sa.ForeignKeyConstraint(["app_id"], ["apps.id"]),
            sa.ForeignKeyConstraint(["assigned_by_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["assigned_engineer_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
            sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
            sa.ForeignKeyConstraint(["reported_by_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("task_no"),
        )

    if "ticket_task_comments" not in existing_tables:
        op.create_table(
            "ticket_task_comments",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("ticket_task_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("comment_text", sa.Text(), nullable=False),
            sa.Column("is_internal", sa.Boolean(), nullable=True),
            sa.Column("reactions_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["ticket_task_id"], ["ticket_tasks.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    if "ticket_task_attachments" not in existing_tables:
        op.create_table(
            "ticket_task_attachments",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("ticket_task_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("stored_filename", sa.String(length=255), nullable=False),
            sa.Column("original_filename", sa.String(length=255), nullable=False),
            sa.Column("content_type", sa.String(length=128), nullable=True),
            sa.Column("file_size", sa.Integer(), nullable=True),
            sa.Column("uploaded_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["ticket_task_id"], ["ticket_tasks.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    op.execute(
        """
        INSERT INTO ticket_tasks (
            task_no, ticket_id, client_id, instrument_id, app_id, ticket_for,
            reported_by_id, assigned_engineer_id, assigned_by_id, priority,
            subject, description, status, kanban_bucket, created_at, updated_at,
            closed_at, started_date, is_working, date_needed, target_date
        )
        SELECT
            ticket_no, CAST(SUBSTRING(category, 6) AS UNSIGNED), client_id,
            instrument_id, app_id, ticket_for, reported_by_id, assigned_engineer_id,
            assigned_by_id, priority, subject, description, status, kanban_bucket,
            created_at, updated_at, closed_at, started_date, is_working,
            date_needed, target_date
        FROM tickets
        WHERE category LIKE 'task:%'
          AND CAST(SUBSTRING(category, 6) AS UNSIGNED) IN (SELECT id FROM tickets)
          AND NOT EXISTS (
              SELECT 1 FROM ticket_tasks existing_task
              WHERE existing_task.task_no = tickets.ticket_no
          )
        """
    )
    op.execute(
        """
        INSERT INTO ticket_task_comments (
            ticket_task_id, user_id, comment_text, is_internal, reactions_json, created_at
        )
        SELECT tt.id, tc.user_id, tc.comment_text, tc.is_internal, tc.reactions_json, tc.created_at
        FROM ticket_comments tc
        JOIN tickets old_task ON old_task.id = tc.ticket_id
        JOIN ticket_tasks tt ON tt.task_no = old_task.ticket_no
        WHERE old_task.category LIKE 'task:%'
          AND NOT EXISTS (
              SELECT 1 FROM ticket_task_comments existing_comment
              WHERE existing_comment.ticket_task_id = tt.id
                AND existing_comment.user_id = tc.user_id
                AND existing_comment.comment_text = tc.comment_text
                AND existing_comment.created_at = tc.created_at
          )
        """
    )
    op.execute(
        """
        INSERT INTO ticket_task_attachments (
            ticket_task_id, user_id, stored_filename, original_filename, content_type,
            file_size, uploaded_at
        )
        SELECT tt.id, ta.user_id, ta.stored_filename, ta.original_filename,
            ta.content_type, ta.file_size, ta.uploaded_at
        FROM ticket_attachments ta
        JOIN tickets old_task ON old_task.id = ta.ticket_id
        JOIN ticket_tasks tt ON tt.task_no = old_task.ticket_no
        WHERE old_task.category LIKE 'task:%'
          AND NOT EXISTS (
              SELECT 1 FROM ticket_task_attachments existing_attachment
              WHERE existing_attachment.ticket_task_id = tt.id
                AND existing_attachment.stored_filename = ta.stored_filename
          )
        """
    )


def downgrade():
    op.drop_table("ticket_task_attachments")
    op.drop_table("ticket_task_comments")
    op.drop_table("ticket_tasks")
