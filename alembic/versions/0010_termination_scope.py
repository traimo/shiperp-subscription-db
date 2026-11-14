"""termination_term: a termination can end only some contracts (terms) of a subscription

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-14 00:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'termination_term',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('termination_id', sa.Integer(), nullable=False),
        sa.Column('term_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['termination_id'], ['termination.id']),
        sa.ForeignKeyConstraint(['term_id'], ['term.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_termination_term_termination_id', 'termination_term', ['termination_id'])
    op.create_index('ix_termination_term_term_id', 'termination_term', ['term_id'])


def downgrade():
    op.drop_index('ix_termination_term_term_id', table_name='termination_term')
    op.drop_index('ix_termination_term_termination_id', table_name='termination_term')
    op.drop_table('termination_term')
