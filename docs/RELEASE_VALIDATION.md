# Analytics/PDF release validation — 2026-10-21

No production migration, deployment, Git push, Zoho/Unify call, or real invoice creation
was performed during validation. Export tests use fake invoice services only.

| Gate | Result |
| --- | --- |
| Complete backend/security/PostgreSQL/integration suite | 385 passed, no skips |
| PostgreSQL server | Isolated loopback PostgreSQL 17.6; EFS-encrypted storage |
| Frontend reproducible install | npm ci from committed lockfile; Next.js 14.2.35 |
| Node 22 frontend lint | Passed, no warnings/errors |
| Node 22 frontend security tests | 5 passed |
| Node 22 production build/types | Passed |
| Node 22 Next.js -> HMAC v3 -> FastAPI -> PostgreSQL | Passed |
| Tenant isolation | Cross-company requests denied; company signature tampering rejected; composite company foreign keys enforced |
| Database API protection | New tables have RLS; PUBLIC/anon/authenticated table and sequence access revoked; drift rejected |
| Known pre-created Analytics schema | Requires explicit repair flag; revalidated under transaction locks; unknown columns/policies rejected |
| Production-copy migration rehearsal | 0002 -> 0003 repair passed on restored PostgreSQL 17.6; rows and original schema preserved |
| Fresh production backup/restore | All 13 existing Zconnect tables plus audit trigger function restored and compared on PostgreSQL 17.6 |
| PDF | Download MIME/attachment/cache headers and bytes verified; synthetic report rendered and visually reviewed |
| Render dependencies | reportlab 4.4.10 and tzdata 2026.2 pinned and installed from backend/requirements.txt |
| Email/Resend | Deferred; no integration added |

Fresh encrypted backup evidence is outside Git under:
`C:\Users\Camilo\ZconnectBackups\analytics-release-backup-20261021T195013Z`.
Retain **both** `zconnect-pre-0003.dump` and `zconnect-supporting-functions.sql`, their
snapshot and `restore-verification.json`. All are EFS-encrypted. The source dump checksum
and supporting SQL checksum are recorded in that evidence file. Recheck backup freshness
after fencing production writers before using `--backup-verified`.

See [the exact gated migration/deployment procedure](ANALYTICS_RELEASE.md).
Production is at 0002 with known pre-created Analytics tables: the approved next command
must target `0003_analytics` with `--repair-existing-analytics`, never stamp those tables.
After successful migration and read-only schema/security/row-preservation postchecks,
push only the reviewed release commit, then coordinate the HMAC v3 backend and frontend
deployment. An old v2 deployment is not a compatible rollback.
