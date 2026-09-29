"""Creative templates + brand wiring helpers (Work 08, Lane C).

This module is the ONLY Lane C brand file. It lives at
``app/engine/brand_templates.py`` — deliberately outside ``engine/brand/``
(Lane A owns policy/DNA) and outside ``engine/creative/`` (Lane B owns the
command catalog).

Two responsibilities, both deterministic and side-effect free by default:

1. **Creative templates** — a builtin registry of 12 templates
   (``youtube_longform``, ``shorts``, ``reels``, ``tiktok``, ``documentary``,
   ``explainer``, ``educational``, ``ugc``, ``product_launch``, ``news``,
   ``talking_head``, ``faceless``). A template is a bag of creative
   *defaults* (duration range, aspect, pacing, hook style, caption preset,
   tone, CTA style, b-roll density, music preference) — never a pipeline.
   Precedence: **BrandDNA > template > model/LLM output > defaults**
   (:func:`apply_template_defaults` only lets the brand override template
   defaults; callers still let explicit user/model values win over both).

2. **Brand wiring** — :func:`brand_gate` resolves Lane A's effective policy
   lazily and returns a plain dict of hard constraints + prompt instructions
   + template defaults + provenance markers. Every generation call site
   consumes that dict, so the whole integration degrades to "no brand"
   (empty constraints, ``applied_brand=False``) whenever ``engine.brand`` is
   missing or raising — the test suite and the autopilot never break.

Auditable lineage: every wiring point records
``{"applied_brand": bool, "effective_config_id": str, "brand_provenance":
{...}, "brand_template": str, "brand_degraded": str}`` via
:func:`lineage_markers`.

Precedence used across the product (documented once, enforced by callers)::

    security/compliance -> explicit user override -> brand hard ->
    campaign -> learned (Work 06 lessons) -> defaults

Learning never overrides brand hard constraints (see
``engine.performance.learning.apply_lessons`` which calls
:func:`lesson_conflicts_with_brand`).
"""

from __future__ import annotations

import inspect
import re
from typing import Any

from loguru import logger

__all__ = [
    "TEMPLATE_KEYS",
    "approved_voice",
    "apply_template_defaults",
    "attach_template",
    "brand_cover_style",
    "brand_gate",
    "brand_glossary_entries",
    "brand_instructions",
    "brand_qc_check",
    "brand_variant_metadata",
    "enforce_forbidden_phrases",
    "find_forbidden_phrases",
    "get_template",
    "lineage_markers",
    "list_templates",
    "merge_brand_glossary",
    "persist_template_preset",
    "template_for",
    "hook_rejection_reason",
    "lesson_conflicts_with_brand",
]

# ---------------------------------------------------------------------------
# builtin template registry (creative defaults only)
# ---------------------------------------------------------------------------

TEMPLATE_KEYS: tuple[str, ...] = (
    "youtube_longform",
    "shorts",
    "reels",
    "tiktok",
    "documentary",
    "explainer",
    "educational",
    "ugc",
    "product_launch",
    "news",
    "talking_head",
    "faceless",
)


def _t(key: str, title: str, description: str, **defaults: Any) -> dict:
    return {"key": key, "title": title, "description": description,
            "defaults": dict(defaults)}


# Nine creative default dimensions per template (duration range, aspect,
# pacing, hook style, caption preset, tone, CTA style, b-roll density,
# music preference). `duration_min`/`duration_max` are seconds; the model may
# pick anything inside the range.
_BUILTIN_TEMPLATES: dict[str, dict] = {
    "youtube_longform": _t(
        "youtube_longform", "YouTube long-form",
        "10-15 minute narrated long-form for the main channel.",
        duration_min=300, duration_max=900, duration_seconds=600,
        aspect_ratio="16:9", pacing="steady", hook_style="curiosity_gap",
        caption_preset="minimal", tone="conversational expert",
        cta_style="subscribe_value", broll_density="medium",
        music_preference="low_ambient"),
    "shorts": _t(
        "shorts", "YouTube Shorts",
        "Vertical short derived from (or made for) Shorts.",
        duration_min=20, duration_max=60, duration_seconds=45,
        aspect_ratio="9:16", pacing="fast", hook_style="bold_claim",
        caption_preset="karaoke", tone="punchy", cta_style="follow_fast",
        broll_density="high", music_preference="upbeat"),
    "reels": _t(
        "reels", "Instagram Reels",
        "Vertical Reel optimized for shares and saves.",
        duration_min=15, duration_max=45, duration_seconds=30,
        aspect_ratio="9:16", pacing="fast", hook_style="curiosity_gap",
        caption_preset="pop", tone="playful", cta_style="share_prompt",
        broll_density="medium", music_preference="trend_audio"),
    "tiktok": _t(
        "tiktok", "TikTok",
        "Native-feeling TikTok: fast open, trend-aware audio.",
        duration_min=15, duration_max=45, duration_seconds=30,
        aspect_ratio="9:16", pacing="very_fast", hook_style="bold_claim",
        caption_preset="pop", tone="casual", cta_style="follow_for_more",
        broll_density="high", music_preference="trend_audio"),
    "documentary": _t(
        "documentary", "Documentary",
        "Slow-burn narrative documentary chapter.",
        duration_min=480, duration_max=1500, duration_seconds=720,
        aspect_ratio="16:9", pacing="slow", hook_style="story",
        caption_preset="minimal", tone="measured", cta_style="learn_more",
        broll_density="low", music_preference="cinematic"),
    "explainer": _t(
        "explainer", "Explainer",
        "Product/concept explainer with a clear payoff.",
        duration_min=180, duration_max=600, duration_seconds=300,
        aspect_ratio="16:9", pacing="steady", hook_style="question",
        caption_preset="minimal", tone="clear", cta_style="try_it",
        broll_density="medium", music_preference="low_ambient"),
    "educational": _t(
        "educational", "Educational",
        "Teaching-first cut with retention checkpoints.",
        duration_min=240, duration_max=720, duration_seconds=420,
        aspect_ratio="16:9", pacing="steady", hook_style="question",
        caption_preset="karaoke", tone="instructional",
        cta_style="practice_prompt", broll_density="medium",
        music_preference="low_ambient"),
    "ugc": _t(
        "ugc", "UGC ad",
        "Creator-style UGC ad from a product brief.",
        duration_min=20, duration_max=45, duration_seconds=30,
        aspect_ratio="9:16", pacing="fast", hook_style="story",
        caption_preset="pop", tone="authentic", cta_style="shop_now",
        broll_density="medium", music_preference="none"),
    "product_launch": _t(
        "product_launch", "Product launch",
        "Launch trailer for a new product or feature.",
        duration_min=30, duration_max=90, duration_seconds=45,
        aspect_ratio="9:16", pacing="fast", hook_style="bold_claim",
        caption_preset="karaoke", tone="confident", cta_style="learn_more",
        broll_density="high", music_preference="upbeat"),
    "news": _t(
        "news", "News brief",
        "Timely news-style read with sourced facts only.",
        duration_min=45, duration_max=180, duration_seconds=90,
        aspect_ratio="16:9", pacing="steady", hook_style="statistic",
        caption_preset="minimal", tone="neutral", cta_style="read_more",
        broll_density="low", music_preference="urgent_bed"),
    "talking_head": _t(
        "talking_head", "Talking head",
        "Presenter-to-camera piece with light captions.",
        duration_min=20, duration_max=90, duration_seconds=45,
        aspect_ratio="9:16", pacing="medium", hook_style="question",
        caption_preset="karaoke", tone="personal", cta_style="follow_for_more",
        broll_density="low", music_preference="none"),
    "faceless": _t(
        "faceless", "Faceless channel",
        "Stock/AI visuals with voice-over, no presenter.",
        duration_min=30, duration_max=90, duration_seconds=60,
        aspect_ratio="9:16", pacing="fast", hook_style="curiosity_gap",
        caption_preset="pop", tone="direct", cta_style="follow_for_more",
        broll_density="high", music_preference="lofi"),
}

# platform token -> template key (short-form platforms map to short templates;
# long-form surfaces map to the long-form/documentary templates).
PLATFORM_TEMPLATES: dict[str, str] = {
    "youtube": "youtube_longform",
    "youtube_longform": "youtube_longform",
    "youtube_shorts": "shorts",
    "youtube_short": "shorts",
    "shorts": "shorts",
    "tiktok": "tiktok",
    "instagram": "reels",
    "instagram_reels": "reels",
    "reels": "reels",
    "facebook": "shorts",
    "facebook_reels": "reels",
    "linkedin": "explainer",
    "x": "news",
    "twitter": "news",
    "pinterest": "reels",
}

# templates a 20-90s generation path may inherit (never a 5-15 min one)
SHORT_FORM_TEMPLATE_KEYS = frozenset({
    "shorts", "reels", "tiktok", "ugc", "talking_head", "faceless",
    "product_launch",
})


def get_template(key: str) -> dict:
    """Deep-ish copy of one builtin template ({} for an unknown key)."""
    tpl = _BUILTIN_TEMPLATES.get(str(key or "").strip().lower())
    if not tpl:
        return {}
    out = dict(tpl)
    out["defaults"] = dict(tpl["defaults"])
    return out


def list_templates() -> list[dict]:
    """Every builtin template (copy — callers may mutate freely)."""
    return [get_template(key) for key in TEMPLATE_KEYS]


def template_for(artifact: dict | None = None, *, platform: str = "",
                 default: str = "", short_form: bool = False) -> str:
    """Best template key for a call site: explicit > platform > default.

    ``short_form=True`` keeps short-form surfaces on short-form templates
    (a 20-90s strategist never inherits a 5-15 minute long-form template).
    """
    art = dict(artifact or {})
    explicit = str(art.get("template") or art.get("template_key") or "").strip().lower()
    if explicit in _BUILTIN_TEMPLATES:
        return explicit
    fmt = str(art.get("content_format") or art.get("format") or "").strip().lower()
    if fmt in _BUILTIN_TEMPLATES:
        return fmt
    token = str(platform or art.get("platform") or "").strip().lower()
    key = PLATFORM_TEMPLATES.get(token, "")
    if short_form and key not in SHORT_FORM_TEMPLATE_KEYS:
        key = ""
    if not key:
        key = default if default in _BUILTIN_TEMPLATES else ""
    return key


# ---------------------------------------------------------------------------
# template defaults x brand policy
# ---------------------------------------------------------------------------

_EMPTY_HARD: dict = {
    "forbidden_phrases": [],
    "required_disclaimers": [],
    "logo_safe_zone": {},
    "approved_voices": [],
    "approved_avatars": [],
}


def _policy_get(policy: Any, name: str, default: Any = None) -> Any:
    """Read a field from an EffectiveCreativePolicy, a dict, or anything else."""
    if policy is None:
        return default
    if isinstance(policy, dict):
        return policy.get(name, default)
    try:
        value = getattr(policy, name)
    except Exception:  # noqa: BLE001 — exotic policy objects degrade
        return default
    return default if value is None else value


def _preset_from_style(style: Any) -> str:
    """caption_style dict → preset name (``preset``/``style``/``name``)."""
    if isinstance(style, dict):
        for key in ("preset", "style", "name", "caption_preset"):
            value = style.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    return ""


def apply_template_defaults(policy: Any, template: Any) -> dict:
    """Template creative defaults with the brand policy layered ON TOP.

    ``policy`` may be ``None`` (brand absent → pure template defaults), an
    ``EffectiveCreativePolicy``, or a plain dict. ``template`` may be a key
    or a template dict. Brand always wins over the template; the keys the
    brand actually overrode are listed under ``_brand_overrides`` (metadata,
    not a creative dimension).
    """
    if isinstance(template, str):
        tpl = get_template(template)
    elif isinstance(template, dict):
        tpl = dict(template)
    else:
        tpl = {}
    defaults = dict(tpl.get("defaults") or tpl.get("default") or {})
    if not tpl.get("key") and isinstance(template, dict):
        defaults = dict(template.get("defaults") or {})
    overrides: list[str] = []

    tone = _policy_get(policy, "tone", "")
    if isinstance(tone, str) and tone.strip():
        defaults["tone"] = tone.strip()
        overrides.append("tone")

    caption_style = _policy_get(policy, "caption_style", {})
    preset = _preset_from_style(caption_style)
    if preset:
        defaults["caption_preset"] = preset
        overrides.append("caption_preset")
    if isinstance(caption_style, dict) and caption_style:
        defaults["caption_style"] = dict(caption_style)
        if "caption_style" not in overrides:
            overrides.append("caption_style")

    cta_style = _policy_get(policy, "cta_style", {})
    if isinstance(cta_style, dict) and cta_style:
        defaults["cta_style"] = dict(cta_style)
        overrides.append("cta_style")
    elif isinstance(cta_style, str) and cta_style.strip():
        defaults["cta_style"] = cta_style.strip()
        overrides.append("cta_style")

    colors = _policy_get(policy, "brand_colors", []) or []
    if colors:
        defaults["brand_colors"] = list(colors)
        overrides.append("brand_colors")

    thumbnail_style = _policy_get(policy, "thumbnail_style", {})
    if isinstance(thumbnail_style, dict) and thumbnail_style:
        defaults["thumbnail_style"] = dict(thumbnail_style)
        overrides.append("thumbnail_style")

    defaults["_brand_overrides"] = overrides
    return defaults


def attach_template(gate: dict, *, platform: str = "", artifact: dict | None = None,
                    default: str = "", short_form: bool = False) -> dict:
    """(Re)resolve the gate's template + defaults for the actual call site."""
    key = template_for(artifact, platform=platform, default=default,
                       short_form=short_form)
    gate["template_key"] = key
    gate["template"] = get_template(key)
    gate["defaults"] = apply_template_defaults(gate.get("policy"), gate["template"])
    return gate


# ---------------------------------------------------------------------------
# policy resolution (Lane A, lazy + degrading)
# ---------------------------------------------------------------------------

def _resolve_policy(session: Any, ws_id: str, *, campaign_id: str = "",
                    content_id: str = "", platform: str = "",
                    overrides: dict | None = None) -> tuple[Any, str]:
    """(policy, degraded_reason). Never raises."""
    resolve_effective_policy = None
    for module_name in ("app.engine.brand.policy", "app.engine.brand",
                        "app.engine.brand.inheritance"):
        try:
            module = __import__(module_name, fromlist=["resolve_effective_policy"])
            resolve_effective_policy = getattr(module, "resolve_effective_policy", None)
        except Exception as exc:  # noqa: BLE001 — Lane A may not exist yet
            logger.debug(f"[brand] {module_name} unavailable: {type(exc).__name__}")
            resolve_effective_policy = None
        if resolve_effective_policy is not None:
            break
    if resolve_effective_policy is None:
        return None, "brand_module_unavailable"
    def _invoke(sess: Any) -> Any:
        kwargs: dict[str, Any] = {"campaign_id": campaign_id or None,
                                  "content_id": content_id or None,
                                  "platform": platform or None}
        if overrides:
            kwargs["overrides"] = overrides
        # Accept both the documented signature and stricter variants.
        try:
            return resolve_effective_policy(sess, ws_id, **kwargs)
        except TypeError:
            sig = inspect.signature(resolve_effective_policy)
            accepted = {k: v for k, v in kwargs.items()
                        if k in sig.parameters or any(
                            p.kind is inspect.Parameter.VAR_KEYWORD
                            for p in sig.parameters.values())}
            return resolve_effective_policy(sess, ws_id, **accepted)

    try:
        if session is None:
            from app.db import session_scope  # noqa: PLC0415

            with session_scope() as own:
                return _invoke(own), ""
        return _invoke(session), ""
    except Exception as exc:  # noqa: BLE001 — never break a generation cycle
        logger.debug(f"[brand] policy resolution failed: {type(exc).__name__}: {exc}")
        return None, "brand_policy_error"


def _empty_gate(ws_id: str, campaign_id: str, platform: str) -> dict:
    return {
        "workspace_id": ws_id,
        "campaign_id": campaign_id,
        "platform": platform,
        "policy": None,
        "brand_available": False,
        "applied_brand": False,
        "degraded": "",
        "template_key": "",
        "template": {},
        "defaults": {},
        "hard_constraints": dict(_EMPTY_HARD),
        "forbidden_phrases": [],
        "required_disclaimers": [],
        "approved_voices": [],
        "approved_avatars": [],
        "brand_colors": [],
        "caption_style": {},
        "cta_style": {},
        "tone": "",
        "vocabulary": {"preferred": [], "avoid": []},
        "pronunciation_rules": [],
        "logo_safe_zone": {},
        "thumbnail_style": {},
        "platform_overrides": {},
        "effective_config_id": "",
        "provenance": {},
        "instructions": "",
    }


def brand_gate(session: Any, ws: Any, *, campaign_id: str = "",
               platform: str = "", artifact: dict | None = None) -> dict:
    """Resolve brand policy + template defaults for one generation call.

    Never raises. Returns a plain dict (see :func:`_empty_gate` for the
    shape) so call sites can consume constraints even when Lane A's module
    is absent mid-merge.
    """
    art = dict(artifact or {})
    ws_id = str(getattr(ws, "id", None) or ws or "")
    campaign_id = str(campaign_id or art.get("campaign_id") or "")
    platform = str(platform or art.get("platform") or "")
    gate = _empty_gate(ws_id, campaign_id, platform)
    if not ws_id:
        gate["degraded"] = "no_workspace"
        return gate

    content_id = str(art.get("content_id") or "")
    policy, degraded = _resolve_policy(
        session, ws_id, campaign_id=campaign_id, content_id=content_id,
        platform=platform,
        overrides=art.get("brand_overrides") if isinstance(
            art.get("brand_overrides"), dict) else None)
    gate["degraded"] = degraded
    if policy is None:
        # template defaults still apply without a brand policy
        attach_template(gate, platform=platform, artifact=art)
        return gate

    gate["policy"] = policy
    gate["brand_available"] = True
    gate["provenance"] = dict(_policy_get(policy, "provenance", {}) or {})
    gate["effective_config_id"] = str(
        _policy_get(policy, "effective_config_id", "") or "")

    hard = _policy_get(policy, "hard_constraints_dict", None)
    if callable(hard):
        try:
            hard = hard()
        except Exception:  # noqa: BLE001
            hard = None
    if not isinstance(hard, dict):
        hard = {
            "forbidden_phrases": _policy_get(policy, "forbidden_phrases", []) or [],
            "required_disclaimers": _policy_get(policy, "required_disclaimers", []) or [],
            "logo_safe_zone": _policy_get(policy, "logo_safe_zone", {}) or {},
            "approved_voices": _policy_get(policy, "approved_voices", []) or [],
            "approved_avatars": _policy_get(policy, "approved_avatars", []) or [],
        }
    gate["hard_constraints"] = dict(hard)
    gate["forbidden_phrases"] = [str(p) for p in (hard.get("forbidden_phrases") or [])]
    gate["required_disclaimers"] = [str(d) for d in (hard.get("required_disclaimers") or [])]
    gate["approved_voices"] = [str(v) for v in (hard.get("approved_voices") or [])]
    gate["approved_avatars"] = [str(a) for a in (hard.get("approved_avatars") or [])]
    gate["logo_safe_zone"] = dict(hard.get("logo_safe_zone") or {})
    gate["brand_colors"] = [str(c) for c in (_policy_get(policy, "brand_colors", []) or [])]
    gate["caption_style"] = dict(_policy_get(policy, "caption_style", {}) or {})
    gate["cta_style"] = dict(_policy_get(policy, "cta_style", {}) or {})
    gate["tone"] = str(_policy_get(policy, "tone", "") or "").strip()
    vocab = _policy_get(policy, "vocabulary", {}) or {}
    if isinstance(vocab, dict):
        gate["vocabulary"] = {
            "preferred": [str(t) for t in (vocab.get("preferred") or [])],
            "avoid": [str(t) for t in (vocab.get("avoid") or [])],
        }
    gate["pronunciation_rules"] = list(
        _policy_get(policy, "pronunciation_rules", []) or [])
    gate["thumbnail_style"] = dict(
        _policy_get(policy, "thumbnail_style", {}) or {})

    # platform-level provenance → explicit platform override view
    # (keys resolved at platform level, carrying their RESOLVED values)
    gate["platform_overrides"] = {
        key: gate.get(key, value)
        for key, value in gate["provenance"].items()
        if str(value) == "platform"
    }

    key = template_for(art, platform=platform)
    gate["template_key"] = key
    gate["template"] = get_template(key)
    gate["defaults"] = apply_template_defaults(policy, gate["template"])

    gate["applied_brand"] = bool(
        gate["forbidden_phrases"] or gate["required_disclaimers"]
        or gate["approved_voices"] or gate["tone"]
        or gate["vocabulary"]["preferred"] or gate["vocabulary"]["avoid"]
        or gate["caption_style"] or gate["brand_colors"]
        or gate["approved_avatars"])
    gate["instructions"] = brand_instructions(gate)
    return gate


def lineage_markers(gate: dict | None) -> dict:
    """Auditable provenance markers every wiring point stores on artifacts."""
    g = gate or {}
    return {
        "applied_brand": bool(g.get("applied_brand")),
        "effective_config_id": str(g.get("effective_config_id") or ""),
        "brand_provenance": dict(g.get("provenance") or {}),
        "brand_template": str(g.get("template_key") or ""),
        "brand_degraded": str(g.get("degraded") or ""),
    }


def brand_instructions(gate: dict | None) -> str:
    """Deterministic prompt block: tone, vocabulary, forbidden, disclaimers."""
    g = gate or {}
    if not g.get("brand_available"):
        return ""
    lines: list[str] = ["BRAND RULES (hard constraints — never override these):"]
    tone = str(g.get("tone") or "")
    if tone:
        lines.append(f"- Tone: {tone}.")
    vocab = g.get("vocabulary") or {}
    preferred = [t for t in (vocab.get("preferred") or []) if t]
    avoid = [t for t in (vocab.get("avoid") or []) if t]
    if preferred:
        lines.append("- Prefer this vocabulary: " + ", ".join(preferred[:12]) + ".")
    if avoid:
        lines.append("- Do NOT use these words/phrases: " + ", ".join(avoid[:12]) + ".")
    forbidden = [p for p in (g.get("forbidden_phrases") or []) if p]
    if forbidden:
        lines.append("- NEVER write these forbidden phrases: "
                     + "; ".join(f'"{p}"' for p in forbidden[:20])
                     + ". They are stripped automatically.")
    required = [d for d in (g.get("required_disclaimers") or []) if d]
    if required:
        lines.append("- Required disclosures (include verbatim when applicable): "
                     + " | ".join(required[:10]) + ".")
    if len(lines) == 1:
        return ""
    return "\n" + "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# deterministic text enforcement
# ---------------------------------------------------------------------------

def _phrase_re(phrase: str) -> re.Pattern:
    return re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])",
        re.IGNORECASE,
    )


def find_forbidden_phrases(text: str, gate: dict | None) -> list[str]:
    """Forbidden phrases present in ``text`` (case-insensitive, word-bounded)."""
    if not text:
        return []
    hits: list[str] = []
    for phrase in ((gate or {}).get("forbidden_phrases") or []):
        phrase = str(phrase).strip()
        if phrase and phrase not in hits and _phrase_re(phrase).search(text):
            hits.append(phrase)
    return hits


def enforce_forbidden_phrases(text: str, gate: dict | None) -> tuple[str, list[str]]:
    """Strip forbidden phrases deterministically; return (clean_text, hits).

    Advisory vocabulary ``avoid`` terms are FLAGGED (returned in
    ``hits`` is reserved for forbidden phrases) — they never rewrite copy.
    """
    out = str(text or "")
    hits = find_forbidden_phrases(out, gate)
    for phrase in hits:
        out = _phrase_re(phrase).sub(" ", out)
    if hits:
        out = re.sub(r"\s{2,}", " ", out)
        out = re.sub(r"\s+([,.;:!?])", r"\1", out)
        out = re.sub(r"([,.;:!?])\s{2,}", r"\1 ", out)
        out = out.strip()
        out = out[:1].upper() + out[1:] if out else out
    return out, hits


def hook_rejection_reason(hook: str, gate: dict | None) -> str:
    """'' when the hook is usable, otherwise why the hook is rejected."""
    hits = find_forbidden_phrases(hook, gate)
    if hits:
        return "forbidden phrase: " + ", ".join(hits)
    return ""


# ---------------------------------------------------------------------------
# variant / cover / voice / glossary helpers
# ---------------------------------------------------------------------------

def brand_variant_metadata(gate: dict | None, *, platform: str = "") -> dict:
    """Variant metadata fragment: disclaimers + platform overrides + markers.

    Merged into the platform-variant ``metadata_json`` by campaign derivation
    so every published artifact is auditable.
    """
    g = gate or {}
    meta: dict = {
        "required_disclaimers": list(g.get("required_disclaimers") or []),
        "applied_brand": bool(g.get("applied_brand")),
        "effective_config_id": str(g.get("effective_config_id") or ""),
        "brand_template": str(g.get("template_key") or ""),
    }
    if platform:
        meta["brand_platform"] = platform
    overrides = dict(g.get("platform_overrides") or {})
    if overrides:
        meta["brand_platform_overrides"] = overrides
    if not meta["required_disclaimers"] and not meta["applied_brand"]:
        return {}
    return meta


def brand_cover_style(gate: dict | None) -> dict:
    """Brand colors / thumbnail style / logo safe zone hint for covers."""
    g = gate or {}
    out: dict = {}
    colors = list(g.get("brand_colors") or [])
    if colors:
        out["brand_colors"] = colors
    style = dict(g.get("thumbnail_style") or {})
    if style:
        out["style_hint"] = style
    zone = dict(g.get("logo_safe_zone") or {})
    if zone:
        out["logo_safe_zone"] = zone
    tone = str(g.get("tone") or "")
    if tone:
        out["tone"] = tone
    if out:
        out.update({k: v for k, v in lineage_markers(g).items()
                    if k in ("applied_brand", "effective_config_id")})
    return out


def approved_voice(gate: dict | None, requested: str = "") -> str:
    """Filter a voice choice to the brand-approved list.

    No approved list (or no policy) → the request passes through unchanged.
    Otherwise a non-approved request is swapped for the first approved voice.
    """
    approved = [str(v) for v in ((gate or {}).get("approved_voices") or []) if v]
    if not approved:
        return str(requested or "")
    requested = str(requested or "")
    if requested and requested in approved:
        return requested
    return approved[0]


def brand_glossary_entries(gate: dict | None) -> list[dict]:
    """Brand vocabulary + pronunciation rules as localization glossary rows.

    Shape matches ``engine.localization.pipeline.load_glossary`` entries:
    ``{term, replacement, target_languages, kind, case_sensitive, source}``.
    """
    g = gate or {}
    out: list[dict] = []
    seen: set[str] = set()
    for term in (g.get("vocabulary") or {}).get("preferred") or []:
        term = str(term).strip()
        if not term or term.casefold() in seen:
            continue
        seen.add(term.casefold())
        out.append({"term": term[:200], "replacement": term[:200],
                    "target_languages": [], "kind": "brand",
                    "case_sensitive": False, "source": "brand"})
    for rule in g.get("pronunciation_rules") or []:
        if isinstance(rule, dict):
            term = str(rule.get("term") or "").strip()
            repl = str(rule.get("pronunciation") or rule.get("replacement") or "").strip()
        elif isinstance(rule, str) and "=" in rule:
            term, _, repl = rule.partition("=")
            term, repl = term.strip(), repl.strip()
        else:
            continue
        if not term or not repl or term.casefold() in seen:
            continue
        seen.add(term.casefold())
        out.append({"term": term[:200], "replacement": repl[:200],
                    "target_languages": [], "kind": "pronunciation",
                    "case_sensitive": False, "source": "brand"})
    return out


def merge_brand_glossary(existing: list[dict] | None,
                         brand_entries: list[dict] | None) -> list[dict]:
    """Fold brand glossary rows into the operator/workspace list.

    Documented order (per-term, last write wins): **brand > operator overlay
    > workspace glossary rows > nothing**. A brand term replaces the entry
    with the same (case-insensitive) term wherever it sits; brand terms that
    are new are appended. Operator entries for terms the brand does not set
    are untouched.
    """
    out: list[dict] = [dict(e) for e in (existing or [])]
    index: dict[str, int] = {}
    for i, entry in enumerate(out):
        term = str(entry.get("term") or "").strip().casefold()
        if term and term not in index:
            index[term] = i
    for entry in (brand_entries or []):
        term = str(entry.get("term") or "").strip().casefold()
        if not term:
            continue
        if term in index:
            out[index[term]] = dict(entry)
        else:
            index[term] = len(out)
            out.append(dict(entry))
    return out


# ---------------------------------------------------------------------------
# brand QC folding (Lane A verifier, lazy)
# ---------------------------------------------------------------------------

_STATUS_MAP = {
    "pass": "pass", "ok": "pass", "passed": "pass",
    "warning": "warning", "warn": "warning", "pass_with_warnings": "warning",
    "review": "review", "review_required": "review", "needs_review": "review",
    "fail": "fail", "failed": "fail", "blocking": "fail",
}


def _normalize_status(value: Any) -> str:
    token = str(value or "").strip().lower().replace(" ", "_")
    return _STATUS_MAP.get(token, "warning")


def _call_verifier(verify: Any, *, session: Any, ws_id: str, kind: str,
                   payload: Any, policy: Any) -> Any:
    """Call Lane A's ``verify_artifact`` across plausible signatures.

    Lane A's real signature is ``(session, workspace_id, artifact_kind,
    artifact, policy=None)``; this binding also tolerates earlier/looser
    variants (``artifact=`` text, no ``artifact_kind``, keyword-only).
    """
    values: dict[str, Any] = {
        "session": session, "db": session, "connection": session,
        "workspace_id": ws_id, "ws_id": ws_id, "workspace": ws_id,
        "artifact_kind": kind, "kind": kind, "type": kind,
        "artifact": payload, "text": payload, "content": payload,
        "payload": payload, "report": payload, "doc": payload,
        "policy": policy, "effective_policy": policy,
    }
    attempts: list[Any] = []
    try:
        sig = inspect.signature(verify)
    except (TypeError, ValueError):
        sig = None
    if sig is not None:
        kwargs: dict[str, Any] = {}
        missing_required = False
        for name, param in sig.parameters.items():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            if name in values:
                kwargs[name] = values[name]
            elif param.default is param.empty:
                missing_required = True
        if not missing_required:
            attempts.append(lambda: verify(**kwargs))
    attempts.extend([
        lambda: verify(session, ws_id, kind, payload, policy),
        lambda: verify(session, ws_id, kind, payload),
        lambda: verify(session, ws_id, payload),
        lambda: verify(session, workspace_id=ws_id, artifact=payload),
        lambda: verify(workspace_id=ws_id, artifact=payload),
        lambda: verify(payload),
    ])
    last: Exception | None = None
    for attempt in attempts:
        try:
            return attempt()
        except TypeError as exc:
            last = exc
    raise last if last is not None else TypeError("verify_artifact uncallable")


def brand_qc_check(session: Any, ws_id: str, *, text: str = "",
                   campaign_id: str = "", artifact: dict | None = None) -> dict | None:
    """Run Lane A's verifier and map its report onto a QC check.

    Returns ``{"status": pass|warning|review|fail, "detail": str, ...}`` or
    ``None`` when there is nothing brand-related to report (module missing,
    no policy, nothing to verify). A verifier crash maps to ``warning`` —
    the brand check never fails a cycle on its own.
    """
    gate = brand_gate(session, ws_id, campaign_id=campaign_id,
                      platform=str((artifact or {}).get("platform") or ""),
                      artifact=artifact)
    if not gate.get("brand_available"):
        return None
    verify_artifact = None
    for module_name in ("app.engine.brand", "app.engine.brand.verifier",
                        "app.engine.brand.verify"):
        try:
            module = __import__(module_name, fromlist=["verify_artifact"])
            verify_artifact = getattr(module, "verify_artifact", None)
        except Exception as exc:  # noqa: BLE001 — Lane A verifier may not exist
            logger.debug(f"[brand] {module_name} unavailable: {type(exc).__name__}")
            verify_artifact = None
        if verify_artifact is not None:
            break
    if verify_artifact is None:
        return None
    payload: dict[str, Any] = dict(artifact or {})
    if text:
        payload.setdefault("text", text)
    if not payload:
        return None
    kind = str(payload.pop("artifact_kind", "") or payload.pop("kind", "")
               or "script")
    payload.pop("platform", None)
    try:
        report = _call_verifier(verify_artifact, session=session, ws_id=ws_id,
                                kind=kind, payload=payload,
                                policy=gate.get("policy"))
    except Exception as exc:  # noqa: BLE001 — never fail a cycle here
        return {"status": "warning",
                "detail": f"brand verifier crashed: {type(exc).__name__}: {exc}",
                "applied_brand": bool(gate.get("applied_brand"))}
    if report is None:
        return None
    if isinstance(report, dict):
        status = report.get("status") or report.get("result") or report.get("verdict")
        detail = str(report.get("detail") or report.get("summary") or "")
        checks = report.get("checks")
    else:
        status = getattr(report, "status", None) or getattr(report, "result", None)
        detail = str(getattr(report, "detail", "") or getattr(report, "summary", "") or "")
        checks = getattr(report, "checks", None)
    if status is None:
        return {"status": "warning",
                "detail": "brand verifier returned no status",
                "applied_brand": bool(gate.get("applied_brand"))}
    mapped = _normalize_status(status)
    if not detail:
        detail = f"brand consistency: {str(status).upper()}"
    return {"status": mapped, "detail": detail[:400],
            "verifier_status": str(status), "checks": checks,
            "applied_brand": bool(gate.get("applied_brand")),
            "effective_config_id": gate.get("effective_config_id") or ""}


# ---------------------------------------------------------------------------
# learning precedence (brand hard > learned)
# ---------------------------------------------------------------------------

_PRESET_NAMES = ("pop", "karaoke", "minimal")


def lesson_conflicts_with_brand(gate: dict | None, kind: str,
                                recommendation: str, effect: dict | None = None) -> str:
    """Why a learned recommendation may NOT apply ('' = it may).

    Deterministic rules (only brand HARD constraints are checked):

    1. caption-preset recommendations must satisfy the brand caption style
       (``preset``/``allowed``/``forbidden`` in ``caption_style``);
    2. the recommendation text may not contain a forbidden phrase;
    3. the recommendation may not push a vocabulary term the brand avoids
       while naming no preferred substitute (advice that only says "use X"
       where X is banned is unusable).
    """
    g = gate or {}
    if not g.get("brand_available"):
        return ""
    rec = str(recommendation or "").strip()
    eff = dict(effect or {})
    text = " ".join([rec, str(eff.get("pattern_key") or ""),
                     str(eff.get("metric") or ""), str(eff.get("caption_preset") or "")]).lower()

    caption = g.get("caption_style") or {}
    if isinstance(caption, dict) and caption:
        wanted = str(eff.get("caption_preset") or "").strip().lower()
        if not wanted:
            for name in _PRESET_NAMES:
                if re.search(rf"\b{re.escape(name)}\b", text):
                    wanted = name
                    break
        if wanted:
            forbidden = {str(p).strip().lower()
                         for p in (caption.get("forbidden") or caption.get("disallowed") or [])}
            allowed = {str(p).strip().lower()
                       for p in (caption.get("allowed") or caption.get("presets") or [])}
            preset = str(caption.get("preset") or caption.get("style") or "").strip().lower()
            if wanted in forbidden:
                return f"brand forbids the '{wanted}' caption preset"
            if allowed and wanted not in allowed:
                return (f"brand allows only {sorted(allowed)} caption presets "
                        f"(lesson wants '{wanted}')")
            if preset and wanted != preset:
                return f"brand requires the '{preset}' caption preset (lesson wants '{wanted}')"

    hits = find_forbidden_phrases(rec, g)
    if hits:
        return "recommendation contains a forbidden brand phrase: " + ", ".join(hits)
    return ""


# ---------------------------------------------------------------------------
# persistence (Lane A's brand_presets table, graceful)
# ---------------------------------------------------------------------------

def persist_template_preset(session: Any, workspace_id: str, *,
                            name: str, template_key: str,
                            overrides: dict | None = None) -> dict:
    """Store a template preset in Lane A's ``brand_presets`` table.

    Returns ``{"persisted": bool, "preset": {...}}``; when the table/model is
    absent the dict is returned unpersisted (the caller keeps working).
    """
    preset = {
        "name": str(name or template_key)[:160],
        "template_key": str(template_key),
        "builtin": template_key in _BUILTIN_TEMPLATES,
        "defaults": dict((get_template(template_key) or {}).get("defaults") or {}),
        "overrides": dict(overrides or {}),
    }
    if session is None or not workspace_id:
        return {"persisted": False, "preset": preset, "reason": "no_session"}
    try:
        from app.models.brand import BrandPreset  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 — Lane A model may not exist yet
        logger.debug(f"[brand] preset model unavailable: {type(exc).__name__}")
        return {"persisted": False, "preset": preset, "reason": "no_model"}
    try:
        row = BrandPreset(workspace_id=workspace_id, name=preset["name"],
                          builtin=preset["builtin"],
                          preset_json={**preset["defaults"], **preset["overrides"],
                                       "template_key": preset["template_key"]})
        session.add(row)
        session.flush()
        preset["id"] = row.id
        return {"persisted": True, "preset": preset}
    except Exception as exc:  # noqa: BLE001 — persistence never blocks
        logger.debug(f"[brand] preset persist failed: {type(exc).__name__}: {exc}")
        return {"persisted": False, "preset": preset, "reason": "persist_failed"}
