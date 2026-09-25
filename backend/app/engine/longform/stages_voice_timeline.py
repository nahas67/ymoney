"""Long-form stages, part 3: voice production + canonical timeline assembly.

Audio durations are MEASURED (ffprobe on real files) and become authoritative:
caption timing, scene ranges, and chapter boundaries all derive from them.
"""

from __future__ import annotations


def stage_voice(session, project, job_ctx) -> str:
    from app.models import LongFormChapter
    from app.models.assets import MediaAsset
    from app.providers.tts import TTSError, get_tts_provider
    from app.services import jobs as _jobs
    from app.services.storage import STORAGE_ROOT, probe_metadata

    chapters = session.query(LongFormChapter).filter(
        LongFormChapter.project_id == project.id).order_by(LongFormChapter.index).all()
    provider = get_tts_provider()
    voice_name = project.voice_name or "default-narrator"
    audio_dir = STORAGE_ROOT / project.workspace_id / "longform" / project.id / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    segments, failed, chars = [], [], 0
    for ch in chapters:
        for seg in (ch.script_json or {}).get("segments", []):
            _jobs.check_cancelled(job_ctx) if job_ctx is not None else None
            text = _apply_pronunciation(seg["narration"], project.pronunciation_json or {})
            attempt, asset_id, duration = 0, None, None
            while attempt < 2 and asset_id is None:
                try:
                    res = provider.synthesize(text, voice=voice_name, rate=1.0)
                    fname = f"ch{ch.index:02d}_{seg['id']}.{res.format or 'mp3'}"
                    (audio_dir / fname).write_bytes(res.audio_bytes)
                    meta = probe_metadata(audio_dir / fname)
                    duration = meta.get("duration_seconds")
                    if not duration:
                        raise TTSError("unmeasurable narration audio")
                    row = MediaAsset(
                        workspace_id=project.workspace_id, type="voice",
                        origin="generated", provider=getattr(provider, "name", "tts"),
                        storage_key=f"longform/{project.id}/audio/{fname}",
                        mime_type="audio/mpeg", duration_seconds=duration,
                        meta_json={"segment_id": seg["id"], "voice": voice_name,
                                   "chars": len(text)})
                    session.add(row)
                    session.flush()
                    asset_id = row.id
                    chars += len(text)
                except (TTSError, OSError) as exc:
                    attempt += 1
                    if attempt >= 2:
                        failed.append({"segment": seg["id"], "error": str(exc)[:200]})
            segments.append({"segment_id": seg["id"], "chapter": ch.index,
                             "asset_id": asset_id, "duration": duration,
                             "words": seg.get("words", 0)})
    session.flush()
    total = sum(s["duration"] or 0 for s in segments)
    # cost row in THIS session: track_cost() opens its own session, which
    # deadlocks on SQLite while the stage transaction holds the write lock
    from app.models import CostEntry

    tts_cost = round(chars * 0.0002, 6)
    if tts_cost > 0:
        session.add(CostEntry(
            workspace_id=project.workspace_id, category="tts",
            amount_usd=tts_cost, provider=getattr(provider, "name", "tts"),
            detail_json={"project_id": project.id, "chars": chars}))
    project.cost_usd = (project.cost_usd or 0.0) + chars * 0.0002
    project.voice_json = {"voice": voice_name, "segments": segments,
                          "failed": failed, "measured_seconds": round(total, 2)}
    project.stage_progress_json = {**(project.stage_progress_json or {}),
                                   "segments": f"{len(segments) - len(failed)}/{len(segments)}"}
    summary = f"{len(segments) - len(failed)}/{len(segments)} segments, {total:.0f}s measured"
    if failed:
        summary += f" ({len(failed)} FAILED — QC will gate)"
    return summary


def _apply_pronunciation(text: str, rules: dict) -> str:
    for term, pron in (rules or {}).items():
        if term and pron and term in text:
            text = text.replace(term, str(pron))
    return text


# -- TIMELINE ---------------------------------------------------------------

def stage_timeline(session, project, job_ctx) -> str:
    """Assemble the canonical ContentTimeline from measured voice + assets.

    Every generated element stays individually editable: voice/caption clips
    per segment, broll clips per visual beat, chapter title cards as text.
    """
    from app.engine.timeline import add_clip, create_empty
    from app.models import ContentItem, ContentTimeline, LongFormChapter
    from app.models.assets import Scene

    voice = project.voice_json or {}
    by_segment = {s["segment_id"]: s for s in voice.get("segments", []) if s.get("asset_id")}
    if not by_segment:
        raise ValueError("no measured narration — run VOICE first")
    chapters = session.query(LongFormChapter).filter(
        LongFormChapter.project_id == project.id).order_by(LongFormChapter.index).all()
    scenes = session.query(Scene).filter(
        Scene.workspace_id == project.workspace_id,
        Scene.chapter_id.in_([c.id for c in chapters])).order_by(Scene.index).all()
    if project.content_item_id is None:
        item = ContentItem(workspace_id=project.workspace_id,
                           campaign_id=project.campaign_id,
                           topic=f"{project.topic} (long-form master)",
                           status="PRODUCTION")
        session.add(item)
        session.flush()
        project.content_item_id = item.id
    doc = create_empty(project.workspace_id, aspect=project.aspect_ratio)
    t = 0.0
    cap_count, clip_count = 0, 0
    for ch in chapters:
        ch_start = t
        # chapter title card (editable text, 4s)
        add_clip(doc, track="text", clip_id=f"title_ch{ch.index:02d}",
                 name=ch.title, start=t, duration=4.0,
                 text={"content": ch.title, "size": 72})
        clip_count += 1
        for seg in (ch.script_json or {}).get("segments", []):
            meas = by_segment.get(seg["id"])
            if meas is None:
                continue  # failed narration: gap QC gates, never silent filler
            dur = float(meas["duration"])
            add_clip(doc, track="voice", clip_id=f"v_{seg['id']}",
                     name=seg["narration"][:120], start=t, duration=dur,
                     source={"asset_id": meas["asset_id"]})
            add_clip(doc, track="caption", clip_id=f"c_{seg['id']}",
                     name=seg["narration"][:200], start=t, duration=dur)
            clip_count += 2
            cap_count += 1
            t += dur
        ch.script_json = {**(ch.script_json or {}),
                          # full precision: rounding chapter boundaries breaks
                          # exact chaining with voice times (see b_2_0 overlap)
                          "measured_start": ch_start,
                          "measured_end": t}
    # broll per visual beat against MEASURED scene ranges (beats rescaled
    # from estimates onto real narration durations)
    offsets = _scene_offsets(chapters, scenes, by_segment)
    for sc in scenes:
        abs_start, measured = offsets.get(sc.id, (0.0, 0.0))
        est_total = sum(float(b.get("duration", 0.0)) for b in (sc.beats_json or [])) or 1.0
        scale = (measured / est_total) if measured > 0 else 1.0
        for beat in sc.beats_json or []:
            refs = [r for r in (sc.assets_json or [])
                    if r.get("role") == f"beat-{beat['index']}"]
            if not refs:
                continue
            add_clip(doc, track="broll", clip_id=f"b_{sc.index}_{beat['index']}",
                     name=f"scene {sc.index} beat {beat['index']}",
                     start=abs_start + float(beat.get("start", 0.0)) * scale,
                     duration=float(beat.get("duration", 3.0)) * scale,
                     source={"asset_id": refs[0]["asset_id"]})
            clip_count += 1
        sc.start_seconds = abs_start
        sc.end_seconds = abs_start + measured
    row = ContentTimeline(
        workspace_id=project.workspace_id, content_item_id=project.content_item_id,
        name=f"{project.topic} (master)"[:200],
        fps=30.0, duration_seconds=round(t, 2), tracks_json=doc)
    session.add(row)
    session.flush()
    project.timeline_id = row.id
    # scene ranges follow measured audio; link timeline
    for sc in scenes:
        sc.timeline_id = row.id
        sc.content_item_id = project.content_item_id
    project.render_json = {**(project.render_json or {}),
                           "generated_timeline_version": 1,
                           "generated_manifest": _manifest_hash(doc)}
    project.stage_progress_json = {**(project.stage_progress_json or {}),
                                   "timeline": "1/1"}
    return (f"timeline {row.id[:8]}: {t:.0f}s, {clip_count} clips, "
            f"{cap_count} captions, {len(scenes)} scenes")


def _scene_offsets(chapters, scenes, by_segment: dict) -> dict:
    """{scene_id: (absolute_start, measured_duration)} from real narration."""
    seg_dur = {}
    for ch in chapters:
        for seg in (ch.script_json or {}).get("segments", []):
            meas = by_segment.get(seg["id"])
            seg_dur[seg["id"]] = float(meas["duration"]) if meas else 0.0
    offsets = {}
    for ch in chapters:
        base = float((ch.script_json or {}).get("measured_start", 0.0))
        cursor = base
        for sc in [s for s in scenes if s.chapter_id == ch.id]:
            ids = (sc.script_segment or "").split()
            measured = sum(seg_dur.get(i, 0.0) for i in ids)
            if measured <= 0:  # chapter-title scenes carry no segments
                measured = 4.0 if "CHAPTER_TITLE" in (sc.title or "") else 0.0
            offsets[sc.id] = (cursor, measured)
            cursor += measured
    return offsets


def _manifest_hash(doc: dict) -> str:
    from app.engine.timeline import render_manifest

    return render_manifest(doc)["manifest_hash"]
