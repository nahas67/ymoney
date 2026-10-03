# Deployment Runbook — Work 16 §14

Deploy and rollback procedure for the YMONEY backend + frontend.

**Read this first, it changes what "rollback" means.**
`run_migrations` (`backend/app/migrations/runner.py`) is a **custom ordered Python
migration runner, not Alembic**, and it has **no down/rollback support**.
Migrations are **one-way**. §6 is the precise statement of what redeploying the
previous image does and does not buy you. If you take one thing from this
document, take that one.

Every command below was executed against this repository. The verification
method for each is in §9. Anything not verified is marked as such.

---

## 1. Pre-deploy checks

Run these **before** the deploy window opens. Each maps to a fact in the code.

### 1.1 The secret gate is real and will refuse to boot

`app/main.py:100` raises at lifespan start if `YMONEY_ENV=production` and
`YMONEY_SECRET_KEY` still starts with `change-me`:

```python
if settings.is_production and settings.secret_key.startswith("change-me"):
    raise RuntimeError(
        "YMONEY_SECRET_KEY must be set to a strong random value in production"
    )
```

Generate one and put it in the secret store, never in the repo:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 1.2 Take a backup first, and **verify** it

A backup that has never been restored is not a backup. The commands:

```bash
cd /opt/ymoney/backend
python -m app.scripts.backup_restore --dsn "$DATABASE_URL" \
    --out /var/backups/ymoney/$(date -u +%Y%m%dT%H%M%SZ) backup
```

`verify` takes a `--database`, so you can verify a backup **without** restoring it
into anything:

```bash
python -m app.scripts.backup_restore --dsn "$DATABASE_URL" \
    --out /var/backups/ymoney/<timestamp> verify --database <db-name>
```

Exit codes: `0` verified, `1` verification FAILED, `2` the tool could not run.
**A non-zero exit here blocks the deploy.**

Full drill, timings and RPO/RTO: [`BACKUP_RESTORE_RUNBOOK.md`](BACKUP_RESTORE_RUNBOOK.md).

### 1.3 Confirm the pool fits under `max_connections`

`config.py` states the rule and `internal_ops._check_database` repeats it in its
remediation text: `DB_POOL_SIZE + DB_MAX_OVERFLOW` is a hard ceiling on
concurrent connections **per process**, and it must stay below the server's
`max_connections` with headroom for the migration runner and any operator
session.

The shipped defaults are `10 + 10 = 20` per process. With N API replicas the true
demand is **N × 20**. On the PostgreSQL 17.11 server used for
[`PRODUCTION_LOAD_REPORT.md`](PRODUCTION_LOAD_REPORT.md) (`max_connections = 100`),
that is comfortable at 1–2 replicas and wrong at 5.

```bash
psql "$DATABASE_URL" -c "show max_connections"
```

### 1.4 Know your migration count

The runner applies every module in `app/migrations/versions/` that has an
`upgrade(session)` function, in lexical filename order, skipping anything already
in `schema_migrations`. Read the ledger and the on-disk set **before** you deploy:

```bash
cd /opt/ymoney/backend
python -c "
from app.db import session_scope
from app.migrations.runner import applied_versions, load_migrations
with session_scope() as s:
    print('applied:', len(applied_versions(s)))
print('on disk:', len(load_migrations()))
"
```

Compare `on disk` with `applied` **on the current image**, then again after the
deploy. A gap that grows is a migration that failed.

> **Known benign warning.** `migration sequence collision on prefix 0003:
> 0003_video_progress, 0003_video_thumbnails` is emitted on **every** run. It is
> a warning, not a failure: the runner keys applied state on the full filename,
> and both migrations are additive column guards. See
> `app/migrations/versions/README.md`.

### 1.5 Compose will not even start without the full variable set

`docker-compose.prod.yml` uses `${VAR:?message}` for three variables. Compose
interpolates the **whole file** before doing anything, so a missing value fails
`ps` and `logs` too, not just `up`:

```bash
$ docker compose -f docker-compose.prod.yml ps
error while interpolating services.backend.environment.DATABASE_URL:
required variable POSTGRES_PASSWORD is missing a value: set a db password
```

Required: `YMONEY_SECRET_KEY`, `POSTGRES_PASSWORD`, `CORS_ALLOWED_ORIGINS`.
Export them from your secret store before **any** compose command, or pass
`--env-file /path/to/prod.env`.

```bash
docker compose -f docker-compose.prod.yml config --services
# postgres
# redis
# backend
# web
```

### 1.6 Know what the shipped compose file does *not* configure

`docker-compose.prod.yml` predates Work 16 and passes through **none** of the
pool / lease / GPU / storage / observability / backup settings. A deploy with the
shipped file therefore runs on the `config.py` **defaults**:

`JOB_LEASE_SECONDS=120`, `JOB_RECLAIM_INTERVAL_SECONDS=15`,
`JOB_WORKER_POOLS=""` (the default plan: 4 SMALL + 1 each of CPU/IO/PUBLISH/
INTELLIGENCE/RENDER = 9 slots, no GPU), `JOB_DRAIN_TIMEOUT_SECONDS=30`,
`GPU_SCHEDULER_ENABLED=true`, `STORAGE_PENDING_TTL_SECONDS=3600`,
`OBSERVABILITY_ENABLED=true`. Those defaults are safe and are what
`PRODUCTION_OBSERVABILITY.md` and `WORKER_POOL_RUNBOOK.md` were measured against.

To tune anything, add it to the `backend.environment:` block yourself. Names are
the `config.py` field names upper-cased; all 48 are listed in
`PRODUCTION_ARCHITECTURE.md` §6.

---

## 2. Build

```bash
cd /opt/ymoney
docker compose -f docker-compose.prod.yml build
```

The image is `Dockerfile.backend`: `python:3.12-slim`, `apt-get install ffmpeg`,
`WORKDIR /app`, `pip install backend/pyproject.toml`, `COPY backend/app ./app`,
`EXPOSE 8100`.

Build the frontend separately — Caddy serves `frontend/dist` as a read-only bind
mount, so the container will happily serve a **stale or missing** bundle:

```bash
cd /opt/ymoney/frontend && npm ci && npm run build
```

`npm run build` is also the type-check gate; CI runs exactly this
(`.github/workflows/ci.yml`, job `frontend`). If it fails, the bundle you have is
the one already in `frontend/dist`.

---

## 3. Migrations — the one-way step

### 3.1 There is no separate migration command

`grep` for `alembic` in `backend/pyproject.toml`: **no match**. There is no
`alembic.ini`, no `env.py`, no `__main__.py` under `app/migrations/`. The runner
is invoked from exactly one place: `app/main.py:105`, inside the lifespan.

```python
with session_scope() as session:
    applied = run_migrations(session)
if applied:
    logger.info(f"migrations applied: {applied}")
```

**Consequence for deployment:** migrations run **inside the API container, on
boot, before the app serves anything**. `run_migrations` calls
`Base.metadata.create_all()` first (so a fresh install gets every table), then
applies pending migrations **one at a time, each in its own transaction**, writing
the filename into `schema_migrations` in the *same* transaction as the migration
body. A failure rolls back that migration and re-raises — the ones already applied
stay applied.

### 3.2 Running the step ahead of the rollout (optional but recommended)

You can run the same runner standalone, against the same DSN, so a slow or
failing migration is discovered **before** a new replica starts:

```bash
cd /opt/ymoney/backend
python -c "
from app.db import session_scope
from app.migrations.runner import run_migrations, applied_versions
with session_scope() as s:
    print('already applied:', len(applied_versions(s)))
    print('pending now:', run_migrations(s))
"
```

On an up-to-date database this prints `pending now: []` and exits 0 — verified.
**This is the deploy gate:** a non-empty `pending now` that then raises, or a
non-zero exit, stops the deploy.

⚠️ Do not run it concurrently with a booting replica. Two runners racing on the
same `schema_migrations` table is not a supported configuration; there is no
advisory lock in `runner.py`.

### 3.3 What the runner does not do

* **No down migrations. No `alembic downgrade`. No auto-revert.** See §6.
* **No `precondition` / `expected_current` gate** — it does not check that the
  ledger is at a version you expect before applying.
* **No checksum.** `schema_migrations` stores `version` and `applied_at` only
  (`runner.py:22-30`). An *edited* migration body is invisible to the runner: it
  will happily re-apply a modified file under a **new** filename and never tell
  you the old one means something different now.
* **No squashing.** Every historical migration is applied to a fresh install.

---

## 4. Roll out

### 4.1 Drain before you replace

An in-flight render is money. `worker_pool.WorkerPool.drain()` stops claiming,
lets in-flight work finish, and past `JOB_DRAIN_TIMEOUT_SECONDS` **cancels the
worker tasks without cancelling their jobs** — the job stays `RUNNING` with a
lease that stops being renewed, so the next recovery sweep reclaims it exactly as
it would reclaim a crashed worker.

Give renders enough room:

```bash
docker compose -f docker-compose.prod.yml stop -t 120 backend
```

`stop -t 120` is longer than the 30 s default drain, so the graceful path
completes before Docker escalates. Procedure and evidence:
[`WORKER_POOL_RUNBOOK.md`](WORKER_POOL_RUNBOOK.md) §7.

### 4.2 Start the new replica

```bash
docker compose -f docker-compose.prod.yml up -d --no-deps backend
```

`--no-deps` keeps a redeploy of `backend` from also restarting `postgres` or
`redis` and bouncing every live connection.

### 4.3 ⚠ Local media is not on the volume

`services/storage.py:18` is `STORAGE_ROOT = Path("data/videos")` — a **relative
path resolved against the process working directory**, which the image sets to
`/app`. The compose file mounts the named volume at `/data`, and the SQLite DSN
points there (`sqlite:////data/ymoney.db`), so **the database persists but the
media tree does not**: local renders live on the container's writable layer and
are destroyed by `docker compose down`, a volume prune, or any container
recreate.

Two supported fixes, pick one before the first production render:

```yaml
# (a) put the media tree on a volume — cheap, keeps STORAGE_BACKEND=local
backend:
  volumes:
    - ymoney-data:/data
    - ymoney-media:/app/data      # <-- add this
```

```bash
# (b) or move the bytes off the box entirely
STORAGE_BACKEND=s3
S3_BUCKET=<bucket>
S3_ENDPOINT_URL=<endpoint>
S3_ACCESS_KEY=<key>
S3_SECRET_KEY=<key>
```

With (b), verify the backend: `STORAGE_AVAILABLE` and
`ymoney_collector_up{collector="storage"}` must both be `1` in §5.2. A
`FINALIZED` row whose bytes are gone is a database that lies about what it has —
that is item 1 in `BACKUP_RESTORE_RUNBOOK.md` §6.

### 4.4 The GPU lane is a separate replica, and it is optional

`docker-compose.prod.yml` sets `GPU_WORKER: ${GPU_WORKER:-false}`. With
`GPU_WORKER=false` the default pool plan allocates **no GPU slot at all**, so
GPU-gated jobs are deferred, not run. Capacity comes from the `gpu_devices`
**table**, which is empty on a stock database — verified:

```
$ python -c "from app.services import gpu_scheduler as gs; print(gs.snapshot())"
{'enabled': True, ..., 'devices': [], 'held_reservations': 0, 'total_vram_mb': 0, ...}
```

Populate it on the GPU host:

```bash
python -c "from app.services import gpu_scheduler as gs; print(gs.probe_devices()); print(gs.sync_devices())"
python -c "from app.services import gpu_scheduler as gs; print(gs.ensure_cpu_device()); print(gs.snapshot())"
```

Also note `MPT_BASE_URL: http://video-engine:8080` is hard-coded in the prod
compose file and **no `video-engine` service is defined in it**. That is only
read when `VIDEO_ENGINE=moneyprinterturbo`; the default `ffmpeg_avatar` renders
in-process and never looks at it. If you set `VIDEO_ENGINE=moneyprinterturbo`
you must start that service yourself.

---

## 5. Verify the deploy

### 5.1 Probe the backend directly — not through Caddy

`deploy/Caddyfile` proxies `handle /api/*` only. `/livez` and `/readyz` do not
match, so a probe at the public host falls through to
`try_files {path} /index.html` and gets **HTTP 200 with the SPA's `index.html`**.
That is a false-healthy probe and it will not go away when the backend is down.

```bash
docker compose -f docker-compose.prod.yml exec backend \
  python -c "
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as c:
    for p in ('/livez','/readyz'):
        r = c.get(p); print(p, r.status_code, r.json().get('status'))
"
```

Or, over the network from any container on the same compose network:

```bash
python -c "
import urllib.request, json
for p in ('/livez','/readyz'):
    with urllib.request.urlopen('http://backend:8100'+p) as r:
        print(p, r.status, json.load(r)['status'])
"
```

**`/readyz` must report `"status": "ready"` and `"blocking_failures": []`.**
A 503 means one of three critical checks failed, and the failing check's
`detail` + `remediation` are in the payload:

| `blocking_failures` contains | Meaning | First move |
|---|---|---|
| `database` | no pooled connection / `SELECT 1` failed | §1.3 pool vs `max_connections`; disk; DSN |
| `migrations` | `schema_migrations` missing, unreadable, or **empty** | §3.2 — the ledger exists but records nothing, so the schema is behind the code |
| `job_backend` | the `jobs` table cannot be queried, so no job can ever be claimed | the database is usable in a way `SELECT 1` does not exercise |

`degraded: ["storage"]` and `providers_down` are **non-blocking** and leave
readiness at 200 by design. See `PRODUCTION_OBSERVABILITY.md` §9.

### 5.2 Scrape metrics

```bash
docker compose -f docker-compose.prod.yml exec backend \
  python -c "
from app.services.observability import metrics as m
print(m.collect_all())
print(len(m.REGISTRY.render().splitlines()), 'metric lines')
"
```

Expect, minimum:

```
{'workers': True, 'database': True, 'jobs': True, 'gpu_slots': True, 'storage': True}
```

`workers: False` means the pool is not running **in that process** — which is
expected for `exec` (a fresh process has no pool) and is *not* evidence of an
incident. Read worker liveness from `GET /internal/alerts` on the **running**
service instead.

Live checks worth eyeballing:

```
ymoney_collector_up{collector="database"} 1
ymoney_storage_available 1
ymoney_paid_submission_unknown_total 0      <- any non-zero value is an INCIDENT
ymoney_paid_unknown_exposure_usd 0
ymoney_build_info ...
```

### 5.3 Read the alert verdicts

```bash
docker compose -f docker-compose.prod.yml exec backend \
  python -c "
import json
from app.services.observability import slo
print(json.dumps(slo.alert_catalog(), indent=2))
"
```

**No alert rule must be firing after a deploy.** `paid_submission_unknown`,
`unknown_exposure_high`, `queue_stalled`, `worker_fleet_unavailable`,
`db_unavailable`, `storage_unavailable` are `CRITICAL`;
`repeated_publish_failure`, `gpu_queue_starvation` are `WARNING`.

⚠️ **Alert thresholds are not configurable.** Every threshold in `slo.py` is a
literal (`total > 1.0`, `idle > 900.0`, `worst >= 3.0`, `up < 1.0`). Grepping the
backend for `settings.alert_` / `settings.slo_` returns **no matches**, so setting
`ALERT_UNKNOWN_EXPOSURE_USD=10` changes nothing about what fires. If a rule
fires when you expected it not to, that is a code change, not a config change.

### 5.4 Confirm the pool started with the shape you intended

```bash
docker compose -f docker-compose.prod.yml logs backend | grep "job worker pool"
```

```
job worker pool <host>:<pid>:<nonce> starting: CPU=1, INTELLIGENCE=1, IO=1,
PUBLISH=1, RENDER=1, SMALL=4
```

That is the default plan (verified on this repository). If you set
`JOB_WORKER_POOLS`, the line reads your spec instead. If `GPU_WORKER=true` and
`GPU` is absent, the pool is not allocating GPU slots.

---

## 6. Rollback — the precise version

### 6.1 What the runner actually is

```python
# app/migrations/runner.py:90-104
for name, mod in load_migrations():
    if name in applied:
        continue
    try:
        mod.upgrade(session)
        session.execute(text("INSERT INTO schema_migrations (version) VALUES (:v)"), {"v": name})
        session.commit()
        done.append(name)
    except Exception:
        session.rollback()
        logger.exception(f"migration {name} failed")
        raise
```

**One transaction per migration. One ledger row per filename. No `down` function
is ever read, because none is looked for.** `load_migrations()` only collects
modules that `hasattr(mod, "upgrade")`. If you want to roll the schema back, the
code that could do it does not exist.

### 6.2 What redeploying the previous image actually does

**The database is already migrated and stays migrated.** Redeploying the old
image does not un-apply anything; `schema_migrations` still lists the new
versions and `run_migrations` on the old image will simply find nothing pending
(it cannot know about files it does not have). So:

**Rolling back the image is safe only if that image's code is compatible with
the already-migrated schema.**

| The migration did | Rolling back the image |
|---|---|
| **Added** a table | **Safe.** Old code never asks for it. The extra table is inert. |
| **Added** a column with a default / nullable | **Usually safe.** Old code does not select it explicitly in most cases. Verify the specific query paths — `SELECT *` mapped to a dataclass, or an `INSERT` with a NOT NULL column the old code does not populate, will break. |
| **Added** an index | **Safe.** Indexes are invisible to code. |
| **Backfilled** existing rows with a value | **Usually safe, and often necessary** — old code that reads a newly-required column will not crash if the backfill gave it something. |
| **Renamed** a column | **NOT safe.** Old code names the old column; it does not exist. |
| **Dropped** a column or table | **NOT safe.** Old code names it. This is the case that turns a rollback into an outage. |
| **Changed a column type / narrowed it** | **NOT safe.** Old code may write a value the new type rejects. |
| **Added a NOT NULL column with no default** | **NOT safe** — old code's `INSERT` omits it. |

**The failure is loud, not silent:** a missing column raises at query time
(`UndefinedColumn` / `OperationalError`), the request 500s, and `/readyz` may go
`not_ready`. But it fails *after* you have rolled back, on live traffic.

### 6.3 Decision tree

```
Deploy failed or regressed.
│
├─ Was any migration applied by THIS deploy?
│   │
│   ├─ NO  → the schema is unchanged. Redeploy the previous image freely.
│   │        This is the common case for an application-only change.
│   │
│   └─ YES → read every migration in the applied list, and classify each:
│       │
│       ├─ ALL are additive (new table / new nullable-or-defaulted column /
│       │  new index / backfill)
│       │    → redeploy the previous image is SAFE, provided you check the
│       │      specific queries above. Verify, do not assume.
│       │
│       ├─ ANY drops, renames, narrows a type, or adds NOT NULL without default
│       │    → DO NOT redeploy the previous image.
│       │      The schema has moved past it.
│       │      Options, in order of preference:
│       │        1. FORWARD-FIX. Ship a new image that understands the new
│       │           schema. This is the correct answer most of the time.
│       │        2. Restore from backup (§6.5). Costs RPO.
│       │        3. Hand-write the inverse DDL. There is no tooling for it,
│       │           no record of it, and it can silently disagree with the
│       │           data the migration backfilled.
│       │
│       └─ Not sure which category? Treat as NOT safe.
│
└─ The deploy never completed / app will not boot
   → the failure may be a half-applied migration. Go to §6.4.
```

### 6.4 A failed migration leaves a *consistent* database, not a broken one

Each migration is one transaction. A migration that raises rolls back **itself**
and re-raises; the already-applied ones remain applied and remain in the ledger.
So a failed deploy leaves you at a **known schema version** — the last migration
that committed.

```bash
cd /opt/ymoney/backend
python -c "
from app.db import session_scope
from app.migrations.runner import applied_versions, load_migrations
with session_scope() as s:
    print(sorted(applied_versions(s))[-5:])
print('on disk:', [n for n,_ in load_migrations()][-5:])
"
```

What you **cannot** infer from this: a migration that ran `CREATE INDEX
CONCURRENTLY`-style non-transactional DDL, or one whose body committed
internally, may have left partial objects. Check `schema_migrations` against the
migration's own docstring. There is no `down` to run and no `alembic history` to
read.

The one genuinely unsafe residue is
`Base.metadata.create_all()`: it runs **before** the migrations and is not
transactional with them. On a fresh database it creates every table the models
declare, so a later migration that assumes a table does not exist may find it
there. This is a known, documented property of the runner, not a new finding.

### 6.5 Recovery is restore-from-backup

Because there is no supported down path, **the restore drill is the rollback
mechanism for anything schema-destructive.** Procedure, measured RTO (~17 s on the
16 MB test database) and the exact commands:
[`BACKUP_RESTORE_RUNBOOK.md`](BACKUP_RESTORE_RUNBOOK.md).

Two things to know before you need it:

* `restore()` **refuses to target the source database**. Restore into a new name,
  verify, then cut over.
* The **media bytes are not in the backup** — only the `storage_objects`
  inventory and checksums. A restored `FINALIZED` row whose bytes are gone is a
  database that lies about what it has (12-item list in `BACKUP_RESTORE_RUNBOOK.md`
  §6; that is item 1).

---

## 7. Rollback decision table (fast)

| Symptom | Action |
|---|---|
| `/readyz` 503, `blocking_failures: ["database"]` | **Not** a rollback trigger. Fix the DB / pool. |
| `/readyz` 503, `blocking_failures: ["migrations"]` | Ledger empty or unreadable. Run §3.2 and read its output. |
| `/readyz` 503, `blocking_failures: ["job_backend"]` | `jobs` unreadable. Database problem, not deploy problem. |
| `db_unavailable` firing | Database problem. See [`INCIDENT_RUNBOOK.md`](INCIDENT_RUNBOOK.md) §D. |
| `storage_unavailable` firing | Local media volume gone/read-only. See §4.3 and `INCIDENT_RUNBOOK.md` §E. |
| `queue_stalled` firing | Backlog + nothing started for 900 s. See `INCIDENT_RUNBOOK.md` §B. |
| `worker_fleet_unavailable` firing | `start_workers()` did not complete, or every worker task died. Check the lifespan log. |
| `paid_submission_unknown` firing | **Never** roll back for this. See `INCIDENT_RUNBOOK.md` §C. |
| Repeated `UndefinedColumn` after redeploy | You rolled back across a rename/drop. Forward-fix or restore. |
| Frontend serves an old bundle | Not a backend rollback. Rebuild `frontend/dist`. |

---

## 8. Commands — verification method

| Command | Verified how |
|---|---|
| `python -m app.scripts.backup_restore --help` | executed; exit 0; printed `{backup,restore,verify,drill}` and `--dsn/--container/--out` |
| `… verify --help` | executed; `--from`, `--database` |
| `… restore --help` | executed; `--target-db` (required), `--replace`, `--from` |
| `… drill --help` | executed; `--target-db --mutate-table --mutate-column --mutate-value --mutate-id` |
| migration-ledger `python -c` (§1.4, §3.2, §6.4) | executed against the repo DB; printed `already applied: 37`, `pending now: []` |
| `docker compose -f docker-compose.prod.yml config --services` | executed (Docker 29.5.3, Compose **v5.1.4**); printed `postgres redis backend web` |
| `docker compose … ps` without `POSTGRES_PASSWORD` | executed; exit 1 with the quoted interpolation error |
| `/livez`, `/readyz`, `/internal/*` via `TestClient` | executed against a real lifespan boot; all **200**; `/internal/metrics` returned `text/plain; version=0.0.4; charset=utf-8`, 273 lines |
| `metrics.collect_all()` / `REGISTRY.render()` | executed; `{'workers':…, 'database': True, 'jobs': True, 'gpu_slots': True, 'storage': True}` |
| `slo.alert_catalog()` | executed; endpoint `/internal/alerts` returned 200 |
| `gpu_scheduler.probe_devices()/sync_devices()/ensure_cpu_device()/snapshot()/sweep()` | executed; `snapshot()` on a stock DB returned `devices: []` |
| `worker_pool.default_plan(4).as_text()` | executed; `CPU=1, INTELLIGENCE=1, IO=1, PUBLISH=1, RENDER=1, SMALL=4` |
| 48 env-var names | bound through `Settings(_env_file=None)`; all 48 accepted (differences were only `str` → `float`/`bool` coercion) |

**Not verified, and therefore not claimed:** `docker compose up -d` end to end,
`docker compose exec` against a running stack, TLS termination, and any
Kubernetes/managed-platform path. Docker and Compose are installed on this host;
the stack was not brought up by this lane.

---

## 9. What this document does not cover

* **The root [`DEPLOYMENT.md`](../DEPLOYMENT.md)** is the short developer-facing
  version (compose files, ports, volumes). This document is the production
  procedure and the migration/rollback semantics.
* **Backup contents, RPO/RTO and the drill** →
  [`BACKUP_RESTORE_RUNBOOK.md`](BACKUP_RESTORE_RUNBOOK.md).
* **Worker drain, leases, paid re-entry** →
  [`WORKER_POOL_RUNBOOK.md`](WORKER_POOL_RUNBOOK.md).
* **Alert rules and their hard-coded thresholds** →
  [`PRODUCTION_OBSERVABILITY.md`](PRODUCTION_OBSERVABILITY.md) §10.
* **Live incidents** → [`INCIDENT_RUNBOOK.md`](INCIDENT_RUNBOOK.md).
