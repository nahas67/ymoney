# Production Load Report — Work 16 §7 and §13

**What this document is.** Measured limits for YMONEY, taken against a real
PostgreSQL server and a real connection pool, plus eight controlled failure
drills. Every number below was produced by a run on this machine and is
reproducible with the commands in [§9](#9-reproducing-everything).

**What it is not.** Not a target, not a capacity plan, not a claim about
production hardware. One Windows development host, one PostgreSQL 17 container,
one process, `loopback` networking for the API workload. A number measured here
is the number this configuration does; it is not the number a 16-core cloud host
does. Where a number is unflattering it is printed as measured.

---

## 1. Environment

| | |
|---|---|
| Server | PostgreSQL **17.11** (Debian 17.11-1.pgdg13+2), `max_connections = 100` |
| Container | `w16-pg-alt`, `127.0.0.1:56432` |
| Scratch DB | `w16_load_<random>`, created and dropped `WITH (FORCE)` per run |
| Migrations | **37 applied clean** in 5.2–5.4 s (`create_all` + runner) |
| Pool | `pool_size=10`, `max_overflow=10`, `pool_timeout=30` (shipped config) |
| Drivers | psycopg 3.3.6, SQLAlchemy 2.0.52, httpx 0.28.1 |

**Deviation from the brief, stated up front.** The brief specifies the container
`ymoney-w16-pg` on port `55432`. That container could not be started on this
host:

```
$ docker start ymoney-w16-pg
Error response from daemon: ports are not available: exposing port TCP
0.0.0.0:55432 -> 127.0.0.1:0: listen tcp 0.0.0.0:55432: bind: An attempt was
made to access a socket in a way forbidden by its access permissions.

$ netsh interface ipv4 show excludedportrange protocol=tcp
  55364       55463          <-- 55432 is inside this range
```

Port 55432 sits inside a Windows/Hyper-V reserved range, and the shell is not
elevated, so the range cannot be released. A second container
(`w16-pg-alt`) was therefore started on `127.0.0.1:56432` with the same
credentials, the same image (`postgres:17`), and `YMONEY_LOAD_PG_ADMIN` pointing
at it. **Everything else about the database is as specified.** The server version,
`max_connections`, and the 37 clean migrations are all verified above.

---

## 2. Correcting the harness before measuring

The inherited `backend/app/services/load_harness.py` did not run. Nine defects
were found and fixed; five of them would have produced *plausible-looking but
meaningless* numbers rather than an error.

| # | Defect | Consequence had it shipped | Fix |
|---|---|---|---|
| 1 | `api_session` called `issue_tokens(user, …)`; the signature is `issue_tokens(db, user, …)` | `TypeError` on the first API run | pass the session |
| 2 | `_project_write` added its row **after** the `with session_scope()` block closed | every "successful" write was rolled back; the workload measured nothing and reported 100% success | write inside the scope |
| 3 | `pool_stats` did `int(pool.overflow)`; `QueuePool.overflow` is a **method** | `TypeError` at the end of *every* run, including the passing ones | call it if callable |
| 4 | `_run_coroutine` created a fresh `asyncio.run()` per request while the `httpx` client stayed bound to the first loop | request 2 on each worker died on a cross-loop error **and never returned its pool connection** — a self-inflicted pool leak | one persistent loop per worker thread; drop the client when the loop dies |
| 5 | `_planner` ran against a workspace with no evidenced signals at `RECOMMEND` autonomy | the engine returns early by design: **100% `SKIPPED`, 0% errors, a "perfect" curve measuring a `return`** | `seed_signals()` writes two evidenced observations per topic; workload runs at `APPROVAL` (the lowest mode that permits `CREATE_PLAN_ITEM`) |
| 6 | `asset_upload` / `asset_roundtrip` passed `kind="load"` | 9/9 `CanonicalRuleViolation` — the kind registry correctly rejects it; the workload measured its own argument | use the canonical kind `generated` |
| 7 | `pg_available()` opened a socket at **collection time** regardless of the opt-in | every default-suite run blocked on a connect to a server nobody asked about | short-circuit on `load_enabled()` |
| 8 | `create_app()` (~2.5–4.2 s) was charged to whichever worker ran first | a **2 983 ms p90 on a service whose steady-state p50 is 6.8 ms** | new `warm_api()`, untimed |
| 9 | `SIM105` on the barrier; `SKIPPED`/`format_table` missing from `__all__` | lint | fixed |

Defects 2, 5, 6 and 8 are the dangerous class: all four produced a **green run
measuring nothing**. That is the reason this section exists.

---

## 3. The curve

Reproduce with:

```
python -m tests.test_work16_load --sweep --iterations 6
```

Ladder `c = 1, 2, 4, 8, 16, 24, 32`. 6 iterations per worker. One scratch
database; a fresh 4 000-deep queue primed for the queue-shaped workloads.
`pool_timeout = 8` for the sweep (the ceiling does not depend on it; a 30 s
wait would just spend 30 s per blocked thread reporting the same failure).

```
workload            conc  ops    wall   throughput   p50ms     p90ms     p99ms    ok/ref/skip/fail
api_traffic         1     6        0.15s      39.49/s     24.1     31.3     31.3  6/0/0/0
api_traffic         2     12       0.28s      42.51/s     38.4     69.1     80.3  12/0/0/0
api_traffic         4     24       0.49s      48.94/s     61.2    123.6    167.7  24/0/0/0
api_traffic         8     48       0.78s      61.74/s    102.0    193.5    257.5  48/0/0/0
api_traffic         16    96       2.63s      36.45/s    209.0    892.5   1982.7  96/0/0/0
api_traffic         24    144      2.58s      55.88/s    146.3    927.1   1423.3  144/0/0/0
api_traffic         32    192      3.66s      52.46/s    212.1   1391.3   2346.3  192/0/0/0
asset_roundtrip     1     6        0.44s      13.59/s     73.7     84.7     84.7  6/0/0/0
asset_roundtrip     2     12       0.56s      21.42/s     88.5    108.8    113.9  12/0/0/0
asset_roundtrip     4     24       0.85s      28.35/s    133.0    156.8    162.9  24/0/0/0
asset_roundtrip     8     48       1.51s      31.80/s    236.1    307.0    315.0  48/0/0/0
asset_roundtrip     16    96       3.40s      28.26/s    544.4    669.7    753.3  96/0/0/0
asset_roundtrip     24    144      4.74s      30.40/s    766.3    920.0   1089.4  144/0/0/0
asset_roundtrip     32    192      6.34s      30.30/s   1001.5   1173.1   1310.4  192/0/0/0
asset_upload        1     6        0.28s      21.67/s     44.3     49.8     49.8  6/0/0/0
asset_upload        2     12       0.31s      38.44/s     51.7     57.5     59.5  12/0/0/0
asset_upload        4     24       0.35s      69.30/s     52.9     76.1     78.4  24/0/0/0
asset_upload        8     48       0.80s      60.13/s    126.6    159.6    187.9  48/0/0/0
asset_upload        16    96       1.56s      61.42/s    242.9    295.2    403.8  96/0/0/0
asset_upload        24    144      2.74s      52.58/s    443.0    543.7    672.6  144/0/0/0
asset_upload        32    192      3.80s      50.52/s    603.9    735.5    820.5  192/0/0/0
cost_reserve        1     6        0.27s      21.82/s     33.6     81.3     81.3  6/0/0/0
cost_reserve        2     12       0.34s      35.14/s     52.4     70.0     87.7  12/0/0/0
cost_reserve        4     24       0.41s      58.62/s     56.5     87.4    121.1  24/0/0/0
cost_reserve        8     48       0.64s      75.20/s     90.2    131.0    185.4  48/0/0/0
cost_reserve        16    96       1.13s      85.27/s     92.1    290.3    348.1  96/0/0/0
cost_reserve        24    144      2.32s      62.10/s    148.0    455.0   1697.1  144/0/0/0
cost_reserve        32    192      2.26s      85.04/s    133.7    424.5   1547.0  192/0/0/0
cost_reserve_settle 1     6        0.37s      16.41/s     56.0     71.9     71.9  6/0/0/0
cost_reserve_settle 2     12       0.28s      43.04/s     44.3     52.9     54.1  12/0/0/0
cost_reserve_settle 4     24       0.32s      74.67/s     48.6     59.5     80.5  24/0/0/0
cost_reserve_settle 8     48       0.60s      80.55/s     79.3    145.8    240.5  48/0/0/0
cost_reserve_settle 16    96       1.07s      89.81/s    108.6    250.3    348.0  96/0/0/0
cost_reserve_settle 24    144      1.73s      83.04/s    108.0    427.3   1432.4  144/0/0/0
cost_reserve_settle 32    192      1.82s     105.27/s    128.0    313.8   1242.7  192/0/0/0
editor_autosave     1     6        0.07s      80.53/s     11.6     13.9     13.9  6/0/0/0
editor_autosave     2     12       0.08s     151.03/s     11.8     15.7     17.2  12/0/0/0
editor_autosave     4     24       0.16s     150.12/s     23.5     29.6     35.0  24/0/0/0
editor_autosave     8     48       0.30s     157.76/s     41.6     78.0    109.2  48/0/0/0
editor_autosave     16    96       0.28s     337.04/s     23.9     86.4    196.1  96/0/0/0
editor_autosave     24    144      0.43s     333.16/s     29.2    116.0    298.8  144/0/0/0
editor_autosave     32    192      0.61s     312.37/s     32.3    175.2    465.0  192/0/0/0
inbox_sync          1     6        0.35s      17.28/s     48.7     91.7     91.7  6/0/0/0
inbox_sync          2     12       0.27s      45.07/s     40.9     46.4     47.6  12/0/0/0
inbox_sync          4     24       0.28s      86.25/s     42.3     51.0     54.6  24/0/0/0
inbox_sync          8     48       0.66s      72.72/s     94.6    173.9    206.4  48/0/0/0
inbox_sync          16    96       1.34s      71.60/s    206.4    259.5    310.7  96/0/0/0
inbox_sync          24    144      1.80s      80.17/s    234.4    328.4   1522.9  144/0/0/0
inbox_sync          32    192      2.79s      68.93/s    234.4    428.2   1924.5  192/0/0/0
job_create          1     6        0.07s      87.81/s     10.6     14.1     14.1  6/0/0/0
job_create          2     12       0.07s     180.57/s      9.5     13.3     13.5  12/0/0/0
job_create          4     24       0.08s     306.47/s     12.0     14.4     16.6  24/0/0/0
job_create          8     48       0.15s     319.20/s     22.8     28.6     37.5  48/0/0/0
job_create          16    96       0.31s     311.51/s     43.2     63.5    119.0  96/0/0/0
job_create          24    144      0.49s     291.44/s     57.1     90.0    378.7  144/0/0/0
job_create          32    192      0.74s     260.10/s     64.2    127.4    524.4  192/0/0/0
planner             1     6        1.81s       3.32/s    274.2    357.9    357.9  6/0/0/0
planner             2     12       1.53s       7.85/s    225.5    256.4    390.3  12/0/0/0
planner             4     24       2.00s      11.97/s    285.0    411.6    656.7  24/0/0/0
planner             8     48       3.55s      13.54/s    292.1   1042.2   1912.7  48/0/0/0
planner             16    96       8.40s      11.42/s    374.8   2762.0   6173.6  96/0/0/0
planner             24    144      14.14s      10.18/s    504.1   5373.4  11068.8  144/0/0/0
planner             32    192      17.96s      10.41/s    856.1   5771.6  13226.3  187/0/0/5   <-- BREAKS
project_read        1     6        0.05s     126.16/s      7.9      9.4      9.4  6/0/0/0
project_read        2     12       0.04s     282.90/s      6.1      8.9     10.1  12/0/0/0
project_read        4     24       0.04s     578.67/s      6.4      7.4      7.8  24/0/0/0
project_read        8     48       0.09s     539.77/s     12.5     18.2     22.3  48/0/0/0
project_read        16    96       0.20s     487.08/s     25.9     35.7     64.6  96/0/0/0
project_read        24    144      0.33s     433.44/s     34.0     60.0    221.8  144/0/0/0
project_read        32    192      0.44s     441.03/s     36.3     80.1    276.0  192/0/0/0
project_write       1     6        0.12s      49.50/s     19.2     25.6     25.6  6/0/0/0
project_write       2     12       0.11s     107.98/s     18.0     23.5     25.5  12/0/0/0
project_write       4     24       0.13s     189.12/s     17.0     28.0     32.2  24/0/0/0
project_write       8     48       0.20s     239.81/s     26.4     40.5     41.6  48/0/0/0
project_write       16    96       0.30s     319.74/s     39.4     61.7     91.4  96/0/0/0
project_write       24    144      0.33s     430.51/s     36.1     72.4    237.5  144/0/0/0
project_write       32    192      0.41s     464.56/s     33.2     74.7    275.3  192/0/0/0
publish             1     6        0.41s      14.47/s     65.6     77.5     77.5  6/0/0/0
publish             2     12       0.43s      27.67/s     70.5     76.7     91.0  12/0/0/0
publish             4     24       0.61s      39.11/s     94.8    107.4    126.5  24/0/0/0
publish             8     48       0.98s      48.90/s    148.4    178.1    293.4  48/0/0/0
publish             16    96       1.87s      51.22/s    282.8    398.6    630.7  96/0/0/0
publish             24    144      2.78s      51.78/s    412.9    579.1    877.6  144/0/0/0
publish             32    192      4.26s      45.12/s    575.6    926.5   1772.6  192/0/0/0
publish_idempotent  1     6        0.15s      39.09/s     21.3     40.0     40.0  6/0/0/0
publish_idempotent  2     12       0.12s      51.73/s     15.5     23.6     27.6  6/0/6/0
publish_idempotent  4     24       0.13s      95.17/s     18.8     21.9     26.7  12/0/12/0
publish_idempotent  8     48       0.22s     109.37/s     32.9     41.7     46.2  24/0/24/0
publish_idempotent  16    96       0.50s      96.47/s     72.1    103.8    127.9  48/0/48/0
publish_idempotent  24    144      0.75s      63.73/s     83.9    124.1    521.9  48/0/96/0
publish_idempotent  32    192      0.91s      52.65/s     83.8    143.3    630.6  48/0/144/0
worker_claim        1     6        0.31s      19.38/s     48.8     65.5     65.5  6/0/0/0
worker_claim        2     12       0.36s      33.58/s     58.3     64.2     65.1  12/0/0/0
worker_claim        4     24       0.55s      43.29/s     87.9     98.4    122.3  24/0/0/0
worker_claim        8     48       0.89s      53.88/s    108.6    214.8    339.1  48/0/0/0
worker_claim        16    96       2.06s      46.54/s    152.1    580.3   1572.9  96/0/0/0
worker_claim        24    144      4.09s      35.19/s    243.1   1441.5   3046.4  144/0/0/0
worker_claim        32    192      6.20s      30.95/s    200.4   1805.7   4115.4  192/0/0/0
```

### 3.1 Where each workload stops scaling

Throughput is `(ok + refused) / wall`. Latencies are nearest-rank percentiles of
the observed samples — no interpolation, so a p99 here is a value something
actually did.

| Workload | c=1 | peak | peak at | c=32 | p99 @ c=32 | first failure |
|---|---|---|---|---|---|---|
| `api_traffic` | 39.5/s | **61.7/s** | c=8 | 52.5/s | 2 346 ms | none ≤ 32 |
| `asset_roundtrip` | 13.6/s | **31.8/s** | c=8 | 30.3/s | 1 310 ms | none ≤ 32 |
| `asset_upload` | 21.7/s | **69.3/s** | c=4 | 50.5/s | 821 ms | none ≤ 32 |
| `cost_reserve` | 21.8/s | **85.3/s** | c=16 | 85.0/s | 1 547 ms | none ≤ 32 |
| `cost_reserve_settle` | 16.4/s | **105.3/s** | c=32 | 105.3/s | 1 243 ms | none ≤ 32 |
| `editor_autosave` | 80.5/s | **337.0/s** | c=16 | 312.4/s | 465 ms | none ≤ 32 |
| `inbox_sync` | 17.3/s | **86.2/s** | c=4 | 68.9/s | 1 925 ms | none ≤ 32 |
| `job_create` | 87.8/s | **319.2/s** | c=8 | 260.1/s | 524 ms | none ≤ 32 |
| `planner` | 3.3/s | **13.5/s** | c=8 | 10.4/s | **13 226 ms** | **c=32 — 5/192 `TimeoutError`** |
| `project_read` | 126.2/s | **578.7/s** | c=4 | 441.0/s | 276 ms | none ≤ 32 |
| `project_write` | 49.5/s | **464.6/s** | c=32 | 464.6/s | 275 ms | none ≤ 32 |
| `publish` | 14.5/s | **51.8/s** | c=24 | 45.1/s | 1 773 ms | none ≤ 32 |
| `publish_idempotent` | 39.1/s | **109.4/s** | c=8 | 52.7/s | 631 ms | none ≤ 32 |
| `worker_claim` | 19.4/s | **53.9/s** | c=8 | 30.9/s | 4 115 ms | none ≤ 32 |

**Reading this honestly.**

* **Only one workload actually broke, and it is `planner`, at c=32.** Its p99 is
  **13.2 s** against a **0.27 s** p50 at c=1 — a 49× tail — and 5 of 192
  operations died on a pool `TimeoutError`.
* **Eleven of the fourteen have a knee between c=4 and c=16.** Past the knee,
  extra concurrency buys latency, not throughput: `api_traffic` peaks at c=8 and
  is 15% *slower* at c=32; `inbox_sync` peaks at c=4 and is 20% slower at c=32;
  `worker_claim` peaks at c=8 and is 43% slower at c=32. The pool is not the
  limit here — the pool was measured idle (`checked_out=0`, 10 backends) at the
  end of every one of those rungs.
* **`publish_idempotent`'s `skip` column is the guarantee working, not failing.**
  Each operation is a matched pair of enqueues with the same key: at c=32, 48
  pairs produced 48 jobs and 144 dedupes. `ok/ref/skip/fail = 48/0/144/0`.
* **This host is not CPU-bound at these numbers.** Twelve of fourteen workloads
  flatten rather than collapse, which is the signature of a shared serialisation
  point rather than of saturated cores.

---

## 4. The measured bottleneck, with evidence

**Not the database, and not the pool ceiling for most workloads.** At the end of
every rung above, `pool.checked_out == 0` and `pg_stat_activity` shows 10
backends — the pool's idle size. The pool is *available* at c=32 for everything
except `planner`.

**The bottleneck is connections held across in-database lock waits.** For
`planner`, sampled directly at c=32 against **one** workspace:

```
planner c=32 ONE workspace: wall=7.07s pool_capacity=20
                            peak_checked_out=30 peak_pg_backends=21
```

Thirty connections outstanding against a twenty-slot pool, held for the whole
7 seconds. The mechanism is not speculation and not a guess from the latency
curve: 32 planner runs on one workspace serialise on that workspace's rows, and
a thread **blocked on a row lock still owns its pooled connection**. The pool is
therefore consumed by threads that are doing no I/O at all, and the queue behind
them is what pushes p99 to 13.2 s and takes the last five operations past
`pool_timeout`.

**Why it is not accumulation.** A separate run measured planner cost against
accumulated plan count on one workspace:

```
after  50 plans:  163.2 ms
after 100 plans:  227.0 ms
after 150 plans:  470.5 ms
after 200 plans:  230.3 ms
after 400 plans:  224.1 ms
```

Flat after warm-up. The planner does **not** degrade as a workspace accumulates
plans, so the c=32 failure is contention, not growth. (One hypothesis — that
the 470 ms at 150 was the start of a leak — is not supported by 200/250/300/350
all returning to ~210–230 ms.)

**The second bottleneck: `worker_claim` starves when the queue is shallow.**
`claim_next` reads candidates with `FOR UPDATE SKIP LOCKED`. When concurrent
pollers hold every candidate row's lock, a poller's candidate set comes back
**empty** and `claim_next` returns `None` on its first read — `_CANDIDATE_TRIES`
is never reached, because the empty-set short circuit fires first. Measured:

```
depth=20    pollers=20  winners=5   empty_handed=15  starvation=0.75
depth=100   pollers=20  winners=20  empty_handed=0   starvation=0.00
depth=1000  pollers=20  winners=20  empty_handed=0   starvation=0.00
depth=20    pollers=8   winners=8   empty_handed=0   starvation=0.00
depth=100   pollers=8   winners=8   empty_handed=0   starvation=0.00
```

Starvation appears when **queue depth ≤ concurrent pollers**, and not otherwise.
With 20 jobs and 20 pollers, 15 of 20 workers were told "nothing to do" while 15
jobs sat `QUEUED`. In the committed test (`test_one_worker_wins_each_job…`) the
rate is **0.66** over 120 attempts. *This is reported, not fixed:
`app/services/job_leases.py` belongs to another lane.* It does **not** break the
exactly-once property — the conditional `UPDATE` still elects one winner per job
— it breaks throughput and adds latency for no reason.

---

## 5. Pool saturation and recovery

`backend/tests/test_work16_load.py::test_pool_saturation_times_out_callers_and_then_serves_again`

Deliberately small pool — `pool_size=4`, `max_overflow=2`, `pool_timeout=1.5` —
so a 30-second wait is survivable inside a test. **The ceiling is the same
shape; only the wait differs.** Server `max_connections` is 100, so the
20-connection shipped pool has 5× headroom against the server.

```
pool drill: capacity=6 contenders=18 holder_timeouts=12
            holder_outcomes=['TimeoutError', 'ok'] caller_error=TimeoutError
            peak_backends=6
```

Three claims, each asserted:

1. **It is the pool, not the server.** `pg_stat_activity` peaked at exactly
   **6** backends for this database — `pool_size + max_overflow`, no more. With
   `max_connections=100` available, a backend count of 6 is the application
   declining to hand out a seventh.
2. **A real caller timed out.** `cost.reserve_spend` — the most contended path in
   the product — raised `sqlalchemy.exc.TimeoutError` with `QueuePool` in the
   message while the pool was full. A harness counting its own thread failures
   would not have shown that the product's own entry point is what breaks.
3. **It recovers.** After every holder returned, `checked_out == 0` and a fresh
   `reserve_spend` succeeded.

**And the timed-out call left nothing behind.** The workspace ledger after the
drill is `$0.01` — the recovery call alone. The saturated call raised and wrote
no reservation, so a pool timeout is not an ownerless spend. That is asserted,
not assumed.

---

## 6. Cross-workspace isolation under load

`test_cross_workspace_budget_isolation_under_load`

Two workspaces, A capped at `$0.02`, B at `$50.00` (a cap B can never
approach), 16 threads × 4 iterations round-robin:

```
isolation: A_total=$0.0200 B_total=$0.3200 ok=34 refused=30 errors={}
```

* A: `$0.0200` against a `$0.0200` cap. Not over. Not under.
* B: `$0.3200` = **32 × `$0.01`** — every one of B's 32 attempts succeeded, none
  refused.
* 30 refusals, all of them A's.

---

## 7. Failure and chaos drills (§13)

`backend/tests/test_work16_chaos.py` — 8 drills. Reproduce:

```
set YMONEY_LOAD_TESTS=1
set YMONEY_LOAD_PG_ADMIN=postgresql://ymoney:ymoney_w16@127.0.0.1:56432/postgres
set YMONEY_LOAD_PG_CONTAINER=w16-pg-alt
python -m pytest tests/test_work16_chaos.py -m "live and slow" -s
```

### 7.1 What was REAL and what was simulated

| Drill | Method | Verdict |
|---|---|---|
| API restart | `taskkill /F` on a live process + a genuinely cold second interpreter | **REAL process death.** Not a supervisor reload — there is no uvicorn here, and it is not claimed to be one. From the data layer's point of view it *is* an API restart. |
| Worker kill | `taskkill /F` on a process holding a RUNNING job, a begun run and an open paid reservation | **REAL abrupt termination.** No drain, no release, no `finally`. |
| Render worker death | `taskkill /F` after the provider accepted | **REAL.** |
| DB restart | `docker restart w16-pg-alt` | **REAL.** |
| Queue interruption | `pg_terminate_backend` on live backends | **REAL.** |
| Network / provider timeout | real TCP connect to `10.255.255.1:9`; provider is a double | Socket **REAL**, provider **DOUBLE**. Never called "live". |
| Storage interruption | storage root renamed away and replaced by a **file** | **SIMULATED**, deterministically. Not a SAN outage. |
| GPU worker death | a registered synthetic 8 GB CUDA device; a HELD reservation whose holder stops renewing | **SIMULATED, and there is no GPU here** — `torch` is not installed and no CUDA device exists on this host. What is exercised is the evidence path: a stale reservation and the sweep that frees it. |

### 7.2 Results, as printed by the runs

```
api restart:        killed rc=1 pid=228  cost_rows=1 money=$0.02
worker kill:        rc=1 pid=2100 job=9e50bf17… verdict=BLOCKED cost_rows=1 money=$0.0500
render death:       rc=1 pid=20672 verdict=REATTACH
                    remote=render-remote-eb01de26… cost_rows=1
db restart:         container=w16-pg-alt downtime=2.41s downtime_error=OperationalError
                    new_backends=1 reused=0 rows_after=1
storage interruption: failure=FileExistsError pending_promise=0ed83bee…
                    finalized_leaked=0 canonical_intact=3 leftovers=0
network timeout:    real_socket=TimeoutError: timed out in 1.01s
                    undelivered=RELEASED ambiguous=UNKNOWN_EXPOSURE reentry=BLOCKED
gpu worker death:   freed=1 live_kept=True slot_deadline=2026-10-03 11:15:39 re_admitted=5a345e31
queue interruption: backends_killed=8 poller_errors=['OperationalError']
                    claimed=16 reclaimed=18 ledger_rows=1
```

Counts and pids vary run to run; the invariants did not.

### 7.3 The six invariants, per drill

**Durable recovery.** Every drill. `db restart` is the strongest: **2.08–2.41 s**
of downtime across runs, during which a real `reserve_spend` raised
`OperationalError` rather than succeeding; afterwards `new_backends=1 reused=0`
— the pool discarded every socket the restart killed, which is `pool_pre_ping`
doing its job, and the service accepted new work immediately.

**No duplicate publication.** Asserted everywhere, and it is the assertion the
unique index carries: 8 threads enqueueing one publish key produced
`outcomes=['IntegrityError', 'accepted', 'deduped']` and **exactly 1 row**.
*Finding, reported not fixed:* the sequential path returns `None` for a
duplicate; the concurrent path raises `IntegrityError`. Uniqueness is enforced
by the database, which is the stronger guarantee, but the caller-visible shape
differs between the two paths. `app/services/jobs.py` belongs to another lane.

**No duplicated paid submission.** Three drills prove it:

* *worker kill* — an **open** paid reservation existed for the dead worker's job;
  `assess_paid_reentry` returned **`BLOCKED`**. One money row, `$0.0500`.
* *render worker death* — the provider had already **accepted** and returned a
  remote id; the verdict is **`REATTACH`** to the row that already owns it, never
  a resubmit. `reattach_by_remote_id` returned the **same `entry_id`** the dead
  worker had written. One money row.
* *network timeout* — the ambiguous case produces `UNKNOWN_EXPOSURE` (kept,
  marked, amount not invented) and the next attempt is **`BLOCKED`**.

**No ownerless spend.** Asserted by a shared helper on every drill: every money
row names its workspace, and none has an empty one. The undelivered-timeout case
goes further and asserts `$0.00` in the ledger — `mark_rejected(
nothing_billed=True)` **voids** the reservation, because a kept row would shrink
a workspace's budget for work that never happened.

**No corrupted timeline.** Storage interruption: the upload raised
`FileExistsError` (the storage root is a file, so every join under it fails);
**0** rows were published `FINALIZED`; **1** `PENDING` promise was left — a record
of an in-flight write, never content under a name that claims to be complete;
**3/3** previously canonical objects read back byte-identical; **0** `.part`
leftovers after recovery. The write lane serves again once the root is restored.

**No lost canonical state.** Snapshot-compared before and after on every drill:
ORM rows, money rows, asset checksums and remote ids all read back identical.

---

## 8. Mutation tests — the guards are load-bearing

Five critical guards, each broken **at runtime** by rebinding a module attribute
in the harness process. No file in another lane is edited, so there is no window
in which a shared repo holds a broken `cost.py` or `job_leases.py`; the revert is
a process exit.

| Guard | Unmutated | Mutation | Real failure |
|---|---|---|---|
| **(a) cap fits fewer reservations than the concurrency** — 16 threads, `$0.05` cap, `$0.01` each | `ok=5 refused=27 failed=0` total **`$0.0500`** | workspace row lock removed (`exclusive_workspace_lock` → no-op) | `ok=11 refused=21` total **`$0.1100`** — **2.2× the cap** |
| **(b) concurrent worker claims** — 20 jobs, 20 pollers | `running=20 distinct_owners=20 of 20`; 0 jobs claimed twice | `_take` forced to always return `WON` | `distinct_job_ids=1 ids_awarded_more_than_once=1` — **one job awarded to several workers** |
| **(c) killed-worker recovery** | lease TTL 120 s; `first=1 second=0`, `status=RETRYING`, `owner=''`, `lost_owner='mut-ghost'` — **recovered exactly once** | `lease_expires_at <= now` condition dropped (the old `recover_orphans` behaviour) | `live_owner=mut-live-a lease_still_valid=True first_sweep=1 stolen_from_live_worker=True` — **a LIVE worker's job handed to a second worker** |
| **(d) pool recovery** | `caller_error=TimeoutError`, drained to `checked_out=0`, `recovered=True` | `QueuePool._do_return_conn` neutered — connections never checked in | `leaked_connections=6`, pool frozen at `checked_out=6 size=4 overflow=2`, `caller_error=TimeoutError` — **the system never serves again** |
| **(e) cross-workspace isolation** | `A=$0.0200` (cap `$0.02`, 2 successes), `B=$0.3200` (32 successes, 0 refusals) | spend window made global (`_window_totals` ignores `workspace_id`) | `A=$0.0000` (cap `$0.02`), `refused=32` — **A's own untouched budget was refused because B spent first: one tenant consumed another's** |

Every guard fails loudly under mutation. (b)'s baseline `ok` figure varies between
runs — 20 winners when the queue drains within the rounds, fewer when pollers
starve — which is why the assertion is on the **database** (`running=20`,
`distinct_owners=20`), not on the harness's own tally.

---

## 9. Reproducing everything

```
set YMONEY_LOAD_TESTS=1
set YMONEY_LOAD_PG_ADMIN=postgresql://ymoney:ymoney_w16@127.0.0.1:56432/postgres
set YMONEY_LOAD_PG_CONTAINER=w16-pg-alt

cd backend
python -m pytest tests/test_work16_load.py  -m "live and slow" -q -s
python -m pytest tests/test_work16_chaos.py  -m "live and slow" -q -s
python -m tests.test_work16_load --sweep --iterations 6
```

**16 tests, all opt-in, two independent declarative gates.** Both files carry
`@pytest.mark.live` and `@pytest.mark.slow` — markers this repo already declares,
which `addopts = -m 'not live and not slow'` **deselects**, so a default suite
run does not even collect these bodies — plus a declarative
`pytest.mark.skipif(not (load_enabled() and pg_available()))`. There is no
imperative `pytest.skip(` anywhere in either file. The default SQLite suite needs
no PostgreSQL and pays no collection-time socket.

Tests: **8 load + 8 chaos = 16.**

---

## 10. Findings in files this lane does not own

Reported, not fixed, not worked around.

1. **`claim_next` starves when queue depth ≤ concurrent pollers.** Up to **0.75**
   of polls return `None` while jobs are `QUEUED`. `FOR UPDATE SKIP LOCKED` plus
   the empty-candidate-set short circuit means `_CANDIDATE_TRIES` is never
   reached. Exactly-once is unaffected. → `app/services/job_leases.py`.
2. **A concurrent duplicate `enqueue` raises `IntegrityError` instead of
   returning `None`.** The unique index holds — no duplicate publish job — but
   the caller-visible shape differs from the sequential path. →
   `app/services/jobs.py`.
3. **The planner holds a pooled connection across its internal lock waits**, so
   32 concurrent runs exhaust the 20-slot pool while doing no I/O. →
   `app/engine/planning/engine.py` and/or the pool configuration.

---

## 11. What this lane could not honour

* **Port 55432 / container `ymoney-w16-pg`.** Inside a Windows reserved range
  (`55364–55463`); the shell is not elevated. Used `w16-pg-alt` on 56432 with the
  same image and credentials. Detailed in §1.
* **No real GPU workload.** `torch` is not installed and there is no CUDA device.
  The GPU drill is the stale-reservation/lease evidence path only, and says so in
  its own docstring, its print line, and the table in §7.1.
* **No real ASGI supervisor.** "API restart" is a real forced process kill plus a
  genuinely cold process. It is not a uvicorn reload and is not described as one.
* **No real storage outage.** The storage interruption is a deterministic
  filesystem substitution, not a volume disappearing.
* **Single-host numbers.** One Windows box, loopback networking, no separate app
  tier. Absolute throughput here is not a production capacity figure; the
  *breaking points*, the *pool behaviour* and the *starvation threshold* are the
  transferable results.
* **The sweep uses `pool_timeout=8`**, not the shipped 30 s, so a rung above the
  ceiling terminates. The ceiling is unaffected; the wait is. Set
  `YMONEY_LOAD_POOL_TIMEOUT=30` to reproduce the shipped wait.