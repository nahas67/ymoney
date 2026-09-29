"""Quality gates for derived shorts and whole campaigns."""

from __future__ import annotations

HOOK_WINDOW_SECONDS = 3.5
DURATION_PASS_MIN = 20.0
DURATION_PASS_MAX = 60.0
DURATION_FAIL_MIN = 15.0
DURATION_FAIL_MAX = 70.0
OVERLAP_FAIL = 0.8
OVERLAP_WARN = 0.6


def _check(status: str, detail: str = "") -> dict:
    return {"status": status, "detail": detail}


def short_qc(doc: dict, file_meta: dict | None = None) -> dict:
    """QC one short timeline doc.

    Returns {result: PASS|PASS_WITH_WARNINGS|FAIL, checks: {...}} over
    duration / aspect / audio / captions / hook / safe_zone /
    duplicate_overlap.
    """
    meta = dict(file_meta or {})
    checks: dict[str, dict] = {}

    duration = doc.get("duration_seconds", 0.0)
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 0.0
    if duration < DURATION_FAIL_MIN or duration > DURATION_FAIL_MAX:
        checks["duration"] = _check("fail", f"{duration:.1f}s outside 15-70s window")
    elif duration < DURATION_PASS_MIN or duration > DURATION_PASS_MAX:
        checks["duration"] = _check("warning", f"{duration:.1f}s outside ideal 20-60s band")
    else:
        checks["duration"] = _check("pass", f"{duration:.1f}s")

    aspect = doc.get("aspect_ratio", "")
    if aspect == "9:16":
        checks["aspect"] = _check("pass", aspect)
    else:
        checks["aspect"] = _check("fail", f"'{aspect}' is not vertical 9:16")

    has_audio = meta.get("has_audio")
    if has_audio is True:
        checks["audio"] = _check("pass", "audio track present")
    elif has_audio is False:
        checks["audio"] = _check("fail", "no audio track")
    else:
        checks["audio"] = _check("warning", "audio presence unknown")

    caption_clips = _track_clips(doc, "caption")
    if caption_clips:
        checks["captions"] = _check("pass", f"{len(caption_clips)} caption clip(s)")
    else:
        checks["captions"] = _check("fail", "no caption clips")

    hook_ok = False
    for c in _track_clips(doc, "text"):
        try:
            clip_start = float(c.get("start", 99.0))
        except (TypeError, ValueError):
            continue
        label = f"{c.get('id', '')} {c.get('name', '')}".lower()
        if "hook" in label and clip_start <= HOOK_WINDOW_SECONDS:
            hook_ok = True
            break
    if hook_ok:
        checks["hook"] = _check("pass", "hook opens within 3.5s")
    else:
        checks["hook"] = _check("fail", "no hook clip in the first 3.5s")

    unsafe = [
        c.get("id", "?") for c in _track_clips(doc, "text") + caption_clips
        if _outside_safe_zone(c)
    ]
    if unsafe:
        checks["safe_zone"] = _check("fail", f"clips outside safe zone: {unsafe[:3]}")
    else:
        checks["safe_zone"] = _check("pass", "text within safe zone")

    overlap = meta.get("max_transcript_overlap", 0.0)
    try:
        overlap = float(overlap)
    except (TypeError, ValueError):
        overlap = 0.0
    if overlap > OVERLAP_FAIL:
        checks["duplicate_overlap"] = _check("fail", f"overlap {overlap:.0%} above 80%")
    elif overlap > OVERLAP_WARN:
        checks["duplicate_overlap"] = _check("warning", f"overlap {overlap:.0%} above 60%")
    else:
        checks["duplicate_overlap"] = _check("pass", f"overlap {overlap:.0%}")

    statuses = {c["status"] for c in checks.values()}
    if "fail" in statuses:
        result = "FAIL"
    elif "warning" in statuses:
        result = "PASS_WITH_WARNINGS"
    else:
        result = "PASS"
    return {"result": result, "checks": checks}


def campaign_qc(session, campaign_id: str) -> dict:
    """Roll-up QC: counts, diversity, platform coverage, metadata completeness."""
    from app.engine.campaign.diversity import transcript_overlap
    from app.models import ContentItem, ContentTimeline
    from app.models.campaign import CampaignPlan, PlatformVariant

    plan = session.query(CampaignPlan).filter(
        CampaignPlan.campaign_id == campaign_id,
    ).one_or_none()
    if plan is None:
        return {
            "result": "FAIL", "error": f"no campaign plan for '{campaign_id}'",
            "counts": {"shorts": 0, "timelines": 0, "variants": 0, "platforms": 0},
            "diversity": {"chapters_covered": 0, "pairwise_overlap_max": 0.0,
                          "shorts_checked": 0},
            "platform_coverage": {"expected": [], "present": {}, "missing": []},
            "metadata_completeness": {"complete": 0, "total": 0, "ratio": 0.0},
        }

    ws_id = plan.workspace_id
    shorts = session.query(ContentItem).filter(
        ContentItem.workspace_id == ws_id,
        ContentItem.campaign_id == campaign_id,
        ContentItem.derivation_type == "short",
    ).all()
    short_ids = [s.id for s in shorts]
    timelines = (
        session.query(ContentTimeline).filter(
            ContentTimeline.workspace_id == ws_id,
            ContentTimeline.content_item_id.in_(short_ids),
        ).all() if short_ids else []
    )
    variants = session.query(PlatformVariant).filter(
        PlatformVariant.workspace_id == ws_id,
        PlatformVariant.campaign_id == campaign_id,
    ).all()

    texts, chapters = _diversity_inputs(session, ws_id, shorts)
    overlap_max = 0.0
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            overlap_max = max(overlap_max, transcript_overlap(texts[i], texts[j]))

    expected = list(plan.target_platforms or [])
    present: dict[str, int] = {}
    for v in variants:
        present[v.platform] = present.get(v.platform, 0) + 1
    missing = [p for p in expected if p not in present]

    complete = sum(1 for v in variants if v.metadata_json)
    total = len(variants)

    # Brand consistency (Work 08 Lane C): Lane A's verifier folded into the
    # rollup as a `brand` check. A verifier crash maps to a WARNING (it can
    # never fail a cycle on its own); when the brand module is absent the
    # check is omitted entirely and QC is byte-identical to pre-brand QC.
    brand_check: dict | None = None
    try:
        from app.engine.brand_templates import brand_qc_check

        corpus = " ".join(texts)
        brand_check = brand_qc_check(
            session, ws_id, campaign_id=campaign_id,
            artifact={"text": corpus, "artifact_kind": "short"})
    except Exception as exc:  # noqa: BLE001 — never fail a cycle here
        brand_check = {"status": "warning",
                       "detail": f"brand QC unavailable: {type(exc).__name__}: {exc}"}
    brand_status = (brand_check or {}).get("status") or ""

    if not shorts or overlap_max > OVERLAP_FAIL or brand_status == "fail":
        result = "FAIL"
    elif brand_status == "review":
        result = "REVIEW_REQUIRED"
    elif missing or complete < total or len(timelines) < len(shorts) \
            or brand_status == "warning":
        result = "PASS_WITH_WARNINGS"
    else:
        result = "PASS"
    out = {
        "result": result,
        "counts": {"shorts": len(shorts), "timelines": len(timelines),
                   "variants": total, "platforms": len(present)},
        "diversity": {"chapters_covered": len(chapters),
                      "pairwise_overlap_max": round(overlap_max, 3),
                      "shorts_checked": len(texts)},
        "platform_coverage": {"expected": expected, "present": present,
                              "missing": missing},
        "metadata_completeness": {"complete": complete, "total": total,
                                  "ratio": round(complete / total, 3) if total else 0.0},
    }
    if brand_check:
        out["checks"] = {"brand": brand_check}
    return out


def _track_clips(doc: dict, kind: str) -> list[dict]:
    for track in (doc or {}).get("tracks", []):
        if track.get("kind") == kind:
            clips = track.get("clips", [])
            return list(clips) if isinstance(clips, list) else []
    return []


def _outside_safe_zone(clip: dict) -> bool:
    """Text pinned outside the vertical safe band (top 12% / bottom 18%)."""
    transform = clip.get("transform") or {}
    y = transform.get("y")
    if y is None:
        return False
    try:
        y = float(y)
    except (TypeError, ValueError):
        return False
    return y < 0.12 or y > 0.82


def _diversity_inputs(session, ws_id: str, shorts: list) -> tuple[list[str], set[str]]:
    """(per-short transcript texts, chapters covered via parent scenes)."""
    from app.models import Scene

    texts: list[str] = []
    chapters: set[str] = set()
    for short in shorts:
        scenes = session.query(Scene).filter(
            Scene.workspace_id == ws_id,
            Scene.content_item_id == short.id,
        ).all()
        words: list[str] = []
        for scene in scenes:
            if scene.script_segment:
                words.append(scene.script_segment)
            parent = (
                session.get(Scene, scene.parent_scene_id)
                if scene.parent_scene_id else None
            )
            if parent is not None and parent.chapter_id:
                chapters.add(parent.chapter_id)
        if words:
            texts.append(" ".join(words))
    return texts, chapters
