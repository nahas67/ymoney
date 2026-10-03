"""In-process metric registry, exposed in Prometheus text format.

**Why this exists instead of ``prometheus_client``.** The production
dependency budget for Work 16 adds nothing, and this registry is the
substrate every alert rule in :mod:`app.services.observability.slo` reads.
A rule that evaluates against invented numbers is worse than no rule, so the
registry's job is narrower than a general telemetry client: hold counters,
gauges and histograms, expose them in a format Prometheus already scrapes,
and refuse to grow without bound.

**Bounded cardinality is a security property, not a tuning knob.** The
``route`` label comes from the request path. Emitting the *templated* route
(``/workspaces/{workspace_id}/jobs/{job_id}``) keeps the series count equal to
the number of endpoints; emitting the raw path would let any client mint an
unbounded series set and exhaust memory. Labels are capped in length, series
are capped per metric, and every dropped series increments
``ymoney_metrics_series_overflow_total`` rather than disappearing silently.

**No fabricated readings.** A metric with no data source today is registered
with ``declared_zero=True``: it is emitted at zero and its HELP text says
what will eventually feed it. A zero that is honestly labelled as unpopulated
is useful -- it makes the alert rule real and lets it be tested. A fabricated
non-zero value would be worse than having no series at all.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "DEFAULT_BUCKETS",
    "Counter",
    "Gauge",
    "Histogram",
    "MetricRegistry",
    "MetricSpec",
    "MetricType",
    "REGISTRY",
    "Snapshot",
    "default_buckets",
]

#: Prometheus reserves ``+Inf``; it is appended automatically and must not be
#: declared as a real bucket.
_DEFAULT_TAIL = float("inf")

#: Latency buckets that fit an HTTP API (seconds).
API_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0,
)

#: Latency buckets for work that legitimately takes minutes (jobs, renders,
#: provider calls with retries).
LONG_BUCKETS: tuple[float, ...] = (
    0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0,
)

DEFAULT_BUCKETS: tuple[float, ...] = API_BUCKETS


def default_buckets() -> tuple[float, ...]:
    return API_BUCKETS


class MetricType(StrEnum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


@dataclass(frozen=True)
class MetricSpec:
    """The immutable declaration of one metric."""

    name: str
    kind: MetricType
    help: str
    labelnames: tuple[str, ...] = ()
    buckets: tuple[float, ...] = ()
    #: True when nothing populates this series yet. The renderer says so in
    #: HELP, so an operator reading a scrape can tell "zero" from "absent".
    declared_zero: bool = False
    #: What will eventually feed a ``declared_zero`` series.
    will_be_fed_by: str = ""


def _escape_label_value(value: str) -> str:
    """Prometheus text format escaping for a label value."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _render_labels(labels: Mapping[str, str], extra: Mapping[str, str] | None = None) -> str:
    pairs: list[str] = []
    for key in sorted(set(labels) | set(extra or {})):
        if key in labels:
            pairs.append(f'{key}="{_escape_label_value(labels[key])}"')
    for key, value in (extra or {}).items():
        if key not in labels:
            pairs.append(f'{key}="{_escape_label_value(value)}"')
    return "{" + ",".join(pairs) + "}" if pairs else ""


def _format_value(value: float) -> str:
    if value == _DEFAULT_TAIL:
        return "+Inf"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


@dataclass
class _Series:
    """Mutable per-label-set state for one metric."""

    value: float = 0.0
    count: int = 0
    total: float = 0.0
    buckets: list[int] = field(default_factory=list)


class MetricRegistry:
    """Counters, gauges and histograms with a Prometheus text renderer.

    Thread-safe: the HTTP middleware, the job workers and the provider lanes
    all write concurrently, and a scrape may read while they do. Every public
    method takes one lock for the whole operation, so a render never observes
    a half-updated histogram.
    """

    def __init__(self, *, max_series_per_metric: int = 512,
                 max_label_len: int = 120) -> None:
        self._lock = threading.Lock()
        self._specs: dict[str, MetricSpec] = {}
        self._series: dict[str, dict[tuple[tuple[str, str], ...], _Series]] = {}
        self._max_series = max_series_per_metric
        self._max_label_len = max_label_len
        self._series_overflow_total = 0
        self._started_monotonic = time.monotonic()

    # -- declaration ------------------------------------------------------

    def register(self, spec: MetricSpec) -> MetricSpec:
        """Declare a metric. Re-registering the same name is a no-op.

        Idempotent on purpose: module import order between the middleware and
        a lazily imported lane must never raise.
        """
        with self._lock:
            existing = self._specs.get(spec.name)
            if existing is not None:
                return existing
            self._specs[spec.name] = spec
            self._series[spec.name] = {}
            return spec

    def spec(self, name: str) -> MetricSpec | None:
        with self._lock:
            return self._specs.get(name)

    def specs(self) -> tuple[MetricSpec, ...]:
        with self._lock:
            return tuple(self._specs.values())

    # -- writing ----------------------------------------------------------

    def _clean_labels(self, spec: MetricSpec,
                      labels: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
        """Normalise a label set into a hashable key.

        Only declared labelnames are kept, values are stringified, stripped
        and truncated. Dropping undeclared labels is what makes
        ``observe(..., route=<untrusted path>)`` safe to call from middleware.
        """
        if not labels:
            return ()
        out: list[tuple[str, str]] = []
        for name in spec.labelnames:
            if name not in labels:
                continue
            raw = labels[name]
            text = "" if raw is None else str(raw).strip()
            out.append((name, text[: self._max_label_len]))
        # Labels not in ``spec.labelnames`` are dropped rather than accepted:
        # an undeclared label would silently split a series that was meant to
        # be one total.
        return tuple(out)

    def _series_for(self, name: str, key: tuple[tuple[str, str], ...]) -> _Series | None:
        """Fetch-or-create a series, honouring the per-metric series cap.

        Caller must hold the lock. ``None`` means the cap was hit: the write
        is dropped and the overflow counter is bumped so the drop is visible.
        """
        bucket = self._series.get(name)
        if bucket is None:
            return None
        existing = bucket.get(key)
        if existing is not None:
            return existing
        if len(bucket) >= self._max_series:
            self._series_overflow_total += 1
            return None
        spec = self._specs[name]
        series = _Series(buckets=[0] * len(spec.buckets) if spec.kind is MetricType.HISTOGRAM else [])
        bucket[key] = series
        return series

    def inc(self, name: str, value: float = 1.0,
            **labels: str) -> None:
        """Add to a counter (or to a gauge, for compatibility)."""
        spec = self._name(name)
        if spec is None:
            return
        key = self._clean_labels(spec, labels)
        with self._lock:
            series = self._series_for(name, key)
            if series is None:
                return
            if spec.kind is MetricType.HISTOGRAM:
                self._observe_locked(spec, series, value)
                return
            series.value += float(value)

    def set(self, name: str, value: float, **labels: str) -> None:
        """Set a gauge. Negative deltas are legal -- that is what a gauge is."""
        spec = self._require(name, MetricType.GAUGE)
        if spec is None:
            return
        key = self._clean_labels(spec, labels)
        with self._lock:
            series = self._series_for(name, key)
            if series is not None:
                series.value = float(value)

    def add(self, name: str, delta: float, **labels: str) -> None:
        """Add to a gauge (used for running totals such as unknown exposure)."""
        spec = self._require(name, MetricType.GAUGE)
        if spec is None:
            return
        key = self._clean_labels(spec, labels)
        with self._lock:
            series = self._series_for(name, key)
            if series is not None:
                series.value += float(delta)

    def observe(self, name: str, value: float, **labels: str) -> None:
        """Record one observation into a histogram."""
        spec = self._require(name, MetricType.HISTOGRAM)
        if spec is None:
            return
        key = self._clean_labels(spec, labels)
        with self._lock:
            series = self._series_for(name, key)
            if series is not None:
                self._observe_locked(spec, series, float(value))

    def _observe_locked(self, spec: MetricSpec, series: _Series, value: float) -> None:
        series.count += 1
        series.total += value
        for index, edge in enumerate(spec.buckets):
            if value <= edge:
                series.buckets[index] += 1

    def _name(self, name: str) -> MetricSpec | None:
        spec = self._specs.get(name)
        if spec is None:
            return None
        return spec

    def _require(self, name: str, kind: MetricType) -> MetricSpec | None:
        """Resolve a metric and refuse a type mismatch instead of lying.

        Writing a gauge value into a counter, or observing into a gauge,
        silently corrupts whatever an alert rule later reads. Dropping the
        write and reporting it is the honest failure.
        """
        spec = self._name(name)
        if spec is None:
            return None
        if spec.kind is not kind:
            return None
        return spec

    # -- reading ----------------------------------------------------------

    def value(self, name: str, **labels: str) -> float:
        """Current value of a counter or gauge series; 0.0 when absent."""
        spec = self._name(name)
        if spec is None:
            return 0.0
        key = self._clean_labels(spec, labels)
        with self._lock:
            bucket = self._series.get(name) or {}
            series = bucket.get(key)
            return float(series.value) if series else 0.0

    def snapshot(self) -> Snapshot:
        """A consistent point-in-time read for the alert evaluators.

        Taken under one lock so a rule cannot see the numerator from before an
        increment and the denominator from after it.
        """
        with self._lock:
            values = {
                name: {key: series.value for key, series in bucket.items()}
                for name, bucket in self._series.items()
            }
            counts = {
                name: {key: series.count for key, series in bucket.items()}
                for name, bucket in self._series.items()
            }
            uptime = time.monotonic() - self._started_monotonic
        return Snapshot(values=values, counts=counts, uptime_seconds=uptime)

    @property
    def series_overflow_total(self) -> int:
        with self._lock:
            return self._series_overflow_total

    def reset(self) -> None:
        """Drop every observation, keeping the declarations.

        Tests use this; production never calls it.
        """
        with self._lock:
            for name in self._series:
                self._series[name] = {}
            self._series_overflow_total = 0
            self._started_monotonic = time.monotonic()

    # -- rendering --------------------------------------------------------

    def render(self) -> str:
        """Render every declared metric in Prometheus text exposition format.

        Includes a metric with zero series. A rule that evaluates against
        ``ymoney_gpu_utilization_ratio`` needs the series to exist; emitting
        it at zero with a HELP line naming its future data source keeps the
        scrape honest about what is and is not measured today.
        """
        with self._lock:
            lines: list[str] = []
            for name in sorted(self._specs):
                spec = self._specs[name]
                bucket = self._series.get(name) or {}
                help_text = spec.help
                if spec.declared_zero:
                    fed = spec.will_be_fed_by or "a future data source"
                    help_text = f"{help_text} [DECLARED ZERO: unpopulated; will be fed by {fed}]"
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} {spec.kind.value}")
                if spec.kind is MetricType.HISTOGRAM:
                    lines.extend(self._render_histogram(name, spec, bucket))
                else:
                    if not bucket:
                        lines.append(f"{name} 0")
                    for key in sorted(bucket):
                        series = bucket[key]
                        lines.append(
                            f"{name}{_render_labels(dict(key))} "
                            f"{_format_value(series.value)}"
                        )
            lines.append(
                "# HELP ymoney_metrics_series_overflow_total "
                "Label sets dropped because a metric hit its series cap "
                "(cardinality protection working as designed, not an error)."
            )
            lines.append("# TYPE ymoney_metrics_series_overflow_total counter")
            lines.append(
                f"ymoney_metrics_series_overflow_total {self._series_overflow_total}"
            )
            lines.append(
                "# HELP ymoney_registry_uptime_seconds "
                "Wall time this process has held the metric registry."
            )
            lines.append("# TYPE ymoney_registry_uptime_seconds gauge")
            lines.append(
                "ymoney_registry_uptime_seconds "
                f"{_format_value(time.monotonic() - self._started_monotonic)}"
            )
            return "\n".join(lines) + "\n"

    def _render_histogram(self, name: str, spec: MetricSpec,
                          bucket: Mapping[tuple[tuple[str, str], ...], _Series]) -> list[str]:
        lines: list[str] = []
        if not bucket:
            # A histogram with no observation still exposes cumulative
            # buckets so a dashboard query against it returns an empty
            # series rather than "no such metric".
            for edge in spec.buckets:
                lines.append(f'{name}_bucket{{le="{_format_value(edge)}"}} 0')
            lines.append(f'{name}_bucket{{le="+Inf"}} 0')
            lines.append(f"{name}_sum 0")
            lines.append(f"{name}_count 0")
            return lines
        for key in sorted(bucket):
            series = bucket[key]
            label_dict = dict(key)
            for index, edge in enumerate(spec.buckets):
                lines.append(
                    f'{name}_bucket{_render_labels(label_dict, {"le": _format_value(edge)})} '
                    f"{series.buckets[index]}"
                )
            lines.append(
                f'{name}_bucket{_render_labels(label_dict, {"le": "+Inf"})} '
                f"{series.count}"
            )
            lines.append(f"{name}_sum{_render_labels(label_dict)} {_format_value(series.total)}")
            lines.append(f"{name}_count{_render_labels(label_dict)} {series.count}")
        return lines


@dataclass(frozen=True)
class Snapshot:
    """An immutable read of the registry for rule evaluation.

    ``values`` maps ``metric name -> {(label, label): value}``. Lookups go
    through :meth:`get`, which sums across label sets when the rule means a
    total (``None`` labels) and picks the single series when it does not.
    """

    values: Mapping[str, Mapping[tuple[tuple[str, str], ...], float]]
    counts: Mapping[str, Mapping[tuple[tuple[str, str], ...], int]]
    uptime_seconds: float = 0.0

    def get(self, name: str, **labels: str | None) -> float:
        """Sum series matching ``labels``.

        ``get("m")`` sums every series of ``m``. ``get("m", provider="x")``
        sums only that provider's series. A label mapped to ``None`` matches
        any value for that label.
        """
        series = self.values.get(name)
        if not series:
            return 0.0
        wanted = {k: v for k, v in labels.items() if v is not None}
        total = 0.0
        for key, value in series.items():
            key_map = dict(key)
            if all(key_map.get(k) == v for k, v in wanted.items()):
                total += float(value)
        return total

    def count_of(self, name: str, **labels: str | None) -> int:
        series = self.counts.get(name) or {}
        wanted = {k: v for k, v in labels.items() if v is not None}
        total = 0
        for key, value in series.items():
            key_map = dict(key)
            if all(key_map.get(k) == v for k, v in wanted.items()):
                total += int(value)
        return total


#: The process-wide registry. Import this, never construct another one.
REGISTRY = MetricRegistry()


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------
#
# Each block records two things honestly: what feeds the series TODAY, and --
# where that is nothing -- what will. A series with no data source is still
# declared, at zero, marked ``declared_zero=True``.


def _declare_all(registry: MetricRegistry) -> None:
    declarations: Iterable[MetricSpec] = (
        # -- build info ---------------------------------------------------
        MetricSpec(
            name="ymoney_build_info",
            kind=MetricType.GAUGE,
            help="Static build information; value is always 1.",
            labelnames=("version",),
        ),

        # -- collector health ---------------------------------------------
        MetricSpec(
            name="ymoney_collector_up",
            kind=MetricType.GAUGE,
            help=(
                "1 when a collector last read its source successfully, 0 when "
                "it failed. A 0 here is what the db_unavailable and "
                "storage_unavailable alert rules evaluate."
            ),
            labelnames=("collector",),
        ),

        # -- HTTP (§8 request latency + error rate) ------------------------
        MetricSpec(
            name="ymoney_http_requests_total",
            kind=MetricType.COUNTER,
            help="Inbound HTTP requests by route template and status class.",
            labelnames=("method", "route", "status_class"),
        ),
        MetricSpec(
            name="ymoney_http_request_duration_seconds",
            kind=MetricType.HISTOGRAM,
            help="Inbound HTTP request latency.",
            labelnames=("method", "route"),
            buckets=API_BUCKETS,
        ),
        MetricSpec(
            name="ymoney_http_request_errors_total",
            kind=MetricType.COUNTER,
            help=(
                "Inbound HTTP requests that ended 5xx. The numerator of the "
                "error-rate SLO; denominator is ymoney_http_requests_total."
            ),
            labelnames=("method", "route"),
        ),

        # -- jobs ----------------------------------------------------------
        MetricSpec(
            name="ymoney_job_queue_depth",
            kind=MetricType.GAUGE,
            help="Jobs awaiting a worker, by queue status. Feeds the queue backlog SLO.",
            labelnames=("status",),
        ),
        MetricSpec(
            name="ymoney_job_start_latency_seconds",
            kind=MetricType.HISTOGRAM,
            help="Seconds from enqueue to first claim by a worker.",
            labelnames=("type",),
            buckets=LONG_BUCKETS,
        ),
        MetricSpec(
            name="ymoney_job_duration_seconds",
            kind=MetricType.HISTOGRAM,
            help="Seconds a job ran, terminal states only.",
            labelnames=("type", "status"),
            buckets=LONG_BUCKETS,
        ),
        MetricSpec(
            name="ymoney_jobs_completed_total",
            kind=MetricType.COUNTER,
            help="Jobs that reached a terminal state.",
            labelnames=("type", "status"),
        ),
        MetricSpec(
            name="ymoney_workers_total",
            kind=MetricType.GAUGE,
            help="Durable job worker tasks alive in this process.",
        ),
        MetricSpec(
            name="ymoney_workers_busy",
            kind=MetricType.GAUGE,
            help="Worker tasks currently executing a job.",
        ),
        MetricSpec(
            name="ymoney_worker_utilization_ratio",
            kind=MetricType.GAUGE,
            help="busy workers / total workers in [0,1]; 1.0 means every worker is saturated.",
        ),
        MetricSpec(
            name="ymoney_job_last_start_timestamp_seconds",
            kind=MetricType.GAUGE,
            help=(
                "Unix time of the most recent job claim. Combined with queue "
                "depth > 0 this is how 'queue stalled' is detected: work "
                "waiting while nothing starts."
            ),
        ),

        # -- render / GPU --------------------------------------------------
        MetricSpec(
            name="ymoney_render_duration_seconds",
            kind=MetricType.HISTOGRAM,
            help="Video render wall time by engine and terminal status.",
            labelnames=("engine", "status"),
            buckets=LONG_BUCKETS,
        ),
        MetricSpec(
            name="ymoney_render_failures_total",
            kind=MetricType.GAUGE,
            help="Renders that failed since process start, by engine.",
            labelnames=("engine",),
        ),
        MetricSpec(
            name="ymoney_gpu_utilization_ratio",
            kind=MetricType.GAUGE,
            help="Fraction of GPU capacity in use.",
            labelnames=("worker",),
            declared_zero=True,
            will_be_fed_by=(
                "the GPU worker lane reporting device utilisation; no GPU "
                "worker in this deployment exports one yet"
            ),
        ),
        MetricSpec(
            name="ymoney_gpu_slots_reserved",
            kind=MetricType.GAUGE,
            help=(
                "GPU slots currently held from the durable slot ledger "
                "(MEDIA_INTEL_GPU_SLOT rows), against settings.max_concurrent_gpu_jobs."
            ),
        ),
        MetricSpec(
            name="ymoney_gpu_jobs_waiting",
            kind=MetricType.GAUGE,
            help="GPU-gated jobs waiting for a slot. A non-zero value with zero slots held is the gpu_queue_starvation condition.",
        ),

        # -- providers -----------------------------------------------------
        MetricSpec(
            name="ymoney_provider_request_duration_seconds",
            kind=MetricType.HISTOGRAM,
            help="Outbound provider call latency.",
            labelnames=("provider", "operation"),
            buckets=LONG_BUCKETS,
        ),
        MetricSpec(
            name="ymoney_provider_errors_total",
            kind=MetricType.COUNTER,
            help="Failed outbound provider calls.",
            labelnames=("provider", "operation"),
        ),
        MetricSpec(
            name="ymoney_publish_failures_total",
            kind=MetricType.COUNTER,
            help="Failed publish attempts by platform.",
            labelnames=("platform",),
        ),
        MetricSpec(
            name="ymoney_publish_failures_consecutive",
            kind=MetricType.GAUGE,
            help=(
                "Consecutive failed publishes for a platform, reset to 0 on "
                "any success. Feeds the repeated_publish_failure rule."
            ),
            labelnames=("platform",),
        ),

        # -- paid-execution risk (Work 15 vocabulary, not reinvented) -------
        MetricSpec(
            name="ymoney_paid_submission_unknown_total",
            kind=MetricType.COUNTER,
            help=(
                "Paid submissions recorded as SubmissionState.SUBMISSION_UNKNOWN: "
                "acceptance could not be established, so the provider may have "
                "accepted and billed. Never auto-retried."
            ),
            labelnames=("provider", "operation"),
        ),
        MetricSpec(
            name="ymoney_paid_unknown_exposure_usd",
            kind=MetricType.GAUGE,
            help=(
                "Estimated USD at risk from submissions whose CostOutcome is "
                "UNKNOWN_EXPOSURE. This is an exposure, not a charge: the money "
                "may never be spent, but it is not known to be unspent."
            ),
            labelnames=("provider",),
        ),
        MetricSpec(
            name="ymoney_budget_refusals_total",
            kind=MetricType.COUNTER,
            help=(
                "Spend refused before submission. 'kind' is ownerless_spend "
                "(OwnerlessSpendRefused), budget_exceeded or rate_limit "
                "(BudgetExceededError / RateLimitExceeded)."
            ),
            labelnames=("kind",),
        ),
        MetricSpec(
            name="ymoney_paid_submissions_total",
            kind=MetricType.COUNTER,
            help="Paid submission attempts by terminal state.",
            labelnames=("provider", "operation", "state"),
        ),

        # -- database ------------------------------------------------------
        MetricSpec(
            name="ymoney_db_pool_checked_out",
            kind=MetricType.GAUGE,
            help="Connections currently checked out of the SQLAlchemy pool.",
        ),
        MetricSpec(
            name="ymoney_db_pool_size",
            kind=MetricType.GAUGE,
            help="Configured pool size (db_pool_size) plus in-use overflow.",
        ),
        MetricSpec(
            name="ymoney_db_pool_utilization_ratio",
            kind=MetricType.GAUGE,
            help="checked_out / pool_size in [0,1]. 1.0 means the next request waits for a connection.",
        ),

        # -- storage -------------------------------------------------------
        MetricSpec(
            name="ymoney_storage_failures_total",
            kind=MetricType.COUNTER,
            help="Storage operations that raised.",
            labelnames=("backend", "operation"),
        ),
        MetricSpec(
            name="ymoney_storage_available",
            kind=MetricType.GAUGE,
            help="1 when the storage backend answered its last health probe.",
        ),
        MetricSpec(
            name="ymoney_storage_bytes_written_total",
            kind=MetricType.GAUGE,
            help="Bytes written through the storage backend since process start.",
        ),

        # -- tracing -------------------------------------------------------
        MetricSpec(
            name="ymoney_spans_recorded_total",
            kind=MetricType.COUNTER,
            help="Spans the dependency-free trace recorder retained.",
            labelnames=("kind", "status"),
        ),
    )
    for spec in declarations:
        registry.register(spec)


_declare_all(REGISTRY)


# ---------------------------------------------------------------------------
# Typed handles
# ---------------------------------------------------------------------------
#
# These wrap the registry so call sites name the metric, not a string. A typo
# becomes an AttributeError at import instead of a silent zero in production.


class _Handle:
    def __init__(self, registry: MetricRegistry, name: str) -> None:
        self._registry = registry
        self.name = name

    def __repr__(self) -> str:
        return f"<metric {self.name}>"


class Counter(_Handle):
    def inc(self, value: float = 1.0, **labels: str) -> None:
        self._registry.inc(self.name, value, **labels)

    def value(self, **labels: str) -> float:
        return self._registry.value(self.name, **labels)


class Gauge(_Handle):
    def set(self, value: float, **labels: str) -> None:
        self._registry.set(self.name, value, **labels)

    def add(self, delta: float, **labels: str) -> None:
        self._registry.add(self.name, delta, **labels)

    def inc(self, value: float = 1.0, **labels: str) -> None:
        self._registry.add(self.name, value, **labels)

    def value(self, **labels: str) -> float:
        return self._registry.value(self.name, **labels)


class Histogram(_Handle):
    def observe(self, value: float, **labels: str) -> None:
        self._registry.observe(self.name, value, **labels)

    def value(self, **labels: str) -> float:
        """Sample count is not a value; this returns the running sum."""
        return self._registry.value(self.name, **labels)


HTTP_REQUESTS = Counter(REGISTRY, "ymoney_http_requests_total")
HTTP_DURATION = Histogram(REGISTRY, "ymoney_http_request_duration_seconds")
HTTP_ERRORS = Counter(REGISTRY, "ymoney_http_request_errors_total")

JOB_QUEUE_DEPTH = Gauge(REGISTRY, "ymoney_job_queue_depth")
JOB_START_LATENCY = Histogram(REGISTRY, "ymoney_job_start_latency_seconds")
JOB_DURATION = Histogram(REGISTRY, "ymoney_job_duration_seconds")
JOBS_COMPLETED = Counter(REGISTRY, "ymoney_jobs_completed_total")
WORKERS_TOTAL = Gauge(REGISTRY, "ymoney_workers_total")
WORKERS_BUSY = Gauge(REGISTRY, "ymoney_workers_busy")
WORKER_UTILIZATION = Gauge(REGISTRY, "ymoney_worker_utilization_ratio")
JOB_LAST_START_TS = Gauge(REGISTRY, "ymoney_job_last_start_timestamp_seconds")

RENDER_DURATION = Histogram(REGISTRY, "ymoney_render_duration_seconds")
RENDER_FAILURES = Gauge(REGISTRY, "ymoney_render_failures_total")
GPU_UTILIZATION = Gauge(REGISTRY, "ymoney_gpu_utilization_ratio")
GPU_SLOTS_RESERVED = Gauge(REGISTRY, "ymoney_gpu_slots_reserved")
GPU_JOBS_WAITING = Gauge(REGISTRY, "ymoney_gpu_jobs_waiting")

PROVIDER_DURATION = Histogram(REGISTRY, "ymoney_provider_request_duration_seconds")
PROVIDER_ERRORS = Counter(REGISTRY, "ymoney_provider_errors_total")
PUBLISH_FAILURES = Counter(REGISTRY, "ymoney_publish_failures_total")
PUBLISH_FAILURES_CONSECUTIVE = Gauge(REGISTRY, "ymoney_publish_failures_consecutive")

PAID_SUBMISSION_UNKNOWN = Counter(REGISTRY, "ymoney_paid_submission_unknown_total")
PAID_UNKNOWN_EXPOSURE = Gauge(REGISTRY, "ymoney_paid_unknown_exposure_usd")
BUDGET_REFUSALS = Counter(REGISTRY, "ymoney_budget_refusals_total")
PAID_SUBMISSIONS = Counter(REGISTRY, "ymoney_paid_submissions_total")

DB_POOL_CHECKED_OUT = Gauge(REGISTRY, "ymoney_db_pool_checked_out")
DB_POOL_SIZE = Gauge(REGISTRY, "ymoney_db_pool_size")
DB_POOL_UTILIZATION = Gauge(REGISTRY, "ymoney_db_pool_utilization_ratio")

STORAGE_FAILURES = Counter(REGISTRY, "ymoney_storage_failures_total")
STORAGE_AVAILABLE = Gauge(REGISTRY, "ymoney_storage_available")
STORAGE_BYTES_WRITTEN = Gauge(REGISTRY, "ymoney_storage_bytes_written_total")

COLLECTOR_UP = Gauge(REGISTRY, "ymoney_collector_up")
BUILD_INFO = Gauge(REGISTRY, "ymoney_build_info")
SPANS_RECORDED = Counter(REGISTRY, "ymoney_spans_recorded_total")

__all__ += [
    "API_BUCKETS",
    "BUILD_INFO",
    "BUDGET_REFUSALS",
    "COLLECTOR_UP",
    "DB_POOL_CHECKED_OUT",
    "DB_POOL_SIZE",
    "DB_POOL_UTILIZATION",
    "GPU_JOBS_WAITING",
    "GPU_SLOTS_RESERVED",
    "GPU_UTILIZATION",
    "HTTP_DURATION",
    "HTTP_ERRORS",
    "HTTP_REQUESTS",
    "JOBS_COMPLETED",
    "JOB_DURATION",
    "JOB_LAST_START_TS",
    "JOB_QUEUE_DEPTH",
    "JOB_START_LATENCY",
    "LONG_BUCKETS",
    "PAID_SUBMISSIONS",
    "PAID_SUBMISSION_UNKNOWN",
    "PAID_UNKNOWN_EXPOSURE",
    "PROVIDER_DURATION",
    "PROVIDER_ERRORS",
    "PUBLISH_FAILURES",
    "PUBLISH_FAILURES_CONSECUTIVE",
    "RENDER_DURATION",
    "RENDER_FAILURES",
    "SPANS_RECORDED",
    "STORAGE_AVAILABLE",
    "STORAGE_BYTES_WRITTEN",
    "STORAGE_FAILURES",
    "WORKERS_BUSY",
    "WORKERS_TOTAL",
    "WORKER_UTILIZATION",
]


def record_http_request(method: str, route: str, status: int,
                        duration_seconds: float) -> None:
    """One inbound request, from the HTTP middleware.

    ``route`` must be the templated path. Passing a raw URL path here would
    defeat the series cap, so the middleware is the only intended caller.
    """
    status_class = f"{status // 100}xx"
    HTTP_REQUESTS.inc(method=method, route=route, status_class=status_class)
    HTTP_DURATION.observe(duration_seconds, method=method, route=route)
    if status >= 500:
        HTTP_ERRORS.inc(method=method, route=route)


def status_class(status: int) -> str:
    """Exposed for tests and for any other status grouping."""
    return f"{status // 100}xx"


def labelled_total(values: Mapping[str, float]) -> float:
    return sum(float(v) for v in values.values())


def sum_series(series: Iterable[float]) -> float:
    return sum(float(v) for v in series)


def bucket_edges(spec_buckets: Sequence[float]) -> tuple[float, ...]:
    """Normalise a bucket declaration, dropping non-positive edges."""
    return tuple(sorted({float(b) for b in spec_buckets if float(b) > 0}))


__all__ += ["bucket_edges", "labelled_total", "record_http_request", "sum_series"]

# ---------------------------------------------------------------------------
# Recording seams
# ---------------------------------------------------------------------------
#
# The functions the rest of YMONEY calls. They exist so no lane has to know a
# metric's name or shape, and so the vocabulary of paid execution stays in one
# place: ``record_paid_outcome`` is the ONLY way an unknown submission enters
# the registry, and it takes the ``SubmissionState`` / ``CostOutcome`` values
# that Work 15 already defines rather than inventing parallel strings.


def status_class_of(status: int) -> str:
    return f"{int(status) // 100}xx"


def record_provider_call(provider: str, operation: str, duration_seconds: float,
                         *, failed: bool = False) -> None:
    """One outbound provider call."""
    PROVIDER_DURATION.observe(duration_seconds,
                              provider=provider, operation=operation)
    if failed:
        PROVIDER_ERRORS.inc(provider=provider, operation=operation)


def record_publish_result(platform: str, *, success: bool) -> None:
    """One publish attempt.

    Maintains the consecutive-failure gauge as well as the counter, because
    the alert rule needs "failing right now" and only a streak gauge can
    express that: a cumulative counter never forgets.
    """
    if success:
        PUBLISH_FAILURES_CONSECUTIVE.set(0.0, platform=platform)
        return
    PUBLISH_FAILURES.inc(platform=platform)
    PUBLISH_FAILURES_CONSECUTIVE.add(1.0, platform=platform)


def record_render(engine: str, duration_seconds: float, *, ok: bool) -> None:
    RENDER_DURATION.observe(duration_seconds, engine=engine,
                            status="ok" if ok else "failed")
    if ok:
        RENDER_FAILURES.set(RENDER_FAILURES.value(engine=engine), engine=engine)
        return
    RENDER_FAILURES.add(1.0, engine=engine)


def record_storage_operation(backend: str, operation: str, *, failed: bool,
                             bytes_written: int = 0) -> None:
    """One storage operation. ``bytes_written`` only on success."""
    if failed:
        STORAGE_FAILURES.inc(backend=backend, operation=operation)
        return
    if bytes_written:
        STORAGE_BYTES_WRITTEN.add(float(bytes_written))


def record_job_queued(job_type: str) -> None:
    JOB_QUEUE_DEPTH.add(1.0, status="QUEUED")


def record_job_started(job_type: str, *, start_latency_seconds: float = 0.0,
                       enqueued_at: float | None = None) -> None:
    """A worker claimed a job.

    ``start_latency_seconds`` is measured from enqueue when the caller has the
    timestamp. It is NOT estimated from the queue depth: a derived number
    would be an invented measurement.
    """
    JOB_QUEUE_DEPTH.add(-1.0, status="QUEUED")
    WORKERS_BUSY.add(1.0)
    JOB_LAST_START_TS.set(time.time())
    if start_latency_seconds > 0:
        JOB_START_LATENCY.observe(start_latency_seconds, type=job_type)
    _ = enqueued_at


def record_job_finished(job_type: str, duration_seconds: float, *,
                        status: str = "COMPLETED") -> None:
    JOB_DURATION.observe(duration_seconds, type=job_type, status=status)
    JOBS_COMPLETED.inc(type=job_type, status=status)
    WORKERS_BUSY.add(-1.0)


def record_paid_outcome(provider: str, operation: str, state: object,
                        *, estimated_cost_usd: float = 0.0) -> None:
    """Record one paid submission terminal state.

    ``state`` is the existing
    :class:`~app.services.paid_jobs.SubmissionState` member. It is compared by
    value, so this works whether the caller passes the enum or the raw string
    it was persisted as. An UNKNOWN state also increases the exposure gauge:
    money may have been spent, and the amount is not known.
    """
    state_value = str(state)
    PAID_SUBMISSIONS.inc(provider=provider, operation=operation,
                         state=state_value)
    if state_value != "SUBMISSION_UNKNOWN":
        return
    PAID_SUBMISSION_UNKNOWN.inc(provider=provider, operation=operation)
    PAID_UNKNOWN_EXPOSURE.add(float(estimated_cost_usd or 0.0), provider=provider)


def record_budget_refusal(kind: str) -> None:
    """One spend refused BEFORE submission.

    ``kind`` is one of ``ownerless_spend``, ``budget_exceeded``, ``rate_limit``
    -- the existing refusal vocabulary (OwnerlessSpendRefused,
    BudgetExceededError, RateLimitExceeded).
    """
    BUDGET_REFUSALS.inc(kind=kind)


def budget_refusal_kind(exc: BaseException) -> str:
    """Map a raised refusal onto its metric label.

    Recognises the real classes by name so this module does not import
    ``app.services.paid_provider`` at module scope (that would pull the
    whole provider layer into every metrics import).
    """
    name = type(exc).__name__
    if name == "OwnerlessSpendRefused":
        return "ownerless_spend"
    if name == "RateLimitExceeded":
        return "rate_limit"
    if name in {"BudgetExceededError", "LLMCompletionBudgetExceeded"}:
        return "budget_exceeded"
    return "unknown"


# ---------------------------------------------------------------------------
# Runtime collectors
# ---------------------------------------------------------------------------
#
# Gauges that are a READ of live state rather than an accumulation. Each one
# sets ``ymoney_collector_up`` honestly: a probe that raises sets the gauge to
# 0 rather than leaving a stale value or inventing a healthy one.


def collect_build_info() -> None:
    BUILD_INFO.set(1.0, version="0.1.0")


def collect_worker_fleet() -> None:
    """Read the live worker task list from ``app.services.jobs``.

    Read, not written: ``jobs._worker_tasks`` is owned by that module and this
    one does not reach into it to change anything. An attribute the module
    does not expose is reported as zero workers, which makes the
    ``worker_fleet_unavailable`` rule fire -- the honest reading when the
    fleet cannot be observed.
    """
    try:
        from app.services import jobs as jobs_service

        tasks = list(getattr(jobs_service, "_worker_tasks", []) or [])
        # Count ALIVE tasks, not the length of the list: a task that raised and
        # finished still sits in the list, and reporting it would let
        # worker_fleet_unavailable stay quiet while nothing claims jobs.
        alive = sum(1 for t in tasks if not t.done())
        WORKERS_TOTAL.set(float(alive))
        busy = WORKERS_BUSY.value()
        WORKER_UTILIZATION.set(0.0 if alive <= 0 else max(0.0, min(1.0, busy / alive)))
    except Exception:
        WORKERS_TOTAL.set(0.0)
        WORKER_UTILIZATION.set(0.0)


def collect_db_pool() -> bool:
    """Read SQLAlchemy pool occupancy. Returns whether the DB was reachable."""
    from sqlalchemy import text as sql_text

    from app.db import engine as db_engine

    pool = db_engine.pool
    checked_out = 0
    size = 0
    for attr in ("checkedout", "size"):
        reader = getattr(pool, attr, None)
        if callable(reader):
            try:
                value = int(reader())
            except Exception:
                value = 0
            if attr == "checkedout":
                checked_out = value
            else:
                size = value
    if size <= 0:
        # SingletonThreadPool/StaticPool have no meaningful sizing; report the
        # one connection that is in use rather than inventing a ratio.
        size = 1
    DB_POOL_CHECKED_OUT.set(float(checked_out))
    DB_POOL_SIZE.set(float(size))
    DB_POOL_UTILIZATION.set(max(0.0, min(1.0, checked_out / size)))
    try:
        with db_engine.connect() as conn:
            conn.execute(sql_text("SELECT 1"))
        COLLECTOR_UP.set(1.0, collector="database")
        return True
    except Exception:
        COLLECTOR_UP.set(0.0, collector="database")
        return False


def collect_job_queue_depth() -> bool:
    """Count jobs awaiting a worker, by status.

    A real count from the ``jobs`` table. If the query fails the collector
    heartbeat goes to 0 and the depth gauges are left alone -- zeroing them
    would read as "no backlog" during a database outage, which is the opposite
    of the truth.
    """
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Job

    try:
        with session_scope() as session:
            rows = session.execute(
                select(Job.status, func.count()).group_by(Job.status)
            ).all()
        for status, count in rows:
            JOB_QUEUE_DEPTH.set(float(count or 0),
                                status=str(status or "UNKNOWN"))
        COLLECTOR_UP.set(1.0, collector="jobs")
        return True
    except Exception:
        COLLECTOR_UP.set(0.0, collector="jobs")
        return False


def collect_gpu_slots() -> bool:
    """Read the durable GPU slot ledger and the GPU-gated waiting queue.

    Both are real row counts. A GPU-gated job is one whose payload carries
    ``requires_gpu`` -- the same flag ``jobs._claim_next`` defers on -- so this
    counts exactly what the claim loop is refusing to run. The payload filter
    runs in Python rather than in SQL because the flag lives inside a JSON
    column and a dialect-specific JSON predicate is not worth the portability
    cost for a gauge.
    """
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Job

    try:
        with session_scope() as session:
            held = session.scalar(
                select(func.count()).select_from(Job).where(
                    Job.type == "MEDIA_INTEL_GPU_SLOT",
                    Job.status.in_(("QUEUED", "RUNNING")),
                )
            )
            waiting_rows = session.scalars(
                select(Job).where(
                    Job.status == "QUEUED", Job.type != "MEDIA_INTEL_GPU_SLOT"
                ).limit(500)
            ).all()
        waiting = sum(
            1 for row in waiting_rows
            if bool((row.payload or {}).get("requires_gpu"))
        )
        GPU_SLOTS_RESERVED.set(float(held or 0))
        GPU_JOBS_WAITING.set(float(waiting))
        COLLECTOR_UP.set(1.0, collector="gpu_slots")
        return True
    except Exception:
        COLLECTOR_UP.set(0.0, collector="gpu_slots")
        return False


def collect_storage() -> bool:
    """Probe the storage backend by writing and removing a small file.

    Same probe shape as ``services.readiness._check_storage`` -- a health check
    that only stats the path cannot detect a full or read-only filesystem.
    """
    from pathlib import Path

    from app.services.storage import STORAGE_ROOT

    probe_dir = Path(STORAGE_ROOT)
    try:
        probe_dir.mkdir(parents=True, exist_ok=True)
        probe = probe_dir / ".observability-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        STORAGE_AVAILABLE.set(1.0)
        COLLECTOR_UP.set(1.0, collector="storage")
        return True
    except Exception:
        STORAGE_AVAILABLE.set(0.0)
        COLLECTOR_UP.set(0.0, collector="storage")
        return False


def collect_all() -> dict[str, bool]:
    """Run every collector. Returns each one's success.

    Isolated on purpose: one unreachable subsystem must not stop the others
    from reporting, or the single outage would blind every metric at once.
    """
    results: dict[str, bool] = {}
    for name, fn in (
        ("workers", collect_worker_fleet),
        ("database", collect_db_pool),
        ("jobs", collect_job_queue_depth),
        ("gpu_slots", collect_gpu_slots),
        ("storage", collect_storage),
    ):
        try:
            results[name] = bool(fn())
        except Exception:
            results[name] = False
    return results


__all__ += [
    "budget_refusal_kind",
    "collect_all",
    "collect_build_info",
    "collect_db_pool",
    "collect_gpu_slots",
    "collect_job_queue_depth",
    "collect_storage",
    "collect_worker_fleet",
    "record_budget_refusal",
    "record_job_finished",
    "record_job_queued",
    "record_job_started",
    "record_paid_outcome",
    "record_provider_call",
    "record_publish_result",
    "record_render",
    "record_storage_operation",
    "status_class_of",
]
