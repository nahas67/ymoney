"""Audience → content feedback loop (Work 09, Lane C).

Repeated questions are normalized into ``CommunityInsight`` rows keyed by
``topic_key`` (deterministic hash of the normalized phrase). ``evidence_count``
grows with every supporting interaction; confidence stays ``low`` below the
evidence threshold (default 3) and is labeled explicitly — a single comment
never becomes a trend.

``community_content_feedback(db, workspace_id)`` aggregates those rows into
suggestions (topic, evidence, interaction ids, sample text, follow-up) that
feed the content-opportunity loop.
"""

from __future__ import annotations

import hashlib
import re

DEFAULT_EVIDENCE_THRESHOLD = 3
MAX_TOPIC_TOKENS = 6

_STOPWORDS_TEXT = """
a an and are as at be but by can could did do does for from had has have how
i if in into is it its me my no not of on or our so than that the their them
then there these they this to too us was we were what when where which who
why will with would you your please anyone everybody someone anyone help
thanks thank hi hey hello really just very
"""
_STOPWORDS = frozenset(_STOPWORDS_TEXT.split())

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize_topic(text: str) -> tuple[str, str]:
    """Deterministic (topic_key, topic) for one interaction body.

    Punctuation, stopwords and casing are removed; the remaining tokens are
    sorted so rephrasings of the same question map onto the same key.
    """
    raw = str(text or "").lower()
    tokens = [t for t in _TOKEN_RE.findall(raw)
              if t not in _STOPWORDS and len(t) > 2]
    if not tokens:
        return "", ""
    kept = tokens[:MAX_TOPIC_TOKENS]
    canonical = " ".join(sorted(set(kept)))
    key = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:32]
    topic = " ".join(kept)
    return key, topic


def _confidence_for(count: int) -> str:
    if count < DEFAULT_EVIDENCE_THRESHOLD:
        return "low"
    if count < 6:
        return "medium"
    return "high"


def record_insight(db, workspace_id: str, interaction, *,
                   topic_hint: str = "") -> object | None:
    """Upsert one insight for the interaction's normalized topic."""
    from sqlalchemy import select

    from app.models.community import CommunityInsight

    key, topic = normalize_topic(topic_hint or (interaction.text or ""))
    if not key:
        return None

    row = db.scalar(
        select(CommunityInsight).where(
            CommunityInsight.workspace_id == workspace_id,
            CommunityInsight.topic_key == key,
        )
    )
    evidence = list(row.source_interaction_ids) if row is not None else []
    if interaction.id not in evidence:
        evidence.append(interaction.id)

    if row is None:
        row = CommunityInsight(
            workspace_id=workspace_id,
            topic_key=key,
            topic=topic,
            representative_text=str(interaction.text or "")[:600],
            evidence_count=1,
            source_interaction_ids=[interaction.id],
            platforms=[interaction.platform] if getattr(interaction, "platform", "") else [],
            confidence="low",
        )
        db.add(row)
    else:
        row.evidence_count = len(evidence) or (row.evidence_count + 1)
        row.source_interaction_ids = evidence
        platforms = list(row.platforms or [])
        if interaction.platform and interaction.platform not in platforms:
            platforms.append(interaction.platform)
        row.platforms = platforms
        if not row.representative_text:
            row.representative_text = str(interaction.text or "")[:600]
    row.confidence = _confidence_for(len(evidence))
    db.flush()
    return row


def _followup(topic: str) -> str:
    return (f"Film a short answer to \"{topic}\" and pin a link to it in the "
            f"thread; reference the audience wording verbatim.")


def community_content_feedback(db, workspace_id: str, *,
                               threshold: int = DEFAULT_EVIDENCE_THRESHOLD,
                               limit: int = 100) -> list[dict]:
    """Aggregate audience questions into content suggestions.

    Every insight is returned; those below ``threshold`` carry an explicit
    ``low_confidence`` flag so a single comment is never presented as a trend.
    """
    from sqlalchemy import select

    from app.models.community import CommunityInsight

    rows = db.scalars(
        select(CommunityInsight)
        .where(CommunityInsight.workspace_id == workspace_id,
               CommunityInsight.state != "dismissed")
        .order_by(CommunityInsight.evidence_count.desc(),
                  CommunityInsight.created_at.asc())
        .limit(max(1, min(limit, 500)))
    ).all()

    suggestions: list[dict] = []
    for row in rows:
        count = int(row.evidence_count or 0)
        meets = count >= threshold
        suggestions.append({
            "topic": row.topic,
            "topic_key": row.topic_key,
            "evidence_count": count,
            "interaction_ids": list(row.source_interaction_ids or []),
            "sample_text": str(row.representative_text or "")[:400],
            "suggested_followup": _followup(row.topic),
            "platforms": list(row.platforms or []),
            "confidence": row.confidence or "low",
            "low_confidence": not meets,
            "meets_threshold": meets,
            "threshold": threshold,
            "suggested_action": "create_content" if meets else "watch",
            "insight_id": row.id,
        })
    return suggestions


__all__ = [
    "DEFAULT_EVIDENCE_THRESHOLD",
    "community_content_feedback",
    "normalize_topic",
    "record_insight",
]
