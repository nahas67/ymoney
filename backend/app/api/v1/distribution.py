"""Work 14 distribution API (§12) -- read-only, workspace-scoped.

Four read-only surfaces for the campaign/distribution UI:

* ``GET  /distribution/platforms``            -- every verified profile
* ``GET  /distribution/platforms/{platform}`` -- one profile, with provenance
* ``GET  /distribution/capabilities``         -- capability badges + publish mode
* ``POST /distribution/optimize``             -- the variant optimization diff

Nothing here publishes, mutates, or accepts a token. The endpoints exist so the
UI can show capability badges, the optimization diff, platform-specific
warnings, and LIVE / MOCK / HANDOFF / UNAVAILABLE without the frontend
re-deriving any of it.

The publish mode is read from the publisher registry
(:data:`HANDOFF_PLATFORMS`), never from a platform-name check, so adding a
platform cannot make this drift.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.engine.distribution.profiles import (
    _Unknown as _UnknownSentinel,
)
from app.engine.distribution.profiles import (
    get_profile,
    profile_platforms,
)
from app.engine.platform_registry import get_registry
from app.models import Workspace
from app.providers.publishers.factory import HANDOFF_PLATFORMS
from app.schemas.responses import DistributionCapabilityListOut
from app.services.auth_service import require_workspace_role

distribution_router = APIRouter(
    prefix="/workspaces/{workspace_id}/distribution",
    tags=["distribution-work14"],
)


def _publish_mode(platform: str) -> str:
    """The UI-facing mode label.

    Deliberately the CAPABILITY name (``USER_HANDOFF``) rather than the
    stored enum value (``HANDOFF``): this string is rendered on a card, and the
    capability vocabulary is what the rest of the registry and the DoD speak in.
    """
    return ("USER_HANDOFF" if platform in HANDOFF_PLATFORMS
            else "DIRECT_PUBLISH")


@distribution_router.get("/platforms", summary="Verified platform profiles")
def list_platforms(ws: Workspace = Depends(require_workspace_role("viewer"))) -> dict:
    """Every verified profile, with what is verified AND what is not.

    ``unverified`` is the load-bearing field: those are the constraints the
    platform does not document, and the UI must show them as unknown rather
    than as a limit.
    """
    registry = get_registry()
    items = []
    for name in profile_platforms():
        profile = get_profile(name)
        items.append({
            "platform": name,
            "media_types": sorted(profile.media_types),
            "capabilities": sorted(
                str(c) for c in registry.capabilities(name)),
            "publish_mode": _publish_mode(name),
            "verified_limits": sorted(
                f for f in profile.__dataclass_fields__
                if f != "platform" and profile.known(f)),
            "unverified": sorted(
                f for f in profile.__dataclass_fields__
                if f != "platform" and not profile.known(f)),
            "verified_notes": list(profile.verified_notes),
        })
    return {"items": items}


@distribution_router.get("/platforms/{platform}",
                         summary="One verified profile with provenance")
def platform_detail(platform: str,
                    ws: Workspace = Depends(require_workspace_role("viewer"))
                    ) -> dict:
    try:
        profile = get_profile(platform)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload: dict = {"platform": profile.platform}
    for name in profile.__dataclass_fields__:
        if name == "platform":
            continue
        raw = getattr(profile, name)
        if hasattr(raw, "source"):        # a verified ProfileLimit
            payload[name] = {"value": raw.value, "source": raw.source,
                             "note": raw.note, "verified": True}
        elif isinstance(raw, _UnknownSentinel):
            # UNDOCUMENTED. Emitted in the SAME shape as a verified limit so
            # the UI renders one component; `verified: False` is what makes it
            # show "unknown" instead of a number.
            payload[name] = {"value": "UNKNOWN", "source": "",
                             "note": "not stated in the platform's official "
                                     "documentation; no limit was invented",
                             "verified": False}
        elif isinstance(raw, (frozenset, tuple, list)):
            payload[name] = (sorted(raw) if isinstance(raw, frozenset)
                             else list(raw))
        else:
            payload[name] = raw
    registry = get_registry()
    payload["capabilities"] = sorted(
        str(c) for c in registry.capabilities(profile.platform))
    payload["publish_mode"] = _publish_mode(profile.platform)
    return payload


@distribution_router.get("/capabilities",
                          summary="Capability badges + publish mode",
                          responses={200: {"model": DistributionCapabilityListOut}},
                        )
def capabilities(ws: Workspace = Depends(require_workspace_role("viewer"))) -> dict:
    """Capability badges and readiness for every account platform.

    Includes the pre-Work-14 platforms so the UI has a single list to render,
    and reports the four publication modes so a card can never be labelled
    "published" when it was a handoff.
    """
    registry = get_registry()
    items = []
    for spec in registry.specs():
        names = {str(c) for c in spec.capabilities}
        items.append({
            "platform": spec.platform,
            "capabilities": sorted(names),
            "publish_mode": _publish_mode(spec.platform),
            "direct_publish": "DIRECT_PUBLISH" in names,
            "user_handoff": "USER_HANDOFF" in names,
            "supports_inbox": spec.supports_inbox,
            "supports_analytics": spec.supports_analytics,
            "campaign_platforms": list(spec.campaign_platforms),
            "media": spec.media,
            "metadata_limits": spec.metadata_limits,
        })
    return {"items": items}


class _OptimizeRequest(BaseModel):
    platform: str
    master: dict = Field(default_factory=dict)
    brand: dict | None = None
    overrides: dict | None = None
    learned: dict | None = None


@distribution_router.post("/optimize", summary="Platform-specific variant diff")
def optimize(request: _OptimizeRequest,
             ws: Workspace = Depends(require_workspace_role("viewer"))) -> dict:
    """Run the optimizer and return the spec plus its full provenance.

    The BrandDNA is built from the request fragment through the real schema, so
    an invalid fragment is a 422 rather than a silently-ignored one.
    """
    from app.engine.brand.dna import BrandDNA, BrandDNASchemaError
    from app.engine.distribution.optimizer import PlatformVariantOptimizer

    brand = None
    if request.brand:
        try:
            brand = BrandDNA(**request.brand)
        except BrandDNASchemaError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    result = PlatformVariantOptimizer().optimize(
        platform=request.platform, master=request.master, brand=brand,
        overrides=request.overrides, learned=request.learned)
    payload = result.to_dict()
    payload["workspace_id"] = ws.id
    payload["unknown_limits_note"] = (
        "a field listed in `unverified` is NOT documented by the platform; "
        "no limit was invented for it")
    return payload


__all__ = ["distribution_router"]
