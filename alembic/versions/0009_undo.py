"""undo: change_record (row-level before/after per audited change) + undo columns on audit_log

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-13 15:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('audit_log') as b:
        b.add_column(sa.Column('undone_at', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('undone_by', sa.String(length=60), nullable=True))
        b.add_column(sa.Column('undo_of_id', sa.Integer(), nullable=True))
    op.create_table(
        'change_record',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('audit_id', sa.Integer(), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('table_name', sa.String(length=60), nullable=False),
        sa.Column('row_pk', sa.Integer(), nullable=False),
        sa.Column('op', sa.String(length=8), nullable=False),
        sa.Column('before', sa.Text(), nullable=True),
        sa.Column('after', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['audit_id'], ['audit_log.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_change_record_audit_id', 'change_record', ['audit_id'])


def downgrade():
    op.drop_index('ix_change_record_audit_id', table_name='change_record')
    op.drop_table('change_record')
    with op.batch_alter_table('audit_log') as b:
        b.drop_column('undo_of_id')
        b.drop_column('undone_by')
        b.drop_column('undone_at')
