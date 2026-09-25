"""Orphan sweep: dangling media/publish rows surface as counts (delta-based)."""
from __future__ import annotations

import uuid


def test_orphan_sweep_counts_deltas():
    from fastapi.testclient import TestClient

    from app.db import session_scope
    from app.main import create_app
    from app.models.content import PublishedPost

    client = TestClient(create_app(), raise_server_exceptions=False)
    email = f"orp{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    ws_id = r.json()["workspace"]["id"]

    def _counts():
        r = client.get("/api/v1/system/orphans")
        assert r.status_code == 200, r.text
        body = r.json()
        assert {"videos_orphaned", "variants_orphaned", "publishing_jobs_orphaned",
                "published_posts_orphaned", "healthy"} <= set(body)
        return body

    before = _counts()

    # NOTE: Video.variant_id, VideoVariant.content_item_id and
    # PublishingJob.video_id are FK-enforced (IntegrityError above), so only
    # PublishedPost.video_id (plain String, no FK) can actually dangle.
    with session_scope() as s:
        s.add(PublishedPost(video_id="orphan-video", workspace_id=ws_id, platform="youtube"))
        s.flush()

    after = _counts()
    assert after["published_posts_orphaned"] == before["published_posts_orphaned"] + 1
    assert after["videos_orphaned"] == before["videos_orphaned"]
    assert after["variants_orphaned"] == before["variants_orphaned"]
    assert after["publishing_jobs_orphaned"] == before["publishing_jobs_orphaned"]
    assert after["healthy"] is False
