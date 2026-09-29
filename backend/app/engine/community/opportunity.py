"""Lead & opportunity detection (Work 09, Lane C).

Turns classified interactions into ``CommunityOpportunity`` rows
(type ∈ ``OPPORTUNITY_TYPES``) with ``evidence_count`` + a bounded
``source_interaction_ids`` list. Single-source signals stay ``confidence=low``.

Evidence is quoted from the interaction only — contact details are redacted
and no commercial intent or contact data is ever invented.
"""

from __future__ import annotations

import re

from app.engine.community.classify import labels_of, redact_sensitive
from app.engine.community.insight import normalize_topic
from app.models.community import OPPORTUNITY_TYPES

SPONSOR_RX = re.compile(r"\b(sponsor|sponsorship|brand deal|advertise|ad budget)\b",
                        re.IGNORECASE)
_PRODUCT_RX = re.compile(
    r"\b(price|pricing|cost|plan|plans|feature|features|integrate|integration|"
    r"api|works with|compatible|available on|does it|do you support|roadmap)\b",
    re.IGNORECASE)
_PURCHASE_RX = re.compile(
    r"\b(buy|purchase|sign up|subscribe|checkout|how much|pricing|price|"
    r"upgrade|get started|order|trial)\b", re.IGNORECASE)

REPEAT_THRESHOLD = 3
_MAX_EVIDENCE_IDS = 25


def _confidence_for(count: int) -> str:
    if count < REPEAT_THRESHOLD:
        return "low"
    if count < 6:
        return "medium"
    return "high"


def _decide_type(text: str, labels: list[str]) -> str | None:
    """Map one interaction onto OPPORTUNITY_TYPES (None = no opportunity)."""
    low = str(text or "").lower()
    have = set(labels)
    if "COLLABORATION" in have:
        return "sponsorship" if SPONSOR_RX.search(low) else "partnership"
    if "CONTENT_REQUEST" in have:
        return "content_request"
    if "QUESTION" in have and _PRODUCT_RX.search(low):
        return "product_question"
    if "LEAD" in have and _PURCHASE_RX.search(low):
        return "purchase_intent"
    if "LEAD" in have:
        return "purchase_intent"
    return None


def _find_existing(db, workspace_id: str, opportunity_type: str, title: str):
    from sqlalchemy import select

    from app.models.community import CommunityOpportunity

    return db.scalar(
        select(CommunityOpportunity).where(
            CommunityOpportunity.workspace_id == workspace_id,
            CommunityOpportunity.opportunity_type == opportunity_type,
            CommunityOpportunity.state == "open",
            CommunityOpportunity.title == title,
        )
    )


def detect_for_interaction(db, workspace_id: str, interaction, *,
                           labels: list[str] | None = None) -> dict | None:
    """Create or grow the opportunity for one interaction. Idempotent."""
    from app.models.community import CommunityOpportunity

    if interaction.workspace_id != workspace_id:
        return None
    label_values = labels if labels is not None else labels_of(interaction)
    text = str(interaction.text or "")
    opportunity_type = _decide_type(text, label_values)
    if opportunity_type is None or opportunity_type not in OPPORTUNITY_TYPES:
        return None

    _key, topic = normalize_topic(text)
    title = (topic or text.strip()[:80] or opportunity_type)[:300]
    evidence_text = redact_sensitive(" ".join(text.split()))[:400]

    row = _find_existing(db, workspace_id, opportunity_type, title)
    if row is None:
        row = CommunityOpportunity(
            workspace_id=workspace_id,
            opportunity_type=opportunity_type,
            title=title,
            detail=evidence_text,
            evidence_count=1,
            source_interaction_ids=[interaction.id],
            confidence="low",
        )
        db.add(row)
    else:
        ids = list(row.source_interaction_ids or [])
        if interaction.id not in ids:
            ids.append(interaction.id)
        if len(ids) > _MAX_EVIDENCE_IDS:
            ids = ids[-_MAX_EVIDENCE_IDS:]
        row.source_interaction_ids = ids
        row.evidence_count = len(ids)
        if not row.detail:
            row.detail = evidence_text
    row.confidence = _confidence_for(int(row.evidence_count or 1))
    db.flush()
    return row


def detect_repeated_requests(db, workspace_id: str, *, limit: int = 200) -> list[dict]:
    """Aggregate repeated audience requests across interactions (≥ threshold)."""
    from sqlalchemy import select

    from app.models.community import CommunityInsight, CommunityOpportunity

    insights = db.scalars(
        select(CommunityInsight).where(
            CommunityInsight.workspace_id == workspace_id,
            CommunityInsight.evidence_count >= REPEAT_THRESHOLD,
        )
    ).all()
    created: list[dict] = []
    for insight in insights:
        existing = db.scalar(
            select(CommunityOpportunity).where(
                CommunityOpportunity.workspace_id == workspace_id,
                CommunityOpportunity.opportunity_type == "repeated_request",
                CommunityOpportunity.state == "open",
                CommunityOpportunity.title == insight.topic[:300],
            )
        )
        ids = list(insight.source_interaction_ids or [])[:_MAX_EVIDENCE_IDS]
        if existing is not None:
            merged = list(dict.fromkeys(list(existing.source_interaction_ids or []) + ids))
            existing.source_interaction_ids = merged
            existing.evidence_count = len(merged)
            existing.confidence = _confidence_for(len(merged))
            continue
        row = CommunityOpportunity(
            workspace_id=workspace_id,
            opportunity_type="repeated_request",
            title=(insight.topic or "repeated request")[:300],
            detail=redact_sensitive(str(insight.representative_text or ""))[:400],
            evidence_count=len(ids) or int(insight.evidence_count or 0),
            source_interaction_ids=ids,
            confidence=_confidence_for(int(insight.evidence_count or 0)),
        )
        db.add(row)
        db.flush()
        created.append({"id": row.id, "title": row.title,
                        "evidence_count": row.evidence_count})
    return created


def detect_opportunities(db, workspace_id: str, *, limit: int = 200) -> list[dict]:
    """Run detection over recent classified interactions, then repeats."""
    from sqlalchemy import select

    from app.models.community import SocialInteraction

    rows = db.scalars(
        select(SocialInteraction)
        .where(SocialInteraction.workspace_id == workspace_id,
               SocialInteraction.status.in_(["classified", "drafted", "replied",
                                              "escalated", "read"]))
        .order_by(SocialInteraction.created_at.desc())
        .limit(max(1, min(limit, 500)))
    ).all()
    found: list[dict] = []
    for row in rows:
        opp = detect_for_interaction(db, workspace_id, row)
        if opp is not None:
            found.append({"id": opp.id, "type": opp.opportunity_type,
                          "title": opp.title,
                          "evidence_count": opp.evidence_count,
                          "confidence": opp.confidence})
    detect_repeated_requests(db, workspace_id)
    db.flush()
    return found


def convert_idea(db, workspace_id: str, opportunity_id: str, *,
                 title: str = "") -> dict:
    """Attach a content idea: link the content-loop ``Opportunity`` row.

    Never invents contact details or commercial terms — the idea carries the
    audience evidence verbatim (redacted) and nothing more.
    """
    from app.models import Opportunity
    from app.models.community import CommunityOpportunity

    row = db.get(CommunityOpportunity, opportunity_id)
    if row is None or row.workspace_id != workspace_id:
        return {"found": False, "opportunity_id": opportunity_id}

    idea_title = (title or row.title or row.opportunity_type)[:400]
    payload = {
        "evidence": str(row.detail or "")[:400],
        "interaction_ids": list(row.source_interaction_ids or []),
        "evidence_count": int(row.evidence_count or 1),
        "confidence": row.confidence,
        "opportunity_type": row.opportunity_type,
        "source": "community",
    }
    opportunity = Opportunity(
        workspace_id=workspace_id,
        topic=idea_title,
        source="community",
        external_ref=f"community:{row.id}",
        raw_payload=payload,
        score=0.0,
        recommendation="WAIT",
    )
    db.add(opportunity)
    db.flush()

    row.converted_idea_json = {
        "title": idea_title,
        "topic": idea_title,
        "interaction_ids": payload["interaction_ids"],
        "opportunity_type": row.opportunity_type,
        "converted_opportunity_id": opportunity.id,
        "note": "no contact details or commercial terms inferred",
    }
    row.converted_opportunity_id = opportunity.id
    row.state = "converted"
    db.flush()
    return {
        "found": True,
        "opportunity_id": row.id,
        "state": row.state,
        "idea": dict(row.converted_idea_json),
        "converted_opportunity_id": opportunity.id,
    }


__all__ = [
    "REPEAT_THRESHOLD",
    "convert_idea",
    "detect_for_interaction",
    "detect_opportunities",
    "detect_repeated_requests",
]
