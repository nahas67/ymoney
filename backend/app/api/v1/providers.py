"""Provider maturity API (Work 15.6 §4) — read-only, honest, no secrets.

Mounted once by the orchestrator in ``api/v1/__init__.py``::

    GET /api/v1/provider-maturity                     all records (global)
    GET /api/v1/provider-maturity/summary             counts + what is ready
    GET /api/v1/provider-maturity/tts/qualification   the donor TTS verdicts
    GET /api/v1/workspaces/{ws}/provider-maturity     + credential resolution
    GET /api/v1/workspaces/{ws}/provider-maturity/incidents  paid-job incidents

Every route is a ``GET``. Nothing here configures, probes, or enables anything:
the surface exists so an operator can *see* that a provider is unverified or
blocked before somebody builds a cycle on top of it.

Three properties this module keeps, each of them a way the status could lie:

* **No secret, no fingerprint.** ``resolved_credential_status`` is a state word
  (``CONFIGURED`` / ``NOT_CONFIGURED`` / ``UNRESOLVED``). Not the key, not its
  length, not a prefix, not a digest — a leaked fingerprint makes brute-forcing
  a short key cheap, so it is treated as the secret.
* **No status is inferred at read time.** The registry is data; the API
  serialises it. Health is only probed when the caller explicitly asks, and an
  absent probe reports ``UNKNOWN`` rather than guessing.
* **Nothing is reported ready.** ``production_ready`` is a derived conclusion
  over four axes; the payload also ships ``blockers`` so a reader can see *why*
  rather than trusting a boolean.

Work 15.7 §13 adds one route that is about the past rather than the catalogue:
``GET /workspaces/{ws}/provider-maturity/incidents``. Knowing a provider is
``LIVE_VERIFIED`` tells an operator nothing about whether this workspace has
already been charged for a render whose response was lost, so that is now an
answerable question — and ``SUBMISSION_UNKNOWN`` is reported under its own name,
never folded into ``FAILED``.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.models import CostEntry, Video, Workspace
from app.providers import maturity
from app.services.auth_service import require_workspace_role

provider_maturity_router = APIRouter(tags=["provider-maturity-work15"])
workspace_maturity_router = APIRouter(
    prefix="/workspaces/{workspace_id}/provider-maturity", tags=["provider-maturity-work15"]
)
logger = logging.getLogger("ymoney.providers")


def _short(exc: BaseException, limit: int = 180) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:limit]


def _guard():
    """Uniform error policy mirroring ``api/v1/music.py``."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any):
            try:
                return fn(*args, **kwargs)
            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=_short(exc)) from None
            except Exception:  # noqa: BLE001 — deliberate catch-all at the edge
                logger.exception("provider maturity route failed: %s",
                                 getattr(fn, "__name__", fn))
                raise HTTPException(status_code=500, detail="internal error") from None

        return run

    return decorate


def _records(capability: str, workspace_id: str | None,
             probe: bool, *, resolve_credentials: bool = True) -> list[dict]:
    """Serialise the table, then attach blockers and (optionally) health.

    Credential resolution is opt-in. With ``workspace_id=None`` the resolver
    answers for the *global* scope, and a global answer printed on a
    tenant-free route reads as "this deployment has no key" — which is not what
    it means. So the unscoped routes pass ``resolve_credentials=False`` and the
    rows carry no credential verdict at all.
    """
    records = maturity.list_maturity(capability)
    rows = (maturity.with_resolved_credentials(records, workspace_id)
            if resolve_credentials else [r.to_dict() for r in records])
    for row in rows:
        record = maturity.get_maturity(row["provider"], row["capability"])
        if record is not None:
            row["blockers"] = maturity.production_blockers(record)
            # Health is opt-in: an unprobed row must not read as healthy, and a
            # probe that fails must not read as unhealthy either.
            row["health"] = maturity.probe_health(record) if probe else maturity.HEALTH_UNKNOWN
    return rows


def _reject_unknown_capability(capability: str) -> str:
    if capability and capability not in maturity.capabilities():
        raise HTTPException(
            status_code=422,
            detail=f"unknown capability {capability!r}; pick from "
                   f"{list(maturity.capabilities())}")
    return capability


@provider_maturity_router.get(
    "/provider-maturity",
    summary="Every provider's honest maturity (no credential resolution)",
)
@_guard()
def list_provider_maturity(
    capability: str = Query(default="", max_length=32),
    probe: bool = Query(default=False,
                        description="Opt in to health probes. Off by default: a "
                                    "probe is a network call."),
) -> dict:
    """The full table.

    Workspace-scoped credential resolution is deliberately *not* done here:
    this route takes no workspace, so any credential state it printed would be
    ambiguous between "global" and "not looked at". Use the workspace route for
    that.
    """
    rows = _records(_reject_unknown_capability(capability), None, probe,
                    resolve_credentials=False)
    return {
        "items": rows,
        "count": len(rows),
        "capabilities": list(maturity.capabilities()),
        "states": sorted(maturity.STATES),
        "registry_reviewed_at": maturity.REGISTRY_REVIEWED_AT,
        "note": ("A status is recorded after evidence and is never derived from "
                 "a module importing. live_status=UNVERIFIED means nobody has "
                 "exercised this provider against its real service yet."),
    }


@provider_maturity_router.get(
    "/provider-maturity/summary",
    summary="Counts per state, and what is actually production-ready",
)
@_guard()
def provider_maturity_summary(
    capability: str = Query(default="", max_length=32),
) -> dict:
    """Aggregate view.

    ``production_ready`` is expected to be **empty** for this repository. A
    summary listing a long set of ready providers is itself the bug this
    registry exists to catch, which is why the field is named for what it
    claims rather than hidden inside ``items``.
    """
    summary = maturity.status_summary(
        maturity.list_maturity(_reject_unknown_capability(capability)))
    return {
        **summary,
        "note": ("production_ready is a conclusion over implementation, "
                 "contract, live and commercial status — never a stored label. "
                 "Nothing here is live-verified, so nothing is ready."),
    }


@provider_maturity_router.get(
    "/provider-maturity/tts/qualification",
    summary="TTS adapters: what exists, and what the matrix only proposed",
)
@_guard()
def tts_qualification() -> dict:
    """The TTS verdicts, including the six donor candidates that were never built.

    Exposed because ``MERGE`` in the technology matrix reads as "done" once
    enough time passes. ``merged: false`` on every donor candidate makes the
    distinction unmissable.
    """
    from app.providers.tts_qualification import (
    donor_candidates,
    list_qualifications,
    qualification_labels,
)

    return {
        "implemented": [
            {"provider": q.provider, "label": q.label, "adapter": q.adapter,
             "implementation_status": q.implementation_status,
             "contract_status": q.contract_status,
             "live_status": q.live_status,
             "commercial_status": q.commercial_status,
             "qualification_labels": list(qualification_labels(q)),
             "simulation_only": q.simulation_only,
             "credential_keys": list(q.credential_keys),
             "gaps": list(q.gaps)}
            for q in list_qualifications()
        ],
        "donor_candidates": list(donor_candidates()),
        "probeable": _probeable(),
        "note": ("Contract-tested means an offline test drove the adapter and "
                 "checked it returns TTSResult, honours errors, and labels "
                 "itself. It does not mean the vendor works."),
    }


def _probeable() -> list[str]:
    from app.providers.tts_qualification import probed_providers

    return list(probed_providers())


@workspace_maturity_router.get(
    "",
    summary="Maturity with this workspace's credential resolution",
)
@_guard()
def workspace_provider_maturity(
    capability: str = Query(default="", max_length=32),
    probe: bool = Query(default=False),
    ws: Workspace = Depends(require_workspace_role("viewer")),
) -> dict:
    """The table, plus whether *this* workspace holds each credential.

    Only a state word is returned. The resolver's value never leaves the
    process, and neither does its length or a digest of it.
    """
    _reject_unknown_capability(capability)
    rows = _records(capability, ws.id, probe)
    return {
        "workspace_id": ws.id,
        "items": rows,
        "count": len(rows),
        "resolved_credential_states": sorted(maturity.RESOLVED_CREDENTIAL_STATES),
        "note": ("resolved_credential_status is a state, never a value, a "
                 "length, or a digest. UNRESOLVED means the resolver failed, "
                 "which is not the same as NOT_CONFIGURED."),
        "credential_summary": _credential_summary(rows),
    }


@workspace_maturity_router.get(
    "/summary",
    summary="Counts per state for this workspace's credentials",
)
@_guard()
def workspace_maturity_summary(
    ws: Workspace = Depends(require_workspace_role("viewer")),
) -> dict:
    rows = _records("", ws.id, False)
    return {
        "workspace_id": ws.id,
        **maturity.status_summary(maturity.list_maturity()),
        "credential_summary": _credential_summary(rows),
    }


@workspace_maturity_router.get(
    "/incidents",
    summary="Paid-job incidents: what may have been billed and what to do",
)
@_guard()
def workspace_paid_incidents(
    limit: int = Query(default=50, ge=1, le=200),
    ws: Workspace = Depends(require_workspace_role("viewer")),
) -> dict:
    """Every paid submission this workspace still has to deal with.

    Work 15.7 §13. The maturity table says whether a provider *can* be trusted.
    It says nothing about whether this workspace has already been charged for
    something that went wrong, so an operator reading it had no way to answer
    "did I pay for that failure?".

    Two sources, both already persisted, so no new table:

    * ``videos.submission_state`` (Work 15.5 §7) for a render submit that may
      have been billed -- the durable record of an ambiguous submit;
    * ``cost_entries`` rows marked ``UNKNOWN_EXPOSURE`` for a call that was
      accepted and whose amount nobody can price.

    Two rules the payload enforces rather than leaves to the reader:

    * **``SUBMISSION_UNKNOWN`` is never reported as ``FAILED``.** The two mean
      opposite things to the wallet -- one is a confirmed rejection, the other
      may already be an invoice -- so the state travels verbatim and
      ``display_state`` is a separate field. A UI that maps an incident to
      "Failed" is then visibly wrong rather than quietly wrong.
    * **Retry is offered only when it is proven safe.** ``retry_safe`` comes from
      :class:`~app.services.paid_executor.RetrySafety`, which is SAFE only for a
      provably-undelivered submit. There is no "retry" affordance for an
      ambiguous one, because sending it again is how one incident becomes two
      charges.

    The recommended action is derived by :func:`paid_executor.verdict_for` and
    the record's own ``reconciliation`` field rather than re-derived here, so the
    API, a CLI and the UI cannot answer differently.
    """
    from app.services import paid_executor
    from app.services.paid_jobs import SubmissionState

    items: list[dict] = []

    with workspace_paid_incidents_session() as db:
        rows = db.query(Video).filter(
            Video.workspace_id == ws.id,
            Video.submission_state.in_([
                str(SubmissionState.SUBMISSION_UNKNOWN),
                str(SubmissionState.SUBMISSION_ATTEMPTED),
            ]),
        ).order_by(Video.updated_at.desc()).limit(limit).all()
        for row in rows:
            items.append(_video_incident(paid_executor, SubmissionState, row))
        exposure = db.query(CostEntry).filter(
            CostEntry.workspace_id == ws.id,
        ).all()
        items.extend(
            _exposure_incidents(paid_executor, exposure, limit=limit))

    items.sort(key=lambda item: str(item.get("attempted_at") or ""), reverse=True)
    return {
        "workspace_id": ws.id,
        "items": items[:limit],
        "count": len(items[:limit]),
        "unknown_exposure_count": sum(
            1 for item in items
            if item["exposure"] == str(paid_executor.CostOutcome.UNKNOWN_EXPOSURE)),
        "states": [str(state) for state in SubmissionState],
        "note": ("SUBMISSION_UNKNOWN means the provider MAY have accepted and "
                 "billed the request. It is never shown as FAILED, and no retry "
                 "is offered unless retry_safe is true, which requires a "
                 "provably-undelivered submit."),
    }


@contextmanager
def workspace_paid_incidents_session():
    """A read session for the incidents route.

    ``providers.py`` deliberately holds no database session of its own -- it is a
    read-only registry surface -- so the one route that needs rows opens one
    here instead of changing that for every other route.
    """
    from app.db import session_scope

    with session_scope() as db:
        yield db


def _video_incident(paid_executor, SubmissionState, row) -> dict:
    """One ambiguous render submit, in the paid contract's vocabulary."""
    state = str(row.submission_state)
    record = paid_executor.PaidSubmission(
        workspace_id=row.workspace_id,
        provider=str(row.engine or "unknown"),
        operation="video.render.submit",
        remote_id=str(row.provider_task_id or row.engine_task_id or ""),
        state=SubmissionState(state) if state in SubmissionState.__members__.values()
        else SubmissionState.SUBMISSION_UNKNOWN,
        detail=str(row.submission_detail or ""),
    )
    # An ambiguous record is UNSAFE by default and only SAFE when the failure
    # proved nothing was delivered. `submission_detail` is the only evidence a
    # crashed submit left, so it is shown verbatim rather than summarised into
    # something that reads like a verdict.
    record.cost = paid_executor.CostRecord(
        outcome=paid_executor.CostOutcome.UNKNOWN_EXPOSURE,
        estimated=float((row.params_json or {}).get("estimated_cost_usd") or 0.0),
    )
    return _incident_row(record, record.state, source="video_submission",
                         reference_id=row.id,
                         attempted_at=_iso(row.created_at))


def _exposure_incidents(paid_executor, rows, *, limit: int) -> list[dict]:
    """Cost rows whose exposure is unknown, as incidents."""
    out: list[dict] = []
    for entry in rows:
        detail = entry.detail_json or {}
        if not detail.get("exposure_unknown"):
            continue
        record = paid_executor.PaidSubmission(
            workspace_id=entry.workspace_id,
            provider=str(entry.provider or "unknown"),
            operation=f"{entry.category}.billable_call",
            state=paid_executor.SubmissionState.SUBMISSION_UNKNOWN,
            detail=str(detail.get("reason") or ""),
        )
        record.cost = paid_executor.CostRecord(
            outcome=paid_executor.CostOutcome.UNKNOWN_EXPOSURE)
        out.append(_incident_row(
            record, paid_executor.SubmissionState.SUBMISSION_UNKNOWN,
            source="cost_entry", reference_id=entry.id,
            attempted_at=_iso(entry.created_at)))
        if len(out) >= limit:
            break
    return out


def _incident_row(record, state, *, source: str, reference_id: str,
                  attempted_at: str = "") -> dict:
    """Project one submission record onto the incident payload.

    Every field an operator needs to decide, and nothing that needs a secret:
    provider, operation, when it was attempted, the provider's own id if one came
    back, the exposure, the recommended action, and whether a retry is proven
    safe.
    """
    from app.services.paid_executor import Reconciliation, RetrySafety

    return {
        "incident_id": f"{source}:{reference_id}",
        "source": source,
        "provider": record.provider,
        "operation": record.operation,
        "attempted_at": attempted_at,
        "remote_id": record.remote_id,
        # Verbatim. The state is the fact; the UI decides how to word it.
        "state": str(state),
        "display_state": str(state),
        "exposure": str(record.cost.outcome),
        "estimated_exposure_usd": (
            None if record.cost.ledger_value is None
            else round(float(record.cost.ledger_value), 6)),
        "exposure_unknown": record.exposure_unknown,
        "recommended_action": str(record.reconciliation),
        "retry_safe": record.retry_safety is RetrySafety.SAFE,
        "may_resubmit": record.may_resubmit,
        "detail": record.detail[:400],
        "note": ("reconcile with the provider before doing anything else"
                 if record.reconciliation is Reconciliation.RECONCILE else ""),
    }


def _iso(value) -> str:
    """A timestamp as ISO-8601 UTC, or '' when there is none."""
    if value is None:
        return ""
    try:
        return value.isoformat()
    except Exception:  # noqa: BLE001 - a row with an odd timestamp is not a 500
        return str(value)


@workspace_maturity_router.get(
    "/{provider}",
    summary="One provider's full record",
)
@_guard()
def one_provider(
    provider: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
) -> dict:
    """A single provider, or 404.

    ``UNIMPLEMENTED`` TTS candidates 404 rather than being invented on the fly:
    they *are* in the table, but under a lookup that states plainly that no
    adapter exists, which is what ``/tts/qualification`` reports.
    """
    record = maturity.get_maturity(provider)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown provider {provider!r}")
    payload = maturity.with_resolved_credentials((record,), ws.id)[0]
    record_found = maturity.get_maturity(payload["provider"], payload["capability"])
    payload["blockers"] = maturity.production_blockers(record_found or record)
    payload["health"] = maturity.HEALTH_UNKNOWN
    return payload


def _credential_summary(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        state = row["resolved_credential_status"]
        counts[state] = counts.get(state, 0) + 1
    return {"by_state": counts,
            "providers_without_credentials": sorted(
                row["provider"] for row in rows
                if row["resolved_credential_status"] == maturity.CREDENTIAL_NOT_CONFIGURED)}


__all__ = [
    "provider_maturity_router",
    "workspace_maturity_router",
]