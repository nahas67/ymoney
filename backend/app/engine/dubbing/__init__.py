"""Speaker-aware dubbing planning (Work 07 Lane B).

`plan.build_plan` turns cues + speaker labels into a typed `DubbingPlan`
(one target voice per speaker); `plan.fit_plan` measures generated audio and
flags segments that cannot be synced safely instead of shipping extreme
speeds. TTS synthesis itself stays in `app.providers.dubbing` / TTS providers.
"""

from app.engine.dubbing.plan import (
    DEFAULT_SINGLE_SPEAKER,
    MAX_SPEAKING_RATE,
    MIN_SPEAKING_RATE,
    PLAN_STATUSES,
    DubbingPlan,
    PlanError,
    SegmentPlan,
    SpeakerPlan,
    TimingConstraints,
    build_plan,
    fit_plan,
    list_plans,
    load_plan,
    plan_from_dict,
    plan_to_dict,
    save_plan,
)

__all__ = [
    "DEFAULT_SINGLE_SPEAKER",
    "MAX_SPEAKING_RATE",
    "MIN_SPEAKING_RATE",
    "PLAN_STATUSES",
    "DubbingPlan",
    "PlanError",
    "SegmentPlan",
    "SpeakerPlan",
    "TimingConstraints",
    "build_plan",
    "fit_plan",
    "list_plans",
    "load_plan",
    "plan_from_dict",
    "plan_to_dict",
    "save_plan",
]
