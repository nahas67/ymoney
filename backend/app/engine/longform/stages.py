"""Stage dispatch: pipeline.py calls stage_<name> here."""

from app.engine.longform.stages_early import (
    stage_outline,
    stage_research,
    stage_script,
    stage_strategy,
    stage_verify,
)
from app.engine.longform.stages_finish import (
    stage_metadata,
    stage_qc,
    stage_render,
)
from app.engine.longform.stages_late import (
    stage_asset_acquire,
    stage_asset_plan,
    stage_scene_plan,
)
from app.engine.longform.stages_voice_timeline import (
    stage_timeline,
    stage_voice,
)

__all__ = ["stage_research", "stage_strategy", "stage_outline", "stage_script",
           "stage_verify", "stage_scene_plan", "stage_asset_plan",
           "stage_asset_acquire", "stage_voice", "stage_timeline",
           "stage_qc", "stage_render", "stage_metadata"]
