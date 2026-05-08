"""Add client_code column replacing branch_name usage

Revision ID: 8f56d5c1c1c0
Revises: 2d4d02a5eacb
Create Date: 2025-02-06
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "8f56d5c1c1c0"
down_revision = "2d4d02a5eacb"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("clients", sa.Column("client_code", sa.String(length=255), nullable=True))

    # Backfill client_code from branch_name if present
    conn = op.get_bind()
    try:
        conn.execute(sa.text("UPDATE clients SET client_code = branch_name WHERE client_code IS NULL AND branch_name IS NOT NULL"))
    except Exception:
        # If branch_name column is missing, ignore
        pass


def downgrade():
    op.drop_column("clients", "client_code")
