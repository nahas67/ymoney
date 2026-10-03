"""Music policy API (Work 15.6) -- the operator's control over generated music.

Mounted once by the orchestrator in ``api/v1/__init__.py``::

    GET /workspaces/{ws}/music/policy    viewer
    GET /workspaces/{ws}/music/providers viewer
    PUT /workspaces/{ws}/music/policy    admin

The whole surface is the policy, because the *generation* is a pipeline stage,
not an endpoint. Nothing here spends money: the routes report the resolved
decision and let an admin flip the opt-in. That keeps the one billable call
inside the pipeline, where the budget gate and the paid-job contract already
live.

Three properties this module keeps:

* **Unset is not enabled.** ``GET /policy`` on a workspace that never configured
  music returns ``generate: false`` with ``reason: "policy_not_enabled"``, not a
  default genre and not a silent yes. Inventing a preference nobody stated is
  the failure this endpoint exists to prevent.
* **The brand can refuse.** ``PUT`` cannot turn generation on for a brand whose
  ``music_prefs["enabled"] is False``; the response says ``brand_disabled``.
* **Workspace scoping on every route**, with the existing
  ``require_workspace_role`` floors. No new capability name is invented.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Workspace
from app.providers.music import MUSIC_PROVIDERS
from app.providers.music.policy import (
    MUSIC_PREF_KEYS,
    MusicPolicy,
    brand_music_prefs,
    music_policy,
    music_settings,
)
from app.services.auth_service import require_workspace_role

music_router = APIRouter(prefix="/workspaces/{workspace_id}/music",
                         tags=["music-work15"])
logger = logging.getLogger("ymoney.music")


class MusicPolicyBody(BaseModel):
    """``PUT /policy`` body.

    ``generate`` is ``bool | None`` so an omitted key is a no-op rather than an
    accidental disable, matching the ``safety.py`` PUT convention.
    """

    generate: bool | None = Field(default=None)
    provider_key: str | None = Field(default=None, max_length=60)


def _short(exc: BaseException, limit: int = 180) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:limit]


def _guard(value_error: int = 422):
    """Uniform error policy mirroring ``api/v1/media_intel.py``."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any):
            try:
                return fn(*args, **kwargs)
            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=value_error,
                                    detail=_short(exc)) from None
            except Exception:  # noqa: BLE001 -- deliberate catch-all at the edge
                logger.exception("music route failed: %s",
                                 getattr(fn, "__name__", fn))
                raise HTTPException(status_code=500, detail="internal error") from None

        return run

    return decorate


def _brand_dna(db: Session, workspace_id: str) -> Any:
    """The effective BrandDNA for the workspace, or ``None``.

    An unreadable brand yields ``None``, which the policy reads as "no
    preference stated" -- never as a licence to choose one.
    """
    try:
        from app.engine.brand.dna import effective_dna
        from app.engine.brand.inheritance import brand_layer

        dna, _brand_id = brand_layer(db, workspace_id)
        return effective_dna(dna)
    except Exception as exc:  # noqa: BLE001 — report, never invent
        logger.warning("music: brand document unavailable (%s)",
                       type(exc).__name__)
        return None


def _payload(ws: Workspace, db: Session, policy: MusicPolicy) -> dict:
    settings = music_settings(ws)
    return {
        "workspace_id": ws.id,
        "generate": policy.generate,
        "reason": policy.reason,
        "brand_disabled": policy.brand_disabled,
        "provider_key": policy.provider_key,
        "configured": bool(settings),
        "settings_key": "music",
        "forbidden_genres": list(policy.forbidden_genres),
        "prefs": dict(policy.prefs),
        "prefs_keys": list(MUSIC_PREF_KEYS),
        "raw_settings": settings,
        "note": ("generate=false is a recorded decision, not a failure: unset "
                 "policy means nobody opted in, so no money is spent"),
    }


@music_router.get("/policy", summary="Is generated music allowed, and why")
@_guard()
def get_music_policy(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
) -> dict:
    """The resolved decision, including the preferences it was resolved from.

    Exposes the forbidden-genre list so an operator sees what the brand refuses
    before a run does.
    """
    policy = music_policy(ws, _brand_dna(db, ws.id), workspace_id=ws.id)
    return _payload(ws, db, policy)


@music_router.get("/providers", summary="Music provider availability")
@_guard()
def list_music_providers(
    ws: Workspace = Depends(require_workspace_role("viewer")),
) -> dict:
    """Provider keys with their honest health.

    Availability is probed from the registry, never asserted: an unconfigured
    backend reports ``available: false`` with its reason instead of appearing
    ready and failing at generation time.
    """
    items: list[dict] = []
    for key in MUSIC_PROVIDERS:
        entry: dict[str, Any] = {"key": key, "available": False, "detail": ""}
        try:
            from app.providers.music import get_music_provider

            provider = get_music_provider(key, workspace_id=ws.id)
            entry.update(provider.health())
        except Exception as exc:  # noqa: BLE001 -- availability is not an error
            entry["detail"] = _short(exc)
        items.append(entry)
    return {"items": items,
            "available": [i["key"] for i in items if i.get("available")]}


@music_router.put("/policy", summary="Set the workspace music opt-in")
@_guard()
def put_music_policy(
    body: MusicPolicyBody,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db: Session = Depends(get_db),
) -> dict:
    """Write the opt-in. A brand refusal cannot be overridden here.

    An unknown ``provider_key`` is a 422 rather than a silent fallback: naming a
    backend that does not exist and then generating with a different one is how a
    workspace ends up paying a provider nobody selected.
    """
    if body.provider_key and body.provider_key not in MUSIC_PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown provider {body.provider_key!r}; pick from "
                   f"{list(MUSIC_PROVIDERS)}")
    current = dict((ws.settings_json or {}).get("music") or {})
    updates = body.model_dump(exclude_none=True)
    current.update(updates)
    merged = dict(ws.settings_json or {})
    merged["music"] = current
    ws.settings_json = merged
    db.commit()
    policy = music_policy(ws, _brand_dna(db, ws.id), workspace_id=ws.id)
    return _payload(ws, db, policy)


@music_router.get("/prefs", summary="The BrandDNA music_prefs this policy reads")
@_guard()
def get_music_prefs(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
) -> dict:
    """The recognised ``BrandDNA.music_prefs`` keys and their values.

    Unrecognised keys are dropped rather than echoed, so an operator typo shows
    up as a missing value instead of a preference that silently does nothing.
    """
    prefs = brand_music_prefs(_brand_dna(db, ws.id))
    return {"workspace_id": ws.id, "prefs": prefs,
            "keys": list(MUSIC_PREF_KEYS),
            "stated": bool(prefs),
            "note": ("empty prefs mean the brand stated no music preference; "
                     "that is not an instruction to pick one")}


__all__ = ["music_router"]