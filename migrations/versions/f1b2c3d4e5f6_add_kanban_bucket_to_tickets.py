"""add kanban bucket to tickets

Revision ID: f1b2c3d4e5f6
Revises: 1e0271bda361
Create Date: 2026-05-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f1b2c3d4e5f6'
down_revision = '1e0271bda361'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('tickets', schema=None) as batch_op:
        batch_op.add_column(sa.Column('kanban_bucket', sa.String(length=20), nullable=True))


def downgrade():
    with op.batch_alter_table('tickets', schema=None) as batch_op:
        batch_op.drop_column('kanban_bucket')
