"""EffectiveMotionPolicy -- BrandDNA-driven motion constraints (Work 13 §11).

Work 08 BrandDNA remains authoritative. This module PROJECTS the already
resolved :class:`~app.engine.brand.policy.EffectiveCreativePolicy` into the
motion-specific constraint set, and adds the two places where motion needs a
hard rule that the creative policy does not carry:

* ``motion_language`` -- a field that already exists on ``BrandDNA``
  (``dna.py``) but was declared and never wired to anything. Rather than
  inventing a new top-level key, Work 13 uses it.
* ``forbidden_effects`` / ``forbidden_motion_templates`` -- motion-specific
  deny lists that learning may recommend against but never override.

Precedence, matching the existing brand system exactly: workspace -> brand ->
campaign -> content -> platform, resolved by
``resolve_effective_policy``. Learning may RECOMMEND a motion style through
:meth:`EffectiveMotionPolicy.recommend`, and a recommendation is dropped when
it conflicts with a hard rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.engine.captions.presets import PRESET_NAMES, get_preset
from app.engine.motion.effects import EFFECT_NAMES
from app.engine.motion.templates import TEMPLATES

__all__ = [
    "MOTION_INTENSITIES",
    "EffectiveMotionPolicy",
    "MotionPolicyError",
    "resolve_motion_policy",
]

#: How much motion a brand tolerates. Ordering matters: a lower tier may not
#: select a higher one, and ``none`` forbids animation outright.
MOTION_INTENSITIES: tuple[str, ...] = ("none", "subtle", "standard", "expressive")

#: Brand keys that are refused outright (never, under any override).
_HARD_FORBIDDEN_EFFECTS = frozenset({"BACKGROUND_BLUR"})  # needs a mask asset


class MotionPolicyError(ValueError):
    """A motion policy request violated a brand rule."""


@dataclass
class EffectiveMotionPolicy:
    """Resolved, motion-scoped brand constraints."""

    workspace_id: str = ""
    brand_id: str | None = None
    #: Approved caption presets; empty means "no brand restriction".
    approved_caption_presets: tuple[str, ...] = ()
    #: The brand's caption style patch, applied over a preset.
    caption_style: dict = field(default_factory=dict)
    approved_fonts: tuple[str, ...] = ()
    approved_colors: tuple[str, ...] = ()
    lower_third_preset: str = ""
    transition_style: str = ""
    motion_intensity: str = "standard"
    logo_animation: str = "none"
    forbidden_effects: tuple[str, ...] = ()
    forbidden_motion_templates: tuple[str, ...] = ()
    #: Platform caption safe box (fractions), when the caller knows one.
    safe_zone: dict = field(default_factory=dict)
    dna_version: str = ""
    provenance: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A brand list that names a preset which no longer exists must not make
        # every real preset look "not approved". Applied on EVERY construction
        # path, not just the resolver, so the two agree.
        known = {p.lower() for p in PRESET_NAMES}
        self.approved_caption_presets = tuple(
            p for p in self.approved_caption_presets if str(p).lower() in known
        )
        self.forbidden_effects = tuple(
            e for e in self.forbidden_effects
            if str(e).upper() in EFFECT_NAMES
        )

    # -- queries -----------------------------------------------------------

    def caption_preset_allowed(self, preset: str) -> bool:
        if not self.approved_caption_presets:
            return True
        return str(preset).strip().lower() in {
            p.strip().lower() for p in self.approved_caption_presets
        }

    def effect_allowed(self, effect: str) -> bool:
        key = str(effect or "").strip().upper()
        if key in _HARD_FORBIDDEN_EFFECTS:
            return False
        if key in {e.upper() for e in self.forbidden_effects}:
            return False
        return key in EFFECT_NAMES

    def template_allowed(self, template: str) -> bool:
        key = str(template or "").strip().lower()
        if key in {t.strip().lower() for t in self.forbidden_motion_templates}:
            return False
        return key in TEMPLATES

    def font_allowed(self, family: str) -> bool:
        if not self.approved_fonts:
            return True
        return str(family or "").strip().lower() in {
            f.strip().lower() for f in self.approved_fonts
        }

    def color_allowed(self, color: str) -> bool:
        """Brand colours are a palette, not a whitelist of every legal hex.

        Mirrors ``verifier._check_colors``: an off-brand colour is a REVIEW
        item, not a hard block, because a caption must remain legible even when
        the brand has no exact match.
        """
        if not self.approved_colors:
            return True
        token = str(color or "").strip().lower()
        return token in {c.strip().lower() for c in self.approved_colors}

    def animation_allowed(self, animation: str) -> bool:
        if self.motion_intensity == "none":
            return str(animation or "none") in ("none", "fade")
        return True

    def require_effect(self, effect: str) -> None:
        if not self.effect_allowed(effect):
            raise MotionPolicyError(
                f"effect {effect!r} is not permitted by brand"
                + (" (hard block)" if str(effect).upper() in _HARD_FORBIDDEN_EFFECTS else "")
            )

    def require_preset(self, preset: str) -> None:
        if not self.caption_preset_allowed(preset):
            raise MotionPolicyError(
                f"caption preset {preset!r} is not in the brand's approved list "
                f"{list(self.approved_caption_presets)}"
            )

    def require_template(self, template: str) -> None:
        if not self.template_allowed(template):
            raise MotionPolicyError(
                f"motion template {template!r} is not permitted by brand"
            )

    def intensity_allows(self, animation: str) -> bool:
        """Tier-check an animation against the brand's motion intensity."""
        if self.motion_intensity == "none":
            return str(animation or "none") in ("none", "fade")
        if self.motion_intensity == "subtle":
            return str(animation or "none") in ("none", "fade", "reveal", "wipe")
        return True

    def recommend(self, preset: str, *, source: str = "learning") -> dict:
        """Learning/advisory entry point.

        Returns a decision dict. A recommendation that violates a brand rule is
        REFUSED here, so no downstream caller can act on it -- learning may
        suggest, brand decides.
        """
        try:
            self.require_preset(preset)
        except MotionPolicyError as exc:
            return {"accepted": False, "preset": preset, "source": source,
                    "reason": str(exc)}
        try:
            resolved = get_preset(preset)
        except Exception as exc:  # unknown preset is also a refusal
            return {"accepted": False, "preset": preset, "source": source,
                    "reason": str(exc)}
        return {"accepted": True, "preset": resolved.key, "source": source,
                "style": resolved.style.to_dict()}

    def to_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "brand_id": self.brand_id,
            "approved_caption_presets": list(self.approved_caption_presets),
            "caption_style": dict(self.caption_style),
            "approved_fonts": list(self.approved_fonts),
            "approved_colors": list(self.approved_colors),
            "lower_third_preset": self.lower_third_preset,
            "transition_style": self.transition_style,
            "motion_intensity": self.motion_intensity,
            "logo_animation": self.logo_animation,
            "forbidden_effects": list(self.forbidden_effects),
            "forbidden_motion_templates": list(self.forbidden_motion_templates),
            "safe_zone": dict(self.safe_zone),
            "dna_version": self.dna_version,
            "provenance": dict(self.provenance),
            "known_presets": list(PRESET_NAMES),
        }


def _as_list(value) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    try:
        return tuple(str(v) for v in value if str(v or "").strip())
    except TypeError:
        return ()


def resolve_motion_policy(
    session,
    workspace_id: str,
    *,
    campaign_id: str | None = None,
    content_id: str | None = None,
    platform: str | None = None,
    brand_id: str | None = None,
    safe_zone: dict | None = None,
) -> EffectiveMotionPolicy:
    """Resolve the brand's motion policy through the EXISTING brand chain.

    Delegates to ``app.engine.brand.inheritance.resolve_effective_policy`` so
    Work 13 inherits the established workspace -> brand -> campaign -> content
    -> platform precedence (and its provenance) for free, then projects the
    motion-relevant fields. Never raises: an unresolvable brand yields the
    permissive defaults rather than blocking a render.
    """
    policy = EffectiveMotionPolicy(workspace_id=workspace_id, brand_id=brand_id)
    try:
        from app.engine.brand.inheritance import resolve_effective_policy
    except Exception:
        return policy
    try:
        creative = resolve_effective_policy(
            session, workspace_id, campaign_id=campaign_id,
            content_id=content_id, platform=platform, brand_id=brand_id,
        )
    except Exception:
        return policy

    policy.dna_version = str(getattr(creative, "dna_version", "") or "")
    policy.provenance = dict(getattr(creative, "provenance", {}) or {})

    caption_style = dict(getattr(creative, "caption_style", {}) or {})
    policy.caption_style = caption_style

    # `approved_caption_presets` may arrive on the policy dict-like fields;
    # the creative policy carries a free-form caption_style, so read both.
    policy.approved_caption_presets = _as_list(
        caption_style.get("approved_presets")
        or caption_style.get("presets")
        or caption_style.get("allowed")
    )
    policy.approved_fonts = _as_list(
        _flatten_fonts(getattr(creative, "fonts", {}) or {})
    )
    policy.approved_colors = _as_list(getattr(creative, "brand_colors", ()))
    policy.lower_third_preset = str(
        caption_style.get("lower_third_preset") or "").strip().lower()
    policy.transition_style = str(
        caption_style.get("transition_style") or "").strip().lower()
    intensity = str(caption_style.get("motion_intensity") or "").strip().lower()
    if intensity in MOTION_INTENSITIES:
        policy.motion_intensity = intensity
    policy.logo_animation = str(
        caption_style.get("logo_animation") or "none").strip().lower()

    motion_language = dict(caption_style.get("motion_language") or {})
    if not motion_language:
        motion_language = dict(getattr(creative, "subject", {}) or {}).get(
            "motion_language", {}) or {}
    policy.forbidden_effects = _as_list(
        motion_language.get("forbidden_effects") or caption_style.get("forbidden_effects")
    )
    policy.forbidden_motion_templates = _as_list(
        motion_language.get("forbidden_motion_templates")
        or caption_style.get("forbidden_motion_templates")
    )
    zone = safe_zone or motion_language.get("safe_zone") or {}
    policy.safe_zone = {k: float(v) for k, v in dict(zone).items()
                         if k in ("top", "right", "bottom", "left")}
    return policy


def _flatten_fonts(fonts) -> list[str]:
    """Pull candidate family names out of the brand ``fonts`` mapping.

    Mirrors the existing ``verifier._approved_fonts`` approach: an explicit
    approved/allowed list wins, otherwise every string in the mapping is a
    candidate.
    """
    if not isinstance(fonts, dict):
        return []
    for key in ("approved", "families", "allowed"):
        value = fonts.get(key)
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value if str(v or "").strip()]
    out: list[str] = []
    for value in fonts.values():
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
        elif isinstance(value, dict):
            for inner in value.values():
                if isinstance(inner, str) and inner.strip():
                    out.append(inner.strip())
    return out