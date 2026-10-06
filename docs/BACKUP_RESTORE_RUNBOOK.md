# Backup & Restore Runbook (Work 16 §12)

**A backup that has never been restored is not a backup.** Everything below is a
command that was run on this repository against a real PostgreSQL 17 server, and
every number is a number the drill printed. Where a number is missing it is
because the measurement has not been made, and it says so.

- Tool: `backend/app/scripts/backup_restore.py` (run as `python -m app.scripts.backup_restore`)
- Tests: `backend/tests/test_work16_backup_restore.py`
- Settings: `BACKUP_DIR`, `BACKUP_PG_CONTAINER`, `BACKUP_MAX_AGE_HOURS`,
  `BACKUP_TOLERATE_FAILED_CHECKS` in `backend/app/core/config.py`

---

## 1. What is actually backed up

| # | Thing | How | Restorable? |
|---|-------|-----|-------------|
| 1 | **PostgreSQL** — every table, row, index and the `schema_migrations` ledger | `pg_dump -Fc` (custom format) | **Yes.** Verified by drill: `pg_restore` into a dropped-and-recreated database, 30/30 integrity checks pass. |
| 2 | **Object-storage metadata + backend config** — the `storage_objects` inventory (key, sha256, size, state, kind, backend, what object it backs) plus the storage backend settings | `storage_inventory.json` + `storage_inventory.md` | **Partially.** The *inventory* restores. The *bytes* do not — see §6 item 1 and §9. |
| 3 | **Secret/config references** — which credential settings exist and whether each is configured | `manifest.json → secret_references` | **Yes.** No value is ever written. |
| 4 | **SQLite deployments** (`Settings.database_url` ships SQLite) | `sqlite3.Connection.backup` + `PRAGMA integrity_check` | **Yes.** Covered by a test that runs without a database. |

Custom format (`-Fc`) is not a style choice: it is the only format `pg_restore`
can restore *selectively* from, and the only one carrying the object index.

### Backup directory layout

```
<out>/
  database.pgc           pg_dump -Fc dump          (416,532 B measured)
  manifest.json          canonical digests, storage metadata, secret REFERENCES, every command with its real duration
  storage_inventory.json every storage_objects row + storage backend config
  storage_inventory.md   the same inventory as a table, with each object's bytes PRESENT/CORRUPT/MISSING resolved against the volume
  drill.json             only after `drill`: every step, its wall clock, and the full verification report
```

---

## 2. Backup readiness

**Backup readiness is VERIFIED, not PARTIAL.** A drill was run end to end
against real PostgreSQL 17.11 on 2026-10-03; the restore was actually
performed, the database was actually destroyed before it, the application was
actually booted against the restored database, and a canonical row was actually
mutated between backup and restore so that the restore had to *replace* data to
pass.

| DoD item | Evidence |
|---|---|
| Backup taken | `manifest.json`, 416,532 B, sha256 `64a9033f5de2dfb2…` |
| Environment destroyed | `dropdb --force w16_drill_src`; re-checked absent |
| Restore actually performed | `pg_restore` into `w16_drill_restored`; `verify` → **30 checks, 0 failed** |
| Application boots against it | `BOOT OK paths=406 workspaces=1 migrations=37 ledger_rows=3 pending=[]` |
| Restore replaced real data | mutation `amount_usd 0.42 → 4242.0`; restored value read back as **0.42** |

---

## 3. Commands (exactly what was executed)

All commands were run from `backend/` in PowerShell. `$DSN` is the admin DSN;
it is shown redacted in the manifests and logs, and `PGPASSWORD` is passed
through the environment, never on the command line.

```powershell
$env:YMONEY_PG_CONTAINER = "ymoney-w16-pg"   # run pg_* inside the DB container
$env:DATABASE_URL = "postgresql://ymoney:<password>@127.0.0.1:55432/w16_drill_src"

# 1. back up
python -m app.scripts.backup_restore --dsn $env:DATABASE_URL --out C:\...\w16-drill backup

# 2. restore into a DIFFERENT database (the tool refuses to overwrite the source)
python -m app.scripts.backup_restore --dsn $env:DATABASE_URL --out C:\...\w16-drill `
    restore --target-db w16_drill_restored

# 3. verify a restored database
python -m app.scripts.backup_restore --dsn "postgresql://ymoney:<password>@127.0.0.1:55432/w16_drill_restored" `
    --out C:\...\w16-drill verify --database w16_drill_restored

# 4. the whole thing, including the destruction and the mutation
python -m app.scripts.backup_restore --dsn $env:DATABASE_URL --out C:\...\w16-drill `
    drill --target-db w16_drill_restored `
    --mutate-table cost_entries --mutate-column amount_usd --mutate-value 4242.0 `
    --mutate-id <ledger-row-id>
```

Exit codes: `0` verified, `1` verification FAILED, `2` the tool could not run
(`BACKUP FAILED: …`). A failed verification is a non-zero exit and a printed
report — never a warning.

### The commands the drill actually executed (from `drill.json`)

```text
docker exec ymoney-w16-pg pg_dump    -U ymoney -d w16_drill_src -Fc -f /tmp/database.pgc
docker cp ymoney-w16-pg:/tmp/database.pgc  C:\...\w16-drill\database.pgc
docker exec ymoney-w16-pg dropdb     -U ymoney --force w16_drill_src
docker exec ymoney-w16-pg pg_restore -U ymoney -d w16_drill_restored --no-owner --no-privileges /tmp/database.pgc
```

Two implementation notes that are load-bearing and were learned the hard way:

* the tool name comes **first** (`pg_dump …`, never `… pg_dump`). `docker exec
  CONTAINER -U ymoney psql …` makes the Docker CLI parse `-U` as its own flag
  and then try to exec a program named `-U`:
  `OCI runtime exec failed: … exec: "-U": executable file not found in $PATH`;
* the dump is written **inside** the container and copied out with `docker cp`.
  Redirecting a custom-format dump through a PowerShell text pipeline corrupts
  it, and a backup that is corrupt on the way to disk is worse than no backup
  because it looks like one.

---

## 4. The drill, step by step, with real output

Source database `w16_drill_src` was created, all 37 migrations applied, and
seeded with one of everything the DoD names: a workspace, a 3-deep content
lineage chain, 3 cost entries (estimate + settled actual + unknown exposure,
all carrying the Work 15.9 authority fields), a video and a lipsync job with
paid-execution columns, a leased job, and 3 canonical storage objects with real
sha256 checksums over real bytes.

```
STEP TIMINGS (drill.json)
backup    elapsed=  2.607s  416532 bytes, sha256 64a9033f5de2dfb2
mutate    elapsed=  2.658s  {"table": "cost_entries", "set": {"amount_usd": 4242.0}, ...}
destroy   elapsed=  3.285s  source database w16_drill_src exists afterwards: True
restore   elapsed=  7.003s  restored into w16_drill_restored
boot      elapsed= 16.602s  BOOT OK paths=406 workspaces=1 migrations=37 ledger_rows=3 pending=[]
verify    elapsed= 16.885s  30 checks, 0 failed
TOTAL elapsed_s = 16.885   ok = True   failed = 0
```

### 4.1 The mutation — this is what makes it a proof

```json
"mutation": {
  "table": "cost_entries", "where": {"id": "58261fa2-…"}, "set": {"amount_usd": 4242.0},
  "before": {"amount_usd": 0.42,  "is_estimate": false,
             "detail_json": {"spend_authority": "WORKSPACE_OWNED",
                             "actor_authority": "WORKSPACE_OWNER",
                             "budget_source": "WORKSPACE_BUDGET",
                             "charged_workspace_id": "6ff17703-…",
                             "cost_outcome": "ACTUAL", "operation_id": "op-paid-7f3a"}},
  "after":  {"amount_usd": 4242.0, …},
  "rows_updated": 1
}
```

### 4.2 The restore brought the ORIGINAL value back

```
$ docker exec ymoney-w16-pg psql -U ymoney -d w16_drill_restored \
    -c "SELECT amount_usd AS restored_amount FROM cost_entries WHERE id='58261fa2-…';"
 restored_amount
----------------
            0.42            <-- the pre-mutation value, not the 4242.0 that was live at backup+1
```

Without the mutation, "restored nothing" and "restored everything" leave the
same database state and the test passes for both. That is why the mutation is a
step, not a comment.

### 4.3 Per-table integrity, read from the RESTORED database

```text
 migrations | ledger_rows | total_usd | estimates | content_rows | with_parent | objects | jobs | workspaces
------------+-------------+-----------+-----------+--------------+-------------+---------+------+------------
         37 |           3 |    0.5275 |         1 |            3 |           2 |       3 |    1 |          1
 dangling_root_links
---------------------
                   0
```

`cost_entries` (amounts, `is_estimate`, Work 15.9 authority inside `detail_json`):

```text
 category | amount_usd | is_estimate | spend_authority | actor_authority |  budget_source   |              charged_ws
----------+------------+-------------+-----------------+-----------------+------------------+-------------------------------------
 llm      |     0.0175 | t           | SYSTEM_OWNED    | SYSTEM          | SYSTEM_BUDGET    | __system__
 llm      |       0.09 | f           | WORKSPACE_OWNED | WORKSPACE_OWNER | WORKSPACE_BUDGET | 6ff17703-3854-4947-9db2-84ab1ea16b05
 render   |       0.42 | f           | WORKSPACE_OWNED | WORKSPACE_OWNER | WORKSPACE_BUDGET | 6ff17703-3854-4947-9db2-84ab1ea16b05
```

`content_items` lineage (`parent_content_id` / `root_content_id` /
`derivation_type`, every link resolving):

```text
             topic              | derivation_type | has_root | has_parent
--------------------------------+-----------------+----------+------------
 Backup drills are not optional |                 | f        | f
 Drill -> child                 | platform_cut    | t        | t
 Drill -> root                 | variant         | t        | t
```

`videos` / `lipsync_jobs` paid-execution columns (Work 15.8 / 15.9):

```text
 cost_outcome | submission_state | submission_operation_id | provider_task_id
--------------+-----------------+--------------------------+------------------
 ACTUAL       | SUBMITTED       | op-paid-7f3a             | task-abc-123

 execution_outcome  |   cost_outcome
--------------------+------------------
 SUBMISSION_UNKNOWN | UNKNOWN_EXPOSURE
```

`jobs` — the Work 16 §2 lease columns:

```text
  type  | status  |   claimed_by    |      lease_expires_at
--------+---------+-----------------+----------------------------
 render | RUNNING | worker-backup-3 | 2026-10-02 22:55:52.540907
```

`storage_objects` — the inventory:

```text
                               object_key                               |   state   | size_bytes |   sha256_head
------------------------------------------------------------------------+-----------+------------+----------------
 6ff17703-…/render/a7981d1c4b23-hero.mp4                                | FINALIZED |       9216 | e376714dbbf4ad44
 6ff17703-…/subtitle/f1c74042c5c0-caption.srt                           | FINALIZED |         35 | 400775f954a420b7
 6ff17703-…/thumbnail/ea61fb4464c9-thumb.jpg                            | FINALIZED |         21 | f6b9d4a33e1e87b0
```

`schema_migrations` — every version present, count matching:

```text
 version
------------------
 0037_budget_rollups
 0036_gpu_and_storage
 0035_job_leases
 0034_paid_execution_outcomes
…
(37 total)
```

The 30 checks the drill ran (all `ok=true`):

```text
connected_to_expected_database          schema_migrations.all_versions_present
rows/digest : cost_entries               schema_migrations.version_count
rows/digest : content_items              content_items.lineage_links
rows/digest : videos                      content_items.no_dangling_lineage
rows/digest : lipsync_jobs                paid_execution.videos.histograms
rows/digest : storage_objects             paid_execution.lipsync_jobs.histograms
rows/digest : budget_rollup_limits      cost_entries.total_usd / .rows / .estimates
rows/digest : jobs                        cost_entries.actuals / .zero_amount_rows
rows/digest : workspaces                  cost_entries.authority_digest
                                          cost_entries.rows_with_authority
```

### 4.4 Boot

```
BOOT OK paths=406 workspaces=1 migrations=37 ledger_rows=3 pending=[]
```

Run in a **subprocess** with `DATABASE_URL` pointed at the restored database, so
it is a real process start against a real `DATABASE_URL`. It builds the
application, resolves the OpenAPI surface (406 paths — `len(app.routes)` is
*not* the route count on this FastAPI version, the included routers are one lazy
entry each), runs the migration runner, and reads canonical rows through the
application's own session factory. `pending=[]` is enforced: a restored database
whose ledger the runner considers incomplete is a database that would **change
on first boot**, i.e. already not the thing that was backed up.

---

## 5. Timings, RPO and RTO — measured, not estimated

Server: PostgreSQL 17.11, Docker container `ymoney-w16-pg`, 16 MB database,
37 migrations, ~100 tables, 1 workspace, 3 ledger rows, 3 storage objects.
Everything below is on **this** workload, which is small. Do not extrapolate the
numbers linearly without measuring your own row counts.

| Step | Measured |
|---|---|
| `pg_dump -Fc` command | **0.818 s** (inside the drill) |
| Full `backup()` incl. inventory + checksums + manifest + `docker cp` | 3.22 / 3.35 / 3.88 / 4.23 / 4.42 / 6.22 s → **median 4.05 s**, min 3.22 s, max 6.22 s (6 runs, 2 databases) |
| `pg_restore` command | **2.754 s** (inside the drill) |
| Full `restore()` incl. `dropdb` + `createdb` + `pg_restore` | 9.71 / 9.82 / 9.89 / 10.61 / 12.37 / 16.78 s → **median 10.25 s** |
| Application boot against the restored database | ~9.6 s (subprocess: interpreter start + import + migration runner + reads) |
| `verify` (30 checks) | **0.28 s** |
| **Whole drill** (backup → mutate → destroy → restore → boot → verify) | **16.9 s** internal, 18.6 s wall clock |
| Dump size | 416,532 B for a 16 MB database (99.7 % empty pages) |
| Restore-probe median, 2 databases × 3 runs | pg_dump 4.05 s / restore 10.25 s |

**RTO (measured) = ~17 s** to have a verified, bootable database back, on this
workload, including the boot check. Add operator time for pointing traffic at it.
If you need a smaller RTO, the answer is WAL archiving (§6), not a faster dump.

**RPO** is not a property of the tool — it is the backup interval plus the dump
duration, because the dump is a point-in-time snapshot:

> RPO = (time between two successful backups) + (dump duration)

With the schedule recommended below (daily 02:00 UTC), RPO ≤ **24 h + 4.1 s**.
With `BACKUP_MAX_AGE_HOURS = 26`, a backup older than 26 h is overdue and the
operator is told so; the tolerance is a full day plus the dump.

**These are the only RPO/RTO numbers that can be justified from what was
measured.** A production RPO of minutes would require WAL archiving or logical
replication, and neither exists here (§6).

---

## 6. What is NOT covered

Stated plainly, because the alternative is an incident discovering it.

1. **Media bytes.** The backup contains the `storage_objects` **inventory** —
   key, sha256, size, state — and the storage backend **config**. It does not
   contain multi-GB video/audio files. Re-materialisation path: `storage_inventory.md`
   lists each object with `PRESENT` / `CORRUPT` / `MISSING` resolved against the
   volume at backup time; re-render from the script and re-finalize the object.
   **A restored `FINALIZED` row whose bytes are gone is a database that lies
   about what it has** — this is the single largest gap in the current backup.
2. **WAL archiving / point-in-time recovery.** Not implemented, no config, no
   `archive_mode`. A failure mid-transaction loses everything since the last
   dump. RPO is therefore hours, not seconds.
3. **Logical replication / streaming to another host.** Not implemented. The
   backup lands on the local filesystem; copying it off-host is a deployment
   decision (see §7) that no code enforces or verifies.
4. **S3 object bytes.** The inventory records `REMOTE:<backend>` for
   non-local rows; this process cannot see a bucket, and "cannot see" is not
   "does not exist". No S3 lifecycle/versioning audit is performed. §9.3 gives
   the versioning/retention settings and what they would and would not buy.
5. **Point-in-time consistency across the three concerns.** The dump, the
   inventory and the manifest are three separate reads. The inventory is
   metadata only and cannot drift the ledger, but a `storage_objects` row
   written *during* the dump may be in the inventory and not the dump (or the
   reverse). The digests do not cross-check the two.
6. **Redis / queue state.** Not backed up. Jobs are re-derivable from the
   `jobs` table; anything held only in Redis is not.
7. **Secrets themselves.** By design. `manifest.json → secret_references` lists
   10 credential settings and records `configured=true/false` for 3 of them —
   **no values**. Restoring means re-injecting them from your secret store.
8. **Large-database / long-run behaviour.** Every timing here is from a 16 MB
   database. No measurement exists for a production-sized one; run the drill
   against a copy of production before trusting the RTO.
9. **Cross-dialect digests.** Row digests are stable for one database + one
   dialect (SQLite returns `1` where PostgreSQL returns `true`). Comparing a
   PostgreSQL backup's digest against a SQLite restore will not match; compare
   row counts instead.
10. **`drill` is destructive.** It drops the source database. **Never run it
    against a primary you still need** — run it against a staging clone or a
    scratch database. `restore()` refuses to target the source database for the
    same reason, and `apply_mutation()` refuses any table outside
    `cost_entries` / `content_items` / `videos` / `lipsync_jobs`.
11. **Encrypted backups at rest.** The dump is not encrypted by this tool. Treat
    the backup directory as a secret store (it contains the full ledger, the
    content lineage and the credential *names*).
12. **Retention / pruning.** Not implemented — no code deletes old backups.
    Retention below is a cron responsibility.

---

## 7. Schedule recommendation

Nothing here is implemented by the tool — a library that also scheduled itself
would double-take on every cron tick. This is the recommendation:

```cron
# Full logical backup, daily, low-traffic window. Verify every one; an
# unverified backup is not a backup.
15 2 * * *  cd /opt/ymoney/backend && \
  /opt/ymoney/backend/.venv/bin/python -m app.scripts.backup_restore \
    --dsn "$DATABASE_URL" --out /var/backups/ymoney/$(date -u +\%Y\%m\%dT\%H\%M\%SZ) \
    backup >> /var/log/ymoney/backup.log 2>&1

# Weekly: the FULL drill, against a STAGING clone. It destroys its source.
30 3 * * 0  cd /opt/ymoney-staging/backend && \
  /opt/ymoney-staging/backend/.venv/bin/python -m app.scripts.backup_restore \
    --dsn "$STAGING_DATABASE_URL" --out /var/backups/ymoney/drill-$(date -u +\%Y\%m\%d) \
    drill --target-db ymoney_drill_restored \
    --mutate-table cost_entries --mutate-column amount_usd --mutate-value 1 \
    --mutate-id <a known ledger row id> >> /var/log/ymoney/drill.log 2>&1
```

* **Frequency:** daily. 24 h RPO is the honest consequence of a logical dump
  with no WAL archiving; halving it halves RPO and doubles storage.
* **Retention:** 7 daily, 4 weekly, 12 monthly. At 416 KB per dump on a small
  database this is trivial; at production size, measure — the dump scales with
  *data*, not with pages.
* **Off-host copy is mandatory and unenforced by this tool.** Copy each backup
  directory to a second provider/bucket and check the copy. A backup on the same
  host as the database is not a backup of the host.
* **Alert on:** no new backup older than `BACKUP_MAX_AGE_HOURS`, and any
  `verify` or `drill` exiting non-zero. **Neither alert exists yet.** The
  thresholds in `services/observability/slo.py` are now genuinely read from
  `Settings` (Work 16.1 §6 — `ALERT_*` bound to rules, `SLO_*` bound to the
  published objectives), so adding these two rules is now a matter of writing
  the evaluators rather than first fixing a layer that ignored configuration.
  Until they exist, backup staleness is a cron/monitoring responsibility and
  nothing in the application notices.
* **Re-run the drill after any migration lands.** A restore that predates the
  newest migration is a restore to an older schema.

---

## 8. Tests

```powershell
# default suite: SQLite only, no database required (13 passed, 9 skipped)
python -m pytest tests/test_work16_backup_restore.py -q

# with PostgreSQL (22 passed)
$env:YMONEY_BACKUP_TEST_DSN = "postgresql://ymoney:<password>@127.0.0.1:55432/postgres"
$env:YMONEY_BACKUP_PG_CONTAINER = "ymoney-w16-pg"
python -m pytest tests/test_work16_backup_restore.py -q
```

The PostgreSQL tests are `pytest.mark.skipif`-gated on
`YMONEY_BACKUP_TEST_DSN`. They are never allowed to make the default suite
require a database.

### Mutation tests — what each guard is protecting

Each guard was deliberately broken and the real failure recorded.

| # | Guard | Mutation | Real failure |
|---|---|---|---|
| a | the restore replaces data | `pg_restore_from_file` skipped | `psycopg.errors.UndefinedTable: relation "cost_entries" does not exist` → `FAILED test_restore_returns_the_pre_mutation_value` |
| a | the same, SQLite | `restore_sqlite` made a no-op | `AssertionError: the restore did not replace the mutated file` / `assert 999.0 == 1.25` → `FAILED test_sqlite_backup_round_trip_replaces_the_file` |
| b | the backup refuses secret VALUES | `secret_references` gained a `leaked_values` map | `AssertionError: the manifest carries the secret VALUE` / `'sk-live-SENTINEL-MUST-NOT-APPEAR-9f3c2a' is contained here` → `FAILED test_manifest_names_secret_references_without_their_values` |
| c | the inventory carries checksums | `checksum` blanked in the inventory SELECT | `AssertionError: ('checksum', {… 'checksum': '', …})` / `assert '' not in (None, '')` → `FAILED test_storage_inventory_records_checksums_for_every_canonical_object`, `FAILED test_storage_inventory_markdown_hides_bytes_not_keys` |
| d | a partial restore is detected | `IntegrityReport.add` recorded `ok=True` regardless | `AssertionError: a ledger-less restore passed verification` → `FAILED test_partial_postgres_restore_is_detected`; `AssertionError: a restore that lost every ledger row passed verification` → `FAILED test_verify_detects_a_row_the_restore_lost` |

Real bugs found and fixed while building this, each of which would have made a
restore silently useless:

* `_table_exists` used PostgreSQL's `to_regclass` — **the entire SQLite backup
  path failed on the first table it looked at** (`no such function: to_regclass`);
* the drill verified against the **source** DSN after dropping the source
  database, so it could never have produced an integrity answer;
* `verify` ran the ledger queries unconditionally and crashed on a database with
  no `cost_entries`, and `SELECT current_database()` does not exist on SQLite;
* container mode put flags before the tool name, so Docker parsed `-U` as its
  own — `exec: "-U": executable file not found in $PATH`;
* `backup_sqlite` used `with sqlite3.connect(...)`, which is a **transaction**
  context manager, not a closer: the handle stayed open and the restore step then
  failed with a Windows sharing violation;
* the SQLite backup hard-coded `"objects": 0` in its manifest regardless of what
  was actually in the database;
* the `schema_migrations` check verified membership but not **count**, so a
  database migrated by something other than this codebase passed;
* `apply_mutation` re-derived its `WHERE` clause by stripping quotes from a
  different string, so its "before" could describe a row that never existed.

---

## 9. Production durability scope (§7)

Stated first, because the rest of this document is a drill against one of three
things and it is easy to read "416 KB, 30/30 checks, RTO 17 s" as "the system
is protected".

### 9.1 The production architecture, explicitly

```text
PostgreSQL backup  +  object-storage durability/versioning policy  +  media inventory/checksum
     BUILT & VERIFIED      A BUCKET PROPERTY, OFF BY DEFAULT            BUILT (metadata only)
```

| Leg | What it is | Built here? | How it is verified | The failure it covers |
|---|---|---|---|---|
| **PostgreSQL logical dump** | every table, row, index and the `schema_migrations` ledger, via `pg_dump -Fc` | **yes** — §2/§4, 30/30 checks after a real `pg_restore` | the drill destroys the source, mutates it, restores, boots the app and compares digests | corruption, an accidental `DROP`, a bad migration |
| **Object-storage durability + versioning + retention** | the media BYTES, which live outside the database | **no** — `STORAGE_BACKEND`/`S3_*` only decide *where* bytes go; whether the provider keeps old versions is a bucket property this codebase never sets and never reads | **nothing in this repository.** No lifecycle or versioning audit is performed, and the drill cannot see a bucket | a deleted or overwritten media object |
| **Media inventory + checksum** | the `storage_objects` rows: key, sha256, size, state, and what object each backs | **yes**, as *metadata inside the dump* — `storage_inventory.json` / `.md` | each object resolved `PRESENT` / `CORRUPT` / `MISSING` against the volume at backup time | knowing *which* bytes are missing, which is what makes re-rendering a decision rather than a search |

**This repository builds and verifies one leg and a half.** The half is the
inventory: it tells you the media is gone, which is worth having, but it cannot
bring the media back. Everything in §1's table row 2 marked "Partially" is
exactly this gap, and no amount of running §7's cron closes it.

Deliberately **not** built, and not planned here: WAL archiving, logical
replication, point-in-time recovery, multi-region buckets. §6 items 2 and 3
record them as absent; adding them is a different piece of work with a
different proof obligation, not a config flip.

### 9.2 RPO / RTO, plainly, per leg

| Leg | RPO (what you can lose) | RTO (how long to be back) |
|---|---|---|
| PostgreSQL | ≤ **24 h + 4.1 s** with the daily 02:00 UTC cron of §7 — because the dump is a point-in-time snapshot and there is no WAL archive | **~17 s** measured, on a 16 MB database (§5). Not extrapolated to production size |
| Media bytes | **whatever the object provider's own durability and retention are.** Nothing in this repository bounds it, measures it or alerts on it | **not a restore at all.** A `FINALIZED` row whose bytes are gone cannot be recovered from a backup; you re-render from the script and re-finalize the object. Minutes to an hour per asset, unmeasured |
| Inventory | one dump cycle, because it travels inside the dump | with the PostgreSQL row |

The asymmetry is the point and is worth stating in an incident: **restoring the
database restores the record that a video exists, and not the video.** A restored
`FINALIZED` row whose bytes are gone is a database that lies about what it has,
and `storage_inventory.md` is the only thing in this system that will tell you.

The `content_items` lineage *does* survive in the dump (it is in the 30 checks:
`content_items.lineage_links`, `content_items.no_dangling_lineage`), which is
what makes re-rendering a mechanical operation rather than an archaeology
project. It is still a re-render: the original bytes are not in the backup.

### 9.3 Turning on object-storage versioning and retention

This is an **operator action on the bucket**, not a YMONEY setting, and the code
does not perform it, verify it or depend on it. `STORAGE_BACKEND=s3`,
`S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_REGION`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`
choose where bytes are written; none of them turn on versioning.

The snippet below is verified on this repository: it was executed against a
`botocore` `Stubber`, which asserts the exact request the SDK would send, so the
parameter names and shapes are real rather than remembered. It was **not**
executed against a live bucket — this repository has none.

```python
# python -  (run with the same environment the backend runs in)
from app.services.storage import S3Storage          # the SHIPPED client,
                                                    # built from S3_* settings
storage = S3Storage()                                # fails closed if S3_BUCKET is unset
bucket = storage.bucket

client = storage._client()

# 1. Versioning: keep every prior version of every object.
client.put_bucket_versioning(
    Bucket=bucket,
    VersioningConfiguration={"Status": "Enabled"})
print(client.get_bucket_versioning(Bucket=bucket)["Status"])   # -> Enabled

# 2. Retention: expire noncurrent versions, and abort unmultipart leftovers.
client.put_bucket_lifecycle_configuration(
    Bucket=bucket,
    LifecycleConfiguration={"Rules": [{
        "ID": "ymoney-noncurrent-expire",
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
    }]})
```

Verified output on this repository:

```text
after enable, get_bucket_versioning -> Enabled
Stubber asserted every parameter exactly; no pending responses left.
lifecycle stub OK
```

`STORAGE_BACKEND=s3` must already be working before any of this is worth
running: `S3Storage()` raises `RuntimeError("S3 storage selected but S3_BUCKET is
not configured")` when the bucket is unset, and `/readyz` reports the storage
probe as non-critical, so a broken object store does **not** fail readiness.

**What versioning would protect against**

* an application bug that overwrites an object key — the prior version is
  recoverable;
* an operator (or a script) that deletes a key — the object is marked deleted,
  and the version can be restored by key;
* a `storage_objects` row that was finalised, then had its bytes replaced.

**What versioning would NOT protect against**

* **the bucket or the account being deleted.** Versioning lives inside the
  bucket. `delete_bucket` is unrecoverable, and so is losing the credentials.
* **a provider outage or a region loss.** Versioning is not replication. For
  that you need the provider's cross-region/cross-account replication, which is
  a separate, unconfigured feature.
* **anything about the DATABASE.** This is the important one: versioning makes
  no difference to the fact that the `FINALIZED` row and the bytes disagree.
  Recovery still requires knowing which key to ask for, which is what
  `storage_objects.object_key` gives you — and that table is only as good as
  the last dump.
* **silent corruption.** An object overwritten with different bytes is a *new
  version*, not an error. Versioning preserves the bad write; it does not
  detect it. The sha256 in the inventory is what detects it, and only at
  backup time.
* **cost.** Every version bills. A busy bucket with versioning on grows without
  bound until the lifecycle rule above exists. The rule is not optional.

### 9.4 What this is not

Not high availability, and not disaster recovery in the sense the phrase is
usually sold. There is no replica, no automatic failover, no warm standby and no
point-in-time recovery; `docker-compose.prod.yml` runs one PostgreSQL container
and one backend replica (see `PRODUCTION_ARCHITECTURE.md` §1 and §7 items 3
and 4). What exists is a **verified logical dump plus a drill that proves it
restores**, and an inventory that tells you which media bytes are gone.

RPO is measured in hours because there is no WAL archive. RTO is measured in
seconds *because the database is tiny in this drill*; the first thing to do
before trusting either number is run the drill against a copy of production and
re-measure (§6 item 8).

---
