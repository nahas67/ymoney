"""UGC video pipeline (Work 07 Lane C): brief → editable timeline → render → QC.

One flow, nine presets, ZERO second video engine — every stage reuses the
canonical pieces (ScriptAgent/Strategist, BrandDNA, providers/tts, the b-roll
planner, engine/timeline, timeline_render, campaign QC patterns).

Safety gates live in `qc.py` (testimonial quotes must trace to a
user-supplied `source_quote`; unsupported numeric/absolute claims →
REVIEW_REQUIRED) and in `pipeline.regenerate()` (never clobbers a manually
edited timeline).
"""

from __future__ import annotations

from app.engine.ugc.pipeline import (
    PRESET_DEFAULTS,
    UGC_PRESETS,
    UGCBlockedError,
    UGCError,
    UGCVideoPipeline,
    normalize_brief,
)
from app.engine.ugc.qc import (
    QC_STATUSES,
    AvatarQCReport,
    UGCQCReport,
    probe_has_audio,
    run_avatar_qc,
    run_ugc_qc,
)

__all__ = [
    "AvatarQCReport",
    "PRESET_DEFAULTS",
    "QC_STATUSES",
    "UGCBlockedError",
    "UGCError",
    "UGC_PRESETS",
    "UGCQCReport",
    "UGCVideoPipeline",
    "normalize_brief",
    "probe_has_audio",
    "run_avatar_qc",
    "run_ugc_qc",
]
