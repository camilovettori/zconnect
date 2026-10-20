"""export_events: append-only audit history of export operations

Additive only: creates one new table (plus indexes and append-only triggers). Existing
tables and rows are not modified, rewritten or backfilled.

Revision ID: 0002_export_events
Revises: 0001_initial
Create Date: 2026-10-19 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0002_export_events"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "export_events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("unify_order_id", sa.String(length=128), nullable=False),
        sa.Column("export_record_id", sa.Integer(), sa.ForeignKey("exported_orders.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("sync_run_id", sa.Integer(), sa.ForeignKey("sync_runs.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("previous_status", sa.String(length=32), nullable=True),
        sa.Column("new_status", sa.String(length=32), nullable=True),
        sa.Column("zoho_invoice_id", sa.String(length=128), nullable=True),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_export_events_tenant_order", "export_events", ["tenant_id", "unify_order_id", "id"])
    op.create_index("ix_export_events_tenant_created", "export_events", ["tenant_id", "created_at"])
    op.create_index("ix_export_events_sync_run", "export_events", ["sync_run_id"])
    op.create_index("ix_export_events_export_record", "export_events", ["export_record_id"])

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE FUNCTION export_events_append_only() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'export_events is append-only (% rejected)', TG_OP;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            "CREATE TRIGGER export_events_no_update_delete BEFORE UPDATE OR DELETE ON export_events "
            "FOR EACH ROW EXECUTE FUNCTION export_events_append_only();"
        )
    elif dialect == "sqlite":
        op.execute(
            "CREATE TRIGGER export_events_no_update BEFORE UPDATE ON export_events "
            "BEGIN SELECT RAISE(ABORT, 'export_events is append-only (UPDATE rejected)'); END;"
        )
        op.execute(
            "CREATE TRIGGER export_events_no_delete BEFORE DELETE ON export_events "
            "BEGIN SELECT RAISE(ABORT, 'export_events is append-only (DELETE rejected)'); END;"
        )


def downgrade():
    # Removes only what upgrade() created. Note: this discards the audit history; run a
    # downgrade in production only under an approved plan (export the table first).
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS export_events_no_update_delete ON export_events;")
        op.execute("DROP FUNCTION IF EXISTS export_events_append_only();")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS export_events_no_update;")
        op.execute("DROP TRIGGER IF EXISTS export_events_no_delete;")
    op.drop_index("ix_export_events_export_record", table_name="export_events")
    op.drop_index("ix_export_events_sync_run", table_name="export_events")
    op.drop_index("ix_export_events_tenant_created", table_name="export_events")
    op.drop_index("ix_export_events_tenant_order", table_name="export_events")
    op.drop_table("export_events")
