"""Provider-independent localization pipeline (Work 07 Lane A).

Flow (one run = one target language)::

    Source -> Transcript -> Speakers -> Translation -> Cultural Adaptation
          -> TTS -> Timing -> Captions -> Graphics -> Metadata -> Timeline -> QC

Design rules:

- The source is NEVER mutated. Output lands on a NEW ContentItem child
  (derivation kind ``localized``, parent=root linked through
  :func:`app.engine.content_graph.derive_content`), a new ContentTimeline,
  Scene rows, MediaAsset lineage rows and a ``localized_contents`` row that
  carries ``source_content_id`` / ``language`` / ``locale`` /
  ``translation_version``.
- Providers are reached through module-level hooks (``translate_texts``,
  ``choose_voice``, ``synthesize_texts``) so tests can swap them; the product
  default is :mod:`app.providers.dubbing` (translate_segments / pick_voice /
  synthesize_segments) on top of :mod:`app.providers.tts`.
- Glossary terms are enforced deterministically BEFORE QC: mangled
  occurrences are rewritten, vanished brand/product terms are re-inserted
  (flagged as repairs). Literal (structure-faithful) and localized
  (culturally adapted) strings are kept separately — captions/voice use the
  localized one, QC sees the final text.
- Timing preserves the source cue windows verbatim and flags drift beyond the
  threshold instead of silently re-cutting the edit.
- Costs have no ``production_costs`` table in this repo (Lane B does not
  create one), so translation/TTS cost is recorded in ``lineage_json["costs"]``.
- Every stage honors ``jobs.check_cancelled`` when a job context is supplied,
  so a run is cancellable through the existing job cancellation path.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from app.engine import timeline as tl
from app.engine.localization import quality
from app.models import (
    ContentItem,
    ContentTimeline,
    GlossaryTerm,
    LocalizedContent,
    MediaAsset,
    Scene,
    VideoVariant,
)

#: stages in execution order (surfaced in lineage + API responses)
STAGES = (
    "source", "transcript", "speakers", "translation", "cultural_adaptation",
    "tts", "timing", "captions", "graphics", "metadata", "timeline", "qc",
)

#: localized text may run this far past its source cue window before QC flags it
TIMING_SOFT_DRIFT = 0.75  # seconds
#: visual tracks copied from the source so the editor keeps picture continuity
COPIED_TRACKS = ("video", "broll", "avatar")
#: metadata keys localized as prose (everything else is copied verbatim —
#: numbers/metrics are never invented or reworded)
META_TEXT_KEYS = frozenset({
    "title", "description", "hook", "angle", "cta", "summary",
    "thumbnail_text", "on_screen_text", "caption", "subtitle",
})
META_LIST_KEYS = frozenset({"hashtags", "keywords", "tags"})


class LocalizationError(Exception):
    """Deterministic pipeline failure (no transcript, bad source, ...)."""


# ---------------------------------------------------------------------------
# provider hooks — module level on purpose: tests swap them, product does not
# ---------------------------------------------------------------------------

def translate_texts(texts: list[str], target_lang: str, *, workspace_id: str = "",
                    glossary: dict[str, str] | None = None) -> list[str]:
    """Literal translation through the dubbing provider (LLM)."""
    from app.providers import dubbing

    return dubbing.translate_segments(texts, target_lang, workspace_id,
                                      glossary=dict(glossary) if glossary else None)


def choose_voice(target_lang: str, explicit: str = "") -> str:
    from app.providers import dubbing

    return dubbing.pick_voice(target_lang, explicit)


def synthesize_texts(texts: list[str], voice: str, work_dir: Path,
                     workspace_id: str = "") -> list[Path]:
    """Speak the localized cues. The workspace travels with the call (15.9 §2).

    TTS is billable, and the provider lane resolves its tenant from an explicit
    argument before falling back to an ambient context variable. The pipeline
    holds a canonical workspace and must not leave the speech leg to guess one:
    a wrong guess charges the wrong tenant, and no guess at all leaves the
    spend unowned.
    """
    from app.providers import dubbing

    return [Path(p) if p else Path("") for p
            in dubbing.synthesize_segments(
                texts, voice, work_dir, workspace_id=workspace_id or "")]


# ---------------------------------------------------------------------------
# glossary + text utilities (deterministic)
# ---------------------------------------------------------------------------

def _contains(text: str, needle: str, case_sensitive: bool = False) -> bool:
    if not needle:
        return True
    if case_sensitive:
        return needle in (text or "")
    return needle.casefold() in (text or "").casefold()


def _replace(text: str, needle: str, replacement: str, case_sensitive: bool) -> str:
    if not needle:
        return text
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.sub(re.escape(needle), lambda _m: replacement, text,
                  count=1, flags=flags)


def glossary_applies(entry: dict, language: str) -> bool:
    langs = [str(x) for x in (entry.get("target_languages") or [])]
    return not langs or language in langs


def load_glossary(db, workspace_id: str, language: str,
                  overlay: list[dict] | None = None) -> list[dict]:
    """Workspace DB terms (language-filtered) merged with a per-run overlay."""
    rows = db.scalars(select(GlossaryTerm).where(
        GlossaryTerm.workspace_id == workspace_id)).all()
    entries: list[dict] = []
    for row in rows:
        entry = {
            "term": row.term,
            "replacement": row.replacement or "",
            "target_languages": list(row.target_languages or []),
            "kind": row.kind or "terminology",
            "case_sensitive": bool(row.case_sensitive),
            "source": "workspace",
        }
        if glossary_applies(entry, language):
            entries.append(entry)
    for raw in overlay or []:
        term = str(raw.get("term") or "").strip()
        if not term:
            continue
        entry = {
            "term": term[:200],
            "replacement": str(raw.get("replacement") or "")[:200],
            "target_languages": [str(x) for x in (raw.get("target_languages") or [])],
            "kind": str(raw.get("kind") or "terminology"),
            "case_sensitive": bool(raw.get("case_sensitive")),
            "source": "overlay",
        }
        if glossary_applies(entry, language):
            entries.append(entry)
    return entries


def _dedupe_terms(entries: list[dict]) -> list[dict]:
    """Keep the FIRST entry per (case-insensitive) term.

    Used after brand folding: ``merge_brand_glossary`` replaces the first
    occurrence of a term in place, so dropping later duplicates leaves the
    brand value authoritative no matter which layer (workspace row vs
    operator overlay) also defined the term.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        term = str(entry.get("term") or "").strip().casefold()
        if not term or term in seen:
            continue
        seen.add(term)
        out.append(entry)
    return out


def brand_glossary_overlay(db, workspace_id: str,
                           overlay: list[dict] | None = None) -> list[dict]:
    """Operator overlay with the brand glossary folded in (never raises).

    Documented precedence per term: **brand > operator overlay > workspace
    glossary rows > nothing**. Brand vocabulary (preferred terms) and
    pronunciation rules become glossary rows so a translation can never drop
    a brand/product term. When the brand module or policy is unavailable the
    overlay is returned unchanged (``source == "overlay"`` rows untouched).
    """
    existing = [dict(e) for e in (overlay or [])]
    try:
        from app.engine.brand_templates import (  # noqa: PLC0415
            brand_gate,
            brand_glossary_entries,
            merge_brand_glossary,
        )

        gate = brand_gate(db, workspace_id)
        brand_rows = brand_glossary_entries(gate)
    except Exception:  # noqa: BLE001 — brand must never break localization
        return existing
    if not brand_rows:
        return existing
    return _dedupe_terms(merge_brand_glossary(existing, brand_rows))


def enforce_glossary(source_text: str, target_text: str,
                     entries: list[dict], language: str) -> tuple[str, list[dict]]:
    """Deterministic post-translation enforcement.

    A term present in the source and applicable to the target language must
    appear in the output as its replacement (or the term itself when no
    replacement is configured). Mangled occurrences are rewritten in place;
    brand/product terms that vanished entirely are re-inserted (flagged as a
    repair). Returns ``(text, events)``.
    """
    text = target_text or ""
    events: list[dict] = []
    for entry in entries:
        term = str(entry.get("term") or "")
        if not term or not glossary_applies(entry, language):
            continue
        if not _contains(source_text, term, bool(entry.get("case_sensitive"))):
            continue
        expected = str(entry.get("replacement") or "").strip() or term
        cs = bool(entry.get("case_sensitive"))
        if _contains(text, expected, cs):
            continue
        if str(entry.get("kind") or "") == "pronunciation":
            continue  # phonetic spelling is TTS-only; captions keep the term
        if _contains(text, term, cs):
            text = _replace(text, term, expected, cs)
            events.append({"term": term, "expected": expected,
                           "action": "replaced",
                           "kind": entry.get("kind") or "terminology"})
        elif str(entry.get("kind") or "") in ("brand", "product"):
            text = f"{text.rstrip()} {expected}".strip()
            events.append({"term": term, "expected": expected,
                           "action": "reinserted",
                           "kind": entry.get("kind") or "brand"})
        else:
            events.append({"term": term, "expected": expected,
                           "action": "missing",
                           "kind": entry.get("kind") or "terminology"})
    return text, events


def apply_pronunciation(text: str, entries: list[dict], language: str) -> str:
    """Phonetic spelling for TTS only (captions keep the real spelling)."""
    out = text or ""
    for entry in entries:
        if str(entry.get("kind") or "") != "pronunciation":
            continue
        if not glossary_applies(entry, language):
            continue
        term = str(entry.get("term") or "")
        replacement = str(entry.get("replacement") or "").strip()
        if term and replacement and _contains(out, term,
                                              bool(entry.get("case_sensitive"))):
            out = _replace(out, term, replacement,
                           bool(entry.get("case_sensitive")))
    return out


def adapt_culture(text: str, language: str) -> str:
    """Cultural adaptation of a literal translation (deterministic, no LLM).

    Whitespace normalization + CTA canonicalization: a CTA phrase that came
    through in another language is swapped for the target-language canonical
    phrasing of the SAME meaning (keyword map, no invention).
    """
    out = re.sub(r"\s+", " ", (text or "")).strip()
    if not out:
        return out
    hit = _find_cta_phrase(out)
    if not hit:
        return out
    category, phrase = hit
    target_kws = quality.CTA_KEYWORDS.get(category, {}).get(language) or []
    if not target_kws or quality.cta_category_present([out], category, language):
        return out
    canonical = target_kws[0]
    pattern = re.compile(re.escape(phrase), re.IGNORECASE)
    if pattern.search(out):
        return pattern.sub(canonical, out, count=1)
    return f"{out} {canonical}".strip()


def _find_cta_phrase(text: str) -> tuple[str, str] | None:
    """Longest CTA keyword present in the text → (category, matched phrase)."""
    blob = (text or "").casefold()
    best: tuple[str, str] | None = None
    for category, by_lang in quality.CTA_KEYWORDS.items():
        for keywords in by_lang.values():
            for kw in keywords:
                kw_fold = kw.casefold()
                if kw_fold in blob and (best is None or len(kw) > len(best[1])):
                    best = (category, kw)
    return best


def localize_hashtag(tag: str, translated: str) -> str:
    """Localize a hashtag only when the translation stays tag-shaped."""
    raw = (translated or "").strip().lstrip("#")
    if not raw:
        return tag
    words = raw.split()
    if not words or len(words) > 3 or any(len(w) > 24 for w in words):
        return tag  # prose, not a tag: keep the original for discoverability
    compact = "".join(re.sub(r"[^\w]", "", w, flags=re.IGNORECASE) for w in words)
    if not compact:
        return tag
    return "#" + compact.lower()


def _accepts_kwarg(fn, name: str) -> bool:
    """Whether a module-level hook takes ``name``. Signature, not guesswork.

    A test double written against the pre-15.9 hook signatures must keep
    working, and a ``TypeError`` fallback would be wrong: it would also swallow
    a ``TypeError`` raised *inside* a hook that does accept the argument. So the
    decision is made from the signature. Anything uninspectable (a C callable)
    is given the argument, because the modern signature is the default.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _call_with_workspace(fn, args: list, workspace_id: str = ""):
    """Call a pipeline hook with the canonical workspace when it takes one."""
    if _accepts_kwarg(fn, "workspace_id"):
        return fn(*args, workspace_id=workspace_id or "")
    logger.warning(
        "[localization] hook %s takes no workspace_id; its billable legs are "
        "unowned", getattr(fn, "__name__", repr(fn)))
    return fn(*args)


def localize_metadata(meta: dict, language: str,
                      translate_fn, workspace_id: str = "") -> tuple[dict, int]:
    """Translate prose fields of a metadata dict; copy everything else verbatim.

    Returns ``(localized_metadata, changed_string_count)``. Numbers, booleans
    and unknown keys pass through untouched — metrics are never invented.

    ``workspace_id`` is required for a billable ``translate_fn`` (Work 15.9 §2).
    This used to hardcode ``workspace_id=""`` here while every other stage in
    this same file passed ``self.row.workspace_id``, which made the metadata
    leg the one ownerless spend in the run: the request went out, the tenant's
    cap never saw it, and nothing on the ledger said who paid. The caller owns a
    canonical ``localized_contents`` row that already carries the workspace, so
    the id travels with the call rather than being re-derived here.
    """
    text_values: list[str] = []
    list_values: list[str] = []

    def _walk(obj, active_list: bool) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                is_list_key = key.casefold() in META_LIST_KEYS
                if isinstance(value, str):
                    if is_list_key:
                        list_values.append(value)
                    elif key.casefold() in META_TEXT_KEYS:
                        text_values.append(value)
                elif isinstance(value, (dict, list)):
                    _walk(value, is_list_key or active_list)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, str):
                    if active_list:
                        list_values.append(item)
                else:
                    _walk(item, active_list)

    _walk(meta, False)
    ordered = list(dict.fromkeys([v for v in text_values + list_values if v]))
    translated = list(ordered)
    if ordered:
        try:
            result = translate_fn(list(ordered), language,
                                  workspace_id=workspace_id or "",
                                  glossary=None)
            if isinstance(result, list) and len(result) == len(ordered):
                translated = [str(r or o) for r, o in zip(result, ordered, strict=False)]
        except Exception as exc:  # metadata must never fail the run
            logger.warning(f"[localization] metadata translation degraded: {exc}")
    mapping = dict(zip(ordered, translated, strict=False))
    changed = sum(1 for o, t in zip(ordered, translated, strict=False) if t != o)

    def _apply(obj, active_list: bool):
        if isinstance(obj, dict):
            out = {}
            for key, value in obj.items():
                is_list_key = key.casefold() in META_LIST_KEYS
                if isinstance(value, str) and not is_list_key \
                        and key.casefold() in META_TEXT_KEYS:
                    out[key] = mapping.get(value, value)
                elif isinstance(value, list):
                    out[key] = _apply(value, is_list_key)
                elif isinstance(value, dict):
                    out[key] = _apply(value, active_list)
                else:
                    out[key] = value  # numbers/bools/unknown: verbatim
            return out
        if isinstance(obj, list):
            out_list = []
            for item in obj:
                if isinstance(item, str):
                    if not active_list:
                        out_list.append(item)
                        continue
                    new = mapping.get(item, item)
                    if item.startswith("#"):
                        out_list.append(localize_hashtag(item, new))
                    else:
                        out_list.append(new)
                else:
                    out_list.append(_apply(item, active_list))
            return out_list
        return obj

    localized = _apply(meta, False)
    return (localized if isinstance(localized, dict) else {}), changed


# ---------------------------------------------------------------------------
# source bundle
# ---------------------------------------------------------------------------

@dataclass
class Cue:
    index: int
    start: float
    end: float
    text: str
    speaker: str = ""
    scene_id: str = ""
    origin: str = "scene"  # scene | caption


@dataclass
class SourceBundle:
    content: ContentItem
    doc: dict
    scenes: list
    duration: float
    aspect: str
    fps: float
    variants: list = field(default_factory=list)


def _load_source(db, workspace_id: str, source_content_id: str) -> SourceBundle:
    content = db.get(ContentItem, source_content_id)
    if content is None or content.workspace_id != workspace_id:
        raise LocalizationError(
            f"source content '{source_content_id}' not found in this workspace")
    timeline = db.scalars(
        select(ContentTimeline)
        .where(ContentTimeline.workspace_id == workspace_id,
               ContentTimeline.content_item_id == source_content_id)
        .order_by(ContentTimeline.updated_at.desc())
    ).first()
    doc = dict(timeline.tracks_json or {}) if timeline else {}
    scenes = list(db.scalars(
        select(Scene).where(Scene.workspace_id == workspace_id,
                            Scene.content_item_id == source_content_id)
        .order_by(Scene.index)
    ).all())
    variants = list(db.scalars(
        select(VideoVariant).where(VideoVariant.content_item_id == source_content_id)
    ).all())
    duration = float(doc.get("duration_seconds") or 0.0)
    if not duration and scenes:
        duration = max(float(s.end_seconds or 0.0) for s in scenes)
    if not duration:
        duration = float(tl.FALLBACK_DURATION_SECONDS)
    return SourceBundle(
        content=content,
        doc=doc,
        scenes=scenes,
        duration=duration,
        aspect=str(doc.get("aspect_ratio") or "9:16"),
        fps=float(doc.get("fps") or 30.0),
        variants=variants,
    )


def _cues_from_source(bundle: SourceBundle) -> list[Cue]:
    cues: list[Cue] = []
    if bundle.scenes:
        strategy_speakers: list[str] = []
        raw_speakers = (bundle.content.strategy_json or {}).get("speakers")
        if isinstance(raw_speakers, list):
            strategy_speakers = [str(s) for s in raw_speakers if s]
        for i, sc in enumerate(bundle.scenes):
            text = (sc.narration or "").strip() or (sc.script_segment or "").strip()
            start = float(sc.start_seconds or 0.0)
            end = float(sc.end_seconds or 0.0)
            if not text or end <= start:
                continue
            speaker = (strategy_speakers[i % len(strategy_speakers)]
                       if strategy_speakers else f"scene_{i}")
            cues.append(Cue(index=len(cues) + 1, start=start, end=end, text=text,
                            speaker=speaker, scene_id=sc.id, origin="scene"))
    if not cues:
        for tr in bundle.doc.get("tracks", []):
            if tr.get("kind") != "caption":
                continue
            for clip in tr.get("clips", []):
                text_obj = clip.get("text") or {}
                text = (text_obj.get("content") or clip.get("name") or "").strip()
                start = float(clip.get("start") or 0.0)
                duration = float(clip.get("duration") or 0.0)
                if not text or duration <= 0:
                    continue
                cues.append(Cue(index=len(cues) + 1, start=start,
                                end=start + duration, text=text,
                                speaker=f"scene_{len(cues)}", origin="caption"))
    cues.sort(key=lambda c: c.start)
    for i, cue in enumerate(cues, 1):
        cue.index = i
    if not cues:
        raise LocalizationError(
            "no transcript: source has neither scene narration nor a caption track")
    return cues


def _tile(cues: list[Cue]) -> list[tuple[float, float]]:
    """Non-overlapping, positive windows derived from the source cue windows."""
    out: list[tuple[float, float]] = []
    cursor = 0.0
    for cue in cues:
        start = max(cue.start, cursor)
        end = max(cue.end, start)
        duration = end - start
        if duration <= 0:
            out.append((start, 0.0))
            continue
        out.append((round(start, 3), round(duration, 3)))
        cursor = start + duration
    return out


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

class LocalizationPipeline:
    """Runs one ``localized_contents`` row end to end."""

    STAGES = STAGES

    def __init__(self, db, *, localized_content_id: str, workspace_id: str,
                 job_ctx=None, synthesize_audio: bool = True,
                 translate_fn=None, voice_fn=None, tts_fn=None):
        self.db = db
        self.job_ctx = job_ctx
        self.synthesize_audio = bool(synthesize_audio)
        self.translate_fn = translate_fn or translate_texts
        self.voice_fn = voice_fn or choose_voice
        self.tts_fn = tts_fn or synthesize_texts
        row = db.get(LocalizedContent, localized_content_id)
        if row is None or row.workspace_id != workspace_id:
            raise LocalizationError(
                f"localization '{localized_content_id}' not found in this workspace")
        self.row = row
        self.language = row.language
        self.stage_log: list[dict] = []
        self.warnings: list[str] = []
        self.repairs: list[dict] = []
        self.costs: dict = {
            "mode": "lineage_json_stub",
            "translation_usd": 0.0,
            "tts_usd": 0.0,
            "note": ("no production_costs table exists in this deployment; "
                     "translation/TTS cost is tracked in lineage_json"),
            "translation_chars": 0,
            "audio_files": 0,
        }

    # -- infrastructure ---------------------------------------------------

    def _check(self) -> None:
        if self.job_ctx is not None:
            from app.services import jobs as jobs_service

            jobs_service.check_cancelled(self.job_ctx)

    def _stage(self, name: str, detail: str = "", status: str = "ok") -> None:
        self._check()
        self.stage_log.append({"stage": name, "status": status, "detail": detail})

    # -- stages -----------------------------------------------------------

    def run(self) -> dict:
        """Execute every stage; commits durable state at stage boundaries."""
        row, db = self.row, self.db
        row.status = "RUNNING"
        row.error = ""
        db.commit()
        try:
            result = self._execute()
        except Exception as exc:
            from app.services import jobs as jobs_service

            db.rollback()
            if isinstance(exc, jobs_service._Cancelled):
                row.status = "CANCELLED"
            else:
                row.status = "FAILED"
                row.error = str(exc)[:2000]
            self._flush_lineage_on_failure()
            db.commit()
            raise
        row.status = "READY"
        row.error = ""
        db.commit()
        db.refresh(row)
        return result

    def _flush_lineage_on_failure(self) -> None:
        try:
            lineage = dict(self.row.lineage_json or {})
            lineage["stages"] = list(self.stage_log)
            lineage["warnings"] = list(self.warnings)
            lineage["costs"] = dict(self.costs)
            self.row.lineage_json = lineage
        except Exception:  # pragma: no cover - best-effort diagnostics
            logger.warning("[localization] could not persist failure lineage")

    def _execute(self) -> dict:
        lang = self.language
        db = self.db
        lineage = dict(self.row.lineage_json or {})
        overlay = lineage.get("glossary_overlay") or []
        voice_prefs = lineage.get("voice_prefs") or {}
        source_language = str(lineage.get("source_language") or "en")

        # 1. source -------------------------------------------------------
        bundle = _load_source(db, self.row.workspace_id, self.row.source_content_id)
        self._stage("source", f"content={bundle.content.id} "
                              f"duration={bundle.duration:.1f}s")

        # 2. transcript ---------------------------------------------------
        cues = _cues_from_source(bundle)
        self._stage("transcript", f"{len(cues)} cue(s) from {cues[0].origin}")

        # 3. speakers -----------------------------------------------------
        speakers = sorted({c.speaker for c in cues if c.speaker})
        self._stage("speakers", ", ".join(speakers[:8]) or "none")

        # 4. translation --------------------------------------------------
        # Brand glossary (Work 08 Lane C): brand vocabulary / pronunciation
        # rules fold into this run's overlay — precedence per term is
        # brand > operator overlay > workspace glossary rows > nothing.
        overlay = brand_glossary_overlay(db, self.row.workspace_id, overlay)
        entries = load_glossary(db, self.row.workspace_id, lang, overlay)
        gloss_map = {e["term"]: (e["replacement"] or e["term"]) for e in entries}
        raw = self.translate_fn([c.text for c in cues], lang,
                                workspace_id=self.row.workspace_id,
                                glossary=gloss_map or None)
        if not isinstance(raw, list):
            raw = []
        raw = [str(x) if x is not None else "" for x in raw]
        if len(raw) < len(cues):
            raw = raw + [""] * (len(cues) - len(raw))
        raw = raw[:len(cues)]
        literal: list[str] = []
        for cue, text in zip(cues, raw, strict=False):
            fixed, events = enforce_glossary(cue.text, text, entries, lang)
            literal.append(fixed)
            self.repairs.extend({**e, "cue": cue.index} for e in events)
        self.costs["translation_chars"] = sum(len(t) for t in literal)
        self._stage("translation",
                    f"{len(literal)} literal line(s), "
                    f"{sum(1 for e in self.repairs)} glossary repair(s)")

        # 5. cultural adaptation ------------------------------------------
        localized = [adapt_culture(t, lang) for t in literal]
        self._stage("cultural_adaptation",
                    f"{sum(1 for a, b in zip(literal, localized, strict=False) if a != b)} "
                    f"line(s) adapted")

        # 6. TTS ----------------------------------------------------------
        voice_texts = [apply_pronunciation(t, entries, lang) for t in localized]
        voice_plan = self._resolve_voices(voice_prefs, speakers, lang)
        audio = self._synthesize(voice_texts, voice_plan, lang)
        self._stage("tts",
                    f"voice={audio['voice']} files={audio['files']}"
                    + (f" degraded={len(audio['issues'])}" if audio["issues"] else ""))
        self.warnings.extend(audio["issues"])

        # 7. timing -------------------------------------------------------
        timing = self._timing(cues, localized)
        self._stage("timing",
                    f"worst drift {timing['worst_drift_seconds']:+.2f}s, "
                    f"{len(timing['flagged'])} cue(s) beyond threshold")

        # 8. captions -----------------------------------------------------
        windows = _tile(cues)
        caption_clips, srt_text = self._captions(cues, localized, windows)
        self._stage("captions", f"{len(caption_clips)} caption clip(s)")

        # 9. graphics (on-screen text) ------------------------------------
        graphics_clips = self._graphics(bundle, lang)
        self._stage("graphics", f"{len(graphics_clips)} on-screen text clip(s)")

        # 10. metadata ----------------------------------------------------
        metadata, meta_translated = self._metadata(bundle, lang)
        self._stage("metadata", f"{meta_translated} field(s) localized")

        # 11. timeline ----------------------------------------------------
        artifacts = self._persist_timeline(
            bundle, cues, windows, localized, caption_clips, graphics_clips,
            audio, voice_plan, metadata, srt_text, timing, speakers)
        self._stage("timeline",
                    f"timeline={artifacts['timeline_id']} "
                    f"child={artifacts['child_content_id']}")

        # 12. QC ----------------------------------------------------------
        report = self._qc(bundle, cues, localized, entries, speakers,
                          artifacts["doc"], timing, source_language)
        self._stage("qc", f"status={report['status']}")

        lineage.update({
            "source_content_id": self.row.source_content_id,
            "root_content_id": bundle.content.root_content_id or bundle.content.id,
            "child_content_id": artifacts["child_content_id"],
            "timeline_id": artifacts["timeline_id"],
            "language": lang,
            "locale": self.row.locale or "",
            "translation_version": int(self.row.translation_version or 1),
            "source_language": source_language,
            # literal (structure-faithful) vs localized (culturally adapted):
            # captions/voice/QC use `localized`, `literal` is kept for audit
            "literal_texts": list(literal),
            "localized_texts": list(localized),
            "stages": list(self.stage_log),
            "warnings": list(self.warnings),
            "repairs": list(self.repairs),
            "speakers": speakers,
            "voice_plan": voice_plan,
            "timing": {k: v for k, v in timing.items() if k != "cues"},
            "glossary": entries,
            "costs": dict(self.costs),
            "qc_status": report["status"],
        })
        self.row.lineage_json = lineage
        self.row.child_content_id = artifacts["child_content_id"]
        self.row.timeline_id = artifacts["timeline_id"]
        db.commit()

        return {
            "localized_content_id": self.row.id,
            "language": lang,
            "status": "READY",
            "qc_status": report["status"],
            "timeline_id": artifacts["timeline_id"],
            "child_content_id": artifacts["child_content_id"],
            "segments": len(cues),
            "stages": list(self.stage_log),
            "warnings": list(self.warnings),
        }

    # -- stage implementations -------------------------------------------

    def _resolve_voices(self, voice_prefs, speakers: list[str], lang: str) -> dict:
        """speaker → voice id (per-language default, per-speaker overrides)."""
        pref = (voice_prefs or {}).get(lang)
        plan: dict[str, str] = {}
        for speaker in (speakers or ["default"]):
            explicit = ""
            if isinstance(pref, dict):
                explicit = str(pref.get(speaker) or pref.get("default") or "")
            elif isinstance(pref, str):
                explicit = pref
            plan[speaker] = self.voice_fn(lang, explicit) or ""
        return plan

    def _synthesize(self, texts: list[str], voice_plan: dict, lang: str) -> dict:
        """Per-cue audio; honest degradation when the TTS layer is unavailable."""
        primary = next(iter(voice_plan.values()), "") if voice_plan else ""
        out = {"voice": primary, "files": 0, "paths": [""] * len(texts),
               "issues": []}
        if not self.synthesize_audio or not any(t.strip() for t in texts):
            out["issues"].append("audio synthesis skipped (plan-only run)")
            return out
        from app.services.storage import STORAGE_ROOT

        work_dir = (STORAGE_ROOT / self.row.workspace_id / "localization"
                    / f"{self.row.id}_{lang}")
        try:
            paths = _call_with_workspace(
                self.tts_fn, [list(texts), primary, work_dir],
                workspace_id=self.row.workspace_id)
        except Exception as exc:
            out["issues"].append(f"TTS degraded: {type(exc).__name__}: {exc}"[:300])
            return out
        paths = [Path(p) if p else Path("") for p in (paths or [])]
        if len(paths) < len(texts):
            paths = paths + [Path("")] * (len(texts) - len(paths))
        out["paths"] = [str(p) if p and Path(p).exists() else ""
                        for p in paths[:len(texts)]]
        out["files"] = sum(1 for p in out["paths"] if p)
        self.costs["audio_files"] = out["files"]
        if not out["files"]:
            out["issues"].append("TTS produced no audio files")
        return out

    def _timing(self, cues: list[Cue], localized: list[str]) -> dict:
        rows: list[dict] = []
        worst = 0.0
        flagged: list[int] = []
        total_est, total_window = 0.0, 0.0
        for cue, text in zip(cues, localized, strict=False):
            window = max(0.0, cue.end - cue.start)
            est = quality.estimate_seconds(text)
            drift = round(est - window, 3)
            threshold = max(TIMING_SOFT_DRIFT, window * 0.2)
            is_flagged = abs(drift) > threshold
            if is_flagged:
                flagged.append(cue.index)
            if abs(drift) > abs(worst):
                worst = drift
            total_est += est
            total_window += window
            rows.append({"cue": cue.index, "window_seconds": round(window, 3),
                         "estimated_seconds": est, "drift_seconds": drift,
                         "flagged": is_flagged})
        return {
            "cues": rows,
            "worst_drift_seconds": round(worst, 3),
            "flagged": flagged,
            "threshold_seconds": TIMING_SOFT_DRIFT,
            "estimated_duration_seconds": round(total_est, 3),
            "source_window_seconds": round(total_window, 3),
        }

    def _captions(self, cues: list[Cue], localized: list[str],
                  windows: list[tuple[float, float]]) -> tuple[list[dict], str]:
        """Caption clips preserve the SOURCE cue windows (tile-repaired)."""
        clips: list[dict] = []
        for i, (cue, text, (start, duration)) in enumerate(
                zip(cues, localized, windows, strict=False), 1):
            if duration <= 0:
                self.warnings.append(f"cue {cue.index} dropped (empty window)")
                continue
            clips.append({"id": f"cap_{i}", "name": f"cap_{i}",
                          "start": start, "duration": duration, "text": text,
                          "cue": cue.index, "source_start": round(cue.start, 3)})
        from app.providers.dubbing import SrtCue, format_srt

        srt = format_srt([SrtCue(index=c["cue"], start=c["start"],
                                 end=c["start"] + c["duration"], text=c["text"])
                          for c in clips])
        return clips, srt

    def _graphics(self, bundle: SourceBundle, lang: str) -> list[dict]:
        """Localize on-screen text clips; windows are preserved."""
        raw: list[dict] = []
        for tr in bundle.doc.get("tracks", []):
            if tr.get("kind") != "text":
                continue
            for clip in tr.get("clips", []):
                text_obj = clip.get("text") or {}
                text = (text_obj.get("content") or clip.get("name") or "").strip()
                if text:
                    raw.append({"clip": clip, "text": text})
        if not raw:
            return []
        try:
            translated = self.translate_fn([r["text"] for r in raw], lang,
                                           workspace_id=self.row.workspace_id,
                                           glossary=None)
        except Exception as exc:
            self.warnings.append(f"on-screen text translation degraded: {exc}"[:200])
            translated = []
        out: list[dict] = []
        for i, item in enumerate(raw):
            text = str(translated[i]) if i < len(translated) and translated[i] \
                else item["text"]
            clip = item["clip"]
            start = float(clip.get("start") or 0.0)
            duration = float(clip.get("duration") or 0.0)
            if duration <= 0:
                continue
            out.append({"id": f"gfx_{i + 1}",
                        "name": clip.get("name") or f"gfx_{i + 1}",
                        "start": start, "duration": duration, "text": text,
                        "source_id": clip.get("id")})
        return out

    def _metadata(self, bundle: SourceBundle, lang: str) -> tuple[dict, int]:
        strategy = dict(bundle.content.strategy_json or {})
        meta: dict = {
            "strategy": strategy,
            "variants": [dict(v.metadata_json or {}) for v in bundle.variants],
        }
        return localize_metadata(meta, lang, self.translate_fn,
                                 workspace_id=self.row.workspace_id)

    # -- persistence ------------------------------------------------------

    def _persist_timeline(self, bundle, cues, windows, localized, caption_clips,
                          graphics_clips, audio, voice_plan, metadata,
                          srt_text, timing, speakers) -> dict:
        from app.engine.content_graph import derive_content
        from app.models import ContentTimeline as TimelineRow

        db, ws, lang = self.db, self.row.workspace_id, self.language
        child = derive_content(
            db, parent_id=bundle.content.id, workspace_id=ws,
            derivation_type="localized",
            topic=f"{bundle.content.topic} [{lang}]"[:400])

        strategy = dict(bundle.content.strategy_json or {})
        strategy["localization"] = {
            "source_content_id": bundle.content.id,
            "language": lang,
            "locale": self.row.locale or "",
            "translation_version": int(self.row.translation_version or 1),
            "localized_content_id": self.row.id,
        }
        strategy["localized_metadata"] = metadata
        child.strategy_json = strategy

        doc = tl.create_empty(ws, duration_seconds=bundle.duration,
                              fps=bundle.fps, aspect=bundle.aspect)

        # visual continuity: copy picture tracks from the source untouched
        for tr in bundle.doc.get("tracks", []):
            kind = tr.get("kind")
            if kind not in COPIED_TRACKS:
                continue
            for clip in tr.get("clips", []):
                try:
                    tl.add_clip(
                        doc, track=kind, clip_id=str(clip.get("id")),
                        name=str(clip.get("name") or clip.get("id")),
                        start=float(clip.get("start") or 0.0),
                        duration=float(clip.get("duration") or 0.0),
                        source=clip.get("source") or {},
                        effects=list(clip.get("effects") or []),
                        source_start=float(clip.get("source_start") or 0.0),
                        volume=float(clip.get("volume", 1.0)),
                        speed=float(clip.get("speed", 1.0)),
                        fade_in=float(clip.get("fade_in") or 0.0),
                        fade_out=float(clip.get("fade_out") or 0.0),
                        transform=clip.get("transform") or None,
                        text=clip.get("text") or None,
                        transition_in=str(clip.get("transition_in") or "cut"),
                        transition_out=str(clip.get("transition_out") or "cut"),
                    )
                except tl.TimelineValidationError as exc:
                    raise LocalizationError(f"source track copy invalid: {exc}") from exc

        # localized voice track (source windows, per-speaker voices)
        for i, (cue, text, (start, duration)) in enumerate(
                zip(cues, localized, windows, strict=False), 1):
            if duration <= 0:
                continue
            voice = voice_plan.get(cue.speaker) or audio["voice"]
            path = audio["paths"][i - 1] if i - 1 < len(audio["paths"]) else ""
            tl.add_clip(
                doc, track="voice", clip_id=f"v_{i}", name=f"voice {i}",
                start=start, duration=duration,
                source={"lang": lang, "voice": voice, "speaker": cue.speaker,
                        "estimated_seconds": quality.estimate_seconds(text),
                        "audio_path": path or "", "planned": not bool(path),
                        "cue": cue.index},
                speed=1.0, volume=1.0)

        # captions (source windows) + graphics (on-screen text)
        for clip in caption_clips:
            tl.add_clip(doc, track="caption", clip_id=clip["id"],
                        name=clip["name"], start=clip["start"],
                        duration=clip["duration"],
                        source={"cue": clip["cue"], "lang": lang,
                                "source_start": clip["source_start"]},
                        text={"content": clip["text"]})
        cursor = 0.0
        for clip in graphics_clips:
            start = max(clip["start"], cursor)
            duration = clip["duration"]
            if start + duration <= start:
                continue
            tl.add_clip(doc, track="text", clip_id=clip["id"], name=clip["name"],
                        start=start, duration=duration,
                        source={"lang": lang, "source_id": clip.get("source_id")},
                        text={"content": clip["text"]})
            cursor = start + duration

        try:
            tl.validate_timeline(doc)
        except tl.TimelineValidationError as exc:
            raise LocalizationError(
                f"localized timeline failed validation: {exc}") from exc

        row_obj = TimelineRow(
            workspace_id=ws, content_item_id=child.id,
            name=f"main-{lang}"[:200], fps=bundle.fps,
            duration_seconds=float(doc.get("duration_seconds") or bundle.duration),
            tracks_json=doc, version=1)
        db.add(row_obj)
        db.flush()

        # scenes: one row per cue, localized narration for the editor
        for i, (cue, text, (start, duration)) in enumerate(
                zip(cues, localized, windows, strict=False)):
            if duration <= 0:
                continue
            db.add(Scene(
                workspace_id=ws, content_item_id=child.id,
                timeline_id=row_obj.id, index=i,
                title=(cue.speaker or f"scene {i}")[:200],
                script_segment=cue.text, narration=text,
                visual_intent="localized from source scene" if cue.scene_id else "",
                start_seconds=start, end_seconds=start + duration,
                assets_json=([{"audio_path": audio["paths"][i]}]
                             if i < len(audio["paths"]) and audio["paths"][i]
                             else []),
                captions_json=[{"start": start, "end": start + duration,
                                "text": text}],
                parent_scene_id=cue.scene_id or None,
            ))

        asset_ids: list[str] = []
        subtitle_id = self._save_subtitle_asset(srt_text, lang, row_obj.id, child.id)
        if subtitle_id:
            asset_ids.append(subtitle_id)
        for i, path in enumerate(audio["paths"]):
            if path:
                aid = self._save_voice_asset(path, lang,
                                             cues[i] if i < len(cues) else None)
                if aid:
                    asset_ids.append(aid)
        db.flush()
        return {"child_content_id": child.id, "timeline_id": row_obj.id,
                "doc": doc, "asset_ids": asset_ids}

    def _save_subtitle_asset(self, srt_text: str, lang: str, timeline_id: str,
                             child_id: str) -> str | None:
        if not srt_text.strip():
            return None
        from app.services.storage import STORAGE_ROOT

        name = f"localization_{self.row.id[:8]}_{lang}.srt"
        meta = {"lang": lang, "timeline_id": timeline_id,
                "child_content_id": child_id,
                "localized_content_id": self.row.id}
        try:
            dest = STORAGE_ROOT / self.row.workspace_id
            dest.mkdir(parents=True, exist_ok=True)
            (dest / name).write_text(srt_text, encoding="utf-8")
            key = name
        except OSError as exc:
            self.warnings.append(f"subtitle file not persisted: {exc}"[:200])
            meta["inline_srt"] = srt_text[:20000]
            key = ""
        asset = MediaAsset(
            workspace_id=self.row.workspace_id, type="subtitle",
            origin="generated", provider="localization", storage_key=key,
            mime_type="application/x-subrip", meta_json=meta)
        self.db.add(asset)
        self.db.flush()
        return asset.id

    def _save_voice_asset(self, path: str, lang: str, cue) -> str | None:
        p = Path(path)
        if not p.exists():
            return None
        from app.services.storage import STORAGE_ROOT

        try:
            storage_key = str(p.resolve().relative_to(STORAGE_ROOT.resolve()))
        except ValueError:
            storage_key = str(p)
        asset = MediaAsset(
            workspace_id=self.row.workspace_id, type="voice",
            origin="generated", provider="tts", storage_key=storage_key,
            mime_type="audio/wav" if p.suffix.lower() == ".wav" else "audio/mpeg",
            file_size=p.stat().st_size,
            duration_seconds=quality.estimate_seconds(getattr(cue, "text", "") or ""),
            meta_json={"lang": lang, "localized_content_id": self.row.id,
                       "cue": getattr(cue, "index", None),
                       "speaker": getattr(cue, "speaker", "")})
        self.db.add(asset)
        self.db.flush()
        return asset.id

    def _qc(self, bundle, cues, localized, entries, speakers, doc, timing,
            source_language: str) -> dict:
        from app.models import LocalizationQCReport

        target_cues = [{"index": cue.index, "start": cue.start, "end": cue.end,
                        "text": text, "speaker": cue.speaker}
                       for cue, text in zip(cues, localized, strict=False)]
        target_speakers = sorted({c["speaker"] for c in target_cues if c["speaker"]})
        result = quality.evaluate(
            workspace_id=self.row.workspace_id,
            language=self.language,
            source_cues=[{"index": c.index, "start": c.start, "end": c.end,
                          "text": c.text, "speaker": c.speaker} for c in cues],
            target_cues=target_cues,
            glossary_entries=entries,
            source_speakers=speakers,
            target_speakers=target_speakers,
            timeline_doc=doc,
            source_duration=timing["source_window_seconds"],
            localized_duration=timing["estimated_duration_seconds"],
            source_language=source_language,
            translation_version=int(self.row.translation_version or 1),
        )
        checks = list(result.get("checks") or [])
        for warning in self.warnings:
            checks.append({"name": "pipeline_warning", "status": "warn",
                           "detail": warning})
        if self.repairs:
            checks.append({"name": "glossary_repairs", "status": "warn",
                           "detail": f"{len(self.repairs)} deterministic repair(s)",
                           "repairs": self.repairs})
            if result["status"] == "PASS":
                result["status"] = "PASS_WITH_WARNINGS"
        result["checks"] = checks
        report = LocalizationQCReport(
            workspace_id=self.row.workspace_id,
            localized_content_id=self.row.id,
            status=str(result["status"]),
            checks_json=dict(result))
        self.db.add(report)
        self.db.commit()
        self.db.refresh(report)
        return result


# ---------------------------------------------------------------------------
# run preparation (used by the API before the job is enqueued)
# ---------------------------------------------------------------------------

def normalize_languages(languages: list[str]) -> list[str]:
    from app.providers.dubbing import LANG_LOCALES

    out: list[str] = []
    for raw in languages or []:
        lang = str(raw or "").strip().lower()
        if not lang:
            continue
        if lang not in LANG_LOCALES:
            raise LocalizationError(
                f"unsupported target language '{raw}' "
                f"(supported: {', '.join(sorted(LANG_LOCALES))})")
        if lang not in out:
            out.append(lang)
    if not out:
        raise LocalizationError("at least one target language is required")
    return out


def prepare_localizations(db, *, workspace_id: str, source_content_id: str,
                          target_languages: list[str],
                          locales: dict[str, str] | None = None,
                          glossary_overlay: list[dict] | None = None,
                          voice_prefs: dict | None = None,
                          translation_version: int = 1,
                          source_language: str = "en") -> list[LocalizedContent]:
    """Create PENDING ``localized_contents`` rows (one per target language)."""
    source = db.get(ContentItem, source_content_id)
    if source is None or source.workspace_id != workspace_id:
        raise LocalizationError(
            f"source content '{source_content_id}' not found in this workspace")
    languages = normalize_languages(target_languages)
    rows: list[LocalizedContent] = []
    for lang in languages:
        row = LocalizedContent(
            workspace_id=workspace_id,
            source_content_id=source_content_id,
            language=lang,
            locale=str((locales or {}).get(lang, "") or "")[:20],
            translation_version=max(1, int(translation_version or 1)),
            status="PENDING",
            lineage_json={
                "source_content_id": source_content_id,
                "source_language": source_language,
                "glossary_overlay": list(glossary_overlay or []),
                "voice_prefs": dict(voice_prefs or {}),
                "translation_version": max(1, int(translation_version or 1)),
                "costs": {"mode": "lineage_json_stub", "translation_usd": 0.0,
                          "tts_usd": 0.0,
                          "note": "costs recorded here until a production_costs "
                                  "table exists"},
            })
        db.add(row)
        rows.append(row)
    db.commit()
    for row in rows:
        db.refresh(row)
    return rows


def run_prepared(db, localized_content_ids: list[str], *, workspace_id: str,
                 job_ctx=None, **pipeline_kwargs) -> list[dict]:
    """Run a batch of prepared rows (job handler entry point)."""
    results: list[dict] = []
    for lid in localized_content_ids:
        pipeline = LocalizationPipeline(
            db, localized_content_id=lid, workspace_id=workspace_id,
            job_ctx=job_ctx, **pipeline_kwargs)
        results.append(pipeline.run())
    return results


__all__ = [
    "STAGES",
    "Cue",
    "LocalizationError",
    "LocalizationPipeline",
    "adapt_culture",
    "apply_pronunciation",
    "brand_glossary_overlay",
    "choose_voice",
    "enforce_glossary",
    "load_glossary",
    "localize_hashtag",
    "localize_metadata",
    "normalize_languages",
    "prepare_localizations",
    "run_prepared",
    "synthesize_texts",
    "translate_texts",
]
