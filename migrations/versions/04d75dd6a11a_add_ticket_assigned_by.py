"""Add assigned_by to tickets

Revision ID: 04d75dd6a11a
Revises: 8f56d5c1c1c0
Create Date: 2025-02-06
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "04d75dd6a11a"
down_revision = "8f56d5c1c1c0"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = [col["name"] for col in inspector.get_columns("tickets")]
    if "assigned_by_id" not in existing_cols:
        op.add_column("tickets", sa.Column("assigned_by_id", sa.Integer(), nullable=True))
        op.create_foreign_key(None, "tickets", "users", ["assigned_by_id"], ["id"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_cols = [col["name"] for col in inspector.get_columns("tickets")]
    if "assigned_by_id" in existing_cols:
        op.drop_constraint(None, "tickets", type_="foreignkey")
        op.drop_column("tickets", "assigned_by_id")
