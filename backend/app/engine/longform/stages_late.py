"""Long-form stages, part 2: scenes → assets → voice → timeline → QC → render → metadata.

Scene types drive visual planning; visual beats (2–6s shot units) live on the
scene row (beats_json) so thousands of shot changes never become thousands of
heavyweight Scene rows. Asset fallbacks are recorded, never silent.
"""

from __future__ import annotations

from app.providers.longform_assets import AssetProviderError

SCENE_TYPES = ("HOOK", "NARRATION_BROLL", "NARRATION_IMAGE", "NARRATION_GRAPHIC",
               "TALKING_HEAD", "AVATAR", "QUOTE", "DATA_VISUAL", "TIMELINE_GRAPHIC",
               "MAP", "TRANSITION", "CHAPTER_TITLE", "CTA", "OUTRO")


def stage_scene_plan(session, project, job_ctx) -> str:
    from app.models import LongFormChapter
    from app.models.assets import Scene

    chapters = session.query(LongFormChapter).filter(
        LongFormChapter.project_id == project.id).order_by(LongFormChapter.index).all()
    if not chapters:
        raise ValueError("no chapters — run OUTLINE/SCRIPT first")
    ch_ids = [c.id for c in chapters]
    old = session.query(Scene).filter(
        Scene.workspace_id == project.workspace_id).all()
    for row in old:
        if row.chapter_id in ch_ids or (
                project.content_item_id and row.content_item_id == project.content_item_id):
            session.delete(row)
    session.flush()
    n_scenes = 0
    for ch in chapters:
        segments = (ch.script_json or {}).get("segments", [])
        if not segments:
            continue
        groups = _group_segments(segments)
        for g, group in enumerate(groups):
            stype = _scene_type(ch, group, g, len(groups))
            dur = sum(len(s["narration"].split()) * 60.0 / 150 for s in group)
            beats = _visual_beats(stype, dur, ch.index, g)
            session.add(Scene(
                workspace_id=project.workspace_id,
                content_item_id=project.content_item_id,
                chapter_id=ch.id, index=n_scenes,
                title=f"{ch.title} · {stype}"[:200],
                script_segment="\n".join(s["id"] for s in group),
                narration=" ".join(s["narration"] for s in group)[:4000],
                visual_intent=f"{stype}: {group[0]['narration'][:160]}",
                start_seconds=0.0, end_seconds=round(dur, 2),
                beats_json=beats))
            n_scenes += 1
    session.flush()
    project.stage_progress_json = {**(project.stage_progress_json or {}),
                                   "scenes": f"{n_scenes}/{n_scenes}"}
    return f"{n_scenes} scenes, {len(chapters)} chapters"


def _group_segments(segments: list[dict]) -> list[list[dict]]:
    """Pack segments into 30–90s scenes (estimated at narration pace)."""
    groups, current, acc = [], [], 0.0
    for seg in segments:
        est = len(seg["narration"].split()) * 60.0 / 150
        if current and acc + est > 90.0:
            groups.append(current)
            current, acc = [], 0.0
        current.append(seg)
        acc += est
    if current:
        if groups and acc < 20.0:
            groups[-1].extend(current)  # avoid orphan slivers
        else:
            groups.append(current)
    return groups


def _scene_type(chapter, group: list[dict], g: int, n: int) -> str:
    types = {s.get("type") for s in group}
    if g == 0 and chapter.index == 0 and "hook" in types:
        return "HOOK"
    if g == 0 and chapter.index > 0:
        return "CHAPTER_TITLE"
    if "quote" in types:
        return "QUOTE"
    if "cta" in types:
        return "CTA"
    if "evidence" in types:
        return "DATA_VISUAL"
    return "NARRATION_BROLL"


def _visual_beats(stype: str, duration: float, ch_idx: int, g: int) -> list[dict]:
    """Shot-level beats inside one scene (2–6s each). No DB rows per beat."""
    if stype in ("CHAPTER_TITLE", "QUOTE", "DATA_VISUAL"):
        kinds = ["graphic", "broll"]
    elif stype == "HOOK":
        kinds = ["broll", "broll", "graphic"]
    else:
        kinds = ["broll", "image", "broll", "graphic"]
    beats, t, i = [], 0.0, 0
    duration = max(duration, 4.0)
    while t < duration - 0.5:
        length = min(6.0 if i % 3 else 3.5, duration - t)
        beats.append({"index": i, "kind": kinds[i % len(kinds)],
                      "start": round(t, 2), "duration": round(length, 2),
                      "query": ""})
        t += length
        i += 1
    return beats


# -- ASSET PLAN ---------------------------------------------------------------

def stage_asset_plan(session, project, job_ctx) -> str:
    from app.models import LongFormChapter
    from app.models.assets import Scene

    ch_ids = [c.id for c in session.query(LongFormChapter).filter(
        LongFormChapter.project_id == project.id).all()]
    scenes = session.query(Scene).filter(
        Scene.workspace_id == project.workspace_id,
        Scene.chapter_id.in_(ch_ids)).order_by(Scene.index).all() if ch_ids else []
    plan, usage = [], {}
    for sc in scenes:
        primary, fallbacks = _asset_choice(project.budget_strategy, sc.title)
        queries = [f"{project.topic} {(sc.visual_intent or '')[:40]}".strip()[:120]]
        plan.append({"scene_id": sc.id, "scene_index": sc.index,
                     "primary": primary, "fallbacks": fallbacks, "queries": queries})
        for beat in sc.beats_json or []:
            beat["query"] = queries[0]
    session.flush()
    project.asset_plan_json = {"scenes": plan, "usage": usage,
                               "strategy": project.budget_strategy}
    project.stage_progress_json = {**(project.stage_progress_json or {}),
                                   "assets": f"0/{len(plan)}"}
    return f"{len(plan)} scenes planned ({project.budget_strategy})"


def _asset_choice(budget: str, title: str):
    if budget == "ECONOMY":
        return "stock", ["local", "graphic"]
    if budget == "PREMIUM":
        return "ai_video", ["ai_image", "stock", "local", "graphic"]
    return "stock", ["ai_image", "local", "graphic"]


# -- ASSET ACQUIRE --------------------------------------------------------------


def stage_asset_acquire(session, project, job_ctx) -> str:
    from app.models.assets import Scene

    plan = (project.asset_plan_json or {}).get("scenes", [])
    usage = (project.asset_plan_json or {}).get("usage", {})
    done, fallback_count = 0, 0
    for entry in plan:
        sc = session.get(Scene, entry["scene_id"])
        if sc is None:
            continue
        refs = []
        for beat in sc.beats_json or []:
            asset_id, used_fallback, reason = _acquire_beat(
                session, project, sc, beat, entry, usage)
            refs.append({"asset_id": asset_id, "role": f"beat-{beat['index']}",
                         "fallback": used_fallback, "reason": reason})
            if used_fallback:
                fallback_count += 1
        sc.assets_json = refs
        done += 1
        _check(job_ctx)
    session.flush()
    project.asset_plan_json = {**(project.asset_plan_json or {}), "usage": usage}
    project.stage_progress_json = {**(project.stage_progress_json or {}),
                                   "assets": f"{done}/{len(plan)}"}
    return f"{done} scenes acquired, {fallback_count} fallbacks"


def _acquire_beat(session, project, scene, beat: dict, entry: dict, usage: dict):
    """Try primary then fallbacks. Returns (asset_id, used_fallback, reason)."""
    chain = [entry["primary"], *entry.get("fallbacks", [])]
    last_error = "no provider attempted"
    for kind in chain:
        try:
            asset_id = _fetch_kind(session, project, scene, beat, kind, usage)
            used = kind != entry["primary"]
            return asset_id, used, (f"primary {kind}" if not used
                                    else f"fallback to {kind}: {last_error}")
        except AssetProviderError as exc:
            last_error = str(exc)[:160]
            continue
    raise AssetProviderError(f"all providers failed for scene {scene.index}: {last_error}")


def _fetch_kind(session, project, scene, beat: dict, kind: str, usage: dict) -> str:
    from app.providers.longform_assets import get_asset_provider

    provider = get_asset_provider(kind)
    asset = provider.fetch(session, project, scene, beat)
    key = asset.id
    usage[key] = usage.get(key, 0) + 1
    return key


def _check(job_ctx) -> None:
    if job_ctx is not None:
        from app.services import jobs as _jobs

        _jobs.check_cancelled(job_ctx)
