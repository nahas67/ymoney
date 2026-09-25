"""Long-form stages, part 4: QC report, chunked render, metadata package."""

from __future__ import annotations

from pathlib import Path

QC_RESULTS = ("PASS", "PASS_WITH_WARNINGS", "REQUIRES_REGENERATION",
              "REQUIRES_REVIEW", "FAIL")


def stage_qc(session, project, job_ctx, *, final_path: str | None = None) -> str:
    """Pre-render QC gates the render; post-render QC validates the file."""
    from app.models import ContentTimeline, LongFormChapter

    warnings, failures = [], []
    chapters = session.query(LongFormChapter).filter(
        LongFormChapter.project_id == project.id).all()
    if not chapters:
        failures.append("no chapters")
    unscripted = [c.index for c in chapters if not (c.script_json or {}).get("segments")]
    if unscripted:
        failures.append(f"chapters without script: {unscripted}")
    voice = project.voice_json or {}
    if voice.get("failed"):
        failures.append(f"{len(voice['failed'])} narration segments failed TTS")
    row = session.get(ContentTimeline, project.timeline_id) if project.timeline_id else None
    if row is None:
        failures.append("no timeline built")
    doc = (row.tracks_json or {}) if row else {}
    clips = [c for tr in doc.get("tracks", []) for c in tr.get("clips", [])]
    if row and not clips:
        failures.append("timeline has no clips")
    # missing/unresolvable media
    from app.providers.video_engine.timeline_render import resolve_clip_source

    missing = sorted({(c.get("source") or {}).get("asset_id", "?")
                      for tr in doc.get("tracks", []) if tr.get("kind") in (
                          "video", "broll", "avatar", "voice", "music", "sfx")
                      for c in tr.get("clips", [])
                      if resolve_clip_source(project.workspace_id, session,
                                             c.get("source") or {}) is None})
    if missing:
        failures.append(f"{len(missing)} unresolvable media refs: {missing[:5]}")
    # repetition: one asset dominating the screen
    usage: dict[str, float] = {}
    total_dur = float(doc.get("duration_seconds", 0.0)) or 1.0
    for tr in doc.get("tracks", []):
        if tr.get("kind") not in ("video", "broll", "avatar"):
            continue
        for c in tr.get("clips", []):
            aid = (c.get("source") or {}).get("asset_id")
            if aid:
                usage[aid] = usage.get(aid, 0.0) + float(c.get("duration", 0.0))
    for aid, secs in usage.items():
        if secs / total_dur > 0.35:
            warnings.append(f"asset {aid[:8]} covers {secs / total_dur:.0%} of screen time")
    # fact risk from the verify stage
    risky = (project.fact_report_json or {}).get("risky_claims", 0)
    # file-level checks when a render exists
    file_info: dict = {}
    if final_path:
        file_info = _probe_final(final_path, warnings, failures)
    if failures:
        result = "FAIL"
    elif risky:
        result = "REQUIRES_REVIEW"
    elif warnings:
        result = "PASS_WITH_WARNINGS"
    else:
        result = "PASS"
    project.qc_json = {"result": result, "warnings": warnings,
                       "failures": failures, "file": file_info,
                       "risky_claims": risky,
                       "clips": len(clips), "chapters": len(chapters)}
    project.stage_progress_json = {**(project.stage_progress_json or {}), "checks": "1/1"}
    return f"{result}: {len(failures)} failures, {len(warnings)} warnings"


def _probe_final(path: str, warnings: list, failures: list) -> dict:
    import json as _json
    import shutil
    import subprocess

    info: dict = {"path": path}
    try:
        size = __import__("pathlib").Path(path).stat().st_size
    except OSError:
        size = 0
    info["bytes"] = size
    if size == 0:
        failures.append("zero-byte output")
        return info
    if shutil.which("ffprobe") is None:
        warnings.append("ffprobe unavailable — stream checks skipped")
        return info
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=120)
        data = _json.loads(out.stdout or "{}")
    except Exception as exc:
        failures.append(f"ffprobe failed: {exc}")
        return info
    streams = data.get("streams", [])
    kinds = {s.get("codec_type") for s in streams}
    fmt = data.get("format", {})
    info.update({"duration": float(fmt.get("duration") or 0),
                 "video_codec": next((s.get("codec_name") for s in streams
                                      if s.get("codec_type") == "video"), None),
                 "audio_codec": next((s.get("codec_name") for s in streams
                                      if s.get("codec_type") == "audio"), None),
                 "width": next((s.get("width") for s in streams
                                if s.get("codec_type") == "video"), None),
                 "height": next((s.get("height") for s in streams
                                 if s.get("codec_type") == "video"), None)})
    if "video" not in kinds:
        failures.append("no video stream")
    if "audio" not in kinds:
        failures.append("no audio stream")
    # black-frame + silence screens (real filters, bounded runtime)
    if shutil.which("ffmpeg") is not None:
        _screen(path, info, warnings)
    return info


def _screen(path: str, info: dict, warnings: list) -> None:
    import subprocess

    try:
        black = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", path, "-vf",
             "blackdetect=d=2:pic_th=0.98", "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=300)
        n_black = (black.stderr or "").count("black_start")
        info["black_segments"] = n_black
        if n_black > 3:
            warnings.append(f"{n_black} black segments detected")
        silence = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", path, "-af",
             "silencedetect=noise=-40dB:d=5", "-vn", "-f", "null", "-"],
            capture_output=True, text=True, timeout=300)
        n_sil = (silence.stderr or "").count("silence_start")
        info["long_silences"] = n_sil
        if n_sil > 2:
            warnings.append(f"{n_sil} long silences detected")
    except Exception as exc:
        warnings.append(f"screening skipped: {type(exc).__name__}")


# -- CHUNKED RENDER -------------------------------------------------------------

def stage_render(session, project, job_ctx) -> str:
    """Chapter-chunked render with per-chunk retry, hash cache, and concat."""
    from app.models import ContentTimeline
    from app.providers.video_engine.timeline_render import TimelineRenderError, render_timeline

    row = session.get(ContentTimeline, project.timeline_id) if project.timeline_id else None
    if row is None:
        raise ValueError("no timeline — run TIMELINE first")
    doc = dict(row.tracks_json or {})
    bounds = _chapter_bounds(session, project, doc)
    if not bounds:
        raise ValueError("nothing to render (no chapter ranges, no duration)")
    workdir = _workdir(project)
    chunks, cache = [], dict((project.render_json or {}).get("chunk_cache", {}))
    import hashlib
    import json as _json

    for i, (start, end) in enumerate(bounds):
        sub = _slice_doc(doc, start, end)
        key = hashlib.sha256(_json.dumps(sub, sort_keys=True, default=str).encode()
                             ).hexdigest()[:16]
        dest = workdir / f"chunk_{i:02d}_{key}.mp4"
        cached = cache.get(str(i))
        if cached and cached.get("hash") == key and (workdir / cached["file"]).exists():
            chunks.append({"index": i, "file": cached["file"], "hash": key,
                           "cached": True})
            continue
        attempt, last_err = 0, None
        while attempt < 2:
            try:
                if job_ctx is not None:
                    from app.services import jobs as _jobs

                    _jobs.check_cancelled(job_ctx)
                out = render_timeline(project.workspace_id, session, sub,
                                      out_name=f"chunk_{i:02d}.mp4")
                __import__("shutil").move(out["path"], dest)
                chunks.append({"index": i, "file": dest.name, "hash": key,
                               "cached": False})
                cache[str(i)] = {"file": dest.name, "hash": key}
                last_err = None
                break
            except TimelineRenderError as exc:
                last_err = str(exc)[:300]
                attempt += 1
        if last_err:
            raise TimelineRenderError(f"chunk {i} failed twice: {last_err}")
    final = _concat(workdir, chunks, project)
    from app.models.assets import MediaAsset
    from app.services.storage import STORAGE_ROOT, probe_metadata

    dest_dir = STORAGE_ROOT / project.workspace_id / "longform" / project.id
    dest_dir.mkdir(parents=True, exist_ok=True)
    final_dest = dest_dir / "master.mp4"
    if final.exists():
        __import__("shutil").move(str(final), final_dest)
    meta = probe_metadata(final_dest)
    asset = MediaAsset(
        workspace_id=project.workspace_id, type="video", origin="render",
        provider="longform_chunked", storage_key=f"longform/{project.id}/master.mp4",
        mime_type="video/mp4", duration_seconds=meta.get("duration_seconds"),
        width=meta.get("width"), height=meta.get("height"),
        file_size=final_dest.stat().st_size if final_dest.exists() else None,
        meta_json={"project_id": project.id, "chunks": len(chunks)})
    session.add(asset)
    session.flush()
    project.render_json = {"asset_id": asset.id, "chunks": chunks,
                           "chunk_cache": cache,
                           "duration": meta.get("duration_seconds"),
                           "strategy": "chapter-chunked"}
    project.stage_progress_json = {**(project.stage_progress_json or {}),
                                   "chunks": f"{len(chunks)}/{len(bounds)}"}
    # post-render QC on the real file (result lands in qc_json)
    post = stage_qc(session, project, job_ctx, final_path=str(final_dest))
    return (f"{len(chunks)} chunks assembled "
            f"({sum(1 for c in chunks if c.get('cached'))} cached); post-render QC: {post}")


def _workdir(project):
    from app.services.storage import STORAGE_ROOT

    # absolute: ffmpeg subprocesses andconcat entries must not depend on
    # process CWD (tests chdir into tmp workspaces; workers may differ)
    workdir = (Path.cwd() / STORAGE_ROOT / project.workspace_id / "longform" / project.id / "chunks").resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def _chapter_bounds(session, project, doc=None) -> list[tuple[float, float]]:
    from app.models import LongFormChapter

    chapters = session.query(LongFormChapter).filter(
        LongFormChapter.project_id == project.id).order_by(LongFormChapter.index).all()
    bounds = []
    for ch in chapters:
        start = float((ch.script_json or {}).get("measured_start", 0.0))
        end = float((ch.script_json or {}).get("measured_end", start))
        if end > start:
            bounds.append((start, end))
    if not bounds and doc and float(doc.get("duration_seconds", 0.0)) > 0:
        bounds = [(0.0, float(doc.get("duration_seconds")))]
    return bounds


def _slice_doc(doc: dict, start: float, end: float) -> dict:
    """Clips intersecting [start, end), shifted to zero. Chapter-title cards
    at exactly `start` are kept so every chunk opens with context."""
    out = {**doc, "tracks": [], "duration_seconds": round(end - start, 2)}
    for tr in doc.get("tracks", []):
        clips = []
        for c in tr.get("clips", []):
            cs, ce = float(c.get("start", 0.0)), float(c.get("start", 0.0)) + float(c.get("duration", 0.0))
            if ce <= start or cs >= end:
                continue
            keep_start, keep_end = max(cs, start), min(ce, end)
            # chapter bounds can slice a beat within rounding noise of its
            # edge — drop sub-millisecond slivers instead of failing validation
            if round(keep_end - keep_start, 3) <= 0:
                continue
            cc = dict(c, start=round(keep_start - start, 3),
                      duration=round(keep_end - keep_start, 3))
            ss = float(c.get("source_start", 0.0)) + (keep_start - cs)
            cc["source_start"] = round(ss, 3)
            clips.append(cc)
        out["tracks"].append({**tr, "clips": sorted(clips, key=lambda x: x["start"])})
    return out


def _concat(workdir, chunks: list[dict], project):
    import shutil
    import subprocess

    from app.providers.video_engine.timeline_render import TimelineRenderError

    if len(chunks) == 1:
        # copy (never move): the chunk file is the resume cache and must survive
        final = workdir / "final.mp4"
        __import__("shutil").copy2(workdir / chunks[0]["file"], final)
        return final
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise TimelineRenderError("ffmpeg missing for final assembly")
    lst = workdir / "concat.txt"
    # forward slashes: the concat demuxer treats backslash as an escape,
    # so Windows paths must use POSIX separators here
    lst.write_text("".join(f"file '{(workdir / c['file']).as_posix()}'\n" for c in chunks),
                   encoding="utf-8")
    final = workdir / "final.mp4"
    proc = subprocess.run(
        [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", str(final)], capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not final.exists():
        raise TimelineRenderError(f"concat failed: {(proc.stderr or '')[-300:]}")
    return final


# -- METADATA -------------------------------------------------------------------

def stage_metadata(session, project, job_ctx) -> str:
    from app.engine.longform.stages_early import llm_json_or_none
    from app.models import LongFormChapter
    from app.models.assets import Scene

    scenes = session.query(Scene).filter(
        Scene.workspace_id == project.workspace_id,
        Scene.timeline_id == (project.timeline_id or "__none__")).order_by(
        Scene.index).all()
    chapters = session.query(LongFormChapter).filter(
        LongFormChapter.project_id == project.id).order_by(LongFormChapter.index).all()
    # YouTube chapters from FINAL measured scene ranges (never estimates)
    yt, cursor = [], 0.0
    for sc in scenes:
        yt.append({"at": _stamp(max(sc.start_seconds, cursor)),
                   "title": (sc.title or "")[:100]})
        cursor = max(cursor, sc.end_seconds)
    titles = _titles(project, llm_json_or_none)
    description = _description(project, chapters, yt)
    first_title = titles[0]["title"] if titles else project.topic
    thumbs = _thumbnails(session, project, first_title)
    project.metadata_json = {"titles": titles, "description": description,
                             "chapters": yt, "hashtags": ["#explained"],
                             "thumbnails": thumbs}
    project.stage_progress_json = {**(project.stage_progress_json or {}),
                                   "artifacts": f"{3 + len(thumbs)}/{3 + len(thumbs)}"}
    return f"{len(titles)} titles, {len(yt)} chapters, {len(thumbs)} thumbnails"


def _titles(project, llm_fn) -> list[dict]:
    intents = ["SEARCH", "CURIOSITY", "AUTHORITY", "NEWS", "EXPLAINER", "CONTRARIAN"]
    base = [f"{project.topic}: {w}" for w in
            ("What Actually Matters", "The Full Story", "What Nobody Tells You",
             "The Complete Breakdown", "Why It Changes Everything", "Beginner's Guide")]
    llm = llm_fn("You are a YouTube title writer. Return JSON {titles: "
                 "[{title, intent}]} with 6 titles, no clickbait unsupported by the video.",
                 f"Topic: {project.topic}. Format: {project.content_format}.",
                 project.workspace_id)
    if isinstance((llm or {}).get("titles"), list) and llm["titles"]:
        out = []
        for t in llm["titles"][:6]:
            if isinstance(t, dict) and t.get("title"):
                out.append({"title": str(t["title"])[:100],
                            "intent": str(t.get("intent", "EXPLAINER")).upper()[:20]})
        if out:
            return out
    return [{"title": t[:100], "intent": intent}
            for t, intent in zip(base, intents)]


def _description(project, chapters, yt: list) -> str:
    lines = [project.topic, "",
             (project.strategy_json or {}).get("core_theme", project.topic), ""]
    for entry in yt:
        lines.append(f"{entry['at']} {entry['title']}")
    lines += ["", "Chapters generated from the final edit.",
              f"Format: {project.content_format}."]
    return "\n".join(lines)[:4000]


def _stamp(sec: float) -> str:
    s = max(0, int(sec))
    return f"{s // 60:02d}:{s % 60:02d}"


def _thumbnails(session, project, title: str) -> list[dict]:
    import shutil
    import subprocess

    from app.providers.video_engine.timeline_render import resolve_font
    from app.services.storage import STORAGE_ROOT

    ffmpeg = shutil.which("ffmpeg")
    font = resolve_font()
    if not ffmpeg or not font:
        return []
    concepts = [("big subject + short text", title[:40]),
                ("comparison", f"{project.topic[:30]} vs the hype"),
                ("curiosity gap", "What nobody tells you")]
    out, thumb_dir = [], STORAGE_ROOT / project.workspace_id / "longform" / project.id / "thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    from app.models.assets import MediaAsset

    for i, (concept, text) in enumerate(concepts):
        clean = text.replace(":", " -").replace("'", "")
        font_esc = font.replace("\\", "\\\\").replace(":", "\\:")
        dest = thumb_dir / f"thumb_{i}.png"
        proc = subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i", "color=0x111827:1280x720",
             "-frames:v", "1", "-vf",
             f"drawtext=fontfile='{font_esc}':text='{clean}':fontsize=72:"
             f"fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2",
             str(dest)], capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            continue
        row = MediaAsset(workspace_id=project.workspace_id, type="thumbnail",
                         origin="generated", provider="thumbnail_cards",
                         storage_key=f"longform/{project.id}/thumbs/thumb_{i}.png",
                         mime_type="image/png", width=1280, height=720,
                         meta_json={"concept": concept, "text": clean})
        session.add(row)
        session.flush()
        out.append({"asset_id": row.id, "concept": concept, "text": clean})
    return out
