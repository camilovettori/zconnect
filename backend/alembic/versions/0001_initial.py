"""initial schema

Revision ID: 0001_initial
Revises: 
Create Date: 2026-03-31 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'settings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('key', sa.String(length=128), nullable=False, unique=True),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'sync_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('date_from', sa.String(length=32), nullable=False),
        sa.Column('date_to', sa.String(length=32), nullable=False),
        sa.Column('total_orders', sa.Integer(), nullable=True),
        sa.Column('total_customers', sa.Integer(), nullable=True),
        sa.Column('total_invoices', sa.Integer(), nullable=True),
        sa.Column('errors', sa.Text(), nullable=True),
    )

    op.create_table(
        'exported_orders',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('sync_run_id', sa.Integer(), sa.ForeignKey('sync_runs.id'), nullable=False),
        sa.Column('unify_order_id', sa.String(length=128), nullable=False),
        sa.Column('customer_name', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('unify_order_id', name='uq_unify_order_id'),
    )

    op.create_table(
        'exported_invoices',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('sync_run_id', sa.Integer(), sa.ForeignKey('sync_runs.id'), nullable=False),
        sa.Column('unify_customer_name', sa.String(length=255), nullable=False),
        sa.Column('unify_order_ids', sa.JSON(), nullable=False),
        sa.Column('zoho_invoice_id', sa.String(length=128), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'customer_mappings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('unify_customer_name', sa.String(length=255), nullable=False, unique=True),
        sa.Column('zoho_contact_id', sa.String(length=128), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'item_mappings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('unify_item_sku', sa.String(length=128), nullable=False, unique=True),
        sa.Column('zoho_item_id', sa.String(length=128), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table('item_mappings')
    op.drop_table('customer_mappings')
    op.drop_table('exported_invoices')
    op.drop_table('exported_orders')
    op.drop_table('sync_runs')
    op.drop_table('settings')
