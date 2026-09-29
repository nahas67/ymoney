"""Brand consistency verifier (Work 08 Lane A).

Turns an `EffectiveCreativePolicy` into a `BrandConsistencyReport` with the
same status vocabulary as the other gates
(`PASS | PASS_WITH_WARNINGS | REVIEW_REQUIRED | FAIL`) and the same rollup
pattern as `engine/ugc/qc.py` (worst check wins).

Deterministic FIRST: every check is a metadata/payload/substring check that
runs without a model -- forbidden phrases and required disclosures are found
by scanning the artifact's own text, colors/fonts/logos/caption/CTA by
comparing declared values against the policy, watermark and thumbnail by
presence/convention rules.

The DecisionEngine semantic review is SHADOW-only and advisory: it is appended
AFTER the rollup with `authoritative=False`, wrapped in lazy imports and
bare excepts, so it can never change the verdict and never raise.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.engine.brand.policy import EffectiveCreativePolicy

QC_STATUSES = ("PASS", "PASS_WITH_WARNINGS", "REVIEW_REQUIRED", "FAIL")
_CHECK_ORDER = {"fail": 3, "review": 2, "warning": 1, "pass": 0}
_STATUS_FOR = {
    "fail": "FAIL",
    "review": "REVIEW_REQUIRED",
    "warning": "PASS_WITH_WARNINGS",
    "pass": "PASS",
}

HEX_TOKEN_RE = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
# grayscale/neutral tokens that are never "off-brand"
NEUTRAL_HEX = {
    "#000000", "#ffffff", "#f5f5f5", "#f9fafb", "#f8fafc", "#e5e7eb", "#e2e8f0",
    "#d1d5db", "#cbd5e1", "#9ca3af", "#6b7288", "#64748b", "#475569", "#374155",
    "#334155", "#1f2937", "#111827", "#0f172a",
}

VOICE_KEYS = ("voice_id", "voice_ref", "tts_voice", "narrator_voice", "voice")
AVATAR_KEYS = ("avatar_id", "avatar_ref", "avatar_profile_id", "avatar")
LOGO_KEYS = ("logo", "logo_id", "logo_ref", "logo_asset_id", "logos")
COLOR_KEYS = (
    "colors", "palette", "brand_colors", "accent",
    "text_color", "background_color", "bg_color",
)
CAPTION_KEYS = ("caption_style", "caption_style_id", "captions_style")
CTA_KEYS = ("cta_style", "cta_style_id")
FONT_KEYS = ("font", "fonts", "font_family", "heading_font", "body_font", "typography")
WATERMARK_KEYS = ("watermark", "watermark_ref", "watermark_asset_id", "watermark_id")
REF_KEYS = ("id", "ref", "asset_id", "media_asset_id", "profile_id",
            "voice_id", "avatar_id", "name", "value")

VISUAL_KINDS = frozenset(
    {"clip", "render", "video", "timeline", "short", "visual", "thumbnail",
     "cover", "thumb", "broll"}
)
THUMBNAIL_KINDS = frozenset({"thumbnail", "cover", "thumb"})

DEFAULT_THUMBNAIL_ASPECTS = ["9:16"]


def _check(status: str, detail: str = "", **extra: Any) -> dict:
    return {"status": status, "detail": detail, **extra}


def _rollup(checks: dict[str, dict]) -> str:
    worst = max(
        (c.get("status", "pass") for c in checks.values()),
        key=lambda s: _CHECK_ORDER.get(s, 0),
        default="pass",
    )
    return _STATUS_FOR[worst]


# ---------------------------------------------------------------------------
# artifact normalization + scanning
# ---------------------------------------------------------------------------


def _as_artifact(artifact: Any) -> dict:
    if isinstance(artifact, dict):
        return artifact
    if isinstance(artifact, str):
        text = artifact.strip()
        if text[:1] in "{[":
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        return {"text": artifact}
    if isinstance(artifact, (list, tuple)):
        return {"items": list(artifact)}
    return {"value": str(artifact)}


def _walk(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key).lower(), value
            yield from _walk(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk(item)


def _corpus(artifact: dict) -> str:
    """Every string value in the artifact -- the deterministic text surface."""
    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(artifact)
    return "\n".join(p for p in parts if p)


def _find_values(artifact: dict, keys: tuple[str, ...]) -> list[Any]:
    wanted = {k.lower() for k in keys}
    return [value for key, value in _walk(artifact) if key in wanted]


def _refs_from(value: Any) -> list[str]:
    """Turn a declared value into reference strings (ignores bools/numbers)."""
    out: list[str] = []
    if isinstance(value, str):
        text = value.strip()
        if text:
            out.append(text)
    elif isinstance(value, dict):
        for key in REF_KEYS:
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                out.append(nested.strip())
    elif isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_refs_from(item))
    return out


def _declared_refs(artifact: dict, keys: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for value in _find_values(artifact, keys):
        for ref in _refs_from(value):
            if ref.lower() not in {r.lower() for r in out}:
                out.append(ref)
    return out


def _contains_phrase(corpus: str, phrase: str) -> bool:
    """Word-boundary aware (whole words when the phrase is word-like)."""
    needle = str(phrase or "").strip()
    if not needle or not corpus:
        return False
    if re.search(r"\w", needle):
        pattern = r"(?<!\w)" + re.escape(needle) + r"(?!\w)"
        return re.search(pattern, corpus, re.IGNORECASE) is not None
    return needle.lower() in corpus.lower()


def _expand_hex(token: str) -> str:
    raw = token.strip().lower()
    if len(raw) == 4:  # #abc -> #aabbcc
        return "#" + "".join(ch * 2 for ch in raw[1:])
    return raw


def _extract_hexes(artifact: dict, corpus: str) -> list[str]:
    found: list[str] = []
    for value in _find_values(artifact, COLOR_KEYS):
        if isinstance(value, (str, dict, list, tuple)):
            for token in HEX_TOKEN_RE.findall(json.dumps(value) if not isinstance(value, str) else value):
                found.append(_expand_hex(token))
    for token in HEX_TOKEN_RE.findall(corpus):
        found.append(_expand_hex(token))
    out: list[str] = []
    for hex_value in found:
        if hex_value not in out:
            out.append(hex_value)
    return out


def _scalar(value: Any) -> Any:
    return value if isinstance(value, (str, int, float, bool)) else None


def _style_id(value: Any) -> str:
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, dict):
        for key in ("style", "name", "preset", "id"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip().lower()
    return ""


def _style_mismatches(declared: Any, policy_style: dict) -> list[str]:
    """Overlapping scalar keys with different values (deterministic compare)."""
    if not isinstance(declared, dict) or not policy_style:
        return []
    mismatches: list[str] = []
    for key, policy_value in policy_style.items():
        if key in ("approved", "id", "preset", "name", "style"):
            continue
        if key not in declared:
            continue
        left, right = _scalar(declared.get(key)), _scalar(policy_value)
        if left is None or right is None:
            continue
        if str(left).strip().lower() != str(right).strip().lower():
            mismatches.append(f"{key}: {left!r} != {right!r}")
    return mismatches


def _approved_fonts(policy: EffectiveCreativePolicy) -> list[str]:
    fonts = policy.fonts or {}
    if not isinstance(fonts, dict) or not fonts:
        return []
    for key in ("approved", "families", "allowed"):
        if isinstance(fonts.get(key), (list, tuple)):
            return [str(v).strip() for v in fonts[key] if str(v or "").strip()]
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str) and node.strip():
            out.append(node.strip())
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(fonts)
    seen: list[str] = []
    for name in out:
        if name.lower() not in {s.lower() for s in seen}:
            seen.append(name)
    return seen


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@dataclass
class BrandConsistencyReport:
    """One brand-consistency verdict (rollup of deterministic checks)."""

    status: str = "PASS"
    checks: dict[str, dict] = field(default_factory=dict)
    artifact_kind: str = ""
    workspace_id: str = ""
    # deterministic checks are authoritative; the SHADOW semantic block never is
    authoritative: bool = True
    effective_config_id: str = ""
    provenance: dict[str, str] = field(default_factory=dict)
    semantic: dict = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.status == "FAIL"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "checks": self.checks,
            "artifact_kind": self.artifact_kind,
            "workspace_id": self.workspace_id,
            "authoritative": self.authoritative,
            "effective_config_id": self.effective_config_id,
            "provenance": dict(self.provenance),
            "semantic": dict(self.semantic),
            "report_type": "brand",
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> BrandConsistencyReport:
        raw = dict(data or {})
        status = str(raw.get("status") or "PASS")
        return cls(
            status=status if status in QC_STATUSES else "PASS",
            checks=dict(raw.get("checks") or {}),
            artifact_kind=str(raw.get("artifact_kind") or ""),
            workspace_id=str(raw.get("workspace_id") or ""),
            authoritative=bool(raw.get("authoritative", True)),
            effective_config_id=str(raw.get("effective_config_id") or ""),
            provenance=dict(raw.get("provenance") or {}),
            semantic=dict(raw.get("semantic") or {}),
        )


# ---------------------------------------------------------------------------
# SHADOW semantic advisory (never authoritative, never raises)
# ---------------------------------------------------------------------------


def _semantic_advisory(workspace_id: str, corpus: str,
                       policy: EffectiveCreativePolicy) -> dict:
    try:
        from app.engine.intelligence.decision import DecisionEngine
    except Exception as exc:  # pragma: no cover - engine always ships, be safe
        return _check(
            "pass",
            f"skipped (DecisionEngine unavailable: {type(exc).__name__})",
            authoritative=False, mode="SHADOW",
        )
    try:
        engine = DecisionEngine(workspace_id, mode="SHADOW", persist=False)
        out, _record = engine.verify(
            {
                "claim": (corpus or "")[:4000],
                "evidence": json.dumps(
                    {
                        "tone": policy.tone,
                        "vocabulary": policy.vocabulary,
                        "brand_colors": policy.brand_colors,
                        "caption_style": policy.caption_style,
                        "forbidden_phrases": policy.forbidden_phrases,
                    },
                    sort_keys=True,
                )[:4000],
                "criterion": "content matches the brand voice and visual identity",
            }
        )
        verdict = str((out or {}).get("status", "")) if isinstance(out, dict) else ""
        return _check(
            "pass" if verdict in ("VERIFIED", "") else "warn",
            f"SHADOW advisory: {verdict or 'no verdict'}",
            authoritative=False, mode="SHADOW",
        )
    except Exception as exc:  # advisory only - any failure degrades to skipped
        return _check(
            "pass",
            f"skipped ({type(exc).__name__})",
            authoritative=False, mode="SHADOW",
        )


# ---------------------------------------------------------------------------
# the deterministic checks
# ---------------------------------------------------------------------------


def _check_forbidden_phrases(corpus: str, policy: EffectiveCreativePolicy) -> dict:
    phrases = list(policy.forbidden_phrases)
    if not phrases:
        return _check("pass", "no forbidden phrases configured")
    found = [p for p in phrases if _contains_phrase(corpus, p)]
    if found:
        return _check("fail", f"forbidden phrase(s) present: {', '.join(found)}")
    if not corpus.strip():
        return _check("pass", "no text to scan")
    return _check("pass", "no forbidden phrases detected")


def _check_disclaimers(corpus: str, policy: EffectiveCreativePolicy) -> dict:
    required = list(policy.required_disclaimers)
    if not required:
        return _check("pass", "no disclosures required")
    if not corpus.strip():
        return _check("review", "required disclosure(s) cannot be verified: no text payload")
    missing = [d for d in required if not _contains_phrase(corpus, d)]
    if missing:
        return _check("fail", f"missing required disclosure(s): {', '.join(missing)}")
    return _check("pass", "all required disclosures present")


def _check_colors(artifact: dict, corpus: str, policy: EffectiveCreativePolicy) -> dict:
    approved = {c.lower() for c in policy.brand_colors}
    found = _extract_hexes(artifact, corpus)
    if not approved:
        return _check("pass", "no approved palette configured")
    if not found:
        return _check("pass", "no colors declared in artifact")
    unknown = [h for h in found if h not in approved and h not in NEUTRAL_HEX]
    if unknown:
        return _check(
            "review",
            f"off-brand color(s): {', '.join(unknown)} (approved: {', '.join(sorted(approved))})",
        )
    return _check("pass", f"{len(found)} color token(s) on-brand")


def _check_fonts(artifact: dict, policy: EffectiveCreativePolicy) -> dict:
    approved = _approved_fonts(policy)
    if not approved:
        return _check("pass", "no font policy configured")
    declared: list[str] = []
    for value in _find_values(artifact, FONT_KEYS):
        for ref in _refs_from(value):
            if ref.lower() not in {d.lower() for d in declared}:
                declared.append(ref)
    if not declared:
        return _check("pass", "no font declared in artifact")
    approved_lower = {a.lower() for a in approved}
    off = [d for d in declared if d.lower() not in approved_lower]
    if off:
        return _check("review", f"off-brand font(s): {', '.join(off)}")
    return _check("pass", "declared font(s) approved")


def _check_logos(artifact: dict, policy: EffectiveCreativePolicy) -> dict:
    declared = _declared_refs(artifact, LOGO_KEYS)
    approved = list(policy.approved_logos)
    if not approved:
        return _check("pass", "no approved logo registry configured")
    if not declared:
        return _check("pass", "no logo declared in artifact")
    approved_lower = {a.lower() for a in approved}
    off = [d for d in declared if d.lower() not in approved_lower]
    if off:
        return _check("fail", f"unapproved logo reference(s): {', '.join(off)}")
    return _check("pass", "logo reference approved")


def _check_caption_style(artifact: dict, policy: EffectiveCreativePolicy) -> dict:
    policy_style = policy.caption_style
    declared = _find_values(artifact, CAPTION_KEYS)
    if not policy_style:
        return _check("pass", "no caption style policy configured")
    if not declared:
        return _check("pass", "no caption style declared in artifact")
    policy_id = _style_id(policy_style)
    for value in declared:
        declared_id = _style_id(value)
        if declared_id and policy_id and declared_id != policy_id:
            return _check(
                "review",
                f"caption style {declared_id!r} != approved {policy_id!r}",
            )
        mismatches = _style_mismatches(value, policy_style)
        if mismatches:
            return _check("review", "caption style mismatch: " + "; ".join(mismatches))
    return _check("pass", "caption style matches policy")


def _check_cta_style(artifact: dict, policy: EffectiveCreativePolicy) -> dict:
    policy_style = policy.cta_style
    declared = _find_values(artifact, CTA_KEYS)
    if not policy_style:
        return _check("pass", "no CTA style policy configured")
    if not declared:
        return _check("pass", "no CTA style declared in artifact")
    policy_id = _style_id(policy_style)
    for value in declared:
        declared_id = _style_id(value)
        if declared_id and policy_id and declared_id != policy_id:
            return _check("review", f"CTA style {declared_id!r} != approved {policy_id!r}")
        mismatches = _style_mismatches(value, policy_style)
        if mismatches:
            return _check("review", "CTA style mismatch: " + "; ".join(mismatches))
    return _check("pass", "CTA style matches policy")


def _check_voice(artifact: dict, policy: EffectiveCreativePolicy) -> dict:
    approved = list(policy.approved_voices)
    declared = _declared_refs(artifact, VOICE_KEYS)
    if not approved:
        return _check("pass", "no approved voice registry configured")
    if not declared:
        return _check("pass", "no voice declared in artifact")
    approved_lower = {a.lower() for a in approved}
    off = [d for d in declared if d.lower() not in approved_lower]
    if off:
        return _check("fail", f"unapproved voice(s): {', '.join(off)}")
    return _check("pass", "voice approved")


def _check_avatar(artifact: dict, policy: EffectiveCreativePolicy) -> dict:
    approved = list(policy.approved_avatars)
    declared = _declared_refs(artifact, AVATAR_KEYS)
    if not approved:
        return _check("pass", "no approved avatar registry configured")
    if not declared:
        return _check("pass", "no avatar declared in artifact")
    approved_lower = {a.lower() for a in approved}
    off = [d for d in declared if d.lower() not in approved_lower]
    if off:
        return _check("fail", f"unapproved avatar(s): {', '.join(off)}")
    return _check("pass", "avatar approved")


def _check_terminology(corpus: str, policy: EffectiveCreativePolicy) -> dict:
    vocabulary = policy.vocabulary or {}
    preferred = list(vocabulary.get("preferred") or [])
    avoid = list(vocabulary.get("avoid") or [])
    found_avoid = [t for t in avoid if _contains_phrase(corpus, t)]
    if found_avoid:
        return _check("review", f"discouraged term(s) used: {', '.join(found_avoid)}")
    if preferred and corpus.strip():
        found_preferred = [t for t in preferred if _contains_phrase(corpus, t)]
        if not found_preferred:
            return _check(
                "warning",
                "none of the preferred term(s) used: " + ", ".join(preferred[:5]),
            )
    return _check("pass", "terminology within vocabulary")


def _check_watermark(artifact: dict, artifact_kind: str,
                     policy: EffectiveCreativePolicy) -> dict:
    watermark = policy.watermark or {}
    required = bool(watermark.get("required") or watermark.get("enabled"))
    if not required:
        return _check("pass", "watermark not required by policy")
    kind = (artifact_kind or "").lower()
    if kind not in VISUAL_KINDS:
        return _check("pass", f"not applicable to artifact kind {kind or 'unknown'!r}")
    declared = _declared_refs(artifact, WATERMARK_KEYS)
    if not declared:
        return _check(
            "review",
            "watermark not declared in payload (metadata check cannot confirm pixels)",
        )
    target = ""
    for key in ("media_asset_id", "asset_id", "ref", "asset_ref", "id"):
        value = watermark.get(key)
        if isinstance(value, str) and value.strip():
            target = value.strip()
            break
    if target and target.lower() not in {d.lower() for d in declared}:
        return _check("fail", f"watermark {target!r} required but artifact uses {declared}")
    return _check("pass", "watermark declared")


def _check_thumbnail(artifact: dict, artifact_kind: str,
                     policy: EffectiveCreativePolicy) -> dict:
    kind = (artifact_kind or "").lower()
    if kind not in THUMBNAIL_KINDS:
        return _check("pass", f"not applicable to artifact kind {kind or 'unknown'!r}")
    style = policy.thumbnail_style or {}
    allowed: list[str] = []
    if isinstance(style.get("aspect_ratios"), (list, tuple)):
        allowed = [str(a) for a in style["aspect_ratios"]]
    elif isinstance(style.get("aspect_ratio"), str) and style["aspect_ratio"].strip():
        allowed = [style["aspect_ratio"].strip()]
    allowed = allowed or list(DEFAULT_THUMBNAIL_ASPECTS)
    declared = artifact.get("aspect_ratio") or artifact.get("aspect")
    if not isinstance(declared, str) or not declared.strip():
        return _check("review", f"no aspect ratio declared (expected one of {allowed})")
    if declared.strip() not in allowed:
        return _check(
            "review", f"aspect ratio {declared.strip()!r} not in approved {allowed}"
        )
    return _check("pass", f"aspect ratio {declared.strip()} approved")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def verify_artifact(
    session: Session,
    workspace_id: str,
    artifact_kind: str,
    artifact: Any,
    policy: EffectiveCreativePolicy | None = None,
) -> BrandConsistencyReport:
    """Verify one artifact payload against the effective brand policy.

    `artifact` is metadata (dict/JSON/text) -- clip payload keys, script text,
    caption/CTA declarations, palette tokens, refs. When `policy` is None the
    effective policy for the workspace is resolved (and snapshotted) first.

    Deterministic checks decide the status; the DecisionEngine semantic review
    is appended afterwards as SHADOW advisory (authoritative=False) and can
    never change the verdict.
    """
    from app.engine.brand.inheritance import resolve_effective_policy

    if isinstance(policy, dict):  # tolerate a serialized policy (wiring callers)
        from dataclasses import fields as dataclass_fields

        names = {f.name for f in dataclass_fields(EffectiveCreativePolicy)}
        policy = EffectiveCreativePolicy(
            **{k: v for k, v in policy.items() if k in names}
        )
    if policy is None:
        policy = resolve_effective_policy(session, workspace_id)

    doc = _as_artifact(artifact)
    corpus = _corpus(doc)
    kind = str(artifact_kind or "").strip().lower()

    checks: dict[str, dict] = {}
    checks["forbidden_phrases"] = _check_forbidden_phrases(corpus, policy)
    checks["required_disclaimers"] = _check_disclaimers(corpus, policy)
    checks["approved_colors"] = _check_colors(doc, corpus, policy)
    checks["fonts"] = _check_fonts(doc, policy)
    checks["logo_use"] = _check_logos(doc, policy)
    checks["caption_style"] = _check_caption_style(doc, policy)
    checks["cta_style"] = _check_cta_style(doc, policy)
    checks["voice_approval"] = _check_voice(doc, policy)
    checks["avatar_approval"] = _check_avatar(doc, policy)
    checks["terminology"] = _check_terminology(corpus, policy)
    checks["watermark"] = _check_watermark(doc, kind, policy)
    checks["thumbnail_conventions"] = _check_thumbnail(doc, kind, policy)

    # rollup FIRST; SHADOW advisory appended after so it never changes status
    status = _rollup(checks)
    semantic = _semantic_advisory(workspace_id, corpus, policy)

    return BrandConsistencyReport(
        status=status,
        checks=checks,
        artifact_kind=kind,
        workspace_id=workspace_id,
        authoritative=True,
        effective_config_id=policy.effective_config_id,
        provenance=dict(policy.provenance),
        semantic=semantic,
    )
