"""Visual media-intelligence API: masks, background ops, active speaker.

Work 12 Lane F -- contracts §14 (the ``/masks``, ``/background`` and
``/active-speaker`` routes) + §9/§10/§12.

Mounted once by the orchestrator in ``api/v1/__init__.py``::

    POST /workspaces/{ws}/media-intel/masks                 member + edit cap
    GET  /workspaces/{ws}/media-intel/masks/{run_id}        viewer
    POST /workspaces/{ws}/media-intel/active-speaker        member + edit cap
    GET  /workspaces/{ws}/media-intel/active-speaker/{id}   viewer

(Lane G owns the reframe router -- ``/reframe`` + keyframes, and it already
serves ``POST /media-intel/background``. Nothing here duplicates either.)

Rules this module keeps (contracts §14 + the Work 11 conventions):

* Workspace scoping on every route: a foreign run/asset id is **404**, never
  403, and the 404 is raised BEFORE the capability check so it can never be used
  as an existence oracle.
* Floors are the existing ``require_workspace_role`` dependencies; the
  project-scoped ``edit_timeline`` capability (an EXISTING name from the locked
  Work 11 matrix -- ``project_auth`` would reject a new one with 422) only
  NARROWS the member floor for mutations.
* **Honest unavailability.** With no segmentation backend installed a run ends
  in the terminal ``UNAVAILABLE`` state with the provider's own reason. The
  response says so and creates no mask row -- never a fake success
  (contracts §15).
* **Emission ordering.** ``record_event``/``track_cost`` open their own session,
  so the engines here NEVER emit. They RETURN their events in the payload and
  this module emits them AFTER ``db.commit()`` -- the same ordering
  ``services/media_intel_runs.py::_publish`` and ``engine/collab/reviews.py``
  use, and the reason SQLite never drops an event.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db import get_db
from app.engine.intel import active_speaker as speaker_engine
from app.engine.intel import registry as intel_registry
from app.engine.intel import segmentation as segmentation_engine
from app.engine.intel.jobs import JOB_ACTIVE_SPEAKER, JOB_SEGMENT
from app.models import MediaAsset, User, Workspace
from app.services import media_intel_runs as runs_service
from app.services.auth_service import get_current_user, require_workspace_role
from app.services.project_auth import assert_capability

visual_router = APIRouter(prefix="/workspaces/{workspace_id}/media-intel",
                          tags=["media-intel-visual"])
logger = logging.getLogger("ymoney.media_intel")

#: existing project capability a mutation needs (Work 11 matrix; NOT a new name)
MUTATION_CAPABILITY = "edit_timeline"

#: activity-feed kinds this router emits. The orchestrator whitelists them in
#: ``services/webhooks.WEBHOOK_EVENTS`` at integration time -- the allowlist
#: silently drops unknown kinds, so an un-whitelisted event is invisible to
#: subscribers (contracts §3 emission-ordering note).
#:
#: ``POST /media-intel/background`` is NOT here: lane G's reframe router already
#: serves that path, so a second registration would shadow it. The composition
#: engine behind it lives in ``engine/intel/segmentation.py``
#: (``background_blur`` / ``background_replace`` / ``subject_crop`` /
#: ``tracked_overlay``) and is provider-independent, so lane G's route can call
#: it -- that wiring is an integration decision, reported to the orchestrator.
EVENT_MASKS_READY = "MEDIA_INTEL_MASKS_READY"
EVENT_ACTIVE_SPEAKER_MAPPED = "MEDIA_INTEL_ACTIVE_SPEAKER_MAPPED"
NEW_EVENT_KINDS: tuple[str, ...] = (
    EVENT_MASKS_READY,
    EVENT_ACTIVE_SPEAKER_MAPPED,
)


# ---------------------------------------------------------------------------
# error policy (mirrors api/v1/exports.py)
# ---------------------------------------------------------------------------


def _short(exc: BaseException, limit: int = 180) -> str:
    """One-line, truncated message for deliberate domain errors."""
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:limit]


def _guard(value_error: int = 422):
    """Uniform error policy for one route body.

    ``HTTPException`` passes through (401/403/404/409/422); ``ValueError``
    becomes ``value_error`` with a short detail; anything else is logged on
    ``ymoney.media_intel`` and surfaced as a generic 500 that echoes nothing.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=value_error,
                                    detail=_short(exc)) from None
            except Exception:  # noqa: BLE001 -- deliberate catch-all at the API edge
                logger.exception("media-intel visual route failed: %s",
                                 getattr(fn, "__name__", fn))
                raise HTTPException(status_code=500, detail="internal error") from None

        return run

    return decorate


def _check(db: Session, ws: Workspace, user: User, **kwargs) -> None:
    """assert_capability at the route edge; bad args -> 422, 404/403 pass through."""
    try:
        assert_capability(db, ws, user, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_short(exc)) from None


def _commercial_mode() -> bool:
    """Workspace-wide commercial flag (contracts §1.4), read defensively."""
    return bool(getattr(settings, "commercial_mode", False))


def _load_run(db: Session, ws: Workspace, run_id: str, kind: str):
    """Workspace-scoped run fetch -> 404 for foreign/missing/wrong-kind."""
    row = runs_service.get_run(db, ws.id, run_id)
    if row is None or (kind and str(row.kind or "") != kind):
        raise HTTPException(status_code=404, detail=f"{kind or 'media-intel'} run not found")
    return row


def _load_asset(db: Session, ws: Workspace, asset_id: str) -> MediaAsset:
    """Workspace-scoped asset fetch -> 404 for foreign/missing."""
    row = db.get(MediaAsset, str(asset_id or ""))
    if row is None or row.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="asset not found")
    return row


def _asset_media_path(ws: Workspace, asset: MediaAsset) -> str:
    """Absolute on-disk path of an asset, or a 422 when storage cannot resolve it."""
    path = segmentation_engine.resolve_workspace_file(ws.id, str(asset.storage_key or ""))
    if path is None or not str(path).exists():
        raise HTTPException(status_code=422, detail="asset file is not available in storage")
    return str(path)


def _emit(ws_id: str, kind: str, message: str, *, user_id: str, **extra: Any) -> None:
    """Activity ledger event. Called AFTER ``db.commit()``, never mid-transaction."""
    from app.services.events import record_event

    data: dict[str, Any] = {"actor": user_id}
    data.update(extra)
    record_event(ws_id, kind, message, level="info", source="media_intel", data=data)


# ---------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------


class MaskRequestBody(BaseModel):
    asset_id: str = Field(min_length=1, max_length=36)
    labels: list[str] | None = None
    mask_format: str = Field(default="PNG", pattern="^(PNG|RLE_JSON)$")
    fps: float = Field(default=segmentation_engine.DEFAULT_FPS, gt=0, le=30)
    max_frames: int = Field(default=0, ge=0, le=segmentation_engine.MAX_FRAMES_LIMIT)
    force: bool = False


class ActiveSpeakerRequestBody(BaseModel):
    asset_id: str = Field(min_length=1, max_length=36)
    diarization_run_id: str | None = Field(default=None, max_length=36)
    face_run_id: str | None = Field(default=None, max_length=36)
    use_motion: bool = False
    motion_fps: float = Field(default=speaker_engine.DEFAULT_MOTION_FPS, gt=0, le=30)
    min_overlap: float = Field(default=speaker_engine.DEFAULT_MIN_OVERLAP, ge=0.0, le=1.0)
    min_confidence: float = Field(
        default=speaker_engine.DEFAULT_MIN_CONFIDENCE, ge=0.0, le=1.0
    )
    tie_margin: float = Field(default=speaker_engine.DEFAULT_TIE_MARGIN, ge=0.0, le=1.0)
    force: bool = False


# ---------------------------------------------------------------------------
# endpoints -- masks (contracts §9)
# ---------------------------------------------------------------------------


@visual_router.post("/masks", summary="Segment an asset into mask FILES")
@_guard()
def create_masks(
    body: MaskRequestBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    asset = _load_asset(db, ws, body.asset_id)
    _check(db, ws, user, capability=MUTATION_CAPABILITY,
           target_type="asset", target_id=asset.id)

    provider, reasons = intel_registry.resolve("segmentation",
                                               commercial_mode=_commercial_mode())
    # Media existence is only the caller's problem when there is a provider to
    # feed: with the capability dark the honest answer is UNAVAILABLE, not a 422
    # about a file the (missing) model would never have opened.
    media_path = _asset_media_path(ws, asset) if provider is not None else ""
    provider_key = str(getattr(provider, "key", "") or "sam2_segmentation")
    params = {
        "labels": [str(v).upper() for v in (body.labels or ["PERSON"])],
        "format": body.mask_format,
        "fps": float(body.fps),
        "max_frames": int(body.max_frames),
    }
    run_dto = runs_service.create_run(
        db, ws, kind="segmentation", asset=asset, provider_key=provider_key,
        params=params, force=body.force, requested_by=user.id,
    )
    if run_dto.get("cache_hit"):
        row = _load_run(db, ws, run_dto["id"], "segmentation")
        return {
            "run": run_dto,
            "cache_hit": True,
            "items": _mask_items(db, ws.id, row.id),
        }

    row = _load_run(db, ws, run_dto["id"], "segmentation")
    runs_service.start_run(db, row)
    backend = getattr(provider, "backend", None) if provider is not None else None
    started = time.perf_counter()
    outcome = segmentation_engine.segment(
        workspace_id=ws.id,
        storage_path=media_path,
        run_id=row.id,
        backend=backend,
        params=params,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if not outcome.get("ok"):
        reason = str(outcome.get("reason") or "segmentation failed")
        if outcome.get("unavailable"):
            runs_service.unavailable_run(db, row, reason)
        else:
            runs_service.fail_run(db, row, "SEGMENTATION_FAILED")
        db.commit()
        _emit(ws.id, EVENT_MASKS_READY,
              f"media-intel segmentation unavailable: {reason[:120]}"
              if outcome.get("unavailable")
              else "media-intel segmentation failed",
              user_id=user.id, run_id=row.id, provider=provider_key,
              unavailable=bool(outcome.get("unavailable")),
              reason=reason[:200], job_kind=JOB_SEGMENT)
        return {
            "run": runs_service.run_dto(row),
            "cache_hit": False,
            "unavailable": bool(outcome.get("unavailable")),
            "reason": reason,
            "provider_reasons": dict(reasons or {}),
            "items": [],
            "job_kind": JOB_SEGMENT,
        }

    items = segmentation_engine.persist_masks(
        db, workspace_id=ws.id, run_id=row.id, input_asset_id=asset.id,
        records=outcome.get("masks") or [],
    )
    runs_service.complete_run(
        db, row,
        metrics={**dict(outcome.get("metrics") or {}), "items": len(items)},
        warnings=list(outcome.get("warnings") or []),
        processing_ms=elapsed_ms,
    )
    db.commit()
    _emit(ws.id, EVENT_MASKS_READY,
          f"media-intel segmentation wrote {len(items)} mask file(s)",
          user_id=user.id, run_id=row.id, asset_id=asset.id, provider=provider_key,
          masks=len(items), processing_ms=elapsed_ms, job_kind=JOB_SEGMENT)
    return {
        "run": runs_service.run_dto(row),
        "cache_hit": False,
        "items": items,
        "warnings": list(outcome.get("warnings") or []),
        "job_kind": JOB_SEGMENT,
    }


@visual_router.get("/masks/{run_id}", summary="Mask references for one run")
@_guard()
def list_masks(
    run_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    row = _load_run(db, ws, run_id, "segmentation")
    return {
        "run": runs_service.run_dto(row),
        "items": _mask_items(db, ws.id, row.id),
    }


def _mask_items(db: Session, workspace_id: str, run_id: str) -> list[dict]:
    """Mask rows of one run as DTOs (references + geometry, never pixels)."""
    from sqlalchemy import select

    from app.models import MaskAsset

    rows = db.scalars(
        select(MaskAsset)
        .where(
            MaskAsset.workspace_id == str(workspace_id),
            MaskAsset.run_id == str(run_id),
        )
        .order_by(MaskAsset.created_at.asc())
    ).all()
    return [segmentation_engine.mask_row_dto(row) for row in rows]


# ---------------------------------------------------------------------------
# endpoints -- active speaker (contracts §10)
# ---------------------------------------------------------------------------


@visual_router.post("/active-speaker", summary="Map speakers to faces (or refuse)")
@_guard()
def create_active_speaker(
    body: ActiveSpeakerRequestBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    asset = _load_asset(db, ws, body.asset_id)
    _check(db, ws, user, capability=MUTATION_CAPABILITY,
           target_type="asset", target_id=asset.id)

    diarization_run = _source_run(db, ws, body.diarization_run_id, "diarization", asset.id)
    face_run = _source_run(db, ws, body.face_run_id, "face_tracking", asset.id)
    segments = (
        speaker_engine.load_diarization(db, ws.id, diarization_run.id)
        if diarization_run is not None
        else []
    )
    tracks, samples = (
        speaker_engine.load_face_tracks(db, ws.id, face_run.id)
        if face_run is not None
        else ([], [])
    )
    # `unresolved_crossing` is not a column: Lane E persists the flagged track
    # labels in the face run's manifest, so the provenance is read from there.
    crossings = (
        speaker_engine.face_track_crossings(face_run) if face_run is not None else ()
    )

    params = {
        "diarization_run_id": str(diarization_run.id) if diarization_run else "",
        "face_run_id": str(face_run.id) if face_run else "",
        "use_motion": bool(body.use_motion),
        "min_overlap": float(body.min_overlap),
        "min_confidence": float(body.min_confidence),
        "tie_margin": float(body.tie_margin),
    }
    run_dto = runs_service.create_run(
        db, ws, kind="active_speaker", asset=asset,
        provider_key="active_speaker_mapper",
        model_version=str(getattr(diarization_run, "model_version", "") or ""),
        params=params, force=body.force, requested_by=user.id,
    )
    if run_dto.get("cache_hit"):
        return {"run": run_dto, "cache_hit": True,
                "items": _speaker_items(db, ws.id, run_dto["id"])}

    row = _load_run(db, ws, run_dto["id"], "active_speaker")
    runs_service.start_run(db, row)
    mapper = speaker_engine.ActiveSpeakerMapper(
        min_overlap=body.min_overlap,
        min_confidence=body.min_confidence,
        tie_margin=body.tie_margin,
        motion_fps=body.motion_fps,
    )
    media_path = ""
    if body.use_motion:
        try:
            media_path = _asset_media_path(ws, asset)
        except HTTPException as exc:
            # motion is OPTIONAL evidence: a missing file downgrades to
            # "motion unavailable", it never fails the whole mapping.
            media_path = ""
            logger.info("active-speaker motion evidence skipped: %s", exc.detail)
    report = mapper.map(
        segments=segments,
        tracks=tracks,
        samples=samples,
        use_motion=bool(body.use_motion),
        media_path=media_path or None,
        unresolved_crossings=crossings,
    )
    items = speaker_engine.persist_report(
        db, workspace_id=ws.id, run_id=row.id, asset_id=asset.id, report=report
    )
    runs_service.complete_run(
        db, row,
        metrics={
            "resolved": report.resolved,
            "unresolved": report.unresolved,
            "reasons": report.reason_histogram(),
            "evidence_sources": list(report.evidence_sources),
            "motion_measured": bool(report.motion_measured),
            "thresholds": report.thresholds,
            "face_unresolved_crossings": list(report.unresolved_crossings),
        },
        warnings=list(report.warnings),
        processing_ms=0,
    )
    db.commit()
    _emit(ws.id, EVENT_ACTIVE_SPEAKER_MAPPED,
          f"media-intel active speaker: {report.resolved} resolved, "
          f"{report.unresolved} unresolved",
          user_id=user.id, run_id=row.id, asset_id=asset.id,
          resolved=report.resolved, unresolved=report.unresolved,
          reasons=report.reason_histogram(),
          evidence=list(report.evidence_sources), job_kind=JOB_ACTIVE_SPEAKER)
    return {
        "run": runs_service.run_dto(row),
        "cache_hit": False,
        "items": [r.to_dict() for r in items],
        "evidence_sources": list(report.evidence_sources),
        "unresolved_crossings": list(report.unresolved_crossings),
        "warnings": list(report.warnings),
        "thresholds": report.thresholds,
        "job_kind": JOB_ACTIVE_SPEAKER,
    }


@visual_router.get("/active-speaker/{run_id}", summary="Active-speaker rows for one run")
@_guard()
def list_active_speaker(
    run_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    row = _load_run(db, ws, run_id, "active_speaker")
    items = _speaker_items(db, ws.id, row.id)
    return {
        "run": runs_service.run_dto(row),
        "items": items,
        "resolved": sum(1 for i in items if i["status"] == speaker_engine.STATUS_RESOLVED),
        "unresolved": sum(1 for i in items
                          if i["status"] == speaker_engine.STATUS_UNRESOLVED),
    }


def _speaker_items(db: Session, workspace_id: str, run_id: str) -> list[dict]:
    """Stored mapping rows as DTOs, in media time."""
    rows = speaker_engine.list_rows(db, workspace_id, run_id)
    return [speaker_engine.row_dto(row) for row in rows]


def _source_run(
    db: Session, ws: Workspace, run_id: str | None, kind: str, asset_id: str
):
    """Explicit run id (validated) or the newest COMPLETED run of ``kind``.

    An explicit foreign id is a 404; an absent one falls back to the newest
    matching run for this asset, so a caller does not have to track run ids.
    """
    if run_id:
        row = runs_service.get_run(db, ws.id, run_id)
        if row is None or str(row.kind or "") != kind or row.asset_id != asset_id:
            raise HTTPException(status_code=404, detail=f"{kind} run not found")
        return row
    return speaker_engine.latest_run_of_kind(db, ws.id, asset_id, kind)


__all__ = [
    "EVENT_ACTIVE_SPEAKER_MAPPED",
    "EVENT_MASKS_READY",
    "MUTATION_CAPABILITY",
    "NEW_EVENT_KINDS",
    "visual_router",
]
