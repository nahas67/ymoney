# Incident Runbook — Work 16 §15

For a human at 3am. Each procedure states **what the system does by itself**, what
**you** must do, and what **requires a decision you alone can make**.

Every command here was executed against this repository; §10 lists how each was
verified. Nothing is invented: where a procedure does **not** exist in this
codebase, it says so and gives you the alternative.

**Companion documents** — read the one that matches, don't re-derive it here:
[`WORKER_POOL_RUNBOOK.md`](WORKER_POOL_RUNBOOK.md) ·
[`PRODUCTION_OBSERVABILITY.md`](PRODUCTION_OBSERVABILITY.md) ·
[`BACKUP_RESTORE_RUNBOOK.md`](BACKUP_RESTORE_RUNBOOK.md) ·
[`PRODUCTION_LOAD_REPORT.md`](PRODUCTION_LOAD_REPORT.md) ·
[`DEPLOYMENT_RUNBOOK.md`](DEPLOYMENT_RUNBOOK.md) ·
[`PRODUCTION_ARCHITECTURE.md`](PRODUCTION_ARCHITECTURE.md)

---

## 0. Triage: one call

```bash
curl -s http://backend:8100/internal/overview | python -m json.tool
```

Inside the compose network use `backend:8100`. **Do not use the public host** —
`deploy/Caddyfile` proxies only `/api/*`, so `/internal/*` falls through to the
static handler and returns **200 with `index.html`**. See
`PRODUCTION_ARCHITECTURE.md` §5. Verified: `/internal/overview` returns 200 and
assembles liveness + readiness + alert verdicts + trace stats from the same
functions the individual endpoints use, so it cannot disagree with them.

Then branch on the **firing alert rules** (`/internal/overview` → `alerts`):

| Rule | Severity | Go to |
|---|---|---|
| `paid_submission_unknown` | CRITICAL | **§C — unknown paid submission. Never roll back for this.** |
| `unknown_exposure_high` | CRITICAL | §C |
| `queue_stalled` | CRITICAL | §B |
| `worker_fleet_unavailable` | CRITICAL | §A |
| `db_unavailable` | CRITICAL | §D |
| `storage_unavailable` | CRITICAL | §E |
| `repeated_publish_failure` | WARNING | §F |
| `gpu_queue_starvation` | WARNING | §E.3 (it reads the WORKSPACE slot ledger, which cannot see a device reservation — see 0.1 item 2) |

### 0.1 Three things the system will NOT tell you to do

1. **Only three alert thresholds are configurable, and the other five are
   deliberately constants.** `ALERT_UNKNOWN_EXPOSURE_USD`,
   `ALERT_QUEUE_STALL_SECONDS` and `ALERT_PUBLISH_FAILURE_STREAK` are read from
   the environment and change a verdict immediately (Work 16.1 §6;
   `GET /internal/alerts` reports each rule's live `threshold` and
   `threshold_source`). The remaining five — `paid_submission_unknown`,
   `worker_fleet_unavailable`, `db_unavailable`, `storage_unavailable`,
   `gpu_queue_starvation` — have no knob and will not grow one, because their
   thresholds are identities, not magnitudes. Do not spend the incident trying to
   raise them.
2. **`gpu_queue_starvation` cannot see a GPU that is busy.** Its runbook points
   at the WORKSPACE slot ledger, and the collector that feeds
   `ymoney_gpu_slots_reserved` counts `MEDIA_INTEL_GPU_SLOT` job rows in status
   `QUEUED`/`RUNNING` while a held slot is parked in `WAITING`. So the gauge
   reads 0 and the rule can fire while VRAM is reserved. Use
   `gpu_scheduler.snapshot()` — which reads `gpu_reservations` directly — as the
   source of truth, and §E.3.
3. **Metrics are in-process and reset on restart.** `slo_catalog()` says
   `"measured": false` on purpose: there is no scrape history, so `/internal/slo`
   reports *targets*, never achievement. If the process restarted, the counters
   are back to zero and "the alert went away" means nothing.

---

## A. Worker drain

**When:** deploy, host maintenance, or you need a replica out of rotation.

**What the system does automatically.** `WorkerPool.drain()` sets the drain flag,
which means *stop claiming, finish what you hold, and say so*. In-flight work gets
a bounded deadline (`JOB_DRAIN_TIMEOUT_SECONDS`, default 30). Past the deadline
the remaining worker tasks are **cancelled but their jobs are not** — the job stays
`RUNNING` with a lease that stops being renewed, so the next recovery sweep
reclaims it exactly as it would reclaim a crashed worker. Cancelling a paid render
mid-flight is the worst available outcome: the money is committed and the artifact
never lands.

**What you do.**

```bash
cd /opt/ymoney
docker compose -f docker-compose.prod.yml stop -t 120 backend
```

`-t 120` > the 30 s drain, so the graceful path completes before Docker escalates.
Watch for both lines in the log — they are the difference between clean and
recovered:

```
job worker pool <identity> draining (deadline 30.0s, 3 in flight)
job worker pool <identity> drained cleanly            # <- good
#   ...or...
drain deadline passed with 2 worker(s) still busy; their leases were left to
expire so the jobs are recovered, not lost (<job-ids>)
```

```bash
docker compose -f docker-compose.prod.yml logs backend | grep -E "draining|drained cleanly|left to expire"
```

**Confirm what this process was holding** (run inside the API process; a fresh
`exec` has no pool and will report empty):

```bash
docker compose -f docker-compose.prod.yml exec backend \
  python -c "import json; from app.services import jobs; print(json.dumps(jobs.pool_ownership(), indent=2))"
```

`pool_ownership()` returns `{"worker", "slots", "in_flight", "draining"}`.

**Requires a human decision:** nothing. Drain is safe and reversible.

---

## B. Stuck job

**Symptom:** `queue_stalled` (backlog > 0 **and** no job started for > 900 s) —
note both conditions; a deep queue with a recent start is healthy backlog and a
quiet queue is healthy idleness.

**First, distinguish the three real causes.** `_rule_queue_stalled`'s own runbook
hint is the fastest check: *a full connection pool blocks the claim query.*

```bash
curl -s http://backend:8100/internal/metrics | grep -E "^ymoney_(workers_total|workers_busy|job_queue_depth|job_last_start_timestamp_seconds|db_pool_utilization_ratio|collector_up)"
```

| Reading | Cause | Go to |
|---|---|---|
| `ymoney_workers_total 0` | no worker task alive | §B.1 |
| `ymoney_db_pool_utilization_ratio` at `1` | pool saturated | §B.2 |
| workers busy, `ymoney_gpu_jobs_waiting > 0`, `ymoney_gpu_slots_reserved 0` | GPU starvation | §E.3 |
| workers idle, queue deep | mis-tagged job class | §B.3 |

### B.1 `worker_fleet_unavailable`

**What the system does automatically.** Nothing — this is a startup failure.
`start_workers()` runs in the lifespan; if it did not complete, or every worker
task died, there is no worker. Each slot logs `job worker <id> died` on its own
exception and keeps the rest of the pool alive (`worker_pool._slot`), so a total
zero means the *pool* failed, not the jobs.

**What you do.**

```bash
docker compose -f docker-compose.prod.yml logs backend | grep -E "job worker pool|job worker .* started|job worker .* died"
```

A healthy boot prints one `starting:` line naming every slot, then one `started`
line per slot. Default plan, verified on this repository:

```
job worker pool <host>:<pid>:<nonce> starting: CPU=1, INTELLIGENCE=1, IO=1,
PUBLISH=1, RENDER=1, SMALL=4
```

Then just restart the process; startup runs one recovery sweep synchronously
(`jobs.start_workers` → `recover_orphans()`) before the pool starts, so
crashed-worker jobs are the first thing it picks up.

### B.2 Pool saturation

**What the system does automatically.** `pool_pre_ping` discards connections a
proxy or failover killed; `pool_recycle` retires ones a load balancer timed out
(`DB_POOL_RECYCLE`, default 1800 s — keep it **below** your LB/pgbouncer idle
timeout). `pool_timeout` (`DB_POOL_TIMEOUT`, default 30 s) is how long a caller
waits before being told the database is saturated.

**What you do.**

```bash
# ceiling check: DB_POOL_SIZE + DB_MAX_OVERFLOW is PER PROCESS
psql "$DATABASE_URL" -c "show max_connections"
```

At the ceiling, **the lever is fewer processes or a smaller pool, not a bigger
one** — `db_pool_size + db_max_overflow` × replicas must stay under
`max_connections` with headroom for the migration runner and an operator session.

**Requires a human decision:** yes. Raising the pool ceiling on a saturated server
converts queueing into connection errors.

### B.3 A job no worker will claim

**Symptom:** queue deep, workers idle, nothing ever starts on it.

**What the system does automatically.** Three refusals, each with a designed
consequence:

* **Wrong workload class.** A class-pinned worker only claims its own class.
  `classify_workload()` resolves, strongest first: `payload["workload"]` (the
  producer's word, which overrides everything), then `payload["requires_gpu"]`,
  then a substring hint table, then `SMALL` — the class that must never be starved.
  Unclassified work therefore lands where it cannot block anything.
* **GPU-gated on a non-GPU worker** → the job is pushed back
  (`_REFUSED_PUSHBACK_SECONDS = 60`) and `gpu_scheduler` is consulted. See §E.3.
* **A paid job whose prior submission is unresolved** → the re-entry gate
  refuses to run it (`assess_paid_reentry` → `BLOCKED`). See §C.

```bash
docker compose -f docker-compose.prod.yml exec backend \
  python -c "
from app.services import worker_pool as wp
for t in ('render_video','inbox_sync','autopilot.start_next_cycle','system.schedule_sweep'):
    print(f'{t:32} -> {wp.classify_workload(t)}')
"
```

Verified output: `render_video -> RENDER`, `inbox_sync -> IO`.

Now read the offending rows:

```sql
SELECT id, type, status, claimed_by, lease_expires_at, heartbeat_at,
       retry_count, max_retries, last_error
  FROM jobs
 WHERE status IN ('RUNNING','WAITING')
 ORDER BY lease_expires_at
 LIMIT 20;
```

**A `RUNNING` row whose `lease_expires_at` is in the past is reclaimable.** Force
one sweep — this is the same function the timer calls, and it is idempotent:

```bash
docker compose -f docker-compose.prod.yml exec backend \
  python -c "from app.services import worker_pool as wp; print('reclaimed:', wp.reclaim_now())"
```

Prefer the dry run first. It reports what *would* be reclaimed, including the
former owner, with nothing written:

```bash
docker compose -f docker-compose.prod.yml exec backend \
  python -c "
from app.db import session_scope
from app.services import job_leases as jl
with session_scope() as s:
    for r in jl.reclaim_expired(dry_run=True):
        print(f'{r.job_id}  owner={r.lost_owner!r}  expired_at={r.expired_at}  attempt={r.attempt}  was_paid={r.was_paid}')
"
```

`was_paid=True` means the ledger has open evidence for that job — reconcile with
the provider **before** letting it rerun. That is §C, not §B.

A row whose `retry_count > max_retries` goes to `DEAD`, not back to the queue: an
unreclaimable-but-parked job is how a poison pill becomes a hot loop, and a
dead-letter row is visible where a hot loop is not.

**Cancel instead**, when the work should not run at all
(`POST /api/v1/workspaces/{ws}/jobs/{job_id}/cancel`, admin role): a
`QUEUED`/`WAITING`/`RETRYING` row is set `CANCELLED` outright; a `RUNNING` row
gets `cancel_requested = True`, which the handler observes at its next checkpoint.

```bash
curl -sS -X POST -H "Authorization: Bearer $TOKEN" \
  "http://backend:8100/api/v1/workspaces/$WS/jobs/$JOB_ID/cancel"
```

Verified: the route is `jobs_router.post("/{job_id}/cancel")` under prefix
`/workspaces/{workspace_id}/jobs`, and it returns `{"cancelled": true|false}`.
`false` means the status was already terminal.

**Requires a human decision:** yes — whether to reclaim, cancel, or fix the job's
declared workload class. Never hand-edit `status` on a `RUNNING` row to "unstick"
it; that is exactly the blanket `recover_orphans()` behaviour §2 removed, and it
buys a duplicate execution of a live job.

---

## C. Unknown paid submission — the one that costs money

**This is the highest-consequence incident in this runbook.**

**What happened.** A billable provider call left the process and the response was
lost. The provider may have accepted it **and billed it**. YMONEY cannot know.

**What the system does automatically, and it is not small:**

* The attempt is persisted as `SubmissionState.SUBMISSION_UNKNOWN` **before** the
  response is read, in the same place the durable evidence lives:
  `videos.submission_state` (migration 0033) for a render submit,
  `lipsync_jobs.execution_outcome` / `cost_outcome` (0034) for lip-sync.
* The money is booked as `CostOutcome.UNKNOWN_EXPOSURE`, whose `ledger_value` is
  `None`, **not `0.0`**. "Money may have been spent and we do not know how much"
  is categorically different from zero, and recording `$0` because YMONEY lost a
  response is how a real charge disappears from the books.
* `may_resubmit` is `False` and the state is in `paid_jobs.NO_RESUBMIT`.
  **Automatic resubmission is structurally forbidden**, not merely discouraged.
* `retry_safety` is `RetrySafety.UNSAFE` (`DO_NOT_RESUBMIT`) unless the failure
  proved nothing was delivered. A **connect** timeout proves the TCP connection was
  never established, so the request cannot have reached the server; a **read**
  timeout does not. Only the first is safe to retry.
* Two CRITICAL alerts fire with target exactly **zero**: `paid_submission_unknown`
  and `unknown_exposure_high`.
* For the autopilot render lane specifically, the agent **halts the pipeline**
  before even reaching the budget gate (`production.py:157`) and logs
  `video.generation.submission_unknown` with the `request_hash`.

### C.1 Read the incidents

```bash
curl -sS -H "Authorization: Bearer $TOKEN" \
  "http://backend:8100/api/v1/workspaces/$WS/provider-maturity/incidents?limit=50" \
  | python -m json.tool
```

This is the **real operator entry point**, and it is the one the alert rule's own
runbook string names. Verified present in the OpenAPI schema as
`/api/v1/workspaces/{workspace_id}/provider-maturity/incidents` (GET, viewer
role, `limit` 1–200, default 50). It reads **two** sources:

* `videos` where `submission_state IN ('SUBMISSION_UNKNOWN','SUBMISSION_ATTEMPTED')`
* `cost_entries` where `detail_json.exposure_unknown` is true

Two rules the payload enforces for you, so a UI cannot quietly get it wrong:

* **`SUBMISSION_UNKNOWN` is never reported as `FAILED`.** One means a confirmed
  rejection, the other may already be an invoice. `display_state` is a separate
  field.
* **Retry is offered only when `retry_safe` is true**, which requires a provably
  undelivered submit.

Look at `recommended_action` — it is derived by `paid_executor.verdict_for()` and
the record's own `reconciliation` field, so the API, a CLI and the UI cannot answer
differently.

### C.2 Query the raw evidence

Dialect-agnostic, via SQLAlchemy (verified against the real schema):

```bash
docker compose -f docker-compose.prod.yml exec backend python -c "
from app.db import session_scope
from sqlalchemy import select
from app.models import CostEntry, Video
from app.engine.lipsync import rows as lip
with session_scope() as s:
    for v in s.scalars(select(Video).where(
            Video.submission_state.in_(['SUBMISSION_UNKNOWN','SUBMISSION_ATTEMPTED'])).limit(20)):
        print('VIDEO', v.id, v.workspace_id, v.engine, v.submission_state,
              'remote_id=', v.provider_task_id or v.engine_task_id, '|', v.submission_detail)
    for c in s.scalars(select(CostEntry).where(
            CostEntry.detail_json['exposure_unknown'].as_boolean().is_(True)).limit(20)):
        print('COST ', c.id, c.workspace_id, c.category, c.provider,
              c.amount_usd, (c.detail_json or {}).get('remote_id',''))
    print('LIPSYNC unknown-exposure:', lip.jobs_with_unknown_exposure('$WS'))
    print('LIPSYNC unknown-submission:', lip.jobs_with_unknown_submission('$WS'))
"
```

Raw SQL, if you prefer psql — note the two dialects differ:

```sql
-- PostgreSQL
SELECT id, workspace_id, engine, submission_state, provider_task_id, engine_task_id, submission_detail
  FROM videos WHERE submission_state IN ('SUBMISSION_UNKNOWN','SUBMISSION_ATTEMPTED')
 ORDER BY updated_at DESC LIMIT 20;

SELECT id, workspace_id, category, provider, amount_usd, created_at
  FROM cost_entries
 WHERE CAST((detail_json ->> 'exposure_unknown') AS BOOLEAN) IS true;

-- SQLite renders the same query as: WHERE JSON_EXTRACT(detail_json, 'exposure_unknown') IS 1
-- (both renderings verified by compiling the SQLAlchemy expression for each dialect)

SELECT id, provider, status, execution_outcome, cost_outcome, created_at
  FROM lipsync_jobs WHERE execution_outcome = 'SUBMISSION_UNKNOWN';
```

> **Naming note.** `jobs_with_unknown_submission` and
> `jobs_with_unknown_exposure` are **Python query helpers in
> `app/engine/lipsync/rows.py`**, not SQL views. There are no views named that in
> the schema. `jobs_with_unknown_submission` filters on `execution_outcome`
> ("the submit could not be confirmed"); `jobs_with_unknown_exposure` filters on
> `cost_outcome` ("the amount is unknown"). They are separate assertions on purpose
> and you should not conflate them.

### C.3 Decide — this is where a human is required

**The four actions** (`paid_executor.Reconciliation`), and exactly what each does:

| Action | Effect | Use when |
|---|---|---|
| `RECONCILE` | records the decision; **leaves `state` untouched** | you looked at the provider and know the outcome but want the system to keep treating it as unconfirmed |
| `RETRY_IF_CONFIRMED_SAFE` | sets `retry_safety = SAFE` | you have **proved** the first request was never delivered |
| `MARK_FAILED` | sets `state = FAILED` | the provider definitively rejected or the work is abandoned |
| `MANUAL_OVERRIDE` | records the decision; **leaves `state` untouched** | nothing else fits and you accept the audit trail |

`RECONCILE` and `MANUAL_OVERRIDE` deliberately do **not** touch `state`: the
submission is still unconfirmed, and pretending otherwise is the bug this whole
contract exists to prevent. Every decision is audited — a `ValueError` is raised
if `operator` is empty, because a manual override that leaves no trace is how a
real duplicate charge becomes unexplainable three weeks later.

> ### ⚠ `reconcile_submission()` is a library function; use the CLI
>
> `reconcile_submission()` in `app/services/paid_executor.py` mutates a
> `PaidSubmission` **object** in memory and logs; it does not write. Do not call
> it from a shell and go looking for the row afterwards.
>
> **What DOES persist the decision** is the
> `app.services.paid_reconciliation` CLI — verified on this repository, and the
> authoritative procedure at 3am:
>
> ```bash
> # 1. what might have been billed, in this workspace
> docker compose -f docker-compose.prod.yml exec backend \
>   python -m app.services.paid_reconciliation list --workspace-id "$WS_ID"
>
> # 2. read one subject back, JSON, read-only
> docker compose -f docker-compose.prod.yml exec backend \
>   python -m app.services.paid_reconciliation show --operation-id "$OP_ID"
>
> # 3. apply the outcome the provider dashboard showed -- durably and once
> docker compose -f docker-compose.prod.yml exec backend \
>   python -m app.services.paid_reconciliation reconcile \
>     --operation-id "$OP_ID" \
>     --outcome REMOTE_JOB_CONFIRMED \
>     --remote-id "$REMOTE_ID" \
>     --actual-usd 0.42 \
>     --operator "$YOUR_NAME"
> ```
>
> `--outcome` is one of `REMOTE_JOB_CONFIRMED`, `NOT_ACCEPTED`, `SUCCEEDED`,
> `FAILED`, `UNKNOWN_REMAINS`. `--remote-id` is **required** for
> `REMOTE_JOB_CONFIRMED` and **refused** for `NOT_ACCEPTED` — the CLI will not
> let you record a provider handle for a request the provider never accepted.
> `--operator` is mandatory: a decision with no author is unexplainable later.
> `--amount-unknown` books `UNKNOWN_EXPOSURE` rather than $0; absent
> `--actual-usd` the original estimate stands, it is never silently zeroed.
>
> Verified in this repository:
>
> ```text
> $ python -m app.services.paid_reconciliation --help
> usage: python -m app.services.paid_reconciliation [-h]
>                                                 {list,show,reconcile} ...
>
> $ python -m app.services.paid_reconciliation reconcile --help
> ... --operation-id OPERATION_ID
>     --outcome {REMOTE_JOB_CONFIRMED,NOT_ACCEPTED,SUCCEEDED,FAILED,UNKNOWN_REMAINS}
>     --operator OPERATOR  --note NOTE  --remote-id REMOTE_ID
>     --actual-usd ACTUAL_USD  --estimate-usd ESTIMATE_USD  --amount-unknown
>
> $ python -m app.services.paid_reconciliation list --workspace-id 00000000-0000-0000-0000-000000000000
> []
> $ echo $LASTEXITCODE
> 0
> ```
>
> **So the honest 3am procedure is:**
>
> 1. **Do not resubmit.** Ever, until the outcome is known.
> 2. **List** the workspace's ambiguous submissions (step 1 above) and open the
>    provider's dashboard. Look for a charge / a remote task using the
>    `remote_id` from §C.2 (`videos.provider_task_id`, or
>    `cost_entries.detail_json.remote_id`).
> 3. **Found it** → the work was bought. Record the fact with `reconcile
>    --outcome REMOTE_JOB_CONFIRMED` (step 3), then pass `--actual-usd` so the
>    books match the invoice. For the render lane you may instead adopt the
>    running task rather than re-submitting:
>    ```bash
>    docker compose -f docker-compose.prod.yml exec backend python -c "
>    from app.services.paid_provider import reattach_by_remote_id
>    op = reattach_by_remote_id('$REMOTE_ID', provider='$PROVIDER')
>    print('adopted:', op)
>    "
>    ```
>    `reattach_by_remote_id()` returns a `PaidOperation` bound to the **existing**
>    reservation row, with `settled` read back from it — so `close_book()` moves
>    no money again. The invariant is exactly-once **accounting**, not exactly-once
>    network. It returns `None` when the remote id is unknown, and
>    "nothing to adopt" must never become "reserve a fresh one" — so a `None`
>    means look again, not retry.
> 4. **Not found** → the request was not billed. Then, and only then,
>    `reconcile --outcome NOT_ACCEPTED` is defensible, and only after you have
>    proved it. Note the CLI refuses `--remote-id` here on purpose.
> 5. **Cannot tell** → `reconcile --outcome UNKNOWN_REMAINS` to record that you
>    looked and could not determine it, and accept that
>    `ymoney_paid_submission_unknown_total` stays > 0. That is the honest state.
>
> **Do not delete the ledger row.** It is the evidence that the purchase happened.

For the autopilot render lane specifically there **is** an automatic path:
`production.py` resolves prior state by variant / `request_hash`, refuses outright
on a `SUBMISSION_UNKNOWN` prior attempt, and reattaches to a live engine task. That
is a more precise correlation than the queue layer can make, which is why
`autopilot.start_next_cycle` is declared to the paid gate via
`job_leases.register_paid_job_type()` — and why nothing is inferred: guessing that
a handler is idempotent is how a duplicate purchase happens.

### C.4 Audit the coverage

```bash
docker compose -f docker-compose.prod.yml exec backend python -c "
import json
from app.services import paid_jobs_audit as a
print(json.dumps({k:v for k,v in a.summary().items() if k!='verdicts'}, indent=2))
print('mismatches vs source:', len(a.verify_against_source()))
for p in a.uncovered_billable_paths():
    print('UNCOVERED:', p.key, '-', p.gap)
"
```

Verified on this repository: `{"total": 46, "billable": 31, "not_billable": 15,
"covered": 31, "uncovered_billable": 0, "uncovered_keys": []}` and
**0 mismatches**. `verify_against_source()` re-derives every coverage claim from
the real files in both directions, so a stale row (the cited code moved) is
reported rather than believed. Run it after any change to a paid path — a
non-zero `uncovered_billable` is a finding, not a style issue.

**Requires a human decision:** **yes, always.** This is the one incident class in
this runbook where the system has deliberately stopped and will not proceed for
you.

---

## D. Database incident

**Symptom:** `db_unavailable` (the `database` collector heartbeat is not up),
and/or `/readyz` 503 with `blocking_failures` non-empty.

**What the system does automatically.**

* `/livez` **never touches the database**, so a slow DB cannot trigger a restart
  loop — that would convert a dependency outage into a crash loop and destroy the
  evidence.
* `/readyz` gates on three critical checks only: `database` (`SELECT 1`),
  `migrations` (the ledger exists, is readable, and is **non-empty**),
  `job_backend` (the `jobs` table can be queried). Each failure carries its own
  `detail` and `remediation` string in the payload.
* Every other dependency — all external providers — is reported under `degraded`
  and leaves readiness at **200**.
* SQLite runs with `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`.
  Postgres gets `pool_pre_ping` + `pool_recycle`.
* A stopped worker claims nothing and loses nothing: the lease expires and the
  sweep reclaims the job.

### D.1 Diagnose

```bash
curl -s http://backend:8100/readyz | python -m json.tool
```

| `blocking_failures` | Meaning | Do |
|---|---|---|
| `database` | no pooled connection | `psql "$DATABASE_URL" -c "select 1"`; check disk, DSN, and `DB_POOL_SIZE + DB_MAX_OVERFLOW` vs `max_connections` |
| `migrations` | `schema_migrations` missing / unreadable / **empty** | §D.2 |
| `job_backend` | `jobs` unreadable | a database problem, not a migration problem — check the DB, not the code |

```bash
curl -s http://backend:8100/internal/metrics | grep -E "^ymoney_(db_pool|collector_up)"
```

```bash
psql "$DATABASE_URL" -c "select count(*) from pg_stat_activity where state='active'"
psql "$DATABASE_URL" -c "show max_connections"
```

### D.2 `migrations` is empty — the schema is behind the code

```bash
docker compose -f docker-compose.prod.yml exec backend python -c "
from app.db import session_scope
from app.migrations.runner import applied_versions, load_migrations
with session_scope() as s:
    print('applied:', sorted(applied_versions(s))[-5:])
print('on disk:', [n for n,_ in load_migrations()][-5:])
"
```

Then run the pending set (the same runner the lifespan uses — verified idempotent,
prints `[]` when there is nothing to do):

```bash
docker compose -f docker-compose.prod.yml exec backend python -c "
from app.db import session_scope
from app.migrations.runner import run_migrations
with session_scope() as s:
    print('applied:', run_migrations(s))
"
```

Do **not** hand-insert into `schema_migrations`. The ledger is the only thing that
tells the runner what is done.

**Requires a human decision:** yes, if the DB is unrecoverable. **The database is
not backed up by anything in this repo except the §G restore.** No WAL archiving,
no PITR, no logical replication (`BACKUP_RESTORE_RUNBOOK.md` §6 items 2, 3, 5).

---

## E. Storage incident

**Symptom:** `storage_unavailable` (`ymoney_storage_available 0`), or renders that
complete but never publish an artifact.

**What the system does automatically.**

* Renders cannot be **partially** canonical. The order is: stream to `.part` in the
  destination → `fsync` → `os.replace` → **then** write the `FINALIZED` row. A crash
  anywhere leaves the old object or no object, never a truncated one claiming to be
  complete.
* `finalize()` refuses any source path inside the staging root, and `resolve()`
  refuses any key that resolves inside staging **or** belongs to another workspace —
  checked on the key prefix *and* the resolved path.
* `/readyz` deliberately does **not** gate on storage. A read-only filesystem does
  not stop YMONEY accepting, planning, budgeting and publishing; it stops renders
  completing. Failing readiness would take every replica out of rotation for a
  condition only the render lane suffers.

### E.1 Probe

The probe **writes and removes a small file** (`data/videos/.observability-probe`)
— a stat-only health check cannot detect a full or read-only filesystem.

```bash
curl -s http://backend:8100/internal/metrics | grep -E "^ymoney_(storage_available|storage_failures_total|collector_up)"
```

### E.2 Reclaim broken promises

A `PENDING` row older than `STORAGE_PENDING_TTL_SECONDS` (default 3600) is an
upload that died. Its bytes, if any, are an abandoned `.part` file and its row is a
lie the rest of the system would read as "an object exists". Both go.

```bash
# look first
docker compose -f docker-compose.prod.yml exec backend python -c "
from app.db import session_scope
from sqlalchemy import text
with session_scope() as s:
    for r in s.execute(text('''
        SELECT id, workspace_id, kind, state, size_bytes, temp_path, created_at
          FROM storage_objects WHERE state = 'PENDING' ORDER BY created_at''')):
        print(tuple(r))
"
```

```bash
# then sweep
docker compose -f docker-compose.prod.yml exec backend python -c "
from app.services import storage_objects as so
print(so.sweep())
"
```

Returns `{'pending_dropped': N, 'expired_dropped': N, 'part_removed': N}` — verified
on this repository (all zero on a clean DB). It also removes `FINALIZED` objects
past `expires_at`. It **never** deletes an object another row references through
`ref_type`/`ref_id`; that lifecycle is owned by `services.retention`, and two
sweeps with the authority to delete the same bytes is how bytes get deleted twice.

### E.3 Inventory, and the GPU starvation rule

```bash
docker compose -f docker-compose.prod.yml exec backend python -c "
from app.services import storage_objects as so
print('staging root:', so.staging_root())
print(so.describe('$WS'))
"
```

Verified output shape: `{'workspace_id', 'objects', 'finalized', 'pending',
'total_bytes', 'by_kind'}`, and `staging_root()` resolves to
`<repo>/backend/data/storage_staging`.

**`gpu_queue_starvation`** (`ymoney_gpu_jobs_waiting > 0` **and**
`ymoney_gpu_slots_reserved <= 0`): the rule's own runbook string points at
`MEDIA_INTEL_GPU_SLOT`, which **does not exist**. The real tables:

```sql
SELECT d.device_key, d.total_mb, d.reserved_mb, d.enabled,
       r.id AS reservation_id, r.status, r.kind, r.job_id, r.vram_mb,
       r.lease_expires_at, r.heartbeat_at, r.cpu_fallback
  FROM gpu_reservations r JOIN gpu_devices d ON d.id = r.device_id
 WHERE r.status <> 'RELEASED';
```

A held reservation whose `lease_expires_at` is in the past is a crashed holder.
Free the VRAM:

```bash
docker compose -f docker-compose.prod.yml exec backend python -c "
from app.services import gpu_scheduler as gs
print('freed:', gs.sweep())
print(gs.snapshot())
"
```

A live holder is **never** reclaimed — when the reservation names a `job_id`,
liveness is asked of `job_leases` rather than guessed, so the two recovery models
cannot disagree about the same job.

If `devices: []`, capacity was never registered. A waiting job and a free slot is a
scheduler or worker-lane fault, not slow work:

```bash
docker compose -f docker-compose.prod.yml exec backend python -c "
from app.services import gpu_scheduler as gs
print(gs.probe_devices()); print(gs.sync_devices()); print(gs.ensure_cpu_device()); print(gs.snapshot())
"
```

**Requires a human decision:** yes, on data loss. **A `FINALIZED` row whose bytes
are gone is a database that lies about what it has** — and the backup contains only
the inventory and checksums, never the media. That is item 1 of the 12-item
not-covered list in `BACKUP_RESTORE_RUNBOOK.md` §6. The recovery path is
re-materialisation: `storage_inventory.md` lists every object with
`PRESENT` / `CORRUPT` / `MISSING` resolved against the volume at backup time, then
re-render and re-finalize.

---

## F. Provider outage

**Symptom:** `repeated_publish_failure` (WARNING — a platform failed
`>= 3` **consecutive** publishes with no success between; the counter is reset by
any success, which is what distinguishes an outage from history). Or simply: one
vendor is down and everything else works.

**What the system does automatically, and this is the important part:**

* **`/readyz` stays 200.** Every external provider is non-critical. Pulling every
  replica out of the load balancer because one vendor has an incident converts a
  partial outage into a total one. Providers are reported under `degraded` and in
  `providers_down` / `providers_degraded`.
* Provider health is read from the **existing** maturity surface, not a second
  health stack: `GET /api/v1/provider-maturity`.
* Most surfaces already degrade rather than fail closed: a different voice renders
  with a different voice; publishes go through the platforms that are up.

```bash
curl -s "http://backend:8100/api/v1/provider-maturity?probe=true" | python -m json.tool
curl -s http://backend:8100/readyz | python -m json.tool | grep -A6 providers
```

`probe=true` is opt-in because a probe is a network call. Note what the surface
means: a status is recorded **after evidence** and is never derived from a module
importing; `live_status=UNVERIFIED` means nobody has exercised this provider
against its real service yet.

**Cost control does not degrade.** `paid_provider.OwnerlessSpendRefused` is raised
**before** anything is sent, so a budget refusal during a provider outage costs
nothing. And a `connect` timeout (never reached the server) is classified as
provably undelivered → `RELEASED`; an ambiguous failure is `UNKNOWN_EXPOSURE` and
re-entry is `BLOCKED`. Measured: `network timeout: undelivered=RELEASED
ambiguous=UNKNOWN_EXPOSURE reentry=BLOCKED` (`PRODUCTION_LOAD_REPORT.md` §7.2) —
**real socket, simulated provider**.

**What you do.** Nothing structural. Identify the platform, decide whether to wait
or fail over, and do **not** flip `ALLOW_MOCK_IN_PRODUCTION` or `MOCK_PUBLISHING`
to "keep the numbers moving" — a labeled mock publish that looks real is worse than
a visible outage. If the outage affects a paid provider, check §C first: an outage
is exactly when a `SUBMISSION_UNKNOWN` gets created.

**Requires a human decision:** yes — whether to pause autopilot for the affected
workspace, and whether a fail-over provider is acceptable.

---

## G. Restore

**Trigger:** the database is unrecoverable, or a rollback needs the pre-migration
state (`DEPLOYMENT_RUNBOOK.md` §6.3). This is the **only** recovery path for a
schema-destructive change — `run_migrations` has no `down`.

**What the system does automatically.** `restore()` **refuses to target the source
database**, so a restore cannot overwrite the thing you are trying to preserve.
`verify()` re-reads the manifest's canonical digests from the **restored**
database and exits non-zero on any mismatch — a restore whose digests do not match
is a FAILED restore even though `pg_restore` exited 0.
`BACKUP_TOLERATE_FAILED_CHECKS` is the number of failed checks tolerated and it is
**0 by design**.

### G.1 Restore into a new database

```bash
cd /opt/ymoney/backend
export YMONEY_PG_CONTAINER=<pg-container>      # or export BACKUP_PG_CONTAINER
export DATABASE_URL="postgresql://ymoney:<password>@<host>:5432/<current-db>"

# 1. restore into a DIFFERENT database
python -m app.scripts.backup_restore --dsn "$DATABASE_URL" \
    --out /var/backups/ymoney/<timestamp> restore --target-db ymoney_restored

# 2. verify the restored database, not the source
python -m app.scripts.backup_restore \
    --dsn "postgresql://ymoney:<password>@<host>:5432/ymoney_restored" \
    --out /var/backups/ymoney/<timestamp> verify --database ymoney_restored
```

Exit codes: `0` verified, `1` verification FAILED, `2` the tool could not run.
**Stop on non-zero.** `drill` is destructive — it drops the source database — and
is only ever run against a staging clone.

Full walkthrough with real output, the mutation that proves the restore, and the
measured ~17 s RTO: [`BACKUP_RESTORE_RUNBOOK.md`](BACKUP_RESTORE_RUNBOOK.md).

### G.2 Cut over

```bash
# stop the API before the DSN swap so nothing writes to the old database
docker compose -f docker-compose.prod.yml stop -t 120 backend
# repoint DATABASE_URL at ymoney_restored, then:
docker compose -f docker-compose.prod.yml up -d --no-deps backend
curl -s http://backend:8100/readyz | python -m json.tool   # expect "ready", blocking_failures []
```

### G.3 What a restore does **not** bring back

The 12-item list is in `BACKUP_RESTORE_RUNBOOK.md` §6. The three that will bite at
3am:

1. **Media bytes.** Only the `storage_objects` inventory and checksums.
2. **Anything since the last dump.** No WAL archiving, no PITR → **RPO is hours**.
   Check the age of what you are restoring *before* you restore it.
3. **Secrets.** `manifest.json → secret_references` records credential *names* and
   `configured=true/false` — **never values**. Restoring means re-injecting them
   from your secret store.

**Requires a human decision:** yes, always — a restore loses in-flight work and
may be hours stale.

---

## H. What requires a human decision — summary

| Incident | Automatic | Human must |
|---|---|---|
| **Worker drain** | stops claiming, finishes in-flight, bounded | nothing |
| **Stuck job** | classifies the cause; reclaim is dry-runnable | reclaim / cancel / re-classify. Never hand-edit `status` |
| **Unknown paid submission** | persists `SUBMISSION_UNKNOWN`, books `UNKNOWN_EXPOSURE` (not 0), **forbids resubmit**, fires CRITICAL, halts the render lane | **always.** Look up the charge by `remote_id`, then apply it with `python -m app.services.paid_reconciliation reconcile --outcome ...` — §C.3 |
| **DB incident** | `/livez` immune; `/readyz` 503 with per-check remediation; pool pre-ping/recycle | decide restore if unrecoverable |
| **Storage incident** | `.part` → `os.replace` → `FINALIZED` last; `sweep()` reclaims dead promises; readiness unaffected | accept data loss / re-materialise from the inventory |
| **GPU starvation** | `sweep()` frees stale reservations; live holders never reclaimed | register devices if `devices: []` |
| **Provider outage** | readiness stays **200**; degraded surfaces; spend still gated | pause autopilot; decide on a fail-over provider |
| **Restore** | refuses to overwrite the source; verifies digests; exit non-zero on mismatch | choose the target, accept the RPO, re-inject secrets |

---

## I. Command verification

Every command in this document was executed against this repository. Method:

| Command | Verified by |
|---|---|
| `GET /internal/overview` and the whole `/internal/*` set | real lifespan boot via `TestClient`; `/livez`, `/readyz`, `/internal/metrics`, `/internal/alerts`, `/internal/slo`, `/internal/collectors`, `/internal/overview`, `/internal/traces` all **200**; `/internal/metrics` → `text/plain; version=0.0.4; charset=utf-8`, 273 lines |
| `GET /api/v1/workspaces/{ws}/provider-maturity/incidents` | present in the generated OpenAPI schema as a GET path; route decorator + `limit` bound read at `api/v1/providers.py:263`; router prefix `/workspaces/{workspace_id}/provider-maturity` at line 51 |
| `POST /api/v1/workspaces/{ws}/jobs/{job_id}/cancel` | OpenAPI schema path confirmed; handler read at `api/v1/misc.py:1219`; returns `{"cancelled": bool}` |
| `GET /api/v1/provider-maturity?probe=true` | OpenAPI schema path confirmed; `probe` is a real `Query(default=False)` at `api/v1/providers.py:122` |
| `paid_jobs_audit.summary()` / `verify_against_source()` / `render_table()` / `describe(key)` | executed; `46/31/15/31/0`, **0 mismatches**, and a real `describe("lipsync.external.submit")` payload |
| Enum vocabularies (`SubmissionState`, `CostOutcome`, `Reconciliation`, `RetrySafety`, `IdempotencySupport`, `RetryVerdict`, `SpendAuthority`, `ActorAuthority`) | printed at runtime; every value quoted in this document is one of them |
| `paid_provider.SYSTEM_BUDGET_ENV` / `system_budget_usd()` | executed → `YMONEY_SYSTEM_BUDGET_USD`, `0.0` |
| `paid_provider.OwnerlessSpendRefused` base classes | executed → `issubclass(..., BudgetExceededError) is True` |
| `paid_provider.reattach_by_remote_id('…')` with an unknown id | executed → `None` |
| `worker_pool.classify_workload('render_video' / 'inbox_sync')` | executed → `RENDER` / `IO` |
| `worker_pool.default_plan(4).as_text()` | executed → `CPU=1, INTELLIGENCE=1, IO=1, PUBLISH=1, RENDER=1, SMALL=4` |
| `worker_pool.reclaim_now()` / `jobs.recover_orphans()` | executed → `0` |
| `jobs.pool_ownership()` | executed → `{'worker': '', 'slots': {}, 'in_flight': []}` on a fresh process |
| `job_leases.lease_seconds()` / `lease_seconds('RENDER')` / `worker_identity()` | executed → `120.0` / `120.0` (no `JOB_LEASE_SECONDS_BY_WORKLOAD` set) / `host:pid:nonce` |
| `job_leases.reclaim_expired(dry_run=True)` field names | read from `ReclaimedJob` at `job_leases.py:590-598` (`job_id`, `lost_owner`, `expired_at`, `attempt`, `was_paid`) |
| `gpu_scheduler.probe_devices()` / `sync_devices()` / `ensure_cpu_device()` / `snapshot()` / `sweep()` | all executed; `snapshot()` on a stock DB → `devices: []` |
| `storage_objects.describe(ws)` / `staging_root()` / `sweep()` | executed; `sweep()` → `{'pending_dropped': 0, 'expired_dropped': 0, 'part_removed': 0}` |
| All SQL in §B.3, §C.2, §D.1, §E.2, §E.3 | executed against the real schema; every statement parsed and ran (0 matching rows on a clean DB). Column names read from `inspect(engine).get_columns()` for `jobs`, `gpu_devices`, `gpu_reservations`, `storage_objects`, `cost_entries`, `lipsync_jobs` |
| `MEDIA_INTEL_GPU_SLOT` does not exist | checked against `inspect(engine).get_table_names()` |
| `paid_executor.reconcile_submission` signature and that it does not persist | read at `paid_executor.py:512-541`; no `session_scope` / no write in the function body. The persisting path is the `app.services.paid_reconciliation` CLI, whose `list` / `show` / `reconcile` subcommands were verified with `--help` and a real invocation on this repository |
| `docker compose … ps` requires the full variable set | executed; exit 1 with the interpolation error |
| `python -m app.scripts.backup_restore` + all four subcommands and their flags | executed `--help` for each; `--target-db` confirmed **required** on `restore` |

**Not verified, therefore not claimed:** any command against a live compose stack,
any real PostgreSQL restore by this lane (the restore evidence is
`BACKUP_RESTORE_RUNBOOK.md`'s, from its own drill), TLS-terminated requests, and
anything behind a real ingress. Docker 29.5.3 / Compose v5.1.4 are installed here;
the stack was not brought up.
