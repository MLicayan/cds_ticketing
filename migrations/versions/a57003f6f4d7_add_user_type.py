"""Add user_type to users

Revision ID: a57003f6f4d7
Revises: 1b8dc29b2c3c
Create Date: 2025-02-07
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a57003f6f4d7"
down_revision = "1b8dc29b2c3c"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("user_type", sa.String(length=50), nullable=True))
    # backfill default
    op.execute("UPDATE users SET user_type = 'Engineer' WHERE user_type IS NULL")


def downgrade():
    op.drop_column("users", "user_type")
