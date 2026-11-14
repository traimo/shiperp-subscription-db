"""sharepoint copies: documents are SharePoint links only; a link can hold a read-only copy of the file

url becomes NOT NULL (uploads were removed); new columns copied_at, copy_error, sp_etag, sp_modified; the CHECK
becomes "a stored copy has a size". Aborts if upload-only rows (no url) exist. Explicit rebuild like 0006.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-13 10:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None

COMMON = ("id, customer_id, term_id, maintenance_contract_id, link_qb, link_source_ref, filename, content_type, "
          "size_bytes, sha256, storage_key, url, doc_type, note, uploaded_by, uploaded_at")
INDEXES = ('customer_id', 'term_id', 'maintenance_contract_id', 'sha256')


def _create(name, v7: bool):
    cols = [
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('term_id', sa.Integer(), nullable=True),
        sa.Column('maintenance_contract_id', sa.Integer(), nullable=True),
        sa.Column('link_qb', sa.Integer(), nullable=False),
        sa.Column('link_source_ref', sa.String(length=60), nullable=True),
        sa.Column('filename', sa.String(length=200), nullable=False),
        sa.Column('content_type', sa.String(length=100), nullable=True),
        sa.Column('size_bytes', sa.Integer(), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('storage_key', sa.String(length=200), nullable=True),
        sa.Column('url', sa.String(length=1000), nullable=not v7),
    ]
    if v7:
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
        sa.CheckConstraint('storage_key IS NULL OR size_bytes > 0', name='ck_document_copy_size') if v7 else
        sa.CheckConstraint("(url IS NOT NULL AND storage_key IS NULL) OR "
                           "(url IS NULL AND storage_key IS NOT NULL AND size_bytes > 0)",
                           name='ck_document_file_or_link'),
        sa.ForeignKeyConstraint(['customer_id'], ['customer.id']),
        sa.ForeignKeyConstraint(['maintenance_contract_id'], ['maintenance_contract.id']),
        sa.ForeignKeyConstraint(['term_id'], ['term.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('storage_key'),
    ]
    op.create_table(name, *cols)


def _swap(v7: bool):
    _create('contract_document_new', v7)
    op.execute(f"INSERT INTO contract_document_new ({COMMON}) SELECT {COMMON} FROM contract_document")
    for col in INDEXES:
        op.drop_index(f'ix_contract_document_{col}', table_name='contract_document')
    op.drop_table('contract_document')
    op.rename_table('contract_document_new', 'contract_document')
    for col in INDEXES:
        op.create_index(f'ix_contract_document_{col}', 'contract_document', [col])


def upgrade():
    n = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM contract_document WHERE url IS NULL")).scalar()
    if n:
        raise RuntimeError(f"{n} uploaded document(s) have no SharePoint link. Uploads were removed in 0007: link "
                           "those files on SharePoint (or delete them) before upgrading.")
    _swap(v7=True)


def downgrade():
    op.execute("UPDATE contract_document SET storage_key = NULL, size_bytes = NULL, sha256 = NULL, "
               "content_type = NULL")      # 0006 does not allow a link with a stored copy
    _swap(v7=False)
