"""document links: a contract document can be a SharePoint link (url) instead of an uploaded file

File columns (content_type, size_bytes, sha256, storage_key) become nullable; new column url; the size check becomes
"either a link or a stored file". The table is rebuilt explicitly (create / copy / drop / rename) so the new CHECK
constraint is exact on SQLite as well.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-13 09:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

COPY = ("id, customer_id, term_id, maintenance_contract_id, link_qb, link_source_ref, filename, content_type, "
        "size_bytes, sha256, storage_key, doc_type, note, uploaded_by, uploaded_at")
INDEXES = ('customer_id', 'term_id', 'maintenance_contract_id', 'sha256')


def _create(name, link_check: bool):
    nullable = link_check
    cols = [
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('term_id', sa.Integer(), nullable=True),
        sa.Column('maintenance_contract_id', sa.Integer(), nullable=True),
        sa.Column('link_qb', sa.Integer(), nullable=False),
        sa.Column('link_source_ref', sa.String(length=60), nullable=True),
        sa.Column('filename', sa.String(length=200), nullable=False),
        sa.Column('content_type', sa.String(length=100), nullable=nullable),
        sa.Column('size_bytes', sa.Integer(), nullable=nullable),
        sa.Column('sha256', sa.String(length=64), nullable=nullable),
        sa.Column('storage_key', sa.String(length=200), nullable=nullable),
    ]
    if link_check:
        cols.append(sa.Column('url', sa.String(length=1000), nullable=True))
    cols += [
        sa.Column('doc_type', sa.Enum('SIGNED_CONTRACT', 'ORDER_FORM', 'AMENDMENT', 'OTHER', name='doctype',
                                      native_enum=False, length=24), nullable=False),
        sa.Column('note', sa.String(length=300), nullable=True),
        sa.Column('uploaded_by', sa.String(length=60), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('term_id IS NULL OR maintenance_contract_id IS NULL', name='ck_document_one_parent'),
        sa.CheckConstraint("(url IS NOT NULL AND storage_key IS NULL) OR "
                           "(url IS NULL AND storage_key IS NOT NULL AND size_bytes > 0)",
                           name='ck_document_file_or_link') if link_check else
        sa.CheckConstraint('size_bytes > 0', name='ck_document_size'),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
        sa.ForeignKeyConstraint(['maintenance_contract_id'], ['maintenance_contract.id']),
        sa.ForeignKeyConstraint(['term_id'], ['term.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('storage_key'),
    ]
    op.create_table(name, *cols)


def _swap(link_check: bool):
    _create('contract_document_new', link_check)
    op.execute(f"INSERT INTO contract_document_new ({COPY}) SELECT {COPY} FROM contract_document")
    for col in INDEXES:
        op.drop_index(f'ix_contract_document_{col}', table_name='contract_document')
    op.drop_table('contract_document')
    op.rename_table('contract_document_new', 'contract_document')
    for col in INDEXES:
        op.create_index(f'ix_contract_document_{col}', 'contract_document', [col])


def upgrade():
    _swap(link_check=True)


def downgrade():
    op.execute("DELETE FROM contract_document WHERE url IS NOT NULL")
    _swap(link_check=False)
