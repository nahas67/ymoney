# Worker pool runbook (Work 16 §2/§3)

Operational guide to leases, crash recovery, workload pools, and the paid
re-entry gate. Read this before adding a job type that spends money, and before
scaling the API process past one replica.

---

## 1. What changed, and why it was urgent

`jobs.recover_orphans()` used to run on every worker boot and reset **every**
`RUNNING` job to `RETRYING`:

```sql
UPDATE jobs SET status = 'RETRYING' WHERE status = 'RUNNING'
```

With one process that was survivable. With two it is a live incident:

```text
worker A claims a 30-minute render          -> jobs.status = RUNNING
worker B restarts for an unrelated deploy   -> recover_orphans() resets A's job
worker B (or A's own loop) polls            -> claims it and runs it AGAIN
```

Nothing in the schema could tell "orphaned by a dead worker" from "actively
running on a live worker", so the code had to assume the rare case and break the
common one. The consequences were a duplicate render, a duplicate provider
submission, a duplicate publish — and for a paid render, a second invoice.

**The fix is a lease.** A worker takes one, renews it while it works, and only
an **expired** lease may be reclaimed. Expiry is proof of absence: a live worker
heartbeats, so a live job's lease never lapses; a dead one stops heartbeating, so
its job becomes reclaimable within one TTL.

---

## 2. The lifecycle

```text
QUEUED / RETRYING
      |  claim  (one conditional UPDATE; rowcount decides the winner)
      v
  CLAIMED      status=RUNNING, claimed_at set, started_at NULL, lease_expires_at = now + TTL
      |  begin_run   (the handler is being entered)
      v
  RUNNING      started_at set; lease renewed every TTL/3
      |
      +--> COMPLETED / FAILED / CANCELLED / DEAD   (lease released with the outcome)
      |
      +--> lease expires  --> reclaim sweep --> RETRYING (retry_count + 1)
```

Two states, not one blur. `claimed_at` (lease taken) and `started_at` (handler
entered) are separate columns, so `lease_state()` in `job_leases` can answer
"is a worker holding this" and "is a handler executing it" independently.

`jobs.status` deliberately keeps saying `RUNNING` across both. A dozen readers
across the codebase (`api/v1/knowledge.py`, `engine/sources/sync.py`,
`services/observability/metrics.py`, `engine/decision.py`) query
`status IN ('QUEUED','RUNNING')` as "in flight". Writing a new value they do not
know about would make a live job invisible to all of them at once.

---

## 3. Columns (migration `0035_job_leases`)

| Column | Meaning |
|---|---|
| `claimed_by` | Worker identity holding the lease. `''` = nobody. |
| `claimed_at` | When the lease was taken. |
| `lease_expires_at` | The deadline. **The only thing that may authorise a steal.** `NULL` = nobody can renew this row, so it is recoverable. |
| `heartbeat_at` | Last proof of life. Distinct from `claimed_at`: "how long have you had it" and "are you still there" are different questions. |

Indexes: `ix_jobs_lease_recovery (status, lease_expires_at)` serves the sweep;
`ix_jobs_claimed_by` answers "what is worker X doing".

Nothing is invented on upgrade. A pre-existing `RUNNING` row has
`lease_expires_at IS NULL`, and that NULL reads as "nobody can renew this, so
it is recoverable" — the same treatment it got before, and no worse. It is *not*
backfilled with a far-future timestamp, which would mark every pre-existing
in-flight job permanently un-reclaimable.

---

## 4. Claiming: why it is atomic across processes

One conditional UPDATE. The status predicate is **inside the statement that
writes**, so the database itself rejects the second worker:

```sql
UPDATE jobs
   SET status='RUNNING', claimed_by=:w, claimed_at=:now,
       lease_expires_at=:now + :ttl, heartbeat_at=:now, started_at=NULL
 WHERE id = :id
   AND status IN ('QUEUED','RETRYING')
   AND (lease_expires_at IS NULL OR lease_expires_at <= :now)
   AND next_run_at <= :now
```

`rowcount == 1` means won; anything else means somebody else got there first.
A plain `SELECT ... LIMIT 1` followed by an `UPDATE` is a lost-update race: two
workers read the same id and both proceed.

The candidate `SELECT` takes `FOR UPDATE SKIP LOCKED` on PostgreSQL so N workers
read N *different* candidates instead of queue behind one row's lock. SQLite has
no such clause and does not need one — the conditional UPDATE decides the
winner, not the SELECT.

`renew_lease` is conditional on `claimed_by`, so a worker that was slow past its
TTL **cannot resurrect a lease another worker now owns**. Without that predicate
both workers would believe they hold the job.

> **Reviewer note.** `_Take` is a `StrEnum`, so every member is a non-empty
> string and `if not _take(...)` is False for `LOST` too. Compare with
> `is _Take.WON`. A truthiness check here silently turned every lost race into a
> second execution of somebody else's job; the concurrency test is the only
> thing that can catch it.

---

## 5. Crash recovery

`jobs.recover_orphans()` now delegates to `job_leases.reclaim_expired()`:

```sql
-- conceptually
UPDATE jobs SET status='RETRYING', retry_count = retry_count + 1, ...
 WHERE status = 'RUNNING'
   AND (lease_expires_at IS NULL OR lease_expires_at <= now())
```

- **Boot**: one sweep runs synchronously in `start_workers()`.
- **Runtime**: a timer sweep (`job_reclaim_interval_seconds`, default 15s) runs
  in the pool, because the worker that died is usually *not* this process and
  nothing else would ever reclaim its work.
- `WAITING` rows are untouched, which is what keeps the GPU slot ledger
  (`media_intel_runs`) intact: a held slot is parked in `WAITING` precisely so
  recovery cannot see it.
- A crash consumes a retry. When the budget is spent the job goes `DEAD` rather
  than being requeued forever — a poison pill must not become a hot loop, and a
  dead-letter row is visible where a hot loop is not.

### Verifying a recovery in production

```sql
SELECT id, type, claimed_by, heartbeat_at, lease_expires_at,
       extract(epoch FROM (lease_expires_at - now())) AS secs_left
  FROM jobs
 WHERE status = 'RUNNING'
 ORDER BY claimed_at;
```

- `secs_left` steadily climbing → heartbeats are working.
- `claimed_by` empty → a legacy row from before 0035.
- A row whose `secs_left` is negative for longer than one sweep interval → the
  sweep is not running; check the `job-lease-reclaim` task in the startup log.

From Python:

```python
from app.services import job_leases
job_leases.reclaim_expired(dry_run=True)   # what WOULD be reclaimed, changes nothing
job_leases.held_job_ids("replica-7:SMALL")  # what a worker says it holds
jobs_service.pool_ownership()                # slots + in-flight, process-wide
```

---

## 6. Worker pools

Seven workload classes. The point is **separation without microservices**: every
class is a task in the same process, against the same database, with the same
handler registry. Only the rows a given worker will claim change.

| Class | For | Starves if merged |
|---|---|---|
| `SMALL` | Planner bookkeeping, inbox sync, retention, small agent runs | The default, and the class that must never wait |
| `CPU` | Transcode, audio analysis, hashing | — |
| `IO` | Connector syncs, analytics collection, exports | — |
| `RENDER` | Video / lip-sync / avatar / B-roll. Minutes, and usually money | **This is the class that starves everything else** |
| `GPU` | GPU-gated work; also requires `gpu_worker=true` | — |
| `PUBLISH` | Anything that can reach an audience. Irreversible when it goes wrong | — |
| `INTELLIGENCE` | LLM / enrichment. Slow, token-spending, safe to defer | — |

**Routing**, strongest first (`worker_pool.classify_workload`):

1. `payload["workload"]` — the producer's explicit word; nothing overrides it.
2. `payload["requires_gpu"]` — the GPU gate is already a first-class flag.
3. A substring hint table against the lowercased job type.
4. `SMALL`.

Unclassified work lands in `SMALL`, because that is the class where being wrong
costs least.

**Default sizing** (`job_worker_pools` unset): `job_worker_count` SMALL slots
plus one slot per other class (GPU only when `gpu_worker` is set). An idle slot
is one indexed SELECT per poll interval, which is cheap; a starved planner is not.
Configure explicitly with e.g. `JOB_WORKER_POOLS="SMALL=6,RENDER=2,GPU=1"`.

`start_workers(count=N)` keeps the historical meaning: N **undifferentiated**
slots (`ANY=N`), identical to pre-16 behaviour. Use it only if you are migrating.

---

## 7. Draining and shutdown

`stop_workers()` → `pool.drain(timeout)`:

1. Set the drain flag: workers stop **claiming**.
2. In-flight work continues to completion.
3. Past the deadline (`job_drain_timeout_seconds`, default 30s) the remaining
   worker tasks are cancelled.

Past the deadline we do **not** try to finish the job, and we do not mark it
failed. Those jobs stay `RUNNING` with a lease that stops being renewed, so the
next recovery sweep reclaims them exactly as it would reclaim a crashed worker.
Cancelling a paid render mid-flight is the worst available outcome: the money is
committed and the artifact never lands. An abandoned lease is recoverable; that
is not.

Jobs queued behind a draining pool stay `QUEUED` and untouched.

---

## 8. Paid re-entry (the part that costs money)

> A worker crash after a paid submission must never cause automatic repurchase.

Work 15.7–15.9 made submissions durable and classified ambiguous ones. What was
missing was at the **queue** layer: `_execute` called the handler
unconditionally, so any attempt after the first (a retry, or a job just recovered
from a dead worker) re-ran the whole thing — including its billable submit. That
is the path Work 15.6 counted as reachable up to four times per job.

`job_leases.assess_paid_reentry()` runs **before** the handler on every attempt
where `attempt > 1`:

| Verdict | Meaning | What the runner does |
|---|---|---|
| `NONE` | No prior submission. A fresh attempt cannot double-charge. | Runs the handler. |
| `REATTACH` | A ledger row already owns this work. | Runs the handler with `ctx.paid_remote_id` / `ctx.paid_entry_id` set and `ctx.recovery_mode="REATTACH"`. The handler must poll, never submit. |
| `BLOCKED` | Money may be gone and unattributable. | **Does not run the handler.** Job → `WAITING`, `paid.reentry_blocked` event. |
| `DEFERRED` | This handler reconciles its own prior submission. | Recorded, then deferred to the handler. |

Evidence, weakest to strongest, read off `cost_entries.detail_json`:

- an **open reservation** (no cost outcome recorded) — `authorize` writes it
  before the request leaves, so its presence proves a billable request was about
  to be made and nothing has since said it was not;
- **`UNKNOWN_EXPOSURE`** — the response was lost, money may be gone;
- a **`remote_id`** — the provider accepted and returned a durable handle. A
  crash cannot fabricate this, which is why it is the strongest.

Correlation is **exact, never guessed**. "The newest paid row in this workspace"
is the right row for one job at a time and the wrong one for two, and a wrong
reattach would poll somebody else's remote job. In order:

1. a registered probe (`job_leases.register_paid_probe(job_type, probe)`) — the
   producer says "this job already exists at remote id X";
2. a money row whose `detail_json["idempotency_key"]` equals the job's
   `idempotency_key`;
3. for a job declared `paid=True`, *any* unresolved submission in the workspace.

A remote id **no ledger row owns** is `BLOCKED`, never "go ahead and buy it
again": the provider accepted, and the crash lost the only record of the
reservation. Booking `$0` erases a real charge; submitting again doubles it.

### What you must do when you add a paid job type

- `enqueue(..., paid=True)`. This is the one-word contract with the recovery
  path. It is opt-in on purpose: assuming every job spends money would let one
  unresolved row in a workspace stall every unrelated retry in it.
- Pass the same `idempotency_key` in `reservation_extra` so recovery gets an
  exact reattach instead of a block.
- Read `ctx.paid_remote_id` in the handler and **resume** that remote job. Never
  treat a re-entry as a blank slate.

### Operator queries

```sql
-- jobs stopped by the paid gate
SELECT id, type, last_error FROM jobs
 WHERE status = 'WAITING' AND last_error LIKE 'paid re-entry blocked%';

-- unresolved money in a workspace
SELECT id, provider, category, amount_usd, detail_json->>'remote_id' AS remote_id,
       detail_json->>'cost_outcome' AS cost_outcome
  FROM cost_entries
 WHERE cost_entries.id IN (SELECT id FROM cost_entries) AND (
       detail_json->>'cost_outcome' = 'UNKNOWN_EXPOSURE'
    OR detail_json->>'remote_id' IS NOT NULL)
 ORDER BY created_at DESC LIMIT 50;
```

Resolve a `BLOCKED` job by reconciling the provider against the remote id in the
event's `data`, then clearing the job's `cancel_requested` / re-enqueueing. Do
not delete the ledger row: it is the evidence that the purchase happened.

---

## 9. Configuration

| Setting | Default | Meaning |
|---|---|---|
| `JOB_LEASE_SECONDS` | `120` | Base lease TTL. Only has to outlast a missed heartbeat, never a job. |
| `JOB_LEASE_SECONDS_BY_WORKLOAD` | `""` | `"RENDER=900,GPU=1800"` |
| `JOB_RECLAIM_INTERVAL_SECONDS` | `15` | Recovery sweep cadence. |
| `JOB_WORKER_POOLS` | `""` | `"SMALL=4,RENDER=2,GPU=1"`; empty = the default plan. |
| `JOB_DRAIN_TIMEOUT_SECONDS` | `30` | Bounded drain. |
| `JOB_WORKER_IDENTITY` | `""` | Override the derived `host:pid:nonce`. |

Setting a lease TTL **shorter than the heartbeat interval** is the one
misconfiguration that breaks the safety argument: every job would look expired
while it is still running. The pool always heartbeats at `TTL/3`, so the
default cannot drift, but a hand-set TTL with a long poll interval can.

---

## 10. Known limitations

- **Cross-workspace spend is prevented by the paid machinery, not by the pool.**
  A `BLOCKED` verdict is decided from the job's own workspace. `paid_provider`
  owns ownership enforcement (`SpendAuthority`, `OwnerlessSpendRefused`) and is
  unchanged by Work 16.
- **`CLAIMED` is derived, not a status value.** `lease_state()` computes it from
  `claimed_at` / `started_at`. A reader that filters on
  `Job.status IN ('QUEUED','RUNNING')` still sees the job as in flight, which is
  the point.
- **`DEFERRED` job types must be declared.**
  `register_paid_job_type(job_type)` marks a handler that reconciles its own
  prior submission. Nothing is inferred: guessing that a handler is idempotent is
  how a duplicate purchase happens.
- **Candidate batch.** A class-pinned worker reads 20 candidate rows per poll and
  widens to 1000 after a *full* miss. A queue deeper than 1000 rows with a whole
  class missing from it would delay that class by one poll interval.
- **No distributed rate limiting.** Pool slots bound concurrency *per process*.
  Across N replicas the effective concurrency is N × slots. The lease bounds
  *ownership*, not throughput.