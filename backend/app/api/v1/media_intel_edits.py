"""Silence + filler editing API (Work 12 Lane D) -- contracts §7 + §14.

Mounted by the orchestrator in ``api/v1/__init__.py``::

    POST /workspaces/{ws}/media-intel/audio/silence        member + edit cap
    POST /workspaces/{ws}/media-intel/audio/fillers        member + edit cap
    GET  /workspaces/{ws}/media-intel/proposals            viewer
    POST /workspaces/{ws}/media-intel/proposals/{id}/decide member + edit cap
    POST /workspaces/{ws}/media-intel/proposals/apply      member + edit cap
    GET  /workspaces/{ws}/media-intel/time-map             viewer

Only the §7/§14 surface this lane owns lives here; the alignment, speaker,
visual, reframe and QC routes belong to the other lanes and are NOT duplicated.

Rules this module keeps:

* **Workspace scoping on every route.** A foreign asset / proposal / timeline id
  is **404**, never 403, and the 404 is raised BEFORE the capability check so it
  can never be an oracle. ``proposals/apply`` gets its 404 for free: the
  canonical timelines route does its own workspace-scoped lookup.
* **Floors + capabilities.** The floor is the existing
  ``require_workspace_role`` dependency; the project-scoped capability is the
  existing ``edit_timeline`` name from ``services/project_auth`` -- it only
  NARROWS the member floor, never widens it.
* **The QC gate runs first.** ``app.engine.intel.qc.assert_qc_allows_apply`` is
  consulted for every run in the batch BEFORE the plan is built: no verdict means
  refused, a FAIL needs an override capability plus a recorded override, and any
  refusal is a 409 carrying ``QCApplyBlocked.as_dict()``.
* **The apply path is the Work 02 path.** ``POST /proposals/apply`` does not
  save anything itself: it builds the operation batch and submits it through
  ``api.v1.timelines.apply_timeline_operations`` -- the SAME function the
  editor route uses, so the ``base_version`` optimistic-concurrency gate and the
  canonical scene resync still apply and a stale base version is a 409. There is
  deliberately no second save path (contracts §7).
* **Emission after commit.** ``record_event``/``track_cost`` open their own
  session, so an engine may never call them mid-transaction (contracts §3).
  Every engine call here RETURNS its ``events``; the route commits first and
  emits afterwards, mirroring ``engine/collab/reviews.py``.
* ``{"items": [...]}`` envelopes, hand-written dict DTOs, short ``HTTPException``
  details, generic 500s that log the detail server-side only.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.engine.intel import silence_fillers as engine
from app.models import ContentTimeline, MediaAsset, User, Workspace
from app.services.auth_service import get_current_user, require_workspace_role
from app.services.media_intel_runs import complete_run, create_run, run_dto, start_run
from app.services.project_auth import assert_capability
from app.services.storage import managed_path

media_intel_edits_router = APIRouter(
    prefix="/workspaces/{workspace_id}/media-intel", tags=["media-intel-edits"]
)
logger = logging.getLogger("ymoney.media_intel")

#: existing project capability a mutation needs (Work 11 matrix; project_auth
#: rejects any other name with 422, so this must stay an EXISTING name)
MUTATION_CAPABILITY = "edit_timeline"
#: applying a QC-FAIL plan is a governance action; the closest existing "manage"
#: name in the locked matrix is ``edit_project`` (owner/admin). The SAME name
#: Lane H's ``POST /qc/{run_id}/override`` uses -- one rule, one place.
OVERRIDE_CAPABILITY = "edit_project"


# ---------------------------------------------------------------------------
# error policy (mirrors api/v1/exports.py)
# ---------------------------------------------------------------------------


def _short(exc: BaseException, limit: int = 180) -> str:
    """One-line, truncated message for deliberate domain errors."""
    text = " ".join(str(exc or "").split()) or type(exc).__name__
    return text[:limit]


def _guard(value_error: int = 422):
    """Uniform error policy for one route body.

    ``HTTPException`` passes through (401/403/404/409/422); ``ValueError``
    becomes ``value_error`` with a short detail; ``KeyError`` becomes 404; and
    anything else is logged on ``ymoney.media_intel`` and surfaced as a generic
    500 that echoes nothing.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except HTTPException:
                raise
            except KeyError as exc:
                raise HTTPException(status_code=404,
                                    detail=_short(exc)) from None
            except ValueError as exc:
                raise HTTPException(status_code=value_error,
                                    detail=_short(exc)) from None
            except Exception:  # noqa: BLE001 -- deliberate catch-all at the API edge
                logger.exception("media-intel edits route failed: %s",
                                 getattr(fn, "__name__", fn))
                raise HTTPException(status_code=500, detail="internal error") from None

        return run

    return decorate


def _check(db: Session, ws: Workspace, user: User, **kwargs) -> None:
    """assert_capability at the route edge; bad args -> 422, 403/404 pass through."""
    try:
        assert_capability(db, ws, user, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_short(exc)) from None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _asset(db: Session, ws: Workspace, asset_id: str) -> MediaAsset:
    """Workspace-scoped asset fetch -> 404 for foreign/missing (never 403)."""
    row = db.get(MediaAsset, str(asset_id or ""))
    if row is None or row.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="asset not found")
    return row


def _asset_path(ws: Workspace, asset: MediaAsset) -> str:
    """The asset's REAL file, resolved fail-closed inside the workspace root.

    ``managed_path`` returns None for a key that escapes the managed directory
    or for a ``mock:`` reference; either way there is no honest media to
    measure, and the detection is reported UNAVAILABLE rather than guessed.
    """
    path = managed_path(ws.id, str(asset.storage_key or ""))
    if path is None or not path.exists():
        raise HTTPException(
            status_code=422,
            detail="asset has no readable media file inside this workspace",
        )
    return str(path)


def _timeline(db: Session, ws: Workspace, timeline_id: str) -> ContentTimeline:
    row = db.get(ContentTimeline, str(timeline_id or ""))
    if row is None or row.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="timeline not found")
    return row


def _emit(ws_id: str, events: list[dict]) -> None:
    """Publish the engine's events AFTER the commit (contracts §3)."""
    from app.services.events import record_event

    for event in events or []:
        try:
            record_event(ws_id, str(event.get("kind") or ""),
                         str(event.get("message") or ""),
                         level=str(event.get("level") or "info"),
                         source="media_intel", data=dict(event.get("data") or {}))
        except Exception:  # noqa: BLE001 -- telemetry must never fail a request
            logger.warning("media-intel edit event %s failed", event.get("kind"))


# ---------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------


class SilenceRequest(BaseModel):
    asset_id: str = Field(min_length=1, max_length=36)
    timeline_id: str | None = Field(default=None, max_length=36)
    project_id: str | None = Field(default=None, max_length=36)
    #: recompute instead of reusing a cached COMPLETED run
    force: bool = False
    policy: dict | None = None


class FillerRequest(BaseModel):
    asset_id: str = Field(min_length=1, max_length=36)
    timeline_id: str | None = Field(default=None, max_length=36)
    project_id: str | None = Field(default=None, max_length=36)
    #: explicit timed cues ``[{text, start_s, end_s}]`` -- used only when the
    #: asset has no word rows and the timeline has no caption clips
    cues: list | None = None
    force: bool = False
    policy: dict | None = None


class DecideRequest(BaseModel):
    #: a closed vocabulary, so an unknown decision is a pydantic 422 and never a
    #: state conflict
    decision: Literal["keep", "remove", "shorten"]


class ApplyRequest(BaseModel):
    #: the canonical timeline the ops are submitted to (contracts §7: never a
    #: second audio-edit timeline)
    timeline_id: str = Field(min_length=1, max_length=36)
    #: the version the editor last loaded -- the Work 02 concurrency gate
    base_version: int = Field(ge=1)
    proposal_ids: list | None = None
    asset_id: str | None = Field(default=None, max_length=36)
    policy: dict | None = None
    #: set only by a caller that holds the QC-override capability AND has
    #: already recorded an attributable override (Lane H's gate requires BOTH)
    override: bool = False


# ---------------------------------------------------------------------------
# detection routes
# ---------------------------------------------------------------------------


@media_intel_edits_router.post("/audio/silence", summary="Propose dead-air cuts")
@_guard()
def detect_silence_edits(
    body: SilenceRequest,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    policy = engine.EditPolicy.from_dict(body.policy)
    asset = _asset(db, ws, body.asset_id)  # 404 first: never a capability oracle
    _check(db, ws, user, capability=MUTATION_CAPABILITY,
           target_type="asset", target_id=asset.id)
    path = _asset_path(ws, asset)

    created = create_run(
        db, ws, kind="silence", asset=asset,
        provider_key=engine.SILENCE_PROVIDER, model_version=engine.SILENCE_MODEL,
        params={"policy": policy.to_dict(), "source": "silencedetect"},
        force=bool(body.force), requested_by=user.id,
    )
    if created["cache_hit"]:
        # a cached run is prior work: reuse its proposals, recompute nothing
        items = engine.list_proposals(db, ws.id, run_id=created["id"])
        return {"run": created, "cache_hit": True, "applied": False,
                "proposals": items, "counts_by_reason": {},
                "policy": policy.to_dict(), "policy_id": policy.policy_id,
                "auto_apply": bool(policy.auto_apply)}

    row = _run_row(db, created["id"])
    start_run(db, row)
    result = engine.detect_silence(
        db, ws, asset, row, path, policy,
        project_id=body.project_id, timeline_id=body.timeline_id,
    )
    complete_run(db, row, metrics=result["metrics"], warnings=[]
                 if result["detection"]["measured"] else [result["detection"]["reason"]])
    db.commit()  # the engine's events are emitted AFTER this line
    _emit(ws.id, result["events"])
    return {"run": run_dto(row), "cache_hit": False, **result}


@media_intel_edits_router.post("/audio/fillers", summary="Propose filler/stutter cuts")
@_guard()
def detect_filler_edits(
    body: FillerRequest,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    policy = engine.EditPolicy.from_dict(body.policy)
    asset = _asset(db, ws, body.asset_id)
    _check(db, ws, user, capability=MUTATION_CAPABILITY,
           target_type="asset", target_id=asset.id)
    source = engine.resolve_units(
        db, ws.id, asset.id,
        timeline_id=body.timeline_id,
        cues=body.cues or None,
    )

    created = create_run(
        db, ws, kind="fillers", asset=asset,
        provider_key=engine.FILLER_PROVIDER, model_version=engine.FILLER_MODEL,
        params={"policy": policy.to_dict(), "source": source.get("source", "none"),
                "lexicon_version": engine.FILLER_LEXICON_VERSION},
        force=bool(body.force), requested_by=user.id,
    )
    if created["cache_hit"]:
        items = engine.list_proposals(db, ws.id, run_id=created["id"])
        return {"run": created, "cache_hit": True, "applied": False,
                "proposals": items, "counts_by_reason": {},
                "text_source": source.get("source", "none"),
                "policy": policy.to_dict(), "policy_id": policy.policy_id,
                "auto_apply": bool(policy.auto_apply)}

    row = _run_row(db, created["id"])
    start_run(db, row)
    result = engine.detect_fillers(
        db, ws, asset, row, source.get("units") or [], source, policy,
        project_id=body.project_id, timeline_id=body.timeline_id,
    )
    complete_run(db, row, metrics=result["metrics"], warnings=[]
                 if source.get("available") else [source.get("reason", "")])
    db.commit()
    _emit(ws.id, result["events"])
    return {"run": run_dto(row), "cache_hit": False, **result}


def _run_row(db: Session, run_id: str):
    from app.models import MediaIntelRun

    row = db.get(MediaIntelRun, str(run_id))
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return row


# ---------------------------------------------------------------------------
# proposals
# ---------------------------------------------------------------------------


@media_intel_edits_router.get("/proposals", summary="List edit proposals")
@_guard()
def list_edit_proposals(
    asset_id: str | None = Query(default=None, max_length=36),
    run_id: str | None = Query(default=None, max_length=36),
    status: str | None = Query(default=None, max_length=16),
    kind: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=200, ge=1, le=500),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    items = engine.list_proposals(db, ws.id, asset_id=asset_id, run_id=run_id,
                                  status=status, kind=kind, limit=limit)
    return {"items": items, "total": len(items)}


@media_intel_edits_router.post("/proposals/{proposal_id}/decide",
                               summary="Keep / remove / shorten one proposal")
@_guard(value_error=409)
def decide_edit_proposal(
    proposal_id: str,
    body: DecideRequest,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if engine.get_proposal(db, ws.id, proposal_id) is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    _check(db, ws, user, capability=MUTATION_CAPABILITY,
           target_type="timeline", target_id=proposal_id)
    row = engine.decide_proposal(db, ws.id, proposal_id, body.decision, user_id=user.id)
    db.commit()
    _emit(ws.id, [{
        "kind": engine.EVENT_PROPOSAL_DECIDED,
        "message": (f"edit proposal {row.id[:8]} decided "
                    f"'{str(row.decision or '')}' ({row.kind})"),
        "level": "info",
        "data": {"proposal_id": row.id, "decision": row.decision,
                 "kind": row.kind, "actor": user.id, "run_id": row.run_id},
    }])
    return engine.proposal_dto(row, _evidence_of(db, row))


def _evidence_of(db: Session, row) -> dict:
    from app.models import MediaIntelRun

    run = db.get(MediaIntelRun, str(row.run_id or ""))
    if run is None:
        return {}
    return dict((run.metrics_json or {}).get("evidence") or {}).get(row.id, {})


# ---------------------------------------------------------------------------
# apply -- the canonical Work 02 operations path
# ---------------------------------------------------------------------------


def _qc_gate(
    db: Session, ws: Workspace, user: User, rows: list, *, override: bool
) -> list[dict]:
    """Consult Lane H's apply gate for EVERY run in the batch (contracts §13).

    ``app.engine.intel.qc.assert_qc_allows_apply`` is the single authority on
    whether a plan may be applied: PASS / PASS_WITH_WARNINGS / REVIEW_REQUIRED
    apply, a FAIL needs BOTH an override capability and a recorded, attributable
    override, and a run with NO verdict at all is refused (no verdict means the
    plan was never reviewed). Every one of those refusals is a 409 with
    ``QCApplyBlocked.as_dict()`` -- the machine-readable ``failures`` /
    ``needs_override`` the UI needs to render the right prompt.

    A batch may legitimately span several runs (a silence plan plus a filler
    plan on the same asset), so EVERY run is gated -- an all-or-nothing batch
    that skipped the run carrying the FAIL would be a hole in the gate.
    """
    from app.engine.intel import qc as intel_qc

    if intel_qc is None:  # pragma: no cover - sibling lane absent
        raise HTTPException(
            status_code=500,
            detail="QC module unavailable: an edit plan cannot be applied unreviewed",
        )
    if override:
        # governance action: the same existing capability Lane H's override
        # route uses (project_auth has no QC-specific name and none is invented)
        _check(db, ws, user, capability=OVERRIDE_CAPABILITY,
               target_type="timeline", target_id=rows[0].asset_id)
        # W11.5 B-F1: that capability is vacuous on unlinked timelines, so
        # applying a FAIL plan additionally needs the admin floor.
        from app.services.project_auth import assert_workspace_admin
        assert_workspace_admin(db, ws, user, action="applying a QC-FAIL plan")
    decisions: list[dict] = []
    for run_id in sorted({str(row.run_id) for row in rows}):
        try:
            decision = intel_qc.assert_qc_allows_apply(db, ws, run_id, override=bool(override))
        except intel_qc.QCApplyBlocked as exc:
            raise HTTPException(status_code=409, detail=exc.as_dict()) from None
        decisions.append(decision)
    return decisions


@media_intel_edits_router.post("/proposals/apply",
                               summary="Apply decided proposals as timeline operations")
@_guard(value_error=409)
def apply_edit_proposals(
    body: ApplyRequest,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.api.v1.timelines import OperationsBody, apply_timeline_operations

    # 404 for a foreign timeline BEFORE the capability check
    timeline = _timeline(db, ws, body.timeline_id)
    plan = engine.plan_apply(db, ws.id, proposal_ids=body.proposal_ids,
                             asset_id=body.asset_id)
    rows = plan["rows"]
    _check(db, ws, user, capability=MUTATION_CAPABILITY,
           target_type="timeline", target_id=body.timeline_id)

    if body.policy:
        try:
            policy = engine.EditPolicy.from_dict(body.policy)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=_short(exc)) from None
    else:
        policy = _policy_of(db, rows)
    # the QC gate runs BEFORE the plan is built, so a refused plan never even
    # computes operations, let alone writes one
    qc_decisions = _qc_gate(db, ws, user, rows, override=bool(body.override))
    doc = dict(timeline.tracks_json or {})
    doc.setdefault("fps", timeline.fps)
    doc.setdefault("duration_seconds", timeline.duration_seconds)
    prepared = engine.apply_plan(db, rows, doc, policy=policy)

    # THE save: the editor's own operations endpoint (same function, same
    # base_version 409 gate, same scene resync). No parallel save path exists.
    applied = apply_timeline_operations(
        body.timeline_id,
        OperationsBody(base_version=body.base_version,
                       operations=prepared["operations"]),
        ws=ws, db=db,
    )
    time_map = prepared["time_map"]
    engine.mark_applied(db, prepared["proposal_ids"])
    engine.build_time_map(
        db, ws.id, plan["asset_id"], policy,
        time_map.removals, prepared["media_span_s"], created_by=user.id,
    )
    mapped_scenes = engine.map_scenes(db, ws.id, body.timeline_id, time_map)
    db.commit()  # every event is emitted AFTER this line
    _emit(ws.id, [{
        "kind": engine.EVENT_EDITS_APPLIED,
        "message": (f"applied {len(prepared['proposal_ids'])} silence/filler edit(s) "
                    f"to timeline {body.timeline_id[:8]} via canonical operations"),
        "level": "success",
        "data": {"timeline_id": body.timeline_id, "actor": user.id,
                 "proposals": prepared["proposal_ids"],
                 "operations": len(prepared["operations"]),
                 "removed_duration_s": time_map.removed_duration_s,
                 "removal_ratio": time_map.removal_ratio,
                 "policy_id": prepared["policy_id"],
                 "resynced_scenes": applied.get("resynced_scenes", 0),
                 "mapped_scenes": mapped_scenes,
                 "qc_verdicts": [d.get("verdict") for d in qc_decisions],
                 "override_used": any(d.get("override_used") for d in qc_decisions)},
    }])
    return {
        "applied": len(prepared["operations"]),
        "operations": prepared["operations"],
        "proposals": [engine.proposal_dto(row) for row in rows],
        "proposal_ids": prepared["proposal_ids"],
        "skipped": plan["skipped"],
        "policy": policy.to_dict(),
        "policy_id": prepared["policy_id"],
        "time_map": time_map.to_dict(policy_id=prepared["policy_id"]),
        "mapped_scenes": mapped_scenes,
        "qc": qc_decisions,
        "timeline": applied,
    }


def _policy_of(db: Session, rows: list) -> engine.EditPolicy:
    """The policy that produced these proposals, for a faithful apply.

    Read back from the run manifest so the SAME thresholds (``keep_padding_s``,
    ``shorten_to_s``, ...) govern the cut that the proposal previewed. Falls
    back to the defaults when a manifest predates the field.
    """
    from app.models import MediaIntelRun

    run = db.get(MediaIntelRun, str(rows[0].run_id or ""))
    stored = dict((run.metrics_json or {}).get("policy") or {}) if run is not None else {}
    return engine.EditPolicy.from_dict(stored)


# ---------------------------------------------------------------------------
# time map
# ---------------------------------------------------------------------------


@media_intel_edits_router.get("/time-map", summary="Source <-> edited audio mapping")
@_guard()
def get_audio_time_map(
    asset_id: str = Query(..., min_length=1, max_length=36),
    policy_id: str | None = Query(default=None, max_length=64),
    source_time: float | None = Query(default=None, ge=0, description="map a source instant"),
    range_start: float | None = Query(default=None, ge=0, description="map a source span"),
    range_end: float | None = Query(default=None, ge=0),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    row = engine.load_time_map(db, ws.id, asset_id, policy_id)
    if row is None:
        raise HTTPException(status_code=404, detail="time map not found")
    time_map = engine.TimeMap.from_persisted(row)
    assert time_map is not None  # from_persisted only returns None for None
    payload = time_map.to_dict(policy_id=str(row.policy_id or ""))
    payload["id"] = row.id
    payload["asset_id"] = row.asset_id
    payload["created_at"] = (row.created_at.isoformat() + "Z") if row.created_at else None
    # The ONE mapping function every consumer (captions, scenes, UI) must go
    # through. `t` maps a source instant, `range_start`/`range_end` a source
    # span; the inverse is exposed for the same honesty.
    mapped: dict[str, Any] = {}
    if source_time is not None:
        mapped["time"] = {
            "source_s": source_time,
            "edited_s": time_map.map_time(source_time),
            "inverse_s": time_map.inverse_time(time_map.map_time(source_time)),
        }
    if range_start is not None or range_end is not None:
        start = range_start if range_start is not None else 0.0
        end = range_end if range_end is not None else time_map.source_duration_s
        mapped["range"] = {
            "source": {"start_s": start, "end_s": end},
            **time_map.map_range(start, end),
        }
    if mapped:
        payload["mapped"] = mapped
    return payload


__all__ = ["MUTATION_CAPABILITY", "media_intel_edits_router"]
