# Production Observability, Health/Readiness, SLO & Alerts

Work 16 §8, §9, §10. Dependency-free: **no new pip package was added.**

---

## §8 Observability

### 8.1 Metrics

`backend/app/services/observability/metrics.py` — an in-process registry with a
Prometheus text renderer, served at `GET /internal/metrics`
(`text/plain; version=0.0.4`).

`prometheus_client` is **not** installed and adding it is out of scope, so the
registry is ~400 lines of stdlib. The trade-offs that shaped it:

- **Bounded cardinality.** Per-metric series cap (512, `observability_max_series_per_metric`)
  and 120-char label values. The `route` label is the **templated** path
  (`/jobs/{job_id}`), never the raw URL — otherwise any client could mint an
  unbounded series set and exhaust memory. Dropped series increment
  `ymoney_metrics_series_overflow_total` rather than vanishing.
- **Type-safe writes.** Writing a gauge value into a counter is *refused*, not
  merged. A silent corruption here would mislead whichever alert reads it.
- **One snapshot, one lock.** `REGISTRY.snapshot()` gives every alert rule a
  consistent read, so a rule cannot see a numerator from before an increment
  and a denominator from after it.

#### Series inventory — what feeds it today

| Series | Type | Fed today by |
|---|---|---|
| `ymoney_http_requests_total{method,route,status_class}` | counter | HTTP middleware (`main.py`) |
| `ymoney_http_request_duration_seconds{method,route}` | histogram | HTTP middleware |
| `ymoney_http_request_errors_total{method,route}` | counter | HTTP middleware, **5xx only** |
| `ymoney_job_queue_depth{status}` | gauge | `collect_job_queue_depth()` — real `GROUP BY status` on `jobs` |
| `ymoney_job_start_latency_seconds{type}` | histogram | `record_job_started()` (only when the caller has a real enqueue timestamp) |
| `ymoney_job_duration_seconds{type,status}` | histogram | `record_job_finished()` |
| `ymoney_jobs_completed_total{type,status}` | counter | `record_job_finished()` |
| `ymoney_workers_total` | gauge | `collect_worker_fleet()` — counts **alive** tasks in `jobs._worker_tasks` |
| `ymoney_workers_busy` | gauge | `record_job_started/finished()` |
| `ymoney_worker_utilization_ratio` | gauge | derived `busy / alive` |
| `ymoney_job_last_start_timestamp_seconds` | gauge | `record_job_started()` |
| `ymoney_render_duration_seconds{engine,status}` | histogram | `record_render()` |
| `ymoney_render_failures_total{engine}` | gauge | `record_render()` |
| `ymoney_gpu_slots_reserved` | gauge | `collect_gpu_slots()` — real `MEDIA_INTEL_GPU_SLOT` rows |
| `ymoney_gpu_jobs_waiting` | gauge | `collect_gpu_slots()` — queued jobs with `payload.requires_gpu` |
| `ymoney_provider_request_duration_seconds{provider,operation}` | histogram | `record_provider_call()` |
| `ymoney_provider_errors_total{provider,operation}` | counter | `record_provider_call()` |
| `ymoney_publish_failures_total{platform}` | counter | `record_publish_result()` |
| `ymoney_publish_failures_consecutive{platform}` | gauge | `record_publish_result()`, reset on success |
| `ymoney_paid_submission_unknown_total{provider,operation}` | counter | `record_paid_outcome()` with `SubmissionState.SUBMISSION_UNKNOWN` |
| `ymoney_paid_unknown_exposure_usd{provider}` | gauge | `record_paid_outcome()` |
| `ymoney_paid_submissions_total{provider,operation,state}` | counter | `record_paid_outcome()` |
| `ymoney_budget_refusals_total{kind}` | counter | `record_budget_refusal()` |
| `ymoney_db_pool_checked_out` / `_size` / `_utilization_ratio` | gauge | `collect_db_pool()` — real `engine.pool` + `SELECT 1` |
| `ymoney_storage_failures_total{backend,operation}` | counter | `record_storage_operation()` |
| `ymoney_storage_available` | gauge | `collect_storage()` — real write/unlink probe |
| `ymoney_storage_bytes_written_total` | gauge | `record_storage_operation()` |
| `ymoney_collector_up{collector}` | gauge | every collector; **0 on failure** |
| `ymoney_spans_recorded_total{kind,status}` | counter | the trace recorder |
| `ymoney_metrics_series_overflow_total`, `ymoney_registry_uptime_seconds`, `ymoney_build_info` | — | registry internals |

#### Declared-zero placeholder

Exactly one series has no data source:

```
ymoney_gpu_utilization_ratio{worker} 0
```

It is emitted at zero and its HELP text says
`[DECLARED ZERO: unpopulated; will be fed by the GPU worker lane reporting
device utilisation; no GPU worker in this deployment exports one yet]`.

**A zero that is honestly labelled beats a fabricated reading.** The series
exists so the GPU alert rule is real and testable, and so the HELP line tells an
operator reading a scrape which numbers are measured. `test_measured_series_are_not_marked_declared_zero`
asserts this is the *only* such series, so it cannot quietly become normal.

#### Collector honesty

A collector that raises sets `ymoney_collector_up{collector=...} = 0` and
**leaves its own gauges untouched**. Zeroing queue depth during a database
outage would read as "no backlog" — the opposite of the truth. `collect_all()`
isolates every collector so one outage cannot blind the rest.

### 8.2 Logs

`backend/app/services/observability/logging_setup.py`.

One JSON object per line:

```json
{"ts":"2026-10-02T23:45:52.653+05:30","level":"INFO","logger":"app.services.jobs",
 "module":"jobs._worker_loop","line":214,"message":"job worker #0 started",
 "request_id":"9f2c1a7b4e0d3518","workspace_id":"ws-7"}
```

Correlation IDs — `request_id`, `workspace_id`, `job_id`, `campaign_id`,
`content_id`, `provider`, `operation_id` — come from two places:

- a **contextvar**, so a provider lane deep in the stack inherits the request's
  correlation with no call-site plumbing;
- loguru's `bind()`, so an explicit ID **overrides** the ambient one. A
  background job has no inbound request, and inheriting an unrelated task's ID
  would be worse than having none.

`app.core.request_context` (the pre-existing Work 12 store) is **reused**, not
replaced: `request_scope()` sets both stores and restores both, so the request
has exactly one ID across both subsystems.

`X-Request-ID` is honoured inbound but **clamped to 64 chars** and filtered to
`[A-Za-z0-9-_:]` — echoing an unbounded caller string into every log line is a
log-injection and storage-amplification vector.

#### Implementation note that matters

The sink is a loguru **sink function**, not a `format=` callable:

```python
logger.add(json_sink(sys.stderr), format="{message}", ...)
```

loguru memoises a callable `format` into a `string.Formatter` and runs
`format_map` over it — parsing the *source text* of the callable as format
fields. A formatter containing a dict literal raises `KeyError` on its own
source code (observed: `KeyError: '"ts"'`). `Message.record` is the supported
way to reach the record.

### 8.3 Redaction

`backend/app/services/observability/redaction.py`. **Structural, not per-call-site.**

Three layers, each catching what the others miss:

1. **Registered literals** — every credential in `Settings` (scanned
   reflectively, so a secret added later is covered automatically) plus any
   runtime credential via `register_runtime_secret()`. Scrubbed from free text,
   exception messages and third-party dicts.
2. **Key-name patterns** — `api_key`, `Authorization`, `password`,
   `client_secret`, … are replaced by **key name**, whatever the value looks like.
   This catches a credential YMONEY merely passed through.
3. **Value shapes** — `Bearer …`, `sk-…`, `sk-ant-…`, `ghp_…`, `xoxb-…`,
   `AKIA…`, `AIza…`, JWTs, `?api_key=` in URLs, `scheme://user:pass@host`.

The scrub runs as the **last step inside the sink** (`scrub_record`), so no call
site can bypass it by interpolating into a message. `diagnose=True` is forced
off: it serialises locals, and a local can hold a credential that redaction
cannot see inside a traceback string.

**Deliberate non-redaction.** `idempotency_key` is **not** a secret — it is the
reconciliation handle that makes a `SUBMISSION_UNKNOWN` submission traceable.
Redacting any `*_key` would destroy the evidence the Work 15 contract exists to
preserve. `public_key`, `key_id`, `cache_key`, `secret_id`, `workspace_id`,
`campaign_id`, `content_id`, `operation_id` are likewise excluded.
`test_redaction_does_not_destroy_reconciliation_evidence` pins both directions.

### 8.4 Tracing

**What was checked first.** `backend/.venv/Lib/site-packages` contains **no
`opentelemetry*` distribution** and no `prometheus*`. Adding OTel is outside the
Work 16 dependency budget, so `tracing.py` provides the trace *shape* —
parent/child spans, timing, status, structured attributes — with no exporter.

**What it is not.** Not OTLP. `/internal/traces` does not emit an OTLP payload
and `otel_installed: false` is reported in every response. IDs follow the W3C
trace-context shape (32-hex trace, 16-hex span) so a future bridge has something
to map onto. `Tracer` is the swap point.

`GET /internal/traces` — recent spans, filterable by `trace_id`, `kind`,
`min_duration_ms`; `?group_by_trace=true` returns per-trace span trees
(the `API → service → job → provider → DB/storage` view). Buffer is bounded
(2000); drops are counted. An exception is recorded on the span and
**re-raised** — a tracer that swallows an error makes the trace lie.

Span attributes are redacted on output, so a provider span that recorded a
header cannot publish it.

---

## §9 Health / Readiness

### `/livez` — "should the supervisor restart me?"

**Touches no dependency.** A liveness probe that fails because Postgres is slow
tells the orchestrator to kill a healthy process that would have recovered:
restarting converts a dependency outage into a crash loop and destroys evidence.
`test_livez_touches_no_database` monkeypatches `Engine.connect` to raise; any DB
access during `/livez` fails the test.

### `/readyz` — "should traffic be routed here?"

**Critical (503 when failing):**

| Dependency | Probe |
|---|---|
| `database` | `SELECT 1` through `app.db.session_scope` |
| `migrations` | `schema_migrations` exists and is non-empty |
| `job_backend` | the `jobs` table answers a `COUNT(*)` |

**Non-critical (reported, readiness stays READY):** `storage`,
`db_pool_headroom`, and every external provider.

`storage` is deliberately non-critical: a read-only filesystem stops renders
completing but not accepting, planning, budgeting or publishing. Failing
readiness on it would pull **every** replica out of rotation for a condition
only the render lane suffers.

### The provider-outage distinction

`provider_degradation()` reads the **existing** `app.providers.maturity` surface
— the same registry Work 11's ops dashboard uses. No second health stack.
Down/degraded providers are returned with `blocking: false` and surface at
`/api/v1/provider-maturity`; `/readyz` reports them under `degraded`.

Pulling every replica out of the load balancer because one vendor has an
incident converts a partial outage into a total one.

`test_c_readiness_stays_ready_when_an_optional_provider_is_down` forces
**every** provider to `HEALTH_DOWN` and asserts `200`, `status == "ready"`,
`blocking_failures == []`, `providers.degraded is True`,
`providers.blocking is False`.

Probe functions are stored as **names** and resolved at call time
(`resolve_critical_check`), not captured as function objects at import — a tuple
of bound functions would make each probe unreplaceable.

---

## §10 SLO & Alerts

### SLO targets — declarative, **no achieved values**

`GET /internal/slo` returns `measured: false` and an explicit disclaimer.
A compliance percentage cannot be computed from an in-process registry that
resets on restart; printing one derived from five minutes of uptime would be a
fabricated number. Compute achieved values from **retained scrape history**.

Nine objectives: API availability, API latency p95, job start latency, queue
backlog, publish failure rate, render failure rate, unknown paid submissions
(target **exactly 0**), unknown exposure bound, data durability.
`test_every_slo_source_metric_actually_exists` fails if an objective points at a
metric nobody declares.

### Where the numbers come from — §6, and it is one source of truth

```text
environment -> validated Settings -> SLO/alert evaluator -> published verdict
```

`Settings` is constructed at import of `app/core/config.py`, so a threshold that
fails its bound raises `ValidationError` **at startup**, before a request can be
served. It is never coerced into a number nobody chose. The bounds are not
decoration: `ALERT_PUBLISH_FAILURE_STREAK=0` would make `worst >= 0` true for a
healthy system and page on zero failures; `ALERT_QUEUE_STALL_SECONDS=0` would
make any queued job a "stall"; the ceilings exist so a typo (`1e9`) cannot
silently switch paging off.

**Three thresholds are compared against a live metric, and all three come from
`Settings`:**

| Env var | Default | Rule |
|---|---|---|
| `ALERT_UNKNOWN_EXPOSURE_USD` | `1.0` | `unknown_exposure_high` |
| `ALERT_QUEUE_STALL_SECONDS` | `900.0` | `queue_stalled` |
| `ALERT_PUBLISH_FAILURE_STREAK` | `3` | `repeated_publish_failure` |

They are resolved on **every** evaluation (`AlertThresholds.read()`), never
captured at import, so retuning an alert needs no restart and a test can prove a
changed value changes a verdict. `/internal/alerts` reports each rule's live
`threshold` plus a `threshold_source` of `settings:<NAME>` or `constant`, so
"is this configured or is it hard-coded?" is answerable from the endpoint rather
than from a code read.

**The other five thresholds are deliberate CONSTANTS** and will not grow a knob:

| Rule | Threshold | Why it is not configuration |
|---|---|---|
| `paid_submission_unknown` | `> 0` | the objective is exactly zero; a configurable tolerance would be a setting deciding how much money may be spent with an unknown outcome |
| `worker_fleet_unavailable` | `<= 0` | zero durable workers is never an agreed policy |
| `db_unavailable` | `< 1.0` | a collector heartbeat is a boolean; a probe that is 0.5 healthy does not exist |
| `storage_unavailable` | `< 1.0` | a health probe is a boolean |
| `gpu_queue_starvation` | ledger identity | "waiting > 0 and slots <= 0" is an identity, not a magnitude |

**The `SLO_*` settings are a weaker third thing, and the difference is
deliberate.** Nothing is measured, so a target cannot be evaluated. What a target
setting changes is the number the **published objective states** —
`SLO_QUEUE_BACKLOG_MAX=10` republishes `/internal/slo`'s objective as
"<= 10 queued jobs sustained". That is the honest ceiling on what an unmeasured
objective can do, and it is why there is no `slo_api_latency_p95_seconds`: the
"p95 <= 1.0s" objective is wording, and a knob for wording is configurability
with no behaviour behind it.

`SLOTarget` states its number exactly once — a literal `target` **or** a
`setting` + `target_template` — and both or neither raises `ValueError` at
import, so a half-configured objective cannot ship. `GET /internal/alerts` and
`GET /internal/slo` both carry the resolved `thresholds` map, so the ops surface
and the evaluator cannot disagree.

Tests: `tests/test_work16_1_slo_config.py` — every threshold class, the
startup-rejection cases, and AST assertions that a compared threshold is a *name*
resolved from config rather than a literal written back into the rule.

### Alert rules — real evaluators

`GET /internal/alerts` returns definitions **and** current verdicts. Each rule
is a function over a `Snapshot`, so each is tested by feeding it numbers.

| Rule | Severity | Fires when |
|---|---|---|
| `paid_submission_unknown` | CRITICAL | any `SUBMISSION_UNKNOWN` recorded (> 0) |
| `unknown_exposure_high` | CRITICAL | estimated USD at `UNKNOWN_EXPOSURE` > $1.00 |
| `queue_stalled` | CRITICAL | depth > 0 **and** nothing started for > 900 s |
| `worker_fleet_unavailable` | CRITICAL | alive workers ≤ 0 |
| `db_unavailable` | CRITICAL | `collector_up{collector="database"}` < 1 |
| `storage_unavailable` | CRITICAL | `storage_available` < 1 |
| `repeated_publish_failure` | WARNING | ≥ 3 **consecutive** failures for one platform |
| `gpu_queue_starvation` | WARNING | GPU jobs waiting **and** zero slots held |

Design points worth defending:

- **`queue_stalled` needs both conditions.** A deep queue with a recent start is
  healthy backlog; a quiet queue is healthy idleness.
- **`repeated_publish_failure` counts a streak, not a total.** A cumulative
  counter fires forever once a platform has ever failed once. Any success resets.
- **An absent collector heartbeat reads as DOWN.** A collector that never ran is
  not a healthy collector.
- **Unknown paid submissions target zero, not "low".** Every occurrence is
  possible money spent against an unknown outcome.

Thresholds live in `Settings` (`alert_queue_stall_seconds`,
`alert_unknown_exposure_usd`, `alert_publish_failure_streak`) so retuning an
alert does not require a code change — see §10 above for which five thresholds
are deliberately constants and why, and for the startup validation.

> ### ⚠ `gpu_queue_starvation` reads the WORKSPACE ledger, not the device ledger
>
> `ymoney_gpu_slots_reserved` is fed by `collect_gpu_slots()`, which counts
> `jobs` rows of type `MEDIA_INTEL_GPU_SLOT` in status `QUEUED`/`RUNNING`. A held
> workspace slot is parked in **`WAITING`** (`media_intel_runs._try_acquire`
> inserts `status=JobStatus.WAITING.value`, which is also what keeps a live slot
> out of the worker claim query). The filter therefore never matches a held slot
> and the gauge reads 0 — so this rule can fire while VRAM is reserved.
>
> `gpu_scheduler.snapshot()` reads `gpu_reservations` directly and is correct.
> Asserted, not described: `test_neither_guard_moves_the_gpu_slot_gauge_because_the_collector_misses_waiting`
> holds a real composed slot, asserts the gauge is 0.0, and asserts the alert
> fires anyway. The fix is one line in `collect_gpu_slots()`
> (`services/observability/metrics.py`); that file belongs to another lane, so it
> is reported rather than patched here.

### Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /livez` | liveness — never touches the DB |
| `GET /readyz` | readiness — 503 only on a critical dependency |
| `GET /health` | alias for `/readyz` (probes already configured against `/health`) |
| `GET /internal/metrics` | Prometheus text exposition |
| `GET /internal/traces` | recorded spans |
| `GET /internal/slo` | SLO targets + rule definitions |
| `GET /internal/alerts` | rule definitions + live verdicts |
| `GET /internal/collectors` | per-collector up/down |
| `GET /internal/overview` | liveness + readiness + alerts + traces in one call |

These carry **no workspace scope** — an orchestrator cannot authenticate as a
workspace. They expose operational state (counts, dependency up/down,
thresholds), never secrets or customer content.

---

## Settings

```python
observability_enabled: bool = True
observability_json_logs: bool = True
observability_service_name: str = "ymoney"
observability_max_spans: int = 2000
observability_max_series_per_metric: int = 512
```

```python
# §10. `ALERT_*` are COMPARED against a live metric; `SLO_*` are DECLARED and
# change the number the published objective states. Both are validated at
# startup -- the bounds below are pydantic constraints, not documentation.
alert_queue_stall_seconds: float = Field(default=900.0, gt=0.0, le=86_400.0)
alert_unknown_exposure_usd: float = Field(default=1.0, ge=0.0, le=10_000.0)
alert_publish_failure_streak: int = Field(default=3, ge=1, le=100)

slo_api_availability_target: float = Field(default=0.995, gt=0.0, le=1.0)
slo_job_start_latency_seconds: float = Field(default=60.0, gt=0.0, le=86_400.0)
slo_queue_backlog_max: int = Field(default=25, ge=1, le=1_000_000)
slo_publish_failure_rate_max: float = Field(default=0.02, ge=0.0, le=1.0)
slo_render_failure_rate_max: float = Field(default=0.05, ge=0.0, le=1.0)
slo_unknown_exposure_max_usd: float = Field(default=5.0, gt=0.0, le=10_000.0)
```

## Wiring the recording seams

Other lanes call the seams; they never touch a metric name:

```python
from app.services.observability import metrics as m

m.record_paid_outcome("mpt", "render", SubmissionState.SUBMISSION_UNKNOWN,
                      estimated_cost_usd=0.42)
m.record_budget_refusal(m.budget_refusal_kind(exc))
m.record_publish_result("youtube", success=False)
m.record_provider_call("openai", "chat.completions", 1.2, failed=True)
m.record_render("ffmpeg_avatar", 240.0, ok=True)
```

`record_paid_outcome` is the **only** way an unknown submission enters the
registry, and it takes the `SubmissionState` values Work 15 already defines.

## Tests

| File | Covers |
|---|---|
| `backend/tests/test_work16_observability.py` | the registry, sinks, redaction, tracing, probes, and the eight rules driven both ways |
| `backend/tests/test_work16_1_slo_config.py` | §6: settings → validated → evaluator → verdict; startup rejection of nonsense thresholds; and the five thresholds that stay constants |

No autouse workspace scope, no imperative `pytest.skip`, no new fixtures in
`conftest.py`. The PostgreSQL tests in the GPU-admission companion file are
`pytest.mark.skipif`-gated on a reachability probe and skip cleanly with no
server.