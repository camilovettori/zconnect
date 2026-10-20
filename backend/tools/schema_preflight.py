"""Read-only schema preflight for the Zconnect PostgreSQL database (migration 0002).

Answers, before any Alembic stamp or upgrade:
  * Which database is this? (host, port, database, role; the password is never printed)
  * Does the existing schema match 0001_initial? Production tables may have been created
    by `alembic upgrade 0001_initial` or by the app's startup create_all; both reference
    schemas are bundled in tools/schema_references/ and either may match exactly.
  * Is it versioned (alembic_version)? Does export_events already exist?
  * Row counts, status distributions, configured setting NAMES (never values), RLS state
    and Supabase API-role grants on public tables.

Safety:
  * The URL is read only from the DATABASE_URL environment variable (never from a .env
    file or a default), and must be PostgreSQL.
  * The session is read-only (default_transaction_read_only=on, psycopg read_only) with a
    statement timeout; only catalogs, counts and setting names are selected.

Usage (from backend/):
  python tools/schema_preflight.py target
  python tools/schema_preflight.py compare            # against both bundled references
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
APP_TABLES = ["settings", "sync_runs", "exported_orders", "exported_invoices", "customer_mappings", "item_mappings", "zoho_item_mappings"]
SCHEMA_SECTIONS = ("columns", "constraints", "indexes")


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


def snapshot(conn: psycopg.Connection) -> Tuple[Dict[str, List[list]], Dict[str, Any]]:
    def q(sql: str, *args: Any) -> List[list]:
        return [list(row) for row in conn.execute(sql, args).fetchall()]

    def exists(table: str) -> bool:
        return bool(q("select to_regclass(%s) is not null", f"public.{table}")[0][0])

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
    info: Dict[str, Any] = {
        "server_version": q("show server_version")[0][0],
        "current_database": q("select current_database()")[0][0],
        "connected_role": q("select current_user, rolbypassrls from pg_roles where rolname = current_user")[0],
        "public_tables": [r[0] for r in q("select tablename from pg_tables where schemaname = 'public' order by 1")],
        "alembic_version": [r[0] for r in q("select version_num from alembic_version")] if exists("alembic_version") else None,
        "export_events_exists": exists("export_events"),
        "row_counts": {t: q(f'select count(*) from public."{t}"')[0][0] for t in APP_TABLES if exists(t)},
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
    if exists("settings"):
        # Names and a set/empty flag only; values are never selected.
        info["settings_keys"] = q("select key, coalesce(length(value), 0) > 0 from settings order by key")
    if exists("sync_runs"):
        info["sync_runs_status"] = q("select status, count(*), max(started_at)::text from sync_runs group by 1 order by 1")
    if exists("exported_orders"):
        info["exported_orders_status"] = q("select status, count(*) from exported_orders group by 1 order by 1")
    if exists("exported_invoices"):
        info["exported_invoices_status"] = q("select status, count(*) from exported_invoices group by 1 order by 1")
    return schema, info


def load_references(paths: Optional[List[str]] = None) -> Dict[str, Dict[str, List[list]]]:
    files = [Path(p) for p in paths] if paths else sorted(REFERENCE_DIR.glob("*.json"))
    if not files:
        raise PreflightError(f"No reference schemas found in {REFERENCE_DIR}")
    return {f.stem: json.loads(f.read_text(encoding="utf-8-sig"))["schema"] for f in files}


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


def postcheck(conn: psycopg.Connection, info: Dict[str, Any], pre_info: Dict[str, Any]) -> Dict[str, bool]:
    """Checks after the upgrade to 0002_export_events; pre_info comes from a `dump` taken just before."""
    count = lambda sql: conn.execute(sql).fetchone()[0]
    events = info["export_events_exists"]
    return {
        "alembic_version is 0002_export_events": info["alembic_version"] == ["0002_export_events"],
        "export_events table exists": events,
        "export_events has primary key + 4 indexes": events and count("select count(*) from pg_indexes where schemaname = 'public' and tablename = 'export_events'") == 5,
        "append-only trigger present": ["export_events", "export_events_no_update_delete"] in info["user_triggers"],
        "export_events is empty (no backfill)": events and count("select count(*) from export_events") == 0,
        "existing row counts unchanged": info["row_counts"] == pre_info.get("row_counts"),
        "exported_orders statuses unchanged": info.get("exported_orders_status") == pre_info.get("exported_orders_status"),
    }


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
            schema, info = snapshot(conn)
            if argv[0] == "dump":
                document = json.dumps({"target": target, "schema": schema, "info": info}, indent=1, default=str)
                if len(argv) > 1:
                    # Written here as UTF-8: PowerShell 5.1 `>` redirection would produce UTF-16.
                    Path(argv[1]).write_text(document + "\n", encoding="utf-8")
                    print(f"snapshot written to {argv[1]}", file=sys.stderr)
                else:
                    print(document)
                return 0
            if argv[0] == "postcheck":
                pre_info = json.loads(Path(argv[1]).read_text(encoding="utf-8-sig"))["info"]
                checks = postcheck(conn, info, pre_info)
                for name, ok in checks.items():
                    print(f"{'PASS' if ok else 'FAIL'}  {name}")
                passed = all(checks.values())
                print("POST-MIGRATION CHECKS PASSED" if passed else "POST-MIGRATION CHECKS FAILED: stay in maintenance and review")
                return 0 if passed else 1
        matched, differences = compare(schema, load_references(argv[1:] or None))
    except (PreflightError, psycopg.Error) as exc:
        print(f"preflight error: {exc}", file=sys.stderr)
        return 2
    for name, diffs in differences.items():
        print(f"--- against {name}: {'identical' if not diffs else f'{len(diffs)} difference(s)'}")
        for line in diffs:
            print(f"    {line}")
    print(json.dumps(info, indent=1, default=str))
    if matched:
        print(f"SCHEMA MATCHES 0001_initial ({matched})")
        return 0
    print("SCHEMA DIFFERS from every 0001_initial reference: do NOT stamp; stop and review")
    return 1


if __name__ == "__main__":
    sys.exit(main())
