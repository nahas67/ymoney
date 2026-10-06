"""Campaign analytics (Work 04, Lane C).

Attribution chain: Publication → Variant → Short → Master → Campaign.

Rollups aggregate the latest :class:`PostMetric` snapshot per published post
(last snapshot wins, same convention as the Learning Agent). Rates:

- ``engagement_rate`` = (likes + comments + shares + saves) / views
- ``completion`` = views-weighted mean of ``completion_rate``

Forward compatibility with Lane A: ``PublishedPost.platform_variant_id`` /
``PublishedPost.campaign_id`` columns and the ``platform_variants`` /
``campaign_plans`` / ``publishing_plans`` tables may not exist yet. Every
access to them goes through defensive ``getattr`` / metadata lookups, so this
module works both before and after the Lane A migration. Variant linkage also
resolves through the pre-existing path
``PublishedPost.content_item_id → ContentItem (+ VideoVariant)`` lineage,
which is what the tests seed.
"""

from __future__ import annotations

from sqlalchemy import select, text

from app.services import cost as cost_vocab
from app.db import Base
from app.models import (
    Campaign,
    ContentItem,
    LongFormChapter,
    PostMetric,
    PublishedPost,
    Scene,
    VideoVariant,
)

_ROLLUP_KEYS = (
    "views",
    "watch_time",
    "likes",
    "comments",
    "shares",
    "saves",
    "engagement_rate",
    "completion",
)


def empty_rollup() -> dict:
    """UNAVAILABLE rollup carrying the contract keys.

    HONESTY (Work 16.5.7 §8). Every key is ``None``, not ``0``. These values
    used to be the zero initialiser of an accumulator that only ever ADDS a
    row for a post with a ``PostMetric`` snapshot, so a rollup over posts that
    were published but never measured reported ``views: 0`` -- "nobody watched"
    instead of "nobody reported". ``None`` is UNAVAILABLE and renders as such.

    A real measured zero still arrives as ``0``: ``_accumulate`` promotes these
    to numbers the moment a snapshot exists, and a snapshot that genuinely
    recorded 0 views sums to 0.
    """
    return {
        "views": None,
        "watch_time": None,
        "likes": None,
        "comments": None,
        "shares": None,
        "saves": None,
        "engagement_rate": None,
        "completion": None,
    }


def _accumulate(metrics: list[PostMetric]) -> dict:
    """Sum snapshots and derive engagement/completion rates.

    The two rates are DERIVED and are ``None`` -- not ``0.0`` -- when they
    cannot be computed. ``engagement_rate`` needs a non-zero view count to be a
    ratio at all. ``completion`` additionally drops rows whose
    ``completion_rate`` is NULL: a weighted mean that counted a NULL as 0.0
    silently averaged "the provider never reported completion" into the same
    bucket as "the provider reported that nobody finished the video".
    """
    out = empty_rollup()
    if not metrics:
        return out
    views = sum(int(m.views or 0) for m in metrics)
    out["views"] = views
    out["watch_time"] = round(sum(float(m.watch_time_seconds or 0.0) for m in metrics), 2)
    out["likes"] = sum(int(m.likes or 0) for m in metrics)
    out["comments"] = sum(int(m.comments or 0) for m in metrics)
    out["shares"] = sum(int(m.shares or 0) for m in metrics)
    out["saves"] = sum(int(m.saves or 0) for m in metrics)
    if views > 0:
        out["engagement_rate"] = round(
            (out["likes"] + out["comments"] + out["shares"] + out["saves"]) / views, 4
        )
        # Only rows that REPORTED a completion rate carry a weight. A NULL
        # completion_rate is not a zero completion, so it contributes nothing to
        # either the numerator or the denominator.
        completion = cost_vocab.derived_mean([
            (float(m.completion_rate or 0.0), float(m.views or 0))
            for m in metrics
            if m.completion_rate is not None
        ])
        out["completion"] = round(completion, 4) if completion is not None else None
    return out


def latest_metrics(session, post_ids: list[str]) -> dict[str, PostMetric]:
    """Latest metric snapshot per post (last ``captured_at`` wins)."""
    if not post_ids:
        return {}
    rows = session.scalars(
        select(PostMetric)
        .where(PostMetric.post_id.in_(post_ids))
        .order_by(PostMetric.captured_at.asc())
    ).all()
    latest: dict[str, PostMetric] = {}
    for m in rows:  # ascending → last snapshot wins
        latest[m.post_id] = m
    return latest


def _lane_a_table(name: str):
    """Return the Lane A table object if its model is registered, else None."""
    return Base.metadata.tables.get(name)


def _safe_text_rows(session, stmt: str, params: dict) -> list:
    """Run raw SQL (Lane A columns); [] when the columns do not exist yet.

    Uses a savepoint so a missing-column error never poisons the caller's
    transaction.
    """
    try:
        with session.begin_nested():
            return list(session.execute(text(stmt), params).all())
    except Exception:
        return []


def _new_col_post_ids(
    session,
    workspace_id: str,
    *,
    campaign_id: str | None = None,
    variant_id: str | None = None,
) -> list[str]:
    """Post ids via Lane A ``published_posts`` lineage columns (migration 0016)."""
    q = "SELECT id FROM published_posts WHERE workspace_id = :ws"
    params: dict = {"ws": workspace_id}
    if campaign_id is not None:
        q += " AND campaign_id = :cid"
        params["cid"] = campaign_id
    if variant_id is not None:
        q += " AND platform_variant_id = :vid"
        params["vid"] = variant_id
    return [r[0] for r in _safe_text_rows(session, q, params)]


def _post_lineage_cols(session, post_id: str) -> dict:
    """Lane A lineage columns for one post; {} when absent."""
    rows = _safe_text_rows(
        session,
        "SELECT platform_variant_id, campaign_id FROM published_posts WHERE id = :pid",
        {"pid": post_id},
    )
    if not rows:
        return {}
    return {"platform_variant_id": rows[0][0], "campaign_id": rows[0][1]}


def _variant_row(session, variant_id: str) -> dict:
    """Lane A ``platform_variants`` row as a dict; {} when absent."""
    table = _lane_a_table("platform_variants")
    if table is None:
        return {}
    try:
        row = session.execute(select(table).where(table.c.id == variant_id)).mappings().first()
        return dict(row) if row is not None else {}
    except Exception:
        return {}


def _lane_a_rows(session, workspace_id: str, table_name: str, **filters) -> list:
    """Fetch Lane A rows generically; [] when the table does not exist yet."""
    table = _lane_a_table(table_name)
    if table is None:
        return []
    try:
        q = select(table)
        if "workspace_id" in table.c:
            q = q.where(table.c.workspace_id == workspace_id)
        for key, val in filters.items():
            if key in table.c:
                q = q.where(table.c[key] == val)
        return list(session.execute(q).mappings().all())
    except Exception:
        return []


def campaign_items(session, workspace_id: str, campaign_id: str) -> list[ContentItem]:
    """All content items belonging to a campaign (workspace-scoped)."""
    return list(
        session.scalars(
            select(ContentItem).where(
                ContentItem.workspace_id == workspace_id,
                ContentItem.campaign_id == campaign_id,
            )
        ).all()
    )


def _split_master_shorts(
    items: list[ContentItem],
) -> tuple[list[ContentItem], list[ContentItem]]:
    """Split campaign items into (masters, shorts) via lineage.

    Shorts are derived assets (``parent_content_id`` or ``derivation_type``
    set); everything else is a master/root candidate.
    """
    shorts = [i for i in items if i.parent_content_id or i.derivation_type]
    masters = [i for i in items if not (i.parent_content_id or i.derivation_type)]
    return masters, shorts


def campaign_master_id(session, workspace_id: str, campaign_id: str) -> str | None:
    """Resolve the campaign's master content id.

    Prefers Lane A's ``campaign_plans.master_content_id`` when present,
    otherwise the first root (non-derived) campaign item.
    """
    plans = _lane_a_rows(
        session, workspace_id, "campaign_plans", campaign_id=campaign_id
    )
    for p in plans:
        for key in ("master_content_id", "master_id", "content_item_id"):
            if p.get(key):
                return p[key]
    masters, _shorts = _split_master_shorts(
        campaign_items(session, workspace_id, campaign_id)
    )
    return masters[0].id if masters else None


def _posts_for_content_ids(
    session, workspace_id: str, content_ids: list[str]
) -> list[PublishedPost]:
    if not content_ids:
        return []
    return list(
        session.scalars(
            select(PublishedPost).where(
                PublishedPost.workspace_id == workspace_id,
                PublishedPost.content_item_id.in_(content_ids),
            )
        ).all()
    )


def campaign_posts(
    session, workspace_id: str, campaign_id: str
) -> list[PublishedPost]:
    """All published posts attributable to a campaign.

    Uses the Lane A ``PublishedPost.campaign_id`` column (ORM-mapped or
    migration-0016 raw column) when present, plus posts linked through
    campaign content items (covers master direct publications and shorts).
    """
    by_column: list[PublishedPost] = []
    if hasattr(PublishedPost, "campaign_id"):
        by_column = list(
            session.scalars(
                select(PublishedPost).where(
                    PublishedPost.workspace_id == workspace_id,
                    PublishedPost.campaign_id == campaign_id,
                )
            ).all()
        )
    else:
        ids = _new_col_post_ids(session, workspace_id, campaign_id=campaign_id)
        if ids:
            by_column = list(
                session.scalars(
                    select(PublishedPost).where(
                        PublishedPost.workspace_id == workspace_id,
                        PublishedPost.id.in_(ids),
                    )
                ).all()
            )
    item_ids = [i.id for i in campaign_items(session, workspace_id, campaign_id)]
    by_items = _posts_for_content_ids(session, workspace_id, item_ids)
    seen: dict[str, PublishedPost] = {}
    for p in [*by_column, *by_items]:
        seen[p.id] = p
    return list(seen.values())


def _variant_content_id(session, variant_id: str) -> str | None:
    """Resolve a variant id → short content id.

    Lane A ``platform_variants.short_content_id`` first, then generic
    fallbacks, then the pre-existing ``VideoVariant`` table.
    """
    row = _variant_row(session, variant_id)
    if row:
        for key in ("short_content_id", "content_item_id", "content_id", "short_id"):
            if row.get(key):
                return row[key]
    variant = session.get(VideoVariant, variant_id)
    return variant.content_item_id if variant is not None else None


def _posts_for_variant(
    session, workspace_id: str, variant_id: str
) -> list[PublishedPost]:
    """Posts published from one variant.

    Lane A ``platform_variants`` rows are platform-scoped: posts linked via
    ``published_posts.platform_variant_id`` are authoritative; when none
    exist yet, fall back to the short's posts on the variant's platform.
    ``VideoVariant`` ids (content-level) aggregate all of the short's posts.
    """
    lane_a = _variant_row(session, variant_id)
    if lane_a:
        ids = _new_col_post_ids(session, workspace_id, variant_id=variant_id)
        posts: list[PublishedPost] = []
        if ids:
            posts = list(
                session.scalars(
                    select(PublishedPost).where(
                        PublishedPost.workspace_id == workspace_id,
                        PublishedPost.id.in_(ids),
                    )
                ).all()
            )
        if not posts and lane_a.get("short_content_id"):
            lineage = _posts_for_content_ids(
                session, workspace_id, [lane_a["short_content_id"]]
            )
            platform = lane_a.get("platform")
            posts = [p for p in lineage if not platform or p.platform == platform]
        return posts
    posts = []
    if hasattr(PublishedPost, "platform_variant_id"):
        posts = list(
            session.scalars(
                select(PublishedPost).where(
                    PublishedPost.workspace_id == workspace_id,
                    PublishedPost.platform_variant_id == variant_id,
                )
            ).all()
        )
    content_id = _variant_content_id(session, variant_id)
    if content_id:
        for p in _posts_for_content_ids(session, workspace_id, [content_id]):
            if all(existing.id != p.id for existing in posts):
                posts.append(p)
    return posts


def rollup_variant(session, workspace_id: str, variant_id: str) -> dict:
    """Roll up one platform variant → contract metric dict."""
    posts = _posts_for_variant(session, workspace_id, variant_id)
    metrics = latest_metrics(session, [p.id for p in posts])
    out = _accumulate([metrics[p.id] for p in posts if p.id in metrics])
    out["variant_id"] = variant_id
    out["posts"] = len(posts)
    return out


def rollup_short(session, workspace_id: str, short_id: str) -> dict:
    """Roll up one short (ContentItem) aggregating its variants + direct posts."""
    variant_ids = [
        v.id
        for v in session.scalars(
            select(VideoVariant).where(VideoVariant.content_item_id == short_id)
        ).all()
    ]
    table = _lane_a_table("platform_variants")
    if table is not None and "short_content_id" in table.c:
        try:
            rows = session.execute(
                select(table.c.id).where(table.c.short_content_id == short_id)
            ).all()
            variant_ids.extend(r[0] for r in rows if r[0] not in variant_ids)
        except Exception:
            pass
    posts: list[PublishedPost] = []
    for vid in variant_ids:
        for p in _posts_for_variant(session, workspace_id, vid):
            if all(existing.id != p.id for existing in posts):
                posts.append(p)
    for p in _posts_for_content_ids(session, workspace_id, [short_id]):
        if all(existing.id != p.id for existing in posts):
            posts.append(p)
    metrics = latest_metrics(session, [p.id for p in posts])
    out = _accumulate([metrics[p.id] for p in posts if p.id in metrics])
    out["short_id"] = short_id
    out["posts"] = len(posts)
    out["variants"] = {
        vid: rollup_variant(session, workspace_id, vid) for vid in variant_ids
    }
    return out


def rollup_campaign(session, workspace_id: str, campaign_id: str) -> dict:
    """Roll up a campaign: shorts + master totals with per-short breakdown."""
    campaign = session.get(Campaign, campaign_id)
    if campaign is None or campaign.workspace_id != workspace_id:
        raise ValueError("campaign not found")
    items = campaign_items(session, workspace_id, campaign_id)
    masters, shorts = _split_master_shorts(items)
    master_id = campaign_master_id(session, workspace_id, campaign_id)
    master_rollup: dict | None = None
    if master_id is not None:
        master_rollup = rollup_short(session, workspace_id, master_id)
    short_rollups = [rollup_short(session, workspace_id, s.id) for s in shorts]
    # Campaign-wide post coverage (e.g. Lane A direct campaign_id posts) is
    # unioned here so nothing attributable is dropped.
    all_posts = campaign_posts(session, workspace_id, campaign_id)
    metrics = latest_metrics(session, [p.id for p in all_posts])
    totals = _accumulate([metrics[p.id] for p in all_posts if p.id in metrics])
    return {
        "campaign_id": campaign_id,
        "master_content_id": master_id,
        "master": master_rollup,
        "shorts": short_rollups,
        "short_count": len(shorts),
        "totals": totals,
        "post_count": len(all_posts),
        "platforms": sorted({p.platform for p in all_posts}),
    }


def compare_platforms(session, workspace_id: str, campaign_id: str) -> dict:
    """Per-platform totals for a campaign (platform → rollup dict)."""
    posts = campaign_posts(session, workspace_id, campaign_id)
    by_platform: dict[str, list[PublishedPost]] = {}
    for p in posts:
        by_platform.setdefault(p.platform, []).append(p)
    result: dict[str, dict] = {}
    for platform in sorted(by_platform):
        group = by_platform[platform]
        metrics = latest_metrics(session, [p.id for p in group])
        out = _accumulate([metrics[p.id] for p in group if p.id in metrics])
        out["posts"] = len(group)
        result[platform] = out
    return result


def _short_chapter(session, short: ContentItem) -> tuple[str | None, str]:
    """Resolve (chapter_id, chapter_title) for a short.

    Order: Scene rows on the short → LongFormChapter title; explicit
    ``chapter`` marker in strategy/research JSON; graceful fallback to
    per-short grouping (chapter_id None).
    """
    scenes = list(
        session.scalars(
            select(Scene).where(Scene.content_item_id == short.id).order_by(Scene.index.asc())
        ).all()
    )
    for sc in scenes:
        if sc.chapter_id:
            chapter = session.get(LongFormChapter, sc.chapter_id)
            title = chapter.title if chapter and chapter.title else sc.title or sc.chapter_id
            return sc.chapter_id, title
    for blob in (short.strategy_json or {}, short.research_json or {}):
        marker = blob.get("chapter") if isinstance(blob, dict) else None
        if marker:
            label = marker if isinstance(marker, str) else str(marker)
            return label, label
    return None, f"short:{short.id[:8]}"


def chapter_performance(session, workspace_id: str, campaign_id: str) -> list[dict]:
    """Per-chapter short aggregates; degrades to per-short when unknown."""
    _masters, shorts = _split_master_shorts(
        campaign_items(session, workspace_id, campaign_id)
    )
    groups: dict[str, dict] = {}
    for short in shorts:
        chapter_id, title = _short_chapter(session, short)
        key = chapter_id or f"short:{short.id}"
        group = groups.setdefault(
            key, {"chapter": chapter_id, "chapter_title": title, "short_ids": []}
        )
        group["short_ids"].append(short.id)
    result = []
    for key in sorted(groups):
        group = groups[key]
        posts: list[PublishedPost] = []
        for sid in group["short_ids"]:
            for p in _posts_for_content_ids(session, workspace_id, [sid]):
                if all(existing.id != p.id for existing in posts):
                    posts.append(p)
        metrics = latest_metrics(session, [p.id for p in posts])
        totals = _accumulate([metrics[p.id] for p in posts if p.id in metrics])
        result.append({**group, "posts": len(posts), "totals": totals})
    return result


def resolve_attribution(
    session, workspace_id: str, post_id: str
) -> dict:
    """Resolve publication → variant → short → master → campaign chain."""
    post = session.get(PublishedPost, post_id)
    if post is None or post.workspace_id != workspace_id:
        raise ValueError("publication not found")
    lineage = _post_lineage_cols(session, post_id)
    variant_id = getattr(post, "platform_variant_id", None) or lineage.get(
        "platform_variant_id"
    )
    short_id = post.content_item_id
    if variant_id:
        resolved = _variant_content_id(session, variant_id)
        if resolved:
            short_id = resolved
    campaign_id = getattr(post, "campaign_id", None) or lineage.get("campaign_id")
    master_id: str | None = None
    if short_id:
        short = session.get(ContentItem, short_id)
        if short is not None:
            campaign_id = campaign_id or short.campaign_id
            master_id = short.parent_content_id or short.root_content_id
            if master_id == short.id:
                master_id = None
    if campaign_id and not master_id:
        master_id = campaign_master_id(session, workspace_id, campaign_id)
    return {
        "post_id": post.id,
        "platform": post.platform,
        "variant_id": variant_id,
        "short_id": short_id,
        "master_id": master_id,
        "campaign_id": campaign_id,
    }


def record_learning(session, workspace_id: str, campaign_id: str) -> dict:
    """Push campaign lessons through the existing Learning Agent mechanism.

    Findings are stored via ``LearningAgent._upsert_pattern`` (EMA-updated
    ``LearningPattern`` rows) and mirrored to semantic memory via
    ``LearningAgent._remember_pattern`` — no second learning system.
    """
    from app.engine.agents.intelligence import LearningAgent

    campaign = session.get(Campaign, campaign_id)
    if campaign is None or campaign.workspace_id != workspace_id:
        raise ValueError("campaign not found")
    agent = LearningAgent()
    rollup = rollup_campaign(session, workspace_id, campaign_id)
    platforms = compare_platforms(session, workspace_id, campaign_id)
    chapters = chapter_performance(session, workspace_id, campaign_id)
    master = session.get(ContentItem, rollup["master_content_id"]) if rollup["master_content_id"] else None
    master_topic = master.topic if master else campaign.name
    totals = rollup["totals"]
    # HONESTY: `totals["views"] == 0` no longer covers the unmeasured case --
    # `_accumulate` now returns None when nothing was measured, and
    # `None == 0` is False, so the guard would have let an UNMEASURED campaign
    # through and taught the Learning Agent a lesson from no data. `not x`
    # covers both, and both mean the same thing here: there are no measured
    # views to learn from.
    if rollup["post_count"] == 0 or not totals["views"]:
        return {"recorded": 0, "lessons": [], "reason": "no measured posts"}
    lessons: list[dict] = []
    # A platform rollup with no snapshot has views=None. It cannot be ranked
    # against a measured platform, so it is excluded from the ranking entirely
    # rather than sorted as if it scored zero.
    ranked = sorted(
        ((k, v) for k, v in platforms.items() if v["views"] is not None),
        key=lambda kv: kv[1]["views"],
        reverse=True,
    )
    if len(ranked) >= 1:
        best_platform, best = ranked[0]
        others = [v["views"] for _, v in ranked[1:]]
        baseline = (sum(others) / len(others)) if others else 0
        improvement = (
            round((best["views"] / max(baseline, 1) - 1.0) * 100, 1)
            if baseline > 0
            else 100.0
        )
        lessons.append(
            {
                "pattern_key": f"campaign_platform_{best_platform}",
                "description": (
                    f"Campaign '{campaign.name}': {best_platform} led with "
                    f"{best['views']} views across {best['posts']} post(s)."
                ),
                "observed_improvement_pct": max(improvement, 0.0),
                "confidence": "low",
                "sample_size": best["posts"],
                "evidence": {
                    "master_topic": master_topic,
                    "platform": best_platform,
                    "result_summary": (
                        f"{best['views']} views, {best['likes']} likes, "
                        f"engagement {best['engagement_rate']}, "
                        f"completion {best['completion']}"
                    ),
                    "campaign_id": campaign_id,
                },
            }
        )
    # HONESTY: `views` is None for a chapter with no measured short, so the
    # old `> 0` comparison would raise TypeError on the unmeasured case. A
    # chapter only ranks once it has measured views.
    ranked_chapters = sorted(
        [c for c in chapters if c["totals"]["views"]],
        key=lambda c: c["totals"]["views"],
        reverse=True,
    )
    if ranked_chapters:
        top = ranked_chapters[0]
        hook = ""
        if top["short_ids"]:
            first = session.get(ContentItem, top["short_ids"][0])
            if first is not None:
                hook = (first.strategy_json or {}).get("hook", "") or first.topic
        lessons.append(
            {
                "pattern_key": "campaign_chapter_win",
                "description": (
                    f"Campaign '{campaign.name}': chapter "
                    f"'{top['chapter_title']}' outperformed "
                    f"({top['totals']['views']} views)."
                ),
                "observed_improvement_pct": 10.0,
                "confidence": "low",
                "sample_size": max(len(top["short_ids"]), 1),
                "evidence": {
                    "master_topic": master_topic,
                    "chapter": top["chapter_title"],
                    "short_hook": hook,
                    "result_summary": (
                        f"{top['totals']['views']} views across "
                        f"{len(top['short_ids'])} short(s)"
                    ),
                    "campaign_id": campaign_id,
                },
            }
        )
    recorded = 0
    for lesson in lessons:
        try:
            agent._upsert_pattern(workspace_id, lesson)
            agent._remember_pattern(workspace_id, lesson)
            recorded += 1
        except Exception:
            continue
    return {"recorded": recorded, "lessons": lessons}
