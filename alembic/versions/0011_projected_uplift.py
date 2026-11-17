"""term.projected_uplift: per-contract uplift for a Projected Renewal (NULL = the app default, 5%)

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-16 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('term', sa.Column('projected_uplift', sa.Numeric(5, 4), nullable=True))
    op.create_check_constraint('ck_term_projected_uplift', 'term',
                               'projected_uplift IS NULL OR (projected_uplift >= 0 AND projected_uplift <= 0.5)')


def downgrade():
    op.drop_constraint('ck_term_projected_uplift', 'term', type_='check')
    op.drop_column('term', 'projected_uplift')
