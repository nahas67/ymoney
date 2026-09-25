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


def _srt_time(sec: float) -> str:
    ms = max(0, int(round(sec * 1000)))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


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
            vf += ",format=yuv420p"
            fc.append(f"[{idx}:v]{vf}[v{i}]")
        vlabels.append(f"[v{i}]")

    fc.append(f"{''.join(vlabels)}concat=n={len(vlabels)}:v=1:a=0[vcat]")

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
                          f"fontsize={size}:fontcolor={color}:x={x}:y={y}:"
                          f"enable='{window}'[vtxt{n_text}]")
                vcur = f"[vtxt{n_text}]"
                n_text += 1
        caps = [c for tr in doc.get("tracks", []) if tr.get("kind") == "caption"
                for c in tr.get("clips", [])]
        for j, c in enumerate(caps):
            line = (c.get("name") or "").strip()
            if not line:
                continue
            t0, t1 = float(c["start"]), float(c["start"]) + float(c["duration"])
            window = f"gte(t\\,{t0:.3f})*lte(t\\,{t1:.3f})"
            fc.append(f"{vcur}drawtext=fontfile='{font_esc}':text='{_escape_drawtext(line)}':"
                      f"fontsize=56:fontcolor=white:borderw=2:bordercolor=black:"
                      f"x=(w-text_w)/2:y=h*0.78:"
                      f"enable='{window}'[vcap{j}]")
            vcur = f"[vcap{j}]"

    out_path = tmp / out_name
    cmd = ([ffmpeg, "-y", *inputs, "-filter_complex", ";".join(fc),
            "-map", vcur, "-map", "[amix]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-shortest", str(out_path)])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not out_path.exists():
        raise TimelineRenderError(f"ffmpeg failed: {(proc.stderr or '')[-500:]}")
    from app.services.storage import probe_metadata

    meta = probe_metadata(out_path)
    return {"path": str(out_path),
            "duration_seconds": meta.get("duration_seconds"),
            "width": meta.get("width") or width, "height": meta.get("height") or height,
            "warnings": warnings,
            "job_hash": hashlib.sha256(";".join(fc).encode()).hexdigest()[:16]}
