"""SLO targets and critical alert rules -- definitions, not claims.

**What this module does NOT do.** It does not report an achieved SLO. There is
no "current availability 99.95%" in here, because computing one requires
sampling windows, a scrape history and a decision about which series counts as
an error -- none of which exist yet in a process-local registry that resets on
restart. An SLO section that printed a percentage derived from five minutes of
uptime would be a fabricated number wearing a compliance costume.

What it does do:

  * declare objectives as **targets**, with the metric each one will be
    computed from, so the measurement is agreed before the first alert fires;
  * evaluate **alert rules** -- real functions over a
    :class:`~app.services.observability.metrics.Snapshot` -- so each rule can
    be tested by feeding it numbers, with a firing and a non-firing case.

**Severity is a property of the consequence, not the metric.** A lost payment
is CRITICAL. A slow queue is a WARNING until work actually starts failing,
because a queue that is slow and empty is not an incident.

Where the numbers come from
---------------------------
This module used to hard-code every threshold as a literal (``total > 1.0``,
``idle > 900.0``, ``worst >= 3.0``) while ``core/config.py`` advertised
``alert_*`` and ``slo_*`` settings that nothing read. That is the defect this
module now fixes, and the fix is one source of truth in each direction:

**Settings -> validated -> evaluator -> alert verdict.** Exactly three numbers
are COMPARED against a live metric, and all three come from ``Settings``:

    ALERT_QUEUE_STALL_SECONDS      -> queue_stalled
    ALERT_UNKNOWN_EXPOSURE_USD     -> unknown_exposure_high
    ALERT_PUBLISH_FAILURE_STREAK   -> repeated_publish_failure

They are resolved on every evaluation (:meth:`AlertThresholds.read`), never
captured at import, so retuning an alert does not require a restart and a test
can prove a changed value changes a verdict. The bounds live in ``Settings`` as
pydantic constraints, so a nonsense value raises at startup instead of
coercing into a threshold nobody chose.

Everything else in a rule stays a CONSTANT, on purpose, because making it a
setting would be configurability without behaviour:

    paid_submission_unknown      total > 0     the objective is exactly ZERO
    worker_fleet_unavailable     alive <= 0    zero durable workers is never OK
    db_unavailable               up < 1        a heartbeat is a boolean
    storage_unavailable          avail < 1     a health probe is a boolean
    gpu_queue_starvation         waiting>0 AND slots<=0   a ledger identity

The first is the sharpest example: a configurable "how many unknown paid
submissions are acceptable" would let an operator set it to five, and five
billable submissions whose acceptance nobody established is five incidents, not
a threshold.

The ``slo_*`` settings are the third thing, and they are weaker than the
``alert_*`` ones on purpose: nothing is measured, so a target cannot be
evaluated. What a target setting changes is the number the PUBLISHED objective
states (:meth:`SLOTarget.resolved_target`), which is the only honest behaviour
available. ``slo_catalog()`` says ``measured: false`` and this module does not
pretend otherwise.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.core.config import settings
from app.services.observability.metrics import REGISTRY, Snapshot

__all__ = [
    "AlertRule",
    "AlertThresholds",
    "AlertVerdict",
    "Severity",
    "SLO_TARGETS",
    "SLOTarget",
    "alert_rules",
    "alert_thresholds",
    "evaluate_all",
    "firing_alerts",
    "rule_by_id",
    "slo_catalog",
]


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"


class Comparator(StrEnum):
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"


def _compare(observed: float, threshold: float, comparator: Comparator) -> bool:
    """Apply one comparison. Kept tiny and total so every rule shares it."""
    if comparator is Comparator.GT:
        return observed > threshold
    if comparator is Comparator.GTE:
        return observed >= threshold
    if comparator is Comparator.LT:
        return observed < threshold
    return observed <= threshold


# ---------------------------------------------------------------------------
# The numbers an alert is COMPARED against
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlertThresholds:
    """The three compared thresholds, resolved from ``Settings``.

    Read through :meth:`read` on every evaluation rather than captured at
    import. Capturing would make the setting a constant with extra steps, which
    is precisely the fake configurability this exists to remove.

    Attributes are accessed directly (``settings.alert_...``), not through
    ``getattr`` with a fallback default: if a field is renamed or removed the
    first evaluation raises :class:`AttributeError` instead of quietly reverting
    to a baked-in literal nobody can find.
    """

    queue_stall_seconds: float
    unknown_exposure_usd: float
    publish_failure_streak: float

    @classmethod
    def read(cls) -> AlertThresholds:
        return cls(
            queue_stall_seconds=float(settings.alert_queue_stall_seconds),
            unknown_exposure_usd=float(settings.alert_unknown_exposure_usd),
            publish_failure_streak=float(settings.alert_publish_failure_streak),
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "queue_stall_seconds": self.queue_stall_seconds,
            "unknown_exposure_usd": self.unknown_exposure_usd,
            "publish_failure_streak": self.publish_failure_streak,
        }


def alert_thresholds() -> AlertThresholds:
    """The live thresholds. Same object an evaluation just used."""
    return AlertThresholds.read()


# ---------------------------------------------------------------------------
# SLO objectives -- TARGETS ONLY
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SLOTarget:
    """One objective, declared but not yet measured.

    The objective's NUMBER comes from exactly one place, and this class makes
    which one impossible to miss:

    * ``target`` -- the number is part of the wording. The objective is not a
      tunable quantity (unknown paid submissions is *zero*; data durability is
      *100% of probes succeed*), and a knob for wording would be a knob with
      no behaviour behind it.
    * ``setting`` + ``scale`` + ``target_template`` -- the number is a
      configured quantity, read from ``Settings`` when the catalogue is
      serialised. ``target`` must then be empty.

    Both or neither is a :class:`ValueError` at import, so a half-configured
    objective cannot ship.
    """

    id: str
    title: str
    objective: str
    #: The metric(s) the objective will be computed from, as Prometheus-style
    #: names. Stated so the definition is reviewable before the data exists.
    source_metrics: tuple[str, ...]
    window: str
    #: Literal objective text, when the number is not configurable.
    target: str = ""
    #: Explicitly false until a real measurement exists.
    measured: bool = False
    notes: str = ""
    #: ``Settings`` field holding this objective's number, or "".
    setting: str = ""
    #: Multiplier applied before formatting (100 to render 0.995 as 99.5).
    scale: float = 1.0
    #: Format string with a single ``{}`` placeholder for the configured number.
    target_template: str = ""

    def __post_init__(self) -> None:
        if bool(self.target) == bool(self.target_template):
            raise ValueError(
                f"SLO {self.id!r} must state its number exactly once: give "
                "either a literal `target` or a `setting` + `target_template`")
        if self.target_template and not self.setting:
            raise ValueError(
                f"SLO {self.id!r} has a `target_template` but no `setting`; "
                "there is nothing to read the number from")

    def configured_value(self) -> float | int | None:
        """The live number, or ``None`` when the wording carries it.

        The raw value is returned unscaled and uncast when no scale is
        declared, so an integer objective renders as ``25`` and not ``25.0``.
        """
        if not self.setting:
            return None
        raw = getattr(settings, self.setting)
        if float(self.scale) == 1.0:
            return raw
        return float(raw) * float(self.scale)

    def resolved_target(self) -> str:
        """The objective text as it is published right now."""
        if not self.setting:
            return self.target
        value = self.configured_value()
        return self.target_template.format(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "objective": self.objective,
            "source_metrics": list(self.source_metrics),
            "target": self.resolved_target(),
            "window": self.window,
            "measured": self.measured,
            "notes": self.notes,
            "target_setting": self.setting or None,
            "configured_value": self.configured_value(),
        }


SLO_TARGETS: tuple[SLOTarget, ...] = (
    SLOTarget(
        id="api_availability",
        title="API availability",
        objective="Non-5xx share of inbound API requests at or above the target.",
        source_metrics=("ymoney_http_requests_total", "ymoney_http_request_errors_total"),
        setting="slo_api_availability_target",
        scale=100.0,
        target_template="{:.4g}% of non-5xx over 30d",
        window="30d rolling",
        notes=(
            "Excludes /livez and /readyz: a probe that fails because the thing "
            "it probes is down must not itself be counted as an error twice."
        ),
    ),
    SLOTarget(
        id="api_latency_p95",
        title="API request latency",
        objective="p95 inbound API latency at or below the target.",
        source_metrics=("ymoney_http_request_duration_seconds",),
        target="p95 <= 1.0s over 30d",
        window="30d rolling",
        notes=(
            "Deliberately not a setting. There is no measured p95 to retune "
            "and no alert compares against this number, so a knob here would "
            "change the wording of a declaration and nothing else."
        ),
    ),
    SLOTarget(
        id="job_start_latency",
        title="Job start latency",
        objective="Jobs begin executing within the target of enqueue.",
        source_metrics=("ymoney_job_start_latency_seconds",),
        setting="slo_job_start_latency_seconds",
        target_template="95% of jobs start within {:g}s",
        window="7d rolling",
    ),
    SLOTarget(
        id="queue_backlog",
        title="Job queue backlog",
        objective="Sustained queue depth at or below the target.",
        source_metrics=("ymoney_job_queue_depth",),
        setting="slo_queue_backlog_max",
        target_template="<= {} queued jobs sustained",
        window="1h sustained",
    ),
    SLOTarget(
        id="publish_failure_rate",
        title="Publish failure rate",
        objective="Publish attempts failing at or below the target.",
        source_metrics=("ymoney_publish_failures_total", "ymoney_paid_submissions_total"),
        setting="slo_publish_failure_rate_max",
        scale=100.0,
        target_template="<= {:g}% of publish attempts fail",
        window="24h rolling",
        notes=(
            "The objective is a rate over a window; the ALERT on this metric is "
            "a consecutive-failure streak (ALERT_PUBLISH_FAILURE_STREAK). They "
            "are different questions and are tuned separately."
        ),
    ),
    SLOTarget(
        id="render_failure_rate",
        title="Render failure rate",
        objective="Renders failing at or below the target.",
        source_metrics=("ymoney_render_failures_total", "ymoney_render_duration_seconds"),
        setting="slo_render_failure_rate_max",
        scale=100.0,
        target_template="<= {:g}% of renders fail",
        window="24h rolling",
    ),
    SLOTarget(
        id="unknown_paid_submissions",
        title="Unknown paid submissions",
        objective=(
            "Billable submissions whose acceptance could not be established "
            "(SubmissionState.SUBMISSION_UNKNOWN) stay at zero."
        ),
        source_metrics=("ymoney_paid_submission_unknown_total",),
        target="0",
        window="cumulative since process start",
        notes=(
            "Target is exactly zero because every occurrence is possible money "
            "spent against an unknown outcome. One is an incident. Deliberately "
            "not a setting: a configurable tolerance here would let an operator "
            "declare five acceptable unconfirmed purchases, which is not a "
            "target, it is a policy nobody agreed to."
        ),
    ),
    SLOTarget(
        id="unknown_exposure_bounded",
        title="Unknown monetary exposure",
        objective="Estimated USD at UNKNOWN_EXPOSURE stays bounded.",
        source_metrics=("ymoney_paid_unknown_exposure_usd",),
        setting="slo_unknown_exposure_max_usd",
        target_template="<= ${:.2f} at any instant",
        window="continuous",
        notes=(
            "Exposure, not charge: the money may never be spent. The ALERT on "
            "this metric fires far earlier, at ALERT_UNKNOWN_EXPOSURE_USD -- "
            "the objective bounds it, the alert wakes somebody up."
        ),
    ),
    SLOTarget(
        id="data_durability",
        title="Data durability",
        objective="No acknowledged write is lost.",
        source_metrics=("ymoney_collector_up",),
        target="100% of DB collector probes succeed",
        window="continuous",
        notes=(
            "Deliberately not a setting. 100% is the definition of durability; "
            "any lower number would be a decision to tolerate losing "
            "acknowledged writes."
        ),
    ),
)


def slo_catalog() -> dict[str, Any]:
    """The SLO section of the ops API.

    Carries an explicit disclaimer: these are targets, and nothing here has
    been measured. A reader must not be able to mistake this payload for a
    compliance report.
    """
    return {
        "measured": False,
        "disclaimer": (
            "Targets only. No achieved SLO is reported: this registry is "
            "in-process and resets on restart, so it has no history to compute "
            "a compliance window from. Compute achieved values from "
            "retained scrape history, not from this endpoint."
        ),
        "thresholds": alert_thresholds().as_dict(),
        "targets": [t.to_dict() for t in SLO_TARGETS],
    }


# ---------------------------------------------------------------------------
# Alert rules -- evaluators over a Snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlertVerdict:
    """The result of evaluating one rule."""

    rule_id: str
    firing: bool
    observed: float
    threshold: float
    unit: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "firing": self.firing,
            "observed": round(float(self.observed), 6),
            "threshold": float(self.threshold),
            "unit": self.unit,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class AlertRule:
    """A named, testable predicate over the metric snapshot.

    ``setting`` names the ``Settings`` field this rule compares against, and is
    empty for the rules whose threshold is a deliberate CONSTANT (see the module
    docstring). Exactly one of ``setting`` / ``constant_threshold`` decides the
    number, and :meth:`current_threshold` is the only way to read it, so
    ``to_dict()`` cannot publish a stale literal next to a live verdict.
    """

    id: str
    severity: Severity
    title: str
    what_it_detects: str
    unit: str
    evaluate: Callable[[Snapshot, float], AlertVerdict]
    #: Operator action. Never "increase the threshold".
    runbook: str
    #: ``Settings`` field name, when the threshold is configurable.
    setting: str = ""
    #: The literal threshold, when it is not configurable. Zero and one are the
    #: only values used, and each is an identity ("any", "none") rather than a
    #: policy -- see the module docstring.
    constant_threshold: float = 0.0

    def current_threshold(self) -> float:
        """The number this rule compares against, right now."""
        if self.setting:
            return float(getattr(settings, self.setting))
        return float(self.constant_threshold)

    def threshold_source(self) -> str:
        return f"settings:{self.setting.upper()}" if self.setting else "constant"

    def check(self, snapshot: Snapshot, now: float | None = None) -> AlertVerdict:
        """Evaluate now (or at an explicit time, for deterministic tests)."""
        return self.evaluate(snapshot, time.time() if now is None else now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": str(self.severity),
            "title": self.title,
            "detects": self.what_it_detects,
            "threshold": self.current_threshold(),
            "threshold_source": self.threshold_source(),
            "unit": self.unit,
            "runbook": self.runbook,
        }


def _v(rule_id: str, firing: bool, observed: float, threshold: float,
       unit: str, reason: str, **detail: Any) -> AlertVerdict:
    return AlertVerdict(rule_id=rule_id, firing=firing, observed=observed,
                        threshold=threshold, unit=unit, reason=reason,
                        detail=detail)


# -- rule evaluators ------------------------------------------------------


def _rule_submission_unknown(snapshot: Snapshot, _now: float) -> AlertVerdict:
    """ANY SUBMISSION_UNKNOWN is critical. The target is zero, not low.

    The threshold is a CONSTANT and not a setting. One is an incident, so a
    configurable tolerance here would be a setting that decides how much money
    may be spent with an unknown outcome.
    """
    total = snapshot.get("ymoney_paid_submission_unknown_total")
    return _v(
        "paid_submission_unknown",
        firing=total > 0,
        observed=total, threshold=0.0, unit="count",
        reason=(
            "a billable submission is in SubmissionState.SUBMISSION_UNKNOWN: "
            "the provider may have accepted and billed it. Do not resubmit."
        ) if total > 0 else "no unknown paid submissions recorded",
        unknown_exposure_usd=snapshot.get("ymoney_paid_unknown_exposure_usd"),
    )


def _rule_unknown_exposure(snapshot: Snapshot, _now: float) -> AlertVerdict:
    """Configured: ``ALERT_UNKNOWN_EXPOSURE_USD`` (default $1.00)."""
    limit = AlertThresholds.read().unknown_exposure_usd
    total = snapshot.get("ymoney_paid_unknown_exposure_usd")
    firing = total > limit
    return _v(
        "unknown_exposure_high",
        firing=firing,
        observed=total, threshold=limit, unit="usd",
        reason=("estimated USD at UNKNOWN_EXPOSURE exceeds the review threshold"
                if firing else "unknown exposure within threshold"),
        unknown_submissions=snapshot.get("ymoney_paid_submission_unknown_total"),
    )


def _rule_queue_stalled(snapshot: Snapshot, now: float) -> AlertVerdict:
    """Work is waiting AND nothing has started recently.

    Both conditions matter. A deep queue with a recent start is healthy
    backlog; a quiet queue is healthy idleness. Only waiting work plus silence
    is a stall. The silence window is ``ALERT_QUEUE_STALL_SECONDS``.
    """
    limit = AlertThresholds.read().queue_stall_seconds
    depth = snapshot.get("ymoney_job_queue_depth", status="QUEUED")
    last_start = snapshot.get("ymoney_job_last_start_timestamp_seconds")
    idle = (now - last_start) if last_start > 0 else float("inf")
    firing = depth > 0 and idle > limit
    # The prose follows the configured window. A reason string that still said
    # "15 minutes" after the operator retuned the rule would be a second,
    # quieter lie.
    silence = (f"over the configured {limit:g}s window" if idle == float("inf")
               else f"{idle:.0f}s")
    return _v(
        "queue_stalled",
        firing=firing,
        observed=idle if idle != float("inf") else -1.0,
        threshold=limit, unit="seconds_since_last_start",
        reason=(
            f"{int(depth)} job(s) queued but nothing has started for {silence}"
            if firing else
            f"queue depth {int(depth)}, last start {idle:.0f}s ago"
        ),
        queue_depth=depth, last_start_timestamp=last_start,
    )


def _rule_worker_fleet(snapshot: Snapshot, _now: float) -> AlertVerdict:
    """Threshold is a CONSTANT: zero durable workers is never a healthy state.

    There is no deployment in which "no worker will ever claim a job" is the
    agreed policy, so a configurable floor would only ever be set to zero.
    """
    total = snapshot.get("ymoney_workers_total")
    return _v(
        "worker_fleet_unavailable",
        firing=total <= 0,
        observed=total, threshold=0.0, unit="workers",
        reason=("no durable job worker is alive; jobs will queue forever"
                if total <= 0 else f"{int(total)} worker(s) alive"),
        busy=snapshot.get("ymoney_workers_busy"),
        utilization=snapshot.get("ymoney_worker_utilization_ratio"),
    )


def _rule_db_unavailable(snapshot: Snapshot, _now: float) -> AlertVerdict:
    """Reads the collector heartbeat, NOT a hardcoded constant.

    A probe failure sets ``ymoney_collector_up{collector="database"}`` to 0;
    an absent series reads as 0 through ``Snapshot.get`` so a collector that
    never ran at all is treated as down rather than healthy. The ``< 1.0``
    threshold is a CONSTANT because a heartbeat is a boolean: a probe that is
    0.5 healthy does not exist.
    """
    up = snapshot.get("ymoney_collector_up", collector="database")
    return _v(
        "db_unavailable",
        firing=up < 1.0,
        observed=up, threshold=1.0, unit="bool",
        reason=("the database probe failed on its last scrape"
                if up < 1.0 else "database probe succeeded"),
        pool_utilization=snapshot.get("ymoney_db_pool_utilization_ratio"),
    )


def _rule_storage_unavailable(snapshot: Snapshot, _now: float) -> AlertVerdict:
    """Boolean probe, so the threshold is a CONSTANT. See ``db_unavailable``."""
    available = snapshot.get("ymoney_storage_available")
    failures = snapshot.get("ymoney_storage_failures_total")
    firing = available < 1.0
    return _v(
        "storage_unavailable",
        firing=firing,
        observed=available, threshold=1.0, unit="bool",
        reason=("the storage backend failed its last health probe"
                if firing else "storage probe succeeded"),
        failures_total=failures,
    )


def _rule_repeated_publish_failure(snapshot: Snapshot, _now: float) -> AlertVerdict:
    """Consecutive, not cumulative. Streak length is ``ALERT_PUBLISH_FAILURE_STREAK``.

    A cumulative counter fires forever once a platform has ever failed once.
    Consecutive-failure count, reset by any success, is what distinguishes an
    outage from history.
    """
    limit = float(AlertThresholds.read().publish_failure_streak)
    worst = 0.0
    worst_platform = ""
    series = snapshot.values.get("ymoney_publish_failures_consecutive") or {}
    for key, value in series.items():
        if float(value) > worst:
            worst = float(value)
            worst_platform = dict(key).get("platform", "")
    firing = worst >= limit
    return _v(
        "repeated_publish_failure",
        firing=firing,
        observed=worst, threshold=limit, unit="consecutive_failures",
        reason=(f"platform {worst_platform!r} has failed {int(worst)} publishes "
                "in a row" if firing else "no platform is in a failure streak"),
        platform=worst_platform,
        total_failures=snapshot.get("ymoney_publish_failures_total"),
    )


def _rule_gpu_starvation(snapshot: Snapshot, now: float) -> AlertVerdict:
    """GPU work is waiting and no slot has been granted.

    Unlike queue_stalled this has no idle timer: the ledger's slot rows carry
    their own timestamps, but a starvation condition is already actionable the
    moment jobs wait with zero slots held -- a waiting job and a free slot is a
    scheduler or worker-lane fault, not slow work.

    Both halves are ledger IDENTITIES (waiting > 0, slots <= 0), not tunable
    magnitudes, so both stay constants. What is configurable is not whether the
    condition is alarming but whether a GPU worker exists at all
    (``GPU_WORKER``), and that is read by ``worker_pool.parse_pools``.

    ``ymoney_gpu_slots_reserved`` is fed by ``collect_gpu_slots()``, which
    counts the WORKSPACE slot ledger (``jobs.type='MEDIA_INTEL_GPU_SLOT'``) and
    not the device reservations -- see
    ``PRODUCTION_ARCHITECTURE.md`` 4.4 for what a device-only caller therefore
    hides from this rule.
    """
    waiting = snapshot.get("ymoney_gpu_jobs_waiting")
    slots = snapshot.get("ymoney_gpu_slots_reserved")
    firing = waiting > 0 and slots <= 0
    return _v(
        "gpu_queue_starvation",
        firing=firing,
        observed=waiting, threshold=0.0, unit="waiting_jobs",
        reason=(f"{int(waiting)} GPU-gated job(s) waiting while {int(slots)} "
                "slot(s) are held: jobs are queued but nothing can claim them"
                if firing else "GPU-gated jobs are being served"),
        slots_reserved=slots,
        gpu_utilization=snapshot.get("ymoney_gpu_utilization_ratio"),
    )


ALERT_RULES: tuple[AlertRule, ...] = (
    AlertRule(
        id="paid_submission_unknown",
        severity=Severity.CRITICAL,
        title="Paid submission in SUBMISSION_UNKNOWN",
        what_it_detects=(
            "A billable provider call whose acceptance could not be "
            "established. Possible money spent, outcome unknown, automatic "
            "resubmission forbidden."
        ),
        constant_threshold=0.0, unit="count",
        evaluate=_rule_submission_unknown,
        runbook=(
            "GET /api/v1/workspaces/{id}/provider-maturity/incidents, or "
            "`python -m app.services.paid_reconciliation list --workspace-id "
            "{id}`; then `reconcile` with the outcome the provider dashboard "
            "shows. Do NOT retry -- Reconciliation.RECONCILE or "
            "MANUAL_OVERRIDE, both audited."
        ),
    ),
    AlertRule(
        id="unknown_exposure_high",
        severity=Severity.CRITICAL,
        title="Unknown monetary exposure above threshold",
        what_it_detects="Estimated USD at CostOutcome.UNKNOWN_EXPOSURE is above the review threshold.",
        setting="alert_unknown_exposure_usd", unit="usd",
        evaluate=_rule_unknown_exposure,
        runbook="Reconcile unknown submissions; check the provider dashboard for a charge with no matching record.",
    ),
    AlertRule(
        id="queue_stalled",
        severity=Severity.CRITICAL,
        title="Job queue stalled",
        what_it_detects="Jobs are queued and no job has started for the stall window.",
        setting="alert_queue_stall_seconds", unit="seconds_since_last_start",
        evaluate=_rule_queue_stalled,
        runbook="Check ymoney_workers_total and the DB collector heartbeat; a full connection pool blocks the claim query.",
    ),
    AlertRule(
        id="worker_fleet_unavailable",
        severity=Severity.CRITICAL,
        title="Worker fleet unavailable",
        what_it_detects="No durable job worker task is alive in this process.",
        constant_threshold=0.0, unit="workers",
        evaluate=_rule_worker_fleet,
        runbook="Check the lifespan startup log; start_workers() did not complete or every worker task died.",
    ),
    AlertRule(
        id="db_unavailable",
        severity=Severity.CRITICAL,
        title="Database unavailable",
        what_it_detects="The database collector heartbeat is not up.",
        constant_threshold=1.0, unit="bool",
        evaluate=_rule_db_unavailable,
        runbook="Check DATABASE_URL, disk space, and Postgres max_connections against db_pool_size + db_max_overflow.",
    ),
    AlertRule(
        id="storage_unavailable",
        severity=Severity.CRITICAL,
        title="Storage unavailable",
        what_it_detects="The storage backend failed its last health probe.",
        constant_threshold=1.0, unit="bool",
        evaluate=_rule_storage_unavailable,
        runbook="Check STORAGE_ROOT writability, or the S3 endpoint/credentials. Renders cannot complete without it.",
    ),
    AlertRule(
        id="repeated_publish_failure",
        severity=Severity.WARNING,
        title="Repeated publish failure",
        what_it_detects="A platform has failed consecutive publishes with no success in between.",
        setting="alert_publish_failure_streak", unit="consecutive_failures",
        evaluate=_rule_repeated_publish_failure,
        runbook="Check the connected account for that platform: an expired token looks exactly like this.",
    ),
    AlertRule(
        id="gpu_queue_starvation",
        severity=Severity.WARNING,
        title="GPU queue starvation",
        what_it_detects="GPU-gated jobs are waiting while the durable slot ledger reports no slot held.",
        constant_threshold=0.0, unit="waiting_jobs",
        evaluate=_rule_gpu_starvation,
        runbook=(
            "Start a GPU worker (GPU_WORKER=true) so worker_pool.parse_pools "
            "allocates a GPU slot; then free what is held -- gpu_reservations "
            "rows with a lapsed lease are returned by "
            "gpu_scheduler.reclaim_stale(), and the workspace slot ledger is "
            "jobs rows with type='MEDIA_INTEL_GPU_SLOT'. Work admitted through "
            "only ONE of the two guards is invisible to the gauge this rule "
            "reads; see PRODUCTION_ARCHITECTURE.md 4.4."
        ),
    ),
)


def alert_rules() -> tuple[AlertRule, ...]:
    return ALERT_RULES


def rule_by_id(rule_id: str) -> AlertRule | None:
    for rule in ALERT_RULES:
        if rule.id == rule_id:
            return rule
    return None


def evaluate_all(snapshot: Snapshot | None = None,
                 now: float | None = None) -> list[AlertVerdict]:
    """Evaluate every rule against one snapshot, at one instant."""
    snap = snapshot if snapshot is not None else REGISTRY.snapshot()
    moment = time.time() if now is None else now
    return [rule.check(snap, moment) for rule in ALERT_RULES]


def firing_alerts(snapshot: Snapshot | None = None,
                  now: float | None = None) -> list[AlertVerdict]:
    return [v for v in evaluate_all(snapshot, now) if v.firing]


def alert_catalog() -> dict[str, Any]:
    """The rule definitions with each rule's current verdict attached.

    Definitions and verdicts are separate: an operator reading this learns both
    what would page them and whether it is paging now.
    """
    snap = REGISTRY.snapshot()
    verdicts = evaluate_all(snap)
    by_id = {v.rule_id: v for v in verdicts}
    rules_out: list[Mapping[str, Any]] = []
    for rule in ALERT_RULES:
        verdict = by_id.get(rule.id)
        entry = dict(rule.to_dict())
        entry["firing"] = bool(verdict.firing) if verdict else False
        entry["observed"] = (round(verdict.observed, 6)
                             if verdict else None)
        if verdict and verdict.firing:
            entry["reason"] = verdict.reason
        rules_out.append(entry)
    return {
        "evaluated_at_epoch": time.time(),
        "note": (
            "Verdicts reflect this process's in-memory counters since start. "
            "A rule with no data yet evaluates against zero, which is why an "
            "unpopulated metric is declared at zero rather than omitted."
        ),
        "thresholds": alert_thresholds().as_dict(),
        "firing_count": sum(1 for v in verdicts if v.firing),
        "rules": rules_out,
        "verdicts": [v.to_dict() for v in verdicts],
    }