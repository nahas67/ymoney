"""Effective creative policy (Work 08 Lane A).

`EffectiveCreativePolicy` is the DETERMINISTIC hard-constraint view of the
resolved brand configuration: forbidden phrases, required disclaimers, logo
safe zone, approved voices/avatars, brand colors, caption/CTA style, tone,
vocabulary and pronunciation rules -- plus per-field PROVENANCE (which level of
the precedence chain set each field) and the `effective_config_id` of the
persisted snapshot.

Precedence (single source of truth, last wins; each level only overrides the
keys it sets)::

    workspace  ->  brand  ->  campaign  ->  content  ->  platform

LLMs NEVER override these constraints: they may only fill in free-form copy
inside them. Consumers pass the policy to generation and the verifier checks
the output against it -- nothing in this module calls a model.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.engine.brand.dna import HEX_COLOR_RE, normalize_platform

# precedence order -- last wins
LEVELS = ("workspace", "brand", "campaign", "content", "platform")

# keys that are HARD constraints: a level that sets them to an empty value
# does not erase the value inherited from a lower level (empty never overrides).
HARD_CONSTRAINT_KEYS = (
    "forbidden_phrases",
    "required_disclaimers",
    "logo_safe_zone",
    "approved_voices",
    "approved_avatars",
)

DEFAULT_LOGO_SAFE_ZONE = {"top": 0.08, "right": 0.06, "bottom": 0.14, "left": 0.06}

# workspace-level baseline: every policy field starts here (provenance =
# "workspace"), later levels only change keys they explicitly set.
WORKSPACE_DEFAULTS: dict[str, Any] = {
    "forbidden_phrases": [],
    "required_disclaimers": [],
    "logo_safe_zone": dict(DEFAULT_LOGO_SAFE_ZONE),
    "approved_voices": [],
    "approved_avatars": [],
    "brand_colors": {},
    "caption_style": {},
    "cta_style": {},
    "tone": "",
    "vocabulary": {"preferred": [], "avoid": []},
    "pronunciation_rules": [],
}

# candidate dotted paths per policy field (first present-with-highest-level wins)
_FIELD_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "forbidden_phrases": (("forbidden_phrases",),),
    "required_disclaimers": (("required_disclaimers",), ("disclosures",)),
    "logo_safe_zone": (("logo_safe_zone",),),
    "approved_voices": (
        ("approved_voices",),
        ("voice_identity", "approved"),
        ("voice_identity", "approved_ids"),
    ),
    "approved_avatars": (
        ("approved_avatars",),
        ("avatar_identity", "approved"),
        ("avatar_identity", "approved_ids"),
    ),
    "brand_colors": (("brand_colors",), ("colors",)),
    "caption_style": (("caption_style",),),
    "cta_style": (("cta_style",),),
    "tone": (("tone",), ("writing_tone",)),
    "vocabulary": (("vocabulary",),),
    "pronunciation_rules": (("pronunciation_rules",),),
    # extras consumed by the verifier / generation paths
    "approved_logos": (("approved_logos",), ("logos",)),
    "watermark": (("watermark",),),
    "thumbnail_style": (("thumbnail_style",),),
    "fonts": (("fonts",),),
    "claims_policy": (("claims_policy",),),
}

_MISSING = object()


def _get_path(config: dict, path: tuple[str, ...]) -> Any:
    node: Any = config
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _path_level(path: tuple[str, ...], provenance: dict[str, str]) -> int:
    """Highest precedence level that set this path (or any leaf under it)."""
    dotted = ".".join(path)
    best = -1
    order = {name: i for i, name in enumerate(LEVELS)}
    for key, level in provenance.items():
        if key == dotted or key.startswith(dotted + "."):
            best = max(best, order.get(level, -1))
    return best


def _select(
    config: dict, field_name: str, provenance: dict[str, str]
) -> tuple[Any, tuple[str, ...]]:
    """Value + path of a policy field: highest-level candidate wins."""
    candidates = _FIELD_PATHS.get(field_name, ((field_name,),))
    best_key: tuple[int, int] | None = None
    best_value: Any = None
    best_path: tuple[str, ...] = ()
    for index, path in enumerate(candidates):
        value = _get_path(config, path)
        if value is _MISSING or value is None:
            continue
        key = (_path_level(path, provenance), -index)
        if best_key is None or key > best_key:
            best_key, best_value, best_path = key, value, path
    return best_value, best_path


# ---------------------------------------------------------------------------
# normalizers (deterministic, no model calls)
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text and text not in out:
                out.append(text)
        return out
    return []


def _as_hex_list(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        values = list(value.values())
    else:
        values = list(value or []) if isinstance(value, (list, tuple)) else [value]
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        text = text.lower() if HEX_COLOR_RE.match(text) else text
        if text not in out:
            out.append(text)
    return out


def _as_tone(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("tone", "default", "voice", "style"):
            if isinstance(value.get(key), str) and value[key].strip():
                return str(value[key]).strip()
    return ""


def _as_vocabulary(value: Any) -> dict:
    if isinstance(value, dict):
        return {
            "preferred": _as_list(value.get("preferred") or value.get("terms")),
            "avoid": _as_list(value.get("avoid") or value.get("banned")),
        }
    return {"preferred": _as_list(value), "avoid": []}


def _as_dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _as_rules(value: Any) -> list:
    """pronunciation_rules: list of {term, pronunciation} entries."""
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, dict):
        return [value] if value else []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return []


# ---------------------------------------------------------------------------
# the policy
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EffectiveCreativePolicy:
    """Typed hard constraints + provenance for one resolution.

    Deterministic by construction: given the same workspace/brand/overrides,
    every call returns identical values. LLMs may never override these
    constraints -- they are inputs to generation, never outputs of it.
    """

    forbidden_phrases: list[str] = field(default_factory=list)
    required_disclaimers: list[str] = field(default_factory=list)
    logo_safe_zone: dict = field(default_factory=dict)
    approved_voices: list[str] = field(default_factory=list)
    approved_avatars: list[str] = field(default_factory=list)
    brand_colors: list[str] = field(default_factory=list)
    caption_style: dict = field(default_factory=dict)
    cta_style: dict = field(default_factory=dict)
    tone: str = ""
    vocabulary: dict = field(default_factory=dict)
    pronunciation_rules: list = field(default_factory=list)
    platform: str | None = None
    # per-field provenance: which level (workspace|brand|campaign|content|
    # platform) set this field
    provenance: dict[str, str] = field(default_factory=dict)
    effective_config_id: str = ""
    # -- context ----------------------------------------------------------
    dna_version: str = ""
    workspace_id: str = ""
    brand_id: str = ""
    subject: dict = field(default_factory=dict)
    # -- extras used by the verifier / generation -------------------------
    approved_logos: list[str] = field(default_factory=list)
    watermark: dict = field(default_factory=dict)
    thumbnail_style: dict = field(default_factory=dict)
    fonts: dict = field(default_factory=dict)
    claims_policy: dict = field(default_factory=dict)

    def hard_constraints_dict(self) -> dict:
        """The non-negotiable subset every consumer must honor."""
        return {
            "forbidden_phrases": list(self.forbidden_phrases),
            "required_disclaimers": list(self.required_disclaimers),
            "logo_safe_zone": dict(self.logo_safe_zone),
            "approved_voices": list(self.approved_voices),
            "approved_avatars": list(self.approved_avatars),
        }

    def as_dict(self) -> dict:
        """JSON-safe full serialization (API responses, snapshots)."""
        return asdict(self)

    @classmethod
    def build(
        cls,
        config: dict,
        provenance: dict[str, str] | None = None,
        *,
        platform: str | None = None,
        workspace_id: str = "",
        brand_id: str = "",
        subject: dict | None = None,
        dna_version: str = "",
        effective_config_id: str = "",
    ) -> EffectiveCreativePolicy:
        """Project a merged effective config onto the typed policy fields."""
        merged = dict(config or {})
        prov = dict(provenance or {})
        token = normalize_platform(platform) or None

        def pick(name: str) -> tuple[Any, tuple[str, ...]]:
            return _select(merged, name, prov)

        def provenance_for(name: str, used: tuple[str, ...]) -> str:
            if used:
                level = prov.get(".".join(used))
                if level:
                    return level
                idx = _path_level(used, prov)
                if idx >= 0:
                    return LEVELS[idx]
            # fall back: any leaf under the field's candidates
            for path in _FIELD_PATHS.get(name, ((name,),)):
                idx = _path_level(path, prov)
                if idx >= 0:
                    return LEVELS[idx]
            return "workspace"

        fields: dict[str, Any] = {}
        field_prov: dict[str, str] = {}
        for name in (
            "forbidden_phrases",
            "required_disclaimers",
            "logo_safe_zone",
            "approved_voices",
            "approved_avatars",
            "brand_colors",
            "caption_style",
            "cta_style",
            "tone",
            "vocabulary",
            "pronunciation_rules",
            "approved_logos",
            "watermark",
            "thumbnail_style",
            "fonts",
            "claims_policy",
        ):
            value, used = pick(name)
            fields[name] = value
            field_prov[name] = provenance_for(name, used)

        safe_zone = _as_dict(fields["logo_safe_zone"]) or dict(DEFAULT_LOGO_SAFE_ZONE)
        return cls(
            forbidden_phrases=_as_list(fields["forbidden_phrases"]),
            required_disclaimers=_as_list(fields["required_disclaimers"]),
            logo_safe_zone=safe_zone,
            approved_voices=_as_list(fields["approved_voices"]),
            approved_avatars=_as_list(fields["approved_avatars"]),
            brand_colors=_as_hex_list(fields["brand_colors"]),
            caption_style=_as_dict(fields["caption_style"]),
            cta_style=_as_dict(fields["cta_style"]),
            tone=_as_tone(fields["tone"]),
            vocabulary=_as_vocabulary(fields["vocabulary"]),
            pronunciation_rules=_as_rules(fields["pronunciation_rules"]),
            platform=token,
            provenance=field_prov,
            effective_config_id=effective_config_id,
            dna_version=dna_version,
            workspace_id=workspace_id,
            brand_id=brand_id,
            subject=dict(subject or {}),
            approved_logos=_as_list(fields["approved_logos"]),
            watermark=_as_dict(fields["watermark"]),
            thumbnail_style=_as_dict(fields["thumbnail_style"]),
            fonts=_as_dict(fields["fonts"]),
            claims_policy=_as_dict(fields["claims_policy"]),
        )
