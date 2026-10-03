"""Work 15 §10 — topic dedupe and content fatigue.

Answers "have we done this already?" with four verdicts:

    ``NEW``         nothing similar exists
    ``RELATED``     a sibling exists; fine to make, but it is a follow-up
    ``DUPLICATE``   the same thing; block unless explicitly a series
    ``SATURATED``   we have published this angle repeatedly; block

The important nuance is the **series exception**. A legitimate sequel ("part
2", a recurring series, a seasonal follow-up) is intentionally similar, and
blocking it would stop the workspace from ever building a format people
recognise. So an explicitly declared series is exempt — but only when the
declaration names the relationship, and the exemption is recorded so the
decision is auditable rather than invisible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.engine.decision import topic_similarity
from app.models.content import ContentItem, PublishedPost
from app.models.planning import (
    DEDUPE_DUPLICATE,
    DEDUPE_NEW,
    DEDUPE_RELATED,
    DEDUPE_SATURATED,
)

__all__ = [
    "DEDUPE_THRESHOLDS",
    "DedupeResult",
    "detect_dedupe",
    "library_similarity",
]

#: Above this similarity the topic is a DUPLICATE of existing work.
DUPLICATE_SIMILARITY = 0.82
#: Above this it is a legitimate follow-up.
RELATED_SIMILARITY = 0.55
#: How many recent posts on one topic/format make it SATURATED.
SATURATION_COUNT = 3
#: The window saturation is measured over.
SATURATION_WINDOW_DAYS = 45.0
#: Excessive repetition of one platform across a short window.
PLATFORM_REPEAT_WINDOW_DAYS = 7.0
PLATFORM_REPEAT_LIMIT = 3

DEDUPE_THRESHOLDS = {
    "duplicate": DUPLICATE_SIMILARITY,
    "related": RELATED_SIMILARITY,
    "saturation_count": SATURATION_COUNT,
    "saturation_window_days": SATURATION_WINDOW_DAYS,
    "platform_repeat_limit": PLATFORM_REPEAT_LIMIT,
}


@dataclass
class DedupeResult:
    verdict: str
    reason: str
    #: the similar items that produced the verdict, newest first
    matches: list[dict] = field(default_factory=list)
    similarity: float = 0.0
    #: True when an explicit series declaration overrode a block
    series_exempt: bool = False
    series_name: str = ""

    @property
    def is_blocking(self) -> bool:
        return self.verdict in (DEDUPE_DUPLICATE, DEDUPE_SATURATED)

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "reason": self.reason,
                "matches": list(self.matches),
                "similarity": round(self.similarity, 4),
                "series_exempt": self.series_exempt,
                "series_name": self.series_name}


#: Light suffix stripping applied before comparing. ``topic_similarity`` is a
#: Jaccard over exact tokens, so "budget tips" and "budgeting tips" score 0
#: despite being the same subject -- which for a dedupe check is a miss, not a
#: near-miss. This is deliberately the smallest rule set that fixes the
#: inflection we actually see in titles, not a stemmer.
_SUFFIXES = ("ing", "ings", "ed", "es", "s")


def _stem(token: str) -> str:
    for suffix in _SUFFIXES:
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _stemmed(text: str) -> str:
    return " ".join(_stem(t) for t in re.findall(r"[a-z0-9]+",
                                               (text or "").lower()))


def _similarity(a: str, b: str) -> float:
    """Jaccard on stemmed tokens.

    Uses the SAME similarity the DecisionEngine ranks with, so the planner and
    the engine never disagree about what counts as "the same topic".
    """
    if not a or not b:
        return 0.0
    return topic_similarity(_stemmed(a), _stemmed(b))


def _recent_posts(db, workspace_id: str, *, days: float) -> list[ContentItem]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows = db.scalars(
        select(ContentItem).where(
            ContentItem.workspace_id == workspace_id).order_by(
            ContentItem.created_at.desc())).all()
    out = []
    for row in rows:
        created = getattr(row, "created_at", None)
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created >= cutoff:
            out.append(row)
    return out


def _published_topics(db, workspace_id: str) -> list[tuple[str, str]]:
    """(topic, platform) for everything actually published in this workspace.

    BOTH sides are workspace-scoped. Filtering only on ``PublishedPost`` lets a
    post whose content row is stamped with a different workspace join in, which
    would leak that workspace's topic into this one's dedupe.
    """
    rows = db.execute(
        select(ContentItem.topic, PublishedPost.platform)
        .join(PublishedPost, PublishedPost.content_item_id == ContentItem.id)
        .where(PublishedPost.workspace_id == workspace_id,
               ContentItem.workspace_id == workspace_id)).all()
    return [(str(topic or ""), str(platform or "")) for topic, platform in rows]


def library_similarity(db, workspace_id: str, topic: str, *,
                       include_unpublished: bool = True) -> tuple[float, list[dict]]:
    """Closest existing content to ``topic``. Returns (max_similarity, matches)."""
    candidates: list[tuple[str, str]] = []
    if include_unpublished:
        candidates += [(str(c.topic or ""), "library")
                       for c in _recent_posts(db, workspace_id, days=3650)]
    candidates += _published_topics(db, workspace_id)
    best = 0.0
    matches: list[dict] = []
    for candidate, origin in candidates:
        if not candidate:
            continue
        score = _similarity(topic, candidate)
        if score > best:
            best = score
        if score >= RELATED_SIMILARITY:
            matches.append({"topic": candidate, "origin": origin,
                            "similarity": round(score, 4)})
    matches.sort(key=lambda m: -m["similarity"])
    return best, matches[:5]


def _platform_repetition(db, workspace_id: str, platform: str) -> int:
    """How many times ``platform`` was published to inside the window.

    The window is a real filter, not decoration. Counting every post ever made
    the reason string claim "in the last 7 days" about posts from last year.
    """
    cutoff = (datetime.now(UTC)
              - timedelta(days=PLATFORM_REPEAT_WINDOW_DAYS)).replace(
        tzinfo=None)
    rows = db.execute(
        select(PublishedPost.platform, PublishedPost.created_at)
        .where(PublishedPost.workspace_id == workspace_id,
               PublishedPost.created_at >= cutoff)).all()
    return sum(1 for posted_platform, _created in rows
               if str(posted_platform) == str(platform))


def detect_dedupe(db, workspace_id: str, topic: str, *,
                  angle: str = "",
                  platforms: list[str] | None = None,
                  series_name: str = "",
                  as_of: datetime | None = None) -> DedupeResult:
    """Classify a proposed topic against the workspace's existing library.

    ``series_name`` declares an intentional series relationship. It downgrades
    a DUPLICATE/SATURATED to RELATED — but the exemption is returned on the
    result so the planner can record WHY the block was lifted, and the
    similarity that triggered it is preserved.
    """
    as_of = as_of or datetime.now(UTC)
    # The topic and the angle are compared SEPARATELY, then combined with the
    # MAXIMUM. Concatenating them ("budget tips / cover budget tips") diluted an
    # exact-duplicate similarity of 1.00 down to ~0.75 -- below the 0.82
    # DUPLICATE threshold -- which made the duplicate block unreachable from
    # the engine while still looking correct in isolation.
    topic_similarity_best, matches = library_similarity(db, workspace_id, topic)
    best = topic_similarity_best
    if angle.strip():
        angle_similarity, angle_matches = library_similarity(db, workspace_id,
                                                            angle)
        if angle_similarity > best:
            best = angle_similarity
        # an angle match is evidence too, so it joins the match list
        for match in angle_matches:
            if match["topic"] not in {m["topic"] for m in matches}:
                matches.append(match)
        matches.sort(key=lambda m: -m["similarity"])
        matches = matches[:5]

    # Saturation: how often have we published this angle recently?
    window = _recent_posts(db, workspace_id, days=SATURATION_WINDOW_DAYS)
    close_recent = [
        item for item in window
        if max(_similarity(topic, str(item.topic or "")),
               _similarity(angle, str(item.topic or "")) if angle.strip() else 0.0)
        >= RELATED_SIMILARITY
    ]
    saturated = len(close_recent) >= SATURATION_COUNT

    repeated_platform = ""
    for platform in (platforms or []):
        if _platform_repetition(db, workspace_id, platform) >= PLATFORM_REPEAT_LIMIT:
            repeated_platform = platform
            break

    if best >= DUPLICATE_SIMILARITY:
        verdict, reason = DEDUPE_DUPLICATE, (
            f"similarity {best:.2f} >= {DUPLICATE_SIMILARITY} against "
            f"{matches[0]['topic']!r}")
    elif saturated:
        verdict, reason = DEDUPE_SATURATED, (
            f"{len(close_recent)} posts in the last "
            f"{SATURATION_WINDOW_DAYS:.0f} days at >= {RELATED_SIMILARITY} "
            f"similarity")
    elif best >= RELATED_SIMILARITY:
        verdict, reason = DEDUPE_RELATED, (
            f"similarity {best:.2f} >= {RELATED_SIMILARITY}: a follow-up, not a "
            f"repeat")
    elif repeated_platform:
        verdict, reason = DEDUPE_RELATED, (
            f"{repeated_platform} already carries "
            f"{PLATFORM_REPEAT_LIMIT} posts in the last "
            f"{PLATFORM_REPEAT_WINDOW_DAYS:.0f} days; space it out")
    else:
        return DedupeResult(verdict=DEDUPE_NEW,
                            reason="no similar content in the library",
                            similarity=best)

    if series_name and verdict in (DEDUPE_DUPLICATE, DEDUPE_SATURATED):
        # A declared series is intentionally similar. The block is lifted, but
        # the underlying similarity is kept on the record.
        return DedupeResult(
            verdict=DEDUPE_RELATED,
            reason=(f"declared series {series_name!r}: {reason} — allowed as an "
                    f"intentional follow-up"),
            matches=matches, similarity=best, series_exempt=True,
            series_name=series_name)
    return DedupeResult(verdict=verdict, reason=reason, matches=matches,
                        similarity=best, series_name=series_name)
