"""Community analytics (Work 09, Lane C).

``compute_community_metrics(db, workspace_id, days=30)`` returns a
JSON-safe rollup: volume, questions, reply rate, response time
(average + median), sentiment distribution, content requests, lead signals,
moderation counts and a per-content breakdown reached through the linkage
chain Interaction → PublishedPost → platform_variant_id / content_item_id →
campaign_id.
"""

from __future__ import annotations

from datetime import timedelta

from app.models.base import utcnow
from app.models.community import SocialInteraction


def _labels(row) -> set[str]:
    out: set[str] = set()
    for entry in (getattr(row, "classifications_json", None) or []):
        if isinstance(entry, dict) and entry.get("label"):
            out.add(str(entry["label"]))
    return out


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(float(ordered[mid]), 2)
    return round((ordered[mid - 1] + ordered[mid]) / 2.0, 2)


def _linkage(db, row) -> dict:
    """Resolve post → variant/content/campaign when the row is not denormalized."""
    content_item_id = row.content_item_id
    campaign_id = row.campaign_id
    variant_id = None
    post_platform = None
    if row.published_post_id:
        from app.models import PublishedPost

        post = db.get(PublishedPost, row.published_post_id)
        if post is not None and post.workspace_id == row.workspace_id:
            variant_id = post.platform_variant_id
            content_item_id = content_item_id or post.content_item_id
            campaign_id = campaign_id or post.campaign_id
            post_platform = post.platform
    return {
        "published_post_id": row.published_post_id or "",
        "platform_variant_id": variant_id or "",
        "content_item_id": content_item_id or "",
        "campaign_id": campaign_id or "",
        "post_platform": post_platform or row.platform or "",
    }


def compute_community_metrics(db, workspace_id: str, *, days: int = 30) -> dict:
    """Aggregate community activity for one workspace (workspace-isolated)."""
    from sqlalchemy import select

    from app.models.community import CommunityAction

    days = max(1, int(days or 30))
    since = utcnow() - timedelta(days=days)

    rows = list(db.scalars(
        select(SocialInteraction)
        .where(SocialInteraction.workspace_id == workspace_id,
               SocialInteraction.created_at >= since)
        .order_by(SocialInteraction.created_at.asc())
    ).all())

    sentiment = {"positive": 0, "negative": 0, "neutral": 0, "mixed": 0}
    questions = content_requests = lead_signals = spam = 0
    by_content: dict[str, dict] = {}
    moderation = {"allowed": 0, "review": 0, "hidden": 0, "blocked": 0, "pending": 0}

    for row in rows:
        labels = _labels(row)
        sent = str(row.sentiment or "")
        if sent in sentiment:
            sentiment[sent] += 1
        if row.is_question:
            questions += 1
        if "CONTENT_REQUEST" in labels:
            content_requests += 1
        if "LEAD" in labels:
            lead_signals += 1
        if row.status == "spam" or "SPAM" in labels:
            spam += 1
        state = str(row.moderation_state or "pending")
        if state in moderation:
            moderation[state] += 1

        link = _linkage(db, row)
        key = link["published_post_id"] or link["campaign_id"] or link["content_item_id"] or "unlinked"
        bucket = by_content.setdefault(key, {
            "published_post_id": link["published_post_id"],
            "platform": link["post_platform"],
            "content_item_id": link["content_item_id"],
            "platform_variant_id": link["platform_variant_id"],
            "campaign_id": link["campaign_id"],
            "interactions": 0,
            "questions": 0,
            "negative_feedback": 0,
            "content_requests": 0,
            "lead_signals": 0,
            "replies_sent": 0,
            "samples": [],
        })
        bucket["interactions"] += 1
        if row.is_question:
            bucket["questions"] += 1
        if sent in ("negative", "mixed"):
            bucket["negative_feedback"] += 1
        if "CONTENT_REQUEST" in labels:
            bucket["content_requests"] += 1
        if "LEAD" in labels:
            bucket["lead_signals"] += 1
        if len(bucket["samples"]) < 3 and (row.text or "").strip():
            bucket["samples"].append(str(row.text or "")[:160])

    # -- replies + response time ----------------------------------------
    sent_actions = list(db.scalars(
        select(CommunityAction)
        .where(CommunityAction.workspace_id == workspace_id,
               CommunityAction.state == "sent",
               CommunityAction.action_type.in_(["REPLY", "DRAFT"]),
               CommunityAction.sent_at >= since)
    ).all())
    reply_ids = {a.interaction_id for a in sent_actions}
    eligible = [r for r in rows if r.status != "spam"]
    reply_rate = round(len(reply_ids & {r.id for r in eligible}) / len(eligible), 3) \
        if eligible else 0.0

    deltas: list[float] = []
    interaction_index = {r.id: r for r in rows}
    replies_by_post: dict[str, int] = {}
    for action in sent_actions:
        target = interaction_index.get(action.interaction_id)
        if target is None:
            target = db.get(SocialInteraction, action.interaction_id)
        if target is not None and target.published_post_id:
            replies_by_post[target.published_post_id] = \
                replies_by_post.get(target.published_post_id, 0) + 1
        if target is None or action.sent_at is None:
            continue
        started = target.remote_created_at or target.created_at
        if started is None:
            continue
        seconds = (action.sent_at - started).total_seconds()
        if seconds >= 0:
            deltas.append(float(seconds))

    for bucket in by_content.values():
        bucket["replies_sent"] = replies_by_post.get(bucket["published_post_id"], 0)

    breakdown = sorted(by_content.values(),
                       key=lambda b: (-b["interactions"], b["published_post_id"] or ""))

    return {
        "workspace_id": workspace_id,
        "window_days": days,
        "since": since.isoformat(),
        "interactions": len(rows),
        "comments": len(rows),
        "questions": questions,
        "spam": spam,
        "reply_rate": reply_rate,
        "replies_sent": len(reply_ids),
        "response_time": {
            "avg_seconds": _mean(deltas),
            "median_seconds": _median(deltas),
            "samples": len(deltas),
        },
        "sentiment": sentiment,
        "content_requests": content_requests,
        "lead_signals": lead_signals,
        "moderation": moderation,
        "by_content": breakdown,
    }


__all__ = ["compute_community_metrics"]
