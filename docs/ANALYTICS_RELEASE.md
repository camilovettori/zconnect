# Sync Safety, Phase 5 UI, Analytics V1 and PDF Reports

This release supersedes the HMAC v2 deployment sequence in RELEASE_RUNBOOK.md. Email
delivery and Resend are deferred. No invoice creation is part of release verification.

## Release gates

- Backend and frontend must deploy together: both now require HMAC **v3**, binding the
  verified Supabase `app_metadata.company_id` to the exact request body. Do not pair v2
  and v3 deployments. Invalid assignments fail closed; only a trusted platform admin
  can request another company's analytics or PDF. Legacy accounts remain in `default`.
- Render installs `backend/requirements.txt`, including `reportlab==4.4.10` and
  `tzdata==2026.2`. Vercel uses Node 22 and the committed npm lockfile.
- Run `npm ci`, `npm run lint`, `npm test`, `npm run build` in frontend. Then run the
  entire backend suite with `ZCONNECT_TEST_POSTGRES_URL` pointing only to a disposable
  localhost PostgreSQL 17 database containing `test` in its name. No PostgreSQL skips
  are acceptable for release. `test_release_live_flow.py` also needs that frontend build.
- No push while tests fail. **No push until production 0003 and its postchecks pass**:
  the release branch can trigger a Vercel production deployment.

## Current production state and migration

The read-only inspection on 2026-10-21 found version `0002_export_events` plus all four
pre-created Analytics tables. Their schema matches the bundled legacy model reference,
but RLS/API restrictions and composite tenant constraints are missing. Never stamp them
as 0003 or drop/recreate them to make a migration pass.

The reviewed 0003 migration has two paths: create the four tables if absent, or repair
**only the exact legacy schema** with explicit `--repair-existing-analytics`. The latter
revalidates schema under table locks inside the migration transaction, preserves rows,
adds composite company foreign keys, seeds the default company only if absent, revokes
PUBLIC/anon/authenticated table and sequence privileges, and enables RLS. Unknown or
partial schemas fail closed. The seven original tables, foreign `public.export_events`,
foreign `public.alembic_version`, and ZRecipe objects are not altered.

The backend uses owner access; RLS denies Data API users, while signed tenant identity
and scoped queries protect application access. Composite foreign keys prevent fact/run
and line/fact company mismatches even if a privileged writer makes a mistake.

## Fresh backup and restore gate

The backup must include every existing Zconnect table: currently the original seven,
the audit and version tables, and the four pre-created Analytics tables (13 total).
Table-selective `pg_dump` does not include the audit trigger function automatically:
retain **zconnect-supporting-functions.sql** alongside **zconnect-pre-0003.dump**.
Restore that function into an isolated database before restoring the custom dump.
Both files, metadata, diagnostic logs and PostgreSQL cluster storage must remain on
encrypted storage. Compare all selected table definitions and row counts, version,
settings presence, status summaries and the supporting function against the snapshot.
Never restore into production. Preserve `restore-verification.json` and checksums.

A fresh EFS-encrypted backup and complete PostgreSQL 17.6 restore rehearsal were made
on 2026-10-21. The exact known legacy repair was then successfully rehearsed on that
restored copy. Revalidate backup freshness after stopping writers; if production changed,
repeat the backup and restore rather than asserting a stale `--backup-verified` gate.

## Exact production sequence — separate approval required

1. Confirm release commit hashes and the test evidence. Inspect Render/Vercel settings
   without revealing values: same server-only HMAC secret, correct Supabase project,
   Vercel service-role key restricted to Production, HTTPS Render BACKEND_URL without
   trailing slash, frontend root, backend root/build/start, health `/`, auto-deploy off.
2. Stop all Zconnect writers. Suspend only the Zconnect backend if no complete writer
   fence exists. Do not restrict shared database roles or change ZRecipe.
3. Securely load the existing backend/.env DATABASE_URL into only the operator process
   or migration child process, never a command argument or repository file. The runner
   itself intentionally ignores .env. From backend, use the following **after approval**:

```powershell
$py = '.\.venv\Scripts\python.exe'
$guard = '--expected-host','aws-1-eu-central-1.pooler.supabase.com',
         '--expected-database','postgres',
         '--expected-user','postgres.hoqopenktmcqiscweuou'
& $py tools\run_migration.py @guard status
# Require analytics_unversioned, exact known legacy schema, fresh restore-tested backup.
& $py tools\run_migration.py @guard --target 0003_analytics `
    --backup-verified --repair-existing-analytics upgrade
if ($LASTEXITCODE -ne 0) { throw 'Migration failed; stay in maintenance. Do not push.' }
& $py tools\schema_preflight.py postcheck '<FRESH_BACKUP>\pre_migration_snapshot.json'
if ($LASTEXITCODE -ne 0) { throw 'Postchecks failed; do not push or deploy.' }
& $py tools\schema_preflight.py compare
if ($LASTEXITCODE -ne 0) { throw 'Schema/security drift; do not push or deploy.' }
```

Production is already at 0002: **do not stamp 0001 or rerun 0002**. An unversioned fresh
installation instead needs guarded stamp 0001, explicit upgrade 0002, then explicit
upgrade 0003 without the repair flag. Never upgrade `head` or bypass target/backup guards.

4. Require `analytics_migrated`, exact schema/tenant constraints, all four RLS flags,
   no effective PUBLIC/anon/authenticated table or sequence privileges, no permissive
   Analytics policies, original row counts/audit count/foreign object summaries unchanged.
5. Only after these pass and push authorization remains valid, push the reviewed exact
   commit to the approved GitHub release branch. Do not push unrelated local files.
6. Deploy the same approved backend commit to Render and frontend commit to Vercel as
   a coordinated pair during maintenance. Since push may start Vercel first, keep the
   backend stopped/fenced until the v3 pair is ready. Do not resume the old unauthenticated
   backend as a substitute for deploying the approved commit.
7. Check backend `/` 200, unsigned `/api/analytics/summary` 401, docs disabled. Test login,
   History, CSV/XLSX preview, Analytics and a PDF download as the approved company. Verify
   cross-company requests are denied, binary PDF response and attachment headers are
   preserved, and logs have no HMAC failures. Do not sync, reset, reconcile, save settings,
   create users, or call Zoho/Unify during smoke tests. Then end maintenance.

## Rollback

No old v2 frontend/backend pair is a safe rollback for this v3 release. Keep the service
stopped and fix forward if either side fails. Do not downgrade 0003 after values have been
captured; that drops Analytics data. Production restore or any destructive repair needs
its own approval. A failed PostgreSQL migration transaction rolls back; a failed postcheck
after commit means changes **did occur**, so never treat it as an unchanged database.
