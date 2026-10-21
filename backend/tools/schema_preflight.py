"""Read-only schema preflight for Zconnect PostgreSQL migrations 0002 and 0003.

The production database is shared with other applications (ZRecipe), so Zconnect's own
migration objects carry a zconnect_ prefix: the audit table zconnect_export_events (with
its indexes, append-only function and trigger) and the Alembic version table
zconnect_alembic_version. Objects with generic names that other applications may own
(public.export_events, public.alembic_version) are reported for information only: they
never block, and are never modified.

Answers, before any Alembic stamp or upgrade:
  * Which database is this? (host, port, database, role; the password is never printed)
  * Do Zconnect's seven original tables match 0001_initial? They may have been created by
    `alembic upgrade 0001_initial` or by the app's startup create_all; both reference
    schemas are bundled in tools/schema_references/ and either may match exactly.
  * What state are Zconnect's migration objects in? One of:
      ready_to_stamp    no Zconnect version table, no Zconnect audit objects
      ready_to_upgrade  version 0001_initial, no Zconnect audit objects
      migrated          version 0002_export_events and an audit table identical to the reference
      analytics_unversioned  exact known pre-created Analytics schema at 0002; explicit repair needed
      analytics_migrated     version 0003, exact tenant constraints, RLS and revoked API access
      incompatible      any other combination, or an audit table that differs from the reference
      schema_differs    the seven original tables match no reference
    Only the two ready states print "SCHEMA MATCHES".
  * Row counts, status distributions, configured setting NAMES (never values), RLS state
    and Supabase API-role grants on public tables.

Safety:
  * The URL is read only from the DATABASE_URL environment variable (never from a .env
    file or a default), and must be PostgreSQL.
  * The session is read-only (default_transaction_read_only=on, psycopg read_only) with a
    statement timeout; only catalogs, counts and setting names are selected.

Usage (from backend/):
  python tools/schema_preflight.py target
  python tools/schema_preflight.py compare            # against the bundled references
  python tools/schema_preflight.py dump pre_migration_snapshot.json
  python tools/schema_preflight.py postcheck pre_migration_snapshot.json
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

import psycopg

REFERENCE_DIR = Path(__file__).resolve().parent / "schema_references"
AUDIT_REFERENCE = REFERENCE_DIR / "zconnect_export_events.json"
APP_TABLES = ["settings", "sync_runs", "exported_orders", "exported_invoices", "customer_mappings", "item_mappings", "zoho_item_mappings"]
SCHEMA_SECTIONS = ("columns", "constraints", "indexes")

VERSION_TABLE = "zconnect_alembic_version"
AUDIT_TABLE = "zconnect_export_events"
AUDIT_FUNCTION = "zconnect_export_events_append_only"
BASE_REVISION = "0001_initial"
TARGET_REVISION = "0002_export_events"
ANALYTICS_REVISION = "0003_analytics"
ANALYTICS_TABLES = ["zconnect_companies", "zconnect_sync_run_companies", "zconnect_order_facts", "zconnect_order_lines"]
ANALYTICS_REFERENCE = REFERENCE_DIR / "zconnect_analytics.json"
ANALYTICS_LEGACY_REFERENCE = REFERENCE_DIR / "zconnect_analytics_legacy.json"
# Generic names other applications in the shared database may own. Informational only.
FOREIGN_TABLES = ("export_events", "alembic_version")

READY_STATES = ("ready_to_stamp", "ready_to_upgrade")
# Every table Zconnect owns: none of them may be reachable by Supabase's API roles.
ZCONNECT_TABLES = APP_TABLES + [AUDIT_TABLE, VERSION_TABLE] + ANALYTICS_TABLES
API_ROLES = ("anon", "authenticated")


class PreflightError(RuntimeError):
    pass


def target_from_env() -> Tuple[str, Dict[str, Any]]:
    """Return (libpq URL, description) from the DATABASE_URL environment variable only."""
    raw = os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        raise PreflightError("DATABASE_URL is not set in the environment; refusing to guess a target.")
    for prefix in ("postgresql+psycopg://", "postgres://"):
        if raw.startswith(prefix):
            raw = "postgresql://" + raw[len(prefix):]
    if not raw.startswith("postgresql://"):
        raise PreflightError("DATABASE_URL must be a PostgreSQL URL.")
    parts = urlsplit(raw)
    description = {
        "host": parts.hostname,
        "port": parts.port or 5432,
        "database": unquote(parts.path.lstrip("/")) or None,
        "user": unquote(parts.username) if parts.username else None,
    }
    return raw, description


def connect_read_only(url: str) -> psycopg.Connection:
    conn = psycopg.connect(url, options="-c default_transaction_read_only=on -c statement_timeout=15000", connect_timeout=10)
    conn.read_only = True
    return conn


def _query(conn: psycopg.Connection):
    def q(sql: str, *args: Any) -> List[list]:
        return [list(row) for row in conn.execute(sql, args).fetchall()]

    return q


def _exists(q, table: str) -> bool:
    return bool(q("select to_regclass(%s) is not null", f"public.{table}")[0][0])


def audit_definition(conn: psycopg.Connection, table: str = AUDIT_TABLE) -> Optional[Dict[str, List[list]]]:
    """Full definition of zconnect_export_events (None if absent): compared exactly, never by counts."""
    q = _query(conn)
    if not _exists(q, table):
        return None
    precision_columns = ", numeric_precision, numeric_scale, datetime_precision" if table in ANALYTICS_TABLES else ""
    return {
        "columns": q(
            f"""select column_name, data_type, character_maximum_length, is_nullable,
                      regexp_replace(coalesce(column_default, ''), '::regclass', ''), is_identity{precision_columns}
               from information_schema.columns
               where table_schema = 'public' and table_name = %s
               order by column_name""",
            table,
        ),
        "constraints": q(
            "select conname, contype, pg_get_constraintdef(oid) from pg_constraint where conrelid = %s::regclass order by 1",
            f"public.{table}",
        ),
        "indexes": q("select indexname, indexdef from pg_indexes where schemaname = 'public' and tablename = %s order by 1", table),
        "triggers": q(
            "select tgname, pg_get_triggerdef(oid) from pg_trigger where tgrelid = %s::regclass and not tgisinternal order by 1",
            f"public.{table}",
        ),
    }


def snapshot(conn: psycopg.Connection) -> Tuple[Dict[str, List[list]], Dict[str, Any]]:
    q = _query(conn)
    schema = {
        "columns": q(
            """select table_name, column_name, data_type, character_maximum_length, is_nullable,
                      regexp_replace(coalesce(column_default, ''), '::regclass', '')
               from information_schema.columns
               where table_schema = 'public' and table_name = any(%s)
               order by table_name, column_name""",
            APP_TABLES,
        ),
        "constraints": q(
            """select conrelid::regclass::text, contype, pg_get_constraintdef(oid)
               from pg_constraint
               where connamespace = 'public'::regnamespace and conrelid::regclass::text = any(%s)
               order by 1, 2, 3""",
            APP_TABLES,
        ),
        "indexes": q(
            """select tablename, regexp_replace(indexdef, 'INDEX \\S+ ON', 'INDEX <name> ON')
               from pg_indexes where schemaname = 'public' and tablename = any(%s)
               order by 1, 2""",
            APP_TABLES,
        ),
    }
    foreign: Dict[str, Any] = {}
    for table in FOREIGN_TABLES:
        present = _exists(q, table)
        foreign[f"public.{table}"] = {
            "exists": present,
            "columns": q("select count(*) from information_schema.columns where table_schema = 'public' and table_name = %s", table)[0][0] if present else None,
            "rows": q(f'select count(*) from public."{table}"')[0][0] if present else None,
            "indexes": q("select count(*) from pg_indexes where schemaname = 'public' and tablename = %s", table)[0][0] if present else None,
            "custom_triggers": q("select count(*) from pg_trigger where tgrelid = %s::regclass and not tgisinternal", f"public.{table}")[0][0] if present else None,
        }
    info: Dict[str, Any] = {
        "server_version": q("show server_version")[0][0],
        "current_database": q("select current_database()")[0][0],
        "connected_role": q("select current_user, rolbypassrls from pg_roles where rolname = current_user")[0],
        "public_tables": [r[0] for r in q("select tablename from pg_tables where schemaname = 'public' order by 1")],
        "zconnect_alembic_version": [r[0] for r in q(f'select version_num from public."{VERSION_TABLE}"')] if _exists(q, VERSION_TABLE) else None,
        "zconnect_export_events_exists": _exists(q, AUDIT_TABLE),
        "zconnect_append_only_function_exists": bool(q("select count(*) from pg_proc where proname = %s", AUDIT_FUNCTION)[0][0]),
        "foreign_objects": foreign,
        "row_counts": {t: q(f'select count(*) from public."{t}"')[0][0] for t in APP_TABLES if _exists(q, t)},
        "user_triggers": q("select tgrelid::regclass::text, tgname from pg_trigger where not tgisinternal order by 1, 2"),
        # Supabase exposes the public schema through its Data API to these roles; without
        # row-level security a grant here means the anon key can read/write the table.
        "rls_disabled_tables": [r[0] for r in q(
            """select relname from pg_class
               where relnamespace = 'public'::regnamespace and relkind = 'r' and not relrowsecurity
               order by 1""")],
        "api_role_grants": q(
            """select grantee, table_name, string_agg(privilege_type, ',' order by privilege_type)
               from information_schema.role_table_grants
               where table_schema = 'public' and grantee in ('anon', 'authenticated')
               group by 1, 2 order by 1, 2"""),
    }
    # Zconnect tables reachable by Supabase API roles (warning only; never changed here).
    info["zconnect_api_exposure"] = q(
        """select table_name, grantee, string_agg(privilege_type, ',' order by privilege_type)
           from information_schema.role_table_grants
           where table_schema = 'public' and table_name = any(%s) and grantee = any(%s)
           group by 1, 2 order by 1, 2""",
        ZCONNECT_TABLES,
        list(API_ROLES),
    )
    info["zconnect_tables_without_rls"] = [t for t in info["rls_disabled_tables"] if t in ZCONNECT_TABLES]
    info["analytics_definitions"] = {t: audit_definition(conn, t) for t in ANALYTICS_TABLES}
    info["analytics_policies"] = q("select tablename, policyname from pg_policies where schemaname='public' and tablename=any(%s) order by 1,2", ANALYTICS_TABLES)
    # Include PUBLIC and inherited privileges, not just direct role grants.
    info["analytics_api_access"] = q(
        """select c.relname, r.rolname from pg_class c cross join pg_roles r
           where c.relnamespace='public'::regnamespace and c.relname=any(%s)
           and r.rolname in ('anon','authenticated')
           and has_table_privilege(r.oid,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') order by 1,2""", ANALYTICS_TABLES)
    info["analytics_public_grants"] = q(
        """select c.relname, a.privilege_type from pg_class c
           cross join lateral aclexplode(coalesce(c.relacl,acldefault(case when c.relkind='S' then 's'::"char" else 'r'::"char" end,c.relowner))) a
           where c.relnamespace='public'::regnamespace and c.relname=any(%s) and a.grantee=0 order by 1,2""",
        ANALYTICS_TABLES + ["zconnect_order_facts_id_seq", "zconnect_order_lines_id_seq"])
    info["analytics_sequence_access"] = q(
        """select c.relname,r.rolname from pg_class c cross join pg_roles r
           where c.relnamespace='public'::regnamespace and c.relkind='S'
           and c.relname=any(%s) and r.rolname in ('anon','authenticated')
           and has_sequence_privilege(r.oid,c.oid,'USAGE,SELECT,UPDATE') order by 1,2""",
        ["zconnect_order_facts_id_seq", "zconnect_order_lines_id_seq"])
    info["analytics_row_counts"] = {t: q(f'select count(*) from public."{t}"')[0][0] for t in ANALYTICS_TABLES if _exists(q,t)}
    info["analytics_default_company_exists"] = bool(q("select exists(select 1 from public.zconnect_companies where id='default')")[0][0]) if _exists(q, "zconnect_companies") else False
    if _exists(q, AUDIT_TABLE):
        info["audit_row_count"] = q(f'select count(*) from public."{AUDIT_TABLE}"')[0][0]
    if _exists(q, "settings"):
        # Names and a set/empty flag only; values are never selected.
        info["settings_keys"] = q("select key, coalesce(length(value), 0) > 0 from settings order by key")
    if _exists(q, "sync_runs"):
        info["sync_runs_status"] = q("select status, count(*), max(started_at)::text from sync_runs group by 1 order by 1")
    if _exists(q, "exported_orders"):
        info["exported_orders_status"] = q("select status, count(*) from exported_orders group by 1 order by 1")
    if _exists(q, "exported_invoices"):
        info["exported_invoices_status"] = q("select status, count(*) from exported_invoices group by 1 order by 1")
    return schema, info


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_references(paths: Optional[List[str]] = None) -> Dict[str, Dict[str, List[list]]]:
    files = [Path(p) for p in paths] if paths else sorted(REFERENCE_DIR.glob("reference_*.json"))
    if not files:
        raise PreflightError(f"No reference schemas found in {REFERENCE_DIR}")
    return {f.stem: _read_json(f)["schema"] for f in files}


def load_audit_reference() -> Dict[str, List[list]]:
    return _read_json(AUDIT_REFERENCE)["definition"]


def analytics_differences(info: Dict[str, Any]) -> List[str]:
    reference = _read_json(ANALYTICS_REFERENCE)["definitions"]
    diffs = []
    for table in ANALYTICS_TABLES:
        diffs.extend(f"{table}: {d}" for d in audit_differences(info.get("analytics_definitions", {}).get(table), reference[table]))
    if set(ANALYTICS_TABLES) & set(info.get("rls_disabled_tables", [])):
        diffs.append("Analytics RLS is disabled")
    for key in ("analytics_api_access", "analytics_public_grants", "analytics_sequence_access", "analytics_policies"):
        if info.get(key):
            diffs.append(f"Unsafe analytics access: {key}")
    return diffs


def legacy_analytics_matches(info: Dict[str, Any]) -> bool:
    """Only the exact known pre-migration model schema is eligible for explicit repair."""
    definitions = info.get("analytics_definitions", {})
    if info.get("analytics_policies") or not all(definitions.get(t) for t in ANALYTICS_TABLES):
        return False
    reference = _read_json(ANALYTICS_LEGACY_REFERENCE)["definitions"]
    return all(not audit_differences(definitions[t], reference[t]) for t in ANALYTICS_TABLES)


def compare(schema: Dict[str, List[list]], references: Dict[str, Dict[str, List[list]]]) -> Tuple[Optional[str], Dict[str, List[str]]]:
    """Return (name of the first reference matched exactly, differences per reference)."""
    differences: Dict[str, List[str]] = {}
    matched = None
    for name, reference in references.items():
        diffs: List[str] = []
        for section in SCHEMA_SECTIONS:
            have = {json.dumps(r) for r in schema[section]}
            want = {json.dumps(r) for r in reference[section]}
            diffs += [f"MISSING {section}: {r}" for r in sorted(want - have)]
            diffs += [f"EXTRA   {section}: {r}" for r in sorted(have - want)]
        differences[name] = diffs
        if not diffs and matched is None:
            matched = name
    return matched, differences


def audit_differences(definition: Optional[Dict[str, List[list]]], reference: Dict[str, List[list]]) -> List[str]:
    if definition is None:
        return [f"{AUDIT_TABLE} is missing"]
    diffs: List[str] = []
    for section, want_rows in reference.items():
        have = {json.dumps(r) for r in definition.get(section, [])}
        want = {json.dumps(r) for r in want_rows}
        diffs += [f"MISSING {section}: {r}" for r in sorted(want - have)]
        diffs += [f"EXTRA   {section}: {r}" for r in sorted(have - want)]
    return diffs


def assess(matched: Optional[str], info: Dict[str, Any], audit_def: Optional[Dict[str, List[list]]], audit_reference: Dict[str, List[list]]) -> Dict[str, Any]:
    """Classify the database. Foreign objects (public.export_events, public.alembic_version) never count."""
    version = info["zconnect_alembic_version"]
    table = info["zconnect_export_events_exists"]
    function = info["zconnect_append_only_function_exists"]
    analytics_present = any(info.get("analytics_definitions", {}).values())
    problems: List[str] = []
    if matched is None:
        state = "schema_differs"
        problems.append("Zconnect's original tables match no 0001_initial reference")
    elif version is None and not table and not function and not analytics_present:
        state = "ready_to_stamp"
    elif version == [BASE_REVISION] and not table and not function and not analytics_present:
        state = "ready_to_upgrade"
    elif version == [TARGET_REVISION] and table and function and not analytics_present and not audit_differences(audit_def, audit_reference):
        state = "migrated"
    elif version == [TARGET_REVISION] and table and function and not audit_differences(audit_def, audit_reference) and legacy_analytics_matches(info):
        state = "analytics_unversioned"
        problems.append("Known pre-created Analytics tables require explicitly approved repair before 0003; do not stamp them")
    elif version == [ANALYTICS_REVISION] and table and function and not audit_differences(audit_def, audit_reference) and not analytics_differences(info):
        state = "analytics_migrated"
    else:
        state = "incompatible"
        problems.append(f"{VERSION_TABLE}={version}, {AUDIT_TABLE} exists={table}, {AUDIT_FUNCTION}() exists={function}")
        if table:
            problems += [f"{AUDIT_TABLE} {d}" for d in audit_differences(audit_def, audit_reference)]
        if analytics_present or version == [ANALYTICS_REVISION]:
            problems += analytics_differences(info)
    verdicts = {
        "ready_to_stamp": f"SCHEMA MATCHES 0001_initial ({matched}): unversioned, ready to stamp",
        "ready_to_upgrade": f"SCHEMA MATCHES 0001_initial ({matched}): at {BASE_REVISION}, ready to upgrade",
        "migrated": f"ALREADY MIGRATED to {TARGET_REVISION}: nothing to do",
        "analytics_migrated": f"ALREADY MIGRATED to {ANALYTICS_REVISION}: Analytics schema and API protections verified",
        "analytics_unversioned": "ANALYTICS REPAIR REQUIRED: known pre-created tables at 0002; explicit --repair-existing-analytics required",
        "incompatible": "INCOMPATIBLE ZCONNECT OBJECTS PRESENT: do NOT stamp or upgrade; stop and review",
        "schema_differs": "SCHEMA DIFFERS from every 0001_initial reference: do NOT stamp; stop and review",
    }
    return {"state": state, "matched": matched, "problems": problems, "verdict": verdicts[state]}


def inspect_database(conn: psycopg.Connection, reference_paths: Optional[List[str]] = None):
    """Snapshot + classification in one read-only pass: (schema, info, differences, assessment)."""
    schema, info = snapshot(conn)
    matched, differences = compare(schema, load_references(reference_paths))
    assessment = assess(matched, info, audit_definition(conn), load_audit_reference())
    return schema, info, differences, assessment


def postcheck(conn: psycopg.Connection, info: Dict[str, Any], pre_info: Dict[str, Any]) -> Dict[str, bool]:
    """Checks after the upgrade to 0002_export_events; pre_info comes from a `dump` taken just before."""
    definition = audit_definition(conn)
    empty = definition is not None and conn.execute(f'select count(*) from public."{AUDIT_TABLE}"').fetchone()[0] == 0
    analytics = info["zconnect_alembic_version"] == [ANALYTICS_REVISION]
    checks = {
        f"{VERSION_TABLE} is {ANALYTICS_REVISION if analytics else TARGET_REVISION}": info["zconnect_alembic_version"] == [ANALYTICS_REVISION if analytics else TARGET_REVISION],
        f"{AUDIT_TABLE} matches the bundled definition exactly (columns, constraints, indexes, append-only trigger)": definition is not None
        and not audit_differences(definition, load_audit_reference()),
        f"{AUDIT_TABLE} has no migration backfill": (conn.execute(f'select count(*) from public."{AUDIT_TABLE}"').fetchone()[0] == pre_info["audit_row_count"]) if "audit_row_count" in pre_info else empty,
        "existing row counts unchanged": info["row_counts"] == pre_info.get("row_counts"),
        "exported_orders statuses unchanged": info.get("exported_orders_status") == pre_info.get("exported_orders_status"),
        "foreign objects (public.export_events, public.alembic_version) unchanged": info["foreign_objects"] == pre_info.get("foreign_objects"),
    }
    if analytics:
        checks["Analytics schema, tenant constraints, RLS and API privileges match"] = not analytics_differences(info)
        expected_counts = pre_info.get("analytics_row_counts") or {t: int(t == "zconnect_companies") for t in ANALYTICS_TABLES}
        if pre_info.get("analytics_row_counts") and not pre_info.get("analytics_default_company_exists", False):
            expected_counts = {**expected_counts, "zconnect_companies": expected_counts["zconnect_companies"] + 1}
        checks["Analytics rows preserved; default company exists"] = info.get("analytics_row_counts") == expected_counts and info.get("analytics_default_company_exists", False)
    return checks


def _print_foreign(info: Dict[str, Any]) -> None:
    for name, details in info["foreign_objects"].items():
        if details["exists"]:
            print(
                f"INFO: {name} exists ({details['columns']} columns, {details['rows']} rows, {details['indexes']} indexes, "
                f"{details['custom_triggers']} custom triggers). Not a Zconnect object: ignored and never modified."
            )


def _print_exposure(info: Dict[str, Any]) -> None:
    for table, grantee, privileges in info.get("zconnect_api_exposure", []):
        print(f"WARNING: Zconnect table public.{table} is accessible to {grantee} ({privileges}) through the Supabase Data API grants")
    if info.get("zconnect_tables_without_rls"):
        print(f"WARNING: row-level security is disabled on Zconnect tables: {', '.join(info['zconnect_tables_without_rls'])}")


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("target", "dump", "compare", "postcheck") or (argv[0] == "postcheck" and len(argv) != 2):
        print(__doc__, file=sys.stderr)
        return 2
    try:
        url, target = target_from_env()
        print(f"target: host={target['host']} port={target['port']} database={target['database']} user={target['user']}", file=sys.stderr)
        if argv[0] == "target":
            return 0
        with connect_read_only(url) as conn:
            if argv[0] == "postcheck":
                _schema, info = snapshot(conn)
                pre_document = _read_json(Path(argv[1]))
                pre_info = pre_document["info"]
                checks = postcheck(conn, info, pre_info)
                checks["original seven-table schema unchanged"] = _schema == pre_document["schema"]
                for name, ok in checks.items():
                    print(f"{'PASS' if ok else 'FAIL'}  {name}")
                _print_exposure(info)  # new tables inherit default grants: protect them before go-live
                passed = all(checks.values())
                print("POST-MIGRATION CHECKS PASSED" if passed else "POST-MIGRATION CHECKS FAILED: stay in maintenance and review")
                return 0 if passed else 1
            schema, info, differences, assessment = inspect_database(conn, argv[1:] if argv[0] == "compare" else None)
    except (PreflightError, psycopg.Error) as exc:
        print(f"preflight error: {exc}", file=sys.stderr)
        return 2
    if argv[0] == "dump":
        document = json.dumps({"target": target, "assessment": assessment, "schema": schema, "info": info}, indent=1, default=str)
        if len(argv) > 1:
            # Written here as UTF-8: PowerShell 5.1 `>` redirection would produce UTF-16.
            Path(argv[1]).write_text(document + "\n", encoding="utf-8")
            print(f"snapshot written to {argv[1]}", file=sys.stderr)
        else:
            print(document)
        return 0
    for name, diffs in differences.items():
        print(f"--- against {name}: {'identical' if not diffs else f'{len(diffs)} difference(s)'}")
        for line in diffs:
            print(f"    {line}")
    print(json.dumps(info, indent=1, default=str))
    _print_foreign(info)
    _print_exposure(info)
    for problem in assessment["problems"]:
        print(f"PROBLEM: {problem}")
    print(assessment["verdict"])
    return 0 if assessment["state"] in READY_STATES + ("migrated", "analytics_migrated") else 1


if __name__ == "__main__":
    sys.exit(main())
