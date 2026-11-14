"""contract documents: uploaded PDF / Word contracts per term or maintenance line (metadata; bytes in the store)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-12 23:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'contract_document',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('term_id', sa.Integer(), nullable=True),
        sa.Column('maintenance_contract_id', sa.Integer(), nullable=True),
        sa.Column('link_qb', sa.Integer(), nullable=False),
        sa.Column('link_source_ref', sa.String(length=60), nullable=True),
        sa.Column('filename', sa.String(length=200), nullable=False),
        sa.Column('content_type', sa.String(length=100), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('storage_key', sa.String(length=200), nullable=False),
        sa.Column('doc_type', sa.Enum('SIGNED_CONTRACT', 'ORDER_FORM', 'AMENDMENT', 'OTHER', name='doctype',
                                      native_enum=False, length=24), nullable=False),
        sa.Column('note', sa.String(length=300), nullable=True),
        sa.Column('uploaded_by', sa.String(length=60), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('term_id IS NULL OR maintenance_contract_id IS NULL', name='ck_document_one_parent'),
        sa.CheckConstraint('size_bytes > 0', name='ck_document_size'),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
        sa.ForeignKeyConstraint(['maintenance_contract_id'], ['maintenance_contract.id']),
        sa.ForeignKeyConstraint(['term_id'], ['term.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('storage_key'),
    )
    with op.batch_alter_table('contract_document') as b:
        b.create_index('ix_contract_document_customer_id', ['customer_id'])
        b.create_index('ix_contract_document_term_id', ['term_id'])
        b.create_index('ix_contract_document_maintenance_contract_id', ['maintenance_contract_id'])
        b.create_index('ix_contract_document_sha256', ['sha256'])


def downgrade():
    op.drop_table('contract_document')
