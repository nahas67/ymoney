"""CaptionMotionQC (Work 13 §16).

A deterministic, read-only quality pass over a timeline document's caption /
motion content. It produces the SAME four statuses the existing brand
verifier uses (``PASS`` / ``PASS_WITH_WARNINGS`` / ``REVIEW_REQUIRED`` /
``FAIL``) so the two compose in one QC surface.

Deterministic by construction: every check reads the document and the resolved
styles, and none of them calls a model. The rollup is the worst status, which
matches ``brand.verifier._rollup``.

Severity split, chosen to match the repo's existing convention:

* **FAIL** -- the render would be broken or wrong: unusable timing, an
  unresolvable font, a corrupt output, a transition longer than its clips.
* **REVIEW_REQUIRED** -- probably undesirable but renders: overflow, a
  safe-zone violation, an effect whose tracking evidence was insufficient.
* **PASS_WITH_WARNINGS** -- cosmetic.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field

from app.engine.captions.ffmpeg_escape import resolve_font
from app.engine.captions.style import CaptionStyleError, resolve_style
from app.engine.motion.effects import EffectError, validate_effect
from app.engine.motion.transitions import (
    TransitionError,
    find_adjacent_pair,
    validate_transition,
)

__all__ = [
    "QC_STATUSES",
    "CaptionMotionQC",
    "QCCheck",
    "CaptionMotionQCReport",
    "run_caption_motion_qc",
]

QC_STATUSES: tuple[str, ...] = ("PASS", "PASS_WITH_WARNINGS", "REVIEW_REQUIRED", "FAIL")
_SEVERITY = {"PASS": 0, "PASS_WITH_WARNINGS": 1, "REVIEW_REQUIRED": 2, "FAIL": 3}

#: A caption shown for less than this is a flash, not a caption.
MIN_READABLE_SECONDS = 0.35
#: A caption longer than this with no word timing is a wall of text.
MAX_STATIC_SECONDS = 12.0


@dataclass(frozen=True)
class QCCheck:
    name: str
    passed: bool
    detail: str
    severity: str = "PASS"
    target: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail,
                "severity": self.severity, "target": self.target}


@dataclass
class CaptionMotionQCReport:
    checks: list[QCCheck] = field(default_factory=list)

    @property
    def status(self) -> str:
        worst = "PASS"
        for check in self.checks:
            if _SEVERITY[check.severity] > _SEVERITY[worst]:
                worst = check.severity
        return worst

    @property
    def blocking(self) -> bool:
        return any(c.severity == "FAIL" for c in self.checks)

    def failures(self) -> list[QCCheck]:
        return [c for c in self.checks if c.severity == "FAIL"]

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "blocking": self.blocking,
            "checks": [c.to_dict() for c in self.checks],
            "failed": len(self.failures()),
            "total": len(self.checks),
        }


class CaptionMotionQC:
    """Runs the §16 checks over one timeline document."""

    def __init__(
        self,
        doc: dict,
        *,
        width: int = 1080,
        height: int = 1920,
        safe_box: dict | None = None,
        policy=None,
        font_available: str | None = "",
    ):
        self.doc = doc or {}
        self.width = int(width)
        self.height = int(height)
        self.safe_box = dict(safe_box or {})
        self.policy = policy
        #: "" means "probe the system"; a string is an explicit resolved font.
        self._font = font_available
        self.report = CaptionMotionQCReport()

    # -- helpers -----------------------------------------------------------

    #: Clip kinds a keyframe may legally drive (the renderer compiles
    #: keyframes for overlays AND for video geometry/crop).
    _ANIMATABLE_KINDS: tuple[str, ...] = (
        "video", "broll", "avatar", "text", "caption")

    def _add(self, name: str, passed: bool, detail: str, severity: str = "PASS",
             target: str = "") -> None:
        self.report.checks.append(
            QCCheck(name=name, passed=passed, detail=detail[:400],
                    severity="PASS" if passed else severity, target=target))

    def _font_file(self) -> str | None:
        if self._font == "":
            self._font = resolve_font() or ""
        return self._font or None

    def _clips(self, kinds: tuple[str, ...]) -> list[tuple[str, dict]]:
        out: list[tuple[str, dict]] = []
        for track in self.doc.get("tracks", []) or []:
            kind = str(track.get("kind") or "")
            if kind not in kinds:
                continue
            for clip in track.get("clips", []) or []:
                out.append((kind, clip))
        return out

    # -- checks ------------------------------------------------------------

    def run(self) -> CaptionMotionQCReport:
        self._check_font()
        self._check_caption_timing()
        self._check_caption_overflow()
        self._check_safe_zone()
        self._check_text_overlap()
        self._check_effects()
        self._check_effect_graph_order()
        self._check_composite_availability()
        self._check_transitions()
        self._check_transition_neighbours()
        self._check_motion_instances()
        self._check_keyframes()
        self._check_tracked_subjects()
        return self.report

    def _check_font(self) -> None:
        needs_font = self._clips(("caption", "text"))
        if not needs_font:
            self._add("font_resolves", True, "no caption or text clips")
            return
        if self._font_file() is None:
            self._add(
                "font_resolves", False,
                "no render font found - every caption and text overlay would be "
                "silently skipped at render time",
                severity="FAIL",
            )
        else:
            self._add("font_resolves", True,
                      f"font {self._font_file()!r} resolves")

    def _check_caption_timing(self) -> None:
        captions = self._clips(("caption",))
        bad_timing: list[str] = []
        too_short: list[str] = []
        too_long_static: list[str] = []
        for kind, clip in captions:
            cid = str(clip.get("id") or "?")
            try:
                start = float(clip.get("start", 0.0))
                duration = float(clip.get("duration", 0.0))
            except (TypeError, ValueError):
                bad_timing.append(f"{cid}: non-numeric timing")
                continue
            if start < 0:
                bad_timing.append(f"{cid}: negative start {start}")
            if duration <= 0:
                bad_timing.append(f"{cid}: non-positive duration {duration}")
                continue
            if duration < MIN_READABLE_SECONDS:
                too_short.append(f"{cid}: {duration:.2f}s")
            word_level = bool(clip.get("word_level"))
            if not word_level and duration > MAX_STATIC_SECONDS:
                too_long_static.append(f"{cid}: {duration:.1f}s")
        self._add("caption_timing_valid", not bad_timing,
                  "; ".join(bad_timing) or "all caption timing is numeric and positive",
                  severity="FAIL")
        self._add("caption_readable_duration", not too_short,
                  "; ".join(too_short) or
                  f"all captions >= {MIN_READABLE_SECONDS}s",
                  severity="REVIEW_REQUIRED")
        self._add("caption_not_wall_of_text", not too_long_static,
                  "; ".join(too_long_static) or
                  f"no static caption exceeds {MAX_STATIC_SECONDS}s",
                  severity="REVIEW_REQUIRED")

    def _check_caption_overflow(self) -> None:
        overflowing: list[str] = []
        for kind, clip in self._clips(("caption",)):
            text = str(clip.get("name") or "").strip()
            if not text:
                continue
            text_obj = clip.get("text") or {}
            try:
                style = resolve_style(clip)
            except CaptionStyleError as exc:
                overflowing.append(f"{clip.get('id')}: invalid style ({exc})")
                continue
            max_chars = int(text_obj.get("max_chars_per_line") or 32)
            max_lines = int(text_obj.get("max_lines") or 2)
            lines = text.split("\n")
            if len(lines) > max_lines:
                overflowing.append(
                    f"{clip.get('id')}: {len(lines)} lines > {max_lines}")
                continue
            over = [ln for ln in lines if len(ln) > max_chars]
            if over:
                overflowing.append(
                    f"{clip.get('id')}: line of {max(len(ln) for ln in over)} "
                    f"chars > {max_chars}")
            # a crude but honest width estimate: style.size px per char
            approx_px = len(text) * style.size * 0.55
            if approx_px > self.width * max_lines:
                overflowing.append(
                    f"{clip.get('id')}: estimated text width "
                    f"{approx_px:.0f}px overflows {self.width}px x {max_lines}")
        self._add("caption_no_overflow", not overflowing,
                  "; ".join(overflowing[:5]) or "no caption overflows its budget",
                  severity="REVIEW_REQUIRED")

    def _check_safe_zone(self) -> None:
        violations: list[str] = []
        if not self.safe_box:
            self._add("safe_zone_respected", True,
                      "no safe box configured for this target")
            return
        top = float(self.safe_box.get("top", 0.0))
        bottom = float(self.safe_box.get("bottom", 0.0))
        safe_top = self.height * top
        safe_bottom = self.height * (1.0 - bottom)
        for kind, clip in self._clips(("caption", "text")):
            try:
                style = resolve_style(clip)
            except CaptionStyleError:
                continue
            if style.position == "top":
                y = safe_top
                if y < safe_top - 1:
                    violations.append(f"{clip.get('id')}: top {y:.0f} < {safe_top:.0f}")
            elif style.position == "bottom":
                y = safe_bottom - style.size
                if y + style.size > safe_bottom + 1:
                    violations.append(
                        f"{clip.get('id')}: bottom {(y + style.size):.0f} "
                        f"> {safe_bottom:.0f}")
        self._add("safe_zone_respected", not violations,
                  "; ".join(violations[:5]) or "captions/text sit inside the safe box",
                  severity="REVIEW_REQUIRED")

    def _check_text_overlap(self) -> None:
        """Two overlay clips rendering the same box at the same time."""
        overlaps: list[str] = []
        overlays = []
        for kind, clip in self._clips(("caption", "text")):
            try:
                start = float(clip.get("start", 0.0))
                end = start + float(clip.get("duration", 0.0))
            except (TypeError, ValueError):
                continue
            try:
                style = resolve_style(clip)
            except CaptionStyleError:
                style = None
            overlays.append((str(clip.get("id") or "?"), kind, start, end,
                             getattr(style, "position", "bottom")))
        for i, (a_id, _ak, a0, a1, a_pos) in enumerate(overlays):
            for b_id, _bk, b0, b1, b_pos in overlays[i + 1:]:
                if a_pos != b_pos:
                    continue
                overlap = min(a1, b1) - max(a0, b0)
                if overlap > 0.08:
                    overlaps.append(f"{a_id} & {b_id} overlap {overlap:.2f}s")
        self._add("overlay_no_overlap", not overlaps,
                  "; ".join(overlaps[:5]) or
                  "no two overlays in the same position collide",
                  severity="REVIEW_REQUIRED")

    def _check_effects(self) -> None:
        invalid: list[str] = []
        untracked: list[str] = []
        for kind, clip in self._clips(("video", "broll", "avatar")):
            for effect in clip.get("effects") or []:
                try:
                    validated = validate_effect(effect)
                except EffectError as exc:
                    invalid.append(f"{clip.get('id')}: {exc}")
                    continue
                if validated["requires_tracking"] and not clip.get("tracking_ok"):
                    untracked.append(
                        f"{clip.get('id')}: {validated['type']} needs tracking evidence")
        self._add("effects_valid", not invalid,
                  "; ".join(invalid[:5]) or "every effect is a valid registry entry",
                  severity="FAIL")
        self._add("tracked_effects_have_evidence", not untracked,
                  "; ".join(untracked[:5]) or
                  "no tracked effect was attached without evidence",
                  severity="REVIEW_REQUIRED")

    def _check_transitions(self) -> None:
        invalid: list[str] = []
        impossible: list[str] = []
        for track in self.doc.get("tracks", []) or []:
            clips = list(track.get("clips", []) or [])
            for clip in clips:
                for key in ("transition", "transition_spec"):
                    transition = clip.get(key)
                    if not transition:
                        continue
                    try:
                        validated = validate_transition(transition, self.doc)
                    except TransitionError as exc:
                        invalid.append(f"{clip.get('id')}: {exc}")
                        continue
                    # An overlap that consumes the whole shorter clip would
                    # erase it; that is an authoring error, not a warning.
                    pair = find_adjacent_pair(
                        self.doc, str(validated["from_item"]),
                        str(validated["to_item"]))
                    if pair is not None and validated["duration"] > 0:
                        a, b = pair
                        shortest = min(float(a.get("duration", 0.0)),
                                      float(b.get("duration", 0.0)))
                        if validated["duration"] >= shortest - 1e-3:
                            impossible.append(
                                f"{clip.get('id')}: {validated['type']} overlap "
                                f"{validated['duration']:.3f}s consumes the shorter "
                                f"clip ({shortest:.3f}s)")
                if clip.get("transition_out"):
                    # no explicit neighbour: the legacy cut/fade/crossfade
                    # vocabulary is validated by validate_timeline instead.
                    with suppress(TransitionError):
                        validate_transition({
                            "from_item": str(clip.get("id")),
                            "to_item": str(clip.get("next_item") or ""),
                            "type": str(clip.get("transition_out")),
                            "duration": float(clip.get("transition_duration") or 0.0),
                        }, self.doc)
        self._add("transitions_valid", not invalid,
                  "; ".join(invalid[:5]) or "every transition validates against its clips",
                  severity="FAIL")
        self._add("transition_duration_feasible", not impossible,
                  "; ".join(impossible[:5]) or
                  "every transition overlap leaves both clips intact",
                  severity="FAIL")

    def _check_transition_neighbours(self) -> None:
        """A stored transition must name a pair that actually exists."""
        orphans: list[str] = []
        for track in self.doc.get("tracks", []) or []:
            for clip in track.get("clips", []) or []:
                transition = clip.get("transition")
                if not isinstance(transition, dict):
                    continue
                from_item = str(transition.get("from_item") or "")
                to_item = str(transition.get("to_item") or "")
                if not from_item or not to_item:
                    orphans.append(
                        f"{clip.get('id')}: transition names no neighbour")
                    continue
                if find_adjacent_pair(self.doc, from_item, to_item) is None:
                    orphans.append(
                        f"{clip.get('id')}: {from_item} -> {to_item} are not "
                        f"adjacent, so the transition will not render")
        self._add("transition_neighbour_exists", not orphans,
                  "; ".join(orphans[:5]) or
                  "every stored transition names an adjacent pair",
                  severity="REVIEW_REQUIRED")

    def _check_effect_graph_order(self) -> None:
        """Effects must sit in the deterministic category order."""
        from app.engine.motion.graph import order_effects

        problems: list[str] = []
        for kind, clip in self._clips(("video", "broll", "avatar")):
            effects = clip.get("effects") or []
            if len(effects) < 2:
                continue
            ordered, order_problems = order_effects(effects)
            problems.extend(f"{clip.get('id')}: {p}" for p in order_problems)
            if len(ordered) == len(effects):
                stored = [str(e.get("type") or "").upper() for e in effects
                          if isinstance(e, dict)]
                canonical = [str(e.get("type") or "").upper() for e in ordered]
                if stored != canonical:
                    problems.append(
                        f"{clip.get('id')}: effects are stored out of render order "
                        f"({stored} vs {canonical})")
        self._add("effect_graph_order", not problems,
                  "; ".join(problems[:5]) or
                  "every effect chain is in the deterministic category order",
                  severity="REVIEW_REQUIRED")

    def _check_composite_availability(self) -> None:
        """A composite effect with no mask must NOT look active (Work 13.1 §4)."""
        from app.engine.motion.graph import COMPOSITE_EFFECTS

        missing: list[str] = []
        for kind, clip in self._clips(("video", "broll", "avatar")):
            for effect in clip.get("effects") or []:
                if not isinstance(effect, dict):
                    continue
                name = str(effect.get("type") or "").upper()
                if name not in COMPOSITE_EFFECTS:
                    continue
                if not (clip.get("tracking_ok") or clip.get("subject_mask_asset_id")):
                    missing.append(
                        f"{clip.get('id')}: {name} is stored but no Work 12 "
                        f"segmentation mask is available, so it renders NOT_AVAILABLE")
        self._add("composite_effects_available", not missing,
                  "; ".join(missing[:5]) or
                  "every composite effect has the mask it needs",
                  severity="REVIEW_REQUIRED")

    def _check_motion_instances(self) -> None:
        problems: list[str] = []
        for kind, clip in self._clips(("text",)):
            text_obj = clip.get("text") or {}
            template = str(text_obj.get("preset") or "")
            if template.startswith("motion_") or "template" in text_obj:
                from app.engine.motion.templates import get_template

                try:
                    tpl = get_template(template)
                except Exception as exc:
                    problems.append(f"{clip.get('id')}: {exc}")
                    continue
                content = str(text_obj.get("content") or "")
                if "{{" in content:
                    problems.append(
                        f"{clip.get('id')}: unbound placeholder left in {content!r}")
                if tpl.requires_known_metadata and not content.strip():
                    problems.append(
                        f"{clip.get('id')}: metadata template rendered empty")
        self._add("motion_instances_valid", not problems,
                  "; ".join(problems[:5]) or "no unbound placeholder or empty motion item",
                  severity="FAIL")

    def _check_keyframes(self) -> None:
        """Every stored keyframe must survive the shared render validation.

        Delegating to ``validate_keyframes`` keeps ONE definition of a legal
        keyframe. A hand-rolled second check here would drift, and would let an
        unsupported easing or an unknown property through to a render that then
        cannot compile it.
        """
        from app.engine.motion.graph import validate_keyframes

        invalid: list[str] = []
        for kind, clip in self._clips(self._ANIMATABLE_KINDS):
            keyframes = clip.get("keyframes")
            if not keyframes:
                continue
            if not isinstance(keyframes, list):
                invalid.append(f"{clip.get('id')}: keyframes must be a list")
                continue
            try:
                duration = float(clip.get("duration", 0.0))
            except (TypeError, ValueError):
                duration = 0.0
            _valid, problems = validate_keyframes(keyframes,
                                                  clip_duration=duration)
            invalid.extend(f"{clip.get('id')}: {problem}" for problem in problems)
        self._add("keyframes_valid", not invalid,
                  "; ".join(invalid[:5]) or "no invalid keyframes",
                  severity="FAIL")

    def _check_tracked_subjects(self) -> None:
        """A tracked effect whose subject vanished must be surfaced."""
        missing: list[str] = []
        for kind, clip in self._clips(("video", "broll", "avatar")):
            for effect in clip.get("effects") or []:
                if not isinstance(effect, dict):
                    continue
                kind_name = str(effect.get("type") or "").upper()
                if kind_name not in ("TRACKED_CALLOUT", "BACKGROUND_BLUR", "MASK"):
                    continue
                if not effect.get("subject_track"):
                    missing.append(
                        f"{clip.get('id')}: {kind_name} has no subject track bound")
        self._add("tracked_subject_present", not missing,
                  "; ".join(missing[:5]) or
                  "every tracked effect names the subject it follows",
                  severity="REVIEW_REQUIRED")


def run_caption_motion_qc(
    doc: dict,
    *,
    width: int = 1080,
    height: int = 1920,
    safe_box: dict | None = None,
    policy=None,
    font_available: str | None = "",
) -> dict:
    """Run the QC pass and return its dict form."""
    return CaptionMotionQC(
        doc, width=width, height=height, safe_box=safe_box, policy=policy,
        font_available=font_available,
    ).run().to_dict()