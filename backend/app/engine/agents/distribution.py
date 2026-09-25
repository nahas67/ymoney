"""SEO/metadata + publishing agents — PRODUCTION ONLY.

Publishing requires real credentials. Three paths, checked in order:
  1. Upload-Post relay configured → handles tiktok/instagram/youtube/facebook
  2. Platform account connected (OAuth tokens stored encrypted) → direct API
  3. Neither → explicit BLOCKED result with remediation (never fake success)
"""

from __future__ import annotations

import json

from sqlalchemy import select

from app.db import session_scope
from app.engine.agents.base import AgentMeta, BaseAgent
from app.models import SocialAccount
from app.providers import llm
from app.providers.publishers.base import PublishMetadata, PublishResult
from app.providers.publishers.platforms import UploadPostRelay

PLATFORM_META_TEMPLATES = {
    "youtube": {"title_max": 100, "hashtags": ["#shorts", "#money", "#finance"]},
    "tiktok": {"title_max": 150, "hashtags": ["#fyp", "#moneytok", "#finance"]},
    "facebook": {"title_max": 120, "hashtags": ["#moneytips"]},
    "instagram": {"title_max": 125, "hashtags": ["#reels", "#finance"]},
}


def clean_platform_meta(p: str, meta: dict, topic: str, script: str) -> dict | None:
    """Validate + normalize one platform's LLM metadata (pure, unit-tested)."""
    if p not in ("youtube", "tiktok", "facebook", "instagram"):
        return None
    title_max = PLATFORM_META_TEMPLATES.get(p, {}).get("title_max", 100)
    title = str(meta.get("title", topic))[:title_max]
    variants = []
    for cand in (meta.get("title_variants") or meta.get("titles") or []):
        text = str(cand)[:title_max].strip()
        if text and text != title and text not in variants:
            variants.append(text)
        if len(variants) >= 2:
            break
    description = str(meta.get("description", ""))
    finance = _seo_finance(topic, script, description)
    if finance and "Not financial advice" not in description:
        description = (description + " Not financial advice. For education only.").strip()
    return {
        "title": title,
        "title_variants": variants,
        "description": description[:2000],
        "first_comment": str(meta.get("first_comment", ""))[:1000].strip(),
        "pinned_comment": str(meta.get("pinned_comment", ""))[:500].strip(),
        "hashtags": [h if h.startswith("#") else f"#{h}" for h in (meta.get("hashtags") or [])][:8],
        "keywords": [str(k) for k in (meta.get("keywords") or [])][:10],
        "category_id": str(meta.get("category_id") or meta.get("categoryId") or "27"),
        "contains_finance_advice": finance,
        "is_ai_generated": True,
        "altered_content": True,
    }


def _default_metadata(topic: str, script: str, platforms: list[str]) -> dict:
    base = topic if len(topic) <= 60 else topic[:57] + "..."
    finance = _seo_finance(topic, script, "")
    out = {}
    for p in platforms:
        tpl = PLATFORM_META_TEMPLATES.get(p, PLATFORM_META_TEMPLATES["tiktok"])
        tmax = tpl["title_max"]
        title = base[: tmax]
        alts = []
        for v in (f"{base} — explained in 60 seconds",
                  f"What nobody tells you about {base}"):
            t = v[:tmax].strip()
            if t and t != title and t not in alts:
                alts.append(t)
        tags = tpl["hashtags"]
        out[p] = {
            "title": title,
            "title_variants": alts[:2],
            "first_comment": " ".join(tags),
            "pinned_comment": "Which tip will you try first? Comment below.",
            "description": f"{topic} explained in seconds. {script[:80]}..." + (
                " Not financial advice. For education only." if finance else ""
            ),
            "hashtags": tpl["hashtags"],
            "keywords": [w for w in topic.lower().split() if len(w) > 3][:5],
            "category_id": "27",
            "contains_finance_advice": finance,
            "is_ai_generated": True,
            "altered_content": True,
        }
    return out


def _seo_finance(topic: str, script: str, description: str) -> bool:
    t = f"{topic} {script[:500]} {description}".lower()
    return any(k in t for k in ("money", "income", "budget", "save", "invest", "earn", "cash", "finance", "stock", "crypto", "debt", "loan"))


class SEOAgent(BaseAgent):
    meta = AgentMeta(
        key="seo",
        title="SEO Agent",
        description="Generates platform-specific titles, descriptions and hashtags.",
        skills=("publishing",),
        tools=("publish_post",),
        permissions=("publish:write",),
    )

    def metadata(self, ctx, *, topic: str, script: str, platforms: list[str]) -> dict:
        res = llm.complete_json(
            system=(
                "You generate platform-optimized short-form video metadata. For EACH platform in the "
                "list return: title (punchy, within platform limits), title_variants (2 alternate "
                "titles, same limits, different angles for A/B testing), description (1-2 sentences), "
                "first_comment (hashtag/discovery block to post as the first comment), "
                "pinned_comment (short engaging question CTA to pin for replies), "
                "hashtags (4-8 mixing trending, niche and branded tags, platform conventions), "
                "keywords. Title craft: spread curiosity, direct/search and benefit angles "
                "across title + variants; each title must pair with the thumbnail as one "
                "micro-story. JSON keyed by platform."
            ),
            user=json.dumps({"topic": topic, "platforms": platforms, "script_excerpt": script[:600]}),
            workspace_id=ctx.workspace_id or "",
            tier="cheap",
            temperature=0.7,
            max_tokens=1100,
        )
        clean = {}
        for p, meta in (res.items() if isinstance(res, dict) else []):
            if p not in ("youtube", "tiktok", "facebook", "instagram"):
                continue
            cleaned = clean_platform_meta(p, meta if isinstance(meta, dict) else {}, topic, script)
            if cleaned:
                clean[p] = cleaned
        return clean or _default_metadata(topic, script, platforms)

    def run(self, ctx, **kw) -> dict:
        return self.execute(ctx, "metadata", input_summary=kw.get("topic", ""), fn=lambda: self.metadata(ctx, **kw))


class PublisherAgent(BaseAgent):
    meta = AgentMeta(
        key="publisher",
        title="Publisher Agent",
        description="Publishes approved videos to connected platform accounts.",
        skills=("publishing",),
        tools=("publish_post",),
        permissions=("publish:write",),
    )

    def publish_to_platforms(
        self,
        ctx,
        *,
        video_path: str,
        platforms: list[str],
        metadata_by_platform: dict,
        workspace_id: str,
    ) -> list[dict]:
        results = []

        def work():
            relay = (
                UploadPostRelay(*self._relay_creds(workspace_id))
                if self._relay_ready(workspace_id)
                else None
            )
            self.step("resolve_publishers", f"paths for {', '.join(platforms)}")
            for platform in platforms:
                from app.services import jobs as _j

                _j.check_cancelled(ctx)
                self.step(f"publish_{platform}", "resolve account/relay/mock path")
                account = self._account_for(workspace_id, platform)
                meta_d = metadata_by_platform.get(platform) or {}
                meta = PublishMetadata(
                    title=meta_d.get("title", "") or "Untitled",
                    description=meta_d.get("description", ""),
                    hashtags=[h.lstrip("#") for h in meta_d.get("hashtags", [])],
                    keywords=meta_d.get("keywords", []),
                    privacy="public",
                    category_id=str(meta_d.get("category_id") or meta_d.get("categoryId") or "27"),
                    made_for_kids=bool(meta_d.get("made_for_kids", False)),
                    is_ai_generated=bool(meta_d.get("is_ai_generated", True)),
                    altered_content=bool(meta_d.get("altered_content", True)),
                    contains_finance_advice=bool(meta_d.get("contains_finance_advice", False)),
                    thumbnail_path=meta_d.get("thumbnail_path", "") or "",
                    captions_path=meta_d.get("captions_path", "") or "",
                    extra=dict(meta_d.get("extra") or {}),
                )

                # YouTube quota guard: dedicated videos.insert bucket (100/day).
                if platform == "youtube" and account:
                    from app.providers.publishers.platforms import YOUTUBE_MAX_UPLOADS_PER_DAY

                    used = self._youtube_uploads_today(workspace_id)
                    if used >= YOUTUBE_MAX_UPLOADS_PER_DAY:
                        self.step_failed("youtube quota exhausted")
                        results.append({
                            "platform": platform,
                            "success": False,
                            "remote_post_id": "",
                            "remote_url": "",
                            "error": "YouTube upload bucket exhausted (100 videos.insert/day) — retry tomorrow",
                            "mock": False,
                            "blocked": True,
                            "retryable": True,
                        })
                        continue

                # ---- resolve publisher path ----
                publisher = None
                actor = ""
                if account and (account.get("access_token") or account.get("refresh_token")):
                    from app.providers.publishers.factory import _registry

                    pub_cls = _registry.get(platform)
                    if pub_cls:
                        publisher = pub_cls
                        actor = f"direct:{platform}"
                elif relay:
                    publisher = relay
                    actor = "relay"
                elif self._mock_publishing_allowed():
                    # Simulation fallback: clearly-labeled local publish intent.
                    # Never used in production; rows are marked is_mock=True.
                    from app.providers.publishers.mock import MockPublisher

                    publisher = MockPublisher()
                    actor = "mock"

                if publisher is None:
                    self.step_failed("no account, relay, or mock path")
                    results.append({
                        "platform": platform,
                        "success": False,
                        "remote_post_id": "",
                        "remote_url": "",
                        "error": (
                            f"AUTHENTICATION REQUIRED — connect {platform} via Publishing page "
                            "or configure the Upload-Post relay"
                        ),
                        "mock": False,
                        "blocked": True,
                    })
                    continue


                result: PublishResult = publisher.publish(
                    video_path, meta,
                    {"platforms": [platform], **(account or {})} if actor == "relay" else (account or {}),
                )
                if result.success:
                    self.step_done("ok", f"via {actor}, post {result.remote_post_id[:20]}")
                else:
                    self.step_failed(result.error[:120] or "publish failed")
                results.append({
                    "platform": platform,
                    "success": result.success,
                    "remote_post_id": result.remote_post_id,
                    "remote_url": result.remote_url,
                    "error": result.error,
                    "mock": actor == "mock",  # simulation rows are labeled
                    "via": actor,
                    "retryable": result.retryable,
                })
            return results

        return self.execute(ctx, "publish", input_summary=", ".join(platforms), fn=work)

    @staticmethod
    def _mock_publishing_allowed() -> bool:
        from app.core.config import settings

        return bool(settings.mock_publishing) and not settings.is_production

    @staticmethod
    def _relay_ready(workspace_id: str | None = None) -> bool:
        from app.services.provider_settings import upload_post_config

        key, user = upload_post_config(workspace_id=workspace_id)
        return bool(key and user)

    @staticmethod
    def _relay_creds(workspace_id: str | None = None) -> tuple[str, str]:
        from app.services.provider_settings import upload_post_config

        return upload_post_config(workspace_id=workspace_id)

    @staticmethod
    def _account_for(workspace_id: str, platform: str) -> dict | None:
        with session_scope() as s:
            acc = s.scalar(
                select(SocialAccount).where(
                    SocialAccount.workspace_id == workspace_id,
                    SocialAccount.platform == platform,
                    SocialAccount.status == "connected",
                )
            )
            if not acc:
                return None
            from app.core.security import decrypt_secret

            return {
                "platform": acc.platform,
                "external_id": acc.external_id,
                "display_name": acc.display_name,
                "access_token": decrypt_secret(acc.access_token_enc or ""),
                "refresh_token": decrypt_secret(acc.refresh_token_enc or ""),
                "workspace_id": workspace_id,
            }

    @staticmethod
    def _youtube_uploads_today(workspace_id: str) -> int:
        from datetime import timedelta

        from sqlalchemy import select as _select

        from app.db import session_scope
        from app.models import PublishingJob
        from app.models.base import utcnow

        since = utcnow() - timedelta(hours=24)
        with session_scope() as s:
            rows = s.scalars(
                _select(PublishingJob).where(
                    PublishingJob.workspace_id == workspace_id,
                    PublishingJob.platform == "youtube",
                    PublishingJob.status == "PUBLISHED",
                    PublishingJob.created_at >= since,
                )
            ).all()
            return len(rows)
