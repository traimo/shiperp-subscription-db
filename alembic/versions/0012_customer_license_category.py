"""License Category / Hybrid class move from subscription to customer (Hybrid = subscription + perpetual maintenance)

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-18 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('customer', sa.Column('license_category', sa.String(40), nullable=True))
    op.add_column('customer', sa.Column('hybrid_class', sa.String(60), nullable=True))
    # a customer is Hybrid when any of its subscriptions is; otherwise the newest subscription's value.
    # The hybrid class comes from the newest Hybrid subscription that has one (blank / n/a ignored).
    op.execute("""
        UPDATE customer c SET license_category = x.lc, hybrid_class = x.hc
        FROM (SELECT s.customer_id,
                     CASE WHEN bool_or(s.license_category ILIKE 'hybrid') THEN 'Hybrid'
                          ELSE (array_agg(s.license_category ORDER BY s.id DESC)
                                FILTER (WHERE s.license_category IS NOT NULL))[1] END AS lc,
                     (array_agg(s.hybrid_class ORDER BY s.id DESC)
                      FILTER (WHERE s.license_category ILIKE 'hybrid' AND s.hybrid_class IS NOT NULL
                              AND btrim(s.hybrid_class) <> '' AND lower(btrim(s.hybrid_class)) <> 'n/a'))[1] AS hc
              FROM subscription s GROUP BY s.customer_id) x
        WHERE x.customer_id = c.id""")
    op.drop_column('subscription', 'hybrid_class')
    op.drop_column('subscription', 'license_category')


def downgrade():
    op.add_column('subscription', sa.Column('license_category', sa.String(40), nullable=True))
    op.add_column('subscription', sa.Column('hybrid_class', sa.String(60), nullable=True))
    op.execute("UPDATE subscription s SET license_category = c.license_category, hybrid_class = c.hybrid_class "
               "FROM customer c WHERE c.id = s.customer_id")
    op.drop_column('customer', 'hybrid_class')
    op.drop_column('customer', 'license_category')
