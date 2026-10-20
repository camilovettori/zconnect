"""zconnect_export_events: append-only audit history of export operations

Additive only: creates one new table (plus indexes and append-only triggers). Existing
tables and rows are not modified, rewritten or backfilled.

The production database is shared with other applications, so every object created here
carries a zconnect_ prefix. An unrelated table named export_events may already exist there;
this migration never reads, alters or drops it.

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

TABLE = "zconnect_export_events"
FUNCTION = "zconnect_export_events_append_only"
PG_TRIGGER = "zconnect_export_events_no_update_delete"
SQLITE_TRIGGERS = ("zconnect_export_events_no_update", "zconnect_export_events_no_delete")
INDEXES = {
    "ix_zconnect_export_events_tenant_order": ["tenant_id", "unify_order_id", "id"],
    "ix_zconnect_export_events_tenant_created": ["tenant_id", "created_at"],
    "ix_zconnect_export_events_sync_run": ["sync_run_id"],
    "ix_zconnect_export_events_export_record": ["export_record_id"],
}


def upgrade():
    op.create_table(
        TABLE,
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
    for name, columns in INDEXES.items():
        op.create_index(name, TABLE, columns)

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            f"""
            CREATE FUNCTION {FUNCTION}() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION '{TABLE} is append-only (% rejected)', TG_OP;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            f"CREATE TRIGGER {PG_TRIGGER} BEFORE UPDATE OR DELETE ON {TABLE} "
            f"FOR EACH ROW EXECUTE FUNCTION {FUNCTION}();"
        )
    elif dialect == "sqlite":
        op.execute(
            f"CREATE TRIGGER {SQLITE_TRIGGERS[0]} BEFORE UPDATE ON {TABLE} "
            f"BEGIN SELECT RAISE(ABORT, '{TABLE} is append-only (UPDATE rejected)'); END;"
        )
        op.execute(
            f"CREATE TRIGGER {SQLITE_TRIGGERS[1]} BEFORE DELETE ON {TABLE} "
            f"BEGIN SELECT RAISE(ABORT, '{TABLE} is append-only (DELETE rejected)'); END;"
        )


def downgrade():
    # Removes only what upgrade() created. Note: this discards the audit history; run a
    # downgrade in production only under an approved plan (export the table first).
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS {PG_TRIGGER} ON {TABLE};")
        op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}();")
    elif dialect == "sqlite":
        for trigger in SQLITE_TRIGGERS:
            op.execute(f"DROP TRIGGER IF EXISTS {trigger};")
    for name in reversed(list(INDEXES)):
        op.drop_index(name, table_name=TABLE)
    op.drop_table(TABLE)
