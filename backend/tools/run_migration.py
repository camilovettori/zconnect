"""Guarded runner for the 0002_export_events release migration.

Wraps `alembic stamp 0001_initial` and `alembic upgrade 0002_export_events` with the checks
the release runbook requires, so a typo or a stray .env cannot migrate the wrong database:

  * The URL comes only from the DATABASE_URL environment variable (never .env, never the
    SQLite fallbacks in app/config.py or alembic.ini), and must be PostgreSQL.
  * --expected-host and --expected-database must match that URL (and --expected-user, if
    given); otherwise nothing is done.
  * `status` is read-only. `stamp` and `upgrade` also require --backup-verified.
  * The database state comes from tools/schema_preflight.py (one shared classification):
      stamp   only in state ready_to_stamp   (no zconnect_alembic_version, no Zconnect audit objects,
                                              original tables matching a 0001_initial reference)
      upgrade only in state ready_to_upgrade (zconnect_alembic_version = 0001_initial)
    upgrade targets 0002_export_events (not "head"), so later migrations are never applied by
    accident. PostgreSQL runs the DDL in one transaction.
  * Objects other applications may own in the shared database (public.export_events,
    public.alembic_version) are reported for information and never touched: Zconnect uses
    zconnect_export_events and zconnect_alembic_version.

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
from typing import Any, Dict, List, Optional, Tuple

BACKEND_DIR = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("schema_preflight", Path(__file__).resolve().parent / "schema_preflight.py")
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)

REQUIRED_STATE = {"stamp": "ready_to_stamp", "upgrade": "ready_to_upgrade"}


class MigrationRefused(RuntimeError):
    pass


def _inspect(url: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    with preflight.connect_read_only(url) as conn:
        _schema, info, _differences, assessment = preflight.inspect_database(conn)
    return info, assessment


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


def _report(info: Dict[str, Any], assessment: Dict[str, Any]) -> None:
    print(
        f"state={assessment['state']} {preflight.VERSION_TABLE}={info['zconnect_alembic_version']} "
        f"{preflight.AUDIT_TABLE}_exists={info['zconnect_export_events_exists']} schema_reference={assessment['matched']}"
    )
    print(f"row_counts={info['row_counts']}")
    for name, details in info["foreign_objects"].items():
        if details["exists"]:
            print(f"info: {name} exists ({details['columns']} columns, {details['rows']} rows): not a Zconnect object, ignored")
    for problem in assessment["problems"]:
        print(f"problem: {problem}")


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

        info, assessment = _inspect(url)
        _report(info, assessment)
        if args.action == "status":
            return 0
        if not args.backup_verified:
            raise MigrationRefused("--backup-verified is required for stamp and upgrade")
        required = REQUIRED_STATE[args.action]
        if assessment["state"] != required:
            raise MigrationRefused(f"{args.action} requires state {required}, found {assessment['state']} ({assessment['verdict']})")

        from alembic import command

        if args.action == "stamp":
            command.stamp(_alembic_config(url), preflight.BASE_REVISION)
        else:
            command.upgrade(_alembic_config(url), preflight.TARGET_REVISION)

        after_info, after = _inspect(url)
        print(f"done: state={after['state']} {preflight.VERSION_TABLE}={after_info['zconnect_alembic_version']}")
        return 0
    except (MigrationRefused, preflight.PreflightError) as exc:
        print(f"REFUSED: {str(exc).rstrip('.')}. Nothing was changed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(run())
