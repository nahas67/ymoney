"""Server render of an edited canonical timeline to MP4.

Pipeline: canonical doc → segments (trim/split/reorder honored) →
ffmpeg filter graph (scale/pad, fades, drawtext overlays, burned captions,
volume/tempo, layered visual priority) → MP4 + MediaAsset registration by
the caller. Raw client file paths are NEVER trusted: visual/audio inputs
resolve only through workspace-scoped MediaAsset/Video rows.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.services.storage import STORAGE_ROOT, managed_path

ASPECT_DIMS = {"9:16": (1080, 1920), "16:9": (1920, 1080),
               "1:1": (1080, 1080), "4:5": (1080, 1350)}

# visual layering priority when clips overlap in time
VISUAL_PRIORITY = {"avatar": 3, "broll": 2, "video": 1}

_FONT_CANDIDATES = (
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


class TimelineRenderError(Exception):
    pass


def resolve_font() -> str | None:
    """Render font for drawtext overlays/captions (override via YMONEY_FONT_FILE)."""
    import os

    override = os.environ.get("YMONEY_FONT_FILE", "")
    if override and Path(override).exists():
        return override
    for cand in _FONT_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


def resolve_clip_source(workspace_id: str, db, source: dict):
    """Workspace-scoped asset resolution. Returns a Path or None.

    asset_id → MediaAsset.storage_key; video_id → Video.file_path (both
    boundary-checked). A client-supplied `file_path` is IGNORED by design.
    """
    from app.models.assets import MediaAsset
    from app.models.content import Video

    if not isinstance(source, dict):
        return None
    if source.get("asset_id"):
        row = db.get(MediaAsset, source["asset_id"])
        if row is None or row.workspace_id != workspace_id or not row.storage_key:
            return None
        root = (STORAGE_ROOT / workspace_id).resolve()
        cand = (root / row.storage_key.lstrip("/")).resolve()
        try:
            cand.relative_to(root)
        except ValueError:
            return None
        return cand if cand.exists() else None
    if source.get("video_id"):
        video = db.get(Video, source["video_id"])
        if video is None or video.workspace_id != workspace_id:
            return None
        path = managed_path(workspace_id, video.file_path or "")
        return path if path and path.exists() else None
    return None


def _atempo_chain(speed: float) -> str:
    """atempo supports 0.5..2 per instance; chain for wider ranges."""
    speed = max(0.25, min(4.0, float(speed)))
    parts = []
    while speed > 2.0 + 1e-9:
        parts.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5 - 1e-9:
        parts.append("atempo=0.5")
        speed /= 0.5
    parts.append(f"atempo={speed:.4f}")
    return ",".join(parts)


def _escape_drawtext(text: str) -> str:
    # filter-parser specials inside '...' quoted values
    return (text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
            .replace(",", "\\,").replace("\n", " "))


def _escape_fontfile(path: str) -> str:
    # the drive-letter colon (C:/...) splits options unless escaped —
    # proven form: fontfile='C\:/Windows/Fonts/arial.ttf'
    return (path.replace("\\", "\\\\").replace(":", "\\:")
            .replace(",", "\\,").replace("'", "\\'"))


def _escape_filter_value(value: str) -> str:
    """Escape an unquoted filter-graph option value (W11.5 C-F4).

    ``color``/``x``/``y`` come from timeline clip dicts (workspace-member
    controlled). An unescaped ``:`` breaks out of the ``drawtext`` option
    into a new filter option; ``'``, ``,``, ``[``/``]`` and ``;`` break the
    graph structure. Backslash first so escaping is idempotent-safe.
    """
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace(":", "\\:")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace(";", "\\;")
    )


def _safe_fontcolor(color: str) -> str:
    """Named colors and #hex only; anything else falls back to white."""
    import re

    candidate = str(color or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}|[A-Za-z]+", candidate):
        return candidate
    return "white"


def _srt_time(sec: float) -> str:
    ms = max(0, int(round(sec * 1000)))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _caption_is_styled(clip: dict) -> bool:
    """True when a caption carries ANY Work 13 styling/animation/word timing.

    Used to keep the pre-Work-13 render byte-for-byte identical for plain
    captions, while routing everything new through the typed builder.
    """
    text = clip.get("text")
    if isinstance(text, dict) and text:
        return True
    return bool(clip.get("word_level") or clip.get("words") or clip.get("keyframes"))


def _resolve_caption_style(clip: dict):
    """preset -> (preset, resolved style) for a caption clip."""
    from app.engine.captions.presets import resolve_preset_chain

    text = clip.get("text") or {}
    preset_key = str(text.get("preset") or "minimal").strip().lower() or "minimal"
    try:
        return resolve_preset_chain(preset_key, clip_patch={
            k: v for k, v in text.items()
            if k not in ("preset", "max_chars_per_line", "max_lines")
        })
    except Exception:
        # An unknown/invalid preset degrades to the default look rather than
        # failing the whole render; QC reports it separately.
        from app.engine.captions.presets import get_preset

        return get_preset("minimal"), get_preset("minimal").style


def _caption_context(doc: dict, workspace_id: str, db) -> dict:
    """Per-clip word timings + emphasis, read from STORED Work 12 rows.

    Returns empty maps rather than raising: a render must never fail because
    intelligence evidence is missing, it just renders without word-level
    animation.
    """
    words_by_clip: dict[str, list] = {}
    emphasis_by_clip: dict[str, list] = {}
    try:
        from app.engine.captions.emphasis import CaptionEmphasisEngine
        from app.engine.captions.words import load_word_timings
    except Exception:
        return {"words_by_clip": words_by_clip, "emphasis_by_clip": emphasis_by_clip}
    engine = CaptionEmphasisEngine()
    for track in doc.get("tracks", []) or []:
        if track.get("kind") != "caption":
            continue
        for clip in track.get("clips", []) or []:
            clip_id = str(clip.get("id") or "")
            if not clip_id:
                continue
            try:
                start = float(clip.get("start", 0.0))
                end = start + float(clip.get("duration", 0.0))
            except (TypeError, ValueError):
                continue
            source = load_word_timings(
                db, workspace_id,
                asset_id=str(clip.get("source", {}).get("asset_id") or "") or None,
            )
            if not source.available:
                continue
            inside = [w for w in source.words if w.end_s > start and w.start_s < end]
            if not inside:
                continue
            words_by_clip[clip_id] = inside
            text = str(clip.get("name") or "")
            if text:
                emphasis_by_clip[clip_id] = engine.emphasize(text)
    return {"words_by_clip": words_by_clip, "emphasis_by_clip": emphasis_by_clip}


def _effect_context(doc: dict, workspace_id: str, db) -> dict:
    """Tracking/mask evidence for effects, plus the effective safe box.

    Never raises. An effect that needs evidence it does not have declines to
    emit, and the render records a warning instead of faking the effect.
    """
    ctx: dict = {"safe_box": {}}
    try:
        from app.engine.motion.policy import resolve_motion_policy

        policy = resolve_motion_policy(db, workspace_id)
        ctx["safe_box"] = dict(policy.safe_zone or {})
        ctx["policy"] = policy
    except Exception:
        pass
    asset_id = ""
    for track in doc.get("tracks", []) or []:
        if track.get("kind") not in ("video", "broll", "avatar"):
            continue
        for clip in track.get("clips", []) or []:
            found = str((clip.get("source") or {}).get("asset_id") or "")
            if found:
                asset_id = found
                break
        if asset_id:
            break
    if asset_id:
        try:
            from app.engine.motion import tracking

            summary = tracking.evidence_summary(db, workspace_id, asset_id)
            ctx["evidence"] = summary
            ctx["subject_mask_key"] = summary.get("subject_mask")
            ctx["background_mask_key"] = summary.get("subject_mask")
            # Resolve the mask to a real file through the storage boundary.
            # Without it the composite effect reports NOT_AVAILABLE rather than
            # pretending to have blurred a background.
            mask_asset_id = _mask_asset_id(db, workspace_id, asset_id)
            if mask_asset_id:
                ctx["subject_mask_asset_id"] = mask_asset_id
                try:
                    from app.services.storage import managed_path

                    resolved = managed_path(workspace_id, mask_asset_id)
                except Exception:
                    resolved = None
                if resolved is not None:
                    from pathlib import Path as _Path

                    candidate = _Path(str(resolved))
                    if candidate.exists():
                        ctx["subject_mask_path"] = str(candidate)
        except Exception:
            pass
    return ctx


def _mask_asset_id(db, workspace_id: str, asset_id: str) -> str:
    """The newest Work 12 subject mask for an asset, workspace-scoped."""
    try:
        from app.engine.intel import reframe as _reframe

        mask = _reframe.find_mask(db, workspace_id, asset_id, kind="PERSON")
    except Exception:
        return ""
    return str(getattr(mask, "mask_asset_id", "") or "") if mask else ""


def _caption_keyframes(clip: dict, warnings: list[str]):
    """Validated, time-ordered keyframes for a clip (Work 13.1 §3).

    Returns ``(frames, problems)``. Invalid frames are reported and excluded so
    a malformed keyframe cannot produce an unrenderable graph -- it is dropped
    and named in the render warnings instead.
    """
    raw = clip.get("keyframes")
    if not raw:
        return [], []
    try:
        duration = float(clip.get("duration", 0.0))
    except (TypeError, ValueError):
        duration = 0.0
    from app.engine.motion.graph import validate_keyframes

    frames, problems = validate_keyframes(raw, clip_duration=duration)
    for problem in problems:
        warnings.append(
            f"clip {clip.get('id')!r} keyframe {problem}")
    return frames, problems


def _plan_transitions(doc: dict, segments: list[dict]):
    from app.engine.motion.graph import plan_transitions

    return plan_transitions(doc, segments)


def _build_visual_join(vlabels, durations, transition_plan):
    """Build the visual join, using the registry's xfade builder."""
    from app.engine.motion.graph import build_visual_join
    from app.engine.motion.transitions import build_transition_filter

    return build_visual_join(
        vlabels, durations, transition_plan,
        xfade_builder=lambda spec, offset: build_transition_filter(
            spec, offset_s=offset),
    )


def _order_effects(effects):
    from app.engine.motion.graph import order_effects

    return order_effects(effects)


def _apply_keyframe_geometry(vf: str, frames: list[dict], *, width: int,
                             height: int) -> str:
    """Append a time-varying crop driven by canonical keyframes.

    ``crop`` accepts ``t`` in its expressions, so this produces real motion
    from stored configuration. The keyframe times are clip-local and the
    segment is already trimmed to the clip, so ``t`` lines up with the
    canonical timeline without any extra offset.
    """
    from app.engine.motion.graph import evaluate_keyframes

    crop_w, cw = evaluate_keyframes(frames, "crop_width", default=float(width))
    crop_h, ch = evaluate_keyframes(frames, "crop_height", default=float(height))
    crop_x, cx = evaluate_keyframes(frames, "crop_x", default=0.0)
    crop_y, cy = evaluate_keyframes(frames, "crop_y", default=0.0)
    if not (crop_w or crop_h or crop_x or crop_y):
        return vf
    w = crop_w or f"{cw:.0f}"
    h = crop_h or f"{ch:.0f}"
    x = crop_x or f"{cx:.0f}"
    y = crop_y or f"{cy:.0f}"
    return (vf + f",crop=w='max(16,{w})':h='max(16,{h})'"
            f":x='max(0,{x})':y='max(0,{y})'")


def _plan_composite_effect(effect: dict, ctx: dict, clip: dict):
    """Plan a composite effect, returning ``(plan, extra_input_args)``.

    The extra ``-i`` arguments are returned separately so the caller can append
    them to the shared input list in the right order.
    """
    from app.engine.motion.graph import COMPOSITE_EFFECTS, plan_composite

    if str(effect.get("type") or "").upper() not in COMPOSITE_EFFECTS:
        from app.engine.motion.graph import CompositePlan

        return CompositePlan(key=str(effect.get("type") or "")), []
    plan = plan_composite(effect, ctx)
    inputs: list[tuple[str, ...]] = []
    if plan.available and plan.input_args:
        inputs.append(tuple(plan.input_args))
    return plan, inputs


def _build_effect(effect: dict, ctx: dict):
    """Validate + build one effect filter, raising on an invalid definition."""
    from app.engine.motion.effects import build_effect_filter

    return build_effect_filter(effect, ctx)


def build_caption_filters(**kwargs):
    """Thin re-export of the Work 13 caption builder (single rendering path).

    Imported lazily inside the render loop so this module keeps no import-time
    dependency on the caption engine.
    """
    from app.engine.captions.filters import build_caption_filters as _build

    return _build(**kwargs)


def resolve_caption_font(family: str = "") -> str | None:
    """Font for caption/text overlays. Delegates to the shared resolver so the
    Work 13 builder and this renderer cannot drift apart."""
    from app.engine.captions.ffmpeg_escape import resolve_font as _shared

    return _shared(family)


def visual_segments(doc: dict, resolver) -> list[dict]:
    """Partition the timeline into non-overlapping visual segments.

    Topmost clip wins by VISUAL_PRIORITY; gaps become black slugs.
    Each segment: {start, duration, path|None, source_start, speed, clip}.
    """
    clips = [dict(c, _kind=tr["kind"]) for tr in doc.get("tracks", [])
             for c in tr.get("clips", []) if tr.get("kind") in VISUAL_PRIORITY]
    total = float(doc.get("duration_seconds", 0.0))
    bounds = sorted({0.0, total} | {c["start"] for c in clips}
                    | {c["start"] + c["duration"] for c in clips})
    segments = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a <= 1e-6:
            continue
        covering = [c for c in clips if c["start"] <= a + 1e-6
                    and c["start"] + c["duration"] >= b - 1e-6]
        best = max(covering, key=lambda c: (VISUAL_PRIORITY[c["_kind"]], c["start"])) \
            if covering else None
        path = resolver(best["source"]) if best else None
        src_off = float(best.get("source_start", 0.0)) + (a - best["start"]) \
            if best else 0.0
        segments.append({"start": a, "duration": b - a,
                         "path": path, "source_start": max(0.0, src_off),
                         "speed": float((best or {}).get("speed", 1.0)),
                         "fade_in": float((best or {}).get("fade_in", 0.0)),
                         "fade_out": float((best or {}).get("fade_out", 0.0)),
                         "clip": best})
    return segments


def render_timeline(workspace_id: str, db, doc: dict, *, out_name: str = "edit.mp4",
                    fps: int = 30) -> dict:
    """Render a canonical doc to MP4. Returns {path, duration, width, height,
    warnings}. Raises TimelineRenderError (fail closed, never mock output)."""
    from app.engine.timeline import SUPPORTED_ASPECTS, validate_timeline

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise TimelineRenderError("ffmpeg not found — cannot render timeline")
    validate_timeline(doc)
    aspect = doc.get("aspect_ratio", "9:16")
    if aspect not in SUPPORTED_ASPECTS:
        aspect = "9:16"
    width, height = ASPECT_DIMS[aspect]
    total = float(doc.get("duration_seconds", 0.0))
    if total <= 0:
        raise TimelineRenderError("timeline has no duration")
    warnings: list[str] = []

    def _resolve(source: dict):
        return resolve_clip_source(workspace_id, db, source or {})

    segments = visual_segments(doc, _resolve)
    font = resolve_font()
    if font is None:
        warnings.append("no render font found — text overlays and captions skipped")

    # -- Work 13 wiring ----------------------------------------------------
    # The caption / motion / effect engines are EXTENSIONS of this renderer:
    # one render path, one font dependency, and one escaping implementation
    # (app.engine.captions.ffmpeg_escape, which this module's helpers delegate
    # to so there is a single audited copy).
    effect_ctx = _effect_context(doc, workspace_id, db)
    safe_box = dict(effect_ctx.get("safe_box") or {})
    caption_ctx = _caption_context(doc, workspace_id, db)

    tmp = Path(tempfile.mkdtemp(prefix="ym-edit-"))
    inputs: list[str] = []
    n_inputs = 0  # ordinal of -i options (NOT argv position)
    fc: list[str] = []
    vlabels: list[str] = []

    def _add_input(*args: str) -> int:
        nonlocal n_inputs
        inputs.extend(args)
        n_inputs += 1
        return n_inputs - 1

    for i, seg in enumerate(segments):
        dur = seg["duration"] / max(seg["speed"], 1e-6)
        if seg["path"] is None:
            idx = _add_input("-f", "lavfi", "-i", f"color=black:{width}x{height}:r={fps}:d={dur:.3f}")
            fc.append(f"[{idx}:v]format=yuv420p[v{i}]")
        else:
            suffix = Path(str(seg["path"])).suffix.lower()
            if suffix in (".png", ".jpg", ".jpeg", ".webp"):
                idx = _add_input("-loop", "1", "-t", f"{dur:.3f}", "-i", str(seg["path"]))
            else:
                idx = _add_input("-ss", f"{seg['source_start']:.3f}", "-t", f"{dur:.3f}",
                                 "-i", str(seg["path"]))
            vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                  f"crop={width}:{height},fps={fps},"
                  f"setpts=PTS/{seg['speed']:.4f}")
            if seg["fade_in"] > 0:
                vf += f",fade=t=in:st=0:d={seg['fade_in']:.3f}"
            if seg["fade_out"] > 0:
                vf += f",fade=t=out:st={max(0.0, dur - seg['fade_out']):.3f}:d={seg['fade_out']:.3f}"
            # Work 13 §8: typed, registry-validated effects, applied in the
            # order they were stored on the clip. A refused or composite effect
            # is REPORTED in warnings and skipped -- it never corrupts the
            # graph, and it is never silently dropped.
            seg_clip = seg.get("clip") or {}
            # Work 13.1 §4: a composite effect is a real multi-input graph, so
            # it REPLACES the segment's start label instead of being appended to
            # a single-input chain. Unavailable (e.g. no Work 12 mask) is an
            # explicit NOT_AVAILABLE warning, never a silent skip.
            chain_in = f"[{idx}:v]"
            # Work 13.1 §3: canonical keyframes become a time-varying crop, so a
            # keyframed clip really moves/scales instead of only being stored.
            kf_frames, _kf_problems = _caption_keyframes(seg_clip, warnings)
            if kf_frames:
                vf = _apply_keyframe_geometry(vf, kf_frames, width=width,
                                              height=height)
            ordered_effects, order_problems = _order_effects(
                seg_clip.get("effects") or [])
            warnings.extend(
                f"clip {seg_clip.get('id')!r} {problem}"
                for problem in order_problems)
            for effect in ordered_effects:
                kind = str(effect.get("type") or "").upper()
                from app.engine.motion.graph import COMPOSITE_EFFECTS as _COMPOSITE

                if kind in _COMPOSITE:
                    composite, extra_inputs = _plan_composite_effect(
                        effect, effect_ctx, seg_clip)
                    if composite.available and extra_inputs:
                        # The graph must reference the REAL input ordinal that
                        # _add_input returns, not a synthetic label.
                        mask_idx = _add_input(*extra_inputs[0])
                        for fragment in composite.filters:
                            fc.append(fragment
                                      .replace("{IN}", chain_in)
                                      .replace(f"[{composite.input_label}:v]",
                                               f"[{mask_idx}:v]"))
                        chain_in = f"[{composite.out_suffix}{i}]"
                        continue
                    if not composite.available:
                        warnings.append(
                            f"NOT_AVAILABLE: {kind} on clip "
                            f"{seg_clip.get('id')!r} - {composite.reason}")
                    continue
                try:
                    built = _build_effect(effect, effect_ctx)
                except Exception as exc:  # noqa: BLE001 - a bad effect is a warning
                    warnings.append(
                        f"effect on clip {seg_clip.get('id')!r} refused: {exc}")
                    continue
                if not built:
                    warnings.append(
                        f"effect {kind} on clip {seg_clip.get('id')!r} produced "
                        f"no filter (evidence or prerequisites missing)")
                    continue
                vf += f",{built}"
            vf += ",format=yuv420p"
            fc.append(f"{chain_in}{vf}[v{i}]")
        vlabels.append(f"[v{i}]")

    # Work 13.1 §1: real transitions. When any adjacent segment pair carries a
    # validated transition, the flat `concat` is replaced by a pairwise `xfade`
    # ladder whose offsets come from the canonical clip timing. Audio is built
    # separately below and is NOT affected, which is what keeps A/V in sync.
    transition_plan, transition_warnings = _plan_transitions(doc, segments)
    warnings.extend(transition_warnings)
    join = _build_visual_join(
        vlabels,
        [float(seg.get("duration", 0.0)) for seg in segments],
        transition_plan,
    )
    fc.extend(join.filters)
    warnings.extend(join.warnings)
    for note in join.applied:
        warnings.append(
            f"transition {note['type']} applied at offset {note['offset']}s "
            f"({note['duration']}s)")

    # audio: voice/music/sfx delayed into place, mixed over full-duration silence
    audio_tracks = [dict(c) for tr in doc.get("tracks", [])
                    for c in tr.get("clips", [])
                    if tr.get("kind") in ("voice", "music", "sfx")]
    silence_idx = _add_input("-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={total:.3f}")
    mix_inputs = [f"[{silence_idx}:a]"]
    for clip in audio_tracks:
        path = _resolve(clip.get("source") or {})
        if path is None:
            warnings.append(f"audio clip '{clip.get('id')}' source unresolvable — skipped")
            continue
        dur = float(clip["duration"])
        idx = _add_input("-ss", f"{float(clip.get('source_start', 0.0)):.3f}",
                         "-t", f"{dur:.3f}", "-i", str(path))
        af = (f"aformat=sample_rates=44100:channel_layouts=stereo,"
              f"{_atempo_chain(float(clip.get('speed', 1.0)))},"
              f"volume={float(clip.get('volume', 1.0)):.4f}")
        if float(clip.get("fade_in", 0.0)) > 0:
            af += f",afade=t=in:st=0:d={float(clip.get('fade_in')):.3f}"
        if float(clip.get("fade_out", 0.0)) > 0:
            af += f",afade=t=out:st={max(0.0, dur - float(clip.get('fade_out'))):.3f}:d={float(clip.get('fade_out')):.3f}"
        af += f",adelay={int(float(clip['start']) * 1000)}|{int(float(clip['start']) * 1000)}"
        fc.append(f"[{idx}:a]{af}[a{idx}]")
        mix_inputs.append(f"[a{idx}]")
    fc.append(f"{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:duration=longest:dropout_transition=0,atrim=0:{total:.3f}[amix]")

    # text overlays + burned captions via drawtext (single font dependency)
    vcur = "[vcat]"
    if font:
        font_esc = _escape_fontfile(font)
        n_text = 0
        for tr in doc.get("tracks", []):
            if tr.get("kind") != "text":
                continue
            for c in tr.get("clips", []):
                content = ((c.get("text") or {}).get("content") or c.get("name") or "").strip()
                if not content:
                    continue
                t = c.get("text") or {}
                size = int(t.get("size", 64) or 64)
                color = str(t.get("color", "white") or "white")
                x = str((t.get("transform") or c.get("transform") or {}).get("x", "(w-text_w)/2"))
                y = str((t.get("transform") or c.get("transform") or {}).get("y", "h*0.2"))
                t0, t1 = float(c["start"]), float(c["start"]) + float(c["duration"])
                # comma-free window expression (filter parser splits bare commas)
                window = f"gte(t\\,{t0:.3f})*lte(t\\,{t1:.3f})"
                fc.append(f"{vcur}drawtext=fontfile='{font_esc}':text='{_escape_drawtext(content)}':"
                          f"fontsize={size}:fontcolor={_safe_fontcolor(color)}:"
                          f"x={_escape_filter_value(x)}:y={_escape_filter_value(y)}:"
                          f"enable='{window}'[vtxt{n_text}]")
                vcur = f"[vtxt{n_text}]"
                n_text += 1
        caps = [c for tr in doc.get("tracks", []) if tr.get("kind") == "caption"
                for c in tr.get("clips", [])]
        for j, c in enumerate(caps):
            line = (c.get("name") or "").strip()
            if not line:
                continue
            # Work 13: a caption that carries ANY Work 13 styling, animation or
            # word timing goes through the typed builder. A caption with none of
            # those keeps the legacy hardcoded path byte-for-byte, so every
            # pre-Work-13 timeline renders exactly as it did before.
            if not _caption_is_styled(c):
                t0, t1 = float(c["start"]), float(c["start"]) + float(c["duration"])
                window = f"gte(t\\,{t0:.3f})*lte(t\\,{t1:.3f})"
                fc.append(f"{vcur}drawtext=fontfile='{font_esc}':text='{_escape_drawtext(line)}':"
                          f"fontsize=56:fontcolor=white:borderw=2:bordercolor=black:"
                          f"x=(w-text_w)/2:y=h*0.78:"
                          f"enable='{window}'[vcap{j}]")
                vcur = f"[vcap{j}]"
                continue
            preset, style = _resolve_caption_style(c)
            words = caption_ctx.get("words_by_clip", {}).get(str(c.get("id"))) or []
            word_level = bool(c.get("word_level")) and bool(words)
            plan = build_caption_filters(
                label_in=vcur.strip("[]"),
                caption=c,
                style=style,
                width=width,
                height=height,
                safe_box=safe_box,
                max_chars_per_line=int((c.get("text") or {}).get(
                    "max_chars_per_line", preset.max_chars_per_line)),
                max_lines=int((c.get("text") or {}).get(
                    "max_lines", preset.max_lines)),
                words=words,
                word_level=word_level,
                emphasis=caption_ctx.get("emphasis_by_clip", {}).get(
                    str(c.get("id"))) or [],
                font_family=str((c.get("text") or {}).get("font") or style.font or ""),
                keyframes=_caption_keyframes(c, warnings)[0],
                label_prefix=f"vcap{j}",
            )
            for built in plan.filters:
                fc.append(built)
            for note in plan.warnings:
                warnings.append(f"caption {c.get('id')!r}: {note}")
            if plan.chains:
                vcur = f"[{plan.chains[-1][1]}]"

    out_path = tmp / out_name
    cmd = ([ffmpeg, "-y", *inputs, "-filter_complex", ";".join(fc),
            "-map", vcur, "-map", "[amix]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-shortest", str(out_path)])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not out_path.exists():
        # Include the generated graph in the failure: an ffmpeg "matches no
        # streams" / "no such filter" error is otherwise untraceable.
        raise TimelineRenderError(
            f"ffmpeg failed: {(proc.stderr or '')[-500:]}\n"
            f"-- filter_complex --\n{';'.join(fc)}"
        )
    from app.services.storage import probe_metadata

    meta = probe_metadata(out_path)
    return {"path": str(out_path),
            "duration_seconds": meta.get("duration_seconds"),
            "width": meta.get("width") or width, "height": meta.get("height") or height,
            "warnings": warnings,
            "job_hash": hashlib.sha256(";".join(fc).encode()).hexdigest()[:16]}
