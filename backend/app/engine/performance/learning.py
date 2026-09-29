"""Scoped performance lessons + creation influence (Work 06 Lane C).

A lesson is a ``LearningPattern`` row extended through ``evidence_json``::

    {lesson: True, metric, scope{}, effect{}, evidence_ids[],
     created_at, last_validated_at, status, generation, ...}

No schema change is needed: ``LearningPattern`` already carries
``evidence_json``. Persistence goes exclusively through
``LearningAgent._upsert_pattern`` / ``_remember_pattern`` — there is no
second pattern store.

Scope dimensions (workspace is implicit in the row): platform,
content_format, topic, audience, language, duration_bucket. A lesson
matches a query scope only when every scope dimension the lesson sets is
also set — and equal — on the query. Cross-platform / cross-topic
generalization is forbidden by construction: a tiktok-scoped lesson can
never match a youtube query.

Evidence bar: ``sample_size >= 5`` AND ``>= 2`` distinct evidence ids,
otherwise the observation is rejected (one video never becomes a rule).

DecisionEngine use (hook categorization, scene-purpose classification,
semantic similarity, lesson relevance ranking) is SHADOW-only: advisory
outputs are logged, deterministic results are never altered.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import select

KINDS = ("strategy", "script", "hooks", "broll", "derive", "schedule")

SCOPE_DIMS = (
    "platform",
    "content_format",
    "topic",
    "audience",
    "language",
    "duration_bucket",
)

STATUSES = ("fresh", "aging", "stale", "superseded", "disabled")

MIN_SAMPLE_SIZE = 5
MIN_EVIDENCE_IDS = 2

FRESH_DAYS = 30
STALE_DAYS = 90

HOOK_LABELS = ("question", "bold_claim", "story", "statistic", "curiosity_gap", "command")
SCENE_PURPOSES = ("establish", "demonstrate", "payoff", "cta")

# Artifact keys lessons must never alter (QC / budgets / approvals stay authoritative).
# Work 08 Lane C: brand HARD constraints are added so a learned recommendation
# can never rewrite them either — precedence is
# security/compliance -> explicit user override -> brand hard -> campaign ->
# learned -> defaults.
PROTECTED_KEYS = frozenset({
    "min_qc_score", "max_videos_per_day", "approval", "approved",
    "status", "budget_usd", "cost_limit_usd", "qc_threshold",
    "duration_seconds",
    "forbidden_phrases", "required_disclaimers", "approved_voices",
    "approved_avatars", "logo_safe_zone", "caption_style",
})


def _now() -> datetime:
    from app.models.base import utcnow

    return utcnow()


def _parse_dt(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00").replace("+00:00", ""))
        except ValueError:
            return None
    return None


def learning_assist_enabled(workspace_or_settings: Any) -> bool:
    """Workspace opt-in gate: ``settings_json["learning_assist"]``, default False."""
    try:
        if workspace_or_settings is None:
            return False
        if isinstance(workspace_or_settings, dict):
            settings_json = workspace_or_settings
        else:
            settings_json = getattr(workspace_or_settings, "settings_json", None) or {}
        return bool(settings_json.get("learning_assist", False))
    except Exception:
        return False


def _assist_enabled_for(workspace_id: str) -> bool:
    try:
        from app.db import session_scope
        from app.models import Workspace

        with session_scope() as s:
            row = s.get(Workspace, workspace_id)
            settings_json = dict(getattr(row, "settings_json", None) or {})
        return learning_assist_enabled(settings_json)
    except Exception:
        return False


def normalize_scope(scope: dict | None) -> dict:
    """Lowercased, stripped scope with every dimension present ("" = unset)."""
    scope = scope or {}
    return {dim: str(scope.get(dim) or "").strip().lower() for dim in SCOPE_DIMS}


def normalize_effect(effect: Any) -> dict:
    """Accept a bare improvement-pct float or a full effect dict."""
    if isinstance(effect, (int, float)):
        pct = float(effect)
        return {
            "direction": "positive" if pct >= 0 else "negative",
            "improvement_pct": round(pct, 2),
            "recommendation": "",
            "kinds": [],
        }
    eff = dict(effect or {})
    pct = eff.get("improvement_pct", 0.0)
    try:
        pct = round(float(pct), 2)
    except (TypeError, ValueError):
        pct = 0.0
    direction = str(eff.get("direction") or ("positive" if pct >= 0 else "negative")).lower()
    if direction not in ("positive", "negative"):
        direction = "positive" if pct >= 0 else "negative"
    kinds = [k for k in (eff.get("kinds") or []) if k in KINDS]
    return {
        "direction": direction,
        "improvement_pct": pct,
        "recommendation": str(eff.get("recommendation") or "")[:500],
        "kinds": kinds,
        "preferred_hours": [int(h) for h in (eff.get("preferred_hours") or []) if str(h).isdigit()][:8],
        "preferred_queries": [str(q)[:120] for q in (eff.get("preferred_queries") or []) if str(q).strip()][:6],
    }


def confidence_for(sample_size: int, consistency: float | None = None) -> str:
    """low / medium / high from sample size, downgraded by inconsistency.

    ``moderate`` is accepted as an alias of ``medium`` (stored vocabulary is
    low|medium|high to stay compatible with the existing LearningAgent /
    HookOptimizer confidence checks).
    """
    if sample_size >= 30:
        level = "high"
    elif sample_size >= 10:
        level = "medium"
    else:
        level = "low"
    try:
        consistency_f = float(consistency) if consistency is not None else 1.0
    except (TypeError, ValueError):
        consistency_f = 1.0
    if consistency_f < 0.5:
        level = {"high": "medium", "medium": "low", "low": "low"}[level]
    return level


def _weaken(level: str) -> str:
    return {"high": "medium", "medium": "low", "low": "low"}.get(level, "low")


def _slug(text: str, limit: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return slug[:limit]


def lesson_key(metric: str, scope: dict) -> str:
    """Deterministic, scope-bound pattern key (fits the 120-char column)."""
    norm = normalize_scope(scope)
    digest = hashlib.sha256(
        "|".join([str(metric).strip().lower(), *[norm[d] for d in SCOPE_DIMS]]).encode(),
    ).hexdigest()[:8]
    key = ":".join([
        "lesson",
        _slug(str(metric), 20) or "metric",
        _slug(norm["platform"], 12) or "-",
        _slug(norm["topic"], 16) or "-",
        digest,
    ])
    return key[:120]


def is_lesson_row(row) -> bool:
    try:
        return bool((row.evidence_json or {}).get("lesson", False))
    except Exception:
        return False


def effective_status(row) -> str:
    """Freshness from stored status + age. Never mutates the row."""
    ev = dict(getattr(row, "evidence_json", None) or {})
    if not getattr(row, "active", True):
        return "disabled"
    stored = str(ev.get("status") or "fresh")
    if stored == "superseded":
        return "superseded"
    last = _parse_dt(ev.get("last_validated_at")) or _parse_dt(ev.get("created_at"))
    if last is None:
        return stored if stored in STATUSES else "fresh"
    age_days = (_now() - last.replace(tzinfo=None)).days
    if age_days > STALE_DAYS:
        return "stale"
    if age_days > FRESH_DAYS:
        return "aging" if stored == "fresh" else stored
    return stored


def scope_matches(lesson_scope: dict, query_scope: dict) -> bool:
    """True only when every lesson-set dimension equals the query's.

    Empty lesson dimensions are wildcards (a broadly-scoped lesson applies
    within its stated dims); a lesson-set dimension that the query leaves
    unset — or sets differently — never matches.
    """
    lesson_scope = normalize_scope(lesson_scope)
    query_scope = normalize_scope(query_scope)
    for dim in SCOPE_DIMS:
        lesson_val = lesson_scope.get(dim, "")
        if not lesson_val:
            continue
        query_val = query_scope.get(dim, "")
        if not query_val or query_val != lesson_val:
            return False
    return True


def _finding_for(pattern_key: str, description: str, effect: dict,
                 sample_size: int, confidence: str, evidence: dict) -> dict:
    return {
        "pattern_key": pattern_key,
        "description": description,
        "observed_improvement_pct": effect["improvement_pct"],
        "confidence": confidence,
        "sample_size": sample_size,
        "evidence": evidence,
    }


def _persist_finding(workspace_id: str, finding: dict) -> None:
    """Single write path: existing LearningAgent upsert + memory mirror."""
    from app.engine.agents.intelligence import LearningAgent

    agent = LearningAgent()
    agent._upsert_pattern(workspace_id, dict(finding))
    with contextlib.suppress(Exception):
        agent._remember_pattern(workspace_id, dict(finding))


def generate_lessons(session, workspace_id: str, observations: list[dict]) -> dict:
    """Turn evidence-backed observations into scoped lessons.

    Each observation: {metric, scope, effect, evidence_ids, sample_size,
    description?, consistency?}. Requires sample_size >= 5 and >= 2
    distinct evidence ids; anything weaker is rejected, never stored.
    Same-scope agreement revalidates; contradiction weakens (smaller
    sample) or supersedes (larger sample, new generation row — the old
    row is preserved, never deleted).
    """
    from app.models import LearningPattern

    # Writes go through a SEPARATE session (see _persist_finding). An
    # uncommitted write transaction on the caller's session would hold the
    # SQLite write lock and make that separate connection fail with
    # "database is locked" — release it first (stage-commit pattern).
    with contextlib.suppress(Exception):
        session.commit()
    stored, rejected, superseded, weakened, revalidated = 0, [], 0, 0, 0
    for obs in observations or []:
        # writes go through a separate session; re-read fresh
        with contextlib.suppress(Exception):
            session.expire_all()
        obs = dict(obs or {})
        metric = str(obs.get("metric") or "").strip().lower()
        scope = normalize_scope(obs.get("scope"))
        effect = normalize_effect(obs.get("effect"))
        evidence_ids = sorted({str(e).strip() for e in (obs.get("evidence_ids") or []) if str(e).strip()})
        try:
            sample_size = int(obs.get("sample_size") or 0)
        except (TypeError, ValueError):
            sample_size = 0
        if not metric:
            rejected.append({"observation": obs, "reason": "metric is required"})
            continue
        if sample_size < MIN_SAMPLE_SIZE:
            rejected.append({"observation": {"metric": metric, "sample_size": sample_size},
                             "reason": f"sample_size {sample_size} < {MIN_SAMPLE_SIZE}"})
            continue
        if len(evidence_ids) < MIN_EVIDENCE_IDS:
            rejected.append({"observation": {"metric": metric, "evidence_ids": evidence_ids},
                             "reason": f"only {len(evidence_ids)} distinct evidence id(s), need {MIN_EVIDENCE_IDS}"})
            continue
        key = lesson_key(metric, scope)
        confidence = confidence_for(sample_size, obs.get("consistency"))
        description = str(obs.get("description") or effect.get("recommendation") or
                          f"{metric} lesson ({effect['direction']} {effect['improvement_pct']}%)")[:400]
        existing = session.scalar(
            select(LearningPattern).where(
                LearningPattern.workspace_id == workspace_id,
                LearningPattern.pattern_key == key,
            )
        )
        now_iso = _now().isoformat() + "Z"
        if existing is not None and is_lesson_row(existing):
            outcome = _resolve_conflict(
                session, workspace_id, existing, metric, scope, effect,
                evidence_ids, sample_size, confidence, description, now_iso,
            )
            if outcome == "superseded":
                superseded += 1
            elif outcome == "weakened":
                weakened += 1
            else:
                revalidated += 1
            stored += 1
            continue
        evidence = {
            "lesson": True,
            "metric": metric,
            "scope": scope,
            "effect": effect,
            "evidence_ids": evidence_ids,
            "created_at": now_iso,
            "last_validated_at": now_iso,
            "status": "fresh",
            "generation": 1,
            "shadow": _shadow_lesson_notes(effect, scope),
        }
        _persist_finding(workspace_id, _finding_for(
            key, description, effect, sample_size, confidence, evidence))
        stored += 1
    return {
        "stored": stored,
        "rejected": rejected,
        "superseded": superseded,
        "weakened": weakened,
        "revalidated": revalidated,
    }


def _resolve_conflict(session, workspace_id: str, existing, metric: str, scope: dict,
                      effect: dict, evidence_ids: list[str], sample_size: int,
                      confidence: str, description: str, now_iso: str) -> str:
    """Agreement revalidates; contradiction weakens or supersedes. Returns outcome."""
    from app.models import LearningPattern

    old_ev = dict(existing.evidence_json or {})
    old_effect = dict(old_ev.get("effect") or {})
    old_ids = list(old_ev.get("evidence_ids") or [])
    merged_ids = sorted(set(old_ids) | set(evidence_ids))
    old_dir = str(old_effect.get("direction") or "positive")
    if effect["direction"] == old_dir:
        evidence = {
            **old_ev,
            "effect": effect,
            "evidence_ids": merged_ids,
            "last_validated_at": now_iso,
            "status": "fresh",
        }
        _persist_finding(workspace_id, _finding_for(
            existing.pattern_key, description, effect,
            max(sample_size, existing.sample_size), confidence, evidence))
        return "revalidated"
    # Contradiction: bigger new sample supersedes, smaller one weakens.
    if sample_size >= max(existing.sample_size, 1):
        generation = int(old_ev.get("generation") or 1) + 1
        new_key = f"{existing.pattern_key}:g{generation}"[:120]
        new_evidence = {
            "lesson": True,
            "metric": metric,
            "scope": scope,
            "effect": effect,
            "evidence_ids": merged_ids,
            "created_at": now_iso,
            "last_validated_at": now_iso,
            "status": "fresh",
            "generation": generation,
            "supersedes": existing.pattern_key,
            "shadow": _shadow_lesson_notes(effect, scope),
        }
        _persist_finding(workspace_id, _finding_for(
            new_key, description, effect, sample_size, confidence, new_evidence))
        # Old row is preserved (never deleted): flagged superseded + inactive.
        try:
            with _write_session() as w:
                row = w.scalar(
                    select(LearningPattern).where(
                        LearningPattern.workspace_id == workspace_id,
                        LearningPattern.pattern_key == existing.pattern_key,
                    )
                )
                if row is not None:
                    ev = dict(row.evidence_json or {})
                    ev["status"] = "superseded"
                    ev["superseded_by"] = new_key
                    row.evidence_json = ev
                    row.active = False
                    w.flush()
        except Exception as exc:
            logger.debug(f"[lessons] supersede flag failed: {type(exc).__name__}")
        return "superseded"
    conflicts = list(old_ev.get("conflicts") or [])
    conflicts.append({"direction": effect["direction"], "sample_size": sample_size,
                      "at": now_iso})
    evidence = {
        **old_ev,
        "evidence_ids": merged_ids,
        "last_validated_at": now_iso,
        "status": "aging",
        "conflicts": conflicts[-5:],
    }
    weakened_conf = _weaken(str(existing.confidence or "low"))
    aged_effect = {**old_effect, "improvement_pct": old_effect.get("improvement_pct", 0.0)}
    _persist_finding(workspace_id, _finding_for(
        existing.pattern_key, existing.description or description,
        normalize_effect(aged_effect),
        max(existing.sample_size, sample_size), weakened_conf, evidence))
    return "weakened"


def _write_session():
    from app.db import session_scope

    return session_scope()


def matching_lessons(session, workspace_id: str, scope: dict,
                     kinds: str | None = None) -> list:
    """Fresh/aging lesson rows whose scope matches (exact per set dims)."""
    from app.models import LearningPattern

    query_scope = normalize_scope(scope)
    rows = session.scalars(
        select(LearningPattern).where(
            LearningPattern.workspace_id == workspace_id,
            LearningPattern.active.is_(True),
        )
    ).all()
    out = []
    for row in rows:
        if not is_lesson_row(row):
            continue
        ev = dict(row.evidence_json or {})
        if effective_status(row) not in ("fresh", "aging"):
            continue
        if not scope_matches(dict(ev.get("scope") or {}), query_scope):
            continue
        if kinds:
            lesson_kinds = ((ev.get("effect") or {}).get("kinds")) or []
            if lesson_kinds and kinds not in lesson_kinds:
                continue
        out.append(row)
    # Deterministic order: fresh first, then confidence, then sample size.
    rank = {"high": 0, "medium": 1, "moderate": 1, "low": 2}
    out.sort(key=lambda r: (
        0 if effective_status(r) == "fresh" else 1,
        rank.get(str(r.confidence or "low"), 2),
        -(r.sample_size or 0),
        r.pattern_key,
    ))
    return out


def apply_lessons(session, workspace_id: str, scope: dict,
                  artifact: dict, kind: str) -> dict:
    """Inject matching lessons into a COPY of the artifact (auditable).

    Adds ``lesson_recommendations`` + ``applied_lessons: [pattern_keys]``.
    Protected keys (QC thresholds, budgets, approvals, durations) are never
    altered. Returns a plain copy without lesson keys when the workspace
    has not enabled ``learning_assist``. Raises ValueError on unknown kind.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown lesson kind '{kind}' (expected one of {KINDS})")
    artifact = dict(artifact or {})
    if not _assist_enabled_for(workspace_id):
        return dict(artifact)
    try:
        lessons = matching_lessons(session, workspace_id, scope, kinds=kind)
    except Exception as exc:
        logger.debug(f"[lessons] retrieval failed: {type(exc).__name__}")
        return dict(artifact)
    out = dict(artifact)
    if not lessons:
        out["applied_lessons"] = []
        return out
    # Brand hard constraints outrank learned lessons (Work 08 Lane C): any
    # recommendation that conflicts with the effective brand policy is
    # dropped and audited as `brand_blocked_lessons`
    # (``[{pattern_key, reason}]``) — never applied, never fatal.
    blocked: list[dict] = []
    gate = None
    try:
        from app.engine.brand_templates import brand_gate, lesson_conflicts_with_brand

        gate = brand_gate(session, workspace_id)
        if gate and gate.get("brand_available"):
            kept = []
            for row in lessons:
                ev = dict(row.evidence_json or {})
                effect = dict(ev.get("effect") or {})
                recommendation = str(
                    effect.get("recommendation") or row.description or "")
                reason = lesson_conflicts_with_brand(
                    gate, kind, recommendation,
                    {**effect, "pattern_key": row.pattern_key})
                if reason:
                    blocked.append({"pattern_key": row.pattern_key,
                                    "reason": str(reason)[:300]})
                    logger.info(
                        f"[lessons] brand_blocked_lesson {row.pattern_key}: {reason}")
                else:
                    kept.append(row)
            lessons = kept
    except Exception as exc:  # noqa: BLE001 — brand must never break learning
        logger.debug(f"[lessons] brand filter skipped: {type(exc).__name__}")
        gate = None
        blocked = []
    if gate and gate.get("applied_brand"):
        out["applied_brand"] = True
    if not lessons:
        out["applied_lessons"] = []
        if blocked:
            out["brand_blocked_lessons"] = blocked
        return out
    advisory_order = _shadow_rank_lessons(workspace_id, kind, artifact, lessons)
    if advisory_order is not None and advisory_order != [r.pattern_key for r in lessons]:
        logger.info(
            "[lessons] shadow relevance ranking disagrees with deterministic order "
            f"(kind={kind}, deterministic={[r.pattern_key for r in lessons]}, "
            f"advisory={advisory_order})"
        )
    recs = []
    for row in lessons:
        ev = dict(row.evidence_json or {})
        effect = dict(ev.get("effect") or {})
        recs.append({
            "pattern_key": row.pattern_key,
            "metric": ev.get("metric", ""),
            "recommendation": effect.get("recommendation") or row.description,
            "direction": effect.get("direction", ""),
            "improvement_pct": effect.get("improvement_pct", 0.0),
            "confidence": row.confidence,
            "freshness": effective_status(row),
        })
    out["lesson_recommendations"] = recs
    out["applied_lessons"] = [r.pattern_key for r in lessons]
    if blocked:
        out["brand_blocked_lessons"] = blocked
    if kind == "hooks":
        bonus = round(min(sum(
            float((dict(r.evidence_json or {}).get("effect") or {}).get("improvement_pct", 0.0))
            for r in lessons) / 10.0, 5.0), 1)
        if bonus:
            out["lesson_prior_bonus"] = bonus
    if kind == "schedule":
        hours: list[int] = []
        for row in lessons:
            effect = dict((row.evidence_json or {}).get("effect") or {})
            for h in effect.get("preferred_hours") or []:
                try:
                    h_int = int(h)
                except (TypeError, ValueError):
                    continue
                if h_int not in hours:
                    hours.append(h_int)
        if hours:
            out["lesson_preferred_hours"] = hours[:8]
    if kind == "broll":
        queries: list[str] = []
        for row in lessons:
            effect = dict((row.evidence_json or {}).get("effect") or {})
            queries.extend([str(q) for q in (effect.get("preferred_queries") or []) if str(q).strip()])
        if queries:
            out["lesson_queries"] = queries[:6]
    for key in PROTECTED_KEYS:
        if key in artifact:
            out[key] = artifact[key]
    return out


# -- thin call-site helpers (keep agent diffs to a few lines) -----------------

def strategy_scope(topic: str, strategy: dict, workspace_language: str = "") -> dict:
    platforms = strategy.get("platforms") or []
    try:
        duration = int(strategy.get("duration_seconds") or 0)
    except (TypeError, ValueError):
        duration = 0
    return {
        "platform": (platforms[0] if platforms else ""),
        "content_format": "",
        "topic": topic or "",
        "audience": "",
        "language": workspace_language,
        "duration_bucket": duration_bucket(duration),
    }


def duration_bucket(duration_seconds: int) -> str:
    try:
        duration = int(duration_seconds or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration <= 0:
        return ""
    if duration <= 25:
        return "0-25s"
    if duration <= 45:
        return "25-45s"
    return "45-90s"


def maybe_apply_strategy_lessons(workspace_id: str, topic: str, strategy: dict) -> dict:
    """StrategistAgent hook: recommendations annotation, guards untouched."""
    if not _assist_enabled_for(workspace_id):
        return strategy
    try:
        from app.db import session_scope
        from app.models import Workspace

        with session_scope() as s:
            row = s.get(Workspace, workspace_id)
            language = str(getattr(row, "language", "") or "") if row else ""
            out = apply_lessons(s, workspace_id, strategy_scope(topic, strategy, language),
                                dict(strategy), "strategy")
        for key in PROTECTED_KEYS:
            if key in strategy:
                out[key] = strategy[key]
        return out
    except Exception as exc:
        logger.debug(f"[lessons] strategy influence skipped: {type(exc).__name__}")
        return strategy


def script_guidance(workspace_id: str, topic: str, strategy: dict) -> tuple[dict, list[str]]:
    """ScriptAgent hook: lesson guidance folded into a strategy copy.

    Returns (strategy_copy, applied_keys). When disabled, the copy is
    identical to the input and no keys are applied.
    """
    if not _assist_enabled_for(workspace_id):
        return dict(strategy), []
    try:
        from app.db import session_scope

        with session_scope() as s:
            applied = apply_lessons(s, workspace_id, strategy_scope(topic, strategy),
                                    dict(strategy), "script")
        keys = list(applied.get("applied_lessons") or [])
        recs = applied.get("lesson_recommendations") or []
        if recs:
            guidance = "\n".join(f"- {r['recommendation']}" for r in recs if r.get("recommendation"))
            if guidance:
                base = str(applied.get("custom_system_prompt") or "")
                applied["custom_system_prompt"] = (
                    f"{base}\nPerformance lessons for this scope (advisory, keep QC bar):\n{guidance}"
                ).strip()
        for key in PROTECTED_KEYS:
            if key in strategy:
                applied[key] = strategy[key]
        return applied, keys
    except Exception as exc:
        logger.debug(f"[lessons] script influence skipped: {type(exc).__name__}")
        return dict(strategy), []


def apply_hook_lesson_bonus(workspace_id: str, scope: dict, variants: list[dict]) -> list[dict]:
    """HookOptimizer hook: deterministic bonus + audit tags. Unchanged when off."""
    if not _assist_enabled_for(workspace_id):
        return variants
    try:
        from app.db import session_scope

        with session_scope() as s:
            probe = apply_lessons(s, workspace_id, scope, {"scope": dict(scope or {})}, "hooks")
        bonus = float(probe.get("lesson_prior_bonus") or 0.0)
        keys = list(probe.get("applied_lessons") or [])
        _shadow_categorize_hooks(workspace_id, variants)
        if not keys or not bonus:
            return variants
        out = []
        for v in variants:
            copy = dict(v)
            with contextlib.suppress(TypeError, ValueError):
                copy["predicted_score"] = round(min(100.0, float(copy.get("predicted_score") or 0) + bonus), 1)
            copy["applied_lessons"] = keys
            out.append(copy)
        out.sort(key=lambda v: -(v.get("predicted_score") or 0))
        return out
    except Exception as exc:
        logger.debug(f"[lessons] hook influence skipped: {type(exc).__name__}")
        return variants


def broll_keyword_boost(workspace_id: str, topic: str,
                        keywords: list[str]) -> tuple[list[str], list[str], list[dict]]:
    """B-roll hook: lesson-suggested queries merged into a keyword copy."""
    keywords = list(keywords or [])
    if not _assist_enabled_for(workspace_id):
        return keywords, [], []
    try:
        from app.db import session_scope

        scope = {"topic": topic or "", "content_format": "broll"}
        with session_scope() as s:
            probe = apply_lessons(s, workspace_id, scope, {"topic": topic}, "broll")
        _shadow_scene_purposes(workspace_id, keywords)
        extra = list(probe.get("lesson_queries") or [])
        merged = list(keywords)
        for q in extra:
            if q and q.lower() not in {k.lower() for k in merged} and len(merged) < len(keywords) + 2:
                merged.append(q)
        return merged, list(probe.get("applied_lessons") or []), list(probe.get("lesson_recommendations") or [])
    except Exception as exc:
        logger.debug(f"[lessons] broll influence skipped: {type(exc).__name__}")
        return keywords, [], []


def annotate_derive_moment(workspace_id: str, topic: str, enriched: dict) -> dict:
    """Campaign derive hook: recommendations annotation on the moment copy."""
    if not _assist_enabled_for(workspace_id):
        return enriched
    try:
        from app.db import session_scope

        with session_scope() as s:
            out = apply_lessons(s, workspace_id, {"topic": topic or ""}, dict(enriched), "derive")
        return out
    except Exception as exc:
        logger.debug(f"[lessons] derive influence skipped: {type(exc).__name__}")
        return enriched


def schedule_hour_prior(workspace_id: str, hours: list[int],
                        platforms: list[str]) -> tuple[list[int], list[str], list[dict]]:
    """Scheduler hook: stable re-rank of measured hours. Caps/Idempotency untouched."""
    hours = list(hours or [])
    if not _assist_enabled_for(workspace_id):
        return hours, [], []
    try:
        from app.db import session_scope

        scope = {"platform": (platforms[0] if platforms else "")}
        with session_scope() as s:
            probe = apply_lessons(s, workspace_id, scope, {"platforms": list(platforms)}, "schedule")
        preferred = [h for h in (probe.get("lesson_preferred_hours") or []) if h in hours]
        ranked = preferred + [h for h in hours if h not in preferred]
        return ranked, list(probe.get("applied_lessons") or []), list(probe.get("lesson_recommendations") or [])
    except Exception as exc:
        logger.debug(f"[lessons] schedule influence skipped: {type(exc).__name__}")
        return hours, [], []


# -- SHADOW DecisionEngine uses (advisory only, never raise) -------------------

def _shadow_engine(workspace_id: str):
    try:
        from app.engine.intelligence.decision import DecisionEngine

        return DecisionEngine(workspace_id, mode="SHADOW", persist=False)
    except Exception:
        return None


def _shadow_rank_lessons(workspace_id: str, kind: str, artifact: dict,
                         lessons: list) -> list[str] | None:
    """Semantic relevance ranking of lessons; deterministic order always wins."""
    try:
        engine = _shadow_engine(workspace_id)
        if engine is None or not lessons:
            return None
        criterion = f"{kind} guidance relevance for {str(artifact.get('topic') or artifact.get('scope') or kind)[:120]}"
        items = [f"{(r.evidence_json or {}).get('metric', '')}: {r.description}" for r in lessons]
        out, _rec = engine.rank({"items": items, "criterion": criterion})
        order = list((out or {}).get("order", [])) if isinstance(out, dict) else []
        if len(order) != len(lessons):
            return None
        return [lessons[i].pattern_key for i in order if 0 <= i < len(lessons)]
    except Exception as exc:
        logger.debug(f"[lessons] shadow rank skipped: {type(exc).__name__}")
        return None


def _shadow_categorize_hooks(workspace_id: str, variants: list[dict]) -> None:
    """Shadow hook-type classification; logs only, never rescores."""
    try:
        engine = _shadow_engine(workspace_id)
        if engine is None:
            return
        for v in variants or []:
            text = str(v.get("script") or "")[:200]
            if not text.strip():
                continue
            out, rec = engine.classify({"item": text, "labels": list(HOOK_LABELS)})
            label = (out or {}).get("label") if isinstance(out, dict) else None
            if label and rec.agreement is False:
                logger.info(f"[lessons] shadow hook categorization disagrees: '{text[:60]}' → {label}")
    except Exception as exc:
        logger.debug(f"[lessons] shadow hook classify skipped: {type(exc).__name__}")


def _shadow_scene_purposes(workspace_id: str, keywords: list[str]) -> None:
    """Shadow scene-purpose classification for broll queries; logs only."""
    try:
        engine = _shadow_engine(workspace_id)
        if engine is None:
            return
        for kw in (keywords or [])[:4]:
            out, rec = engine.classify({"item": str(kw)[:200], "labels": list(SCENE_PURPOSES)})
            if isinstance(out, dict) and rec.agreement is False:
                logger.info(f"[lessons] shadow scene-purpose disagrees: '{kw[:60]}' → {out.get('label')}")
    except Exception as exc:
        logger.debug(f"[lessons] shadow scene classify skipped: {type(exc).__name__}")


def _shadow_lesson_notes(effect: dict, scope: dict) -> dict:
    """Shadow semantic notes captured at lesson creation (advisory metadata)."""
    notes: dict = {}
    try:
        engine = _shadow_engine("")
        if engine is None:
            return notes
        rec_text = str(effect.get("recommendation") or "")[:300]
        if rec_text:
            out, _rec = engine.verify({"claim": rec_text,
                                       "evidence": f"scope {scope} effect {effect.get('improvement_pct')}"})
            if isinstance(out, dict):
                notes["semantic_check"] = str(out.get("status", ""))[:20]
    except Exception as exc:
        logger.debug(f"[lessons] shadow lesson notes skipped: {type(exc).__name__}")
    return notes


__all__ = [
    "KINDS",
    "MIN_EVIDENCE_IDS",
    "MIN_SAMPLE_SIZE",
    "SCOPE_DIMS",
    "STATUSES",
    "annotate_derive_moment",
    "apply_hook_lesson_bonus",
    "apply_lessons",
    "broll_keyword_boost",
    "confidence_for",
    "duration_bucket",
    "effective_status",
    "generate_lessons",
    "is_lesson_row",
    "learning_assist_enabled",
    "lesson_key",
    "matching_lessons",
    "maybe_apply_strategy_lessons",
    "normalize_effect",
    "normalize_scope",
    "schedule_hour_prior",
    "scope_matches",
    "script_guidance",
    "strategy_scope",
]
