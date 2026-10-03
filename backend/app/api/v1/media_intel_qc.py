"""Media-intelligence QC API (Work 12 Lane H) -- contracts §13/§14.

Mounted once by the orchestrator in ``api/v1/__init__.py``::

    GET  /workspaces/{ws}/media-intel/qc/{run_id}           viewer
    POST /workspaces/{ws}/media-intel/qc/{run_id}/run        member + edit_timeline
    POST /workspaces/{ws}/media-intel/qc/{run_id}/override   member + edit_project

Only the QC surface this lane owns lives here; the providers/runs routes are
Lane A's (``api/v1/media_intel.py``) and the capability/speaker/audio/visual
routes belong to the sibling lanes (``api/v1/media_intel_*.py``). Nothing is
duplicated.

Rules this module keeps (contracts §14 + the Work 11 conventions):

* Workspace scoping on every route: a foreign run id is **404**, never 403, and
  the 404 happens BEFORE the capability check so it can never be used as an
  oracle. Generic 500s, ``{"items": [...]}`` envelopes, short details.
* Capability names come from the LOCKED Work 11 matrix
  (``services/project_auth.py``): ``edit_timeline`` (owner/admin/editor) to run
  QC, ``edit_project`` (owner/admin) to override a FAIL verdict. No new name is
  invented -- ``assert_capability`` would reject one with 422.
* **Emission ordering** (contracts §3): the QC engine SURFACES its activity
  events in the payload, this route commits, and only then emits. A
  mid-transaction ``record_event`` opens a second connection and stalls (or
  drops) the event on SQLite.
* A FAIL verdict cannot be applied by accident: the engine's
  ``assert_qc_allows_apply`` is the gate, and an override is a separate,
  attributable route (who + why are both required).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.engine.intel import qc as intel_qc
from app.models import User, Workspace
from app.services.auth_service import get_current_user, require_workspace_role
from app.services.media_intel_runs import get_run
from app.services.project_auth import assert_capability

media_intel_qc_router = APIRouter(
    prefix="/workspaces/{workspace_id}/media-intel", tags=["media-intel-qc"]
)
logger = logging.getLogger("ymoney.media_intel")

#: existing Work 11 capabilities only (project_auth.CAPABILITIES is the lock)
RUN_CAPABILITY = "edit_timeline"
#: a QC override is a governance action: the closest EXISTING "manage" name in
#: the locked matrix is edit_project (owner/admin). contracts §14's abstract
#: "manage" is deliberately not invented as a new capability.
OVERRIDE_CAPABILITY = "edit_project"


# ---------------------------------------------------------------------------
# error policy (copied from api/v1/exports.py, never re-invented)
# ---------------------------------------------------------------------------


def _short(exc: BaseException, limit: int = 180) -> str:
    """One-line, truncated message for a deliberate domain error."""
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:limit]


def _guard(value_error: int = 422):
    """Uniform error policy for one route body.

    ``HTTPException`` passes through (401/403/404/409/422); ``ValueError``
    (including ``QCApplyBlocked``) becomes ``value_error`` with a short detail;
    anything else is logged server-side and surfaced as a generic 500.
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
                logger.exception("media-intel qc route failed: %s",
                                 getattr(fn, "__name__", fn))
                raise HTTPException(status_code=500, detail="internal error") from None

        return run

    return decorate


def _check(db: Session, ws: Workspace, user: User, **kwargs) -> None:
    """``assert_capability`` at the route edge; a bad capability -> 422."""
    try:
        assert_capability(db, ws, user, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_short(exc)) from None


def _load_run(db: Session, ws: Workspace, run_id: str):
    """Workspace-scoped run fetch -> 404 for foreign/missing (never 403)."""
    row = get_run(db, ws.id, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return row


def _emit(workspace_id: str, events: list[dict] | None) -> list[str]:
    """Emit the engine's surfaced events -- AFTER the caller's commit.

    Best-effort by design: an activity-feed failure must never fail the QC
    verdict the operator just asked for. The kinds are reported to the
    orchestrator for the ``webhooks.WEBHOOK_EVENTS`` allowlist.
    """
    kinds: list[str] = []
    for event in events or []:
        kind = str(event.get("kind") or "")
        if not kind:
            continue
        try:
            from app.services.events import record_event

            record_event(
                workspace_id, kind, str(event.get("message") or ""),
                level=str(event.get("level") or "info"),
                source=str(event.get("source") or "media_intel"),
                data=dict(event.get("data") or {}),
            )
            kinds.append(kind)
        except Exception as exc:  # noqa: BLE001 -- telemetry must not break QC
            logger.warning("qc event %s failed: %s", kind, type(exc).__name__)
    return kinds


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


@media_intel_qc_router.get("/qc/{run_id}", summary="QC verdicts for a run")
@_guard()
def get_intel_qc(
    run_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    _load_run(db, ws, run_id)  # 404 first: never a capability oracle
    rows = intel_qc.list_results(db, ws.id, run_id)
    items = [intel_qc.qc_dto(row) for row in rows]
    latest: dict[str, Any] = {}
    for item in items:  # items are newest-first
        latest.setdefault(str(item["kind"]), item)
    return {
        "items": items,
        "latest": latest,
        "run_id": run_id,
        "verdicts": list(intel_qc.QC_KINDS),
    }


@media_intel_qc_router.post("/qc/{run_id}/run", summary="Run audio/visual QC")
@_guard()
def post_intel_qc_run(
    run_id: str,
    payload: dict = Body(default_factory=dict),
    ws: Workspace = Depends(require_workspace_role("member")),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    run = _load_run(db, ws, run_id)
    _check(db, ws, user, capability=RUN_CAPABILITY,
           target_type="asset", target_id=run.asset_id)
    kind = str((payload or {}).get("kind") or "").strip().lower()
    if kind and kind not in intel_qc.QC_KINDS:
        raise HTTPException(status_code=422, detail=f"unknown qc kind {kind!r}")
    if not kind:
        kind = "visual" if str(run.kind or "") in intel_qc.VISUAL_RUN_KINDS else "audio"
    max_removal_ratio = (payload or {}).get("max_removal_ratio")
    if kind == "audio":
        result = intel_qc.run_audio_qc(
            db, ws, run,
            max_removal_ratio=float(max_removal_ratio) if max_removal_ratio else None,
        )
    else:
        result = intel_qc.run_visual_qc(db, ws, run)
    db.commit()  # durable verdict first, telemetry second (contracts §3)
    events = _emit(ws.id, result.get("events"))
    result.pop("events", None)
    result["events_emitted"] = events
    return result


@media_intel_qc_router.post("/qc/{run_id}/override",
                            summary="Record an attributable QC override")
@_guard(value_error=422)
def post_intel_qc_override(
    run_id: str,
    payload: dict = Body(default_factory=dict),
    ws: Workspace = Depends(require_workspace_role("member")),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    run = _load_run(db, ws, run_id)
    _check(db, ws, user, capability=OVERRIDE_CAPABILITY,
           target_type="asset", target_id=run.asset_id)
    # W11.5 B-F1: the capability above is vacuous on unlinked assets (the
    # route floor would be the only gate), so governance needs admin+.
    from app.services.project_auth import assert_workspace_admin
    assert_workspace_admin(db, ws, user, action="recording a QC override")
    kind = str((payload or {}).get("kind") or "").strip()
    result = intel_qc.get_result(db, ws.id, run_id, kind or None)
    if result is None:
        raise HTTPException(status_code=404, detail="qc result not found")
    checks = (payload or {}).get("checks")
    override = intel_qc.record_override(
        db, ws, result,
        by_user=user.id,
        reason=str((payload or {}).get("reason") or ""),
        checks=[str(c) for c in checks] if isinstance(checks, list) else None,
    )
    db.commit()  # the attributable record first, the event second
    events = _emit(ws.id, override.get("events"))
    override.pop("events", None)
    override["events_emitted"] = events
    # proof that a FAIL verdict is now apply-able (and by whom)
    override["apply_decision"] = intel_qc.assert_qc_allows_apply(
        db, ws, {"result_id": override["id"]}, override=True
    )
    return override


__all__ = ["OVERRIDE_CAPABILITY", "RUN_CAPABILITY", "media_intel_qc_router"]
