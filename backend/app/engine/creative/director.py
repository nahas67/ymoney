"""Creative Director: NL → typed commands → ChangeSet preview → versioned apply.

Contract:
- ``parse`` is deterministic FIRST (keyword/pattern matchers over the raw
  text); the DecisionEngine is consulted only as a SHADOW semantic fallback
  for phrasing the deterministic parser misses, and only ever yields a known
  command type — generated code is never executed.
- ``preview`` is read-only: it validates against the effective creative policy
  (Lane A ``resolve_effective_policy`` when present, otherwise a neutral
  default built from workspace brand settings) and returns change lines,
  affected artifacts, rerender/cost estimates, reversibility and approval flags.
- ``apply`` mutates exclusively through the canonical operation layer
  (``app.engine.timeline_ops``) and the Work 02 version system
  (``save_version``): the previous version row is preserved, the timeline
  version bumps, and the initiating user/agent + command JSON is audited.
  Stale previews (version moved or content manually edited) are refused with
  a REVIEW_REQUIRED error — manually edited work is never overwritten.
- ``undo`` restores the prior version through the parent-pointer system
  (``restore_version``), also append-only.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from loguru import logger

from app.engine.creative.commands import (
    AUTO_APPLY_TYPES,
    COMMAND_TYPES,
    ApplyBrandPreset,
    ChangeAspectRatio,
    ChangeCaptionPreset,
    ChangeCTA,
    ChangeDuration,
    ChangeMusic,
    ChangeStyle,
    ChangeVoice,
    CreateVariant,
    ReframeScene,
    RegenerateScene,
    ReplaceAsset,
    RewriteHook,
    RewriteSegment,
    UnknownCommandError,
    affected_artifacts,
    command_from_dict,
    commands_to_dicts,
    describe_change,
    estimate_command,
    validate_command,
)

# ---------------------------------------------------------------------------
# errors mapped to HTTP by the route layer
# ---------------------------------------------------------------------------


class CreativeError(Exception):
    """Base rejection with a machine-readable detail payload."""

    status_code = 422
    error = "CREATIVE_ERROR"

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.message = message
        override = detail.pop("status_code", None)
        if override is not None:
            self.status_code = int(override)
        self.detail: dict = {"error": self.error, "reason": message, **detail}


class CommandError(CreativeError):
    """Generic invalid request (bad target, missing payload, bad batch)."""

    error = "INVALID_COMMAND"


class RejectedCommands(CreativeError):
    error = "REJECTED"


class ApprovalRequired(CreativeError):
    error = "APPROVAL_REQUIRED"


class StalePreview(CreativeError):
    status_code = 409
    error = "REVIEW_REQUIRED"


class PreviewMismatch(CreativeError):
    status_code = 409
    error = "PREVIEW_MISMATCH"


# ---------------------------------------------------------------------------
# policy (Lane A brand core when present, neutral default otherwise)
# ---------------------------------------------------------------------------

_VOICE_KEYS = ("approved_voices", "voices")
_CAPTION_KEYS = ("approved_caption_presets", "caption_presets")
_ASPECT_KEYS = ("allowed_aspect_ratios", "aspect_ratios")
_PRESET_KEYS = ("brand_presets", "presets")


def _pick(obj: Any, keys: tuple[str, ...], default: Any = None) -> Any:
    """Read the first present key from a mapping OR an object (Lane A shape-agnostic)."""
    for key in keys:
        value = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
        if value is not None:
            return value
    return default


def _names(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(k) for k in value]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    if value:
        return [str(value)]
    return []


def _merged(*groups: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            key = item.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(item.strip())
    return out


def resolve_policy(session, workspace, *, campaign_id: str | None = None,
                   content_id: str | None = None, platform: str | None = None) -> dict:
    """Effective creative policy as a plain dict (never raises).

    Prefers Lane A's ``resolve_effective_policy`` (imported lazily — the brand
    core may land mid-run) and always unions the workspace brand settings on
    top so locally configured hard constraints apply either way. When the brand
    core is unavailable we degrade to a neutral default and say so via
    ``source``.
    """
    settings = dict(getattr(workspace, "settings_json", None) or {})
    brand = settings.get("brand") if isinstance(settings.get("brand"), dict) else {}
    brand = dict(brand or {})

    core: Any = None
    source = "neutral_default"
    try:
        from app.engine.brand.policy import resolve_effective_policy as _core_resolve

        core = _core_resolve(session, workspace.id, campaign_id=campaign_id,
                             content_id=content_id, platform=platform, overrides=None)
        source = "brand_core"
    except Exception as exc:  # noqa: BLE001 - Lane A may not exist yet / may differ
        logger.debug(f"[creative] brand core unavailable ({type(exc).__name__}): {exc}")

    presets_conf = brand.get("presets") if isinstance(brand.get("presets"), (dict, list)) else {}
    preset_names = _merged(_names(_pick(core, _PRESET_KEYS)), _names(presets_conf))
    preset_map = {str(k).lower(): v for k, v in dict(presets_conf).items()} \
        if isinstance(presets_conf, dict) else {}

    policy = {
        "approved_voices": _merged(_names(_pick(core, _VOICE_KEYS)),
                                   _names(brand.get("approved_voices"))),
        "approved_caption_presets": _merged(_names(_pick(core, _CAPTION_KEYS)),
                                            _names(brand.get("approved_caption_presets"))),
        "allowed_aspect_ratios": _merged(_names(_pick(core, _ASPECT_KEYS)),
                                         _names(brand.get("allowed_aspect_ratios"))),
        "brand_presets": preset_names,
        "brand_preset_map": preset_map,
        "min_duration_seconds": _number(_pick(core, ("min_duration_seconds",),
                                              brand.get("min_duration_seconds"))),
        "max_duration_seconds": _number(_pick(core, ("max_duration_seconds",),
                                              brand.get("max_duration_seconds"))),
        "creative_auto_apply": bool(settings.get("creative_auto_apply", False)),
        "source": source,
    }
    return policy


def _number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# voice / preset resolution helpers (deterministic, policy-driven)
# ---------------------------------------------------------------------------

_FEMALE_HINTS = ("ava", "aria", "jenny", "michelle", "sara", "sonia", "neerja",
                 "colette", "rachel", "bella", "amy", "joanna", "ruth", "hazel",
                 "madison", "lily", "grace", "allison", "amanda", "susan", "karen",
                 "victoria", "samantha", "emily", "nicole", "female", "heart",
                 "zoe", "clara", "sarah", "ann", "narrator_female")
_MALE_HINTS = ("andrew", "guy", "christopher", "eric", "roger", "davis", "stefan",
               "adam", "brian", "liam", "dan", "patrick", "william", "male",
               "james", "thomas", "robert", "mark", "guy_neural", "narrator_male")


def voice_gender(voice_id: str) -> str:
    low = str(voice_id or "").lower()
    female = any(h in low for h in _FEMALE_HINTS)
    male = any(h in low for h in _MALE_HINTS)
    if female and not male:
        return "female"
    if male and not female:
        return "male"
    return ""


def _pick_voice(approved: list[str], gender: str) -> str:
    if not approved:
        return ""
    if not gender:
        return approved[0]
    exact = [v for v in approved if voice_gender(v) == gender]
    if exact:
        return exact[0]
    unknown = [v for v in approved if not voice_gender(v)]
    if unknown:
        return unknown[0]  # gender undeterminable → cannot disprove the request
    return ""


# ---------------------------------------------------------------------------
# deterministic NL matchers (pure: text + context + policy → command | None)
# ---------------------------------------------------------------------------

_QUOTE_RE = re.compile(r"[\"“'‘]([^\"”'’]{2,400})[\"”'’]")
_ASPECT_RE = re.compile(r"\b(9:16|16:9|1:1|4:5)\b")
_DURATION_UNIT_RE = re.compile(
    r"(?:shorten|trim|cut|reduce|make|set|change|keep)\b[^0-9]{0,24}"
    r"(\d{1,4})\s*(?:seconds?|secs?|s\b)", re.I)
_DURATION_LOOSE_RE = re.compile(r"\b(\d{1,4})\s*(?:seconds?|secs?)\b", re.I)
_SEG_RE = re.compile(r"\bsegments?\b\s*#?(\d+)?", re.I)
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}

_ASPECT_WORDS = {
    "vertical": "9:16", "portrait": "9:16", "square": "1:1",
    "horizontal": "16:9", "landscape": "16:9", "widescreen": "16:9",
}
_PLATFORM_ALIASES = {"shorts": "youtube", "yt": "youtube", "reels": "instagram",
                     "ig": "instagram", "fb": "facebook", "tt": "tiktok"}


def _target(context: dict, **extra: Any) -> dict:
    target = {
        "timeline_id": str(context.get("timeline_id") or ""),
        "content_id": str(context.get("content_id") or "") or None,
        "campaign_id": str(context.get("campaign_id") or "") or None,
    }
    target.update(extra)
    return {k: v for k, v in target.items() if v}


def _quoted(text: str) -> str:
    m = _QUOTE_RE.search(text)
    return m.group(1).strip() if m else ""


def _scope_hint(text: str) -> tuple[str, str]:
    """Explicit 'only <platform>' phrasing scopes the batch to one platform cut."""
    low = text.lower()
    m = (re.search(r"only\s+(?:the\s+)?([a-z]+)\s+version", low)
         or re.search(r"only\s+on\s+([a-z]+)", low)
         or re.search(r"\bfor\s+([a-z]+)\s+only\b", low)
         or re.search(r"\b([a-z]+)\s+version\s+only\b", low))
    if m:
        return "platform_variant", _PLATFORM_ALIASES.get(m.group(1), m.group(1))
    return "timeline", ""


def _m_hook(text: str, context: dict, policy: dict) -> Any:
    if not re.search(r"\bhook\b", text):
        return None
    quote = _quoted(text)
    instruction = ""
    if not quote:
        m = re.search(r"\bhook\b[^a-z0-9]{0,12}(?:to|:)\s*(.{2,200}?)(?:[.;,]|$)", text, re.I)
        if m:
            quote = m.group(1).strip()
        else:
            m2 = re.search(r"\b(stronger|punchier|shorter|faster|bolder|sharper|"
                           r"clearer|catchier|edgier|warmer|funnier)\b", text, re.I)
            instruction = (m2.group(1).lower() if m2 else "stronger")
    cmd = RewriteHook(target=_target(context), text=quote, instruction=instruction)
    if quote:
        cmd.instruction = ""
    return cmd


def _m_segment(text: str, context: dict, policy: dict) -> Any:
    if not re.search(r"\bsegments?\b|\bnarration\b", text):
        return None
    quote = _quoted(text)
    seg_id = str(context.get("segment_id") or "")
    index: int | None = None
    m = _SEG_RE.search(text)
    if m and m.group(1):
        index = int(m.group(1))
    else:
        for word, num in _ORDINALS.items():
            if re.search(rf"\b{word}\s+segment\b", text, re.I):
                index = num
                break
    if not quote:
        m2 = re.search(r"\bsegments?\b[^a-z0-9]{0,12}(?:to|:)\s*(.{2,300}?)(?:[.;]|$)",
                       text, re.I)
        if m2:
            quote = m2.group(1).strip()
    if not quote and not index and not seg_id:
        return None
    return RewriteSegment(target=_target(context), text=quote, segment_id=seg_id,
                          segment_index=index)


def _m_voice(text: str, context: dict, policy: dict) -> Any:
    if not re.search(r"\bvoice\b|\bnarrat|\bspeaker\b|\bvo\b", text):
        return None
    gender = ""
    if re.search(r"\bfemale\b|\bwoman\b|\bgirl\b|\bher voice\b", text, re.I):
        gender = "female"
    elif re.search(r"\bmale\b|\bman\b|\bboy\b|\bhis voice\b", text, re.I):
        gender = "male"
    voice_id = ""
    quote = _quoted(text)
    if quote and re.search(r"\bvoice\b", text, re.I):
        voice_id = quote
    if not voice_id:
        m = re.search(r"\bvoice\b\s*(?:id\s*)?(?:to|=|:)\s*([A-Za-z0-9][\w\-.]{1,60})", text, re.I)
        if m:
            voice_id = m.group(1)
    if not voice_id:
        m = re.search(r"(?:use|switch(?:\s+to)?|set)\s+(?:the\s+)?voice\s+"
                      r"([A-Za-z0-9][\w\-.]{2,60})", text, re.I)
        if m and m.group(1).lower() not in ("to", "the", "approved", "a", "an"):
            voice_id = m.group(1)
    if not voice_id:
        approved = [str(v) for v in (policy.get("approved_voices") or [])]
        voice_id = _pick_voice(approved, gender)
    return ChangeVoice(target=_target(context), voice_id=voice_id, gender=gender)


def _known_caption_presets(policy: dict) -> list[str]:
    names = set(str(p) for p in (policy.get("approved_caption_presets") or []))
    try:
        from app.providers.clips import CAPTION_PRESETS

        names.update(CAPTION_PRESETS.keys())
    except Exception:  # noqa: BLE001 - registry is best-effort at parse time
        pass
    names.update({"pop", "minimal", "karaoke", "brand_primary"})
    return sorted(names)


def _m_caption(text: str, context: dict, policy: dict) -> Any:
    if not re.search(r"\bcaptions?\b|\bsubtitles?\b", text):
        return None
    preset = ""
    for pattern in (r"\bcaptions?\s+preset\s+(?:to|=|:)?\s*([A-Za-z0-9_\-]+)",
                    r"\bcaptions?\s+(?:to|=|:)\s*([A-Za-z0-9_\-]+)",
                    r"\bpreset\s+(?:to|=|:)?\s*([A-Za-z0-9_\-]+)"):
        m = re.search(pattern, text, re.I)
        if m:
            preset = m.group(1)
            break
    if not preset:
        for name in _known_caption_presets(policy):
            if re.search(rf"\b{re.escape(name)}\b", text, re.I):
                preset = name
                break
    if not preset:
        return None
    return ChangeCaptionPreset(target=_target(context), preset=preset)


def _m_duration(text: str, context: dict, policy: dict) -> Any:
    m = _DURATION_UNIT_RE.search(text) or _DURATION_LOOSE_RE.search(text)
    if not m:
        return None
    return ChangeDuration(target=_target(context), seconds=float(m.group(1)))


def _m_aspect(text: str, context: dict, policy: dict) -> Any:
    m = _ASPECT_RE.search(text)
    if m:
        return ChangeAspectRatio(target=_target(context), aspect=m.group(1))
    low = text.lower()
    if re.search(r"\baspect\b|\bratio\b|\bvertical\b|\blandscape\b|\bhorizontal\b|\bsquare\b",
                 low):
        for word, aspect in _ASPECT_WORDS.items():
            if re.search(rf"\b{word}\b", low):
                return ChangeAspectRatio(target=_target(context), aspect=aspect)
        if re.search(r"\baspect\b|\bratio\b", low):
            return None  # ratio mentioned without a value → nothing to set
    return None


def _m_cta(text: str, context: dict, policy: dict) -> Any:
    if not re.search(r"\bcta\b|\bcall to action\b", text):
        return None
    quote = _quoted(text)
    instruction = ""
    if not quote:
        m = re.search(r"\bcta\b[^a-z0-9]{0,12}(?:to|:)\s*(.{2,200}?)(?:[.;,]|$)", text, re.I)
        if m:
            quote = m.group(1).strip()
        else:
            instruction = "update"
    return ChangeCTA(target=_target(context), text=quote, instruction=instruction)


def _m_music(text: str, context: dict, policy: dict) -> Any:
    if not re.search(r"\bmusic\b|\bsoundtrack\b|\bbackground track\b", text):
        return None
    music_id = ""
    quote = _quoted(text)
    if quote:
        music_id = quote
    if not music_id:
        m = re.search(r"\bmusic\b\s*(?:to|=|:)\s*([A-Za-z0-9][\w\-.]{1,60})", text, re.I)
        if m:
            music_id = m.group(1)
    return ChangeMusic(target=_target(context), music_id=music_id)


def _m_style(text: str, context: dict, policy: dict) -> Any:
    patch: dict = {}
    style_name = ""
    m = re.search(r"\b(?:style|theme|font)\s+(?:to|=|:)\s*([A-Za-z0-9][\w\-\s]{0,40})",
                  text, re.I)
    if m:
        style_name = m.group(1).strip()
    m = re.search(r"\bcolou?r\s+(?:to|=|:)\s*(#[0-9a-fA-F]{3,8}|\w{3,20})", text, re.I)
    if m:
        patch["color"] = m.group(1)
    m = re.search(r"\bfont\s+(?:to|=|:)\s*([A-Za-z][\w\s\-]{2,30})", text, re.I)
    if m and not style_name:
        patch["font"] = m.group(1).strip()
    m = re.search(r"\b(?:text|overlay)s?\s+(?:to|=|:)\s*(bold|uppercase|lowercase)\b",
                  text, re.I)
    if m:
        patch["weight"] = "bold" if m.group(1).lower() == "bold" else "normal"
    if not patch and not style_name:
        return None
    return ChangeStyle(target=_target(context), patch=patch, style_name=style_name)


def _m_replace(text: str, context: dict, policy: dict) -> Any:
    m = re.search(r"\breplace\s+(?:the\s+)?(?:asset|clip|footage|b\-?roll)s?\b"
                  r"[^.]{0,40}?(?:with|to|:)\s*([A-Za-z0-9][\w\-.]{2,60})", text, re.I)
    if not m:
        return None
    return ReplaceAsset(target=_target(context), asset_id=m.group(1))


def _m_regen(text: str, context: dict, policy: dict) -> Any:
    if not re.search(r"\bregenerat", text):
        return None
    indices = [int(n) for n in re.findall(r"\d+", _scene_clause(text))]
    scene_ids = [str(s) for s in (context.get("scene_ids") or [])] if not indices else []
    instruction = _quoted(text)
    if not instruction:
        m = re.search(r"\bregenerat\w*\b(?:\s+the)?\s+scenes?\b[^a-z0-9]{0,12}"
                      r"(?:to|:)\s*(.{2,160}?)(?:[.;]|$)", text, re.I)
        instruction = m.group(1).strip() if m else ""
    return RegenerateScene(target=_target(context), scene_ids=scene_ids,
                           scene_indices=indices, instruction=instruction)


def _m_reframe(text: str, context: dict, policy: dict) -> Any:
    if not re.search(r"\breframe\b|\bre-frame\b|\bface[- ]track", text):
        return None
    focus = "center"
    for word in ("left", "right", "top", "bottom", "face", "center"):
        if re.search(rf"\b{word}\b", text, re.I):
            focus = word
            break
    indices = [int(n) for n in re.findall(r"\d+", _scene_clause(text))]
    scene_ids = [str(s) for s in (context.get("scene_ids") or [])] if not indices else []
    return ReframeScene(target=_target(context), scene_ids=scene_ids,
                        scene_indices=indices, focus=focus)


def _scene_clause(text: str) -> str:
    m = re.search(r"\bscenes?\b[^.]{0,40}", text, re.I)
    return m.group(0) if m else ""


def _m_variant(text: str, context: dict, policy: dict) -> Any:
    m = re.search(r"\b(?:create|make|fork|duplicate|spin(?:\s+off)?)\b[^.]{0,30}?"
                  r"\bvariant\b", text, re.I)
    if not m:
        return None
    label = _quoted(text)
    if not label:
        m2 = re.search(r"\bvariant\b\s*(?:called|named|:)\s*([A-Za-z0-9_\- ]{2,40})",
                       text, re.I)
        label = m2.group(1).strip() if m2 else ""
    platform = str(context.get("platform") or "").strip().lower()
    return CreateVariant(target=_target(context), label=label, platform=platform)


def _m_brand(text: str, context: dict, policy: dict) -> Any:
    m = re.search(r"\b(?:brand\s+preset|apply\s+(?:the\s+)?brand)\b[^a-z0-9]{0,12}"
                  r"(?:to|=|:)?\s*([A-Za-z0-9_\-]{2,40})", text, re.I)
    if not m:
        return None
    preset = m.group(1)
    if preset.lower() in ("look", "style", "kit", "voice"):
        return None
    return ApplyBrandPreset(target=_target(context), preset=preset)


#: deterministic matcher registry — also used to finish an SHADOW-mode guess
MATCHERS: dict[str, Any] = {
    "RewriteHook": _m_hook,
    "RewriteSegment": _m_segment,
    "ChangeVoice": _m_voice,
    "ChangeCaptionPreset": _m_caption,
    "ChangeDuration": _m_duration,
    "ChangeAspectRatio": _m_aspect,
    "ChangeCTA": _m_cta,
    "ChangeMusic": _m_music,
    "ChangeStyle": _m_style,
    "ReplaceAsset": _m_replace,
    "RegenerateScene": _m_regen,
    "ReframeScene": _m_reframe,
    "CreateVariant": _m_variant,
    "ApplyBrandPreset": _m_brand,
}


# ---------------------------------------------------------------------------
# director
# ---------------------------------------------------------------------------


def _doc_of(row) -> dict:
    doc = dict(row.tracks_json or {})
    doc.setdefault("fps", getattr(row, "fps", 30.0))
    doc.setdefault("duration_seconds", getattr(row, "duration_seconds", 0.0))
    doc.setdefault("aspect_ratio", "9:16")
    return doc


def _manifest_hash(doc: dict) -> str:
    from app.engine.timeline import TimelineValidationError, render_manifest

    try:
        return str(render_manifest(dict(doc)).get("manifest_hash") or "")
    except TimelineValidationError:
        return ""


def _load_timeline(session, workspace, timeline_id: str):
    if not timeline_id:
        return None
    from app.models import ContentTimeline

    row = session.get(ContentTimeline, timeline_id)
    if row is None or row.workspace_id != workspace.id:
        return None
    return row


def _base_cost() -> float:
    from app.core.config import settings as cfg

    try:
        return float(getattr(cfg, "mpt_estimated_render_cost_usd", 0.02) or 0.0)
    except (TypeError, ValueError):
        return 0.02


def _within_length_bounds(cmd) -> bool:
    """Auto-apply only covers bounded text edits."""
    payload = cmd.payload()
    text = str(payload.get("text") or "")
    limit = {"RewriteSegment": 600, "ChangeCTA": 300}.get(cmd.type, 600)
    return len(text) <= limit


class CreativeDirector:
    """NL → typed commands → preview → apply, with version safety throughout."""

    # -- 1. parse ---------------------------------------------------------

    @staticmethod
    def parse(session, workspace, text: str, *, context: dict | None = None,
              actor: str = "user", user_id: str | None = None,
              persist: bool = True) -> list:
        """Natural language → typed CreativeCommand list (deterministic first)."""
        context = dict(context or {})
        raw = str(text or "").strip()
        if not raw:
            return []
        policy = resolve_policy(session, workspace)
        commands = _deterministic_parse(raw, context, policy)
        source = "deterministic"
        if not commands:
            commands = _shadow_parse(session, workspace, raw, context, policy)
            source = "shadow" if commands else "none"
        if persist:
            _save_record(
                session, workspace, status="parsed",
                timeline_id=str(context.get("timeline_id") or ""), actor=actor,
                user_id=user_id, text_input=raw, commands=commands,
                change_set={"source": source, "context": context})
            session.commit()
        return commands

    # -- 2. preview (read-only) ------------------------------------------

    @staticmethod
    def preview(session, workspace, commands, *, actor: str = "user",
                user_id: str | None = None, text_input: str = "") -> dict:
        """Validate + describe without mutating the timeline."""
        cmds = _coerce(commands)
        if not cmds:
            raise CommandError("no commands supplied")
        policy = resolve_policy(session, workspace)
        auto_enabled = bool(policy.get("creative_auto_apply"))
        rows: dict[str, Any] = {}
        docs: dict[str, dict | None] = {}
        entries: list[dict] = []
        batch_timeline = ""
        ok_seen = False

        for index, cmd in enumerate(cmds):
            tid = cmd.timeline_id
            if not batch_timeline and tid:
                batch_timeline = tid
            if tid and batch_timeline and tid != batch_timeline:
                entries.append(_entry(
                    index, cmd, ["invalid target: batch targets multiple timelines "
                                 "(preview/apply one timeline at a time)"],
                    doc=None, policy=policy, auto_enabled=auto_enabled))
                continue
            if tid and tid not in rows:
                row = _load_timeline(session, workspace, tid)
                rows[tid] = row
                docs[tid] = _doc_of(row) if row is not None else None
            row = rows.get(tid)
            doc = docs.get(tid)
            reasons = validate_command(cmd, session=session, workspace_id=workspace.id,
                                       timeline=row, doc=doc, policy=policy)
            if not reasons:
                ok_seen = True
            entries.append(_entry(index, cmd, reasons, doc=doc, policy=policy,
                                  auto_enabled=auto_enabled))

        row = _load_timeline(session, workspace, batch_timeline) if batch_timeline else None
        doc = _doc_of(row) if row is not None else None
        change_set = {
            "timeline_id": str(batch_timeline),
            "timeline_version": int(getattr(row, "version", 1) or 1) if row else None,
            "manifest_hash": _manifest_hash(doc) if doc else "",
            "policy_source": str(policy.get("source") or "neutral_default"),
            "auto_apply_enabled": auto_enabled,
            "changes": entries,
            "totals": _totals(entries),
            "lessons": _lessons(session, workspace, entries),
        }
        record = _save_record(
            session, workspace, status="previewed" if ok_seen else "rejected",
            timeline_id=batch_timeline, actor=actor, user_id=user_id,
            text_input=text_input, commands=cmds, change_set=change_set,
            parent_version=change_set["timeline_version"])
        session.commit()
        change_set["preview_id"] = record.id
        return change_set

    # -- 3. apply (mutates through canonical ops + versions) --------------

    @staticmethod
    def apply(session, workspace, commands, *, actor: str = "user",
              user_id: str | None = None, approve: bool = False,
              base_version: int | None = None, preview_record_id: str | None = None,
              text_input: str = "") -> dict:
        """Validate → stale gate → approval gate → canonical ops → new version."""
        preview_row = None
        if preview_record_id:
            from app.models import CreativeCommandRow

            preview_row = session.get(CreativeCommandRow, preview_record_id)
            if preview_row is None or preview_row.workspace_id != workspace.id:
                raise CommandError("preview not found", preview_id=preview_record_id)

        cmds = _coerce(commands)
        if not cmds and preview_row is not None:
            cmds = [command_from_dict(d) for d in (preview_row.commands_json or [])]
        if not cmds:
            raise CommandError("no commands supplied")

        if preview_row is not None:
            posted = commands_to_dicts(cmds)
            stored = list(preview_row.commands_json or [])
            if posted != stored:
                raise PreviewMismatch(
                    "commands differ from the previewed set — preview again",
                    preview_id=str(preview_row.id))

        timeline_id = cmds[0].timeline_id
        if any(c.timeline_id != timeline_id for c in cmds):
            raise CommandError("apply is scoped to a single timeline batch")
        row = _load_timeline(session, workspace, timeline_id)
        if row is None:
            raise CommandError("invalid target: timeline not found in this workspace",
                               timeline_id=timeline_id)

        expected = base_version if base_version is not None else (
            int(preview_row.parent_version) if preview_row is not None
            and preview_row.parent_version is not None else None)
        if expected is None:
            raise CommandError("base_version or preview_id is required for apply")

        doc = _doc_of(row)
        current_hash = _manifest_hash(doc)
        if int(row.version or 1) != int(expected):
            _mark_stale(preview_row, reason="timeline version moved since preview")
            session.commit()  # the refusal itself is durable (status=stale)
            raise StalePreview(
                "timeline changed since this preview — reload and review again",
                expected_version=int(expected), actual_version=int(row.version),
                timeline_id=str(row.id))
        stored_hash = ""
        if preview_row is not None:
            stored_hash = str((preview_row.change_set_json or {}).get("manifest_hash") or "")
        if stored_hash and current_hash and stored_hash != current_hash:
            _mark_stale(preview_row, reason="timeline was edited manually since preview")
            session.commit()  # the refusal itself is durable (status=stale)
            raise StalePreview(
                "the timeline was edited manually after this preview — "
                "manually edited work is never overwritten",
                expected_hash=stored_hash, actual_hash=current_hash,
                timeline_id=str(row.id))

        policy = resolve_policy(session, workspace)
        reasons_by_index: list[list[str]] = []
        for cmd in cmds:
            reasons_by_index.append(validate_command(
                cmd, session=session, workspace_id=workspace.id, timeline=row,
                doc=doc, policy=policy))
        if any(reasons_by_index):
            flat = [r for rs in reasons_by_index for r in rs]
            _save_record(session, workspace, status="rejected",
                         timeline_id=str(row.id), actor=actor, user_id=user_id,
                         text_input=text_input, commands=cmds,
                         change_set={"reasons": reasons_by_index},
                         parent_version=int(row.version or 1))
            session.commit()
            raise RejectedCommands("commands rejected by validation",
                                   reasons=flat, detail_reasons=reasons_by_index)

        auto_enabled = bool(policy.get("creative_auto_apply"))
        pending = [c.type for c in cmds
                   if not (c.type in AUTO_APPLY_TYPES and auto_enabled
                           and _within_length_bounds(c))]
        if pending and not approve:
            raise ApprovalRequired(
                "approval required before these commands can be applied",
                commands=sorted(set(pending)),
                auto_apply_enabled=auto_enabled,
                enable_hint="settings_json['creative_auto_apply'] unlocks "
                            "low-risk commands only")

        try:
            result = _execute(session, workspace, row=row, doc=doc, cmds=cmds,
                              policy=policy, actor=actor, user_id=user_id,
                              approve=approve, text_input=text_input,
                              expected_version=int(expected), preview_row=preview_row)
        except CreativeError as exc:
            # audit the refused apply (never leaves a half-applied timeline)
            _save_record(session, workspace, status="rejected",
                         timeline_id=str(row.id), actor=actor, user_id=user_id,
                         text_input=text_input, commands=cmds,
                         change_set={"apply_error": exc.message},
                         parent_version=int(row.version or 1))
            session.commit()
            raise
        session.commit()
        from app.services.events import record_event

        record_event(
            workspace.id, "creative.applied",
            f"Creative Director applied {len(cmds)} command(s) "
            f"({', '.join(sorted({c.type for c in cmds}))})",
            level="info", source="creative_director",
            data={"timeline_id": result["timeline_id"],
                  "version": result["version"],
                  "actor": actor, "user_id": user_id,
                  "commands": [c.type for c in cmds]})
        return result

    # -- 4. undo (parent-pointer restore) ---------------------------------

    @staticmethod
    def undo(session, workspace, timeline_id: str, *, actor: str = "user",
             user_id: str | None = None) -> dict:
        """Restore the version the last apply replaced (append-only)."""
        from sqlalchemy import select

        from app.engine.timeline import restore_version
        from app.models import ContentTimeline, CreativeCommandRow

        row = session.scalars(
            select(CreativeCommandRow).where(
                CreativeCommandRow.workspace_id == workspace.id,
                CreativeCommandRow.timeline_id == timeline_id,
                CreativeCommandRow.status == "applied",
            ).order_by(CreativeCommandRow.created_at.desc(),
                       CreativeCommandRow.id.desc())).first()
        if row is None:
            raise CommandError("no applied creative command to undo for this timeline",
                               timeline_id=timeline_id, status_code=404)
        result = dict(row.result_json or {})
        previous_id = str(result.get("previous_timeline_id") or "")
        if not previous_id or not result.get("version_bumped"):
            raise CommandError("last change is not version-reversible",
                               record_id=str(row.id))
        current = session.get(ContentTimeline, timeline_id)
        if current is None or current.workspace_id != workspace.id:
            raise CommandError("timeline not found in this workspace",
                               timeline_id=timeline_id, status_code=404)
        applied_hash = str(result.get("applied_manifest_hash") or "")
        if applied_hash and _manifest_hash(_doc_of(current)) != applied_hash:
            raise StalePreview(
                "the timeline was edited after this change — undo would discard "
                "manually edited work",
                timeline_id=str(timeline_id))
        try:
            new_id = restore_version(session, timeline_id, previous_id)
        except Exception as exc:  # noqa: BLE001 - mapped to a clean 404/409
            raise CommandError(f"restore failed: {exc}", timeline_id=timeline_id,
                               status_code=404) from exc
        child = session.get(ContentTimeline, new_id)
        row.status = "undone"
        row.result_json = {**result, "undone_timeline_id": str(new_id),
                           "undone_by": actor, "undone_by_user_id": user_id}
        session.flush()
        session.commit()
        record_event_undo(workspace, timeline_id=timeline_id, new_id=new_id,
                          actor=actor, user_id=user_id, record_id=str(row.id))
        return {"status": "undone", "record_id": str(row.id),
                "timeline_id": str(new_id),
                "previous_timeline_id": str(previous_id),
                "version": int(getattr(child, "version", 0) or 0)}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def record_event_undo(workspace, *, timeline_id: str, new_id: str, actor: str,
                      user_id: str | None, record_id: str) -> None:
    from app.services.events import record_event

    record_event(workspace.id, "creative.undone",
                 "Creative Director undid the last applied change",
                 level="info", source="creative_director",
                 data={"timeline_id": timeline_id, "restored_to": new_id,
                       "record_id": record_id, "actor": actor, "user_id": user_id})


def _coerce(commands) -> list:
    if commands is None:
        return []
    if isinstance(commands, (str, bytes)) or not isinstance(commands, (list, tuple)):
        raise UnknownCommandError("commands must be a list of command objects")
    return [command_from_dict(c) for c in commands]


def _apply_scope(cmd, *, scope: str, platform: str, context: dict) -> None:
    """Attach scope + platform metadata to a freshly parsed command."""
    cmd.scope = scope
    target = dict(cmd.target or {})
    chosen = platform or str(context.get("platform") or "").strip().lower()
    if chosen:
        target["platform"] = chosen
    cmd.target = target


def _deterministic_parse(text: str, context: dict, policy: dict) -> list:
    scope, platform = _scope_hint(text)
    commands: list = []
    for name, matcher in MATCHERS.items():
        try:
            cmd = matcher(text, context, policy)
        except Exception as exc:  # noqa: BLE001 - one bad pattern never kills parse
            logger.debug(f"[creative] matcher {name} failed: {type(exc).__name__}: {exc}")
            continue
        if cmd is None:
            continue
        _apply_scope(cmd, scope=scope, platform=platform, context=context)
        commands.append(cmd)
    return commands


def _shadow_parse(session, workspace, text: str, context: dict, policy: dict) -> list:
    """SHADOW semantic fallback: known command types only, never code."""
    try:
        from app.engine.intelligence.decision import DecisionEngine
    except Exception as exc:  # noqa: BLE001 - intelligence layer optional here
        logger.debug(f"[creative] DecisionEngine unavailable: {type(exc).__name__}")
        return []
    try:
        engine = DecisionEngine(str(workspace.id), mode="SHADOW", persist=True)
        output, record = engine.classify({
            "item": text, "text": text,
            "criterion": "which creative edit command does this request mean",
            "labels": list(COMMAND_TYPES),
        })
        label = str((output or {}).get("label") or "")
        confidence = float((output or {}).get("confidence") or 0.0)
        if label not in COMMAND_TYPES or confidence < 0.15:
            return []
        matcher = MATCHERS.get(label)
        cmd = matcher(text, context, policy) if matcher else None
        if cmd is None:
            cls = COMMAND_TYPES[label]
            cmd = cls(target=_target(context))
        scope, platform = _scope_hint(text)
        _apply_scope(cmd, scope=scope, platform=platform, context=context)
        logger.debug(f"[creative] SHADOW fallback chose {label} "
                     f"(confidence={confidence}, agreement={record.agreement})")
        return [cmd]
    except Exception as exc:  # noqa: BLE001 - shadow must never break parse
        logger.debug(f"[creative] shadow parse failed: {type(exc).__name__}: {exc}")
        return []


def _entry(index: int, cmd, reasons: list[str], *, doc, policy: dict,
           auto_enabled: bool) -> dict:
    status = "ok" if not reasons else "rejected"
    reversible = bool(getattr(cmd, "reversible", True))
    auto_ok = (cmd.type in AUTO_APPLY_TYPES and auto_enabled and _within_length_bounds(cmd))
    return {
        "index": index,
        "type": cmd.type,
        "scope": cmd.scope,
        "target": dict(cmd.target or {}),
        "payload": cmd.payload(),
        "status": status,
        "reasons": list(reasons),
        "lines": describe_change(cmd, doc=doc, policy=policy),
        "affected": affected_artifacts(cmd, doc=doc),
        "estimate": estimate_command(cmd,
                                     duration_seconds=float((doc or {}).get("duration_seconds")
                                                            or 0.0),
                                     base_cost_usd=_base_cost()),
        "reversible": reversible,
        "approval_required": not auto_ok,
        "auto_apply": auto_ok,
        "risk": getattr(cmd, "risk", "medium"),
    }


def _totals(entries: list[dict]) -> dict:
    seconds = round(sum(float(e["estimate"]["rerender_seconds"]) for e in entries), 1)
    cost = round(sum(float(e["estimate"]["cost_usd"]) for e in entries), 4)
    return {
        "rerender_seconds": seconds,
        "cost_usd": cost,
        "commands": len(entries),
        "rejected": sum(1 for e in entries if e["status"] == "rejected"),
        "approval_required": any(e["approval_required"] for e in entries),
        "reversible": all(e["reversible"] for e in entries),
    }


def _lessons(session, workspace, entries: list[dict]) -> dict | None:
    """Work 06 lessons as ADVICE only — hard constraints still win upstream."""
    if not any(e["status"] == "ok" for e in entries):
        return None
    try:
        from app.engine.performance.learning import apply_lessons

        scope = {"platform": next((e["target"].get("platform") or ""
                                   for e in entries if e["target"].get("platform")), "")}
        artifact = apply_lessons(session, workspace.id, scope,
                                 {"changes": [e["lines"] for e in entries]},
                                 "strategy")
        return {"recommendations": artifact.get("lesson_recommendations"),
                "applied": artifact.get("applied_lessons", [])}
    except Exception as exc:  # noqa: BLE001 - lessons are advisory
        logger.debug(f"[creative] lessons unavailable: {type(exc).__name__}")
        return None


def _save_record(session, workspace, *, status: str, timeline_id: str, actor: str,
                 user_id: str | None, text_input: str, commands: list,
                 change_set: dict, parent_version: int | None = None,
                 result: dict | None = None):
    from app.models import CreativeCommandRow

    record = CreativeCommandRow(
        workspace_id=workspace.id,
        timeline_id=str(timeline_id or ""),
        actor=str(actor or "user")[:60],
        source_user_id=user_id,
        text_input=str(text_input or "")[:4000],
        commands_json=commands_to_dicts(commands),
        change_set_json=dict(change_set or {}),
        status=status,
        parent_version=parent_version,
        result_json=dict(result or {}),
    )
    session.add(record)
    session.flush()
    return record


def _mark_stale(preview_row, *, reason: str) -> None:
    if preview_row is None:
        return
    preview_row.status = "stale"
    preview_row.result_json = {**(preview_row.result_json or {}), "stale_reason": reason}


# ---------------------------------------------------------------------------
# op planning (canonical timeline_ops only — no ad-hoc document writes)
# ---------------------------------------------------------------------------


def _clips(doc: dict | None, kind: str) -> list[dict]:
    for tr in (doc or {}).get("tracks", []):
        if tr.get("kind") == kind:
            return sorted(list(tr.get("clips", [])), key=lambda c: float(c.get("start", 0.0)))
    return []


def _rebuild(track: str, clips: list[dict], patch: dict) -> list[dict]:
    """Delete + re-add clips with a patched source payload (canonical ops)."""
    ops: list[dict] = []
    for clip in clips:
        ops.append({"type": "delete_item", "track": track, "clip_id": clip["id"]})
    for clip in clips:
        new_clip = dict(clip)
        source = dict(clip.get("source") or {})
        source.update(patch)
        new_clip["source"] = source
        ops.append({"type": "add_item", "track": track, "clip": new_clip})
    return ops


def _resolve_scenes(session, workspace_id: str, timeline_id: str,
                    payload: dict) -> list[Any]:
    from sqlalchemy import select

    from app.models.assets import Scene

    ids = [str(s) for s in (payload.get("scene_ids") or [])]
    indices = [int(i) for i in (payload.get("scene_indices") or [])]
    scenes = []
    if ids:
        scenes = list(session.scalars(select(Scene).where(
            Scene.workspace_id == workspace_id,
            Scene.timeline_id == timeline_id,
            Scene.id.in_(ids))).all())
    if not scenes and indices:
        rows = list(session.scalars(select(Scene).where(
            Scene.workspace_id == workspace_id,
            Scene.timeline_id == timeline_id).order_by(Scene.index)).all())
        scenes = [r for r in rows if (r.index + 1) in indices]
    return scenes


def _plan(session, workspace, cmd, *, doc: dict, policy: dict) -> tuple[list, dict, dict | None]:
    """Return (operations, doc patches, directive) for one command."""
    p = cmd.payload()
    if cmd.type == "ReplaceAsset":
        track = str(p.get("track") or "video")
        wanted = [str(c) for c in (p.get("clip_ids") or [])]
        clips = [c for c in _clips(doc, track) if not wanted or c.get("id") in wanted]
        if not clips:
            raise CommandError(f"no clips to replace on track '{track}'")
        return (_rebuild(track, clips, {"asset_id": str(p.get("asset_id"))}), {},
                {"op": "replace_asset", "asset_id": str(p.get("asset_id")),
                 "clips": [c["id"] for c in clips]})

    if cmd.type == "RegenerateScene":
        scenes = _resolve_scenes(session, workspace.id, cmd.timeline_id, p)
        return [], {}, {"op": "regenerate_scene",
                        "scene_ids": [str(s.id) for s in scenes],
                        "scene_indices": [int(i) for i in (p.get("scene_indices") or [])],
                        "instruction": str(p.get("instruction") or "")}

    if cmd.type in ("RewriteHook", "RewriteSegment"):
        text = str(p.get("text") or "").strip()
        if not text:
            raise CommandError(
                f"{cmd.type} needs replacement text to apply "
                "(draft it in the editor component first)")
        if cmd.type == "RewriteHook":
            kind, clip = "caption", None
            for candidate_kind in ("caption", "text"):
                found = _clips(doc, candidate_kind)
                if found:
                    kind, clip = candidate_kind, found[0]
                    break
            if clip is None:
                raise CommandError("timeline has no caption/text clip for the hook")
        else:
            seg = str(p.get("segment_id") or "")
            clip = None
            kind = "caption"
            for candidate_kind in ("caption", "text", "voice"):
                for candidate in _clips(doc, candidate_kind):
                    if not seg or candidate.get("id") == seg:
                        clip, kind = candidate, candidate_kind
                        break
                if clip is not None:
                    break
            if clip is None:
                raise CommandError("target segment not found on this timeline")
        if kind == "caption":
            return [{"type": "update_caption", "track": "caption",
                     "clip_id": clip["id"], "text": text[:500]}], {}, None
        return [{"type": "update_text", "track": "text", "clip_id": clip["id"],
                 "text": {"content": text[:500]}}], {}, None

    if cmd.type == "ChangeVoice":
        clips = _clips(doc, "voice")
        if not clips:
            raise CommandError("timeline has no voice clips to re-voice")
        return (_rebuild("voice", clips,
                         {"voice_id": str(p.get("voice_id")),
                          "gender": str(p.get("gender") or "")}), {},
                {"op": "change_voice", "voice_id": str(p.get("voice_id"))})

    if cmd.type == "ChangeCaptionPreset":
        clips = _clips(doc, "caption")
        if not clips:
            raise CommandError("timeline has no caption clips")
        preset = str(p.get("preset"))
        return ([{"type": "update_caption", "track": "caption", "clip_id": c["id"],
                  "style": preset} for c in clips],
                {}, {"op": "caption_preset", "preset": preset})

    if cmd.type == "ChangeMusic":
        clips = _clips(doc, "music")
        if not clips:
            raise CommandError("timeline has no music clip to replace")
        return (_rebuild("music", clips, {"music_id": str(p.get("music_id"))}), {},
                {"op": "change_music", "music_id": str(p.get("music_id"))})

    if cmd.type == "ChangeCTA":
        text = str(p.get("text") or "").strip()
        if not text:
            raise CommandError("ChangeCTA needs replacement text to apply")
        target_clip = None
        kind = "text"
        for clip in _clips(doc, "text"):
            content = str((clip.get("text") or {}).get("content") or clip.get("name") or "")
            if re.search(r"\bcta\b|subscribe|follow|link in bio|comment below",
                         content, re.I):
                target_clip = clip
                break
        if target_clip is None and _clips(doc, "text"):
            kind, target_clip = "text", _clips(doc, "text")[-1]
        elif target_clip is None and _clips(doc, "caption"):
            kind, target_clip = "caption", _clips(doc, "caption")[-1]
        if target_clip is not None:
            if kind == "text":
                return [{"type": "update_text", "track": "text",
                         "clip_id": target_clip["id"],
                         "text": {"content": text[:500]}}], {}, None
            return [{"type": "update_caption", "track": "caption",
                     "clip_id": target_clip["id"], "text": text[:500]}], {}, None
        start = float(doc.get("duration_seconds") or 0.0)
        if start <= 0:
            raise CommandError("timeline has no runtime to place a CTA")
        clip_id = "cta_creative"
        existing = {c.get("id") for c in _clips(doc, "text")}
        while clip_id in existing:
            clip_id += "_x"
        return [{"type": "add_item", "track": "text", "clip": {
            "id": clip_id, "name": "CTA", "start": start, "duration": 3.0,
            "source": {"kind": "cta"}, "effects": [], "text": {"content": text[:500]},
        }}], {}, {"op": "cta", "text": text[:500]}

    if cmd.type == "ChangeStyle":
        patch = {k: v for k, v in dict(p.get("patch") or {}).items()
                 if k in ("content", "font", "size", "weight", "color", "align", "opacity")}
        style_name = str(p.get("style_name") or "")
        ops: list[dict] = []
        for clip in _clips(doc, "text"):
            if patch:
                ops.append({"type": "update_text", "track": "text",
                            "clip_id": clip["id"], "text": patch})
        if style_name:
            for clip in _clips(doc, "caption"):
                ops.append({"type": "update_caption", "track": "caption",
                            "clip_id": clip["id"], "style": style_name})
        if not ops:
            raise CommandError("ChangeStyle needs a text patch (text track) "
                               "or a style_name (caption track)")
        return ops, {}, {"op": "style", "patch": patch, "style_name": style_name}

    if cmd.type == "ChangeDuration":
        target = float(p.get("seconds") or 0.0)
        ops = []
        for track in {tr.get("kind") for tr in doc.get("tracks", [])}:
            for clip in _clips(doc, str(track)):
                start = float(clip.get("start", 0.0))
                end = start + float(clip.get("duration", 0.0))
                if start >= target:
                    ops.append({"type": "delete_item", "track": track,
                                "clip_id": clip["id"]})
                elif end > target:
                    ops.append({"type": "trim_item", "track": track,
                                "clip_id": clip["id"], "edge": "end", "end": target})
        if not ops:
            raise CommandError(
                f"no clip reaches {target:g}s — nothing to trim "
                "(shortening is the only supported duration edit)")
        return ops, {}, {"op": "duration", "seconds": target}

    if cmd.type == "ChangeAspectRatio":
        return [], {"aspect_ratio": str(p.get("aspect"))}, None

    if cmd.type == "CreateVariant":
        return [], {}, {"op": "create_variant", "label": str(p.get("label") or ""),
                        "platform": str(p.get("platform") or "")}

    if cmd.type == "ApplyBrandPreset":
        preset = str(p.get("preset"))
        preset_cfg = dict((policy.get("brand_preset_map") or {}).get(preset.lower(), {}))
        caption_style = str(preset_cfg.get("caption_preset") or preset)
        ops = [{"type": "update_caption", "track": "caption", "clip_id": c["id"],
                "style": caption_style} for c in _clips(doc, "caption")]
        patches: dict = {}
        aspect = str(preset_cfg.get("aspect_ratio") or "")
        if aspect:
            from app.engine.timeline import SUPPORTED_ASPECTS

            if aspect in SUPPORTED_ASPECTS:
                patches["aspect_ratio"] = aspect
        if not ops and not patches:
            raise CommandError(
                f"brand preset '{preset}' applies nothing this timeline has "
                "(captions/aspect)")
        return ops, patches, {"op": "brand_preset", "preset": preset,
                              "caption_preset": caption_style,
                              "aspect_ratio": aspect}

    if cmd.type == "ReframeScene":
        scenes = _resolve_scenes(session, workspace.id, cmd.timeline_id, p)
        if not scenes:
            raise CommandError("target scene not found on this timeline")
        focus = str(p.get("focus") or "center")
        deltas = {"left": {"x": -0.1}, "right": {"x": 0.1}, "top": {"y": -0.1},
                  "bottom": {"y": 0.1}, "face": {"scale": 1.12}, "center": {}}
        patch = dict(deltas.get(focus, {}))
        patch["scale"] = max(float(patch.get("scale", 1.0)), 1.08) if focus != "center" else 1.0
        ops = []
        touched = set()
        for scene in scenes:
            for clip in _clips(doc, "video"):
                start = float(clip.get("start", 0.0))
                end = start + float(clip.get("duration", 0.0))
                if start < float(scene.end_seconds) and end > float(scene.start_seconds):
                    if clip["id"] in touched:
                        continue
                    touched.add(clip["id"])
                    ops.append({"type": "update_transform", "track": "video",
                                "clip_id": clip["id"], "transform": dict(patch)})
        if not ops:
            raise CommandError("no video clips overlap the targeted scene")
        return ops, {}, {"op": "reframe", "focus": focus,
                         "clips": sorted(touched)}

    raise CommandError(f"command '{cmd.type}' has no apply implementation")


def _execute(session, workspace, *, row, doc: dict, cmds: list, policy: dict,
             actor: str, user_id: str | None, approve: bool, text_input: str,
             expected_version: int, preview_row) -> dict:
    from app.engine.timeline import save_version, validate_timeline
    from app.engine.timeline_ops import TimelineOpError, apply_operations
    from app.models import ContentTimeline

    ops: list[dict] = []
    patches: dict = {}
    directives: list[dict] = []
    for cmd in cmds:
        try:
            cmd_ops, cmd_patches, directive = _plan(
                session, workspace, cmd, doc=doc, policy=policy)
        except CommandError:
            raise
        except Exception as exc:  # noqa: BLE001 - planner bugs surface as 422
            raise CommandError(f"{cmd.type}: {exc}") from exc
        ops.extend(cmd_ops)
        patches.update(cmd_patches)
        if directive:
            directives.append({"type": cmd.type, **directive})

    new_doc = copy.deepcopy(doc)
    if ops:
        try:
            new_doc = apply_operations(doc, ops)
        except TimelineOpError as exc:
            raise CommandError(f"operations rejected: {exc}") from exc
    new_doc.update(patches)
    if ops or patches:
        try:
            validate_timeline(new_doc)
        except Exception as exc:  # noqa: BLE001 - mapped to 422
            raise CommandError(f"resulting timeline is invalid: {exc}") from exc

    wants_variant = any(isinstance(c, CreateVariant) for c in cmds)
    version_bumped = bool(ops or patches or wants_variant)
    previous_id = str(row.id)
    previous_version = int(row.version or 1)
    tip_id = previous_id
    version = previous_version

    if version_bumped:
        new_id = save_version(session, previous_id,
                              label="creative: " + ", ".join(
                                  sorted({c.type for c in cmds}))[:80])
        child = session.get(ContentTimeline, new_id)
        child.tracks_json = new_doc
        child.duration_seconds = float(new_doc.get("duration_seconds") or 0.0)
        if wants_variant:
            label = next((str(c.payload().get("label") or "")
                          for c in cmds if isinstance(c, CreateVariant)), "")
            if label:
                child.name = f"{row.name} — {label}"[:200]
        tip_id = str(child.id)
        version = int(child.version or 1)
        new_doc = _doc_of(child)

    try:
        from app.engine import scene_sync as sync_mod

        sync_mod.resync_scene_ranges(session=session, workspace_id=workspace.id,
                                     timeline_id=previous_id, tracks_doc=new_doc)
    except Exception as exc:  # noqa: BLE001 - scene sync must not block an edit
        logger.debug(f"[creative] scene resync skipped: {type(exc).__name__}")

    applied_hash = _manifest_hash(new_doc)
    entries = [_entry(i, c, [], doc=new_doc, policy=policy,
                      auto_enabled=bool(policy.get("creative_auto_apply")))
               for i, c in enumerate(cmds)]
    record = _save_record(
        session, workspace, status="applied", timeline_id=tip_id, actor=actor,
        user_id=user_id, text_input=text_input, commands=cmds,
        change_set={"changes": entries, "totals": _totals(entries),
                    "manifest_hash": applied_hash,
                    "policy_source": policy.get("source"),
                    "preview_id": str(getattr(preview_row, "id", "") or "")},
        parent_version=previous_version,
        result={"previous_timeline_id": previous_id,
                "previous_version": previous_version,
                "new_timeline_id": tip_id,
                "version_bumped": version_bumped,
                "applied_manifest_hash": applied_hash,
                "operations": len(ops),
                "directives": directives,
                "approve": bool(approve),
                "estimated": _totals(entries)})
    if preview_row is not None:
        preview_row.status = "applied"
    session.flush()
    return {"status": "applied", "record_id": str(record.id),
            "timeline_id": tip_id, "version": version,
            "previous_timeline_id": previous_id, "previous_version": previous_version,
            "version_bumped": version_bumped, "applied": len(cmds),
            "operations": len(ops), "directives": directives,
            "changes": entries, "totals": _totals(entries),
            "manifest_hash": applied_hash}


__all__ = [
    "ApprovalRequired",
    "CreativeDirector",
    "CreativeError",
    "MATCHERS",
    "PreviewMismatch",
    "RejectedCommands",
    "StalePreview",
    "resolve_policy",
    "voice_gender",
]
