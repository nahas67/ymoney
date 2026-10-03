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
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.services.observability.metrics import REGISTRY, Snapshot

__all__ = [
    "AlertRule",
    "AlertVerdict",
    "Severity",
    "SLO_TARGETS",
    "SLOTarget",
    "alert_rules",
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
# SLO objectives -- TARGETS ONLY
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SLOTarget:
    """One objective, declared but not yet measured."""

    id: str
    title: str
    objective: str
    #: The metric(s) the objective will be computed from, as Prometheus-style
    #: names. Stated so the definition is reviewable before the data exists.
    source_metrics: tuple[str, ...]
    target: str
    window: str
    #: Explicitly false until a real measurement exists.
    measured: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "objective": self.objective,
            "source_metrics": list(self.source_metrics),
            "target": self.target,
            "window": self.window,
            "measured": self.measured,
            "notes": self.notes,
        }


SLO_TARGETS: tuple[SLOTarget, ...] = (
    SLOTarget(
        id="api_availability",
        title="API availability",
        objective="Non-5xx share of inbound API requests at or above the target.",
        source_metrics=("ymoney_http_requests_total", "ymoney_http_request_errors_total"),
        target="99.5% of non-5xx over 30d",
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
    ),
    SLOTarget(
        id="job_start_latency",
        title="Job start latency",
        objective="Jobs begin executing within the target of enqueue.",
        source_metrics=("ymoney_job_start_latency_seconds",),
        target="95% of jobs start within 60s",
        window="7d rolling",
    ),
    SLOTarget(
        id="queue_backlog",
        title="Job queue backlog",
        objective="Sustained queue depth at or below the target.",
        source_metrics=("ymoney_job_queue_depth",),
        target="<= 25 queued jobs sustained",
        window="1h sustained",
    ),
    SLOTarget(
        id="publish_failure_rate",
        title="Publish failure rate",
        objective="Publish attempts failing at or below the target.",
        source_metrics=("ymoney_publish_failures_total", "ymoney_paid_submissions_total"),
        target="<= 2% of publish attempts fail",
        window="24h rolling",
    ),
    SLOTarget(
        id="render_failure_rate",
        title="Render failure rate",
        objective="Renders failing at or below the target.",
        source_metrics=("ymoney_render_failures_total", "ymoney_render_duration_seconds"),
        target="<= 5% of renders fail",
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
            "spent against an unknown outcome. One is an incident."
        ),
    ),
    SLOTarget(
        id="unknown_exposure_bounded",
        title="Unknown monetary exposure",
        objective="Estimated USD at UNKNOWN_EXPOSURE stays bounded.",
        source_metrics=("ymoney_paid_unknown_exposure_usd",),
        target="<= $5.00 at any instant",
        window="continuous",
        notes="Exposure, not charge: the money may never be spent.",
    ),
    SLOTarget(
        id="data_durability",
        title="Data durability",
        objective="No acknowledged write is lost.",
        source_metrics=("ymoney_collector_up",),
        target="100% of DB collector probes succeed",
        window="continuous",
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
    """A named, testable predicate over the metric snapshot."""

    id: str
    severity: Severity
    title: str
    what_it_detects: str
    threshold: float
    unit: str
    evaluate: Callable[[Snapshot, float], AlertVerdict]
    #: Operator action. Never "increase the threshold".
    runbook: str

    def check(self, snapshot: Snapshot, now: float | None = None) -> AlertVerdict:
        """Evaluate now (or at an explicit time, for deterministic tests)."""
        return self.evaluate(snapshot, time.time() if now is None else now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": str(self.severity),
            "title": self.title,
            "detects": self.what_it_detects,
            "threshold": float(self.threshold),
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
    """ANY SUBMISSION_UNKNOWN is critical. The target is zero, not low."""
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
    total = snapshot.get("ymoney_paid_unknown_exposure_usd")
    return _v(
        "unknown_exposure_high",
        firing=total > 1.0,
        observed=total, threshold=1.0, unit="usd",
        reason=("estimated USD at UNKNOWN_EXPOSURE exceeds the review threshold"
                if total > 1.0 else "unknown exposure within threshold"),
        unknown_submissions=snapshot.get("ymoney_paid_submission_unknown_total"),
    )


def _rule_queue_stalled(snapshot: Snapshot, now: float) -> AlertVerdict:
    """Work is waiting AND nothing has started recently.

    Both conditions matter. A deep queue with a recent start is healthy
    backlog; a quiet queue is healthy idleness. Only waiting work plus silence
    is a stall.
    """
    depth = snapshot.get("ymoney_job_queue_depth", status="QUEUED")
    last_start = snapshot.get("ymoney_job_last_start_timestamp_seconds")
    idle = (now - last_start) if last_start > 0 else float("inf")
    firing = depth > 0 and idle > 900.0
    return _v(
        "queue_stalled",
        firing=firing,
        observed=idle if idle != float("inf") else -1.0,
        threshold=900.0, unit="seconds_since_last_start",
        reason=(
            f"{int(depth)} job(s) queued but nothing has started for "
            f"{'more than 15 minutes' if idle == float('inf') else f'{idle:.0f}s'}"
            if firing else
            f"queue depth {int(depth)}, last start {idle:.0f}s ago"
        ),
        queue_depth=depth, last_start_timestamp=last_start,
    )


def _rule_worker_fleet(snapshot: Snapshot, _now: float) -> AlertVerdict:
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
    never ran at all is treated as down rather than healthy.
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
    """Consecutive, not cumulative.

    A cumulative counter fires forever once a platform has ever failed once.
    Consecutive-failure count, reset by any success, is what distinguishes an
    outage from history.
    """
    worst = 0.0
    worst_platform = ""
    series = snapshot.values.get("ymoney_publish_failures_consecutive") or {}
    for key, value in series.items():
        if float(value) > worst:
            worst = float(value)
            worst_platform = dict(key).get("platform", "")
    firing = worst >= 3.0
    return _v(
        "repeated_publish_failure",
        firing=firing,
        observed=worst, threshold=3.0, unit="consecutive_failures",
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
        threshold=0.0, unit="count",
        evaluate=_rule_submission_unknown,
        runbook=(
            "GET /api/v1/workspaces/{id}/provider-maturity/incidents; reconcile "
            "the submission. Do NOT retry -- Reconciliation.RECONCILE or "
            "MANUAL_OVERRIDE, both audited."
        ),
    ),
    AlertRule(
        id="unknown_exposure_high",
        severity=Severity.CRITICAL,
        title="Unknown monetary exposure above threshold",
        what_it_detects="Estimated USD at CostOutcome.UNKNOWN_EXPOSURE is above the review threshold.",
        threshold=1.0, unit="usd",
        evaluate=_rule_unknown_exposure,
        runbook="Reconcile unknown submissions; check the provider dashboard for a charge with no matching record.",
    ),
    AlertRule(
        id="queue_stalled",
        severity=Severity.CRITICAL,
        title="Job queue stalled",
        what_it_detects="Jobs are queued and no job has started for the stall window.",
        threshold=900.0, unit="seconds_since_last_start",
        evaluate=_rule_queue_stalled,
        runbook="Check ymoney_workers_total and the DB collector heartbeat; a full connection pool blocks the claim query.",
    ),
    AlertRule(
        id="worker_fleet_unavailable",
        severity=Severity.CRITICAL,
        title="Worker fleet unavailable",
        what_it_detects="No durable job worker task is alive in this process.",
        threshold=0.0, unit="workers",
        evaluate=_rule_worker_fleet,
        runbook="Check the lifespan startup log; start_workers() did not complete or every worker task died.",
    ),
    AlertRule(
        id="db_unavailable",
        severity=Severity.CRITICAL,
        title="Database unavailable",
        what_it_detects="The database collector heartbeat is not up.",
        threshold=1.0, unit="bool",
        evaluate=_rule_db_unavailable,
        runbook="Check DATABASE_URL, disk space, and Postgres max_connections against db_pool_size + db_max_overflow.",
    ),
    AlertRule(
        id="storage_unavailable",
        severity=Severity.CRITICAL,
        title="Storage unavailable",
        what_it_detects="The storage backend failed its last health probe.",
        threshold=1.0, unit="bool",
        evaluate=_rule_storage_unavailable,
        runbook="Check STORAGE_ROOT writability, or the S3 endpoint/credentials. Renders cannot complete without it.",
    ),
    AlertRule(
        id="repeated_publish_failure",
        severity=Severity.WARNING,
        title="Repeated publish failure",
        what_it_detects="A platform has failed consecutive publishes with no success in between.",
        threshold=3.0, unit="consecutive_failures",
        evaluate=_rule_repeated_publish_failure,
        runbook="Check the connected account for that platform: an expired token looks exactly like this.",
    ),
    AlertRule(
        id="gpu_queue_starvation",
        severity=Severity.WARNING,
        title="GPU queue starvation",
        what_it_detects="GPU-gated jobs are waiting while the durable slot ledger reports no slot held.",
        threshold=0.0, unit="waiting_jobs",
        evaluate=_rule_gpu_starvation,
        runbook="Start a GPU worker (settings.gpu_worker) or free the held slots; check MEDIA_INTEL_GPU_SLOT rows for stale leases.",
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
        "firing_count": sum(1 for v in verdicts if v.firing),
        "rules": rules_out,
        "verdicts": [v.to_dict() for v in verdicts],
    }