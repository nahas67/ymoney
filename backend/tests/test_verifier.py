"""Lane C verifier tests: four checkers, mock-vs-live, ledger chain, isolation."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from app.main import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _register(client) -> tuple[dict, str]:
    email = f"lane-c-{os.urandom(4).hex()}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["workspace"]["id"]


def _content(db_session, ws_id, **kw):
    from app.models import ContentItem

    item = ContentItem(workspace_id=ws_id, topic="money tips", **kw)
    db_session.add(item)
    db_session.commit()
    return item


def _video(db_session, ws_id, tmp_path, status="READY"):
    from pathlib import Path

    from app.models import ContentItem, MediaAsset, Video, VideoVariant

    item = ContentItem(workspace_id=ws_id, topic="v")
    db_session.add(item)
    db_session.flush()
    variant = VideoVariant(content_item_id=item.id, label="v1", script="hello world script")
    db_session.add(variant)
    db_session.flush()
    f = tmp_path / f"vid-{os.urandom(3).hex()}.mp4"
    f.write_bytes(b"\x00\x01\x02" * 1000)
    video = Video(variant_id=variant.id, workspace_id=ws_id, engine="mock",
                  status=status, file_path=str(f))
    db_session.add(video)
    db_session.flush()
    db_session.add(MediaAsset(workspace_id=ws_id, type="video", origin="render",
                              provider="mock", storage_key=Path(str(f)).name,
                              mime_type="video/mp4", file_size=3000))
    db_session.commit()
    return video


def _probe(monkeypatch, **meta):
    import app.services.storage as storage

    base = {"duration_seconds": 30.0, "width": 1080, "height": 1920}
    base.update(meta)
    monkeypatch.setattr(storage, "probe_metadata", lambda path: dict(base))


# ---------------------------------------------------------------------------
# video checker
# ---------------------------------------------------------------------------

def test_video_verified_when_file_probe_qc_ok(db_session, workspace_with_user, tmp_path, monkeypatch):
    from app.engine.intelligence.verifier import CompletionContract, verify
    from app.models import QualityCheck

    ws = workspace_with_user["workspace"]
    _probe(monkeypatch)
    video = _video(db_session, ws, tmp_path)
    db_session.add(QualityCheck(video_id=video.id, overall=80.0, passed=True))
    db_session.commit()
    row = verify(db_session, ws, CompletionContract(
        kind="video", subject_id=video.id,
        expectations={"duration_seconds": 30.0, "resolution": "1080x1920"}))
    assert row.execution_status == "COMPLETED"
    assert row.verification_status == "VERIFIED"
    assert row.workspace_id == ws


def test_video_not_verified_on_resolution_mismatch(db_session, workspace_with_user, tmp_path, monkeypatch):
    from app.engine.intelligence.verifier import CompletionContract, verify

    ws = workspace_with_user["workspace"]
    _probe(monkeypatch)
    video = _video(db_session, ws, tmp_path)
    row = verify(db_session, ws, CompletionContract(
        kind="video", subject_id=video.id, expectations={"resolution": "1920x1080"}))
    assert row.verification_status == "NOT_VERIFIED"


def test_execution_status_separate_from_verification(db_session, workspace_with_user, tmp_path, monkeypatch):
    from app.engine.intelligence.verifier import CompletionContract, verify

    ws = workspace_with_user["workspace"]
    _probe(monkeypatch)
    video = _video(db_session, ws, tmp_path, status="FAILED")
    row = verify(db_session, ws, CompletionContract(kind="video", subject_id=video.id))
    assert row.execution_status == "FAILED"
    assert row.verification_status in ("VERIFIED", "PARTIALLY_VERIFIED")  # file itself proves out


def test_video_cross_workspace_blocked(db_session, workspace_with_user, tmp_path):
    from app.engine.intelligence.verifier import CompletionContract, verify
    from app.models import Workspace

    ws = workspace_with_user["workspace"]
    video = _video(db_session, ws, tmp_path)
    other = Workspace(name="Other WS", slug=f"ws-other-{os.urandom(4).hex()}")
    db_session.add(other)
    db_session.commit()
    row2 = verify(db_session, other.id, CompletionContract(
        kind="video", subject_id=video.id))
    assert row2.verification_status == "BLOCKED"
    assert row2.execution_status == "UNKNOWN"


# ---------------------------------------------------------------------------
# publication checker (mock vs live)
# ---------------------------------------------------------------------------

def _post(db_session, ws_id, video_id, is_mock=False):
    from app.models import PublishedPost

    # Work 14: publication_mode is declared explicitly. The column default is
    # UNAVAILABLE (fail closed) because a row that states no mode has stated
    # nothing -- so a test that means "live" or "mock" must say which.
    post = PublishedPost(workspace_id=ws_id, video_id=video_id, platform="youtube",
                         remote_post_id=f"remote-{os.urandom(3).hex()}",
                         remote_url="https://youtube.test/v/1", is_mock=is_mock,
                         publication_mode="MOCK" if is_mock else "LIVE")
    db_session.add(post)
    db_session.commit()
    return post


def test_publication_live_verified(db_session, workspace_with_user, tmp_path):
    from app.engine.intelligence.verifier import CompletionContract, verify

    ws = workspace_with_user["workspace"]
    video = _video(db_session, ws, tmp_path)
    post = _post(db_session, ws, video.id)
    row = verify(db_session, ws, CompletionContract(
        kind="publication", subject_id=post.id,
        expectations={"platform": "youtube", "live": True}))
    assert row.execution_status == "COMPLETED"
    assert row.verification_status == "VERIFIED"


def test_publication_mock_never_verifies_live(db_session, workspace_with_user, tmp_path):
    from app.engine.intelligence.verifier import CompletionContract, verify

    ws = workspace_with_user["workspace"]
    video = _video(db_session, ws, tmp_path)
    post = _post(db_session, ws, video.id, is_mock=True)
    live_row = verify(db_session, ws, CompletionContract(
        kind="publication", subject_id=post.id, expectations={"live": True}))
    assert live_row.verification_status == "NOT_VERIFIED"
    mock_row = verify(db_session, ws, CompletionContract(
        kind="publication", subject_id=post.id, expectations={}))
    assert mock_row.verification_status == "VERIFIED"
    assert any("mock" in c["detail"].lower() for c in mock_row.checks_json)


# ---------------------------------------------------------------------------
# campaign checker
# ---------------------------------------------------------------------------

def _campaign_setup(db_session, ws_id, n_items=2, with_plan=True, platforms=("youtube",)):
    from app.models import Campaign, ContentItem, PublishingPlan
    from app.models.campaign import PlatformVariant

    camp = Campaign(workspace_id=ws_id, name="camp")
    db_session.add(camp)
    db_session.flush()
    parent = ContentItem(workspace_id=ws_id, topic="master", campaign_id=camp.id)
    db_session.add(parent)
    db_session.flush()
    for i in range(n_items):
        db_session.add(ContentItem(workspace_id=ws_id, topic=f"short {i}",
                                   campaign_id=camp.id, parent_content_id=parent.id,
                                   root_content_id=parent.id, derivation_type="short"))
    db_session.flush()
    shorts = db_session.query(ContentItem).filter(
        ContentItem.campaign_id == camp.id,
        ContentItem.derivation_type == "short").all()
    for s, p in zip(shorts, list(platforms) * max(1, len(shorts))):
        db_session.add(PlatformVariant(workspace_id=ws_id, campaign_id=camp.id,
                                       short_content_id=s.id, platform=p, status="READY"))
    if with_plan:
        db_session.add(PublishingPlan(workspace_id=ws_id, campaign_id=camp.id,
                                      items_json=[{"platform": "youtube"}], status="READY"))
    db_session.commit()
    return camp


def test_campaign_verified(db_session, workspace_with_user):
    from app.engine.intelligence.verifier import CompletionContract, verify

    ws = workspace_with_user["workspace"]
    camp = _campaign_setup(db_session, ws)
    row = verify(db_session, ws, CompletionContract(
        kind="campaign", subject_id=camp.id,
        expectations={"expected_derivatives": 2, "required_variants": ["youtube"]}))
    assert row.verification_status == "VERIFIED"


def test_campaign_missing_derivatives_or_exceptions(db_session, workspace_with_user):
    from app.engine.intelligence.verifier import CompletionContract, verify

    ws = workspace_with_user["workspace"]
    camp = _campaign_setup(db_session, ws, n_items=1, with_plan=False)
    missing = verify(db_session, ws, CompletionContract(
        kind="campaign", subject_id=camp.id, expectations={"expected_derivatives": 3}))
    assert missing.verification_status == "NOT_VERIFIED"
    excused = verify(db_session, ws, CompletionContract(
        kind="campaign", subject_id=camp.id,
        expectations={"expected_derivatives": 3, "exceptions": ["rights holdout"],
                      "require_publishing_plan": False}))
    assert excused.verification_status in ("VERIFIED", "PARTIALLY_VERIFIED")


# ---------------------------------------------------------------------------
# research checker
# ---------------------------------------------------------------------------

def test_research_verified_with_provenance(db_session, workspace_with_user):
    from app.engine.intelligence.verifier import CompletionContract, verify

    ws = workspace_with_user["workspace"]
    item = _content(db_session, ws, research_json={
        "summary": "brief", "key_facts": ["f1"],
        "claims": [{"claim": "c1", "status": "LIKELY", "confidence": 0.6, "basis": "u1"}],
        "cautions": ["verify"], "factual_confidence": 0.42, "fact_status": "OK",
        "sources": [{"url": "https://example.com/a"}],
    })
    row = verify(db_session, ws, CompletionContract(
        kind="research", subject_id=item.id, expectations={"min_sources": 1}))
    assert row.execution_status == "COMPLETED"
    assert row.verification_status == "VERIFIED"


def test_research_empty_not_verified(db_session, workspace_with_user):
    from app.engine.intelligence.verifier import CompletionContract, verify

    ws = workspace_with_user["workspace"]
    item = _content(db_session, ws)
    row = verify(db_session, ws, CompletionContract(kind="research", subject_id=item.id))
    assert row.verification_status == "NOT_VERIFIED"


# ---------------------------------------------------------------------------
# ledger: append-only + digest chain + isolation
# ---------------------------------------------------------------------------

def test_ledger_chain_links_and_verifies(db_session, workspace_with_user, tmp_path):
    from app.engine.intelligence import ledger as ledger_engine
    from app.engine.intelligence.verifier import CompletionContract, verify

    ws = workspace_with_user["workspace"]
    item = _content(db_session, ws, research_json={"summary": "x"})
    r1 = verify(db_session, ws, CompletionContract(kind="research", subject_id=item.id))
    r2 = verify(db_session, ws, CompletionContract(kind="research", subject_id=item.id))
    assert r1.digest and r2.prev_digest == r1.digest
    assert r1.digest != r2.digest
    chain = ledger_engine.verify_chain(db_session, ws)
    assert chain == {"ok": True, "count": 2, "broken_at": None}
    assert not hasattr(ledger_engine, "update_record")
    assert not hasattr(ledger_engine, "delete_record")


def test_ledger_workspace_isolation(db_session, workspace_with_user):
    from app.engine.intelligence import ledger as ledger_engine
    from app.engine.intelligence.verifier import CompletionContract, verify

    ws = workspace_with_user["workspace"]
    item = _content(db_session, ws, research_json={"summary": "x"})
    verify(db_session, ws, CompletionContract(kind="research", subject_id=item.id))
    own = ledger_engine.list_records(db_session, ws)
    assert len(own) >= 1
    assert ledger_engine.list_records(db_session, "foreign-ws") == []
    assert ledger_engine.verify_chain(db_session, "foreign-ws")["count"] == 0


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

def test_browser_run_routes_isolation_and_guards(client, db_session):
    headers, ws_id = _register(client)
    # create
    r = client.post(f"/api/v1/workspaces/{ws_id}/intelligence/browser/runs",
                    headers=headers,
                    json={"goal": "research rates", "start_urls": [],
                          "allowed_domains": []})
    assert r.status_code == 200, r.text
    run_id = r.json()["id"]
    # get
    g = client.get(f"/api/v1/workspaces/{ws_id}/intelligence/browser/runs/{run_id}",
                   headers=headers)
    assert g.status_code == 200
    assert g.json()["goal"] == "research rates"
    # cross-workspace 404
    headers2, ws2 = _register(client)
    x = client.get(f"/api/v1/workspaces/{ws2}/intelligence/browser/runs/{run_id}",
                   headers=headers2)
    assert x.status_code == 404
    # cancel terminal run -> not cancelled, still 200
    c = client.post(f"/api/v1/workspaces/{ws_id}/intelligence/browser/runs/{run_id}/cancel",
                    headers=headers)
    assert c.status_code == 200
    assert c.json()["cancelled"] is False


def test_browser_disabled_returns_403(client, db_session):
    from app.models import Workspace

    headers, ws_id = _register(client)
    ws = db_session.get(Workspace, ws_id)
    ws.settings_json = {"intelligence": {"browser_enabled": False}}
    db_session.commit()
    r = client.post(f"/api/v1/workspaces/{ws_id}/intelligence/browser/runs",
                    headers=headers, json={"goal": "g"})
    assert r.status_code == 403
    assert "disabled" in r.json()["detail"]
    ws.settings_json = {"intelligence": {"privacy_mode": "LOCAL_ONLY"}}
    db_session.commit()
    r2 = client.post(f"/api/v1/workspaces/{ws_id}/intelligence/browser/runs",
                     headers=headers, json={"goal": "g"})
    assert r2.status_code == 403
    assert "LOCAL_ONLY" in r2.json()["detail"]


def test_verification_routes_and_ledger_filters(client, db_session):
    headers, ws_id = _register(client)
    item = _content(db_session, ws_id, research_json={
        "summary": "s", "key_facts": ["f"],
        "claims": [{"claim": "c", "status": "VERIFIED", "confidence": 0.9, "basis": "u"}],
        "cautions": [], "fact_status": "OK", "sources": [{"url": "https://example.com"}],
    })
    r = client.post(f"/api/v1/workspaces/{ws_id}/intelligence/verification/check",
                    headers=headers,
                    json={"kind": "research", "subject_id": item.id, "expectations": {}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verification_status"] == "VERIFIED"
    assert body["execution_status"] == "COMPLETED"
    assert body["digest"]
    # ledger + filters
    led = client.get(f"/api/v1/workspaces/{ws_id}/intelligence/verification/ledger",
                     headers=headers)
    assert led.status_code == 200
    assert led.json()["chain"]["ok"] is True
    assert len(led.json()["items"]) >= 1
    filt = client.get(
        f"/api/v1/workspaces/{ws_id}/intelligence/verification/ledger?kind=research&status=VERIFIED",
        headers=headers)
    assert filt.status_code == 200
    assert all(i["kind"] == "research" for i in filt.json()["items"])
    # cross-workspace ledger isolation
    headers2, ws2 = _register(client)
    led2 = client.get(f"/api/v1/workspaces/{ws2}/intelligence/verification/ledger",
                      headers=headers2)
    assert led2.status_code == 200
    assert led2.json()["items"] == []
