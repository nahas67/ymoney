"""Schema-restricted motion templates (Work 13 §6).

A :class:`MotionTemplate` is CONFIGURATION: placeholders, brand tokens,
timing, position, animation, asset references and safe-zone rules. It is not
code and there is deliberately no template execution hook -- a template can
only describe text placement and styling, and the renderer builds it with the
same :func:`~app.engine.captions.filters.build_text_filters` path used by any
other text clip. A template that tried to smuggle an ffmpeg fragment would be
rejected by :func:`validate_template`.

A :class:`MotionInstance` binds a template to concrete values at a point in
time. The instance is what lands on the timeline (as a ``text`` clip), so the
whole thing stays editable and non-destructive: change the instance, re-render,
the template definition is untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.engine.captions.style import CaptionStyle

__all__ = [
    "TEMPLATE_TYPES",
    "MotionInstance",
    "MotionTemplate",
    "MotionTemplateError",
    "TEMPLATES",
    "get_template",
    "template_dict",
    "validate_instance",
    "validate_template",
]


class MotionTemplateError(ValueError):
    """A motion template or instance was invalid."""


#: The closed template vocabulary (§6).
TEMPLATE_TYPES: tuple[str, ...] = (
    "TITLE", "LOWER_THIRD", "CALLOUT", "QUOTE", "STAT", "CTA", "CHAPTER",
    "PRODUCT_LABEL", "NAME_TAG",
)

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z0-9_]{1,40})\s*\}\}")
#: Reserved names a template may not bind from user input.
_RESERVED_BINDINGS = frozenset({"type", "start", "duration", "effects",
                                "text", "style", "id"})


def _slots(text: str) -> list[str]:
    seen: list[str] = []
    for name in _PLACEHOLDER_RE.findall(text or ""):
        if name not in seen:
            seen.append(name)
    return seen


@dataclass(frozen=True)
class MotionTemplate:
    """A reusable, schema-only motion layout."""

    key: str
    type: str
    label: str
    #: Body text with ``{{slot}}`` placeholders.
    body: str
    #: Optional second line (e.g. a role under a name).
    sub_body: str = ""
    position: str = "bottom"
    align: str = "left"
    default_duration: float = 3.0
    style: CaptionStyle = field(default_factory=CaptionStyle)
    #: Fraction-of-frame safe-zone inset this template reserves.
    safe_zone: dict = field(default_factory=dict)
    description: str = ""
    #: Whether the template may only be used with metadata that EXISTS.
    requires_known_metadata: bool = False

    @property
    def slots(self) -> list[str]:
        return _slots(f"{self.body}\n{self.sub_body}")

    def to_dict(self) -> dict:
        return {
            "key": self.key, "type": self.type, "label": self.label,
            "description": self.description,
            "body": self.body, "sub_body": self.sub_body,
            "position": self.position, "align": self.align,
            "default_duration": self.default_duration,
            "style": self.style.to_dict(),
            "safe_zone": dict(self.safe_zone),
            "slots": self.slots,
            "requires_known_metadata": self.requires_known_metadata,
        }


def _tpl(key, type_, label, body, **kwargs) -> MotionTemplate:
    sub = kwargs.pop("sub_body", "")
    pos = kwargs.pop("position", "bottom")
    align = kwargs.pop("align", "left")
    dur = kwargs.pop("default_duration", 3.0)
    desc = kwargs.pop("description", "")
    zone = kwargs.pop("safe_zone", {})
    needs = kwargs.pop("requires_known_metadata", False)
    style = CaptionStyle.from_dict(kwargs)
    return MotionTemplate(
        key=key, type=type_, label=label, body=body, sub_body=sub,
        position=pos, align=align, default_duration=float(dur), style=style,
        safe_zone=dict(zone), description=desc,
        requires_known_metadata=needs,
    )


TEMPLATES: dict[str, MotionTemplate] = {
    t.key: t for t in (
        _tpl("title_main", "TITLE", "Main Title",
             "{{headline}}", position="middle", align="center",
             default_duration=2.5,
             size=84, weight=800, case="upper", stroke_width=4,
             animation={"entrance": "pop", "duration": 0.3},
             description="Big centred opening title."),
        _tpl("title_kicker", "TITLE", "Kicker",
             "{{kicker}}", position="top", align="center",
             default_duration=2.0, size=44, weight=600, case="upper",
             animation={"entrance": "fade", "duration": 0.25}),
        _tpl("lower_third_name", "LOWER_THIRD", "Name + Role",
             "{{name}}", sub_body="{{role}}", position="bottom", align="left",
             default_duration=4.0, size=52, weight=700,
             background="#000000", padding=18, corner_radius=8,
             safe_zone={"left": 0.08, "bottom": 0.18},
             animation={"entrance": "slide_left", "duration": 0.3},
             requires_known_metadata=True,
             description="Speaker name with a role line."),
        _tpl("lower_third_topic", "LOWER_THIRD", "Topic",
             "{{topic}}", position="bottom", align="left",
             default_duration=3.5, size=46, weight=600,
             background="#0b0b0b", padding=16, corner_radius=8,
             safe_zone={"left": 0.08, "bottom": 0.18},
             animation={"entrance": "slide_up", "duration": 0.3},
             requires_known_metadata=True),
        _tpl("lower_third_source", "LOWER_THIRD", "Source",
             "Source: {{source}}", position="bottom", align="right",
             default_duration=3.0, size=30, weight=400,
             safe_zone={"right": 0.08, "bottom": 0.18},
             animation={"entrance": "fade", "duration": 0.25},
             requires_known_metadata=True,
             description="Attribution for reused footage."),
        _tpl("callout_emphasis", "CALLOUT", "Callout",
             "{{text}}", position="top", align="left",
             default_duration=2.5, size=48, weight=700,
             primary_color="#ffe94a", stroke_width=3,
             safe_zone={"left": 0.1, "top": 0.14},
             animation={"entrance": "pop", "duration": 0.25}),
        _tpl("quote_card", "QUOTE", "Quote",
             "\u201c{{quote}}\u201d", sub_body="\u2014 {{attribution}}",
             position="middle", align="center", default_duration=4.0,
             size=44, weight=500, italic=False, line_spacing=1.35,
             animation={"entrance": "fade", "easing": "ease_in_out",
                        "duration": 0.45},
             requires_known_metadata=True),
        _tpl("stat_number", "STAT", "Stat",
             "{{value}}", sub_body="{{label}}", position="middle",
             align="center", default_duration=3.0, size=96, weight=800,
             primary_color="#22c55e", animation={"entrance": "pop",
                                                 "duration": 0.3}),
        _tpl("cta_end", "CTA", "Call to Action",
             "{{cta}}", position="bottom", align="center",
             default_duration=3.0, size=56, weight=800, case="upper",
             stroke_width=3, animation={"entrance": "slide_up",
                                        "duration": 0.3}),
        _tpl("chapter_marker", "CHAPTER", "Chapter",
             "Chapter {{number}}", sub_body="{{title}}", position="top",
             align="left", default_duration=2.5, size=38, weight=600,
             case="upper", safe_zone={"left": 0.08, "top": 0.1},
             animation={"entrance": "wipe", "duration": 0.3}),
        _tpl("product_label", "PRODUCT_LABEL", "Product Label",
             "{{product}}", sub_body="{{tagline}}", position="bottom",
             align="right", default_duration=3.0, size=40, weight=600,
             background="#111111", padding=14, corner_radius=10,
             safe_zone={"right": 0.08, "bottom": 0.18},
             animation={"entrance": "fade", "duration": 0.28},
             requires_known_metadata=True),
        _tpl("name_tag", "NAME_TAG", "Name Tag",
             "{{name}}", position="top", align="center",
             default_duration=2.0, size=34, weight=600,
             safe_zone={"top": 0.1}, animation={"entrance": "fade",
                                                "duration": 0.2},
             requires_known_metadata=True),
    )
}


def get_template(key: str) -> MotionTemplate:
    token = str(key or "").strip().lower().replace("-", "_")
    tpl = TEMPLATES.get(token)
    if tpl is None:
        raise MotionTemplateError(
            f"unknown motion template {key!r}; known {sorted(TEMPLATES)}"
        )
    return tpl


def template_dict() -> list[dict]:
    return [t.to_dict() for t in TEMPLATES.values()]


def validate_template(tpl: dict) -> dict:
    """Validate a template DEFINITION supplied by configuration/API.

    Refuses anything that is not plain declarative data: no ``exec``-shaped
    keys, no embedded filter strings, and only the documented keys.
    """
    if not isinstance(tpl, dict):
        raise MotionTemplateError("template must be a mapping")
    allowed = {"key", "type", "label", "body", "sub_body", "position", "align",
               "default_duration", "style", "safe_zone", "description",
               "requires_known_metadata"}
    unknown = set(tpl) - allowed
    if unknown:
        raise MotionTemplateError(
            f"template: unknown key(s) {sorted(unknown)}; allowed {sorted(allowed)}"
        )
    forbidden = {"code", "exec", "script", "filter", "filtergraph", "command",
                 "template_code", "python"}
    present = forbidden & {str(k).lower() for k in tpl}
    if present:
        raise MotionTemplateError(
            f"template: executable keys are never allowed: {sorted(present)}"
        )
    type_ = str(tpl.get("type") or "").strip().upper()
    if type_ not in TEMPLATE_TYPES:
        raise MotionTemplateError(
            f"template: unknown type {type_!r}; allowed {list(TEMPLATE_TYPES)}"
        )
    body = str(tpl.get("body") or "")
    if not body.strip():
        raise MotionTemplateError("template: body must not be empty")
    duration = float(tpl.get("default_duration", 3.0))
    if duration <= 0 or duration > 120:
        raise MotionTemplateError(
            f"template: default_duration {duration}s out of range (0, 120]"
        )
    style = CaptionStyle.from_dict(tpl.get("style") or {})
    safe_zone = tpl.get("safe_zone") or {}
    if not isinstance(safe_zone, dict):
        raise MotionTemplateError("template: safe_zone must be a mapping")
    for edge in ("top", "right", "bottom", "left"):
        value = safe_zone.get(edge, 0.0)
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise MotionTemplateError(
                f"template.safe_zone.{edge}: {value!r} is not a number"
            ) from None
        if not 0.0 <= num <= 0.45:
            raise MotionTemplateError(
                f"template.safe_zone.{edge}: {num} outside 0..0.45"
            )
    return {
        "key": str(tpl.get("key") or type_.lower()),
        "type": type_,
        "label": str(tpl.get("label") or type_.title()),
        "body": body,
        "sub_body": str(tpl.get("sub_body") or ""),
        "position": str(tpl.get("position") or "bottom"),
        "align": str(tpl.get("align") or "left"),
        "default_duration": duration,
        "style": style.to_dict(),
        "safe_zone": {k: float(v) for k, v in safe_zone.items()},
        "description": str(tpl.get("description") or ""),
        "requires_known_metadata": bool(tpl.get("requires_known_metadata")),
    }


@dataclass
class MotionInstance:
    """A template bound to concrete values, ready for the timeline."""

    template_key: str
    bindings: dict = field(default_factory=dict)
    start: float = 0.0
    duration: float = 3.0
    clip_id: str = ""
    style_patch: dict = field(default_factory=dict)

    def to_clip(self) -> dict:
        """The canonical ``text`` clip this instance becomes."""
        tpl = get_template(self.template_key)
        rendered = _render(tpl.body, self.bindings)
        sub = _render(tpl.sub_body, self.bindings) if tpl.sub_body else ""
        content = f"{rendered}\n{sub}" if sub else rendered
        style = tpl.style
        if self.style_patch:
            style = style.patch(self.style_patch)
        return {
            "id": self.clip_id or f"motion_{self.template_key}",
            "name": content.split("\n")[0],
            "start": float(self.start),
            "duration": float(self.duration),
            "source": {},
            "effects": [],
            "source_start": 0.0,
            "volume": 1.0,
            "speed": 1.0,
            "fade_in": 0.0,
            "fade_out": 0.0,
            "transform": {},
            "text": {
                "content": content,
                "preset": tpl.type.lower(),
                **style.to_dict(),
            },
            "transition_in": "cut",
            "transition_out": "cut",
        }


def _render(body: str, bindings: dict) -> str:
    """Substitute ``{{slot}}`` placeholders.

    An unbound slot renders as an EMPTY string rather than leaving the literal
    ``{{name}}`` on screen -- combined with ``requires_known_metadata`` this is
    what stops a lower third from inventing a speaker's name.
    """
    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        value = bindings.get(key)
        return "" if value is None else str(value)

    return _PLACEHOLDER_RE.sub(_sub, body or "").strip()


def validate_instance(instance: dict, *, metadata_available: dict | None = None) -> MotionInstance:
    """Validate an instance and enforce the no-invented-metadata rule.

    When the template declares ``requires_known_metadata`` and the caller
    supplies ``metadata_available`` (what is actually known), any binding
    whose slot is absent from that mapping is refused -- a lower third may not
    assert a name, role, location or source that the system does not know.
    """
    if not isinstance(instance, dict):
        raise MotionTemplateError("instance must be a mapping")
    tpl = get_template(str(instance.get("template") or instance.get("template_key") or ""))
    bindings = instance.get("bindings") or {}
    if not isinstance(bindings, dict):
        raise MotionTemplateError("instance.bindings must be a mapping")
    bad = {k for k in bindings if str(k).lower() in _RESERVED_BINDINGS}
    if bad:
        raise MotionTemplateError(
            f"instance: reserved binding name(s) {sorted(bad)}"
        )
    unknown_slots = set(bindings) - set(tpl.slots)
    if unknown_slots:
        raise MotionTemplateError(
            f"instance: template {tpl.key!r} has no slot(s) {sorted(unknown_slots)}; "
            f"slots are {tpl.slots}"
        )
    if tpl.requires_known_metadata and metadata_available is not None:
        invented = {
            slot for slot, value in bindings.items()
            if str(value or "").strip() and slot not in metadata_available
        }
        if invented:
            raise MotionTemplateError(
                f"instance: {tpl.key} requires known metadata; no source for "
                f"{sorted(invented)} - refusing to invent it"
            )
    try:
        start = float(instance.get("start", 0.0))
        duration = float(instance.get("duration", tpl.default_duration))
    except (TypeError, ValueError):
        raise MotionTemplateError("instance: start/duration must be numbers") from None
    if start < 0:
        raise MotionTemplateError("instance: start must be >= 0")
    if duration <= 0 or duration > 600:
        raise MotionTemplateError(
            f"instance: duration {duration}s out of range (0, 600]"
        )
    return MotionInstance(
        template_key=tpl.key,
        bindings={k: v for k, v in bindings.items() if str(v or "").strip()},
        start=start,
        duration=duration,
        clip_id=str(instance.get("clip_id") or ""),
        style_patch=dict(instance.get("style") or {}),
    )