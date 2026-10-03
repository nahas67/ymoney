"""Work 13 motion + effects engine.

Extends the canonical timeline (Work 02) and the existing renderer (Work 01).
No second render engine, no parallel caption store.

Modules:
    effects      bounded, typed visual-effect registry (§8)
    transitions  registry-backed transitions validated against adjacent clips (§9)
    templates    schema-restricted motion templates + instances (§6)
    lower_thirds lower-third builders that never invent metadata (§7)
    policy       EffectiveMotionPolicy -- BrandDNA-driven motion constraints (§11)
    tracking     Work 12 evidence consumption with honest confidence gating (§10)
    qc           CaptionMotionQC (§16)
"""

from app.engine.motion.effects import (
    EFFECT_NAMES,
    EFFECTS,
    EffectError,
    build_effect_filter,
    effect_dict,
    validate_effect,
)
from app.engine.motion.transitions import (
    TRANSITION_NAMES,
    TRANSITIONS,
    TransitionError,
    build_transition_filter,
    transition_dict,
    validate_transition,
)

__all__ = [
    "EFFECTS",
    "EFFECT_NAMES",
    "EffectError",
    "TRANSITIONS",
    "TRANSITION_NAMES",
    "TransitionError",
    "build_effect_filter",
    "build_transition_filter",
    "effect_dict",
    "transition_dict",
    "validate_effect",
    "validate_transition",
]