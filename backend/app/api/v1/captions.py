"""Work 13 — caption / motion / effect HTTP surface.

Read-mostly registry + tooling endpoints the editor uses:

* the caption presets, motion templates, effect registry and transition
  registry (each with its typed parameters, so the UI can build a form);
* a caption/motion QC run over a submitted document;
* the Work 12 evidence summary that explains why an automated effect was or
  was not attached.

MUTATIONS deliberately do not live here. Captions, motion instances, effects
and transitions are edited through the canonical timeline operation layer
(``POST /timelines/{id}/operations``) and through the CreativeDirector
(``/creative/preview`` + ``/creative/apply``), so there is exactly one write
path and undo/versioning keeps working unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Workspace
from app.services.auth_service import require_workspace_role

captions_router = APIRouter(
    prefix="/workspaces/{workspace_id}/captions", tags=["captions-motion"])


@captions_router.get("/presets", summary="Caption presets (brand-resolved)")
def list_presets(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
) -> dict:
    """Every caption preset, with this workspace's BrandDNA overrides applied."""
    from app.engine.captions.presets import PRESETS
    from app.engine.motion.policy import resolve_motion_policy

    try:
        policy = resolve_motion_policy(db, ws.id)
        brand_patch = dict(policy.caption_style or {})
        approved = list(policy.approved_caption_presets)
    except Exception:
        brand_patch, approved = {}, []
    items = []
    for key, preset in PRESETS.items():
        from app.engine.captions.presets import effective_preset

        try:
            resolved = effective_preset(key, brand_patch=brand_patch)
        except Exception:
            resolved = preset
        payload = resolved.to_dict()
        payload["brand_approved"] = (not approved) or key in approved
        items.append(payload)
    return {"items": items, "brand_approved": approved,
            "brand_caption_style": brand_patch,
            "dna_version": policy.dna_version if approved or brand_patch else ""}


@captions_router.get("/templates", summary="Motion templates (schema only)")
def list_templates(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
) -> dict:
    from app.engine.motion.policy import resolve_motion_policy
    from app.engine.motion.templates import template_dict

    try:
        policy = resolve_motion_policy(db, ws.id)
        forbidden = list(policy.forbidden_motion_templates)
    except Exception:
        forbidden = []
    items = []
    for tpl in template_dict():
        tpl["brand_forbidden"] = tpl["key"] in forbidden
        items.append(tpl)
    return {"items": items, "brand_forbidden": forbidden}


@captions_router.get("/effects", summary="Bounded effect registry")
def list_effects(
    ws: Workspace = Depends(require_workspace_role("viewer")),
) -> dict:
    from app.engine.motion.effects import effect_dict
    from app.engine.motion.policy import EffectiveMotionPolicy

    policy = EffectiveMotionPolicy()
    items = []
    for spec in effect_dict():
        spec["brand_allowed"] = policy.effect_allowed(spec["key"])
        items.append(spec)
    return {"items": items}


@captions_router.get("/transitions", summary="Transition registry")
def list_transitions(
    ws: Workspace = Depends(require_workspace_role("viewer")),
) -> dict:
    from app.engine.motion.transitions import transition_dict

    return {"items": transition_dict()}


@captions_router.get("/emphasis", summary="Emphasis kinds + detectors")
def list_emphasis(
    ws: Workspace = Depends(require_workspace_role("viewer")),
) -> dict:
    from app.engine.captions.emphasis import (
        EMPHASIS_KINDS,
        FORBIDDEN_SENSITIVE_KINDS,
    )
    from app.engine.captions.style import (
        EASINGS,
        ENTRANCES,
        EXITS,
        HIGHLIGHT_ANIMATIONS,
        WEIGHT_ANIMATIONS,
    )

    return {
        "emphasis_kinds": list(EMPHASIS_KINDS),
        # Surfaced deliberately: the UI must never offer these.
        "protected_kinds_never_offered": sorted(FORBIDDEN_SENSITIVE_KINDS),
        "entrances": list(ENTRANCES),
        "exits": list(EXITS),
        "word_animations": list(WEIGHT_ANIMATIONS),
        "highlight_animations": list(HIGHLIGHT_ANIMATIONS),
        "easings": list(EASINGS),
    }


class QcBody(BaseModel):
    timeline_id: str = ""
    doc: dict = Field(default_factory=dict)


@captions_router.post("/qc", summary="Run CaptionMotionQC on a document")
def run_qc(
    body: QcBody,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
) -> dict:
    """Read-only QC. Never mutates the timeline."""
    from app.engine.motion.qc import run_caption_motion_qc
    from app.models import ContentTimeline

    doc = dict(body.doc or {})
    if body.timeline_id:
        row = db.get(ContentTimeline, body.timeline_id)
        if row is None or row.workspace_id != ws.id:
            raise HTTPException(status_code=404, detail="timeline not found")
        doc = dict(row.tracks_json or {})
    if not doc:
        raise HTTPException(status_code=422, detail="provide a doc or timeline_id")

    width, height = 1080, 1920
    aspect = str(doc.get("aspect_ratio") or "9:16")
    from app.providers.video_engine.timeline_render import ASPECT_DIMS

    width, height = ASPECT_DIMS.get(aspect, (1080, 1920))
    safe_box: dict = {}
    try:
        from app.engine.motion.policy import resolve_motion_policy

        safe_box = dict(resolve_motion_policy(db, ws.id).safe_zone or {})
    except Exception:
        safe_box = {}
    return run_caption_motion_qc(doc, width=width, height=height,
                                 safe_box=safe_box)


@captions_router.get("/evidence", summary="Work 12 evidence available for motion")
def evidence(
    asset_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
) -> dict:
    """What automated motion MAY do with this asset, and why.

    A caller that sees ``allows.tracked_callouts = False`` knows not to offer
    the tracked-callout control, instead of silently attaching nothing later.
    """
    from app.engine.motion.tracking import evidence_summary

    return evidence_summary(db, ws.id, asset_id)