"""Hook A/B + title variants: SEO cleaning, manual variant switch, compare data."""
from __future__ import annotations


def test_clean_platform_meta_title_variants():
    from app.engine.agents.distribution import clean_platform_meta

    meta = {"title": "Save money fast",
            "title_variants": ["Save money fast", "Stop losing cash now",
                               "Save money fast — explained in 60 seconds", "", 123],
            "description": "d", "hashtags": ["money"], "keywords": ["save"]}
    out = clean_platform_meta("youtube", meta, "save money", "script words here")
    assert out is not None
    assert out["title"] == "Save money fast"
    # dup of primary dropped, capped at 2, non-strings coerced
    assert out["title_variants"] == ["Stop losing cash now",
                                     "Save money fast — explained in 60 seconds"]
    assert out["is_ai_generated"] is True


def test_clean_platform_meta_rejects_platform_and_caps_length():
    from app.engine.agents.distribution import clean_platform_meta

    assert clean_platform_meta("myspace", {"title": "t"}, "t", "s") is None
    out = clean_platform_meta("youtube", {"title": "x" * 200,
                                          "title_variants": ["y" * 200]}, "t", "s")
    assert out is not None and len(out["title"]) == 100 and len(out["title_variants"][0]) == 100


def test_default_metadata_has_variants():
    from app.engine.agents.distribution import _default_metadata

    out = _default_metadata("save money fast", "script words", ["youtube", "tiktok"])
    for p in ("youtube", "tiktok"):
        assert out[p]["title_variants"]
        assert all(v != out[p]["title"] for v in out[p]["title_variants"])
        assert all(len(v) <= 150 for v in out[p]["title_variants"])


def _mk_content(db, ws_id, status="SCRIPT_READY", n_variants=2):
    from app.models import ContentItem, VideoVariant

    item = ContentItem(workspace_id=ws_id, topic="ab test", status=status)
    db.add(item)
    db.flush()
    ids = []
    for i in range(n_variants):
        v = VideoVariant(content_item_id=item.id, label=f"v{i + 1}",
                         script="word " * 60, predicted_score=70.0 + i,
                         selected=(i == 0))
        db.add(v)
        db.flush()
        ids.append(v.id)
    db.commit()
    return item.id, ids


def test_variant_switch_and_guards():
    import os

    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    email = f"ab{os.urandom(4).hex()}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    ws_id = data["workspace"]["id"]

    from app.db import session_scope

    with session_scope() as s:
        cid, (v1, v2) = _mk_content(s, ws_id)

    r = client.post(f"/api/v1/workspaces/{ws_id}/content/{cid}/variants/{v2}/select",
                    headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["selected"] == v2

    # idempotent re-select
    r = client.post(f"/api/v1/workspaces/{ws_id}/content/{cid}/variants/{v2}/select",
                    headers=headers)
    assert r.status_code == 200

    # unknown variant → 404
    r = client.post(f"/api/v1/workspaces/{ws_id}/content/{cid}/variants/nope/select",
                    headers=headers)
    assert r.status_code == 404

    # rendered video blocks the switch
    from app.models import Video

    with session_scope() as s:
        s.add(Video(variant_id=v2, workspace_id=ws_id, engine="ffmpeg_avatar",
                    status="READY", file_path="data/videos/x.mp4"))
        s.flush()
    r = client.post(f"/api/v1/workspaces/{ws_id}/content/{cid}/variants/{v1}/select",
                    headers=headers)
    assert r.status_code == 409

    # wrong status blocks the switch
    with session_scope() as s:
        from app.models import ContentItem

        s.get(ContentItem, cid).status = "PUBLISHED"
        s.query(Video).delete()
        s.flush()
    r = client.post(f"/api/v1/workspaces/{ws_id}/content/{cid}/variants/{v1}/select",
                    headers=headers)
    assert r.status_code == 409
