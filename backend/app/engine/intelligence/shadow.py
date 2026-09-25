"""Shadow evaluation: run a candidate decision next to the deterministic baseline."""

from __future__ import annotations

import contextlib
import time
from typing import Any


def run_shadow(workspace_id: str, kind: str, payload: dict, *,
               candidate_provider: str = "local",
               persist: bool = True) -> dict:
    """Evaluate ``candidate_provider`` against the deterministic baseline.

    The baseline output is always returned; the candidate never affects the
    caller. Agreement, latency and cost are recorded for :func:`shadow_report`.
    """
    from app.engine.intelligence.decision import DecisionEngine

    baseline_engine = DecisionEngine(workspace_id, mode="DISABLED", persist=False)
    baseline, _ = baseline_engine.decide(kind, payload)

    candidate_engine = DecisionEngine(
        workspace_id, mode="ASSISTED",
        provider_preference=candidate_provider, persist=False)
    started = time.monotonic()
    try:
        candidate, candidate_record = candidate_engine.decide(kind, payload)
        candidate_latency = candidate_record.latency_ms or int((time.monotonic() - started) * 1000)
        candidate_cost = candidate_record.cost_usd
        candidate_error = ""
    except Exception as exc:  # noqa: BLE001 - shadow must never raise
        candidate, candidate_latency, candidate_cost = None, 0, 0.0
        candidate_error = f"{type(exc).__name__}: {exc}"[:300]

    from app.engine.intelligence.decision import _outputs_agree

    agree = _outputs_agree(kind, baseline, candidate) if not candidate_error else False
    entry = {
        "workspace_id": workspace_id,
        "kind": kind,
        "agree": agree,
        "baseline": baseline,
        "candidate": candidate,
        "candidate_provider": candidate_provider,
        "latency_ms": candidate_latency,
        "cost_usd": candidate_cost,
        "error": candidate_error,
    }
    if persist and workspace_id:
        with contextlib.suppress(Exception):
            _persist_shadow(workspace_id, kind, payload, entry)
    return entry


def _persist_shadow(workspace_id: str, kind: str, payload: dict, entry: dict) -> None:
    from app.db import session_scope
    from app.engine.intelligence.decision import _sanitize
    from app.models.intelligence import DecisionRecordRow

    with session_scope() as s:
        s.add(DecisionRecordRow(
            workspace_id=workspace_id,
            kind=kind,
            mode="SHADOW",
            requested_provider=str(entry.get("candidate_provider", "")),
            actual_provider=str(entry.get("candidate_provider", "")),
            model="",
            latency_ms=int(entry.get("latency_ms", 0) or 0),
            cost_usd=float(entry.get("cost_usd", 0.0) or 0.0),
            fallback_reason=str(entry.get("error", "") or "")[:500],
            input_json=_sanitize(dict(payload or {})),
            output_json=_sanitize({
                "agree": entry.get("agree"),
                "baseline": entry.get("baseline"),
                "candidate": entry.get("candidate"),
            }),
            agree=bool(entry.get("agree")),
            shadow_json={"shadow": True},
        ))
        s.flush()


def shadow_report(workspace_id: str, kind: str | None = None) -> dict:
    """Aggregate shadow agreement/latency/cost for a workspace."""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models.intelligence import DecisionRecordRow

    with session_scope() as s:
        query = select(DecisionRecordRow).where(
            DecisionRecordRow.workspace_id == workspace_id,
            DecisionRecordRow.mode == "SHADOW",
        )
        if kind:
            query = query.where(DecisionRecordRow.kind == kind)
        rows = s.scalars(query).all()
        total = len(rows)
        agreed = sum(1 for r in rows if r.agree is True)
        disagreed = sum(1 for r in rows if r.agree is False)
        by_kind: dict[str, dict[str, Any]] = {}
        for r in rows:
            bucket = by_kind.setdefault(r.kind, {"total": 0, "agreed": 0})
            bucket["total"] += 1
            if r.agree is True:
                bucket["agreed"] += 1
        latencies = [int(r.latency_ms or 0) for r in rows]
        costs = [float(r.cost_usd or 0.0) for r in rows]
        return {
            "workspace_id": workspace_id,
            "kind": kind,
            "total": total,
            "agreed": agreed,
            "disagreed": disagreed,
            "agreement_rate": round(agreed / total, 3) if total else None,
            "avg_latency_ms": round(sum(latencies) / total, 1) if total else None,
            "total_cost_usd": round(sum(costs), 6),
            "by_kind": {
                k: {**v, "agreement_rate": round(v["agreed"] / v["total"], 3) if v["total"] else None}
                for k, v in by_kind.items()
            },
        }


__all__ = ["run_shadow", "shadow_report"]
