"""Creative experiments: CRUD state machine + honest stats (Work 06 Lane B).

State machine: DRAFT -> RUNNING -> COMPLETED | INCONCLUSIVE | CANCELLED,
with INSUFFICIENT_DATA as the automatic outcome when the sample gate fails
(re-analysis is allowed from there once more data arrives). Cancel is
allowed from DRAFT, RUNNING and INSUFFICIENT_DATA.

Analysis doctrine (no fake confidence):
- The minimum-data gate runs first: total samples across arms below
  ``minimum_sample`` -> INSUFFICIENT_DATA with a stated reason.
- Arms with zero samples -> INCONCLUSIVE (comparison assumptions unmet).
- A Welch-style 95% interval for the difference of means is computed only
  when each arm has n >= 30; otherwise INCONCLUSIVE with a stated reason.
- COMPLETED requires the interval to exclude zero; the winner is the
  significant variant with the largest relative lift. ``confidence`` is the
  literal ``"95% CI excludes zero"`` on COMPLETED and ``"n/a"`` otherwise —
  never a fabricated universal score.
"""

from __future__ import annotations

import math
from datetime import datetime

from sqlalchemy import or_, select

from app.models.base import utcnow
from app.models.content import PostMetric, PublishedPost
from app.models.experiment import EXPERIMENT_KINDS, EXPERIMENT_STATUSES, Experiment

KINDS = EXPERIMENT_KINDS
STATUSES = EXPERIMENT_STATUSES

#: PostMetric numeric columns eligible as experiment metrics.
METRICS = (
    "views",
    "likes",
    "comments",
    "shares",
    "saves",
    "watch_time_seconds",
    "avg_view_duration_seconds",
    "completion_rate",
    "ctr",
    "followers_gained",
)

#: Per-arm sample size required before a confidence interval is computed.
MIN_ARM_N = 30
#: Two-sided 95% normal critical value (valid at n>=30/arm by CLT).
Z_95 = 1.96


class ExperimentError(ValueError):
    """Invalid experiment transition, payload, or analysis precondition."""


def _clean_ref(value) -> str:
    ref = str(value or "").strip()
    if not ref:
        raise ExperimentError("variant_ref is required")
    return ref[:200]


def create_experiment(
    session,
    workspace_id: str,
    *,
    kind: str,
    hypothesis: str = "",
    control: dict | None = None,
    variants: list | None = None,
    platform: str = "",
    primary_metric: str = "views",
    secondary_metrics: list | None = None,
    minimum_sample: int = 60,
) -> Experiment:
    """Create a DRAFT experiment after validating kind/metrics/arms."""
    norm_kind = str(kind or "").strip().upper()
    if norm_kind not in KINDS:
        raise ExperimentError(f"unknown experiment kind '{kind}'")
    metric = str(primary_metric or "").strip()
    if metric not in METRICS:
        raise ExperimentError(f"unknown primary metric '{primary_metric}'")
    secondaries = [str(m).strip() for m in (secondary_metrics or []) if str(m).strip()]
    for extra in secondaries:
        if extra not in METRICS:
            raise ExperimentError(f"unknown secondary metric '{extra}'")
    try:
        min_sample = int(minimum_sample)
    except (TypeError, ValueError) as exc:
        raise ExperimentError("minimum_sample must be an integer") from exc
    if min_sample < 2:
        raise ExperimentError("minimum_sample must be >= 2")
    control_ref = _clean_ref((control or {}).get("variant_ref"))
    variant_rows = []
    for entry in variants or []:
        if not isinstance(entry, dict):
            raise ExperimentError("each variant must be {variant_ref, descriptor}")
        variant_rows.append({
            "variant_ref": _clean_ref(entry.get("variant_ref")),
            "descriptor": str(entry.get("descriptor", "") or "")[:500],
        })
    if not variant_rows:
        raise ExperimentError("at least one variant arm is required")
    refs = [control_ref, *(v["variant_ref"] for v in variant_rows)]
    if len(set(refs)) != len(refs):
        raise ExperimentError("control and variant refs must be distinct")
    row = Experiment(
        workspace_id=str(workspace_id),
        kind=norm_kind,
        hypothesis=str(hypothesis or "")[:2000],
        control_json={"variant_ref": control_ref},
        variants_json=variant_rows,
        platform=str(platform or "")[:30],
        primary_metric=metric,
        secondary_metrics=secondaries,
        minimum_sample=min_sample,
        status="DRAFT",
    )
    session.add(row)
    session.flush()
    return row


def start_experiment(session, experiment: Experiment) -> Experiment:
    """DRAFT -> RUNNING (records started_at)."""
    if experiment.status != "DRAFT":
        raise ExperimentError(f"cannot start experiment from status '{experiment.status}'")
    experiment.status = "RUNNING"
    experiment.started_at = utcnow()
    session.flush()
    return experiment


def cancel_experiment(session, experiment: Experiment) -> Experiment:
    """Cancel from DRAFT, RUNNING or INSUFFICIENT_DATA (records ended_at)."""
    if experiment.status not in ("DRAFT", "RUNNING", "INSUFFICIENT_DATA"):
        raise ExperimentError(f"cannot cancel experiment from status '{experiment.status}'")
    experiment.status = "CANCELLED"
    experiment.ended_at = utcnow()
    session.flush()
    return experiment


def assign_arms(experiment: Experiment) -> dict[str, str]:
    """Canonical mapping from the experiment definition: ref -> arm name."""
    mapping = {str((experiment.control_json or {}).get("variant_ref", "")): "control"}
    for i, entry in enumerate(experiment.variants_json or []):
        mapping[str((entry or {}).get("variant_ref", ""))] = f"variant_{i}"
    return {ref: arm for ref, arm in mapping.items() if ref}


def auto_assign(candidate_refs: list, num_variant_arms: int) -> dict[str, str]:
    """Deterministically map platform variant refs onto control/variant arms.

    First ref -> control, the rest round-robin over the variant arms. Pure
    function so publishers can assign cuts without touching experiment rows.
    """
    refs = [str(r).strip() for r in (candidate_refs or []) if str(r).strip()]
    if not refs:
        return {}
    arms = ["control", *(f"variant_{i}" for i in range(max(0, int(num_variant_arms))))]
    if len(arms) == 1:
        return {refs[0]: "control"}
    return {ref: arms[i % len(arms)] for i, ref in enumerate(refs)}


def _arm_samples(session, experiment: Experiment, ref: str) -> list[float]:
    """Latest primary-metric value per published post for one arm ref."""
    metric = experiment.primary_metric
    query = select(PublishedPost).where(
        PublishedPost.workspace_id == experiment.workspace_id,
        or_(PublishedPost.platform_variant_id == ref, PublishedPost.id == ref),
    )
    if experiment.platform:
        query = query.where(PublishedPost.platform == experiment.platform)
    values: list[float] = []
    for post in session.scalars(query).all():
        rows = session.scalars(
            select(PostMetric).where(PostMetric.post_id == post.id)
        ).all()
        if not rows:
            continue
        latest = max(rows, key=lambda r: (
            r.captured_at or r.created_at or datetime.min, str(r.id)))
        raw = getattr(latest, metric, None)
        if raw is None:
            continue
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            continue
    return values


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _sample_variance(values: list[float], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    return sum((v - mean) ** 2 for v in values) / (len(values) - 1)


def _welch_interval(control: list[float], treatment: list[float]) -> dict:
    """Welch-style 95% interval for (treatment mean - control mean).

    Returns diff/rel/CI/significance, or a reason when assumptions are unmet
    (n < 30/arm, zero variance). Never fabricates significance.
    """
    n_c, n_t = len(control), len(treatment)
    mean_c, mean_t = _mean(control), _mean(treatment)
    diff = mean_t - mean_c
    rel = (diff / mean_c) if mean_c else None
    base = {"abs_diff": diff, "rel_diff": rel,
            "ci_low": None, "ci_high": None, "significant": False}
    if n_c < MIN_ARM_N or n_t < MIN_ARM_N:
        return {**base,
                "reason": (f"n too small for a confidence interval "
                           f"(control n={n_c}, variant n={n_t}; need >={MIN_ARM_N}/arm)")}
    var_c = _sample_variance(control, mean_c)
    var_t = _sample_variance(treatment, mean_t)
    std_err = math.sqrt(var_c / n_c + var_t / n_t)
    if std_err <= 0:
        return {**base, "reason": "zero variance — effect not measurable"}
    half = Z_95 * std_err
    low, high = diff - half, diff + half
    return {**base, "ci_low": low, "ci_high": high,
            "significant": bool(low > 0 or high < 0), "reason": ""}


def analyze_experiment(session, experiment: Experiment) -> Experiment:
    """Run the minimum-gate then per-arm comparison; transitions status.

    Allowed from RUNNING (first analysis) or INSUFFICIENT_DATA (re-analysis
    after more data arrives).
    """
    if experiment.status not in ("RUNNING", "INSUFFICIENT_DATA"):
        raise ExperimentError(
            f"cannot analyze experiment from status '{experiment.status}'")
    control_ref = str((experiment.control_json or {}).get("variant_ref", ""))
    control_samples = _arm_samples(session, experiment, control_ref)
    arms: list[dict] = []
    for i, entry in enumerate(experiment.variants_json or []):
        ref = str((entry or {}).get("variant_ref", ""))
        samples = _arm_samples(session, experiment, ref)
        stats = _welch_interval(control_samples, samples)
        arms.append({
            "variant_ref": ref,
            "descriptor": str((entry or {}).get("descriptor", "") or ""),
            "arm": f"variant_{i}",
            "n": len(samples),
            "mean": _mean(samples),
            **stats,
        })
    total = len(control_samples) + sum(a["n"] for a in arms)
    result: dict = {
        "primary_metric": experiment.primary_metric,
        "control": {"variant_ref": control_ref,
                    "n": len(control_samples), "mean": _mean(control_samples)},
        "arms": arms,
        "winner": None,
        "reason": "",
        "minimum_sample": experiment.minimum_sample,
        "total_samples": total,
        "analyzed_at": utcnow().isoformat() + "Z",
    }
    if total < int(experiment.minimum_sample or 0):
        experiment.status = "INSUFFICIENT_DATA"
        result["reason"] = (
            f"only {total} sample(s) across arms; need minimum_sample="
            f"{experiment.minimum_sample}")
        experiment.result_json = result
        experiment.confidence = "n/a"
        session.flush()
        return experiment
    if not control_samples or any(a["n"] == 0 for a in arms):
        experiment.status = "INCONCLUSIVE"
        result["reason"] = "at least one arm has no metric samples — comparison unmet"
        experiment.result_json = result
        experiment.confidence = "n/a"
        experiment.ended_at = utcnow()
        session.flush()
        return experiment
    contenders = [a for a in arms
                  if a["significant"] and (a["ci_low"] or 0) > 0 and a["rel_diff"] is not None]
    if not contenders:
        experiment.status = "INCONCLUSIVE"
        blocking = next((a["reason"] for a in arms if a.get("reason")), "")
        result["reason"] = blocking or "no variant beats control (95% CI includes zero)"
        experiment.result_json = result
        experiment.confidence = "n/a"
        experiment.ended_at = utcnow()
        session.flush()
        return experiment
    winner = max(contenders, key=lambda a: float(a["rel_diff"] or 0.0))
    experiment.status = "COMPLETED"
    result["winner"] = winner["variant_ref"]
    result["reason"] = (
        f"variant '{winner['variant_ref']}' lifts {experiment.primary_metric} "
        f"by {float(winner['rel_diff']) * 100:.1f}% (95% CI excludes zero)")
    experiment.result_json = result
    experiment.confidence = "95% CI excludes zero"
    experiment.ended_at = utcnow()
    session.flush()
    return experiment
