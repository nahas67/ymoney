"""Media-intelligence API -- providers + runs (Work 12 Lane A) -- contracts §14.

Mounted once by the orchestrator in ``api/v1/__init__.py``::

    GET  /workspaces/{ws}/media-intel/providers          viewer
    GET  /workspaces/{ws}/media-intel/providers/{key}    viewer
    GET  /workspaces/{ws}/media-intel/runs               viewer
    GET  /workspaces/{ws}/media-intel/runs/{id}          viewer
    POST /workspaces/{ws}/media-intel/runs/{id}/cancel   member + edit cap
    POST /workspaces/{ws}/media-intel/runs/{id}/retry    member + edit cap

Only the two surfaces Lane A owns live here. The capability/alignment, speaker,
audio, visual, reframe and QC routes of contracts §14 belong to the later lanes
(``api/v1/media_intel_*.py``) and are NOT duplicated.

Rules this module keeps (contracts §14 + the Work 11 conventions):

* Workspace scoping on every route: a foreign run id is **404**, never 403, so
  an id from another workspace is indistinguishable from a missing one. The 404
  is raised BEFORE the capability check, so it can never be used as an oracle.
* Floors are the existing ``require_workspace_role`` dependencies; the
  project-scoped ``edit_timeline`` capability from ``services/project_auth``
  (an existing name from the locked Work 11 matrix) only NARROWS the member
  floor for mutations. Reads stay on the viewer floor.
* Provider availability is reported from the registry probe -- the API never
  guesses that a capability exists. A dark capability shows its reason, never a
  fake success (contracts §15).
* ``{"items": [...]}`` envelopes, hand-written dict DTOs, short ``HTTPException``
  details, and a generic 500 that logs the detail server-side only.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db import get_db
from app.engine.intel import registry as intel_registry
from app.models import User, Workspace
from app.services import media_intel_runs as runs_service
from app.services.auth_service import get_current_user, require_workspace_role
from app.services.project_auth import assert_capability

media_intel_router = APIRouter(prefix="/workspaces/{workspace_id}/media-intel",
                               tags=["media-intel"])
logger = logging.getLogger("ymoney.media_intel")

#: existing project capability a run mutation needs (Work 11 matrix; NOT a new
#: name -- project_auth would reject anything else with 422)
MUTATION_CAPABILITY = "edit_timeline"


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
                logger.exception("media-intel route failed: %s",
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


def _commercial_mode() -> bool:
    """Workspace-wide commercial flag (contracts §1.4), read defensively."""
    return bool(getattr(settings, "commercial_mode", False))


def _load_run(db: Session, ws: Workspace, run_id: str):
    """Workspace-scoped run fetch -> 404 for foreign/missing (never 403)."""
    row = runs_service.get_run(db, ws.id, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return row


# ---------------------------------------------------------------------------
# endpoints -- providers (contracts §14)
# ---------------------------------------------------------------------------


@media_intel_router.get("/providers", summary="Provider health, capabilities, license")
@_guard()
def list_intel_providers(
    ws: Workspace = Depends(require_workspace_role("viewer")),
):
    commercial = _commercial_mode()
    items = intel_registry.list_providers(commercial_mode=commercial)
    return {
        "items": items,
        "kinds": list(intel_registry.CAPABILITY_KINDS),
        "commercial_mode": commercial,
        "available": sorted(
            p["key"] for p in items if p["health"]["available"]
        ),
    }


@media_intel_router.get("/providers/{key}", summary="One provider detail")
@_guard()
def get_intel_provider(
    key: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
):
    provider = intel_registry.get_provider(str(key or ""))
    if str(key or "") not in intel_registry.known_keys():
        raise HTTPException(status_code=404, detail="provider not found")
    payload = provider.to_dict()
    payload["kind"] = provider.kind
    payload["chain"] = list(intel_registry.chain_for(provider.kind))
    if _commercial_mode():
        payload["commercial_blocked"] = bool(intel_registry.license_block_reason(provider))
    return payload


# ---------------------------------------------------------------------------
# endpoints -- runs (contracts §14)
# ---------------------------------------------------------------------------


@media_intel_router.get("/runs", summary="List intelligence runs")
@_guard()
def list_intel_runs(
    kind: str | None = Query(default=None, max_length=40),
    status: str | None = Query(default=None, max_length=20),
    asset_id: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=100, ge=1, le=500),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    items = runs_service.list_runs(db, ws.id, kind=kind, status=status,
                                   asset_id=asset_id, limit=limit)
    return {"items": items}


@media_intel_router.get("/runs/{run_id}", summary="Run detail + manifest")
@_guard()
def get_intel_run(
    run_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    row = _load_run(db, ws, run_id)
    return runs_service.run_dto(row)


@media_intel_router.post("/runs/{run_id}/cancel", summary="Cancel a PENDING/RUNNING run")
@_guard(value_error=409)
def cancel_intel_run(
    run_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _load_run(db, ws, run_id)  # 404 first: never a capability oracle
    _check(db, ws, user, capability=MUTATION_CAPABILITY,
           target_type="asset", target_id=row.asset_id)
    runs_service.cancel_run(db, row)
    db.commit()
    return runs_service.run_dto(row)


@media_intel_router.post("/runs/{run_id}/retry", summary="Retry FAILED/CANCELLED/UNAVAILABLE")
@_guard(value_error=409)
def retry_intel_run(
    run_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = _load_run(db, ws, run_id)
    _check(db, ws, user, capability=MUTATION_CAPABILITY,
           target_type="asset", target_id=row.asset_id)
    runs_service.retry_run(db, row)
    db.commit()
    return runs_service.run_dto(row)


__all__ = ["MUTATION_CAPABILITY", "media_intel_router"]
