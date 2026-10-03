# Production Architecture — Work 16 §14

**What this document is.** The topology YMONEY actually has, and — for each
guarantee someone might assume it has — the **module** that enforces it. Where a
guarantee is *not* enforced, that is stated in the same place.

**What it is not.** Not a validated production design. Nothing in this repository
has been run behind a real ingress, across real replicas, or on cloud
orchestration. There is no Kubernetes manifest, no Terraform, no Helm chart and no
multi-region story here. The topology below is the one the shipped
`Dockerfile.backend` + `docker-compose*.yml` + `deploy/Caddyfile` describe, plus
the coordination layer the database provides. Section 8 lists exactly what is
unproven.

---

## 1. Topology

```
                    ┌───────────────────────────────────────────────────────┐
   internet         │  INGRESS / reverse proxy                             │
   :80  :443        │  deploy/Caddyfile,  caddy:2-alpine                   │
   ────────────────▶│    handle /api/*  →  reverse_proxy backend:8100      │
                    │    handle        →  root /srv  (frontend/dist)      │
                    │  ⚠ /livez, /readyz, /internal/* are NOT proxied here  │
                    └────────────────────────┬──────────────────────────────┘
                                             │ HTTP, inside the compose network
                    ┌────────────────────────▼───────────────────────────────┐
                    │  API REPLICAS  (shipped compose runs exactly ONE)     │
                    │  Dockerfile.backend → WORKDIR /app                     │
                    │  python -m uvicorn app.main:app --host 0.0.0.0        │
                    │                       --port 8100 --workers 1          │
                    │                                                       │
                    │  app/main.py lifespan, in order:                      │
                    │    1. refuse to boot if SECRET_KEY is the default      │
                    │    2. run_migrations(session)        (§2, one-way)     │
                    │    3. jobs.start_workers()           (worker pool)     │
                    │    4. autopilot.start_schedule_sweep()                │
                    │    5. recover_stale_cycles()                           │
                    │    6. telegram poller (if enabled)                    │
                    │  shutdown: drain pool → engine.dispose()              │
                    │  GET /livez /readyz /internal/{metrics,traces,slo,     │
                    │                            alerts,collectors,overview} │
                    └────┬────────────────┬──────────────────┬──────────────┘
                         │                │                  │
        ┌────────────────▼──────┐  ┌──────▼──────────┐  ┌────▼─────────────────┐
        │ PostgreSQL            │  │ OBJECT STORAGE  │  │ WORKER POOLS         │
        │ THE coordination      │  │ storage_objects │  │ in-process asyncio   │
        │ layer — see §3        │  │ PENDING→        │  │ tasks, one per slot: │
        │ jobs (+lease cols)    │  │ FINALIZED       │  │  SMALL CPU IO        │
        │ gpu_devices           │  │ .part →         │  │  PUBLISH             │
        │ gpu_reservations      │  │ os.replace      │  │  INTELLIGENCE        │
        │ storage_objects       │  │ FINALIZED row   │  │  RENDER (+GPU)       │
        │ cost_entries          │  │ written last    │  │ + ONE LeaseKeeper    │
        │ budget_rollup_limits  │  │ local or S3     │  │   thread (TTL/3)     │
        │ media_intel_*         │  └─────────────────┘  └────┬─────────────────┘
        │ lipsync_jobs, videos  │                            │ gpu_reservations
        │ schema_migrations     │                            ▼
        └───────────────────────┘              ┌────────────────────────────┐
                                               │ GPU WORKERS               │
                                               │ a replica with            │
                                               │   GPU_WORKER=true         │
                                               │ admission against         │
                                               │   gpu_devices.reserved_mb │
                                               └────────────────────────────┘
```

Redis (`redis:7-alpine` in `docker-compose.prod.yml`, `JOB_QUEUE=redis`) is an
**optional dispatch hint**, not the queue of record. The queue is the `jobs`
table; `app/services/jobs.py` has a Redis path
(`_claim_next_redis`, line 394) and a DB fallback, and
`BACKUP_RESTORE_RUNBOOK.md` §6 item 6 records that Redis state is not backed up
because it is re-derivable from `jobs`. Removing Redis does not lose work.

---

## 2. What runs where — the honest inventory

| Tier | In this repo | Process count in shipped compose |
|---|---|---|
| Ingress | `deploy/Caddyfile` (`caddy:2-alpine`) | 1 |
| API | `Dockerfile.backend`, `app.main:app` | **1** (`--workers 1`) |
| Database | `postgres:16-alpine` (`prod`) / SQLite file (default) | 1 |
| Dispatch hint | `redis:7-alpine` | 1 |
| Worker pools | `app/services/worker_pool.py`, **in-process** | same 1 as the API |
| GPU workers | same image, `GPU_WORKER=true` | **0 by default** |
| Frontend | `frontend/dist`, served by Caddy | 1 |

**Every worker pool lives inside an API process.** That is deliberate
(`worker_pool.py` module docstring): the expensive property of a distributed queue
— a worker that cannot be blocked by its neighbours — is bought with a `WHERE`
clause and a per-class limit, not with a service boundary. There is no separate
worker deployment in this repository and no flag that turns the pool off while
keeping the API up.

---

## 3. The coordination layer: which table owns what

Every cross-process guarantee in YMONEY is a row and a conditional `UPDATE`.
This is the map; the migration that created each table is in brackets.

| Table | Owns | Module |
|---|---|---|
| `jobs` (0035 adds `claimed_by`, `claimed_at`, `lease_expires_at`, `heartbeat_at`) | job state **and job ownership** | `services/job_leases.py` |
| `schema_migrations` | the migration ledger | `migrations/runner.py` |
| `gpu_devices` (0036) | GPU capacity: `total_mb`, `reserved_mb`, `enabled` | `services/gpu_scheduler.py` |
| `gpu_reservations` (0036) | a held slice of VRAM + its own lease | `services/gpu_scheduler.py` |
| `storage_objects` (0036) | object lifecycle: `PENDING` / `FINALIZED` / `DELETED` | `services/storage_objects.py` |
| `cost_entries` | the money ledger, incl. `detail_json.exposure_unknown` | `services/cost.py`, `services/paid_provider.py` |
| `budget_rollup_limits` (0037) | cross-category ceilings | `services/budget_rollup.py` |
| `videos.submission_state` (0033), `lipsync_jobs.execution_outcome` / `cost_outcome` (0034) | the structural record of a billable attempt | `services/paid_jobs.py`, `services/paid_executor.py` |

---

## 4. Where each guarantee is enforced

### 4.1 Job ownership → `services/job_leases.py`

**The guarantee:** no job is executed by two workers at once; a worker that dies
holding a job does not strand it.

**Why the blanket `recover_orphans()` was replaced.** The old function ran
`UPDATE jobs SET status='RETRYING' WHERE status='RUNNING'` on every worker boot,
with no condition at all (`services/jobs.py:216-243` documents the original
verbatim). With one process that was survivable. With two it is a live-work
incident: a second replica restarting for an unrelated deploy flips the first
replica's in-progress 30-minute render back onto the queue, and whichever
worker polls first runs it again — a duplicate render, a duplicate publish, and
for a paid render a second invoice. Nothing in the schema distinguished "orphaned
by a dead worker" from "actively running on a live worker", so the code had to
assume the rare case and break the common one.

**What replaced it.** A lease. `job_leases.claim_next` / `claim_by_id` take the
lease with **one conditional `UPDATE`** whose `rowcount` is the answer; there is
no read-then-write window for two workers to interleave in. On PostgreSQL the
candidate `SELECT` also gets `SKIP LOCKED`. `LeaseKeeper` (a **thread**, not a
task — handlers run in `asyncio.to_thread` and can block the loop for minutes)
renews every held lease at `TTL/3`. Only a lease whose `lease_expires_at <= now`
may be reclaimed, so **expiry is proof of absence, not a guess** —
`job_leases.reclaim_expired` (line 600) can therefore run on a timer from any
process, any number of times, which is exactly what
`worker_pool._start_reclaim_sweep` does every `JOB_RECLAIM_INTERVAL_SECONDS`.

Measured: see `WORKER_POOL_RUNBOOK.md` §4–§5, and the `worker kill` /
`queue interruption` drills in `PRODUCTION_LOAD_REPORT.md` §7.2.

**What the lease does NOT do.** It bounds *ownership*, not *throughput*. See §7.

### 4.2 Money authority → `services/paid_provider.py` (Work 15.9) + `services/budget_rollup.py` (§11)

**The guarantee:** a billable request is refused before it is sent unless a named
budget owner exists; and a reservation must satisfy every configured ceiling at
once.

Two distinct layers, and they answer different questions:

**Who pays (Work 15.9, `paid_provider.py`).** `resolve_ownership()` refuses a
billable operation that has no owner, raising `OwnerlessSpendRefused` — a
`BudgetExceededError` subclass, so every existing budget handler already treats
it as "nothing went out". The three hard stops: `WORKSPACE_OWNED` needs a
workspace; `SYSTEM_OWNED` needs an explicitly configured system budget
(`YMONEY_SYSTEM_BUDGET_USD`, else "unconfigured", never "free"); and
`EXPLICIT_NONBILLABLE` is a positive caller assertion that nothing infers. The
resolution is persisted with the reservation (`SpendOwnership.as_detail()`), so a
later audit sees whose budget this was judged against.

**How much, in total (§11, `budget_rollup.py`).** Before this, every cap answered
"may this **category** afford this?" and none answered "may this **workspace**
afford this?". A workspace with $5/day for `llm` and $5/day for `tts` has spent
$10 against limits that each read $5. `assert_within_rollups()` runs **after**
the Work 15.7 category/per-call caps, **in the same transaction, under the same
workspace lock**, so it does not re-implement them and cannot drift from them.
Cross-workspace system ceilings take their own `FOR UPDATE` lock, acquired always
workspace-then-system, because one fixed order cannot deadlock.

**Opt-in means NULL, and NULL is a real value.** No default ceiling hides in a
column default. A deployment that never writes a `budget_rollup_limits` row
behaves exactly as it did before §11 — verified: `rollup_headroom(session, "")`
returns `{"levels": [], "enforced": false}` on a stock database.

**What is deliberately not enforced here.** `budget_rollup` does not police
`settle_reservation`: a provider-reported actual above its estimate is money
*already spent*, and refusing the write would erase a real charge from the books.
Caps gate what may be **started**.

### 4.3 Storage atomicity → `services/storage_objects.py`

**The invariant, once:** *a temporary file may exist; it may never become
canonical state.*

**The order, and the order is the guarantee:**

1. stream bytes to a `.part` file in the **destination** directory
   (`STORAGE_STREAM_CHUNK_BYTES`, 4 MiB default — an upload is a stream, and a
   4 GB "buffer" is an OOM);
2. `fsync`, then `os.replace` — atomic within a filesystem;
3. **only then** write the `FINALIZED` row.

A crash at any point leaves either the old object or no object. It can never
leave a truncated file that claims to be complete.

**Enforced in two places**, because one place is a promise and two is a boundary:
`finalize()` refuses any source path inside `STORAGE_STAGING_DIR` (the staging
root is deliberately *outside* the managed media tree so a temp file can never be
resolved by `storage.managed_path`), and `resolve()` refuses any key that resolves
inside staging **and** any key belonging to another workspace — checked on both
the key's `{workspace}/…` prefix *and* the resolved filesystem path, so neither a
forged prefix nor a path traversal crosses the line.

`storage_objects` does **not** import `boto3`. S3 support stays behind
`services/storage.py`, so there is exactly one place in the codebase that knows
what a backend is.

Measured: `RENDER_BENCHMARKS.md` §"Streaming: memory does not track file size"
and §"Storage: what the render actually leaves behind"; the
`storage interruption` drill in `PRODUCTION_LOAD_REPORT.md` §7.2
(`finalized_leaked=0 canonical_intact=3 leftovers=0`, **simulated** — see §7.1).

### 4.4 GPU admission → `services/gpu_scheduler.py`

**The guarantee:** admission never oversubscribes a device, and every exit path
releases.

**Why VRAM and not a count.** `max_concurrent_gpu_jobs` sized a semaphore of N
slots. That is correct only while every job's footprint is uniform and unknown. A
4 GB segmentation job and a 300 MB lip-sync job both take "one slot", so five
lip-syncs admit onto a card with 6 GB free and the fifth dies of CUDA OOM — after
the money was spent, before any artifact exists. The failure mode is not "the
queue was busy"; it is "the queue lied about capacity". So capacity is expressed
the way the hardware expresses it: **VRAM on a named device.**

**The mechanism.** Admission is ONE conditional `UPDATE`:

```sql
UPDATE gpu_devices SET reserved_mb = reserved_mb + :need
 WHERE id = :id AND enabled = 1 AND (total_mb - reserved_mb) >= :need
```

`rowcount` is the answer — no read-then-write window, so two processes racing for
the last 4 GB cannot both win. `gpu_slot()` is a **context manager** and the body
does not run until the `UPDATE` has committed: admission before execution, not
run-then-hope. Success, failure, exception, cancellation, timeout and crash all
end in a `finally`; a crash cannot run a `finally`, which is why the reservation
carries its own lease and `reclaim_stale()` gives the VRAM back. When the
reservation names a `job_id`, liveness is asked of `job_leases` rather than
guessed, so the two recovery models cannot disagree about the same job.

**CPU fallback is opt-in twice** — the caller must declare `cpu_capable=True`
*and* the operator must set `GPU_CPU_FALLBACK_ENABLED`. Default off. A job that
needs a GPU and finds none gets `GpuUnavailable`, never a silent downgrade.

Measured: `RENDER_BENCHMARKS.md` §GPU. The `gpu worker death` drill
(`freed=1 live_kept=True re_admitted=…`) is the stale-reservation evidence path
only — **no real GPU workload ran**; see `PRODUCTION_LOAD_REPORT.md` §7.1.

### 4.5 Observability → `services/observability/`

Dependency-free and always importable; the env switches only control whether
sinks are attached.

| Module | Guarantee |
|---|---|
| `metrics.py` | in-process Prometheus-text registry; collectors are **isolated** (`collect_all()` wraps each in `try/except`), and a failed collector reports `ymoney_collector_up{collector=…} 0` and leaves its gauges untouched — zeroing them would read as "no backlog" during an outage |
| `logging_setup.py` | redacted structured JSON sink, added by `install()`; additive and idempotent, never removes loguru's stderr handler |
| `redaction.py` | applied **either way** once a structured sink is installed |
| `tracing.py` | bounded in-memory span recorder (`OBSERVABILITY_MAX_SPANS`, 2000) for `GET /internal/traces`; reports `backend: in_process_recorder` and `otel_installed` explicitly rather than implying OTLP |
| `slo.py` | SLO **targets** and alert-rule evaluators. `slo_catalog()` carries `"measured": false` |

**Wiring seams** (`app/main.py`): the `audit_middleware` binds/clamps an inbound
`X-Request-ID`, opens a server span, records latency and a 5xx counter, and labels
the metric with the **templated** route path — never the raw URL, which would let
any client mint an unbounded series set. `_route_template()` truncates an
unmatched path to 64 chars for the same reason.

Full detail: `PRODUCTION_OBSERVABILITY.md`.

---

## 5. The probe contract, and one thing that will bite you

| Endpoint | Question | 200 when | Never touches |
|---|---|---|---|
| `GET /livez` | should the supervisor restart me? | the event loop accepts work | **the database** |
| `GET /readyz` | should traffic be routed here? | 3 critical checks pass: `database`, `migrations`, `job_backend` | external providers |

`/livez` deliberately never touches the DB: a liveness probe that fails because
Postgres is slow converts a dependency outage into a crash loop and destroys the
evidence. `/readyz` gates on three **critical** dependencies only; every external
provider is reported under `degraded` with readiness staying **200**, because
pulling every replica out of the load balancer because one vendor has an incident
converts a partial outage into a total one.

> ### ⚠ The shipped ingress does not proxy the probes
>
> `deploy/Caddyfile` routes `handle /api/*` to the backend. `/livez`, `/readyz`
> and `/internal/*` **do not match**, so they fall through to
> `handle { root * /srv; try_files {path} /index.html; file_server }`.
>
> A probe pointed at the public host therefore gets **HTTP 200 with the SPA's
> `index.html`** — a false-healthy result that will not go away when the backend
> is down.
>
> Probe the backend container directly (`backend:8100` from inside the compose
> network, or `docker compose exec`). See `DEPLOYMENT_RUNBOOK.md` §5.

Verified on this repository: `/livez`, `/readyz`, `/internal/metrics`,
`/internal/alerts`, `/internal/slo`, `/internal/collectors`, `/internal/overview`
and `/internal/traces` all return 200 when called on the app; `/internal/metrics`
returns `text/plain; version=0.0.4; charset=utf-8`.

---

## 6. Configuration → module map

Every setting is an env var (`app/core/config.py`, `BaseSettings`, no prefix:
field name upper-cased). Verified by binding all 48 of the names below against
`Settings(_env_file=None)`.

| Prefix | Reads |
|---|---|
| `DB_POOL_*` | `db.py` — real pool + bounds on PostgreSQL; SQLite gets `pool_size`, `max_overflow=0` and its own file lock |
| `JOB_LEASE_*`, `JOB_RECLAIM_*`, `JOB_WORKER_POOLS`, `JOB_DRAIN_*`, `JOB_WORKER_IDENTITY`, `JOB_WORKER_COUNT`, `JOB_POLL_*` | `job_leases.py`, `worker_pool.py` |
| `GPU_SCHEDULER_ENABLED`, `GPU_SLOT_LEASE_*`, `GPU_ADMISSION_TIMEOUT_*`, `GPU_CPU_FALLBACK_ENABLED`, `GPU_DEVICE_KEYS`, `GPU_WORKER` | `gpu_scheduler.py` |
| `STORAGE_BACKEND`, `S3_*`, `STORAGE_STREAM_CHUNK_BYTES`, `STORAGE_STAGING_DIR`, `STORAGE_PENDING_TTL_SECONDS` | `storage.py`, `storage_objects.py` |
| `OBSERVABILITY_*` | `observability/*` |
| `ALERT_*`, `SLO_*` | **declared but not read by any rule** — see §7 |
| `BACKUP_*` | `scripts/backup_restore.py` |
| `BUDGET_ROLLUP_SYSTEM_*` | `budget_rollup.py` (only when no `budget_rollup_limits` system row exists) |

**`storage_objects.py` never imports `boto3`**; S3 stays behind
`services/storage.py`.

---

## 7. Honest limitations

1. **Worker pool slots bound concurrency *per process*.** With N API replicas the
   effective concurrency is **N × slots**. The lease bounds *ownership*, not
   throughput. This is stated in `WORKER_POOL_RUNBOOK.md` §10 and is not fixed by
   any setting in `config.py` — there is no distributed rate limiter.
2. **GPU capacity comes from the `gpu_devices` table, not from config
   constants.** `max_concurrent_gpu_jobs` still exists and still gates whether a
   process claims GPU work at all, but how much VRAM a job may hold comes from
   the row. On a stock database `gpu_scheduler.snapshot()` returns
   `devices: []` — verified — until `sync_devices()` / `ensure_cpu_device()` /
   `register_device()` populates it.
3. **Do not call this highly available.** Nothing was run behind a real ingress,
   across real replicas, or through a supervisor that restarts a wedged process.
   The `API restart` drill in `PRODUCTION_LOAD_REPORT.md` §7.1 is a forced
   `taskkill` plus a genuinely cold interpreter, and is labelled as such.
4. **No cloud or Kubernetes validation exists in this repository.** There is no
   manifest. `docker-compose.prod.yml` is a Compose file, not a validated
   deployment; it has been *config-validated* (`docker compose config
   --services` → `postgres, redis, backend, web`), not *run* in this lane.
5. **`ALERT_*` and `SLO_*` env vars do not change what fires.** `slo.py`
   hard-codes every threshold as a literal (`total > 1.0`, `idle > 900.0`,
   `worst >= 3.0`). Grepping the backend for `settings.alert_` / `settings.slo_`
   returns **no matches**. The defaults in `config.py` coincidentally equal the
   literals, which is why this is easy to believe. Retuning an alert today means
   a code change — see `INCIDENT_RUNBOOK.md` §0.
6. **The `gpu_queue_starvation` rule's runbook string names a table that does not
   exist.** It says "check `MEDIA_INTEL_GPU_SLOT` rows". The real tables are
   `gpu_devices` and `gpu_reservations` (0036); `media_intel_runs` is the
   media-intelligence run table. Verified absent from the schema.
7. **`STORAGE_ROOT` is a module constant, not a setting.**
   `services/storage.py:18` is `STORAGE_ROOT = Path("data/videos")` — a
   **relative** path resolved against the process working directory. The image's
   `WORKDIR` is `/app`, so local media lands in `/app/data/videos`, which is
   **not** on the `ymoney-data` volume the compose file mounts at `/data`.
   See `DEPLOYMENT_RUNBOOK.md` §4.
8. **Rollback of the schema is not supported.** `run_migrations` has no `down`.
   See `DEPLOYMENT_RUNBOOK.md` §6 for exactly what "roll back" means.
9. **`ALLOWED_MOCK`/provider flags are real switches with real blast radius.**
   `ALLOW_MOCK_IN_PRODUCTION` lets the factory build a labeled mock engine in
   production; `ALLOW_PRIVATE_CONNECTORS` permits private-target source fetches.
   Both default **off**. Do not set them to "get unstuck".
10. **No PITR / WAL archiving.** See `BACKUP_RESTORE_RUNBOOK.md` §6 items 2 and
    5. RPO is hours.

---

## 8. Cross-references

| Document | Covers |
|---|---|
| [`WORKER_POOL_RUNBOOK.md`](WORKER_POOL_RUNBOOK.md) | lease lifecycle, workload classes, drain, paid re-entry, operator queries |
| [`PRODUCTION_OBSERVABILITY.md`](PRODUCTION_OBSERVABILITY.md) | metrics, redaction, tracing, `/livez` `/readyz`, SLO + alert rules |
| [`PRODUCTION_LOAD_REPORT.md`](PRODUCTION_LOAD_REPORT.md) | measured limits, the bottleneck, 8 chaos drills (each labelled real vs simulated) |
| [`BACKUP_RESTORE_RUNBOOK.md`](BACKUP_RESTORE_RUNBOOK.md) | real restore drill, measured RPO/RTO, what is **not** covered |
| [`RENDER_BENCHMARKS.md`](RENDER_BENCHMARKS.md) | real render measurements, streaming, GPU |
| [`DEPLOYMENT_RUNBOOK.md`](DEPLOYMENT_RUNBOOK.md) | deploy and rollback procedures |
| [`INCIDENT_RUNBOOK.md`](INCIDENT_RUNBOOK.md) | 3am procedures: drain, stuck job, unknown paid submission, DB, storage, provider, restore |
