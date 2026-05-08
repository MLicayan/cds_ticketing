"""Add service log confirmation fields and reopened ticket status

Revision ID: 2d4d02a5eacb
Revises: e64656674204
Create Date: 2025-02-06
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "2d4d02a5eacb"
down_revision = "e64656674204"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("service_logs", sa.Column("confirmed_by", sa.String(length=255), nullable=True))
    op.add_column("service_logs", sa.Column("confirmed_by_position", sa.String(length=255), nullable=True))
    op.add_column("service_logs", sa.Column("confirm_photo_name", sa.String(length=255), nullable=True))

    # SQLite cannot alter enums easily; ensure status string is allowed in code. No DB change needed for string enums.


def downgrade():
    op.drop_column("service_logs", "confirm_photo_name")
    op.drop_column("service_logs", "confirmed_by_position")
    op.drop_column("service_logs", "confirmed_by")
