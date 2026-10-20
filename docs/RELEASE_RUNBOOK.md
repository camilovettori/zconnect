# Zconnect release runbook — security & invoice-safety release

Release commit: **`<RELEASE_COMMIT>`**, the tip of the `release` branch (`git rev-parse --short release`). It must contain the commit “Isolate Zconnect migration objects in the shared Supabase database”. Record the hash in the release ticket and use that exact hash everywhere below.

This release adds signed Next.js → FastAPI requests (HMAC v2 with a body digest), cross-site request protection, server-side session validation, invoice idempotency with Soft Reset and Zoho reconciliation, and the append-only `zconnect_export_events` audit table (migration `0002_export_events`, Alembic history in `zconnect_alembic_version`).

**Shared database.** Production runs in the same Supabase project as ZRecipe. Every object Zconnect adds carries a `zconnect_` prefix. A table named `public.export_events` (13 columns, unknown owner) and possibly a `public.alembic_version` belong to someone else: they are reported for information only and must never be modified, adopted or dropped as part of this release.

**🔒 = changes or contacts production. Each 🔒 step needs explicit approval before it is run.** Steps without 🔒 are local actions or read-only dashboard checks.

Placeholders: `<RELEASE_COMMIT>` (above), `<RENDER_URL>` (e.g. `https://<service>.onrender.com`), `<APP_URL>` (production frontend URL), `<POOLER_HOST>` and `<PROJECT_REF>` (Supabase → Connect → Session pooler), `<MAJOR>` (PostgreSQL major version reported by the preflight), `<SECURE_DIR>` (encrypted storage outside the repository). Never paste secrets into this document, a terminal command line, a ticket or a chat.

---

## 1. Compatibility rules

The frontend signs every API request with HMAC v2; the backend rejects anything else. Frontend and backend must always be deployed as a compatible pair.

| Frontend | Backend | Result |
|---|---|---|
| `<RELEASE_COMMIT>`, `71b88e8`, `dea72a0`, `aedb80c` or `a8c8f79` (identical HMAC v2 frontend) | `<RELEASE_COMMIT>` | ✅ supported (target / frontend-only rollback) |
| any | `a8c8f79` … `71b88e8` | ⛔ **never in the shared database**: these backends use the generic table name `export_events`, which already exists there and belongs to another application. Their startup check would accept it and audit writes would go to it |
| any | `c7affae`, `e49d3d0` | ⛔ same `export_events` collision; `c7affae` is also unauthenticated |
| any | before `c7affae`, or any pre-release deployment | ⛔ **never**: the backend is unauthenticated (anyone can trigger Zoho exports, resets and settings changes) and has no invoice idempotency |

Consequence: there is **no older backend to roll back to**. A backend problem is handled by suspending the Render service (safe outage) and fixing forward (§5).

Never use Render's **Rollback** or Vercel's **Instant Rollback** to a deployment made before this release, and never run `alembic downgrade` once `zconnect_export_events` holds rows.

---

## 2. Configuration checklist

The repository contains no `vercel.json`, `render.yaml` or CI workflow: every setting below lives only in the dashboards and must be checked there.

### Settings that can start a deployment

| Platform | Action | Starts a deployment? |
|---|---|---|
| Render | Push to the service branch with **Auto-Deploy on** | Yes |
| Render | Saving environment variables | Yes, unless a **Save only** option is offered and chosen. If the dashboard shows a single save button, assume it deploys the branch's latest commit |
| Render | **Manual Deploy**, **Resume** of a suspended service, **Rollback**, a call to the service's Deploy Hook URL | Yes (Resume restarts the last deployed version) |
| Render | Auto-Deploy toggle, health check path, build/start commands | No (applied to the next deploy); confirm in the **Events** tab |
| Vercel | Push to the **Production Branch** | Yes, production |
| Vercel | Push to any other branch | Yes, a **Preview** deployment |
| Vercel | **Redeploy**, **Promote**, **Instant Rollback**, a call to a Deploy Hook | Yes |
| Vercel | Changing environment variables or the Production Branch | No (applies to the next deployment) |

After every settings change, open Render → **Events** and Vercel → **Deployments** and confirm nothing started unexpectedly.

### Render (backend service)

| Check | Where | Expected |
|---|---|---|
| Auto-Deploy | Settings → Build & Deploy → **Auto-Deploy** | **Off** (🔒 to change). The service must only deploy when **Manual Deploy** is used |
| Deploy hook | Settings → Build & Deploy → **Deploy Hook** | Not used by any external system (anyone holding that URL can deploy) |
| Root directory | Settings → Build & Deploy | `backend` |
| Build command | Settings → Build & Deploy | `pip install -r requirements.txt` |
| Start command | Settings → Build & Deploy | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Health check path | Settings → **Health Check Path** | **`/`** (🔒 to change). `/` is the only unauthenticated route (returns 200); any `/api/...` path returns 401 after this release and would fail the deploy |
| Python version | latest build log (“Using Python version …”) | Note it. Tests ran on 3.13; `runtime.txt` says 3.12.8 |
| `DATABASE_URL` | Environment | **Required.** Supabase **session pooler** (port 5432) of the production project. There is no default and no fallback: the backend refuses to start if it is missing, is not PostgreSQL, or the database is unreachable at boot (the error names scheme/host/port/database only, never credentials). Render then keeps the previous deploy running |
| `ZCONNECT_ENVIRONMENT` | Environment | `production` (🔒, added in step W5 with the secret). Render's own `RENDER=true` already enables the PostgreSQL-only rule; the explicit value documents it and protects non-Render hosts. Unknown values (e.g. `prod`) are rejected at startup |
| `ZCONNECT_BACKEND_SECRET` | Environment | Added **during the release window** (step W5), identical to Vercel's, ≥ 32 characters |
| `ZCONNECT_BACKEND_SECRET_PREVIOUS` | Environment | Absent (only during a secret rotation) |
| `ZCONNECT_ENABLE_API_DOCS` | Environment | Absent |

### Vercel (frontend project)

| Check | Where | Expected |
|---|---|---|
| Root directory | Settings → General / Build | `frontend` |
| Node.js version | Settings → General | 20.x or 22.x |
| Production Branch | Settings → Git → **Production Branch** | **`release`** (🔒 to change). The branch does not exist until step W6, so no push can publish a production frontend before the backend is ready. Existing production deployments are not affected by the change |
| Deploy hooks | Settings → Git → **Deploy Hooks** | None that target the production branch, or known and unused |
| Deployment Protection | Settings → Deployment Protection | Vercel Authentication enabled for Preview deployments |
| `ZCONNECT_BACKEND_SECRET` | Settings → Environment Variables | Environment = **Production only** (🔒 to add). Not Preview, not Development |
| `SUPABASE_SERVICE_ROLE_KEY` | Environment Variables | **Production only**. A Preview deployment runs code from any pushed branch: it must not hold production admin credentials |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` | Environment Variables | Production: the production project. Preview: absent or a non-production project |
| `BACKEND_URL` | Environment Variables (Production) | `https://<service>.onrender.com`: https, **no trailing slash** (a trailing slash produces `//api/...`, which fails with 404/401), no path |
| `APP_ORIGINS` | Environment Variables (Production) | Usually **unset**. The CSRF check already trusts the host the browser used (Vercel sets `X-Forwarded-Host`). Set it only if another proxy rewrites the host and legitimate logins fail with “Cross-site request blocked”: comma-separated exact origins, e.g. `https://app.example.com` (scheme + host, no path, no trailing slash) |

How to read an environment variable's scope: Settings → Environment Variables → the variable's row lists its environments (Production / Preview / Development). Use **Edit** only to inspect, then Cancel.

Without the secret, a Preview deployment fails closed: its API calls return 503 and nothing reaches the backend.

### Supabase

| Check | How | Expected |
|---|---|---|
| Production project | Project URL `https://<PROJECT_REF>.supabase.co` | Same `<PROJECT_REF>` as Vercel's `SUPABASE_URL` and as the user `postgres.<PROJECT_REF>` in Render's `DATABASE_URL` |
| Backups | Database → Backups | Latest daily backup (and PITR, if enabled) noted before the window |
| Admin users | SQL editor (read-only): `select email, raw_app_meta_data->>'role' as role from auth.users order by email;` | Every operator who needs Reset / Reconcile / Settings / Users has role `admin` |
| Data API exposure | Database → Data API / table settings, and the preflight's `WARNING:` lines | No Zconnect table reachable by `anon` / `authenticated` (list below) |

**Zconnect tables that must not be reachable through the Data API** (the project's anon key is public: ZRecipe ships it to browsers):

| Table | Holds | Status |
|---|---|---|
| `settings` | Zoho / Unify credentials | protected (reported 2026-10-20) |
| `exported_orders` | idempotency records per order | protected (reported 2026-10-20) |
| `sync_runs` | sync history | protected (reported 2026-10-20) |
| `exported_invoices` | Zoho invoice ids, customer names, order lists | 🔒 **protect before go-live** |
| `customer_mappings` | customer → Zoho contact ids | 🔒 **protect before go-live** |
| `item_mappings` | SKU → Zoho item ids | 🔒 **protect before go-live** |
| `zoho_item_mappings` | product → Zoho item ids and tax ids | 🔒 **protect before go-live** |
| `zconnect_export_events` | audit trail (actor ids, invoice ids) | 🔒 created by the migration: **protect in step W4b**, before the backend is deployed |
| `zconnect_alembic_version` | migration version | 🔒 created by the stamp: **protect in step W4b** |

`public.export_events` was also removed from the Data API; it is not a Zconnect table (unknown owner) and Zconnect never uses it. New tables in `public` normally inherit Supabase's default grants to `anon` / `authenticated`, so the two tables the migration creates are exposed until W4b. The Zconnect backend connects as the table owner, so revoking API-role access (or enabling RLS without policies) does not affect it.

Granting admin is a production change (🔒):
```sql
update auth.users
set raw_app_meta_data = coalesce(raw_app_meta_data, '{}'::jsonb) || '{"role":"admin"}'
where email = '<operator email>';
```
The role takes effect on the next request; no re-login is needed.

### Zoho and Unify settings

Credentials live in the production database `settings` table and are managed from the Connections page. **This release does not change them.** The preflight lists which names are set (never values). Expected: `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`, `ZOHO_ORG_ID`, `ZOHO_BASE_URL`, `ZOHO_STANDARD_TAX_ID`, `ZOHO_REDUCED_TAX_ID`, `ZOHO_ZERO_TAX_ID` (invoice lines without a tax id are rejected), plus `UNIFY_CLIENT_ID` / `UNIFY_CLIENT_SECRET` if the Unify API is used.

### Shared HMAC secret

Generate locally; it goes to the clipboard and is never displayed:
```powershell
$b = New-Object byte[] 48; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); [Convert]::ToBase64String($b) | Set-Clipboard; Remove-Variable b
# paste into Vercel (Production) and, in step W5, into Render; then clear the clipboard:
Set-Clipboard -Value " "
```

---

## 3. Database migration tools

Both tools live in `backend/tools/` and are covered by `backend/tests/test_schema_preflight.py`.

* `schema_preflight.py`: **read-only**. The session runs with `default_transaction_read_only=on` and a statement timeout, and selects only catalogs, row counts and setting **names**. Reports the target, the match of Zconnect's seven original tables against the bundled `0001_initial` references (`tools/schema_references/`), the state of Zconnect's migration objects (`zconnect_alembic_version`, `zconnect_export_events` compared exactly against `zconnect_export_events.json`), foreign objects (`public.export_events`, `public.alembic_version`, informational), status distributions, RLS state and Supabase API-role grants.
* `run_migration.py`: guarded `stamp 0001_initial` / `upgrade 0002_export_events`. It refuses unless host, database and role match the expected values, `--backup-verified` is given, and the database is in the state the action requires (§3.3). It never upgrades to `head` and never touches objects without the `zconnect_` prefix other than Zconnect's seven original tables, which it does not modify either.

Both read the URL **only** from the `DATABASE_URL` environment variable (never from `backend/.env`) and accept PostgreSQL only. The application itself has no default database and no SQLite fallback, and `alembic.ini` has no URL: a plain `alembic` command without a valid `DATABASE_URL` fails instead of migrating a local file.

### 3.1 Session setup (operator workstation, PowerShell)

```powershell
cd <repo>\backend
$s = Read-Host "Production DATABASE_URL (session pooler, port 5432)" -AsSecureString
$env:DATABASE_URL = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)); Remove-Variable s
$py = ".\.venv\Scripts\python.exe"
$guard = "--expected-host","<POOLER_HOST>","--expected-database","postgres","--expected-user","postgres.<PROJECT_REF>"
```
Close the session (or `Remove-Item Env:DATABASE_URL`) when done.

### 3.2 Confirm the correct production database (🔒 read-only contact)

1. `& $py tools\schema_preflight.py target` prints host, port, database and user (never the password). They must equal the Supabase production project's session pooler values and Render's `DATABASE_URL`.
2. `& $py tools\schema_preflight.py compare`. In the printed information:
   * `sync_runs_status`: the latest `started_at` matches the most recent run on the production History page;
   * `row_counts`: plausible for production;
   * `settings_keys`: the expected Zoho names are set.
3. Every `run_migration.py` call includes `@guard`; a different host, database or project is refused before anything happens.

### 3.3 Decision table (from `compare`)

The last line of `compare` states the database's state. Only the two *ready* states print `SCHEMA MATCHES`.

| Last line | State | Action |
|---|---|---|
| `SCHEMA MATCHES 0001_initial (...): unversioned, ready to stamp` | `ready_to_stamp` | stamp, then upgrade (expected: production tables were created by the app's startup) |
| `SCHEMA MATCHES 0001_initial (...): at 0001_initial, ready to upgrade` | `ready_to_upgrade` | upgrade only |
| `ALREADY MIGRATED to 0002_export_events: nothing to do` | `migrated` | no migration; continue with W5 only if this was expected |
| `INCOMPATIBLE ZCONNECT OBJECTS PRESENT: ...` | `incompatible` | **stop**: a `zconnect_*` object exists without a matching version, or differs from the reference (the `PROBLEM:` lines list the exact differences) |
| `SCHEMA DIFFERS from every 0001_initial reference: ...` | `schema_differs` | **stop**; do not stamp or upgrade |

Lines starting with `INFO:` describe foreign objects such as `public.export_events` (expected in production: 13 columns, 0 rows, 5 indexes, 0 custom triggers). They never block and are never modified. If their description changes between the preflight and the post-check, the post-check fails.

`WARNING: Zconnect table public.<name> is accessible to anon/authenticated (...)` lists every Zconnect table the Supabase API roles can reach; `WARNING: row-level security is disabled on Zconnect tables: ...` lists those without RLS. Warnings never block the preflight or the post-check, but every Zconnect table must be protected (§2, Supabase) before go-live. The full `rls_disabled_tables` and `api_role_grants` lists also cover other applications' tables, for information.

### 3.4 Backup and restore verification (🔒 read-only export)

Requires `pg_dump` / `pg_restore` with a major version ≥ the `server_version` shown by the preflight. Docker is used for a throwaway restore target.

The dump is limited to Zconnect's seven tables (their id sequences are included automatically). The `public` schema also holds ZRecipe's tables and customer data, which must not be exported by this procedure, and whose Supabase-specific policies would not restore into a plain PostgreSQL.

```powershell
& $py tools\schema_preflight.py dump pre_migration_snapshot.json
$pg = $env:DATABASE_URL -replace '^postgresql\+psycopg://','postgresql://'; $ts = Get-Date -Format yyyyMMdd-HHmm
$tables = "settings","sync_runs","exported_orders","exported_invoices","customer_mappings","item_mappings","zoho_item_mappings" | ForEach-Object { "--table=public.$_" }
pg_dump --format=custom --no-owner --no-privileges @tables --file "<SECURE_DIR>\zconnect-pre-0002-$ts.dump" $pg
Remove-Variable pg

docker run --name zc-restore -e POSTGRES_PASSWORD=restorecheck -p 127.0.0.1:55499:5432 -d postgres:<MAJOR>
pg_restore --no-owner --no-privileges --dbname "postgresql://postgres:restorecheck@127.0.0.1:55499/postgres" "<SECURE_DIR>\zconnect-pre-0002-$ts.dump"
$prod = $env:DATABASE_URL; $env:DATABASE_URL = "postgresql://postgres:restorecheck@127.0.0.1:55499/postgres"
& $py tools\schema_preflight.py dump restore_check.json
$env:DATABASE_URL = $prod; Remove-Variable prod
& $py -c "import json;l=lambda f:json.load(open(f,encoding='utf-8-sig'));a,b=l('pre_migration_snapshot.json'),l('restore_check.json');print('RESTORE VERIFIED' if a['info']['row_counts']==b['info']['row_counts'] and a['schema']==b['schema'] else 'RESTORE MISMATCH - STOP')"
docker rm -f zc-restore
```
* Proceed only after `RESTORE VERIFIED`. A `pg_restore` message that schema `public` already exists can be ignored; any other error means stop.
* Also note the time of Supabase's latest backup (Database → Backups).
* The dump contains the `settings` table (credentials): keep it encrypted in `<SECURE_DIR>`, never in the repository (`*.dump` is git-ignored as a safeguard), and delete it according to the retention policy. The JSON snapshots contain counts and setting names only.
* Use `dump FILE`, not `> FILE`: Windows PowerShell 5.1 redirection writes UTF-16.

### 3.5 Migration (🔒)

```powershell
& $py tools\run_migration.py @guard status                       # expect state=ready_to_stamp and the info: lines for foreign objects
& $py tools\run_migration.py @guard --backup-verified stamp      # only in state ready_to_stamp
& $py tools\run_migration.py @guard --backup-verified upgrade    # expect: done: state=migrated zconnect_alembic_version=['0002_export_events']
```
Expected changes, all new and all prefixed: the version table `zconnect_alembic_version` (created by the stamp), the table `zconnect_export_events` (12 columns), its 4 indexes `ix_zconnect_export_events_*`, the function `zconnect_export_events_append_only()` and the trigger `zconnect_export_events_no_update_delete`. No existing table or row is modified, including `public.export_events` and `public.alembic_version`. PostgreSQL runs the DDL in one transaction: a failure rolls back completely.

### 3.6 Post-migration checks

```powershell
& $py tools\schema_preflight.py postcheck pre_migration_snapshot.json
Remove-Item Env:DATABASE_URL
```
All six checks must print `PASS`: `zconnect_alembic_version` is `0002_export_events`; `zconnect_export_events` matches the bundled definition exactly (columns, constraints, indexes, append-only trigger); it is empty; Zconnect's existing row counts and `exported_orders` statuses are unchanged; the foreign objects (`public.export_events`, `public.alembic_version`) are unchanged. On any `FAIL`: stay in maintenance, do not deploy, review.

After the checks, `WARNING:` lines are expected for `zconnect_export_events` and `zconnect_alembic_version` (inherited default grants): they are resolved in step W4b.

If the upgrade fails: verify with `compare` that the schema is unchanged, do not deploy the new backend, and end the window on the previous version.

---

## 4. Release procedure

### Preparation (before the window)

| # | Step |
|---|---|
| P0 | Local: `git log --oneline -1 --grep "Isolate Zconnect migration objects" release` prints the isolation commit (otherwise **stop**: an older backend would collide with `public.export_events`), and `git rev-parse --short release` gives `<RELEASE_COMMIT>`. |
| P1 | 🔒 Render: Auto-Deploy **Off**; Health Check Path `/`. Confirm no deploy started (Events). |
| P2 | 🔒 Vercel: Production Branch → `release`; add `ZCONNECT_BACKEND_SECRET` (Production only); confirm Preview has neither the secret nor `SUPABASE_SERVICE_ROLE_KEY`. |
| P3 | 🔒 `git push origin main` (fast-forward; `main` and `release` point at `<RELEASE_COMMIT>`). Confirm Render started **no** deploy and Vercel created only a **Preview**. |
| P4 | 🔒 Read-only preflight (§3.2, §3.3); admin roles checked (§2). Protect the four remaining existing Zconnect tables (`exported_invoices`, `customer_mappings`, `item_mappings`, `zoho_item_mappings`) from the Data API, then rerun `compare`: no `WARNING: Zconnect table` line may remain. |

### Release window

| # | Step |
|---|---|
| W1 | Announce the maintenance window. Stop any local backend that may point at the production database. |
| W2 | 🔒 Render: **Suspend** the service (stops the previous, unauthenticated API; no sync can run). |
| W3 | 🔒 Snapshot, backup and restore verification (§3.4) until `RESTORE VERIFIED`. |
| W4 | 🔒 Migration (§3.5) and post-migration checks (§3.6): all `PASS`. |
| W4b | 🔒 Supabase: protect `zconnect_export_events` and `zconnect_alembic_version` from the Data API (same method as P4). Rerun `& $py tools\schema_preflight.py compare`: no `WARNING: Zconnect table` line may remain. |
| W5 | 🔒 Render: Environment → add `ZCONNECT_BACKEND_SECRET` and `ZCONNECT_ENVIRONMENT=production` (choose **Save only** if offered); confirm `DATABASE_URL` present and the docs flag absent → **Resume** → **Manual Deploy → Deploy a specific commit → `<RELEASE_COMMIT>`**. Logs must not show `zconnect_export_events table is missing`, `ZCONNECT_BACKEND_SECRET is missing`, `Configured database is unavailable` or `Production requires a PostgreSQL DATABASE_URL`. Then run the backend checks below. |
| W6 | 🔒 `git push origin release` → Vercel builds production from `<RELEASE_COMMIT>`; confirm the deployment shows that commit. |
| W7 | Read-only smoke tests (below). |
| W8 | End the window. 🔒 The first controlled invoice synchronisation is a separate approval. |

Backend checks (W5, and after any backend change):
```powershell
curl.exe -s -o NUL -w "%{http_code}`n" <RENDER_URL>/                    # 200
curl.exe -s -o NUL -w "%{http_code}`n" <RENDER_URL>/api/export/history  # 401 (unsigned requests rejected)
curl.exe -s -o NUL -w "%{http_code}`n" <RENDER_URL>/docs                # 404
```

### Read-only smoke tests (W7)

1. Private browser window → `<APP_URL>/sync` redirects to `/login`.
2. Log in as an admin → `/sync` loads; the `zconnect_session` cookie is HttpOnly and Secure.
3. History lists past runs (count consistent with the preflight).
4. Connections shows Zoho as configured. **Do not click Save.**
5. Upload a recent Unify CSV or XLSX → the preview loads; previously exported orders show as synced. (The preview only reads the database and makes no Zoho/Unify calls.)
6. Log out → back to `/login`; reloading `/sync` redirects to `/login`.
7. Render logs: no bursts of rejected signatures.

Forbidden during smoke tests: Sync, Sync this order, Reset / Unsync, Reconcile, Save settings, creating or deleting users.

---

## 5. Rollback (every action 🔒)

| Problem | Action |
|---|---|
| Frontend only | `git push --force-with-lease origin aedb80c:refs/heads/release` (or `a8c8f79`; both are HMAC v2 frontends). The backend stays on `<RELEASE_COMMIT>`. |
| Backend | Render → **Suspend** (safe outage), then fix forward with a new reviewed commit. There is no older compatible backend: every backend before `<RELEASE_COMMIT>` either is unauthenticated or uses the generic `export_events` name that collides with the foreign table (§1). |
| Migration failed during W4 | The transaction rolled back. Do not deploy. Resume the previous backend only if service must be restored before a new window (it does not use the new objects). |
| Data corruption | Restore from the verified backup (separate approved procedure). |

After any backend rollback, rerun the backend checks: the unsigned request **must** return 401.

Never: Render **Rollback** or Vercel **Instant Rollback** to a pre-release deployment (the old backend would *silently work* with the new frontend while its API is open to anyone); any backend commit before `<RELEASE_COMMIT>`; `alembic downgrade` once `zconnect_export_events` has rows; any change to `public.export_events` or `public.alembic_version`. If no authenticated version can run, keep the backend suspended.
