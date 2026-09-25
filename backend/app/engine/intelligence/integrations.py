"""Advisory-only intelligence hooks for existing pipelines.

Every helper below is a thin call site: in DISABLED mode it returns inputs
byte-identical (no provider call, no recording); in SHADOW it records a
shadow evaluation and still returns the deterministic baseline untouched;
in ASSISTED/PRIMARY it returns advisory annotations that callers may use
only within their existing deterministic guards (diversity overlap caps,
QC thresholds, master-first dependencies all stay authoritative).
"""

from __future__ import annotations

from typing import Any


def decision_mode_for(workspace_id: str) -> str:
    """Workspace decision_mode; SHADOW when unconfigured (new paths default)."""
    try:
        from app.db import session_scope
        from app.engine.intelligence.decision import get_intelligence_settings
        from app.models import Workspace

        with session_scope() as s:
            row = s.get(Workspace, workspace_id)
            settings_json = dict(getattr(row, "settings_json", None) or {})
        return get_intelligence_settings(settings_json)["decision_mode"]
    except Exception:
        return "SHADOW"


def _engine(workspace_id: str, mode: str):
    from app.engine.intelligence.decision import DecisionEngine

    return DecisionEngine(workspace_id, mode=mode, persist=(mode != "DISABLED"))


def advise_trend_scores(items: list[dict], *, workspace_id: str) -> list[dict]:
    """Advisory trend filtering. Baseline order/scores are never mutated here."""
    mode = decision_mode_for(workspace_id)
    if mode == "DISABLED" or not items:
        return items
    try:
        engine = _engine(workspace_id, mode)
        topics = [str(i.get("topic", "")) for i in items]
        out, _ = engine.rank(
            {"items": topics, "criterion": "emerging viral short-form potential"},
            save=(mode != "DISABLED"),
        )
        advisory = list((out or {}).get("order", [])) if isinstance(out, dict) else []
        if mode == "SHADOW" or not advisory:
            return items
        # ASSISTED/PRIMARY: annotate only; scoring stays authoritative upstream.
        annotated = [dict(i) for i in items]
        for rank_pos, idx in enumerate(advisory):
            if 0 <= idx < len(annotated):
                meta = dict(annotated[idx].get("_decision") or {})
                meta["advisory_rank"] = rank_pos
                annotated[idx]["_decision"] = meta
        return annotated
    except Exception:
        return items


def advise_repurpose_moments(moments: list[dict], *, workspace_id: str) -> list[dict]:
    """Advisory repurpose candidate filtering. Never drops baseline moments."""
    mode = decision_mode_for(workspace_id)
    if mode == "DISABLED" or not moments:
        return moments
    try:
        engine = _engine(workspace_id, mode)
        hooks = [str(m.get("hook", "") or m.get("text", "")) for m in moments]
        out, _ = engine.rank(
            {"items": hooks, "criterion": "standalone viral short hook strength"},
            save=True,
        )
        advisory = list((out or {}).get("order", [])) if isinstance(out, dict) else []
        if mode == "SHADOW" or not advisory:
            return moments
        annotated = [dict(m) for m in moments]
        for rank_pos, idx in enumerate(advisory):
            if 0 <= idx < len(annotated):
                meta = dict(annotated[idx].get("_decision") or {})
                meta["advisory_rank"] = rank_pos
                annotated[idx]["_decision"] = meta
        return annotated
    except Exception:
        return moments


def advise_diversity(candidates: list[dict], selected: list[dict], *,
                     workspace_id: str) -> dict[str, Any]:
    """Advisory diversity scores. The MMR selector stays authoritative."""
    mode = decision_mode_for(workspace_id)
    if mode == "DISABLED" or not candidates:
        return {"mode": mode, "advisory": []}
    try:
        engine = _engine(workspace_id, mode)
        hooks = [str(c.get("hook", "") or c.get("topic", "")) for c in candidates]
        out, _ = engine.score_batch(
            [{"item": h} for h in hooks], save=True)
        scores = []
        for i, result in enumerate(out):
            value = result.get("score", 0.0) if isinstance(result, dict) else 0.0
            try:
                scores.append({"index": i, "advisory_score": float(value)})
            except (TypeError, ValueError):
                scores.append({"index": i, "advisory_score": 0.0})
        return {"mode": mode, "advisory": scores,
                "note": "advisory only; MMR guards stay authoritative"}
    except Exception:
        return {"mode": mode, "advisory": []}


def advise_broll_rank(candidates: list[dict], *, workspace_id: str,
                      topic: str = "") -> list[dict]:
    """Advisory B-roll ranking. Callers keep the deterministic order in SHADOW."""
    mode = decision_mode_for(workspace_id)
    if mode == "DISABLED" or not candidates:
        return candidates
    try:
        engine = _engine(workspace_id, mode)
        texts = [str(c.get("query", "") or c.get("prompt", "")) for c in candidates]
        out, _ = engine.rank(
            {"items": texts, "criterion": f"visual relevance for '{topic[:80]}'"},
            save=True,
        )
        advisory = list((out or {}).get("order", [])) if isinstance(out, dict) else []
        if mode == "SHADOW" or not advisory:
            return candidates
        ordered = [candidates[i] for i in advisory if 0 <= i < len(candidates)]
        ordered += [c for i, c in enumerate(candidates) if i not in set(advisory)]
        return ordered
    except Exception:
        return candidates


__all__ = [
    "advise_broll_rank",
    "advise_diversity",
    "advise_repurpose_moments",
    "advise_trend_scores",
    "decision_mode_for",
]
