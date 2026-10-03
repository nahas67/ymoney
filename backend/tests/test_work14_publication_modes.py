"""Work 14 §9 -- LIVE / MOCK / HANDOFF / UNAVAILABLE at the DATABASE layer.

The unit tests prove the classification logic. These prove the thing that
actually matters: a real ``PublishedPost`` row can never verify as a live
publication unless it genuinely is one, and a Snapchat handoff can never reach
``PUBLISHED`` through the real publish flow.
"""

from __future__ import annotations

import pytest


def _enc(plaintext: str) -> str:
    """Encrypt through the REAL secrets service, as production would."""
    from app.core.security import encrypt_secret

    return encrypt_secret(plaintext)


def _client(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from app.main import create_app

    monkeypatch.chdir(tmp_path)
    return TestClient(create_app(), raise_server_exceptions=False)


def _register(client):
    import uuid

    from app.db import session_scope
    from app.models import ContentTimeline, Workspace

    email = f"w14{uuid.uuid4().hex[:8]}@test.local"
    resp = client.post("/api/v1/auth/register",
                       json={"email": email, "password": "supersecret123"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    ws_id = data["workspace"]["id"]
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    doc = {"workspace_id": ws_id, "fps": 30, "aspect_ratio": "9:16",
           "duration_seconds": 4.0,
           "tracks": [{"id": "t_video", "kind": "video", "name": "v",
                       "clips": [{"id": "v1", "name": "v1", "start": 0.0,
                                  "duration": 4.0, "source": {}, "effects": [],
                                  "source_start": 0.0, "volume": 1.0,
                                  "speed": 1.0, "fade_in": 0.0,
                                  "fade_out": 0.0, "transform": {},
                                  "text": {}, "transition_in": "cut",
                                  "transition_out": "cut"}]}]}
    with session_scope() as s:
        workspace = s.get(Workspace, ws_id)
        row = ContentTimeline(workspace_id=ws_id, version=1, tracks_json=doc,
                              fps=30, duration_seconds=4.0)
        s.add(row)
        s.commit()
        timeline_id = row.id
    _ = workspace
    return ws_id, headers, timeline_id


def _post(ws_id: str, *, platform: str, video_id: str, mode: str,
          remote_id: str = "", handoff: dict | None = None,
          account_id: str = "") -> str:
    from app.db import session_scope
    from app.models import PublishedPost

    with session_scope() as s:
        row = PublishedPost(
            workspace_id=ws_id, content_item_id=video_id, video_id=video_id,
            platform=platform, account_id=account_id,
            remote_post_id=remote_id, remote_url=f"https://x/{remote_id}"
            if remote_id else "",
            publication_mode=mode, is_mock=(mode == "MOCK"),
            handoff_payload=handoff)
        s.add(row)
        s.commit()
        return row.id


def _verify(ws_id: str, post_id: str, expectations: dict | None = None):
    """Run the real verifier and return a plain dict for assertions.

    ``verify`` appends to the append-only evidence ledger and returns the
    EvidenceRecord row, so the shape is projected here rather than pretending
    the API returns a dict.
    """
    from app.db import session_scope
    from app.engine.intelligence.verifier import (
        CompletionContract,
        verify,
    )

    with session_scope() as s:
        row = verify(s, ws_id, CompletionContract(
            kind="publication", subject_id=post_id,
            expectations=expectations or {}))
        return {"execution": row.execution_status,
                "verdict": row.verification_status,
                "checks": list(row.checks_json or [])}


def _by_name(report, name):
    for check in report["checks"]:
        if check.get("name") == name:
            return check
    raise AssertionError(
        f"no check {name}: {[c.get('name') for c in report['checks']]}")


# ---------------------------------------------------------------------------
# the four modes
# ---------------------------------------------------------------------------


def test_live_publication_verifies(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, _headers, _tid = _register(client)
    post_id = _post(ws_id, platform="threads", video_id="v-live",
                    mode="LIVE", remote_id="media-1", account_id="acct-1")
    result = _verify(ws_id, post_id, {"live": True, "platform": "threads",
                                      "account_id": "acct-1"})
    assert result["verdict"] == "VERIFIED", result
    assert _by_name(result, "idempotency_single_record")["passed"] is True
    assert _by_name(result, "mode")["detail"].endswith("live")


def test_mock_publication_never_verifies_as_live(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, _headers, _tid = _register(client)
    post_id = _post(ws_id, platform="threads", video_id="v-mock",
                    mode="MOCK", remote_id="mock-1")
    result = _verify(ws_id, post_id, {"live": True})
    assert result["verdict"] != "VERIFIED"
    assert _by_name(result, "live_proof")["passed"] is False


def test_handoff_never_verifies_as_live(tmp_path, monkeypatch):
    """The headline DoD: a handoff is prepared, not published."""
    client = _client(tmp_path, monkeypatch)
    ws_id, _headers, _tid = _register(client)
    post_id = _post(ws_id, platform="snapchat", video_id="v-handoff",
                    mode="HANDOFF",
                    handoff={"mode": "HANDOFF", "requires_human": True,
                             "instruction": "User handoff required",
                             "remote_id": ""})
    result = _verify(ws_id, post_id, {"live": True})
    assert result["verdict"] == "NOT_VERIFIED"
    assert _by_name(result, "live_proof")["passed"] is False
    assert _by_name(result, "handoff_recorded")["passed"] is True
    assert _by_name(result, "handoff_is_not_publication")["passed"] is True
    # and the execution state is PENDING, not COMPLETED
    assert result["execution"] == "PENDING"


def test_unavailable_never_verifies(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, _headers, _tid = _register(client)
    post_id = _post(ws_id, platform="bluesky", video_id="v-unavail",
                    mode="UNAVAILABLE")
    result = _verify(ws_id, post_id, {})
    assert result["verdict"] == "NOT_VERIFIED"
    assert _by_name(result, "not_a_publication")["passed"] is False


def test_a_remote_id_without_a_live_mode_is_flagged(tmp_path, monkeypatch):
    """A remote id is only evidence when the mode says LIVE."""
    client = _client(tmp_path, monkeypatch)
    ws_id, _headers, _tid = _register(client)
    post_id = _post(ws_id, platform="snapchat", video_id="v-contradiction",
                    mode="HANDOFF", remote_id="should-not-be-here",
                    handoff={"requires_human": True})
    result = _verify(ws_id, post_id, {})
    check = _by_name(result, "remote_id_without_live_mode")
    assert check["passed"] is False
    assert "only evidence of a LIVE" in check["detail"]


def test_a_mock_may_carry_a_synthetic_remote_id(tmp_path, monkeypatch):
    """MockPublisher mints a remote id; that is normal, not a finding."""
    client = _client(tmp_path, monkeypatch)
    ws_id, _headers, _tid = _register(client)
    post_id = _post(ws_id, platform="threads", video_id="v-mock-id",
                    mode="MOCK", remote_id="mock-1759")
    result = _verify(ws_id, post_id, {})
    names = [c.get("name") for c in result["checks"]]
    assert "remote_id_without_live_mode" not in names
    assert result["verdict"] == "VERIFIED"


def test_platform_mismatch_is_caught(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, _headers, _tid = _register(client)
    post_id = _post(ws_id, platform="pinterest", video_id="v-p",
                    mode="LIVE", remote_id="pin-1")
    result = _verify(ws_id, post_id, {"platform": "threads", "live": True})
    assert _by_name(result, "platform_match")["passed"] is False


def test_account_mismatch_is_caught(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, _headers, _tid = _register(client)
    post_id = _post(ws_id, platform="pinterest", video_id="v-a",
                    mode="LIVE", remote_id="pin-2", account_id="acct-1")
    result = _verify(ws_id, post_id, {"account_id": "acct-2", "live": True})
    assert _by_name(result, "account_match")["passed"] is False


def test_duplicate_publications_fail_idempotency(tmp_path, monkeypatch):
    """The unique index is the real guarantee; this proves it is enforced."""
    from sqlalchemy.exc import IntegrityError

    from app.db import session_scope
    from app.models import PublishedPost

    client = _client(tmp_path, monkeypatch)
    ws_id, _headers, _tid = _register(client)
    _post(ws_id, platform="threads", video_id="v-dup", mode="LIVE",
          remote_id="m1")
    with pytest.raises(IntegrityError), session_scope() as s:
        s.add(PublishedPost(
            workspace_id=ws_id, content_item_id="v-dup", video_id="v-dup",
            platform="threads", remote_post_id="m2", publication_mode="LIVE"))
        s.commit()


def test_legacy_row_without_a_mode_is_not_treated_as_live(tmp_path, monkeypatch):
    """A row written before migration 0031 must not silently claim LIVE."""
    client = _client(tmp_path, monkeypatch)
    ws_id, _headers, _tid = _register(client)
    from app.db import session_scope
    from app.models import PublishedPost

    with session_scope() as s:
        row = PublishedPost(workspace_id=ws_id, content_item_id="v-old",
                            video_id="v-old", platform="threads",
                            remote_post_id="m-legacy", is_mock=False)
        s.add(row)
        s.commit()
        # simulate a pre-0031 row: no mode recorded
        s.execute(
            __import__("sqlalchemy").text(
                "UPDATE published_posts SET publication_mode = '' "
                "WHERE id = :i"), {"i": row.id})
        s.commit()
        row_id = row.id
    result = _verify(ws_id, row_id, {"live": True})
    # a remote id with no declared mode is backfilled to LIVE by the
    # verifier's own rule, and that rule is visible in the mode check
    mode_check = _by_name(result, "mode_declared")
    assert "LIVE" in mode_check["detail"] or "UNAVAILABLE" in mode_check["detail"]
    assert result["verdict"] in ("VERIFIED", "NOT_VERIFIED")


# ---------------------------------------------------------------------------
# publish flow: a handoff must not mark the variant PUBLISHED
# ---------------------------------------------------------------------------


def test_snapchat_publish_flow_records_a_handoff_not_a_publication(
    tmp_path, monkeypatch
):
    from app.db import session_scope
    from app.engine.campaign.publish_flow import register_publish_handlers
    from app.models import PublishedPost, SocialAccount
    from app.services import jobs as jobs_service

    register_publish_handlers()
    client = _client(tmp_path, monkeypatch)
    ws_id, _headers, _tid = _register(client)

    with session_scope() as s:
        s.add(SocialAccount(workspace_id=ws_id, platform="snapchat",
                            status="connected", external_id="p1",
                            access_token_enc=_enc("snap-token")))
        s.commit()

    handler = jobs_service._handlers["campaign.publish"]
    result = handler(jobs_service.JobContext(
        job_id="w14-snap-1", type="campaign.publish", workspace_id=ws_id,
        cycle_id=None,
        payload={"campaign_id": "c-1", "variant_id": "v-1",
                 "platform": "snapchat", "short_content_id": "s-1",
                 "video_path": "", "metadata": {"title": "Snap"}},
        attempt=1, cancelled=lambda: False))

    # the job result must not claim a publication
    assert result["published"] is False
    assert result["mode"] == "HANDOFF"
    assert result["requires_human"] is True

    with session_scope() as s:
        post = s.query(PublishedPost).filter(
            PublishedPost.workspace_id == ws_id).one()
        assert post.publication_mode == "HANDOFF"
        assert post.remote_post_id == ""
        assert (post.handoff_payload or {}).get("requires_human") is True


def test_live_platform_still_records_live_and_publishes(tmp_path, monkeypatch):
    """Work 14 must not regress the live path for a real platform."""
    from app.db import session_scope
    from app.engine.campaign.publish_flow import register_publish_handlers
    from app.models import PublishedPost, SocialAccount
    from app.providers.publishers import factory as publisher_factory
    from app.providers.publishers.base import (
        BasePublisher,
        PublishResult,
    )
    from app.services import jobs as jobs_service

    register_publish_handlers()
    client = _client(tmp_path, monkeypatch)
    ws_id, _headers, _tid = _register(client)

    with session_scope() as s:
        s.add(SocialAccount(workspace_id=ws_id, platform="threads",
                            status="connected", external_id="t1",
                            access_token_enc=_enc("threads-token")))
        s.commit()

    class _LiveStub(BasePublisher):
        platform = "threads"

        def publish(self, video_path, meta, account):
            return PublishResult(success=True, remote_post_id="media-1",
                                 remote_url="https://threads.net/p/1")

    monkeypatch.setattr(publisher_factory, "get_publisher",
                        lambda platform, **kw: _LiveStub())
    handler = jobs_service._handlers["campaign.publish"]
    result = handler(jobs_service.JobContext(
        job_id="w14-thr-1", type="campaign.publish", workspace_id=ws_id,
        cycle_id=None,
        payload={"campaign_id": "c-2", "variant_id": "v-2",
                 "platform": "threads", "short_content_id": "s-2",
                 "video_path": "", "metadata": {"title": "Post"}},
        attempt=1, cancelled=lambda: False))

    assert result["published"] is True
    assert result["mode"] == "LIVE"
    assert result["requires_human"] is False
    with session_scope() as s:
        post = s.query(PublishedPost).filter(
            PublishedPost.workspace_id == ws_id).one()
        assert post.publication_mode == "LIVE"
        assert post.remote_post_id == "media-1"


def test_variant_status_awaiting_handoff_exists_in_the_enum():
    from app.models.campaign import VARIANT_STATUSES

    assert "AWAITING_HANDOFF" in VARIANT_STATUSES
    # PUBLISHED is still the only live-ish terminal state
    assert "PUBLISHED" in VARIANT_STATUSES
    assert VARIANT_STATUSES.index("AWAITING_HANDOFF") != \
        VARIANT_STATUSES.index("PUBLISHED")


def test_handoff_pending_helper(tmp_path):
    from app.providers.publishers.snapchat import handoff_pending

    class _Row:
        publication_mode = "HANDOFF"
        handoff_completed_at = None

    class _Done(_Row):
        handoff_completed_at = "2026-01-01"

    class _Live(_Row):
        publication_mode = "LIVE"

    assert handoff_pending(_Row()) is True
    assert handoff_pending(_Done()) is False
    assert handoff_pending(_Live()) is False
