"""Lower thirds (Work 13 §7).

A lower third is an OPTIONAL timeline item built from a
:class:`~app.engine.motion.templates.MotionTemplate`. Nothing here invents
content: :func:`build_lower_third` only fills a slot when the caller supplies a
real value, and the template layer refuses a
``requires_known_metadata`` instance whose bindings have no source. An
unknown name, role, location or source produces NO lower third -- not a
plausible-looking placeholder.

Kinds supported: person/name, role/title, topic, location, source
attribution, product information.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.engine.motion.templates import (
    MotionInstance,
    MotionTemplateError,
    get_template,
    validate_instance,
)

__all__ = [
    "LOWER_THIRD_KINDS",
    "LowerThirdRequest",
    "LowerThirdResult",
    "build_lower_third",
    "lower_third_kinds",
]

#: The documented lower-third kinds (§7) mapped to the template that serves
#: each one. Adding a kind means adding a mapping here.
LOWER_THIRD_KINDS: dict[str, str] = {
    "person": "lower_third_name",
    "name": "lower_third_name",
    "role": "lower_third_name",
    "topic": "lower_third_topic",
    "location": "lower_third_topic",
    "source": "lower_third_source",
    "product": "product_label",
}


def lower_third_kinds() -> list[str]:
    return sorted(LOWER_THIRD_KINDS)


@dataclass
class LowerThirdRequest:
    """What the caller knows. Absent fields stay absent."""

    kind: str = "person"
    start: float = 0.0
    duration: float = 4.0
    name: str = ""
    role: str = ""
    topic: str = ""
    location: str = ""
    source: str = ""
    product: str = ""
    tagline: str = ""
    clip_id: str = ""
    style_patch: dict = field(default_factory=dict)
    #: The set of field names the caller can actually vouch for.
    known: tuple[str, ...] = ()

    def bindings_for(self, kind: str) -> dict[str, str]:
        """Slot bindings for a kind, filtered to KNOWN values only."""
        table = {
            "person": ("name", "role"),
            "name": ("name",),
            "role": ("role",),
            "topic": ("topic",),
            "location": ("location",),
            "source": ("source",),
            "product": ("product", "tagline"),
        }
        slots = table.get(kind, ())
        out: dict[str, str] = {}
        for slot in slots:
            value = str(getattr(self, slot, "") or "").strip()
            if not value:
                continue
            # When a caller declares what it knows, an undeclared field is
            # treated as unknown even if a value happens to be present.
            if self.known and slot not in self.known:
                continue
            out[slot] = value
        return out


@dataclass
class LowerThirdResult:
    """Outcome of a build attempt -- including an honest 'declined'."""

    built: bool
    reason: str = ""
    instance: MotionInstance | None = None
    clip: dict | None = None
    kind: str = ""
    template: str = ""
    missing: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "built": self.built,
            "reason": self.reason,
            "kind": self.kind,
            "template": self.template,
            "missing": list(self.missing),
            "clip_id": (self.clip or {}).get("id", ""),
        }


def build_lower_third(request: LowerThirdRequest) -> LowerThirdResult:
    """Build a lower-third clip, or decline honestly.

    Declines (with a reason) when: the kind is unknown, the template has no
    slot that the caller can fill, or the required metadata is not known.
    """
    kind = str(request.kind or "").strip().lower()
    template_key = LOWER_THIRD_KINDS.get(kind)
    if template_key is None:
        return LowerThirdResult(
            built=False, kind=kind,
            reason=f"unknown lower-third kind {request.kind!r}; "
                   f"known {lower_third_kinds()}",
        )
    tpl = get_template(template_key)
    bindings = request.bindings_for(kind)
    if not bindings:
        return LowerThirdResult(
            built=False, kind=kind, template=template_key,
            reason="no known metadata for this lower third - refusing to invent it",
            missing=tuple(s for s in tpl.slots if s not in bindings),
        )
    missing = tuple(s for s in tpl.slots if s not in bindings)
    try:
        instance = validate_instance(
            {
                "template": template_key,
                "bindings": bindings,
                "start": request.start,
                "duration": request.duration,
                "clip_id": request.clip_id,
                "style": request.style_patch,
            },
            metadata_available=set(bindings),
        )
    except MotionTemplateError as exc:
        return LowerThirdResult(built=False, kind=kind, template=template_key,
                                reason=str(exc), missing=missing)
    return LowerThirdResult(
        built=True, kind=kind, template=template_key,
        instance=instance, clip=instance.to_clip(), missing=missing,
    )