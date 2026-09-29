"""Creative feature extraction (Work 06 Lane B).

Deterministic and metadata-first: every value is read from existing rows
(ContentItem topic/strategy/research, ContentTimeline tracks, Scene rows,
PlatformVariant metadata, MediaAsset cover, ScheduleEntry run_at) and typed
into a CreativeFeature record dict.

Doctrine:
- No semantic guessing. The only classifier used is the rule-based
  ``classify_hook`` from the campaign hook doctrine, applied to the actual
  stored hook text.
- Semantic inference may ONLY come from the DecisionEngine
  classify/compare pair in SHADOW mode, imported lazily so Lane A/C absence
  never breaks extraction. Inferred fields are marked ``inferred: true``.
- Voice metadata is surfaced ONLY when explicitly configured on the voice
  clip payload (voice_id/voice_style). Gender, age, race or any other
  sensitive personal attribute is never extracted or inferred — those keys
  never appear in the output, even if present in a source payload.
- Persistence targets Lane A's ``creative_features`` table via ORM import
  with graceful fallback: when the model or table is missing, the feature
  dict is returned without persisting and extraction still succeeds.
"""

from __future__ import annotations

from sqlalchemy import inspect, select

from app.engine.campaign.hooks import classify_hook
from app.models.assets import MediaAsset, Scene
from app.models.campaign import PlatformVariant
from app.models.content import ContentItem, ScheduleEntry, VideoVariant
from app.models.timeline import ContentTimeline

#: Retention doctrine: scripts run ~2.6 words/second (Script Agent pacing).
WORDS_PER_SECOND = 2.6

#: Sensitive attributes that must never appear in extracted features.
_FORBIDDEN_KEY_FRAGMENTS = ("gender", "age", "race", "ethnicity", "religion")


def _workspace_id(ws) -> str:
    return getattr(ws, "id", None) or str(ws)


def _tracks_by_kind(doc: dict) -> dict[str, list]:
    out: dict[str, list] = {}
    for track in (doc or {}).get("tracks", []) or []:
        kind = str(track.get("kind", ""))
        out.setdefault(kind, []).extend(track.get("clips", []) or [])
    for clips in out.values():
        clips.sort(key=lambda c: (float(c.get("start", 0.0)), str(c.get("id", ""))))
    return out


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip_text(clip: dict) -> dict:
    text = clip.get("text")
    return dict(text) if isinstance(text, dict) else {}


def _caption_style(clips: list) -> str | None:
    """Caption style from the caption clip payload only (preset/style keys)."""
    for clip in clips:
        source = clip.get("source") or {}
        if isinstance(source, dict):
            for key in ("preset", "style", "caption_style"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:80]
    return None


def _caption_position(clips: list) -> str | None:
    """Caption position from explicit clip metadata (position/align keys)."""
    for clip in clips:
        source = clip.get("source") or {}
        if isinstance(source, dict):
            value = source.get("position")
            if isinstance(value, str) and value.strip():
                return value.strip()[:40]
        text = _clip_text(clip)
        for key in ("align", "position", "anchor"):
            value = text.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:40]
    return None


def _voice_field(clips: list, keys: tuple[str, ...]) -> str | None:
    """Voice metadata ONLY when explicitly configured on the clip source.

    Only whitelisted non-sensitive keys are read; gender/age/race (or any
    other personal attribute) is never surfaced, even if configured.
    """
    for clip in clips:
        source = clip.get("source") or {}
        if not isinstance(source, dict):
            continue
        for key in keys:
            if any(frag in key.lower() for frag in _FORBIDDEN_KEY_FRAGMENTS):
                continue
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:120]
    return None


def _music_style(clips: list) -> str | None:
    for clip in clips:
        source = clip.get("source") or {}
        if isinstance(source, dict):
            for key in ("style", "genre", "mood"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:80]
    return None


def _shadow_semantic_label(text: str) -> tuple[str | None, bool]:
    """Semantic label via DecisionEngine classify/compare in SHADOW mode.

    Returns (label, inferred). The DecisionEngine pair is imported lazily;
    when it is absent (Lane A/C not present) this returns (None, False) and
    extraction continues on metadata alone.
    """
    candidate = (text or "").strip()
    if not candidate:
        return None, False
    try:
        from app.engine import decision as decision_mod

        classify = getattr(decision_mod, "classify", None)
        compare = getattr(decision_mod, "compare", None)
        if not callable(classify) or not callable(compare):
            return None, False
        label = classify(candidate, mode="SHADOW")
        shadow = compare(candidate, mode="SHADOW")
        if not isinstance(label, str) or not label.strip():
            return None, False
        if isinstance(shadow, dict) and shadow.get("agree") is False:
            return None, False
        return label.strip()[:80], True
    except Exception:
        return None, False


def _persist_features(session, workspace_id: str, content_item_id: str, features: dict) -> bool:
    """Best-effort upsert into Lane A's creative_features table.

    Introspects the table so unknown Lane A schemas never break Lane B:
    only columns that exist are written; the full dict goes into a JSON
    column when one exists. Any failure returns False (no persist).
    """
    try:
        try:
            from app.models.performance import CreativeFeature
        except ImportError:
            from app.models import CreativeFeature
        table = CreativeFeature.__table__
        if table.name not in inspect(session.bind).get_table_names():
            return False
        cols = set(table.columns.keys())
        lookup: dict = {}
        if "workspace_id" in cols:
            lookup["workspace_id"] = workspace_id
        if "content_item_id" in cols:
            lookup["content_item_id"] = content_item_id
        existing = None
        if len(lookup) == 2:
            existing = session.scalar(
                select(CreativeFeature).where(
                    CreativeFeature.workspace_id == workspace_id,
                    CreativeFeature.content_item_id == content_item_id,
                )
            )
        values = {
            key: val
            for key, val in features.items()
            if key in cols and key not in ("id", "workspace_id", "content_item_id")
        }
        for json_col in ("features_json", "features", "payload", "data_json"):
            if json_col in cols:
                values[json_col] = dict(features)
                break
        if "extracted_at" in cols and "extracted_at" not in values:
            from app.models.base import utcnow

            values["extracted_at"] = utcnow()
        if existing is None:
            session.add(CreativeFeature(**{**lookup, **values}))
        else:
            for key, val in values.items():
                setattr(existing, key, val)
        session.flush()
    except Exception:
        return False
    return True


def extract_features(session, ws, content_item_id: str) -> dict:
    """Extract deterministic typed creative features for a content item.

    Returns the CreativeFeature record dict. Persists to Lane A's
    ``creative_features`` table on a best-effort basis; when that table is
    missing the dict is returned without persisting.
    """
    workspace_id = _workspace_id(ws)
    item = session.get(ContentItem, str(content_item_id))
    if item is None or item.workspace_id != workspace_id:
        raise LookupError("content item not found")

    strategy = dict(item.strategy_json or {})
    inferred: dict[str, bool] = {}

    variant = session.scalar(
        select(VideoVariant)
        .where(VideoVariant.content_item_id == item.id)
        .order_by(VideoVariant.selected.desc(), VideoVariant.created_at.asc(), VideoVariant.id.asc())
        .limit(1)
    )
    hook_text = ((variant.hook if variant else "") or "").strip()
    if hook_text:
        hook_type: str | None = classify_hook(hook_text)
        hook_duration: float | None = round(len(hook_text.split()) / WORDS_PER_SECOND, 2)
    else:
        raw_strategy_hook = str(strategy.get("hook_type", "") or "").strip()
        hook_type = raw_strategy_hook.upper()[:40] or None
        hook_duration = None

    timeline = session.scalar(
        select(ContentTimeline)
        .where(
            ContentTimeline.workspace_id == workspace_id,
            ContentTimeline.content_item_id == item.id,
        )
        .order_by(ContentTimeline.version.desc(), ContentTimeline.created_at.desc())
        .limit(1)
    )
    tracks = _tracks_by_kind(dict(timeline.tracks_json or {}) if timeline else {})
    # A-roll edits define cuts and shot lengths; b-roll is overlay and is
    # measured separately via broll_density (it never creates a "cut").
    video_clips = list(tracks.get("video", []))
    shot_lengths = [_num(c.get("duration")) for c in video_clips if _num(c.get("duration")) > 0]
    if shot_lengths:
        average_shot_length: float | None = round(sum(shot_lengths) / len(shot_lengths), 2)
    else:
        average_shot_length = None
    if len(video_clips) >= 2:
        first_cut_time: float | None = round(_num(video_clips[1].get("start")), 2)
    else:
        first_cut_time = None

    caption_clips = tracks.get("caption", [])
    caption_style = _caption_style(caption_clips)
    caption_position = _caption_position(caption_clips)

    voice_clips = tracks.get("voice", [])
    voice_id = _voice_field(voice_clips, ("voice_id", "voice", "voice_name"))
    voice_style = _voice_field(voice_clips, ("voice_style", "style"))

    music_clips = tracks.get("music", [])
    music_presence = bool(music_clips)
    music_style = _music_style(music_clips)
    avatar_presence = bool(tracks.get("avatar", []))

    timeline_duration = _num((timeline.duration_seconds if timeline else 0.0) or 0.0)
    video_clip_total = sum(_num(c.get("duration")) for c in video_clips)
    broll_total = sum(_num(c.get("duration")) for c in tracks.get("broll", []))
    denom = timeline_duration or video_clip_total
    broll_density: float | None = round(broll_total / denom, 3) if denom > 0 else None

    scenes = session.scalars(
        select(Scene)
        .where(Scene.workspace_id == workspace_id, Scene.content_item_id == item.id)
        .order_by(Scene.index.asc(), Scene.id.asc())
    ).all()
    scene_count = len(scenes)
    chapter: str | None = next(
        (s.chapter_id for s in scenes if s.chapter_id), None
    )

    platform_variant = session.scalar(
        select(PlatformVariant)
        .where(
            PlatformVariant.workspace_id == workspace_id,
            PlatformVariant.short_content_id == item.id,
        )
        .order_by(PlatformVariant.created_at.asc(), PlatformVariant.id.asc())
        .limit(1)
    )
    variant_meta = dict(platform_variant.metadata_json or {}) if platform_variant else {}
    cta_type = variant_meta.get("cta_kind")
    cta_type = str(cta_type).strip()[:40] or None if cta_type is not None else None
    title_style = variant_meta.get("title_style")
    if isinstance(title_style, str) and title_style.strip():
        title_style = title_style.strip()[:80]
    else:
        label, was_inferred = _shadow_semantic_label(variant_meta.get("title", ""))
        title_style = label
        if was_inferred and title_style:
            inferred["title_style"] = True
    thumbnail_style = variant_meta.get("thumbnail_style")
    if isinstance(thumbnail_style, str) and thumbnail_style.strip():
        thumbnail_style = thumbnail_style.strip()[:80]
    else:
        cover_id = platform_variant.cover_asset_id if platform_variant else None
        if cover_id and session.get(MediaAsset, cover_id) is not None:
            thumbnail_style = "custom"
        else:
            thumbnail_style = None

    entry = session.scalar(
        select(ScheduleEntry)
        .where(
            ScheduleEntry.workspace_id == workspace_id,
            ScheduleEntry.content_item_id == item.id,
        )
        .order_by(ScheduleEntry.run_at.asc())
        .limit(1)
    )
    posting_time: str | None = (
        entry.run_at.isoformat() + "Z" if entry is not None and entry.run_at else None
    )

    if timeline_duration > 0:
        video_duration: float | None = round(timeline_duration, 2)
    elif isinstance(strategy.get("duration_seconds"), (int, float)):
        video_duration = round(float(strategy["duration_seconds"]), 2)
    else:
        video_duration = None
    aspect_ratio = (
        ((timeline.tracks_json or {}).get("aspect_ratio") if timeline else None)
        or str(strategy.get("aspect_ratio", "") or "").strip()
        or "9:16"
    )

    features = {
        "hook_type": hook_type,
        "hook_duration": hook_duration,
        "first_cut_time": first_cut_time,
        "average_shot_length": average_shot_length,
        "scene_count": scene_count,
        "caption_style": caption_style,
        "caption_position": caption_position,
        "voice_id": voice_id,
        "voice_style": voice_style,
        "music_presence": music_presence,
        "music_style": music_style,
        "broll_density": broll_density,
        "avatar_presence": avatar_presence,
        "cta_type": cta_type,
        "video_duration": video_duration,
        "aspect_ratio": aspect_ratio,
        "posting_time": posting_time,
        "title_style": title_style,
        "thumbnail_style": thumbnail_style,
        "chapter": chapter,
        "topic": item.topic,
        "inferred": inferred,
    }
    _persist_features(session, workspace_id, item.id, features)
    return features
