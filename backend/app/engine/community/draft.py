"""Brand-aware reply drafting (Work 09, Lane C).

Every drafted reply consumes the Work 08 ``EffectiveCreativePolicy`` /
BrandDNA: tone, vocabulary, forbidden phrases (hard reject → re-draft once →
block), required disclosures and brand terminology. Brand hard constraints
override learned recommendations; the outcome is recorded on the
``CommunityAction.brand_check_json`` field.

Thread context (current interaction + parent chain + conversation tail +
publication metadata + brand constraints) is assembled through the
``ContextBudgetManager`` so a draft never receives the full account history.
"""

from __future__ import annotations

from typing import Any

from app.engine.community.classify import labels_of, redact_sensitive
from app.models.community import SocialInteraction

DEFAULT_MAX_TOKENS = 1200
MAX_PARENTS = 5
MAX_THREAD_TAIL = 6

_TONE_HINTS = {
    "friendly": "Warm, friendly and conversational.",
    "professional": "Professional, precise and courteous.",
    "casual": "Casual, light and human.",
    "playful": "Playful and energetic without being flippant.",
    "empathetic": "Empathetic and patient, especially for complaints.",
    "authoritative": "Authoritative and factual, never arrogant.",
}


# ---------------------------------------------------------------------------
# brand policy resolution (fail-conservative)
# ---------------------------------------------------------------------------

def resolve_policy(db, workspace_id: str, *, platform: str | None = None,
                   campaign_id: str | None = None,
                   content_id: str | None = None) -> Any:
    """Resolve the Work 08 effective creative policy for a reply subject.

    Any resolution failure degrades to the most conservative in-memory
    policy instead of raising — a draft must never bypass brand checks
    because configuration was incomplete.
    """
    from app.engine.brand import EffectiveCreativePolicy, resolve_effective_policy

    attempts = (
        {"campaign_id": campaign_id, "content_id": content_id, "platform": platform},
        {"platform": platform},
        {},
    )
    last_error = ""
    for kwargs in attempts:
        clean = {k: v for k, v in kwargs.items() if v}
        try:
            return resolve_effective_policy(db, workspace_id, **clean)
        except Exception as exc:  # noqa: BLE001 - degrade, never crash
            last_error = f"{type(exc).__name__}: {exc}"[:200]
    policy = EffectiveCreativePolicy.build({}, {}, platform=platform,
                                           workspace_id=workspace_id)
    policy.effective_config_id = ""
    policy.subject = {"resolution_error": last_error}
    return policy


def forbidden_hits(policy, text: str) -> list[str]:
    """Deterministic forbidden-phrase match (case-insensitive, substring)."""
    body = str(text or "")
    low = body.lower()
    hits = []
    for phrase in list(getattr(policy, "forbidden_phrases", []) or []):
        token = str(phrase).strip()
        if token and token.lower() in low:
            hits.append(token)
    return hits


def missing_disclaimers(policy, text: str) -> list[str]:
    body = str(text or "").lower()
    return [str(d) for d in list(getattr(policy, "required_disclaimers", []) or [])
            if str(d) and str(d).lower() not in body]


def brand_check(policy, text: str, *, attempts: int = 1,
                blocked: bool = False) -> dict:
    """The ``CommunityAction.brand_check_json`` payload."""
    return {
        "status": "blocked" if blocked else "pass",
        "forbidden_hits": forbidden_hits(policy, text),
        "required_disclaimers": list(getattr(policy, "required_disclaimers", []) or []),
        "tone": str(getattr(policy, "tone", "") or ""),
        "effective_config_id": str(getattr(policy, "effective_config_id", "") or ""),
        "missing_disclaimers": missing_disclaimers(policy, text),
        "attempts": attempts,
    }


def apply_disclaimers(policy, text: str) -> str:
    """Append every required disclosure that is not already present."""
    body = str(text or "").strip()
    for disclaimer in missing_disclaimers(policy, body):
        body = f"{body} {disclaimer}".strip()
    return body


# ---------------------------------------------------------------------------
# budgeted thread context
# ---------------------------------------------------------------------------

def _parent_chain(db, interaction, limit: int = MAX_PARENTS) -> list:
    chain: list = []
    seen: set[str] = set()
    current = interaction
    for _ in range(limit):
        parent_id = getattr(current, "parent_interaction_id", None)
        if not parent_id or parent_id in seen:
            break
        seen.add(parent_id)
        parent = db.get(SocialInteraction, parent_id)
        if parent is None or parent.workspace_id != interaction.workspace_id:
            break
        chain.append(parent)
        current = parent
    return chain


def _line(row) -> str:
    who = (row.author_name or row.author_remote_id or "user")[:60]
    return f"{who}: {redact_sensitive((row.text or '')[:400])}"


def build_context(db, workspace_id: str, interaction, *,
                  max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    """Budgeted draft context: thread + publication + brand, never full history."""
    from app.engine.intelligence.context_budget import Category, ContextBudgetManager

    manager = ContextBudgetManager()
    # ContextBudgetManager.budget() consumes an explicit item list (the
    # add() return values), so keep them - budgeting the manager without
    # its items would silently drop this whole context.
    items: list = []

    # pinned: the current comment and the brand hard constraints
    items.append(
        manager.add(
            f"CURRENT COMMENT ({interaction.platform}): {_line(interaction)}\n"
            f"classification: {', '.join(labels_of(interaction)) or 'OTHER'}",
            Category.TASK_REQUIRED,
        )
    )

    for idx, parent in enumerate(_parent_chain(db, interaction), start=1):
        items.append(
            manager.add(f"THREAD PARENT {idx}: {_line(parent)}",
                        Category.RECENT_WORKING)
        )

    if interaction.conversation_id:
        from sqlalchemy import select

        tail = db.scalars(
            select(SocialInteraction)
            .where(SocialInteraction.workspace_id == workspace_id,
                   SocialInteraction.conversation_id == interaction.conversation_id)
            .order_by(SocialInteraction.created_at.asc())
        ).all()
        for row in list(tail)[-MAX_THREAD_TAIL:]:
            if row.id == interaction.id:
                continue
            items.append(
                manager.add(f"THREAD MESSAGE: {_line(row)}",
                            Category.RECENT_WORKING)
            )

    # publication metadata (what the comment is attached to)
    if interaction.published_post_id:
        from app.models import PublishedPost

        post = db.get(PublishedPost, interaction.published_post_id)
        if post is not None and post.workspace_id == workspace_id:
            items.append(
                manager.add(
                    "PUBLISHED POST: "
                    f"{(post.title or 'untitled')[:200]} on {post.platform}",
                    Category.LONG_TERM_MEMORY,
                )
            )

    policy = resolve_policy(
        db, workspace_id,
        platform=interaction.platform,
        campaign_id=getattr(interaction, "campaign_id", None),
    )
    vocabulary = dict(getattr(policy, "vocabulary", {}) or {})
    items.append(
        manager.add(
            "BRAND CONSTRAINTS (never override): "
            f"tone={getattr(policy, 'tone', '')}; "
            f"forbidden={list(getattr(policy, 'forbidden_phrases', []) or [])}; "
            f"required_disclaimers={list(getattr(policy, 'required_disclaimers', []) or [])}; "
            f"preferred_terms={list(vocabulary.get('preferred', []) or [])[:12]}; "
            f"avoid_terms={list(vocabulary.get('avoid', []) or [])[:12]}",
            Category.SYSTEM_CRITICAL,
        )
    )

    budget = manager.budget(items, max_tokens=max_tokens)
    kept_text = "\n".join(str(item.get("content", "")) for item in budget.kept)
    return {
        "kept": budget.kept,
        "references": budget.references,
        "metrics": budget.metrics,
        "max_tokens": max_tokens,
        "prompt_block": kept_text,
        "policy": policy,
    }


# ---------------------------------------------------------------------------
# reply generation
# ---------------------------------------------------------------------------

def _system_prompt(policy, labels: list[str], avoid: list[str] | None = None) -> str:
    tone = str(getattr(policy, "tone", "") or "friendly")
    tone_line = _TONE_HINTS.get(tone.lower(), f"Keep the tone: {tone}.")
    vocabulary = dict(getattr(policy, "vocabulary", {}) or [])
    avoid_terms = list(vocabulary.get("avoid", []) or [])
    parts = [
        "You are the community manager for a creator brand. Write ONE short "
        "reply (max 45 words) to a single audience comment. Answer the "
        "comment directly, stay on-brand, and never invent facts, prices, "
        "refunds, legal positions or guarantees.",
        tone_line,
        "Never use these forbidden phrases: "
        + (", ".join(getattr(policy, "forbidden_phrases", []) or []) or "none"),
    ]
    if avoid_terms:
        parts.append("Avoid these words: " + ", ".join(str(w) for w in avoid_terms[:15]))
    preferred = list(vocabulary.get("preferred", []) or [])
    if preferred:
        parts.append("Prefer this terminology: " + ", ".join(str(w) for w in preferred[:15]))
    if avoid:
        parts.append("Your previous draft used forbidden phrases; do NOT repeat: "
                     + ", ".join(avoid))
    if labels:
        parts.append("Classification to address: " + ", ".join(labels))
    return "\n".join(parts)


def _llm_reply(system: str, user: str, workspace_id: str) -> str:
    """Single LLM call seam (tests may replace it). Empty on any failure."""
    from app.providers import llm

    try:
        result = llm.complete(
            system, user, workspace_id=workspace_id, tier="cheap",
            temperature=0.4, max_tokens=400,
        )
    except Exception:  # noqa: BLE001 - drafting degrades to the template
        return ""
    return str(result.text or "").strip()


def _fallback_reply(interaction, labels: list[str], policy) -> str:
    """Deterministic on-brand template when no LLM is configured."""
    text = (interaction.text or "").strip()
    question = "QUESTION" in labels
    if question:
        body = ("Great question — thanks for asking. Here is the short version: "
                "the steps in the video cover it, and I will follow up with more "
                "detail in a follow-up post.")
    elif "NEGATIVE" in labels or "SUPPORT" in labels:
        body = ("Sorry to hear that, and thanks for flagging it. A human on the "
                "team is picking this up so we can get it sorted properly.")
    elif "LEAD" in labels:
        body = ("Thanks for your interest! Pricing and plan details live on our "
                "site, and the team can walk you through the rest.")
    else:
        body = ("Thanks for watching and for taking the time to comment — "
                "glad it resonated.")
    tone = str(getattr(policy, "tone", "") or "")
    if tone:
        body += f" (tone: {tone})"
    return f"{body} — {redact_sensitive(text[:80])}".strip()


def generate_reply(db, workspace_id: str, interaction, *, labels: list[str] | None = None,
                   context: dict | None = None, max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    """Draft a brand-checked reply.

    Forbidden phrases hard-reject: re-draft once with the hits called out,
    then block (``blocked=True``) — the caller must never send it.
    """
    policy = (context or {}).get("policy") if context else None
    context = context or build_context(db, workspace_id, interaction,
                                       max_tokens=max_tokens)
    policy = policy or context.get("policy") or resolve_policy(
        db, workspace_id, platform=interaction.platform,
        campaign_id=getattr(interaction, "campaign_id", None))

    label_values = labels if labels is not None else labels_of(interaction)
    user = (
        "Comment to reply to:\n"
        f"{redact_sensitive((interaction.text or '')[:1200])}\n\n"
        "Thread/brand context:\n"
        f"{context['prompt_block'][:4000]}"
    )

    text = ""
    hits: list[str] = []
    attempts: list[dict] = []
    for attempt in (1, 2):
        system = _system_prompt(policy, label_values,
                                avoid=hits if attempt > 1 else None)
        candidate = _llm_reply(system, user, workspace_id).strip()
        if not candidate:
            candidate = _fallback_reply(interaction, label_values, policy)
        hits = forbidden_hits(policy, candidate)
        attempts.append({
            "attempt": attempt,
            "forbidden_hits": hits,
            "preview": candidate[:160],
        })
        if not hits:
            text = candidate
            break

    blocked = not bool(text)
    if text:
        text = apply_disclaimers(policy, text)
        if forbidden_hits(policy, text):  # a disclosure must not re-introduce one
            text = ""
            blocked = True

    check = brand_check(policy, text, attempts=len(attempts), blocked=blocked)
    return {
        "text": text,
        "blocked": blocked,
        "reason": "forbidden_phrase" if blocked else "",
        "labels": label_values,
        "brand_check": check,
        "attempts": attempts,
        "context": {
            "kept": context["kept"],
            "references": context["references"],
            "metrics": context["metrics"],
            "max_tokens": context["max_tokens"],
        },
        "tone": check["tone"],
        "effective_config_id": check["effective_config_id"],
    }


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "apply_disclaimers",
    "brand_check",
    "build_context",
    "forbidden_hits",
    "generate_reply",
    "missing_disclaimers",
    "resolve_policy",
]
