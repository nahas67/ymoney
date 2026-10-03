"""Typed CreativeCommand union, catalog, validation and estimates (Lane B).

Commands are inert DATA:
- parsing never executes generated code,
- validation reads only (timeline rows, scenes, assets, the brand policy),
- only :mod:`app.engine.creative.director` mutates, and only through the
  canonical operation layer (``app.engine.timeline_ops``) plus the Work 02
  version system (``app.engine.timeline.save_version`` / ``restore_version``).

Risk classification drives the autonomous policy: ``auto_apply=True`` marks
low-risk commands that MAY run unattended when the workspace opts in via
``settings_json["creative_auto_apply"]``. Everything else always requires
approval. Brand/QC/budget hard constraints come from the effective creative
policy and are checked on every parse/preview/apply — never auto-waived.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, ClassVar

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------

SCOPE_TIMELINE = "timeline"
SCOPE_PLATFORM_VARIANT = "platform_variant"
SCOPE_CONTENT = "content"
SCOPES: tuple[str, ...] = (SCOPE_TIMELINE, SCOPE_PLATFORM_VARIANT, SCOPE_CONTENT)

#: persisted row lifecycle (models/creative.CreativeCommand.status)
COMMAND_STATUSES: tuple[str, ...] = (
    "parsed", "previewed", "applied", "rejected", "undone", "stale",
)

#: the ONLY components the generative UI may render (generative UI contract)
APPROVED_COMPONENTS: tuple[str, ...] = (
    "SceneInspector",
    "HookComparison",
    "VariantCard",
    "BrandCheck",
    "CaptionControl",
    "VoiceSelector",
    "AssetCandidate",
    "TimelineJump",
    "CostEstimate",
    "ApplyChange",
)

#: payload keys a generative schema may never carry — nothing executes them,
#: and validate-schema rejects the document outright (never evaluated).
FORBIDDEN_SCHEMA_KEYS: tuple[str, ...] = (
    "script", "javascript", "eval", "onclick", "onerror", "onload", "html",
)

#: primitive/layout types a generative ``type`` field may name, in addition to
#: the command types themselves.
ALLOWED_FIELD_TYPES: frozenset[str] = frozenset({
    "string", "number", "integer", "boolean", "object", "array", "enum",
    "text", "select", "slider", "color", "duration", "seconds", "aspect",
    "id", "json", "section", "group", "row", "column", "card", "label",
    "heading", "help", "toggle", "scene", "segment", "clip", "timeline",
})

RISK_ORDER: tuple[str, ...] = ("low", "medium", "high")


class CommandError(ValueError):
    """Base class for command-layer rejections (always carries a reason)."""


class UnknownCommandError(CommandError):
    """Raised for a type outside the approved command catalog."""


# -- Work 13 command validators ---------------------------------------------
# Each appends reason strings (empty == valid) and NEVER raises, so a bad
# command is rejected through the normal `reasons` channel rather than
# exploding mid-preview.


def _motion_policy(caption_style: dict | None):
    """Project a brand caption_style mapping onto the motion policy."""
    try:
        from app.engine.motion.policy import EffectiveMotionPolicy

        caption_style = dict(caption_style or {})
        return EffectiveMotionPolicy(
            approved_caption_presets=tuple(caption_style.get(
                "approved_presets") or caption_style.get("presets") or ()),
            caption_style=caption_style,
            motion_intensity=str(caption_style.get("motion_intensity")
                                 or "standard"),
            forbidden_effects=tuple(caption_style.get("forbidden_effects") or ()),
        )
    except Exception:
        return None


def _validate_motion_style_command(cmd, policy: dict | None,
                                   reasons: list[str]) -> None:
    from app.engine.captions.presets import UnknownPresetError, get_preset
    from app.engine.captions.style import CaptionStyle, CaptionStyleError
    from app.engine.motion.policy import MOTION_INTENSITIES

    p = cmd.payload()
    preset = str(p.get("preset") or "").strip()
    style = p.get("style") or {}
    if not preset and not isinstance(style, dict):
        reasons.append(f"{cmd.type} requires preset and/or style")
        return
    motion = _motion_policy((policy or {}).get("caption_style"))
    if preset:
        try:
            get_preset(preset)
        except UnknownPresetError as exc:
            reasons.append(str(exc))
            return
        if motion is not None and not motion.caption_preset_allowed(preset):
            reasons.append(
                f"blocked by brand rule: caption preset '{preset}' is not "
                f"approved for this workspace")
    if isinstance(style, dict) and style:
        try:
            CaptionStyle.from_dict({}).patch(style)
        except CaptionStyleError as exc:
            reasons.append(str(exc))
    intensity = str(p.get("motion_intensity") or "").strip()
    if intensity and intensity not in MOTION_INTENSITIES:
        reasons.append(
            f"motion_intensity {intensity!r} not in {list(MOTION_INTENSITIES)}")


def _validate_emphasis_command(cmd, doc: dict, reasons: list[str]) -> None:
    from app.engine.captions.emphasis import (
        EMPHASIS_KINDS,
        EmphasisError,
        assert_no_sensitive_kinds,
    )

    kinds = [str(k).strip().upper() for k in (cmd.kinds or []) if str(k or "").strip()]
    if not kinds:
        reasons.append("HighlightKeyword requires at least one emphasis kind")
        return
    unknown = [k for k in kinds if k not in EMPHASIS_KINDS]
    if unknown:
        reasons.append(f"HighlightKeyword: unknown kind(s) {unknown}")
        return
    try:
        assert_no_sensitive_kinds(kinds)
    except EmphasisError as exc:
        reasons.append(str(exc))


def _validate_motion_insert_command(cmd, reasons: list[str]) -> None:
    from app.engine.motion.lower_thirds import LOWER_THIRD_KINDS
    from app.engine.motion.templates import (
        MotionTemplateError,
        get_template,
        validate_instance,
    )

    p = cmd.payload()
    if cmd.type == "AddLowerThird":
        kind = str(p.get("kind") or "person").strip().lower()
        if kind not in LOWER_THIRD_KINDS:
            reasons.append(
                f"AddLowerThird: unknown kind {kind!r}; "
                f"known {sorted(LOWER_THIRD_KINDS)}")
            return
        template = LOWER_THIRD_KINDS[kind]
        # The command MUST declare which fields it can vouch for. An AI
        # command that asserts a name/role without saying where it came from is
        # exactly the "invented metadata" case the spec forbids, so an absent
        # or empty `known` is a rejection rather than a blank cheque.
        known = {str(k).strip() for k in (cmd.known or []) if str(k or "").strip()}
        if not known:
            reasons.append(
                "AddLowerThird must declare `known` (which of name/role/topic/"
                "source are real) - refusing to assert unvouched metadata")
            return
        candidate = {k: p.get(k) for k in ("name", "role", "topic", "source")
                     if str(p.get(k) or "").strip()}
        bindings = {k: v for k, v in candidate.items() if k in known}
        unvouched = sorted(set(candidate) - set(bindings))
        if unvouched:
            reasons.append(
                f"AddLowerThird: {unvouched} were supplied but are not in "
                f"`known`; dropping them rather than asserting them")
        if not bindings:
            reasons.append(
                "AddLowerThird has no known metadata - refusing to invent a "
                "name, role, topic or source")
            return
    else:
        template = {"AddTitle": "title_main",
                    "AddCallout": "callout_emphasis"}[cmd.type]
        slot = {"AddTitle": "headline", "AddCallout": "text"}[cmd.type]
        value = str(p.get(slot) or "").strip()
        if not value:
            reasons.append(f"{cmd.type} requires {slot!r}")
            return
        bindings = {slot: value}
    try:
        tpl = get_template(template)
        validate_instance(
            {"template": template, "bindings": bindings,
             "start": p.get("start", 0.0),
             "duration": p.get("duration") or tpl.default_duration},
            metadata_available=set(bindings),
        )
    except MotionTemplateError as exc:
        reasons.append(str(exc))


def _validate_transition_command(cmd, doc: dict, reasons: list[str]) -> None:
    from app.engine.motion.transitions import TransitionError, validate_transition

    p = cmd.payload()
    # ChangeTransition replaces an EXISTING transition; AddTransition creates
    # one. Both resolve the same way, but ChangeTransition must find the pair.
    type_field = "transition_type"
    if not str(p.get(type_field) or "").strip():
        reasons.append(f"{cmd.type} requires transition_type")
        return
    if not str(p.get("from_item") or "").strip():
        reasons.append(f"{cmd.type} requires from_item")
        return
    try:
        validate_transition({
            "from_item": p.get("from_item"),
            "to_item": p.get("to_item") or "",
            "type": p.get(type_field),
            "duration": p.get("duration", 0.5),
        }, doc)
    except TransitionError as exc:
        reasons.append(str(exc))


def _validate_animation_command(cmd, doc: dict, reasons: list[str]) -> None:
    """Keyframe animation commands: closed easing, real targets, legal values."""
    from app.engine.motion.graph import EASINGS

    p = cmd.payload()
    easing = str(p.get("easing") or "EASE_IN_OUT").strip().upper()
    if easing not in EASINGS:
        reasons.append(
            f"{cmd.type}: easing {easing!r} not in {list(EASINGS)}")
        return
    targets = _animation_targets(cmd, doc)
    if not targets:
        reasons.append(
            f"{cmd.type}: no animatable clip found on this timeline "
            f"(targets: {sorted(_animatable_kinds(doc))})")
        return
    for name, value in (("from_x", p.get("from_x")), ("to_x", p.get("to_x")),
                        ("from_y", p.get("from_y")), ("to_y", p.get("to_y"))):
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            reasons.append(f"{cmd.type}: {name} must be a number")
            return
        if not -4.0 <= number <= 4.0:
            reasons.append(f"{cmd.type}: {name}={number} outside -4..4")
            return
    for name in ("from_opacity", "to_opacity"):
        value = p.get(name)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            reasons.append(f"{cmd.type}: {name} must be a number")
            return
        if not 0.0 <= number <= 1.0:
            reasons.append(f"{cmd.type}: {name}={number} outside 0..1")
            return
    for name in ("from_scale", "to_scale"):
        value = p.get(name)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            reasons.append(f"{cmd.type}: {name} must be a number")
            return
        if not 0.01 <= number <= 8.0:
            reasons.append(f"{cmd.type}: {name}={number} outside 0.01..8")
            return


#: Clip kinds a keyframe animation can target (visual + overlays).
_ANIMATABLE_KINDS = ("video", "broll", "avatar", "text", "caption")


def _animatable_kinds(doc: dict) -> set[str]:
    return {str(tr.get("kind")) for tr in (doc or {}).get("tracks", [])
            if tr.get("kind") in _ANIMATABLE_KINDS}


def _animation_targets(cmd, doc: dict) -> list[tuple[str, str]]:
    """``(track_kind, clip_id)`` pairs this command will animate."""
    wanted = {str(c) for c in (cmd.clip_ids or []) if str(c or "").strip()}
    out: list[tuple[str, str]] = []
    for track in (doc or {}).get("tracks", []):
        kind = str(track.get("kind") or "")
        if kind not in _ANIMATABLE_KINDS:
            continue
        for clip in track.get("clips", []) or []:
            if not wanted or str(clip.get("id")) in wanted:
                out.append((kind, str(clip.get("id"))))
    return out


def _validate_effect_command(cmd, doc: dict, policy: dict | None,
                             reasons: list[str]) -> None:
    from app.engine.motion.effects import EffectError, validate_effect

    p = cmd.payload()
    try:
        validated = validate_effect(p.get("effect") or {})
    except EffectError as exc:
        reasons.append(str(exc))
        return
    motion = _motion_policy((policy or {}).get("caption_style"))
    if motion is not None and not motion.effect_allowed(validated["type"]):
        reasons.append(
            f"blocked by brand rule: effect '{validated['type']}' is not permitted")


def _caption_clip_ids(doc: dict, requested) -> list[str]:
    """Resolve target clip ids, defaulting to every caption clip."""
    clips = [c for tr in doc.get("tracks", []) if tr.get("kind") == "caption"
             for c in tr.get("clips", [])]
    if requested:
        known = {str(c.get("id")) for c in clips}
        wanted = [str(c) for c in requested if str(c) in known]
        return wanted
    return [str(c.get("id")) for c in clips if c.get("id")]


def _visual_clip_ids(doc: dict, requested, kinds=("video", "broll", "avatar")) -> list[str]:
    clips = [c for tr in doc.get("tracks", []) if tr.get("kind") in kinds
             for c in tr.get("clips", [])]
    if requested:
        known = {str(c.get("id")) for c in clips}
        return [str(c) for c in requested if str(c) in known]
    return [str(c.get("id")) for c in clips if c.get("id")]


# ---------------------------------------------------------------------------
# the union
# ---------------------------------------------------------------------------


@dataclass
class BaseCommand:
    """Envelope shared by every command type.

    ``target`` keys: timeline_id (always), scene_ids, scene_indices, clip_ids,
    segment_ids, platform, content_id. ``payload`` is per-type (the fields
    declared below the envelope).
    """

    target: dict = field(default_factory=dict)
    scope: str = SCOPE_TIMELINE
    note: str = ""

    type: ClassVar[str] = ""
    risk: ClassVar[str] = "medium"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[tuple[str, ...]] = ()
    description: ClassVar[str] = ""

    # -- serialization ----------------------------------------------------
    def payload(self) -> dict:
        base = {f.name for f in fields(BaseCommand)}
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name not in base}

    def to_dict(self) -> dict:
        """Flat, client-facing shape (payload keys at the top level)."""
        out = {
            "type": self.type,
            "scope": self.scope,
            "target": dict(self.target or {}),
            "note": self.note,
        }
        out.update(self.payload())
        return out

    @property
    def timeline_id(self) -> str:
        return str((self.target or {}).get("timeline_id") or "")

    @property
    def platform(self) -> str:
        return str((self.target or {}).get("platform") or "").strip().lower()


@dataclass
class ReplaceAsset(BaseCommand):
    """Swap the source asset behind clips (broad → always approval)."""

    asset_id: str = ""
    clip_ids: list[str] = field(default_factory=list)
    track: str = "video"

    type: ClassVar[str] = "ReplaceAsset"
    risk: ClassVar[str] = "high"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[tuple[str, ...]] = ("media_assets", "video_clips")
    description: ClassVar[str] = "Replace the source asset behind one or more clips."


@dataclass
class RegenerateScene(BaseCommand):
    """Queue a scene for regeneration (expensive side effects → approval)."""

    scene_ids: list[str] = field(default_factory=list)
    scene_indices: list[int] = field(default_factory=list)
    instruction: str = ""

    type: ClassVar[str] = "RegenerateScene"
    risk: ClassVar[str] = "high"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = False
    artifacts: ClassVar[tuple[str, ...]] = ("scenes", "render_jobs")
    description: ClassVar[str] = "Regenerate one or more scenes (new render cost)."


@dataclass
class RewriteHook(BaseCommand):
    """Rewrite the opening hook (high impact → approval)."""

    text: str = ""
    instruction: str = ""

    type: ClassVar[str] = "RewriteHook"
    risk: ClassVar[str] = "medium"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[tuple[str, ...]] = ("captions", "hook")
    description: ClassVar[str] = "Rewrite the first-frame hook text."


@dataclass
class RewriteSegment(BaseCommand):
    """Rewrite one narration/caption segment (low risk inside length bounds)."""

    text: str = ""
    segment_id: str = ""
    segment_index: int | None = None

    type: ClassVar[str] = "RewriteSegment"
    risk: ClassVar[str] = "low"
    auto_apply: ClassVar[bool] = True
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[tuple[str, ...]] = ("captions", "script")
    description: ClassVar[str] = "Rewrite a single segment's text in place."


@dataclass
class ChangeVoice(BaseCommand):
    """Swap the narration voice (validated against approved_voices)."""

    voice_id: str = ""
    gender: str = ""

    type: ClassVar[str] = "ChangeVoice"
    risk: ClassVar[str] = "medium"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[tuple[str, ...]] = ("voice_clips",)
    description: ClassVar[str] = "Change the narration voice on the voice track."


@dataclass
class ChangeCaptionPreset(BaseCommand):
    """Swap the caption style preset (low risk)."""

    preset: str = ""

    type: ClassVar[str] = "ChangeCaptionPreset"
    risk: ClassVar[str] = "low"
    auto_apply: ClassVar[bool] = True
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[tuple[str, ...]] = ("captions",)
    description: ClassVar[str] = "Apply a caption style preset to all caption clips."


@dataclass
class ChangeMusic(BaseCommand):
    """Swap the background music bed."""

    music_id: str = ""

    type: ClassVar[str] = "ChangeMusic"
    risk: ClassVar[str] = "medium"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[tuple[str, ...]] = ("music_clips",)
    description: ClassVar[str] = "Replace the background music track source."


@dataclass
class ChangeCTA(BaseCommand):
    """Change the call to action (low risk, text must be concrete)."""

    text: str = ""
    instruction: str = ""

    type: ClassVar[str] = "ChangeCTA"
    risk: ClassVar[str] = "low"
    auto_apply: ClassVar[bool] = True
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[tuple[str, ...]] = ("text_clips", "cta")
    description: ClassVar[str] = "Set the end-card call-to-action text."


@dataclass
class ChangeStyle(BaseCommand):
    """Restyle overlay text (font/color/size patches)."""

    patch: dict = field(default_factory=dict)
    style_name: str = ""

    type: ClassVar[str] = "ChangeStyle"
    risk: ClassVar[str] = "medium"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[tuple[str, ...]] = ("text_clips", "captions")
    description: ClassVar[str] = "Patch overlay text styling (color/font/size)."


@dataclass
class ChangeDuration(BaseCommand):
    """Shorten/extend runtime (broad structural change → approval)."""

    seconds: float = 0.0

    type: ClassVar[str] = "ChangeDuration"
    risk: ClassVar[str] = "high"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[tuple[str, ...]] = ("timeline", "clips")
    description: ClassVar[str] = "Change total runtime (trims/extends clips)."


@dataclass
class ChangeAspectRatio(BaseCommand):
    """Reframe the canvas for a platform cut."""

    aspect: str = ""

    type: ClassVar[str] = "ChangeAspectRatio"
    risk: ClassVar[str] = "medium"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[tuple[str, ...]] = ("timeline",)
    description: ClassVar[str] = "Change the timeline aspect ratio."


@dataclass
class CreateVariant(BaseCommand):
    """Fork the timeline as a new branch version (always approval)."""

    label: str = ""
    platform: str = ""

    type: ClassVar[str] = "CreateVariant"
    risk: ClassVar[str] = "high"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[tuple[str, ...]] = ("timeline_versions",)
    description: ClassVar[str] = "Fork the current timeline into a new variant branch."


@dataclass
class ApplyBrandPreset(BaseCommand):
    """Apply a named brand preset (captions/aspect/style bundle)."""

    preset: str = ""

    type: ClassVar[str] = "ApplyBrandPreset"
    risk: ClassVar[str] = "high"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[tuple[str, ...]] = ("captions", "timeline", "brand")
    description: ClassVar[str] = "Apply a workspace brand preset bundle to the timeline."


@dataclass
class ReframeScene(BaseCommand):
    """Face/subject-tracking reframe of the clips inside a scene."""

    scene_ids: list[str] = field(default_factory=list)
    scene_indices: list[int] = field(default_factory=list)
    focus: str = "center"

    type: ClassVar[str] = "ReframeScene"
    risk: ClassVar[str] = "medium"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[tuple[str, ...]] = ("video_clips", "scenes")
    description: ClassVar[str] = "Reframe scene clips around a focus point."


# -- Work 13 typed motion commands ------------------------------------------
# These perform REAL motion edits through the typed Work 13 schemas: nothing
# here can emit an arbitrary ffmpeg fragment, and every payload is validated
# by the caption/motion modules before the director plans an operation.


@dataclass
class ChangeCaptionStyle(BaseCommand):
    """Set a typed caption style and/or preset on caption clips."""

    preset: str = ""
    style: dict = field(default_factory=dict)
    clip_ids: list[str] = field(default_factory=list)

    type: ClassVar[str] = "ChangeCaptionStyle"
    risk: ClassVar[str] = "low"
    auto_apply: ClassVar[bool] = True
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[str] = ("captions", "brand")
    description: ClassVar[str] = "Apply a caption preset and/or typed style."


@dataclass
class HighlightKeyword(BaseCommand):
    """Recompute + store semantic emphasis for caption clips (§3)."""

    clip_ids: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)

    type: ClassVar[str] = "HighlightKeyword"
    risk: ClassVar[str] = "low"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[str] = ("captions",)
    description: ClassVar[str] = "Highlight keywords/numbers in captions."


@dataclass
class AddLowerThird(BaseCommand):
    """Insert a lower-third motion instance (§7)."""

    kind: str = "person"
    name: str = ""
    role: str = ""
    topic: str = ""
    source: str = ""
    start: float = 0.0
    duration: float = 4.0
    known: list[str] = field(default_factory=list)

    type: ClassVar[str] = "AddLowerThird"
    risk: ClassVar[str] = "medium"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[str] = ("text_clips", "captions")
    description: ClassVar[str] = "Add a lower third from KNOWN metadata only."


@dataclass
class AddTitle(BaseCommand):
    """Insert a TITLE motion instance (§6)."""

    headline: str = ""
    kicker: str = ""
    start: float = 0.0
    duration: float = 2.5

    type: ClassVar[str] = "AddTitle"
    risk: ClassVar[str] = "medium"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[str] = ("text_clips",)
    description: ClassVar[str] = "Add a title/kicker motion item."


@dataclass
class AddCallout(BaseCommand):
    """Insert a CALLOUT motion instance."""

    text: str = ""
    start: float = 0.0
    duration: float = 2.5

    type: ClassVar[str] = "AddCallout"
    risk: ClassVar[str] = "medium"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[str] = ("text_clips",)
    description: ClassVar[str] = "Add an emphasised callout."


@dataclass
class AddTransition(BaseCommand):
    """Attach a registry-backed transition between adjacent clips (§9)."""

    transition_type: str = ""
    from_item: str = ""
    to_item: str = ""
    duration: float = 0.5

    type: ClassVar[str] = "AddTransition"
    risk: ClassVar[str] = "medium"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[str] = ("video_clips",)
    description: ClassVar[str] = "Add a validated transition between clips."


@dataclass
class ApplyEffect(BaseCommand):
    """Attach a typed visual effect to a clip (§8)."""

    effect: dict = field(default_factory=dict)
    clip_ids: list[str] = field(default_factory=list)

    type: ClassVar[str] = "ApplyEffect"
    risk: ClassVar[str] = "medium"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[str] = ("video_clips",)
    description: ClassVar[str] = "Apply a registry-validated visual effect."


@dataclass
class RemoveEffect(BaseCommand):
    """Remove a typed visual effect from a clip (§8)."""

    effect: str = ""
    clip_ids: list[str] = field(default_factory=list)

    type: ClassVar[str] = "RemoveEffect"
    risk: ClassVar[str] = "low"
    auto_apply: ClassVar[bool] = True
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[str] = ("video_clips",)
    description: ClassVar[str] = "Remove a visual effect from clips."


@dataclass
class ApplyMotionPreset(BaseCommand):
    """Apply a caption preset AND the matching motion intensity (§4, §11)."""

    preset: str = ""
    motion_intensity: str = ""

    type: ClassVar[str] = "ApplyMotionPreset"
    risk: ClassVar[str] = "low"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[str] = ("captions", "brand")
    description: ClassVar[str] = "Apply a caption preset + motion intensity."


@dataclass
class AnimateElement(BaseCommand):
    """Animate a clip's canonical keyframes (position + easing)."""

    clip_ids: list[str] = field(default_factory=list)
    from_x: float = 0.0
    to_x: float = 0.5
    from_y: float = 0.5
    to_y: float = 0.5
    easing: str = "EASE_IN_OUT"

    type: ClassVar[str] = "AnimateElement"
    risk: ClassVar[str] = "medium"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[str] = ("timeline_versions",)
    description: ClassVar[str] = "Animate an element across the frame."


@dataclass
class MoveElement(BaseCommand):
    """Move an element by keyframing position only."""

    clip_ids: list[str] = field(default_factory=list)
    from_x: float = 0.0
    to_x: float = 0.5
    from_y: float = 0.5
    to_y: float = 0.5
    easing: str = "LINEAR"

    type: ClassVar[str] = "MoveElement"
    risk: ClassVar[str] = "medium"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[str] = ("timeline_versions",)
    description: ClassVar[str] = "Move an element to a position."


@dataclass
class AnimateOpacity(BaseCommand):
    """Fade an element in/out with canonical opacity keyframes."""

    clip_ids: list[str] = field(default_factory=list)
    from_opacity: float = 0.0
    to_opacity: float = 1.0
    easing: str = "EASE_IN_OUT"

    type: ClassVar[str] = "AnimateOpacity"
    risk: ClassVar[str] = "medium"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[str] = ("timeline_versions",)
    description: ClassVar[str] = "Fade an element with keyframes."


@dataclass
class AnimateScale(BaseCommand):
    """Scale an element up/down with canonical scale keyframes."""

    clip_ids: list[str] = field(default_factory=list)
    from_scale: float = 1.0
    to_scale: float = 1.4
    easing: str = "EASE_OUT"

    type: ClassVar[str] = "AnimateScale"
    risk: ClassVar[str] = "medium"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[str] = ("timeline_versions",)
    description: ClassVar[str] = "Scale an element with keyframes."


@dataclass
class ChangeTransition(AnimateElement):
    """Replace the transition on an existing clip pair."""

    transition_type: str = ""
    from_item: str = ""
    to_item: str = ""
    duration: float = 0.5

    type: ClassVar[str] = "ChangeTransition"
    risk: ClassVar[str] = "medium"
    auto_apply: ClassVar[bool] = False
    reversible: ClassVar[bool] = True
    artifacts: ClassVar[str] = ("video_clips",)
    description: ClassVar[str] = "Change an existing clip transition."


COMMAND_TYPES: dict[str, type[BaseCommand]] = {
    cls.type: cls
    for cls in (
        ReplaceAsset, RegenerateScene, RewriteHook, RewriteSegment, ChangeVoice,
        ChangeCaptionPreset, ChangeMusic, ChangeCTA, ChangeStyle, ChangeDuration,
        ChangeAspectRatio, CreateVariant, ApplyBrandPreset, ReframeScene,
        ChangeCaptionStyle, HighlightKeyword, AddLowerThird, AddTitle,
        AddCallout, AddTransition, ApplyEffect, RemoveEffect, ApplyMotionPreset,
        AnimateElement, MoveElement, AnimateOpacity, AnimateScale,
        ChangeTransition,
    )
}

#: low-risk commands the workspace may run unattended (auto-apply policy)
AUTO_APPLY_TYPES: tuple[str, ...] = tuple(
    t for t, cls in COMMAND_TYPES.items() if cls.auto_apply)

CreativeCommand = (
    ReplaceAsset | RegenerateScene | RewriteHook | RewriteSegment | ChangeVoice
    | ChangeCaptionPreset | ChangeMusic | ChangeCTA | ChangeStyle | ChangeDuration
    | ChangeAspectRatio | CreateVariant | ApplyBrandPreset | ReframeScene
    | ChangeCaptionStyle | HighlightKeyword | AddLowerThird | AddTitle
    | AddCallout | AddTransition | ApplyEffect | RemoveEffect | ApplyMotionPreset
    | AnimateElement | MoveElement | AnimateOpacity | AnimateScale
    | ChangeTransition
)


def command_from_dict(data: Any) -> BaseCommand:
    """Build a typed command from a wire dict. Unknown types raise (422)."""
    if isinstance(data, BaseCommand):
        return data
    if not isinstance(data, dict):
        raise UnknownCommandError(f"command must be an object, got {type(data).__name__}")
    raw_type = data.get("type", data.get("command"))
    cls = COMMAND_TYPES.get(str(raw_type or ""))
    if cls is None:
        raise UnknownCommandError(f"unknown command type '{raw_type}'")
    nested = data.get("payload")
    source = nested if isinstance(nested, dict) else data
    valid = {f.name for f in fields(cls)}
    kwargs = {k: v for k, v in source.items() if k in valid}
    cmd: BaseCommand = cls(**kwargs)
    cmd.scope = str(data.get("scope") or SCOPE_TIMELINE)
    cmd.target = dict(data.get("target") or {})
    cmd.note = str(data.get("note") or "")
    return cmd


def commands_to_dicts(commands: list[Any]) -> list[dict]:
    return [command_from_dict(c).to_dict() for c in commands or []]


# ---------------------------------------------------------------------------
# approved generative-UI schema validation (the only UI contract)
# ---------------------------------------------------------------------------


def schema_document_errors(payload: Any, *, _path: str = "$") -> list[str]:
    """Walk a generative UI schema and list everything outside the contract.

    Anything not in the approved component catalog / known type vocabulary is
    an error — the document is rejected (422) and never rendered or executed.
    """
    errors: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            k = str(key).lower()
            if k in FORBIDDEN_SCHEMA_KEYS:
                errors.append(f"{_path}.{key}: forbidden key (never executed)")
                continue
            if k in ("component", "widget", "renderer"):
                if str(value) not in APPROVED_COMPONENTS:
                    errors.append(
                        f"{_path}.{key}: component '{value}' is not in the approved catalog")
            elif (k in ("type", "command")
                  and str(value) not in COMMAND_TYPES
                  and str(value) not in ALLOWED_FIELD_TYPES):
                errors.append(f"{_path}.{key}: unknown type '{value}'")
            errors.extend(schema_document_errors(value, _path=f"{_path}.{key}"))
    elif isinstance(payload, list):
        for i, item in enumerate(payload):
            errors.extend(schema_document_errors(item, _path=f"{_path}[{i}]"))
    return errors


# ---------------------------------------------------------------------------
# validation (pure: returns rejection reasons, never mutates)
# ---------------------------------------------------------------------------


def _clips(doc: dict | None, kind: str) -> list[dict]:
    if not doc:
        return []
    for tr in doc.get("tracks", []):
        if tr.get("kind") == kind:
            return list(tr.get("clips", []))
    return []


def _clip_ids(doc: dict | None, kinds: tuple[str, ...]) -> list[str]:
    return [c.get("id", "") for k in kinds for c in _clips(doc, k)]


def _first_clip(doc: dict | None, kinds: tuple[str, ...]) -> tuple[str, dict] | tuple[None, None]:
    for kind in kinds:
        clips = sorted(_clips(doc, kind), key=lambda c: float(c.get("start", 0.0)))
        if clips:
            return kind, clips[0]
    return None, None


def _scenes_exist(session, workspace_id: str, timeline_id: str,
                  scene_ids: list[str]) -> bool:
    from sqlalchemy import select

    from app.models.assets import Scene

    if not scene_ids:
        return False
    rows = session.scalars(select(Scene.id).where(
        Scene.workspace_id == workspace_id,
        Scene.timeline_id == timeline_id,
        Scene.id.in_(list(scene_ids)))).all()
    return len(set(rows)) == len(set(scene_ids))


def _scene_count(session, workspace_id: str, timeline_id: str) -> int:
    from sqlalchemy import func, select

    from app.models.assets import Scene

    return int(session.scalar(select(func.count()).select_from(Scene).where(
        Scene.workspace_id == workspace_id,
        Scene.timeline_id == timeline_id)) or 0)


def _scene_refs_ok(cmd: BaseCommand, *, session, workspace_id: str,
                   timeline_id: str) -> tuple[bool, str]:
    ids = [str(s) for s in (cmd.payload().get("scene_ids") or [])]
    indices = [int(i) for i in (cmd.payload().get("scene_indices") or [])]
    if not ids and not indices:
        return False, "invalid target: no scene targeted (scene_ids/scene_indices)"
    if ids and not _scenes_exist(session, workspace_id, timeline_id, ids):
        return False, f"invalid target: scene {ids} not found on this timeline"
    if indices:
        count = _scene_count(session, workspace_id, timeline_id)
        bad = [i for i in indices if not 1 <= i <= max(count, 1)]
        if bad:
            return False, (f"invalid target: scene index {bad} out of range "
                           f"(timeline has {count} scene(s))")
    return True, ""


def _list(policy: dict | None, key: str) -> list[str]:
    values = (policy or {}).get(key) or []
    return [str(v).strip() for v in values if str(v).strip()]


def _matches(value: str, allowed: list[str]) -> bool:
    low = value.strip().lower()
    return any(low == a.strip().lower() for a in allowed)


def validate_command(cmd: BaseCommand, *, session, workspace_id: str,
                     timeline, doc: dict | None, policy: dict | None) -> list[str]:
    """Return rejection reasons (empty list = accepted). Hard constraints first."""
    reasons: list[str] = []
    if cmd.type not in COMMAND_TYPES:
        return [f"unknown command type '{cmd.type}'"]
    if cmd.scope not in SCOPES:
        reasons.append(f"invalid scope '{cmd.scope}' (expected one of {list(SCOPES)})")
    if cmd.scope == SCOPE_PLATFORM_VARIANT and not cmd.platform:
        reasons.append("invalid target: platform-scoped command requires target.platform")

    timeline_id = cmd.timeline_id
    if not timeline_id:
        reasons.append("invalid target: timeline_id is required")
    elif timeline is None:
        reasons.append("invalid target: timeline not found in this workspace")

    doc_ok = doc is not None
    p = cmd.payload()

    # -- brand / policy hard constraints (never auto-waived) --------------
    if cmd.type == "ChangeVoice":
        voice = str(p.get("voice_id") or "").strip()
        approved = _list(policy, "approved_voices")
        if not voice:
            reasons.append("ChangeVoice requires voice_id")
        elif approved and not _matches(voice, approved):
            reasons.append(
                f"blocked by brand rule: voice '{voice}' is not in approved_voices {approved}")
    elif cmd.type == "ChangeCaptionPreset":
        preset = str(p.get("preset") or "").strip()
        approved = _list(policy, "approved_caption_presets")
        if not preset:
            reasons.append("ChangeCaptionPreset requires preset")
        elif approved and not _matches(preset, approved):
            reasons.append(
                f"blocked by brand rule: caption preset '{preset}' is not in "
                f"approved_caption_presets {approved}")
    elif cmd.type == "ChangeAspectRatio":
        from app.engine.timeline import SUPPORTED_ASPECTS

        aspect = str(p.get("aspect") or "").strip()
        allowed = _list(policy, "allowed_aspect_ratios") or list(SUPPORTED_ASPECTS)
        if not aspect:
            reasons.append("ChangeAspectRatio requires aspect")
        elif aspect not in SUPPORTED_ASPECTS:
            reasons.append(
                f"unsupported aspect '{aspect}'; supported: {list(SUPPORTED_ASPECTS)}")
        elif not _matches(aspect, allowed):
            reasons.append(
                f"blocked by brand rule: aspect '{aspect}' is not allowed by policy {allowed}")
    elif cmd.type == "ApplyBrandPreset":
        preset = str(p.get("preset") or "").strip()
        known = _list(policy, "brand_presets")
        if not preset:
            reasons.append("ApplyBrandPreset requires preset")
        elif not known:
            reasons.append(
                f"blocked by brand rule: no brand preset is configured for this "
                f"workspace (wanted '{preset}')")
        elif not _matches(preset, known):
            reasons.append(
                f"blocked by brand rule: brand preset '{preset}' is not configured "
                f"(available: {known})")
    # -- Work 13 motion commands -------------------------------------------
    # Validation delegates to the Work 13 schemas, so an invalid style, an
    # unknown effect or a transition that does not fit its clips is refused
    # HERE, before preview or any timeline mutation.
    elif cmd.type in ("ChangeCaptionStyle", "ApplyMotionPreset"):
        _validate_motion_style_command(cmd, policy, reasons)
    elif cmd.type == "HighlightKeyword":
        _validate_emphasis_command(cmd, doc, reasons)
    elif cmd.type in ("AddLowerThird", "AddTitle", "AddCallout"):
        _validate_motion_insert_command(cmd, reasons)
    elif cmd.type == "AddTransition":
        _validate_transition_command(cmd, doc, reasons)
    elif cmd.type == "ApplyEffect":
        _validate_effect_command(cmd, doc, policy, reasons)
    elif cmd.type == "RemoveEffect":
        if not str(p.get("effect") or "").strip():
            reasons.append("RemoveEffect requires effect")
    elif cmd.type in ("AnimateElement", "MoveElement", "AnimateOpacity",
                      "AnimateScale"):
        _validate_animation_command(cmd, doc, reasons)
    elif cmd.type == "ChangeTransition":
        _validate_transition_command(cmd, doc, reasons)
    elif cmd.type == "ChangeDuration":
        seconds = float(p.get("seconds") or 0.0)
        minimum = float((policy or {}).get("min_duration_seconds") or 0.0)
        maximum = float((policy or {}).get("max_duration_seconds") or 0.0)
        if seconds <= 0:
            reasons.append("ChangeDuration requires seconds > 0")
        elif minimum and seconds < minimum:
            reasons.append(
                f"blocked by brand rule: duration {seconds}s is below the "
                f"minimum {minimum}s")
        elif maximum and seconds > maximum:
            reasons.append(
                f"blocked by brand rule: duration {seconds}s exceeds the "
                f"maximum {maximum}s")
        elif doc_ok:
            current = float(doc.get("duration_seconds") or 0.0)
            if current and seconds >= current:
                reasons.append(
                    f"invalid target: target duration {seconds}s is not shorter "
                    f"than the current {current}s")

    # -- target / payload integrity ---------------------------------------
    if not doc_ok and timeline_id and timeline is not None:
        reasons.append("invalid target: timeline tracks could not be loaded")

    if cmd.type == "ReplaceAsset" and doc_ok:
        if not str(p.get("asset_id") or "").strip():
            reasons.append("ReplaceAsset requires asset_id")
        else:
            from sqlalchemy import select

            from app.models.assets import MediaAsset

            row = session.scalars(select(MediaAsset.id).where(
                MediaAsset.workspace_id == workspace_id,
                MediaAsset.id == str(p["asset_id"]))).first()
            if row is None:
                reasons.append(
                    f"invalid target: asset '{p['asset_id']}' not found in this workspace")
        track = str(p.get("track") or "video")
        wanted = [str(c) for c in (p.get("clip_ids") or [])]
        known = {c.get("id") for c in _clips(doc, track)}
        missing = [c for c in wanted if c not in known]
        if missing:
            reasons.append(f"invalid target: clip(s) {missing} not on track '{track}'")
        elif not wanted and not known:
            reasons.append(f"invalid target: no clips on track '{track}'")

    elif cmd.type in ("RegenerateScene", "ReframeScene"):
        if timeline_id and timeline is not None:
            ok, why = _scene_refs_ok(cmd, session=session, workspace_id=workspace_id,
                                     timeline_id=timeline_id)
            if not ok:
                reasons.append(why)
        if cmd.type == "ReframeScene":
            focus = str(p.get("focus") or "center")
            if focus not in ("center", "left", "right", "top", "bottom", "face"):
                reasons.append(f"invalid focus '{focus}'")
            elif doc_ok and timeline_id and timeline is not None and not _clips(doc, "video"):
                reasons.append("invalid target: timeline has no video clips to reframe")

    elif cmd.type in ("RewriteHook", "RewriteSegment") and doc_ok and timeline_id:
        if cmd.type == "RewriteHook":
            kind, clip = _first_clip(doc, ("caption", "text"))
            if clip is None:
                reasons.append("invalid target: timeline has no caption/text clip for the hook")
        else:
            seg = str(p.get("segment_id") or "")
            ids = _clip_ids(doc, ("caption", "text", "voice"))
            if seg and seg not in ids:
                reasons.append(f"invalid target: segment '{seg}' not found on this timeline")
            elif not seg and not ids:
                reasons.append("invalid target: timeline has no caption/text/voice segments")
        text = str(p.get("text") or "")
        if not text and not str(p.get("instruction") or ""):
            reasons.append(f"{cmd.type} requires 'text' (or an 'instruction')")

    elif cmd.type == "ChangeVoice" and doc_ok and timeline_id:
        if p.get("voice_id") and not _clips(doc, "voice"):
            reasons.append("invalid target: timeline has no voice clips to re-voice")

    elif cmd.type == "ChangeCaptionPreset" and doc_ok and timeline_id:
        if p.get("preset") and not _clips(doc, "caption"):
            reasons.append("invalid target: timeline has no caption clips")

    elif cmd.type == "ChangeMusic" and doc_ok and timeline_id:
        if not str(p.get("music_id") or "").strip():
            reasons.append("ChangeMusic requires music_id")
        elif not _clips(doc, "music"):
            reasons.append("invalid target: timeline has no music clip to replace")

    elif cmd.type == "ChangeCTA" and doc_ok and timeline_id:
        if not str(p.get("text") or "").strip():
            reasons.append("ChangeCTA requires 'text'")
        elif not (float(doc.get("duration_seconds") or 0.0) > 0
                  or _clip_ids(doc, ("text", "caption"))):
            reasons.append("invalid target: timeline has no runtime to place a CTA")

    elif cmd.type == "ChangeStyle" and doc_ok and timeline_id:
        patch = p.get("patch") or {}
        if not patch and not str(p.get("style_name") or "").strip():
            reasons.append("ChangeStyle requires a style patch or style_name")
        elif not (_clip_ids(doc, ("text",)) or _clips(doc, "caption")):
            reasons.append("invalid target: timeline has no text/caption clips to style")

    elif cmd.type == "CreateVariant" and timeline_id and timeline is None:
        reasons.append("invalid target: timeline to fork was not found")

    return reasons


# ---------------------------------------------------------------------------
# estimates + human-readable change lines
# ---------------------------------------------------------------------------

#: heuristic fraction of the runtime that must be re-rendered per command type
RERENDER_FACTORS: dict[str, float] = {
    "ReplaceAsset": 1.0,
    "RegenerateScene": 1.5,
    "RewriteHook": 0.3,
    "RewriteSegment": 0.3,
    "ChangeVoice": 0.8,
    "ChangeCaptionPreset": 0.5,
    "ChangeMusic": 0.8,
    "ChangeCTA": 0.3,
    "ChangeStyle": 0.4,
    "ChangeDuration": 1.0,
    "ChangeAspectRatio": 1.0,
    "CreateVariant": 1.0,
    "ApplyBrandPreset": 0.6,
    "ReframeScene": 1.2,
}


def estimate_command(cmd: BaseCommand, *, duration_seconds: float,
                     base_cost_usd: float) -> dict:
    """Rerender seconds + cost heuristic (documented, deterministic)."""
    factor = float(RERENDER_FACTORS.get(cmd.type, 0.5))
    if cmd.scope == SCOPE_PLATFORM_VARIANT:
        factor *= 0.5  # one platform cut of the work, not the whole matrix
    seconds = round(max(float(duration_seconds), 0.0) * factor, 1)
    cost = round(max(float(base_cost_usd), 0.0) * factor, 4)
    return {
        "rerender_seconds": seconds,
        "cost_usd": cost,
        "heuristic": "runtime × per-command factor"
                      + (" × 0.5 (single platform cut)" if cmd.scope == SCOPE_PLATFORM_VARIANT
                         else ""),
    }


def _current_caption_preset(doc: dict | None) -> str:
    for clip in _clips(doc, "caption"):
        preset = str((clip.get("text") or {}).get("preset") or "").strip()
        if preset:
            return preset
    return "minimal"


def _current_voice(doc: dict | None) -> str:
    for clip in _clips(doc, "voice"):
        voice = str((clip.get("source") or {}).get("voice_id")
                    or (clip.get("source") or {}).get("voice") or "").strip()
        if voice:
            return voice
    return "auto"


def _current_music(doc: dict | None) -> str:
    for clip in _clips(doc, "music"):
        src = clip.get("source") or {}
        value = str(src.get("music_id") or src.get("track_id") or src.get("name") or "").strip()
        if value:
            return value
    return "none"


def _current_cta(doc: dict | None) -> str:
    for clip in sorted(_clips(doc, "text"), key=lambda c: float(c.get("start", 0.0))):
        content = str((clip.get("text") or {}).get("content") or clip.get("name") or "")
        if content:
            return content
    return ""


def describe_change(cmd: BaseCommand, *, doc: dict | None, policy: dict | None = None) -> list[str]:
    """Human-readable change lines for the preview ChangeSet."""
    p = cmd.payload()
    if cmd.type == "ChangeCaptionPreset":
        return [f"Caption preset: {_current_caption_preset(doc)} → {p.get('preset', '')}"]
    if cmd.type == "ChangeVoice":
        return [f"Voice: {_current_voice(doc)} → {p.get('voice_id', '')}"]
    if cmd.type == "ChangeMusic":
        return [f"Music: {_current_music(doc)} → {p.get('music_id', '')}"]
    if cmd.type == "ChangeDuration":
        current = float((doc or {}).get("duration_seconds") or 0.0)
        return [f"Duration: {current:g}s → {float(p.get('seconds') or 0):g}s"]
    if cmd.type == "ChangeAspectRatio":
        current = str((doc or {}).get("aspect_ratio") or "9:16")
        return [f"Aspect ratio: {current} → {p.get('aspect', '')}"]
    if cmd.type == "RewriteHook":
        if p.get("text"):
            return [f"Hook: {_hook_preview(doc)} → {p['text']}"]
        return [f"Hook: rewrite ({p.get('instruction') or 'stronger'})"]
    if cmd.type == "RewriteSegment":
        if p.get("segment_index") is not None:
            seg = f"#{p.get('segment_index')}"
        else:
            seg = str(p.get("segment_id") or "first")
        return [f"Segment {seg}: → {p.get('text', '')}"]
    if cmd.type == "ChangeCTA":
        current = _current_cta(doc)
        return [f"CTA: {current or '—'} → {p.get('text', '')}"]
    if cmd.type == "ReplaceAsset":
        wanted = [str(c) for c in (p.get("clip_ids") or [])]
        scope = f"{len(wanted)} clip(s)" if wanted else f"all '{p.get('track', 'video')}' clips"
        return [f"Asset: → {p.get('asset_id', '')} ({scope})"]
    if cmd.type == "RegenerateScene":
        refs = list(p.get("scene_ids") or []) or [f"#{i}" for i in (p.get("scene_indices") or [])]
        return [f"Scene {', '.join(str(r) for r in refs)}: regenerate"
                + (f" — {p['instruction']}" if p.get("instruction") else "")]
    if cmd.type == "ReframeScene":
        refs = list(p.get("scene_ids") or []) or [f"#{i}" for i in (p.get("scene_indices") or [])]
        return [f"Scene {', '.join(str(r) for r in refs)}: reframe focus={p.get('focus', 'center')}"]
    if cmd.type == "CreateVariant":
        return [f"Create variant: {p.get('label') or 'copy of current timeline'}"]
    if cmd.type == "ApplyBrandPreset":
        presets = _list(policy, "brand_presets")
        current = presets[0] if len(presets) == 1 and not _matches(p.get("preset", ""), presets) else "—"
        return [f"Brand preset: {current} → {p.get('preset', '')}"]
    if cmd.type == "ChangeStyle":
        patch = p.get("patch") or {}
        detail = ", ".join(f"{k}={v}" for k, v in patch.items())
        if p.get("style_name"):
            detail = f"{detail}, style={p['style_name']}" if detail else f"style={p['style_name']}"
        return [f"Style: {detail or 'reset'}"]
    return [f"{cmd.type}: {p or 'no payload'}"]


def _hook_preview(doc: dict | None) -> str:
    _, clip = _first_clip(doc, ("caption", "text"))
    if not clip:
        return "—"
    return str((clip.get("text") or {}).get("content") or clip.get("name") or "—")


def affected_artifacts(cmd: BaseCommand, *, doc: dict | None) -> dict:
    """Which tracks/clips/scenes a command touches (for the preview)."""
    p = cmd.payload()
    tracks: list[str] = []
    clips: list[str] = []
    mapping = {
        "ReplaceAsset": (str(p.get("track") or "video"),),
        "RegenerateScene": (),
        "RewriteHook": ("caption", "text"),
        "RewriteSegment": ("caption", "text", "voice"),
        "ChangeVoice": ("voice",),
        "ChangeCaptionPreset": ("caption",),
        "ChangeMusic": ("music",),
        "ChangeCTA": ("text",),
        "ChangeStyle": ("text", "caption"),
        "ChangeDuration": tuple(tr.get("kind") for tr in (doc or {}).get("tracks", [])),
        "ChangeAspectRatio": (),
        "CreateVariant": (),
        "ApplyBrandPreset": ("caption",),
        "ReframeScene": ("video",),
    }
    for kind in mapping.get(cmd.type, ()):
        if kind and _clips(doc, kind):
            tracks.append(kind)
            if cmd.type in ("ReplaceAsset",):
                wanted = [str(c) for c in (p.get("clip_ids") or [])]
                clips.extend(wanted or [str(c.get("id")) for c in _clips(doc, kind)])
            elif cmd.type == "ChangeDuration":
                clips.extend(str(c.get("id")) for c in _clips(doc, kind))
    scenes = [str(s) for s in (p.get("scene_ids") or [])]
    scenes += [f"#{i}" for i in (p.get("scene_indices") or [])]
    return {"tracks": sorted(set(tracks)), "clips": sorted(set(clips)), "scenes": scenes}


# ---------------------------------------------------------------------------
# generative UI catalog (the ONLY thing the frontend consumes)
# ---------------------------------------------------------------------------

TARGET_FIELDS: list[dict] = [
    {"name": "timeline_id", "type": "id", "required": True,
     "component": "TimelineJump", "description": "Timeline to edit"},
    {"name": "scene_ids", "type": "array", "component": "SceneInspector",
     "description": "Scene ids targeted by this command"},
    {"name": "clip_ids", "type": "array", "component": "TimelineJump",
     "description": "Clip ids targeted by this command"},
    {"name": "segment_ids", "type": "array", "component": "CaptionControl",
     "description": "Segment (clip) ids targeted by this command"},
    {"name": "platform", "type": "enum", "component": "VariantCard",
     "options": ["youtube", "tiktok", "instagram", "facebook"],
     "description": "Required for platform_variant scope"},
    {"name": "content_id", "type": "id", "component": "VariantCard",
     "description": "Content item this edit belongs to"},
]

#: per-command payload field descriptors (JSON-schema-ish, component-bound)
FIELD_SPECS: dict[str, list[dict]] = {
    "ReplaceAsset": [
        {"name": "asset_id", "type": "string", "required": True,
         "component": "AssetCandidate", "description": "Workspace asset id to swap in"},
        {"name": "clip_ids", "type": "array", "component": "TimelineJump",
         "description": "Clips to swap (empty = every clip on the track)"},
        {"name": "track", "type": "enum", "component": "TimelineJump",
         "options": ["video", "broll", "avatar"], "default": "video"},
    ],
    "RegenerateScene": [
        {"name": "scene_ids", "type": "array", "component": "SceneInspector"},
        {"name": "scene_indices", "type": "array", "component": "SceneInspector",
         "description": "1-based scene numbers as the user speaks them"},
        {"name": "instruction", "type": "text", "component": "SceneInspector"},
    ],
    "RewriteHook": [
        {"name": "text", "type": "text", "component": "HookComparison",
         "description": "Replacement hook text"},
        {"name": "instruction", "type": "text", "component": "HookComparison",
         "description": "Direction (e.g. 'stronger') when text is not yet drafted"},
    ],
    "RewriteSegment": [
        {"name": "text", "type": "text", "required": True, "component": "CaptionControl"},
        {"name": "segment_id", "type": "id", "component": "TimelineJump"},
        {"name": "segment_index", "type": "integer", "component": "TimelineJump"},
    ],
    "ChangeVoice": [
        {"name": "voice_id", "type": "string", "required": True,
         "component": "VoiceSelector", "description": "Must be policy-approved"},
        {"name": "gender", "type": "enum", "component": "VoiceSelector",
         "options": ["", "female", "male"]},
    ],
    "ChangeCaptionPreset": [
        {"name": "preset", "type": "string", "required": True, "component": "CaptionControl"},
    ],
    "ChangeMusic": [
        {"name": "music_id", "type": "string", "required": True,
         "component": "AssetCandidate"},
    ],
    "ChangeCTA": [
        {"name": "text", "type": "text", "required": True, "component": "ApplyChange"},
        {"name": "instruction", "type": "text", "component": "ApplyChange"},
    ],
    "ChangeStyle": [
        {"name": "patch", "type": "object", "component": "CaptionControl",
         "description": "Allowed keys: color, font, size, weight, align"},
        {"name": "style_name", "type": "string", "component": "CaptionControl"},
    ],
    "ChangeDuration": [
        {"name": "seconds", "type": "duration", "required": True,
         "component": "CostEstimate", "description": "Target runtime in seconds"},
    ],
    "ChangeAspectRatio": [
        {"name": "aspect", "type": "aspect", "required": True,
         "component": "TimelineJump", "options": ["9:16", "16:9", "1:1", "4:5"]},
    ],
    "CreateVariant": [
        {"name": "label", "type": "string", "component": "VariantCard"},
        {"name": "platform", "type": "enum", "component": "VariantCard",
         "options": ["youtube", "tiktok", "instagram", "facebook"]},
    ],
    "ApplyBrandPreset": [
        {"name": "preset", "type": "string", "required": True,
         "component": "BrandCheck", "description": "Configured brand preset name"},
    ],
    "ReframeScene": [
        {"name": "scene_ids", "type": "array", "component": "SceneInspector"},
        {"name": "scene_indices", "type": "array", "component": "SceneInspector"},
        {"name": "focus", "type": "enum", "component": "SceneInspector",
         "options": ["center", "left", "right", "top", "bottom", "face"],
         "default": "center"},
    ],
}


def command_catalog(policy: dict | None = None) -> dict:
    """Approved command types + risk classification + component-bound fields.

    This is the entire contract the generative UI may consume: it can compose
    ONLY these types with these components (``APPROVED_COMPONENTS``).
    """
    from app.engine.timeline import SUPPORTED_ASPECTS

    entries = []
    for name, cls in COMMAND_TYPES.items():
        entries.append({
            "type": name,
            "description": cls.description,
            "risk": cls.risk,
            "auto_apply": cls.auto_apply,
            "reversible": cls.reversible,
            "scopes": list(SCOPES),
            "affected_artifacts": list(cls.artifacts),
            "fields": list(TARGET_FIELDS) + list(FIELD_SPECS.get(name, [])),
        })
    return {
        "commands": entries,
        "components": list(APPROVED_COMPONENTS),
        "scopes": list(SCOPES),
        "statuses": list(COMMAND_STATUSES),
        "field_types": sorted(ALLOWED_FIELD_TYPES | set(COMMAND_TYPES)),
        "platforms": ["youtube", "tiktok", "instagram", "facebook"],
        "supported_aspects": list(SUPPORTED_ASPECTS),
        "forbidden_schema_keys": list(FORBIDDEN_SCHEMA_KEYS),
        "auto_apply": {
            "enabled": bool((policy or {}).get("creative_auto_apply", False)),
            "types": list(AUTO_APPLY_TYPES),
            "note": ("auto_apply types run unattended only when the workspace "
                     "enables creative_auto_apply; brand/QC/budget hard "
                     "constraints are never auto-waived"),
        },
        "hard_constraints": {
            "approved_voices": _list(policy, "approved_voices"),
            "approved_caption_presets": _list(policy, "approved_caption_presets"),
            "allowed_aspect_ratios": _list(policy, "allowed_aspect_ratios"),
            "brand_presets": _list(policy, "brand_presets"),
        },
        "policy_source": str((policy or {}).get("source") or "neutral_default"),
    }


__all__ = [
    "ALLOWED_FIELD_TYPES",
    "APPROVED_COMPONENTS",
    "AUTO_APPLY_TYPES",
    "ApplyBrandPreset",
    "BaseCommand",
    "COMMAND_STATUSES",
    "COMMAND_TYPES",
    "CommandError",
    "ChangeAspectRatio",
    "ChangeCTA",
    "ChangeCaptionPreset",
    "ChangeDuration",
    "ChangeMusic",
    "ChangeStyle",
    "ChangeVoice",
    "CreativeCommand",
    "CreateVariant",
    "FORBIDDEN_SCHEMA_KEYS",
    "FIELD_SPECS",
    "RERENDER_FACTORS",
    "ReframeScene",
    "RegenerateScene",
    "ReplaceAsset",
    "RewriteHook",
    "RewriteSegment",
    "SCOPES",
    "SCOPE_CONTENT",
    "SCOPE_PLATFORM_VARIANT",
    "SCOPE_TIMELINE",
    "TARGET_FIELDS",
    "UnknownCommandError",
    "affected_artifacts",
    "command_catalog",
    "command_from_dict",
    "commands_to_dicts",
    "describe_change",
    "estimate_command",
    "schema_document_errors",
    "validate_command",
]
