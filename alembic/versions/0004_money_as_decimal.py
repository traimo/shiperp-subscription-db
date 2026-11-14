"""money as decimal: Float -> Numeric(14, 2) for every money column

Rounds stored amounts to the cent first (only license_purchase.maintenance_amount had sub-cent values, from
list price x maintenance % in the workbook). term_months, quantity and reseller.commission_rate are not money and
stay Float.

On SQLite the tables are rebuilt (batch mode); foreign keys are switched off for the rebuild in alembic/env.py.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-12 22:45:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

MONEY = {
    'term': ['tcv', 'arr_override'],
    'invoice': ['amount'],
    'non_recurring_fee': ['amount'],
    'license_purchase': ['list_price', 'discount', 'amount', 'maintenance_amount'],
    'maintenance_fee': ['amount'],
    'maintenance_invoice': ['amount'],
}
NOT_NULL = {('term', 'tcv'), ('invoice', 'amount'), ('non_recurring_fee', 'amount'), ('license_purchase', 'amount'),
            ('maintenance_fee', 'amount'), ('maintenance_invoice', 'amount')}


def upgrade():
    # PostgreSQL has ROUND(numeric, int) only, not ROUND(double precision, int); SQLite keeps the original SQL.
    pg = op.get_bind().dialect.name == "postgresql"
    for table, cols in MONEY.items():
        for col in cols:
            value = f"CAST({col} AS NUMERIC)" if pg else col
            op.execute(f"UPDATE {table} SET {col} = ROUND({value}, 2) WHERE {col} IS NOT NULL")
        with op.batch_alter_table(table) as b:
            for col in cols:
                b.alter_column(col, existing_type=sa.Float(), type_=sa.Numeric(14, 2),
                               existing_nullable=(table, col) not in NOT_NULL)


def downgrade():
    for table, cols in MONEY.items():
        with op.batch_alter_table(table) as b:
            for col in cols:
                b.alter_column(col, existing_type=sa.Numeric(14, 2), type_=sa.Float(),
                               existing_nullable=(table, col) not in NOT_NULL)
