"""links only: the SharePoint integration (read-only copies for the buyer zip) was removed

Drops the copy columns (content_type, size_bytes, sha256, storage_key, copied_at, copy_error, sp_etag, sp_modified) and
the copy-size CHECK. A contract document is now just a SharePoint link. Explicit rebuild like 0006 / 0007. Copy files
already in data\\contracts\\ are not touched (delete them by hand if no longer needed).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-13 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None

COMMON = ("id, customer_id, term_id, maintenance_contract_id, link_qb, link_source_ref, filename, url, doc_type, "
          "note, uploaded_by, uploaded_at")
INDEXES_V7 = ('customer_id', 'term_id', 'maintenance_contract_id', 'sha256')
INDEXES_V8 = ('customer_id', 'term_id', 'maintenance_contract_id')


def _create(name, v8: bool):
    cols = [
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('term_id', sa.Integer(), nullable=True),
        sa.Column('maintenance_contract_id', sa.Integer(), nullable=True),
        sa.Column('link_qb', sa.Integer(), nullable=False),
        sa.Column('link_source_ref', sa.String(length=60), nullable=True),
        sa.Column('filename', sa.String(length=200), nullable=False),
    ]
    if not v8:
        cols += [sa.Column('content_type', sa.String(length=100), nullable=True),
                 sa.Column('size_bytes', sa.Integer(), nullable=True),
                 sa.Column('sha256', sa.String(length=64), nullable=True),
                 sa.Column('storage_key', sa.String(length=200), nullable=True)]
    cols += [sa.Column('url', sa.String(length=1000), nullable=False)]
    if not v8:
        cols += [sa.Column('copied_at', sa.DateTime(), nullable=True),
                 sa.Column('copy_error', sa.String(length=300), nullable=True),
                 sa.Column('sp_etag', sa.String(length=200), nullable=True),
                 sa.Column('sp_modified', sa.String(length=40), nullable=True)]
    cols += [
        sa.Column('doc_type', sa.Enum('SIGNED_CONTRACT', 'ORDER_FORM', 'AMENDMENT', 'OTHER', name='doctype',
                                      native_enum=False, length=24), nullable=False),
        sa.Column('note', sa.String(length=300), nullable=True),
        sa.Column('uploaded_by', sa.String(length=60), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('term_id IS NULL OR maintenance_contract_id IS NULL', name='ck_document_one_parent'),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
        sa.ForeignKeyConstraint(['maintenance_contract_id'], ['maintenance_contract.id']),
        sa.ForeignKeyConstraint(['term_id'], ['term.id']),
        sa.PrimaryKeyConstraint('id'),
    ]
    if not v8:
        cols += [sa.CheckConstraint('storage_key IS NULL OR size_bytes > 0', name='ck_document_copy_size'),
                 sa.UniqueConstraint('storage_key')]
    op.create_table(name, *cols)


def _swap(v8: bool):
    _create('contract_document_new', v8)
    op.execute(f"INSERT INTO contract_document_new ({COMMON}) SELECT {COMMON} FROM contract_document")
    for col in (INDEXES_V7 if v8 else INDEXES_V8):
        op.drop_index(f'ix_contract_document_{col}', table_name='contract_document')
    op.drop_table('contract_document')
    op.rename_table('contract_document_new', 'contract_document')
    for col in (INDEXES_V8 if v8 else INDEXES_V7):
        op.create_index(f'ix_contract_document_{col}', 'contract_document', [col])


def upgrade():
    _swap(v8=True)


def downgrade():
    _swap(v8=False)            # links come back without copies
