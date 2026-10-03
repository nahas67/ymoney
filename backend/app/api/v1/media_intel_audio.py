"""Audio-intelligence API: ENHANCE only (Work 12 Lane C) -- contracts §6/§14.

Mounted once by the orchestrator in ``api/v1/__init__.py``::

    POST /workspaces/{ws}/media-intel/audio/enhance        member + edit cap
    GET  /workspaces/{ws}/media-intel/audio/enhance/{run}  viewer

Only the enhancement surface lives here. ``POST /audio/silence`` and
``POST /audio/fillers`` belong to the silence/filler lane and are deliberately
NOT duplicated.

Rules kept (contracts §14 + the Work 11 conventions, mirrored from
``api/v1/exports.py`` and ``api/v1/media_intel.py``):

* **Workspace scoping on every route**; a foreign run/asset id is **404**, never
  403, and the 404 is raised BEFORE the capability check so it can never serve as
  an oracle.
* **Floors are the existing dependencies** -- ``require_workspace_role`` for the
  role, and ``assert_capability`` with the EXISTING capability name
  ``edit_timeline`` for the mutation. No new capability name is invented.
* **Emission after commit (hard).** The engine returns the events it wants in
  ``payload["events"]``; this module publishes them only after ``db.commit()``.
  ``record_event``/``track_cost`` each open their own session, so emitting
  mid-transaction is what produced "OperationalError: database is locked" in
  Lane A's first run. The engine never emits.
* ``{"items": [...]}``/dict DTOs, short ``HTTPException`` details, a generic 500
  that logs the detail server-side only.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db import get_db
from app.engine.intel import audio_enhance
from app.models import MediaAsset, User, Workspace
from app.services import media_intel_runs as runs_service
from app.services.auth_service import get_current_user, require_workspace_role
from app.services.project_auth import assert_capability

media_intel_audio_router = APIRouter(
    prefix="/workspaces/{workspace_id}/media-intel", tags=["media-intel-audio"]
)
logger = logging.getLogger("ymoney.media_intel")

#: EXISTING project capability an enhancement mutation needs (Work 11 matrix).
MUTATION_CAPABILITY = "edit_timeline"


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------


class EnhanceRequest(BaseModel):
    """``POST /audio/enhance`` body.

    ``stages`` accepts a plain list or a ``{stage: {params}}`` mapping. An
    unknown stage name is a VISIBLE ``skipped`` row in the response, not a 422,
    so a UI typo shows up as a no-op the operator can see.
    """

    asset_id: str = Field(min_length=1, max_length=64)
    stages: list[str] | dict[str, dict] = Field(default_factory=list)
    stage_params: dict[str, dict] = Field(default_factory=dict)
    params: dict = Field(default_factory=dict)
    force: bool = False
    enqueue: bool = False


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
                logger.exception("media-intel audio route failed: %s",
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


def _load_asset(db: Session, ws: Workspace, asset_id: str) -> MediaAsset:
    """Workspace-scoped asset fetch -> 404 for foreign/missing (never 403)."""
    row = db.get(MediaAsset, str(asset_id or ""))
    if row is None or row.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="asset not found")
    return row


def _load_run(db: Session, ws: Workspace, run_id: str):
    """Workspace-scoped run fetch -> 404 for foreign/missing (never 403)."""
    row = runs_service.get_run(db, ws.id, str(run_id or ""))
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return row


def _commercial_mode() -> bool:
    """Workspace-wide commercial flag (contracts §1.4), read defensively."""
    return bool(getattr(settings, "commercial_mode", False))


def _emit_events(workspace_id: str, events: list[dict]) -> list[str]:
    """Publish engine events AFTER the commit, one at a time.

    The engine returns what it wants (it must not emit mid-transaction), so this
    is the only place a ledger write happens for an enhancement run. Failures
    are logged, never raised: a telemetry problem must not fail the request that
    already committed its run.
    """
    published: list[str] = []
    try:
        from app.services.events import record_event
    except Exception as exc:  # noqa: BLE001 - the ledger is not a correctness need
        logger.warning("event service unavailable: %s", type(exc).__name__)
        return published
    for event in events or []:
        if not isinstance(event, dict) or not event.get("kind"):
            continue
        try:
            record_event(
                str(event.get("workspace_id") or workspace_id),
                str(event["kind"]),
                str(event.get("message") or ""),
                level=str(event.get("level") or "info"),
                source=str(event.get("source") or "media_intel"),
                data=dict(event.get("data") or {}),
            )
            published.append(str(event["kind"]))
        except Exception as exc:  # noqa: BLE001 - telemetry never breaks a request
            logger.warning("could not record %s: %s", event.get("kind"), type(exc).__name__)
    return published


def _enrichment(db: Session, ws: Workspace) -> dict:
    """The provider/health context the UI shows next to the Enhance button."""
    pipeline = audio_enhance.AudioEnhancementPipeline(db)
    return {
        "stages": list(audio_enhance.STAGES),
        "stage_statuses": list(audio_enhance.STAGE_STATUSES),
        "job_kind": audio_enhance.JOB_KIND,
        "providers": pipeline.providers_snapshot(),
        "commercial_mode": _commercial_mode(),
    }


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@media_intel_audio_router.post("/audio/enhance", summary="Run the audio enhancement pipeline")
@_guard()
def enhance_audio(
    body: EnhanceRequest,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Apply the requested stages and return the stage matrix + manifest.

    Synchronous by design: the enhancement engine is pure orchestration plus
    measurement (contracts §1.2), so it needs no worker. ``enqueue=true``
    creates the run, hands it to the ``MEDIA_INTEL_ENHANCE`` queue and returns
    the pending run id instead.
    """
    asset = _load_asset(db, ws, body.asset_id)  # 404 first: never a capability oracle
    _check(db, ws, user, capability=MUTATION_CAPABILITY,
           target_type="asset", target_id=asset.id)
    payload = audio_enhance.AudioEnhancementPipeline(db).run(
        ws, asset,
        stages=body.stages,
        stage_params=body.stage_params or None,
        params=body.params or None,
        force=bool(body.force),
        requested_by=user.id,
        enqueue=bool(body.enqueue),
    )
    db.commit()
    published = _emit_events(ws.id, payload.get("events") or [])
    payload["events_published"] = published
    payload["stages_supported"] = list(audio_enhance.STAGES)
    return payload


@media_intel_audio_router.get("/audio/enhance/{run_id}",
                              summary="Enhancement run + stage matrix + manifest")
@_guard()
def get_enhance_run(
    run_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    row = _load_run(db, ws, run_id)
    dto = runs_service.run_dto(row)
    metrics = dict(row.metrics_json or {})
    stages = metrics.get("stages") or []
    return {
        "run": dto,
        "stages": stages,
        "before_after": metrics.get("before_after") or {},
        "claims": metrics.get("claims") or [],
        "manifest": metrics,
        "providers": _enrichment(db, ws)["providers"],
    }


__all__ = ["MUTATION_CAPABILITY", "EnhanceRequest", "media_intel_audio_router"]
