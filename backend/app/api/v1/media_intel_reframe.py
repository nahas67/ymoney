"""Smart-reframe + background API (Work 12 Lane G) -- contracts 14.

Mounted once by the orchestrator in ``api/v1/__init__.py``::

    POST   /workspaces/{ws}/media-intel/reframe                  member + edit_timeline
    GET    /workspaces/{ws}/media-intel/reframe/{plan_id}        viewer
    PATCH  /workspaces/{ws}/media-intel/reframe/keyframes/{id}   member + edit_timeline
    POST   /workspaces/{ws}/media-intel/background               member + edit_timeline

The LANE-A router (``api/v1/media_intel.py``) already owns ``/providers`` and
``/runs``; contracts 14's reframe + background routes live here so no two files
claim the same paths.

Rules this module keeps (contracts 14 + the Work 11 conventions):

* Workspace scoping on every route: a foreign plan/asset id is **404**, never
  403, and the 404 is raised BEFORE the capability check so it can never be used
  as an oracle.
* Floors are the existing ``require_workspace_role`` dependencies; the
  project-scoped ``edit_timeline`` capability (an EXISTING ``project_auth``
  name -- a new one would be rejected with 422) only NARROWS the member floor
  for mutations. Reads stay on the viewer floor.
* **Emission ordering (contracts 3).** The engine layer RETURNS its events
  instead of publishing them: ``record_event``/``track_cost`` each open their own
  session, so calling them mid-transaction deadlocks the SQLite writer. Every
  route here commits FIRST and publishes SECOND, and the commit is explicit even
  though the run service also commits.
* No crop is ever baked into a source file. A plan is editable keyframe rows; a
  preview is a NEW derived asset with ``parent_asset_id``.
* An unavailable capability (no segmentation provider => no mask) answers 200
  with ``status='UNAVAILABLE'`` and a reason -- never a fabricated success.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import MediaAsset, ReframePlan, User, Workspace, WorkspaceMember
from app.services.auth_service import get_current_user, require_workspace_role
from app.services.project_auth import assert_capability

media_intel_reframe_router = APIRouter(
    prefix="/workspaces/{workspace_id}/media-intel", tags=["media-intel-reframe"]
)
logger = logging.getLogger("ymoney.media_intel")

#: EXISTING project capability a reframe/background mutation needs (contracts 14
#: conventions: mutate -> edit). NOT a new name -- project_auth rejects anything
#: outside its vocabulary with 422.
MUTATION_CAPABILITY = "edit_timeline"


# ---------------------------------------------------------------------------
# error policy (mirrors api/v1/exports.py + api/v1/media_intel.py)
# ---------------------------------------------------------------------------


def _short(exc: BaseException, limit: int = 180) -> str:
    """One-line, truncated message for deliberate domain errors (never a stack)."""
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:limit]


def _guard(value_error: int = 422):
    """Uniform error policy for one route body.

    ``HTTPException`` passes through (401/403/404/409/422); ``ValueError``
    becomes ``value_error`` with a short detail; a missing keyframe (our own
    ``KeyError``) is a 404; anything else is logged and surfaced as a generic
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
                raise HTTPException(status_code=404, detail=_short(exc)) from None
            except ValueError as exc:
                raise HTTPException(
                    status_code=value_error, detail=_short(exc)
                ) from None
            except Exception:  # noqa: BLE001 -- deliberate catch-all at the API edge
                logger.exception(
                    "media-intel reframe route failed: %s", getattr(fn, "__name__", fn)
                )
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
    if row is None or row.workspace_id != str(ws.id):
        raise HTTPException(status_code=404, detail="asset not found")
    return row


def _require_admin(db: Session, ws: Workspace, user: User) -> None:
    """Override a QC FAIL needs the `admin` floor (contracts 14: override -> manage).

    ``assert_qc_allows_apply`` refuses a FAIL without a recorded, attributable
    override, so the route must not let a plain member claim one. The membership
    is read from the table because the ``Workspace`` row carries no role.
    """
    if bool(getattr(user, "is_superuser", False)):
        return
    member = db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == user.id,
        )
    )
    if member is not None and member.role in (
        WorkspaceMember.ROLE_ADMIN, WorkspaceMember.ROLE_OWNER
    ):
        return
    raise HTTPException(
        status_code=403, detail="overriding a QC FAIL requires the admin role"
    )


def _source_path(ws: Workspace, asset: MediaAsset) -> str:
    """Resolve an asset's on-disk path through the storage boundary.

    Both key conventions the repo writes are tried, each through
    ``managed_path`` so the fail-closed check stays in one place: the
    CWD-relative ``data/videos/<ws>/<file>`` and the workspace-relative
    ``<file>`` (see ``qc.asset_media_path``, which resolves the same two).
    """
    from pathlib import Path

    from app.services.storage import STORAGE_ROOT, managed_path

    key = str(asset.storage_key or "")
    for candidate in (key, str(Path(STORAGE_ROOT) / str(ws.id) / key)):
        resolved = managed_path(str(ws.id), candidate)
        if resolved is not None and resolved.is_file():
            return str(resolved)
    raise HTTPException(
        status_code=422, detail="asset has no resolvable storage path on this host"
    )


def _publish(workspace_id: str, events: list[dict]) -> None:
    """Publish already-committed activity events (contracts 3, after commit).

    Every entry is best-effort: telemetry must never fail a request whose work
    is already durable.
    """
    for event in events or []:
        try:
            from app.services.events import record_event

            record_event(
                str(workspace_id),
                str(event.get("kind") or ""),
                str(event.get("message") or ""),
                level=str(event.get("level") or "info"),
                source="media_intel",
                data=dict(event.get("data") or {}),
            )
        except Exception as exc:  # noqa: BLE001 -- telemetry must not break a route
            logger.warning("reframe event %s failed: %s", event.get("kind"), type(exc).__name__)


# ---------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------


class ReframeCreateBody(BaseModel):
    asset_id: str = Field(min_length=1, max_length=64)
    #: one of TARGET_ASPECTS (validated in the engine, so the vocabulary lives
    #: in exactly one place)
    aspect: str = "9:16"
    #: one of LAYOUTS
    layout: str = "ACTIVE_SPEAKER"
    #: restrict evidence to one face/active-speaker run (Lane F)
    evidence_run_id: str | None = None
    #: normalized (0..1) or source-pixel (x, y)
    focal_point: list[float] | None = None
    safe_area: float | None = None
    max_move_per_s: float | None = None
    transition_s: float | None = None
    ramp_steps: int | None = None
    smoothing: float | None = None
    participants: int | None = None
    grid_rows: int | None = None
    sample_fps: float | None = None
    out_height: int | None = None
    tracks: list[str] = Field(default_factory=list)
    #: render a preview into a NEW derived asset (slow; never touches the source)
    preview: bool = False
    #: APPLY: bake the plan (the preview render). Gated by
    #: ``qc.assert_qc_allows_apply`` -- a FAIL verdict needs an override, which
    #: additionally requires the `admin` role floor.
    apply: bool = False
    override_qc: bool = False


class KeyframePatchBody(BaseModel):
    t_s: float | None = None
    #: crop CENTRE, NORMALISED to the source frame (Lane H contract) -- the
    #: same unit the plan was written in, so an operator nudging a keyframe
    #: cannot silently switch the plan into a unit QC misreads.
    x: float | None = None
    y: float | None = None
    #: ZOOM factor (1.0 = whole frame, >1 tighter); visible width = 1/scale
    scale: float | None = None
    confidence: float | None = None
    reason: str | None = None
    rect: dict | None = None
    #: override a QC FAIL when applying; the route additionally requires `admin`
    override_qc: bool = False


class BackgroundBody(BaseModel):
    asset_id: str = Field(min_length=1, max_length=64)
    #: one of BACKGROUND_OPS
    operation: str = "background_blur"
    mask_kind: str = "PERSON"
    mask_asset_id: str | None = None
    background_asset_id: str | None = None
    blur_strength: int = 8
    aspect: str = "9:16"
    out_height: int | None = None


# ---------------------------------------------------------------------------
# routes -- reframe (contracts 11 + 14)
# ---------------------------------------------------------------------------


@media_intel_reframe_router.post("/reframe", summary="Plan a smart reframe / layout")
@_guard()
def create_reframe(
    body: ReframeCreateBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Compute and persist EDITABLE keyframes. The crop is never baked.

    Synchronous by design: the plan is stdlib geometry over rows that already
    exist, so there is nothing to queue and no model to wait for. A worker
    handler (job kind ``MEDIA_INTEL_REFRAME``) wraps the same engine for the
    batch/large-file path at integration.

    ``apply=true`` is the APPLY path: it is gated by
    :func:`app.engine.intel.qc.assert_qc_allows_apply` through
    :func:`reframe.assert_plan_applies`, so a plan nobody checked (or one QC
    failed) cannot be baked into a rendered asset. The plan is still created
    either way -- refusing to PLAN because QC has not run yet would be a worse
    product than refusing to APPLY.
    """
    from app.engine.intel.reframe import (
        assert_plan_applies,
        build_plan_payload,
        list_keyframes,
        persist_plan,
        plan_dto,
        run_manifest,
        validate_request,
    )
    from app.services import media_intel_runs as runs_service

    # Validate BEFORE anything else: the cache is keyed on the params, so an
    # unvalidated aspect would return a good plan's cached result with 200.
    validate_request(body.aspect, body.layout)
    asset = _load_asset(db, ws, body.asset_id)  # 404 first: never a capability oracle
    _check(db, ws, user, capability=MUTATION_CAPABILITY,
           target_type="asset", target_id=asset.id)

    params: dict[str, Any] = {
        "evidence_run_id": body.evidence_run_id,
        "safe_area": body.safe_area,
        "max_move_per_s": body.max_move_per_s,
        "transition_s": body.transition_s,
        "ramp_steps": body.ramp_steps,
        "smoothing": body.smoothing,
        "participants": body.participants,
        "grid_rows": body.grid_rows,
        "sample_fps": body.sample_fps,
        "tracks": list(body.tracks or []),
    }
    if body.focal_point is not None:
        params["focal_point"] = tuple(body.focal_point)
    options = {k: v for k, v in params.items() if v is not None}
    options["out_height"] = body.out_height
    # aspect + layout are part of the cache key: they change the work, so a 4:5
    # plan must never be served for a 9:16 request (or the reverse)
    options["aspect"] = body.aspect
    options["layout"] = str(body.layout).upper()

    # A run is created FIRST and the plan is bound to it: lane H resolves a plan
    # to its run's QC verdict, so a plan without a run_id can never be gated.
    run_dto = runs_service.create_run(
        db, ws, kind="reframe", asset=asset,
        provider_key="motion_reframe", model_version="reframe.v1", params=options,
    )
    if run_dto.get("cache_hit"):
        from app.engine.intel.reframe import get_plan

        prior = get_plan(db, ws.id, "") or db.scalar(
            select(ReframePlan).where(
                ReframePlan.workspace_id == ws.id,
                ReframePlan.run_id == str(run_dto["id"]),
            )
        )
        if prior is not None:
            rows = list_keyframes(db, prior.id)
            dto = plan_dto(prior, rows)
            db.commit()
            return {**dto, "run": run_dto, "cache_hit": True,
                    "preview": None, "baked": False, "editable": True,
                    "qc": None}

    run = runs_service.get_run(db, ws.id, str(run_dto["id"]))
    runs_service.start_run(db, run)
    payload = build_plan_payload(
        db,
        ws.id,
        asset,
        aspect=body.aspect,
        layout=body.layout,
        params=options,
        run_id=run.id,
        out_height=int(body.out_height or 1920),
    )
    plan = persist_plan(db, ws.id, asset, payload, run_id=run.id)
    rows = list_keyframes(db, plan.id)

    preview: dict | None = None
    qc: dict | None = None
    if body.preview or body.apply:
        from app.engine.intel.reframe import render_preview

        # APPLY GATE: no QC verdict -> blocked (contracts 13). `apply=false`
        # with `preview=true` is a read-only draft render, so it is not gated.
        if body.apply or body.override_qc:
            qc = assert_plan_applies(db, ws, plan, override=bool(body.override_qc))
        preview = render_preview(
            db, ws.id, asset, _source_path(ws, asset), plan, rows
        )

    dto = plan_dto(plan, rows)
    result = {"plan_id": plan.id, **(preview or {})}
    runs_service.complete_run(
        db, run,
        metrics=run_manifest(payload, asset, result=result),
        warnings=list((preview or {}).get("warnings") or []),
        output_asset_id=(preview or {}).get("asset_id"),
        processing_ms=int((preview or {}).get("processing_ms") or 0),
    )
    db.commit()
    db.refresh(plan)
    _publish(
        ws.id,
        [
            {
                "kind": "MEDIA_INTEL_REFRAME_PLAN_CREATED",
                "message": (
                    f"Reframe plan ({body.layout} -> {body.aspect}, "
                    f"{len(rows)} keyframes)"
                ),
                "data": {
                    "plan_id": plan.id,
                    "run_id": run.id,
                    "asset_id": asset.id,
                    "layout": plan.layout,
                    "aspect": plan.aspect,
                    "keyframe_count": len(rows),
                    "strategy": plan.strategy,
                    "levels_used": payload.get("levels_used") or [],
                    "preview": bool(preview and preview.get("rendered")),
                },
            }
        ],
    )
    return {
        **dto,
        "run": runs_service.run_dto(run),
        "preview": preview,
        "qc": qc,
        "baked": False,
        "editable": True,
        "keyframe_anchor": "center",
        "keyframe_units": "normalised",
    }


@media_intel_reframe_router.get("/reframe/{plan_id}", summary="One reframe plan + keyframes")
@_guard()
def get_reframe(
    plan_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    from app.engine.intel.reframe import get_plan, list_keyframes, plan_dto

    plan = get_plan(db, ws.id, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="reframe plan not found")
    return plan_dto(plan, list_keyframes(db, plan.id))


@media_intel_reframe_router.patch(
    "/reframe/keyframes/{keyframe_id}", summary="Edit one keyframe (this is 'editable')"
)
@_guard()
def patch_reframe_keyframe(
    keyframe_id: str,
    body: KeyframePatchBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Move/scale/re-time one keyframe; the previous values are kept in history.

    This is the APPLY path for an operator: it changes the geometry a render
    would use, so a plan QC failed is gated here too (contracts 13). The
    ``x``/``y``/``scale`` units are the plan's own -- normalised centre and zoom
    -- so an edit cannot switch a plan into a unit QC misreads.
    """
    from app.engine.intel.reframe import (
        assert_plan_applies,
        get_plan,
        keyframe_dto,
        update_keyframe,
    )

    patch = {k: v for k, v in body.model_dump(exclude_none=True).items() if v is not None}
    override = bool(patch.pop("override_qc", False))
    try:
        row, entry = update_keyframe(
            db, ws.id, keyframe_id, patch, actor_id=getattr(user, "id", None)
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="keyframe not found") from None
    plan = get_plan(db, ws.id, row.plan_id)
    if plan is not None:
        source = _load_asset(db, ws, plan.source_asset_id)
        _check(db, ws, user, capability=MUTATION_CAPABILITY,
               target_type="asset", target_id=source.id)
        if override:
            _require_admin(db, ws, user)
        qc = assert_plan_applies(db, ws, plan, override=override)
    else:  # pragma: no cover - a keyframe always belongs to a plan
        qc = None
    db.commit()
    db.refresh(row)
    _publish(
        ws.id,
        [
            {
                "kind": "MEDIA_INTEL_REFRAME_KEYFRAME_EDITED",
                "message": f"Keyframe moved to t={entry['after'].get('t_s')}",
                "data": {
                    "plan_id": row.plan_id,
                    "keyframe_id": row.id,
                    "fields": sorted(patch),
                    "by": str(entry.get("by") or ""),
                    "qc_verdict": (qc or {}).get("verdict"),
                },
            }
        ],
    )
    return {**keyframe_dto(row), "history_entry": entry, "qc": qc}


# ---------------------------------------------------------------------------
# routes -- background (contracts 12 + 14)
# ---------------------------------------------------------------------------


@media_intel_reframe_router.post("/background", summary="Background blur / replace / mask")
@_guard()
def post_background(
    body: BackgroundBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Apply a background op when a segmentation provider produced a mask.

    With no usable mask the answer is ``status='UNAVAILABLE'`` with the reason
    and the required chain -- a 200, because the request was understood and the
    honest answer is "not installed", not a 500 and never a fake success.
    """
    from app.engine.intel.reframe import (
        DEFAULT_OUT_HEIGHT,
        background_operation,
        run_manifest,
        target_geometry,
    )
    from app.services import media_intel_runs as runs_service

    asset = _load_asset(db, ws, body.asset_id)
    _check(db, ws, user, capability=MUTATION_CAPABILITY,
           target_type="asset", target_id=asset.id)

    run_dto = runs_service.create_run(
        db, ws, kind="background", asset=asset, provider_key="motion_reframe",
        model_version="reframe.v1",
        params={"operation": body.operation, "mask_kind": body.mask_kind,
                "blur_strength": body.blur_strength, "aspect": body.aspect},
    )
    run = runs_service.get_run(db, ws.id, str(run_dto["id"]))
    runs_service.start_run(db, run)
    result = background_operation(
        db,
        ws.id,
        asset,
        body.operation,
        mask_kind=body.mask_kind,
        mask_asset_id=body.mask_asset_id,
        background_asset_id=body.background_asset_id,
        blur_strength=body.blur_strength,
        aspect=body.aspect,
        out_height=int(body.out_height or DEFAULT_OUT_HEIGHT),
    )
    available = result.get("status") == "COMPLETED"
    payload = {
        "geometry": {},
        "layout": "",
        "aspect": body.aspect,
        "keyframes": [],
        "slots": [],
        "ops": [],
        "levels_used": [],
    }
    if int(asset.width or 0) and int(asset.height or 0):
        payload["geometry"] = target_geometry(
            int(asset.width), int(asset.height), body.aspect,
            out_height=int(body.out_height or DEFAULT_OUT_HEIGHT),
        ).to_dict()
    # `metrics_json` in the nested {source, output} shape QC reads
    runs_service.complete_run(
        db, run,
        metrics=run_manifest(payload, asset, result=result),
        output_asset_id=result.get("output_asset_id") or result.get("asset_id"),
        processing_ms=int(result.get("processing_ms") or 0),
    )
    if not available:
        runs_service.fail_run(db, run, "PROVIDER_UNAVAILABLE")
    db.commit()
    available = result.get("status") == "COMPLETED"
    result = {**result, "run": runs_service.run_dto(run)}
    _publish(
        ws.id,
        [
            {
                "kind": (
                    "MEDIA_INTEL_BACKGROUND_APPLIED"
                    if available
                    else "MEDIA_INTEL_BACKGROUND_UNAVAILABLE"
                ),
                "message": (
                    f"Background {body.operation} applied"
                    if available
                    else f"Background {body.operation} unavailable: "
                    f"{str(result.get('reason') or '')[:120]}"
                ),
                "level": "info" if available else "warning",
                "data": {
                    "asset_id": asset.id,
                    "operation": body.operation,
                    "status": str(result.get("status") or ""),
                    "output_asset_id": result.get("output_asset_id"),
                    "rendered": bool(result.get("rendered")),
                    "reason": str(result.get("reason") or "")[:300],
                },
            }
        ],
    )
    return result


__all__ = ["MUTATION_CAPABILITY", "media_intel_reframe_router"]
