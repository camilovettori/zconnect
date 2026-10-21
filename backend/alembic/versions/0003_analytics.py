"""Analytics: companies, sync-run ownership and captured order values

Additive only: creates four new zconnect_ tables and seeds the default company. The seven
original tables are not modified. On Supabase the new tables are immediately removed from
the API roles (PUBLIC, anon, authenticated) and get row-level security; the backend
connects as their owner and is unaffected.

Revision ID: 0003_analytics
Revises: 0002_export_events
Create Date: 2026-10-21 00:00:00.000000
"""
import importlib.util
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "0003_analytics"
down_revision = "0002_export_events"
branch_labels = None
depends_on = None

DEFAULT_COMPANY_ID = "default"
TABLES = ("zconnect_companies", "zconnect_sync_run_companies", "zconnect_order_facts", "zconnect_order_lines")
BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _create_tables():
    op.create_table(
        "zconnect_companies",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.bulk_insert(
        sa.table("zconnect_companies", sa.column("id", sa.String), sa.column("name", sa.String)),
        [{"id": DEFAULT_COMPANY_ID, "name": "Default company"}],
    )

    op.create_table(
        "zconnect_sync_run_companies",
        sa.Column("sync_run_id", sa.Integer(), sa.ForeignKey("sync_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("company_id", sa.String(length=64), sa.ForeignKey("zconnect_companies.id", ondelete="RESTRICT"), nullable=False),
        sa.UniqueConstraint("sync_run_id", "company_id", name="uq_zconnect_run_company"),
    )
    op.create_index("ix_zconnect_sync_run_companies_company", "zconnect_sync_run_companies", ["company_id"])

    op.create_table(
        "zconnect_order_facts",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.String(length=64), sa.ForeignKey("zconnect_companies.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("unify_order_id", sa.String(length=128), nullable=False),
        sa.Column("sync_run_id", sa.Integer(), sa.ForeignKey("sync_runs.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("zoho_invoice_id", sa.String(length=128), nullable=True),
        sa.Column("customer_key", sa.String(length=255), nullable=False),
        sa.Column("customer_name", sa.String(length=255), nullable=True),
        sa.Column("order_date", sa.Date(), nullable=True),
        sa.Column("delivery_date", sa.Date(), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("net_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("company_id", "unify_order_id", name="uq_zconnect_order_facts_company_order"),
        sa.UniqueConstraint("id", "company_id", name="uq_zconnect_fact_company"),
        sa.ForeignKeyConstraint(["sync_run_id", "company_id"], ["zconnect_sync_run_companies.sync_run_id", "zconnect_sync_run_companies.company_id"], name="fk_zconnect_fact_run_company", ondelete="RESTRICT"),
    )
    op.create_index("ix_zconnect_order_facts_company_date", "zconnect_order_facts", ["company_id", "invoice_date"])
    op.create_index("ix_zconnect_order_facts_company_customer", "zconnect_order_facts", ["company_id", "customer_key"])

    op.create_table(
        "zconnect_order_lines",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("order_fact_id", BIGINT, nullable=False),
        sa.Column("company_id", sa.String(length=64), nullable=False),
        sa.Column("product_key", sa.String(length=255), nullable=False),
        sa.Column("product_name", sa.String(length=255), nullable=False),
        sa.Column("quantity", sa.Numeric(14, 3), nullable=False),
        sa.Column("unit_price", sa.Numeric(14, 4), nullable=False),
        sa.Column("net_amount", sa.Numeric(14, 2), nullable=False),
        sa.ForeignKeyConstraint(["order_fact_id", "company_id"], ["zconnect_order_facts.id", "zconnect_order_facts.company_id"], name="fk_zconnect_line_fact_company", ondelete="CASCADE"),
    )
    op.create_index("ix_zconnect_order_lines_order", "zconnect_order_lines", ["order_fact_id"])
    op.create_index("ix_zconnect_order_lines_company_product", "zconnect_order_lines", ["company_id", "product_key"])


def _harden_tables():
    if op.get_bind().dialect.name == "postgresql":
        tables = ", ".join(f"public.{name}" for name in TABLES)
        # API roles exist only on Supabase; elsewhere only PUBLIC is revoked.
        op.execute(
            f"""
            DO $$
            DECLARE api_role text;
            BEGIN
                EXECUTE 'REVOKE ALL ON TABLE {tables} FROM PUBLIC';
                EXECUTE 'REVOKE ALL ON SEQUENCE public.zconnect_order_facts_id_seq, public.zconnect_order_lines_id_seq FROM PUBLIC';
                FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
                        EXECUTE format('REVOKE ALL ON TABLE {tables} FROM %I', api_role);
                        EXECUTE format('REVOKE ALL ON SEQUENCE public.zconnect_order_facts_id_seq, public.zconnect_order_lines_id_seq FROM %I', api_role);
                    END IF;
                END LOOP;
            END $$;
            """
        )
        for name in TABLES:
            op.execute(f"ALTER TABLE public.{name} ENABLE ROW LEVEL SECURITY")


def upgrade():
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names(schema="public" if bind.dialect.name == "postgresql" else None)) & set(TABLES)
    if existing:
        approved = op.get_context().config.get_main_option("zconnect.repair_existing_analytics") == "true"
        if not approved or bind.dialect.name != "postgresql" or existing != set(TABLES):
            raise RuntimeError("Pre-created Analytics tables require guarded, explicitly approved repair; never stamp or partially adopt them")
        # Validate again under locks inside the migration transaction, not only in preflight.
        op.execute("LOCK TABLE " + ", ".join("public." + t for t in TABLES) + " IN ACCESS EXCLUSIVE MODE")
        spec = importlib.util.spec_from_file_location("analytics_preflight", Path(__file__).resolve().parents[2] / "tools/schema_preflight.py")
        preflight = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(preflight)
        raw = bind.connection.driver_connection
        definitions = {t: preflight.audit_definition(raw, t) for t in TABLES}
        policies = raw.execute("select tablename,policyname from pg_policies where schemaname='public' and tablename=any(%s)", (list(TABLES),)).fetchall()
        if not preflight.legacy_analytics_matches({"analytics_definitions": definitions, "analytics_policies": policies}):
            raise RuntimeError("Pre-created Analytics schema differs from the exact approved legacy reference")
        op.create_unique_constraint("uq_zconnect_run_company", "zconnect_sync_run_companies", ["sync_run_id", "company_id"])
        op.create_unique_constraint("uq_zconnect_fact_company", "zconnect_order_facts", ["id", "company_id"])
        op.create_foreign_key("fk_zconnect_fact_run_company", "zconnect_order_facts", "zconnect_sync_run_companies", ["sync_run_id", "company_id"], ["sync_run_id", "company_id"], ondelete="RESTRICT")
        op.drop_constraint("zconnect_order_lines_order_fact_id_fkey", "zconnect_order_lines", type_="foreignkey")
        op.create_foreign_key("fk_zconnect_line_fact_company", "zconnect_order_lines", "zconnect_order_facts", ["order_fact_id", "company_id"], ["id", "company_id"], ondelete="CASCADE")
        op.execute("INSERT INTO public.zconnect_companies(id,name) VALUES ('default','Default company') ON CONFLICT (id) DO NOTHING")
    else:
        _create_tables()
    _harden_tables()


def downgrade():
    # Removes only what upgrade() created. Captured order values are lost: export them first.
    op.drop_index("ix_zconnect_order_lines_company_product", table_name="zconnect_order_lines")
    op.drop_index("ix_zconnect_order_lines_order", table_name="zconnect_order_lines")
    op.drop_table("zconnect_order_lines")
    op.drop_index("ix_zconnect_order_facts_company_customer", table_name="zconnect_order_facts")
    op.drop_index("ix_zconnect_order_facts_company_date", table_name="zconnect_order_facts")
    op.drop_table("zconnect_order_facts")
    op.drop_index("ix_zconnect_sync_run_companies_company", table_name="zconnect_sync_run_companies")
    op.drop_table("zconnect_sync_run_companies")
    op.drop_table("zconnect_companies")
