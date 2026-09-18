"""Analytics collection + learning loop agents."""

from __future__ import annotations

from sqlalchemy import select

from app.db import session_scope
from app.engine.agents.base import AgentMeta, BaseAgent
from app.models import LearningPattern, MemoryRecord, PostMetric, PublishedPost
from app.models.base import utcnow
from app.providers import analytics as analytics_mod


class AnalyticsCollectorAgent(BaseAgent):
    meta = AgentMeta(
        key="analytics",
        title="Analytics Agent",
        description="Collects performance metrics for published posts.",
        skills=(),
        tools=("fetch_metrics",),
        permissions=("analytics:read",),
    )

    def collect_for_posts(self, ctx, post_ids: list[str] | None = None) -> int:
        def work():
            ws = ctx.workspace_id
            updated = 0
            self.step("load_posts", "published posts awaiting metric collection")
            with session_scope() as s:
                q = select(PublishedPost).where(PublishedPost.workspace_id == ws)
                if post_ids:
                    q = q.where(PublishedPost.id.in_(post_ids))
                posts = s.scalars(q.order_by(PublishedPost.published_at.desc()).limit(50)).all()
                for post in posts:
                    provider = analytics_mod.get_provider(post.platform)
                    account = self._account_for(ws, post.platform)
                    try:
                        stats = provider.fetch_stats(
                            {"remote_post_id": post.remote_post_id, "platform": post.platform}, account
                        )
                    except Exception:
                        continue  # provider outage must not break the cycle
                    s.add(
                        PostMetric(
                            post_id=post.id,
                            views=stats.views,
                            likes=stats.likes,
                            comments=stats.comments,
                            shares=stats.shares,
                            saves=stats.saves,
                            avg_view_duration_seconds=stats.avg_view_duration_seconds,
                            completion_rate=stats.completion_rate,
                            followers_gained=stats.followers_gained,
                            captured_at=utcnow(),
                        )
                    )
                    post.is_mock = post.is_mock or getattr(provider, "platform", "") == "mock"
                    updated += 1
            self.step_done("ok", f"{updated} post(s) updated")
            return {"updated": updated, "summary": f"collected metrics for {updated} post(s)"}

        return self.execute(ctx, "collect_metrics", input_summary="", fn=work)

    @staticmethod
    def _account_for(workspace_id: str, platform: str) -> dict:
        from app.models import SocialAccount

        lookup = "meta" if platform in ("facebook", "instagram") else platform
        with session_scope() as s:
            acc = s.scalar(
                select(SocialAccount).where(
                    SocialAccount.workspace_id == workspace_id,
                    SocialAccount.platform.in_([platform, lookup] if lookup != platform else [platform]),
                    SocialAccount.status == "connected",
                )
            )
            if not acc:
                return {"access_token": "", "api_key": ""}
            from app.core.security import decrypt_secret

            return {
                "access_token": decrypt_secret(acc.access_token_enc or ""),
                "api_key": "",
            }


class LearningAgent(BaseAgent):
    meta = AgentMeta(
        key="learning",
        title="Learning Agent",
        description="Extracts performance patterns with confidence scoring.",
        skills=("analytics_learning",),
        tools=("fetch_metrics", "store_memory", "retrieve_memory"),
        permissions=("analytics:read", "memory:read", "memory:write"),
    )

    def learn_from_recent(self, ctx, min_sample: int = 3) -> dict:
        def work():
            ws = ctx.workspace_id
            findings = self._extract_patterns(ws)
            stored = 0
            for f in findings:
                if f["sample_size"] < min_sample:
                    continue
                self._upsert_pattern(ws, f)
                # Mirror into persistent semantic memory (spec #25) so future
                # decision/research contexts can retrieve it by topic scope.
                self._remember_pattern(ws, f)
                stored += 1
            return {"stored": stored, "summary": f"updated {stored} pattern(s) from recent performance"}

        return self.execute(ctx, "learn", input_summary="", fn=work)

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _latest_metrics(session, post_ids: list[str]) -> dict[str, PostMetric]:
        if not post_ids:
            return {}
        metrics: dict[str, PostMetric] = {}
        rows = session.scalars(
            select(PostMetric).where(PostMetric.post_id.in_(post_ids)).order_by(PostMetric.captured_at.asc())
        ).all()
        for m in rows:  # last snapshot wins
            metrics[m.post_id] = m
        return metrics

    def _extract_patterns(self, workspace_id: str) -> list[dict]:
        """Compare top-quartile posts vs channel average on observable features.

        Correlation is NOT causation; confidence scales with sample size.
        """
        self.step("gather_posts", "load published posts + latest metric snapshots")
        with session_scope() as s:
            posts = s.scalars(
                select(PublishedPost).where(PublishedPost.workspace_id == workspace_id).limit(200)
            ).all()
            if len(posts) < 4:
                self.step_done("ok", "fewer than 4 posts — not enough evidence")
                return []
            latest = self._latest_metrics(s, [p.id for p in posts])
            perf = []
            for p in posts:
                m = latest.get(p.id)
                if m and m.views > 0:
                    perf.append((p, m))
            if len(perf) < 4:
                self.step_done("ok", "fewer than 4 posts with view data")
                return []
            self.step_done("ok", f"{len(perf)} posts with metrics")
            views_sorted = sorted(m.views for _, m in perf)
            median_views = views_sorted[len(views_sorted) // 2]
            eng_sorted = sorted(
                (m.likes + m.comments + m.shares) / m.views for _, m in perf
            )
            median_eng = eng_sorted[len(eng_sorted) // 2]
            n = len(perf)

            findings = []

            def feature_hits(fn) -> tuple[int, float]:
                """(n_hits, avg_multiplier when hit). fn receives (post, metric)."""
                hits = 0
                mults = []
                for p, m in perf:
                    try:
                        if fn(p, m):
                            hits += 1
                            mults.append(m.views / max(median_views, 1))
                    except Exception:
                        continue
                return hits, (sum(mults) / len(mults)) if mults else 0.0

            checks = [
                ("hook_style_question", lambda p, m: "?" in (p.title or ""), "question-style titles"),
                ("duration_long_form", lambda p, m: "#shorts" not in (p.title or "").lower(), "non-shorts formatting"),
                ("title_with_numbers", lambda p, m: any(c.isdigit() for c in (p.title or "")), "titles containing numbers"),
                ("high_completion", lambda p, m: (m.completion_rate or 0) >= 0.5, "completion rate ≥50%"),
                ("high_engagement", lambda p, m: ((m.likes + m.comments + m.shares) / m.views) > median_eng * 1.5 if median_eng > 0 else False, "engagement rate 1.5× above median"),
            ]
            for key, fn, desc in checks:
                hits, mult = feature_hits(fn)
                if hits >= 2 and mult > 1.05:
                    improvement = round((mult - 1.0) * 100, 1)
                    confidence = "low" if n < 10 else ("medium" if n < 30 else "high")
                    findings.append(
                        {
                            "pattern_key": key,
                            "description": desc,
                            "observed_improvement_pct": improvement,
                            "confidence": confidence,
                            "sample_size": hits,
                            "evidence": {"median_views": median_views, "multiplier": round(mult, 2)},
                        }
                    )
            self.step("correlate_features", f"tested {len(checks)} observable features vs median")
            self.step_done("ok", f"{len(findings)} pattern(s) found")
            return findings

    def _remember_pattern(self, workspace_id: str, finding: dict) -> None:
        """Store a learned pattern as a semantic memory with a topic scope."""
        try:
            from app.services import memory as memory_service

            memory_service.store(
                workspace_id,
                content=(
                    f"Pattern [{finding['pattern_key']}]: {finding['description']} — "
                    f"+{finding['observed_improvement_pct']}% vs channel median "
                    f"(n={finding['sample_size']}, {finding['confidence']} confidence)."
                ),
                type=MemoryRecord.TYPE_SEMANTIC,
                source=self.meta.key,
                confidence={"low": 0.4, "medium": 0.7, "high": 0.9}.get(finding["confidence"], 0.5),
                importance=0.8,
                scope=finding["pattern_key"],
                related={
                    "pattern_key": finding["pattern_key"],
                    "evidence": finding.get("evidence", {}),
                },
            )
        except Exception:
            # Memory mirroring must never break the learning loop.
            pass

    def _upsert_pattern(self, workspace_id: str, finding: dict) -> None:
        with session_scope() as s:
            existing = s.scalar(
                select(LearningPattern).where(
                    LearningPattern.workspace_id == workspace_id,
                    LearningPattern.pattern_key == finding["pattern_key"],
                )
            )
            if existing:
                # exponential moving update keeps history influence
                existing.observed_improvement_pct = round(
                    existing.observed_improvement_pct * 0.6 + finding["observed_improvement_pct"] * 0.4, 2
                )
                existing.confidence = finding["confidence"]
                existing.sample_size = max(existing.sample_size, finding["sample_size"])
                existing.evidence_json = finding.get("evidence", {})
                existing.active = True
            else:
                s.add(LearningPattern(workspace_id=workspace_id, **{k: v for k, v in finding.items() if k != "evidence"},
                                      evidence_json=finding.get("evidence", {})))
