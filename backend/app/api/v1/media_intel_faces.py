"""Face-tracking API -- ``/media-intel/face-tracks`` (Work 12 Lane E).

Contracts §8 + §14. Mounted once by the orchestrator in
``api/v1/__init__.py``::

    POST /workspaces/{ws}/media-intel/face-tracks            member + edit cap
    GET  /workspaces/{ws}/media-intel/face-tracks/{run_id}   viewer
    GET  /workspaces/{ws}/media-intel/face-tracks            viewer (listing)

Rules this module keeps:

* **Honest unavailability.** ``POST`` resolves the ``face_tracking`` chain
  through the registry. With no detector installed the run is written in the
  terminal ``UNAVAILABLE`` state with the provider's reason -- there is no
  synthetic "0 faces, success!" path.
* **Anonymous only.** A track is a session-local ``FT_00`` label. The DTO has no
  name, alias, gender, age or identity field; operator labels live in
  ``speaker_aliases`` (Lane B) and never here. Nothing infers who a face is.
* **Workspace scoping.** Every lookup is filtered on ``workspace_id``; a foreign
  asset or run id is **404**, never 403, and the 404 is raised BEFORE the
  capability check so it cannot be used as an oracle.
* **Emission rule (contracts §3).** ``record_event``/``track_cost`` open their
  own session, so this module never calls them mid-transaction: pending
  emissions are collected and fired only after ``db.commit()``
  (``engine/collab/reviews.py`` pattern). Lane A's run service already commits
  before emitting its own events.
* ``{items: [...]}`` envelopes, hand-written dict DTOs, short ``HTTPException``
  details and a generic 500 that logs server-side only.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db import get_db
from app.engine.intel import face_tracking as ft
from app.engine.intel import registry as intel_registry
from app.engine.intel.base import (
    IntelRequest,
    ProviderCancelled,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.models import MediaAsset, MediaIntelRun, User, Workspace
from app.models.media_intel import FaceTrack as FaceTrackRow
from app.models.media_intel import FaceTrackSample
from app.services import media_intel_runs as runs_service
from app.services.auth_service import get_current_user, require_workspace_role
from app.services.project_auth import assert_capability
from app.services.storage import STORAGE_ROOT

media_intel_faces_router = APIRouter(
    prefix="/workspaces/{workspace_id}/media-intel", tags=["media-intel-faces"]
)
logger = logging.getLogger("ymoney.media_intel")

#: existing project capability a mutation needs (Work 11 matrix -- NOT a new
#: name; ``project_auth.assert_capability`` rejects anything else with 422)
MUTATION_CAPABILITY = "edit_timeline"

#: capability kind + registry key this router serves
KIND = "face_tracking"
PROVIDER_KEY = "mediapipe_faces"

#: activity-feed event kinds this lane adds on top of Lane A's run events.
#: NEW kinds -> the orchestrator must whitelist them in
#: ``services/webhooks.py::WEBHOOK_EVENTS`` or they are silently dropped.
EVENT_FACE_TRACKS = "MEDIA_INTEL_FACE_TRACKS"

#: sample cap the route enforces (contracts §8 default)
MAX_SAMPLES_PER_TRACK = ft.DEFAULT_MAX_SAMPLES
#: ceiling on the listing
MAX_LIST_LIMIT = 200


# ---------------------------------------------------------------------------
# error policy (mirrors api/v1/exports.py / api/v1/media_intel.py)
# ---------------------------------------------------------------------------


def _short(exc: BaseException, limit: int = 180) -> str:
    """One-line, truncated message for deliberate domain errors."""
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:limit]


def _guard(value_error: int = 422):
    """Uniform error policy for one route body."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(
                    status_code=value_error, detail=_short(exc)
                ) from None
            except Exception:  # noqa: BLE001 -- deliberate catch-all at the edge
                logger.exception(
                    "media-intel face-track route failed: %s",
                    getattr(fn, "__name__", fn),
                )
                raise HTTPException(status_code=500, detail="internal error") from None

        return run

    return decorate


def _check(db: Session, ws: Workspace, user: User, **kwargs: Any) -> None:
    """``assert_capability`` at the route edge; bad args -> 422, 403/404 pass."""
    try:
        assert_capability(db, ws, user, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_short(exc)) from None


def _commercial_mode() -> bool:
    """Workspace-wide commercial flag (contracts §1.4), read defensively."""
    return bool(getattr(settings, "commercial_mode", False))


def _load_asset(db: Session, ws: Workspace, asset_id: str) -> MediaAsset:
    """Workspace-scoped asset fetch -> 404 for foreign/missing."""
    row = db.get(MediaAsset, str(asset_id or ""))
    if row is None or row.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="asset not found")
    return row


def _asset_media_path(ws: Workspace, asset: MediaAsset) -> Path:
    """Absolute path of an asset's media file, boundary-checked (never a client path).

    Mirrors ``providers/video_engine/timeline_render.py``: the stored key is
    untrusted, so it is resolved inside ``STORAGE_ROOT/<workspace>`` only.
    """
    key = str(asset.storage_key or "").strip()
    if not key:
        raise HTTPException(status_code=422, detail="asset has no stored media file")
    root = (STORAGE_ROOT / ws.id).resolve()
    try:
        candidate = (root / key.lstrip("/")).resolve()
        candidate.relative_to(root)
    except (OSError, ValueError):
        raise HTTPException(status_code=422, detail="asset media path is not managed") from None
    if not candidate.exists():
        raise HTTPException(
            status_code=422, detail="asset media file is not present on this host"
        )
    return candidate


def _load_track_run(db: Session, ws: Workspace, run_id: str) -> MediaIntelRun:
    """Workspace-scoped face-track run fetch -> 404 for foreign/missing/wrong kind."""
    row = runs_service.get_run(db, ws.id, run_id)
    if row is None or str(row.kind or "") != KIND:
        raise HTTPException(status_code=404, detail="face-track run not found")
    return row


def _samples_for(db: Session, run_id: str, row_id: str) -> list[FaceTrackSample]:
    return list(
        db.scalars(
            select(FaceTrackSample)
            .where(
                FaceTrackSample.run_id == run_id,
                FaceTrackSample.track_id == row_id,
            )
            .order_by(FaceTrackSample.t_s.asc())
        ).all()
    )


def _track_items(db: Session, run: MediaIntelRun) -> list[dict]:
    """Every persisted track of a run with its samples, anonymous fields only.

    ``unresolved_crossing`` has no column in the Lane A schema, so it is
    reconstructed honestly from the run manifest the same pass wrote.
    """
    crossings = set((run.metrics_json or {}).get("unresolved_crossings") or ())
    rows = db.scalars(
        select(FaceTrackRow)
        .where(FaceTrackRow.run_id == run.id)
        .order_by(FaceTrackRow.start_s.asc(), FaceTrackRow.track_id.asc())
    ).all()
    items: list[dict] = []
    for row in rows:
        item = ft.track_row_dto(row, _samples_for(db, run.id, row.id))
        item["unresolved_crossing"] = str(row.track_id or "") in crossings
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------


class FaceTrackRequest(BaseModel):
    """``POST /face-tracks`` body. Every field is a bounded, honest parameter."""

    asset_id: str = Field(min_length=1, max_length=64)
    #: sampled frames per second (contracts §8 default 2)
    sample_fps: float = Field(default=ft.DEFAULT_SAMPLE_FPS, ge=ft.MIN_SAMPLE_FPS,
                               le=ft.MAX_SAMPLE_FPS)
    #: per-track sample cap; reaching it sets ``truncated``
    max_samples_per_track: int = Field(default=MAX_SAMPLES_PER_TRACK,
                                       ge=ft.MIN_MAX_SAMPLES, le=ft.MAX_MAX_SAMPLES)
    #: bypass the run cache and recompute
    force: bool = False


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@media_intel_faces_router.post("/face-tracks", summary="Track anonymous faces in a video")
@_guard()
def create_face_tracks(
    body: FaceTrackRequest,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Detect + track faces for one asset; the source media is never modified.

    Returns the run DTO plus the tracks. With no detector installed the run ends
    terminal ``UNAVAILABLE`` and carries the provider's reason.
    """
    asset = _load_asset(db, ws, body.asset_id)  # 404 first: never a capability oracle
    _check(db, ws, user, capability=MUTATION_CAPABILITY,
           target_type="asset", target_id=asset.id)

    params = {
        "sample_fps": float(body.sample_fps),
        "max_samples_per_track": int(body.max_samples_per_track),
    }
    provider, reasons = intel_registry.resolve(KIND, commercial_mode=_commercial_mode())
    provider_key = provider.key if provider is not None else PROVIDER_KEY

    created = runs_service.create_run(
        db, ws, kind=KIND, asset=asset, provider_key=provider_key,
        model_version=provider.health().version if provider is not None else "",
        params=params, force=bool(body.force), requested_by=user.id,
    )
    if created.get("cache_hit"):
        # a prior COMPLETED run answered this exact request: no work, no event
        return {"run": created, "items": [], "cache_hit": True}

    run = runs_service.get_run(db, ws.id, created["id"])
    if run is None:  # pragma: no cover - create_run always returns the row
        raise HTTPException(status_code=500, detail="run could not be created")

    if provider is None:
        reason = "; ".join(f"{k}: {v}" for k, v in reasons.items()) or "no provider configured"
        runs_service.unavailable_run(db, run, reason)
        db.commit()
        return {"run": runs_service.run_dto(run), "items": [], "cache_hit": False}

    media_path = _asset_media_path(ws, asset)
    runs_service.start_run(db, run)
    request = IntelRequest(
        workspace_id=ws.id, asset_id=asset.id, storage_path=str(media_path),
        params=params, asset_checksum=str(asset.checksum or ""), run_id=run.id,
    )
    try:
        result = provider.run(
            request,
            progress=lambda pct: setattr(run, "progress", max(0, min(99, int(pct * 100)))),
            should_cancel=lambda: bool(run.cancel_requested),
            deadline=None,
        )
    except ProviderUnavailable as exc:
        runs_service.unavailable_run(db, run, _short(exc, 500))
        db.commit()
        return {"run": runs_service.run_dto(run), "items": [], "cache_hit": False}
    except ProviderCancelled:
        runs_service.cancel_run(db, run)
        db.commit()
        return {"run": runs_service.run_dto(run), "items": [], "cache_hit": False}
    except ProviderTimeout as exc:
        runs_service.fail_run(db, run, "TIMEOUT")
        db.commit()
        return {"run": runs_service.run_dto(run), "items": [], "cache_hit": False,
                "reason": _short(exc)}
    except Exception:
        logger.exception("face-track provider run failed for run %s", run.id)
        runs_service.fail_run(db, run, "PROVIDER_FAILED")
        db.commit()
        return {"run": runs_service.run_dto(run), "items": [], "cache_hit": False,
                "reason": "provider failed"}

    if not getattr(result, "ok", False):
        runs_service.fail_run(db, run, "PROVIDER_FAILED")
        db.commit()
        return {"run": runs_service.run_dto(run), "items": [], "cache_hit": False,
                "reason": str(result.error or "provider returned no result")[:180]}

    payload = (result.artifacts.get("face_tracks") or {}).get("payload") or {}
    persisted = _persist_payload(
        db, run=run, ws=ws, asset=asset, payload=payload,
    )
    metrics = dict(result.metrics or {})
    metrics.update(persisted)
    runs_service.complete_run(
        db, run, metrics=metrics, warnings=list(result.warnings or []),
        processing_ms=int(metrics.get("elapsed_ms") or 0),
        gpu_ms=int(metrics.get("gpu_ms") or 0), cost_micros=int(metrics.get("cost_micros") or 0),
    )
    db.commit()
    _emit_face_track_event(ws.id, run, persisted)
    return {
        "run": runs_service.run_dto(run),
        "items": _track_items(db, run),
        "cache_hit": False,
    }


def _persist_payload(
    db: Session,
    *,
    run: MediaIntelRun,
    ws: Workspace,
    asset: MediaAsset,
    payload: dict,
) -> dict:
    """Rebuild :class:`ft.FaceTrack` objects from the provider payload and store them.

    The provider returns plain JSON-serialisable data (a provider boundary must
    not hand ORM rows or live objects to the service layer), so the tracks are
    rehydrated here through the same :func:`ft.normalize_detection` + add_sample
    path production uses.
    """
    tracks: list[ft.FaceTrack] = []
    crossings: list[str] = []
    for item in payload.get("tracks") or ():
        track = ft.FaceTrack(track_id=str(item.get("track_id") or ""))
        for sample in item.get("samples") or ():
            det = ft.normalize_detection(
                {
                    "x": sample.get("x"),
                    "y": sample.get("y"),
                    "w": sample.get("w"),
                    "h": sample.get("h"),
                    "confidence": sample.get("confidence"),
                    "landmarks": sample.get("landmarks"),
                },
                width=1_000_000_000,
                height=1_000_000_000,
            )
            if det is None:
                continue
            track.add_sample(
                float(sample.get("t_s") or 0.0), det,
                max_samples=max(1, len(item.get("samples") or ())),
            )
        if not track.samples:
            continue
        track.reentry_count = int(item.get("reentry_count") or 0)
        track.truncated = bool(item.get("truncated"))
        track.unresolved_crossing = bool(item.get("unresolved_crossing"))
        if track.unresolved_crossing:
            crossings.append(track.track_id)
        tracks.append(track)
    summary = ft.persist_tracks(
        db, run_id=run.id, workspace_id=ws.id, asset_id=asset.id, tracks=tracks
    )
    metrics = dict(run.metrics_json or {})
    metrics["unresolved_crossings"] = crossings
    run.metrics_json = metrics
    return summary


def _emit_face_track_event(workspace_id: str, run: MediaIntelRun, summary: dict) -> None:
    """Activity-feed event for the written tracks -- AFTER the commit.

    ``record_event`` opens its own session, so this must never run inside the
    caller's write transaction (contracts §3). Best effort: telemetry never
    decides a run's outcome.
    """
    try:
        from app.services.events import record_event

        record_event(
            workspace_id,
            EVENT_FACE_TRACKS,
            f"media-intel face tracking wrote {summary.get('tracks', 0)} anonymous tracks",
            level="info",
            source="media_intel",
            data={
                "run_id": run.id,
                "kind": KIND,
                "provider": str(run.provider_key or ""),
                "tracks": summary.get("tracks", 0),
                "samples": summary.get("samples", 0),
                "truncated_tracks": summary.get("truncated_tracks", []),
                "reentry_total": summary.get("reentry_total", 0),
                "unresolved_crossings": summary.get("unresolved_crossings", []),
            },
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never break a run
        logger.warning("face-track event failed: %s", type(exc).__name__)


@media_intel_faces_router.get("/face-tracks/{run_id}", summary="Face tracks of one run")
@_guard()
def get_face_tracks(
    run_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    run = _load_track_run(db, ws, run_id)
    return {
        "run": runs_service.run_dto(run),
        "items": _track_items(db, run),
        "cache_hit": False,
    }


@media_intel_faces_router.get("/face-tracks", summary="List face-track runs")
@_guard()
def list_face_tracks(
    asset_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=MAX_LIST_LIMIT),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    runs = runs_service.list_runs(
        db, ws.id, kind=KIND, asset_id=asset_id, limit=limit
    )
    return {"items": runs}


__all__ = [
    "EVENT_FACE_TRACKS",
    "KIND",
    "MAX_SAMPLES_PER_TRACK",
    "MUTATION_CAPABILITY",
    "PROVIDER_KEY",
    "FaceTrackRequest",
    "media_intel_faces_router",
]
