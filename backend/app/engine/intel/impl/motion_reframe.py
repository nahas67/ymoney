"""``motion_reframe`` -- the smart-reframe provider adapter (Work 12 Lane G).

Registered in ``engine/intel/registry.py`` for the ``reframe`` chain. The
framing math lives in :mod:`app.engine.intel.reframe` and is pure stdlib; this
module is the thin provider shell around it (contracts 1.1): health,
capabilities, resources, license, cooperative ``run`` and ``cost``.

Two things this adapter deliberately does NOT do:

* **No heavy import at module import.** ``health()`` probes the ffmpeg binary
  via ``shutil.which``; nothing else is imported until ``run()``. The CI venv
  has zero ML packages, so ``from app.main import app`` must boot (contracts 0).
* **No ``active_speaker`` kind.** The registry's ``active_speaker`` chain still
  lists this key (Lane A flagged it); this provider serves the ``reframe``
  capability only, so ``resolve("active_speaker")`` skips it with an honest
  "does not serve" reason instead of pretending to be an active-speaker model.
  The orchestrator repoints that chain at integration.

``health()`` is available whenever ffmpeg is on PATH, because the plan itself is
geometry -- it does not need a model. ``capabilities()`` reports exactly that
distinction: ``models_required: false``, with the evidence lanes the plan will
consult and what happens when they are dark.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.engine.intel.base import (
    COMMERCIAL_REVIEW_REQUIRED,
    MODE_SUBPROCESS,
    CancelFn,
    IntelRequest,
    LicenseInfo,
    MediaIntelProvider,
    ProgressFn,
    ProviderHealth,
    ProviderResult,
    ResourceSpec,
    check_control,
)
from app.engine.intel.ffmpeg_util import ffmpeg_available, ffprobe_available
from app.engine.intel.reframe import (
    BACKGROUND_OPS,
    DEFAULT_MAX_MOVE_PER_S,
    DEFAULT_OUT_HEIGHT,
    DEFAULT_SAFE_AREA,
    DEFAULT_TRANSITION_S,
    KEYFRAME_SOURCES,
    LAYOUTS,
    SOURCE_OPERATOR,
    TARGET_ASPECTS,
    ReframeError,
    build_plan_payload,
    persist_plan,
)

logger = logging.getLogger("ymoney.intel")

#: ffmpeg is invoked as an EXTERNAL process, so the operator's own build and
#: distribution obligations apply (docs/oss/MEDIA_INTEL_LICENSES.md, audit
#: 2026-09-29). REVIEW_REQUIRED, never PERMITTED -- do not upgrade the verdict.
CODE_LICENSE = "LGPL-2.1-or-later"
CODE_LICENSE_URL = "https://ffmpeg.org/legal.html"
LICENSE_NOTES = (
    "ffmpeg core is LGPL-2.1-or-later; build-dependent components may be "
    "GPL-2.0-or-later. Invoked as an external process, so the operator's own "
    "build/distribution obligations apply. No model or weights are involved."
)
AUDITED_ON = "2026-09-29"


class MotionReframeProvider(MediaIntelProvider):
    """Smart reframing + multi-speaker layouts, computed locally.

    The plan is geometry, so this provider is available with no model at all --
    it merely reports which evidence levels went dark (see
    :meth:`capabilities`). It fails closed on an unusable request
    (:class:`~app.engine.intel.base.ProviderUnavailable`) and never invents a
    speaker, a face or a mask.
    """

    key = "motion_reframe"
    kind = "reframe"
    #: Contract 11 capabilities this provider serves. ``reframe`` is repeated
    #: from ``kind`` because the registry reads ``kinds`` for chain membership.
    #: Deliberately NOT "active_speaker" -- see the module docstring.
    kinds = ("reframe",)

    # -- health / capabilities ------------------------------------------------

    def health(self) -> ProviderHealth:
        """Availability. Never raises; a reason is always present when dark.

        Only the ffmpeg BINARY is probed, and only with ``shutil.which`` inside
        this method -- no model import happens on this path.
        """
        try:
            has_ffmpeg = ffmpeg_available()
            has_ffprobe = ffprobe_available()
        except Exception as exc:  # noqa: BLE001 -- a health probe must not raise
            return ProviderHealth(
                available=False,
                reason=f"ffmpeg probe failed: {type(exc).__name__}",
                mode=MODE_SUBPROCESS,
            )
        if not has_ffmpeg:
            return ProviderHealth(
                available=False,
                reason="ffmpeg not on PATH; reframe preview render is unavailable",
                mode=MODE_SUBPROCESS,
                detail={
                    "ffmpeg": False,
                    "ffprobe": has_ffprobe,
                    "models_required": False,
                },
            )
        return ProviderHealth(
            available=True,
            reason="",
            version="reframe.v1",
            mode=MODE_SUBPROCESS,
            detail={
                "ffmpeg": True,
                "ffprobe": has_ffprobe,
                "models_required": False,
                "evidence_lanes": ["active_speaker", "face_tracking", "segmentation"],
            },
        )

    def capabilities(self) -> dict:
        """Honest feature/limit map -- no capability is claimed unprobed."""
        health = self.health()
        return {
            "aspects": list(TARGET_ASPECTS),
            "layouts": list(LAYOUTS),
            "background_ops": list(BACKGROUND_OPS),
            "keyframe_sources": list(KEYFRAME_SOURCES),
            "operator_source": SOURCE_OPERATOR,
            "models_required": False,
            "editable_keyframes": True,
            "bakes_crop_into_source": False,
            "preview_render": bool(health.available),
            "preview_codec": "libx264",
            "defaults": {
                "safe_area": DEFAULT_SAFE_AREA,
                "max_move_per_s": DEFAULT_MAX_MOVE_PER_S,
                "transition_s": DEFAULT_TRANSITION_S,
                "out_height": DEFAULT_OUT_HEIGHT,
            },
            "priority": list(KEYFRAME_SOURCES),
            "note": (
                "Levels 1-2 (active_speaker, subject) need lane F's evidence rows. "
                "With no rows the plan still resolves at level 3 or 4 and records "
                "which level was chosen on every keyframe."
            ),
        }

    def resource_requirements(self) -> ResourceSpec:
        """CPU-only. The plan is arithmetic; only a preview render touches ffmpeg."""
        return ResourceSpec(
            gpu=False,
            vram_mb=0,
            ram_mb=64,
            model_bytes=0,
            cpu_seconds_per_audio_minute=0.0,
            notes=(
                "stdlib geometry + one optional ffmpeg preview pass; no model, "
                "no VRAM, no GPU admission (contracts 3: CPU providers bypass it)"
            ),
        )

    def license_info(self) -> LicenseInfo:
        """Code AND model terms, separately (contracts 1.4).

        There is no model, so ``model_license`` is honestly ``NONE``. The
        ``REVIEW_REQUIRED`` verdict is the audit's, not this adapter's opinion.
        """
        return LicenseInfo(
            code_license=CODE_LICENSE,
            code_license_url=CODE_LICENSE_URL,
            model_license="NONE",
            model_license_url="",
            model_gated=False,
            commercial_use=COMMERCIAL_REVIEW_REQUIRED,
            audited_on=AUDITED_ON,
            notes=LICENSE_NOTES,
        )

    # -- work -----------------------------------------------------------------

    def run(
        self,
        request: IntelRequest,
        *,
        progress: ProgressFn,
        should_cancel: CancelFn,
        deadline: float | None,
    ) -> ProviderResult:
        """Build a reframe plan. Pure orchestration -- writes rows, not media.

        The optional preview render is NOT done here: a render is an explicit,
        separately-authorised step (``render_preview``), so this path stays
        cheap enough to run in-process and never touches a source file.
        """
        from app.db import session_scope
        from app.models import MediaAsset

        params: dict[str, Any] = dict(request.params or {})
        layout = str(params.get("layout") or "ACTIVE_SPEAKER")
        aspect = str(params.get("aspect") or "9:16")
        check_control(should_cancel, deadline)
        progress(0.1)

        with session_scope() as session:
            source = session.get(MediaAsset, str(request.asset_id))
            if source is None or source.workspace_id != str(request.workspace_id):
                raise ReframeError("source asset not found in this workspace")
            check_control(should_cancel, deadline)
            progress(0.4)
            payload = build_plan_payload(
                session,
                request.workspace_id,
                source,
                aspect=aspect,
                layout=layout,
                params=params,
                run_id=request.run_id or None,
                out_height=int(params.get("out_height") or DEFAULT_OUT_HEIGHT),
            )
            check_control(should_cancel, deadline)
            progress(0.8)
            plan = persist_plan(
                session, request.workspace_id, source, payload, run_id=request.run_id or None
            )
            keyframes = [k.to_row() for k in payload.get("keyframe_objects") or []]
            plan_id = plan.id
            session.commit()
        progress(1.0)

        levels = list(payload.get("levels_used") or [])
        warnings: list[str] = []
        if not levels:
            warnings.append("no keyframes produced")
        if "active_speaker" not in levels:
            warnings.append(
                "no RESOLVED active-speaker evidence: plan resolved below priority level 1"
            )
        return ProviderResult(
            ok=True,
            artifacts={
                "reframe_plan": {
                    "plan_id": plan_id,
                    "layout": layout,
                    "aspect": aspect,
                    "keyframe_count": len(keyframes),
                }
            },
            metrics={
                "plan_id": plan_id,
                "levels_used": levels,
                "strategy": str(payload.get("strategy") or ""),
                "jitter": payload.get("jitter") or {},
                "geometry": payload.get("geometry") or {},
                "evidence": payload.get("evidence") or {},
                "slot_count": len(payload.get("slots") or []),
                "op_count": len(payload.get("ops") or []),
                "baked": False,
            },
            warnings=warnings,
        )

    def cost(self, spec: ResourceSpec) -> dict:
        """Zero GPU cost. CPU time is charged by the caller's measured span."""
        return {
            "gpu_ms": 0,
            "cpu_ms": 0,
            "cost_micros": 0,
            "billed": False,
            "notes": "stdlib geometry; no model, no GPU",
        }

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def evidence_status(workspace_id: str, asset_id: str) -> dict:
        """Which of the contract 11 priority levels can actually be served.

        Read-only probe used by the API so the UI can show a read-only state
        (contracts 15) instead of offering a "smart" reframe that would silently
        collapse to a centre crop.
        """
        from app.db import session_scope
        from app.engine.intel.reframe import load_evidence

        with session_scope() as session:
            evidence = load_evidence(session, workspace_id, asset_id)
        return {
            "active_speaker_available": bool(evidence.get("active_speaker")),
            "subject_available": bool(evidence.get("face_boxes")),
            "levels_servable": [
                level
                for level, available in (
                    ("active_speaker", bool(evidence.get("active_speaker"))),
                    ("subject", bool(evidence.get("face_boxes"))),
                    ("focal_point", True),
                    ("fallback", True),
                )
                if available
            ],
            "unresolved_active_speaker_rows": len(evidence.get("unresolved") or []),
        }

    @staticmethod
    def now_ms() -> int:
        """Monotonic-ish wall clock in ms (cost accounting helper)."""
        return int(time.perf_counter() * 1000)


#: the registry imports this symbol (contracts 1.1)
PROVIDER = MotionReframeProvider


def provider() -> MotionReframeProvider:
    """Build the provider (the registry instantiates with no arguments)."""
    return MotionReframeProvider()


__all__ = ["AUDITED_ON", "CODE_LICENSE", "MotionReframeProvider", "PROVIDER", "provider"]
