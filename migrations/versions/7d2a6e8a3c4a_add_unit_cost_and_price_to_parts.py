"""Add unit cost and price to parts

Revision ID: 7d2a6e8a3c4a
Revises: 3b4f6a8a7b2c
Create Date: 2025-02-08
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "7d2a6e8a3c4a"
down_revision = "3b4f6a8a7b2c"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("parts", sa.Column("unit_cost", sa.Numeric(12, 2), nullable=True))
    op.add_column("parts", sa.Column("price", sa.Numeric(12, 2), nullable=True))


def downgrade():
    op.drop_column("parts", "price")
    op.drop_column("parts", "unit_cost")
