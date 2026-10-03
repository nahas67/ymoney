"""Work 15 §11 — the planner feedback loop, and its statistical floor.

    Publication -> performance -> community reactions -> experiments -> lessons
                -> future planner

What reaches the planner is written into GlobalMemory as
``CONTENT_RESULT`` / ``CREATIVE_LESSON`` rows, so the next
:func:`~app.engine.planning.engine.ContentPlanningEngine.plan` run sees it
through the ordinary memory path rather than a bespoke feedback channel.

**The rule that matters most here: one content item is not a lesson.** A single
post's engagement is noise -- it cannot establish that a format "works". So
every lesson is written with the sample size it was derived from, and
:func:`derive_lesson` refuses to emit a lesson below :data:`MIN_SAMPLE` measured
items or with an effect size below :data:`MIN_EFFECT`. That is the difference
between a feedback loop and a machine that confidently learns from its first
hit.

The reported quantity is ``effect_size`` -- a measured engagement-rate gap --
and deliberately NOT ``confidence``. A rate difference is not a probability,
and labelling it as one would reintroduce exactly the kind of claim this module
exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from app.models.content import ContentItem, PublishedPost
from app.models.planning import EditorialPlanItem

__all__ = [
    "FeedbackRecord",
    "MIN_CONFIDENCE",
    "MIN_SAMPLE",
    "collect_feedback",
    "derive_lesson",
    "record_outcome",
]

#: Below this many comparable items, no lesson is emitted at all.
MIN_SAMPLE = 5
#: Below this absolute engagement-rate gap between the best and worst group,
#: the ranking is noise and no lesson is emitted. It is an EFFECT SIZE, not a
#: confidence: a rate difference is not a probability.
MIN_EFFECT = 0.25
#: Backwards-compatible alias for callers that only need the threshold value.
MIN_CONFIDENCE = MIN_EFFECT
#: The window an outcome is considered within.
OUTCOME_WINDOW_DAYS = 45.0

#: Plan-item states that no longer represent outstanding work. They are still
#: eligible as lesson samples if they have a measured outcome, so this is not a
#: filter for the sibling query -- it exists for callers that want "what is
#: still to do".
_OPEN_STATUSES = ("IDEA", "PLANNED", "RESEARCHING", "READY", "IN_PRODUCTION",
                  "BLOCKED")


@dataclass
class FeedbackRecord:
    """What actually happened to one planned item."""

    plan_item_id: str
    content_item_id: str = ""
    platform: str = ""
    views: int = 0
    engagements: int = 0
    engagement_rate: float = 0.0
    community_reactions: int = 0
    experiment_results: list[dict] = field(default_factory=list)
    measured_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: True when a published post was actually found for this item. False with
    #: views=0 means "nothing was published yet", which is NOT the same as
    #: "published and got no views" -- the two must not be reported alike.
    linked: bool = False
    #: How many metric snapshots existed. The totals come from the LATEST one
    #: only, so this is the count that was deliberately NOT summed.
    snapshots_seen: int = 0

    def to_dict(self) -> dict:
        return {"plan_item_id": self.plan_item_id,
                "content_item_id": self.content_item_id,
                "platform": self.platform, "views": self.views,
                "engagements": self.engagements,
                "engagement_rate": round(self.engagement_rate, 4),
                "community_reactions": self.community_reactions,
                "experiment_results": self.experiment_results,
                "linked": self.linked,
                "snapshots_seen": self.snapshots_seen,
                "measured_at": self.measured_at.isoformat()}


def collect_feedback(db, workspace_id: str, plan_item_id: str) -> FeedbackRecord:
    """Gather the measured outcome of a planned item.

    Reads the canonical metrics on the ``PublishedPost`` rows the publication
    pipeline already wrote. Nothing is estimated: an item with no published
    content reports ``views=0`` and ``linked=False`` so the caller can see the
    difference between "published and got no views" and "never published".

    The lookup resolves the item's link to real ``ContentItem`` rows. The item
    holds an ``opportunity_id``, which is an Opportunity primary key -- matching
    it against ``PublishedPost.content_item_id`` (a ContentItem key) compares
    two unrelated identifiers and silently matches nothing, which would make
    every outcome read as zero.
    """
    item = db.get(EditorialPlanItem, plan_item_id)
    record = FeedbackRecord(plan_item_id=plan_item_id)
    if item is None or item.workspace_id != workspace_id:
        return record

    # ContentItem.opportunity_id is the real link: the content that was
    # produced for this opportunity.
    content_ids = set(db.scalars(select(ContentItem.id).where(
        ContentItem.workspace_id == workspace_id,
        ContentItem.opportunity_id == item.opportunity_id)).all()) \
        if item.opportunity_id else set()
    if item.campaign_id:
        content_ids |= set(db.scalars(select(ContentItem.id).where(
            ContentItem.workspace_id == workspace_id,
            ContentItem.campaign_id == item.campaign_id)).all())

    posts: list[PublishedPost] = []
    if content_ids:
        posts = list(db.scalars(select(PublishedPost).where(
            PublishedPost.workspace_id == workspace_id,
            PublishedPost.content_item_id.in_(content_ids))).all())
    if not posts and item.campaign_id:
        posts = list(db.scalars(select(PublishedPost).where(
            PublishedPost.workspace_id == workspace_id,
            PublishedPost.campaign_id == item.campaign_id)).all())

    record.linked = bool(posts)
    for post in posts:
        record.platform = record.platform or str(post.platform or "")
        record.content_item_id = record.content_item_id or str(
            post.content_item_id or "")
        # Only the LATEST snapshot per post. PostMetric rows are CUMULATIVE
        # point-in-time totals and the Analytics Agent writes one per cycle, so
        # summing them counted the same views three times over.
        snapshots = sorted(post.metrics or [],
                           key=lambda m: getattr(m, "captured_at", None)
                           or datetime.min)
        if not snapshots:
            continue
        latest = snapshots[-1]
        # PostMetric carries real columns, not a JSON blob. Reading the columns
        # is what makes this MEASURED rather than assumed.
        record.views += int(getattr(latest, "views", 0) or 0)
        record.engagements += sum(
            int(getattr(latest, name, 0) or 0)
            for name in ("likes", "comments", "shares", "saves"))  # noqa: B007
        record.snapshots_seen += len(snapshots)
    if record.views > 0:
        record.engagement_rate = record.engagements / record.views
    return record


def _rate(sample: FeedbackRecord) -> float:
    """Engagement rate for a sample, computed rather than trusted.

    ``FeedbackRecord.engagement_rate`` is a field the collector fills in when it
    has real metrics. A directly-constructed record (a test, a replay, a future
    importer) may leave it at 0 while carrying valid counts, so derive it here
    rather than comparing a column that was never set -- which would make every
    group look identical and silently suppress every lesson.
    """
    if sample.views > 0:
        return sample.engagements / sample.views
    return 0.0


def derive_lesson(samples: list[FeedbackRecord], *,
                  dimension: str = "platform") -> dict | None:
    """Derive a lesson from comparable outcomes, or refuse.

    Returns ``None`` when the sample is too small, there is only one group (so
    there is nothing to compare), or the spread is noise. The refusal is the
    feature: a feedback loop that always produces a conclusion is not a feedback
    loop, it is a rumour mill.

    ``effect_size`` is the absolute engagement-rate gap -- a MEASURED delta, not
    a probability. It is reported under its own name; ``confidence`` is
    deliberately NOT set to it, because a rate difference is not a confidence
    and labelling it as one would be exactly the kind of claim this module
    exists to avoid.
    """
    usable = [s for s in samples if s.views > 0]
    if len(usable) < MIN_SAMPLE:
        return None
    by_dimension: dict[str, list[FeedbackRecord]] = {}
    for sample in usable:
        key = getattr(sample, dimension, "") or "unknown"
        by_dimension.setdefault(str(key), []).append(sample)
    ranked = sorted(
        ((key, sum(_rate(s) for s in group) / len(group))
         for key, group in by_dimension.items() if group),
        key=lambda pair: -pair[1])
    if len(ranked) < 2:
        return None
    best_key, best_rate = ranked[0]
    worst_key, worst_rate = ranked[-1]
    effect = best_rate - worst_rate
    if effect < MIN_EFFECT:
        return None
    return {
        "type": "CREATIVE_LESSON",
        "dimension": dimension,
        "claim": (f"{best_key} outperformed {worst_key} on engagement rate "
                  f"({best_rate:.3f} vs {worst_rate:.3f})"),
        # the measured gap, named for what it is
        "effect_size": round(effect, 4),
        # deliberately absent: a "confidence" derived from a rate delta
        "sample_size": len(usable),
        "samples_per_group": {k: len(g) for k, g in by_dimension.items()},
        "caveat": (f"derived from {len(usable)} measured item(s); an observed "
                   f"rate difference, NOT a forecast and NOT a guarantee for "
                   f"future content"),
        "derived_at": datetime.now(UTC).isoformat(),
    }


def record_outcome(db, workspace_id: str, plan_item_id: str, *,
                   brand_id: str = "",
                   min_effect: float = MIN_EFFECT) -> dict:
    """Write an item's outcome, and a lesson only when one is warranted.

    The per-item outcome is ALWAYS recorded (it is a fact). A generalised
    lesson is written only when the sample size and the measured effect size
    clear their floors.
    """
    from app.engine.knowledge.memory import GlobalMemory

    record = collect_feedback(db, workspace_id, plan_item_id)
    stored: list[str] = []
    # 1. the factual outcome, always -- but stated honestly when nothing was
    #    published, so a later planner does not read "0 views" as a result
    if record.linked:
        summary = (f"planner item {plan_item_id} on {record.platform}: "
                   f"{record.views} view(s), {record.engagements} engagement(s), "
                   f"rate {record.engagement_rate:.4f}")
        confidence = 0.8
    else:
        summary = (f"planner item {plan_item_id}: no published content linked "
                   f"yet, so no outcome is measured")
        confidence = 0.1
    memory_id = GlobalMemory.store(
        db, workspace_id, type="CONTENT_RESULT", content=summary,
        confidence=confidence, scope="workspace",
        brand_id=brand_id or None, topic=record.platform,
        platform=record.platform, origin="work15.feedback",
        source_ids=[record.content_item_id] if record.content_item_id else None)
    stored.append(memory_id)

    # 2. a generalised lesson, only when warranted
    lesson: dict | None = None
    item = db.get(EditorialPlanItem, plan_item_id)
    if item is not None and item.plan_id and item.workspace_id == workspace_id:
        # Every sibling in the plan is a candidate; ``derive_lesson`` keeps only
        # the ones with a MEASURED outcome. Filtering on a plan-item status here
        # was both the wrong proxy (the publication pipeline, not the planner,
        # decides when work ships) and unreachable (no code writes PUBLISHED),
        # so the sample was always empty and no lesson could ever be derived.
        siblings = db.scalars(select(EditorialPlanItem).where(
            EditorialPlanItem.workspace_id == workspace_id,
            EditorialPlanItem.plan_id == item.plan_id)).all()
        samples = [collect_feedback(db, workspace_id, s.id) for s in siblings]
        lesson = derive_lesson(samples)
    if lesson and lesson["effect_size"] >= min_effect:
        lesson_id = GlobalMemory.store(
            db, workspace_id, type=lesson["type"],
            content=lesson["claim"],
            # Confidence is about EVIDENCE QUALITY, not effect size. A large rate
            # gap is not more trustworthy, and deriving confidence from it
            # would put the exact number this module refuses to report into the
            # memory row. Sample size is the honest driver, tempered because 5
            # items is a floor rather than a certainty.
            confidence=round(min(0.6, 0.2 + 0.05 * lesson["sample_size"]), 4),
            scope="workspace", brand_id=brand_id or None,
            platform=record.platform, origin="work15.feedback.lesson",
            evidence_ids=stored)
        stored.append(lesson_id)
    return {"plan_item_id": plan_item_id, "outcome": record.to_dict(),
            "lesson": lesson, "memory_ids": stored,
            "lesson_written": bool(lesson) and len(stored) > 1}
