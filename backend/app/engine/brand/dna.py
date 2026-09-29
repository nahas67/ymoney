"""Typed BrandDNA core (Work 08 Lane A).

`BrandDNA` is the full creative identity of a brand: visual kit (logos as
MediaAsset REFS only -- never bytes, colors, fonts, typography), motion/caption
language (caption style, CTA, watermark, intro/outro, lower thirds), audio
identity (music, voice, avatar), verbal identity (tone, vocabulary,
pronunciation rules, forbidden phrases, claims policy) plus `platform_overrides`
-- per-platform fragments applied LAST by the inheritance resolver.

Validation is strict where mistakes are expensive:
  * colors must be hex (#abc / #aabbcc),
  * forbidden_phrases must be non-empty strings,
  * voice_identity / avatar_identity hold REFERENCES (ids), never provider blobs,
  * platform_overrides is keyed by platform token and every fragment is checked.

`effective_dna()` is the merging helper: layers merge deep (later wins), the
platform fragment -- when given -- is applied last.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
PLATFORM_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{1,29}$")
# platforms are normalized to lowercase tokens for override lookup
PLATFORM_ALIASES = {"yt": "youtube", "ig": "instagram", "x": "twitter"}


class BrandDNASchemaError(ValueError):
    """Raised when a BrandDNA document (or fragment) fails validation."""


def normalize_platform(platform: str | None) -> str:
    """Lowercase/alias-normalize a platform token ('' when unset)."""
    token = str(platform or "").strip().lower()
    return PLATFORM_ALIASES.get(token, token)


def _normalize_hex(value: Any, ctx: str) -> str:
    raw = str(value or "").strip()
    if not HEX_COLOR_RE.match(raw):
        raise BrandDNASchemaError(f"{ctx}: {raw!r} is not a hex color (#abc/#aabbcc)")
    return raw.lower()


def check_colors(colors: Any, ctx: str = "colors") -> dict[str, str]:
    """Validate a role -> hex map (returns normalized lowercase hex)."""
    if colors is None:
        return {}
    if not isinstance(colors, dict):
        raise BrandDNASchemaError(f"{ctx} must be a mapping of role -> hex color")
    return {str(k): _normalize_hex(v, f"{ctx}.{k}") for k, v in colors.items()}


def check_phrases(phrases: Any, ctx: str = "forbidden_phrases") -> list[str]:
    """Validate a phrase list: only non-empty, non-blank strings."""
    if phrases is None:
        return []
    if isinstance(phrases, str):
        phrases = [phrases]
    if not isinstance(phrases, (list, tuple)):
        raise BrandDNASchemaError(f"{ctx} must be a list of strings")
    out: list[str] = []
    for item in phrases:
        if not isinstance(item, str) or not item.strip():
            raise BrandDNASchemaError(f"{ctx} entries must be non-empty strings: {item!r}")
        text = item.strip()
        if text not in out:
            out.append(text)
    return out


def check_reference_list(value: Any, ctx: str) -> list[str]:
    """A list of references (media asset ids / voice ids / avatar ids)."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise BrandDNASchemaError(f"{ctx} must be a list of reference strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise BrandDNASchemaError(f"{ctx} entries must be non-empty reference strings: {item!r}")
        text = item.strip()
        if text not in out:
            out.append(text)
    return out


def _check_identity(value: Any, ctx: str) -> dict:
    """voice_identity / avatar_identity: reference containers, not provider blobs."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BrandDNASchemaError(f"{ctx} must be a mapping of references")
    out = dict(value)
    for key in ("approved", "approved_ids"):
        if key in out:
            out[key] = check_reference_list(out[key], f"{ctx}.{key}")
    if "default" in out and out["default"] is not None:
        if not isinstance(out["default"], str):
            raise BrandDNASchemaError(f"{ctx}.default must be a reference string")
        out["default"] = out["default"].strip()
    return out


def _check_fragment(fragment: Any, ctx: str) -> dict:
    """Light validation of a partial BrandDNA (platform override fragment)."""
    if fragment is None:
        return {}
    if not isinstance(fragment, dict):
        raise BrandDNASchemaError(f"{ctx} must be a mapping")
    out = dict(fragment)
    if "colors" in out:
        out["colors"] = check_colors(out["colors"], f"{ctx}.colors")
    if "brand_colors" in out:
        out["brand_colors"] = check_colors(out["brand_colors"], f"{ctx}.brand_colors")
    if "forbidden_phrases" in out:
        out["forbidden_phrases"] = check_phrases(out["forbidden_phrases"], f"{ctx}.forbidden_phrases")
    if "required_disclaimers" in out:
        out["required_disclaimers"] = check_phrases(
            out["required_disclaimers"], f"{ctx}.required_disclaimers"
        )
    if "logos" in out:
        out["logos"] = check_reference_list(out["logos"], f"{ctx}.logos")
    return out


class BrandDNA(BaseModel):
    """The full typed brand identity document (stored as `brand_dna.dna_json`)."""

    # extra fields (required_disclaimers, approved_voices, logo_safe_zone, ...)
    # are allowed and pass straight through to the effective config.
    model_config = ConfigDict(extra="allow")

    # -- visual kit: references only, never bytes -------------------------
    logos: list[str] = Field(default_factory=list)  # MediaAsset ids/refs
    colors: dict[str, str] = Field(default_factory=dict)  # role -> hex
    fonts: dict[str, Any] = Field(default_factory=dict)
    typography: dict[str, Any] = Field(default_factory=dict)
    # -- motion / layout --------------------------------------------------
    caption_style: dict[str, Any] = Field(default_factory=dict)
    cta_style: dict[str, Any] = Field(default_factory=dict)
    watermark: dict[str, Any] = Field(default_factory=dict)
    intro: str = ""  # MediaAsset ref for the intro sting
    outro: str = ""  # MediaAsset ref for the outro/end card
    lower_thirds: list[Any] = Field(default_factory=list)
    motion_language: dict[str, Any] = Field(default_factory=dict)
    # -- audio / persona --------------------------------------------------
    music_prefs: dict[str, Any] = Field(default_factory=dict)
    voice_identity: dict[str, Any] = Field(default_factory=dict)  # refs only
    avatar_identity: dict[str, Any] = Field(default_factory=dict)  # refs only
    # -- look & feel ------------------------------------------------------
    visual_style: dict[str, Any] = Field(default_factory=dict)
    broll_prefs: dict[str, Any] = Field(default_factory=dict)
    thumbnail_style: dict[str, Any] = Field(default_factory=dict)
    # -- verbal identity --------------------------------------------------
    writing_tone: str | dict[str, Any] = ""
    vocabulary: dict[str, Any] = Field(default_factory=dict)
    pronunciation_rules: list[Any] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    claims_policy: dict[str, Any] = Field(default_factory=dict)
    # -- per-platform fragments (applied LAST by inheritance) -------------
    platform_overrides: dict[str, dict] = Field(default_factory=dict)

    # -- validators -------------------------------------------------------
    @field_validator("logos")
    @classmethod
    def _v_logos(cls, value: list[str]) -> list[str]:
        return check_reference_list(value, "logos")

    @field_validator("colors")
    @classmethod
    def _v_colors(cls, value: dict[str, str]) -> dict[str, str]:
        return check_colors(value, "colors")

    @field_validator("forbidden_phrases")
    @classmethod
    def _v_phrases(cls, value: list[str]) -> list[str]:
        return check_phrases(value, "forbidden_phrases")

    @field_validator("voice_identity", "avatar_identity")
    @classmethod
    def _v_identity(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _check_identity(value, "identity")

    @field_validator("platform_overrides")
    @classmethod
    def _v_platforms(cls, value: dict[str, dict]) -> dict[str, dict]:
        if not isinstance(value, dict):
            raise BrandDNASchemaError("platform_overrides must be a mapping")
        out: dict[str, dict] = {}
        for raw_key, fragment in value.items():
            key = normalize_platform(str(raw_key))
            if not PLATFORM_TOKEN_RE.match(key):
                raise BrandDNASchemaError(
                    f"platform_overrides key {raw_key!r} is not a platform token"
                )
            out[key] = _check_fragment(fragment, f"platform_overrides.{key}")
        return out

    # -- helpers ----------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        """Deterministic JSON-safe dump (used for storage + version hashing)."""
        return self.model_dump(mode="json")

    def effective_dna(self, platform: str | None = None) -> BrandDNA:
        """This DNA with the platform fragment (if any) applied last."""
        return effective_dna(self, platform=platform)

    def version(self) -> str:
        """Stable content hash of the document (sha256:16 hex chars)."""
        return dna_version(self)


def deep_merge(base: dict[str, Any], patch: dict[str, Any] | None) -> dict[str, Any]:
    """Deep merge: dicts recurse, everything else replaces; None never overrides."""
    merged = dict(base or {})
    for key, value in (patch or {}).items():
        if value is None:
            continue
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _layer_dict(layer: BrandDNA | dict[str, Any] | None) -> dict[str, Any]:
    if layer is None:
        return {}
    if isinstance(layer, BrandDNA):
        return layer.as_dict()
    if isinstance(layer, dict):
        return dict(layer)
    raise BrandDNASchemaError(f"unsupported BrandDNA layer: {type(layer).__name__}")


def effective_dna(
    *layers: BrandDNA | dict[str, Any] | None,
    platform: str | None = None,
) -> BrandDNA:
    """Merge BrandDNA layers in order (later wins), platform fragment last.

    Deterministic: same inputs always produce the same document.
    """
    merged: dict[str, Any] = {}
    for layer in layers:
        merged = deep_merge(merged, _layer_dict(layer))
    token = normalize_platform(platform)
    if token:
        fragment = (merged.get("platform_overrides") or {}).get(token) or {}
        if fragment:
            merged = deep_merge(merged, fragment)
    try:
        return BrandDNA.model_validate(merged)
    except BrandDNASchemaError:
        raise
    except Exception as exc:  # pydantic ValidationError and friends
        raise BrandDNASchemaError(f"invalid BrandDNA: {exc}") from exc


def dna_version(dna: BrandDNA | dict[str, Any] | None) -> str:
    """Stable content hash of a DNA document: ``sha256:<16 hex>``."""
    payload = dna.as_dict() if isinstance(dna, BrandDNA) else dict(dna or {})
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
