"""Release gates exercised against disposable local PostgreSQL, never production."""
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from app import models

from tests.test_schema_preflight import (
    scratch_db, target_env, needs_postgres, create_like_startup,
    add_foreign_application_objects, foreign_definition, run_status, inspect,
    execute, preflight,
)


def migrate(url, target_env):
    create_like_startup(url)
    add_foreign_application_objects(url)
    target_env(url)
    assert run_status(url, "--backup-verified", "stamp") == 0
    assert run_status(url, "--backup-verified", "upgrade") == 0


def precreated_analytics(url):
    engine = create_engine(url)
    models.Base.metadata.create_all(engine, tables=[models.Base.metadata.tables[t] for t in models.ANALYTICS_TABLES])
    engine.dispose()
    execute(url,
            "ALTER TABLE zconnect_order_lines DROP CONSTRAINT fk_zconnect_line_fact_company",
            "ALTER TABLE zconnect_order_facts DROP CONSTRAINT fk_zconnect_fact_run_company",
            "ALTER TABLE zconnect_order_facts DROP CONSTRAINT uq_zconnect_fact_company",
            "ALTER TABLE zconnect_sync_run_companies DROP CONSTRAINT uq_zconnect_run_company",
            "ALTER TABLE zconnect_order_lines ADD FOREIGN KEY(order_fact_id) REFERENCES zconnect_order_facts(id) ON DELETE CASCADE")


@needs_postgres
def test_precreated_analytics_require_explicit_repair_and_preserve_existing_rows(scratch_db, target_env):
    migrate(scratch_db, target_env)
    precreated_analytics(scratch_db)
    execute(scratch_db, "INSERT INTO zconnect_companies(id,name) VALUES ('default','Default company'),('other','Other')")
    before = foreign_definition(scratch_db)
    assert inspect(scratch_db)[0]["state"] == "analytics_unversioned"
    assert run_status(scratch_db, "--target", "0003_analytics", "--backup-verified", "upgrade") == 2
    with preflight.connect_read_only(scratch_db.replace("postgresql+psycopg://", "postgresql://")) as conn:
        _, baseline = preflight.snapshot(conn)
    assert run_status(scratch_db, "--target", "0003_analytics", "--backup-verified", "--repair-existing-analytics", "upgrade") == 0
    assessment, info = inspect(scratch_db)
    assert assessment["state"] == "analytics_migrated"
    assert info["analytics_row_counts"]["zconnect_companies"] == 2
    assert foreign_definition(scratch_db) == before
    with preflight.connect_read_only(scratch_db.replace("postgresql+psycopg://", "postgresql://")) as conn:
        assert all(preflight.postcheck(conn, info, baseline).values())


@needs_postgres
@pytest.mark.parametrize("drift", ["ALTER TABLE zconnect_companies ADD COLUMN unknown_extra text", "CREATE POLICY unknown_access ON zconnect_companies USING (true)"])
def test_precreated_analytics_with_unknown_drift_cannot_be_adopted(scratch_db, target_env, drift):
    migrate(scratch_db, target_env)
    precreated_analytics(scratch_db)
    execute(scratch_db, drift)
    assert inspect(scratch_db)[0]["state"] == "incompatible"
    assert run_status(scratch_db, "--target", "0003_analytics", "--backup-verified", "--repair-existing-analytics", "upgrade") == 2


@needs_postgres
def test_analytics_guarded_upgrade_preserves_shared_database_and_protects_api(scratch_db, target_env):
    migrate(scratch_db, target_env)
    before = foreign_definition(scratch_db)
    with preflight.connect_read_only(scratch_db.replace("postgresql+psycopg://", "postgresql://")) as conn:
        _, baseline = preflight.snapshot(conn)
    assert run_status(scratch_db, "--target", "0003_analytics", "upgrade") == 2
    assert run_status(scratch_db, "--target", "0003_analytics", "--backup-verified", "upgrade") == 0
    assessment, info = inspect(scratch_db)
    assert assessment["state"] == "analytics_migrated"
    assert info["analytics_row_counts"] == {t: int(t == "zconnect_companies") for t in preflight.ANALYTICS_TABLES}
    assert info["analytics_api_access"] == info["analytics_public_grants"] == info["analytics_sequence_access"] == []
    assert not set(preflight.ANALYTICS_TABLES) & set(info["rls_disabled_tables"])
    assert foreign_definition(scratch_db) == before
    with preflight.connect_read_only(scratch_db.replace("postgresql+psycopg://", "postgresql://")) as conn:
        assert all(preflight.postcheck(conn, info, baseline).values())
    assert run_status(scratch_db, "--target", "0003_analytics", "--backup-verified", "upgrade") == 2


@needs_postgres
@pytest.mark.parametrize("tamper", [
    "ALTER TABLE zconnect_order_lines DISABLE ROW LEVEL SECURITY",
    "GRANT SELECT ON zconnect_companies TO PUBLIC",
    "GRANT USAGE ON SEQUENCE zconnect_order_lines_id_seq TO PUBLIC",
    "ALTER TABLE zconnect_order_facts ALTER COLUMN net_amount TYPE numeric(15,2)",
    "ALTER TABLE zconnect_order_lines DROP CONSTRAINT fk_zconnect_line_fact_company",
])
def test_analytics_preflight_rejects_schema_or_security_drift(scratch_db, target_env, tamper):
    migrate(scratch_db, target_env)
    assert run_status(scratch_db, "--target", "0003_analytics", "--backup-verified", "upgrade") == 0
    execute(scratch_db, tamper)
    assert inspect(scratch_db)[0]["state"] == "incompatible"


@needs_postgres
def test_database_rejects_cross_company_fact_relationships(scratch_db, target_env):
    migrate(scratch_db, target_env)
    assert run_status(scratch_db, "--target", "0003_analytics", "--backup-verified", "upgrade") == 0
    execute(scratch_db, "INSERT INTO zconnect_companies(id,name) VALUES ('other','Other')",
            "INSERT INTO sync_runs(id,date_from,date_to,status) VALUES (1,'2026-10-01','2026-10-02','success')",
            "INSERT INTO zconnect_sync_run_companies(sync_run_id,company_id) VALUES (1,'default')")
    engine = create_engine(scratch_db)
    values = "'O1', 'C1', '2026-10-01', 'EUR', 10, 1, 'invoice_created'"
    try:
        with pytest.raises(IntegrityError, match="fk_zconnect_fact_run_company"):
            with engine.begin() as conn:
                conn.execute(text(f"INSERT INTO zconnect_order_facts(company_id,sync_run_id,unify_order_id,customer_key,invoice_date,currency,net_amount,line_count,source) VALUES ('other',1,{values})"))
        with engine.begin() as conn:
            fact_id = conn.execute(text(f"INSERT INTO zconnect_order_facts(company_id,unify_order_id,customer_key,invoice_date,currency,net_amount,line_count,source) VALUES ('default',{values}) RETURNING id")).scalar_one()
        with pytest.raises(IntegrityError, match="fk_zconnect_line_fact_company"):
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO zconnect_order_lines(order_fact_id,company_id,product_key,product_name,quantity,unit_price,net_amount) VALUES (:id,'other','P1','Product',1,10,10)"), {"id": fact_id})
    finally:
        engine.dispose()


@needs_postgres
def test_default_api_grants_revoked_and_rls_denies_even_accidental_select(scratch_db, target_env):
    migrate(scratch_db, target_env)
    execute(scratch_db,
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN CREATE ROLE anon NOLOGIN; END IF; IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF; END $$",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon,authenticated,PUBLIC",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO anon,authenticated,PUBLIC")
    assert run_status(scratch_db, "--target", "0003_analytics", "--backup-verified", "upgrade") == 0
    assert inspect(scratch_db)[0]["state"] == "analytics_migrated"
    engine = create_engine(scratch_db)
    try:
        for role in ("anon", "authenticated"):
            with engine.begin() as conn:
                conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
                conn.execute(text(f"GRANT SELECT ON zconnect_companies TO {role}"))
                conn.execute(text(f"SET LOCAL ROLE {role}"))
                assert conn.execute(text("SELECT * FROM zconnect_companies")).all() == []
    finally:
        engine.dispose()
