"""Persist historical Zoho invoice snapshots separately from Unify sync facts.

Revision ID: 0004_zoho_history
Revises: 0003_analytics
Only additive schema changes; requires explicit Alembic migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_zoho_history"
down_revision = "0003_analytics"
branch_labels = None
depends_on = None

BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade():
    op.create_table(
        "zconnect_historical_invoices",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.String(64), sa.ForeignKey("zconnect_companies.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("zoho_org_id", sa.String(128), nullable=False),
        sa.Column("zoho_invoice_id", sa.String(128), nullable=False),
        sa.Column("invoice_number", sa.String(128), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column("customer_id", sa.String(128), nullable=True),
        sa.Column("customer_name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("net_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("company_id", "zoho_org_id", "zoho_invoice_id", name="uq_zconnect_hist_invoice"),
        sa.UniqueConstraint("id", "company_id", name="uq_zconnect_hist_invoice_company"),
    )
    op.create_index("ix_zconnect_hist_invoice_date", "zconnect_historical_invoices", ["company_id", "invoice_date"])
    op.create_index("ix_zconnect_hist_invoice_customer", "zconnect_historical_invoices", ["company_id", "customer_id"])
    op.create_table(
        "zconnect_historical_invoice_lines",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("invoice_id", BIGINT, nullable=False),
        sa.Column("company_id", sa.String(64), nullable=False),
        sa.Column("zoho_line_item_id", sa.String(128), nullable=True),
        sa.Column("product_key", sa.String(255), nullable=False),
        sa.Column("product_name", sa.String(255), nullable=False),
        sa.Column("quantity", sa.Numeric(14, 3), nullable=False),
        sa.Column("net_amount", sa.Numeric(14, 2), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id", "company_id"], ["zconnect_historical_invoices.id", "zconnect_historical_invoices.company_id"], ondelete="CASCADE", name="fk_zconnect_hist_line_company"),
    )
    op.create_index("ix_zconnect_hist_line_product", "zconnect_historical_invoice_lines", ["company_id", "product_key"])
    if op.get_bind().dialect.name == "postgresql":
        names = "public.zconnect_historical_invoices, public.zconnect_historical_invoice_lines"
        op.execute(f"REVOKE ALL ON TABLE {names} FROM PUBLIC")
        for table in ["zconnect_historical_invoices", "zconnect_historical_invoice_lines"]:
            op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute("REVOKE ALL ON SEQUENCE public.zconnect_historical_invoices_id_seq, public.zconnect_historical_invoice_lines_id_seq FROM PUBLIC")
        op.execute("""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                    REVOKE ALL ON TABLE public.zconnect_historical_invoices, public.zconnect_historical_invoice_lines FROM anon;
                    REVOKE ALL ON SEQUENCE public.zconnect_historical_invoices_id_seq, public.zconnect_historical_invoice_lines_id_seq FROM anon;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                    REVOKE ALL ON TABLE public.zconnect_historical_invoices, public.zconnect_historical_invoice_lines FROM authenticated;
                    REVOKE ALL ON SEQUENCE public.zconnect_historical_invoices_id_seq, public.zconnect_historical_invoice_lines_id_seq FROM authenticated;
                END IF;
            END $$;
        """)


def downgrade():
    # Destructive: historical data must be backed up before downgrading.
    op.drop_index("ix_zconnect_hist_line_product", table_name="zconnect_historical_invoice_lines")
    op.drop_table("zconnect_historical_invoice_lines")
    op.drop_index("ix_zconnect_hist_invoice_customer", table_name="zconnect_historical_invoices")
    op.drop_index("ix_zconnect_hist_invoice_date", table_name="zconnect_historical_invoices")
    op.drop_table("zconnect_historical_invoices")
