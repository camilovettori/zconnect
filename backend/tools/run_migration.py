"""Guarded runner for the 0002_export_events release migration.

Wraps `alembic stamp 0001_initial` and `alembic upgrade 0002_export_events` with the checks
the release runbook requires, so a typo or a stray .env cannot migrate the wrong database:

  * The URL comes only from the DATABASE_URL environment variable (never .env, never the
    SQLite fallbacks in app/config.py or alembic.ini), and must be PostgreSQL.
  * --expected-host and --expected-database must match that URL (and --expected-user, if
    given); otherwise nothing is done.
  * `status` is read-only. `stamp` and `upgrade` also require --backup-verified.
  * stamp: only on an unversioned database (no alembic_version), without export_events,
    whose schema matches a bundled 0001_initial reference.
  * upgrade: only from exactly 0001_initial, without export_events, with a matching
    schema; upgrades to 0002_export_events (not "head"), so later migrations are never
    applied by accident. PostgreSQL runs the DDL in one transaction.

Usage (from backend/):
  python tools/run_migration.py --expected-host H --expected-database postgres status
  python tools/run_migration.py --expected-host H --expected-database postgres --backup-verified stamp
  python tools/run_migration.py --expected-host H --expected-database postgres --backup-verified upgrade
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import List, Optional

BACKEND_DIR = Path(__file__).resolve().parents[1]
BASE_REVISION = "0001_initial"
TARGET_REVISION = "0002_export_events"

_spec = importlib.util.spec_from_file_location("schema_preflight", Path(__file__).resolve().parent / "schema_preflight.py")
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)


class MigrationRefused(RuntimeError):
    pass


def _inspect(url: str):
    with preflight.connect_read_only(url) as conn:
        schema, info = preflight.snapshot(conn)
    matched, _differences = preflight.compare(schema, preflight.load_references())
    return info, matched


def _alembic_config(url: str):
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from alembic.config import Config
    from app.config import normalize_database_url, settings

    sqlalchemy_url = "postgresql+psycopg://" + url[len("postgresql://"):]
    # alembic/env.py takes its URL from the app settings; refuse unless they resolve to
    # exactly the verified target (environment variables take precedence over .env).
    if normalize_database_url(settings.DATABASE_URL) != normalize_database_url(sqlalchemy_url):
        raise MigrationRefused("app settings resolve to a different DATABASE_URL than the verified target")
    config = Config()  # no ini file: no logging reconfiguration, no sqlite fallback URL
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", sqlalchemy_url)
    return config


def run(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Guarded 0002_export_events migration runner.")
    parser.add_argument("--expected-host", required=True)
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--expected-user")
    parser.add_argument("--backup-verified", action="store_true", help="Confirms a restore-tested backup was taken just now")
    parser.add_argument("action", choices=["status", "stamp", "upgrade"])
    args = parser.parse_args(argv)

    try:
        url, target = preflight.target_from_env()
        print(f"target: host={target['host']} port={target['port']} database={target['database']} user={target['user']}")
        expected = {"host": args.expected_host, "database": args.expected_database}
        if args.expected_user:
            expected["user"] = args.expected_user
        for key, value in expected.items():
            if (target[key] or "") != value.strip():
                raise MigrationRefused(f"{key} {target[key]!r} does not match --expected-{key} {value!r}")

        info, matched = _inspect(url)
        print(f"alembic_version={info['alembic_version']} export_events_exists={info['export_events_exists']} schema_reference={matched}")
        print(f"row_counts={info['row_counts']}")
        if args.action == "status":
            return 0
        if not args.backup_verified:
            raise MigrationRefused("--backup-verified is required for stamp and upgrade")
        if matched is None:
            raise MigrationRefused("schema does not match a 0001_initial reference (run schema_preflight.py compare)")
        if info["export_events_exists"]:
            raise MigrationRefused("export_events already exists")

        from alembic import command

        if args.action == "stamp":
            if info["alembic_version"] is not None:
                raise MigrationRefused(f"database is already versioned ({info['alembic_version']}); do not stamp")
            command.stamp(_alembic_config(url), BASE_REVISION)
        else:
            if info["alembic_version"] != [BASE_REVISION]:
                raise MigrationRefused(f"upgrade requires alembic_version {BASE_REVISION}, found {info['alembic_version']}")
            command.upgrade(_alembic_config(url), TARGET_REVISION)

        after, _matched = _inspect(url)
        print(f"done: alembic_version={after['alembic_version']} export_events_exists={after['export_events_exists']}")
        return 0
    except (MigrationRefused, preflight.PreflightError) as exc:
        print(f"REFUSED: {str(exc).rstrip('.')}. Nothing was changed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(run())
