"""Work 11 Lane L: notification events (contracts §10).

Tests MY side of the contract directly -- the event->kind mapping and
the recipient resolution -- because lanes R (reviews/comments) and X
(exports) may not have landed their emitters when this runs. The rows
are seeded here exactly as those lanes would create them, so the
recipient logic is exercised end-to-end without depending on a
sibling lane's import.

Covered:
  * every event kind in EVENT_KIND_MAP produces the right notification
    kind for the right people
  * the actor is never notified about their own action
  * mention -> only the mentioned users
  * export completed/failed -> the job creator
  * an unknown event kind is a no-op (returns 0)
  * the API returns OWN rows only, and a foreign notification id is 404
  * read / read-all are idempotent and unread_only filters correctly
"""
from __future__ import annotations

import uuid

import pytest

from app.db import session_scope
from app.services import notifications as ns


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _seed_workspace(ws_role="admin"):
    from app.models import User, Workspace, WorkspaceMember

    tag = uuid.uuid4().hex[:8]
    with session_scope() as s:
        owner = User(email=f"own{tag}@test.local", password_hash="x")
        ws = Workspace(name="notify", slug=f"nt-{tag}", niche="AI")
        reviewer = User(email=f"rev{tag}@test.local", password_hash="x")
        author = User(email=f"aut{tag}@test.local", password_hash="x")
        outsider = User(email=f"out{tag}@test.local", password_hash="x")
        s.add_all([owner, ws, reviewer, author, outsider])
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=owner.id, role=ws_role))
        for extra in (reviewer, author):
            s.add(WorkspaceMember(workspace_id=ws.id, user_id=extra.id, role="member"))
        # outsider is a user of ANOTHER workspace entirely
        other = Workspace(name="other", slug=f"ot-{tag}", niche="AI")
        s.add(other)
        s.flush()
        s.add(WorkspaceMember(workspace_id=other.id, user_id=outsider.id, role="owner"))
        return {
            "ws": ws.id, "other_ws": other.id,
            "owner": owner.id, "reviewer": reviewer.id,
            "author": author.id, "outsider": outsider.id,
        }


def _seed_review(ids, *, state="IN_REVIEW", target_id="tl-1", assignees=("reviewer",)):
    from app.models import Review, ReviewAssignment

    with session_scope() as s:
        review = Review(
            workspace_id=ids["ws"], target_type="timeline", target_id=target_id,
            title="Review me", state=state, created_by=ids["author"],
        )
        s.add(review)
        s.flush()
        for who in assignees:
            s.add(ReviewAssignment(review_id=review.id, user_id=ids[who],
                                   assigned_by=ids["owner"]))
        return review.id


def _seed_export(ids, *, state="COMPLETE", created_by="owner"):
    from app.models import ExportJob

    with session_scope() as s:
        job = ExportJob(
            workspace_id=ids["ws"], format="MP4", target_type="timeline",
            target_id="tl-1", state=state, created_by=ids[created_by],
        )
        s.add(job)
        s.flush()
        return job.id


def _rows_for(user_id, ws_id=None):
    from app.models import Notification

    with session_scope() as s:
        return [
            {"kind": row.kind, "payload": dict(row.payload_json or {})}
            for row in s.query(Notification).filter(Notification.user_id == user_id).all()
        ]


# ---------------------------------------------------------------------------
# mapping + recipients
# ---------------------------------------------------------------------------


def test_review_events_notify_creator_and_assignees():
    ids = _seed_workspace()
    review_id = _seed_review(ids, assignees=("reviewer",))

    for event_kind, expected in (
        ("REVIEW_ASSIGNED", ns.KIND_REVIEW_ASSIGNED),
        ("CHANGES_REQUESTED", ns.KIND_CHANGES_REQUESTED),
        ("APPROVED", ns.KIND_REVIEW_APPROVED),
    ):
        with session_scope() as s:
            # the reviewer performs the action -> they are NOT notified
            created = ns.on_event(
                s, ids["ws"], event_kind,
                {"actor": ids["reviewer"], "review_id": review_id,
                 "target": {"type": "timeline", "id": "tl-1"}},
            )
        assert created == 1, event_kind  # the review creator
        kinds = {row["kind"] for row in _rows_for(ids["author"])}
        assert expected in kinds, (event_kind, kinds)


def test_review_event_never_notifies_the_actor():
    """The review CREATOR performs the action -> they get no inbox row,
    but the assignee on the other side still does."""
    ids = _seed_workspace()
    review_id = _seed_review(ids, assignees=("reviewer",))
    with session_scope() as s:
        created = ns.on_event(
            s, ids["ws"], "APPROVED",
            {"actor": ids["author"], "review_id": review_id},  # author IS the creator
        )
    assert created == 1  # only the assignee
    assert _rows_for(ids["author"]) == []
    assert [row["kind"] for row in _rows_for(ids["reviewer"])] == [ns.KIND_REVIEW_APPROVED]


def test_mention_notifies_only_the_mentioned_user():
    ids = _seed_workspace()
    with session_scope() as s:
        created = ns.on_event(
            s, ids["ws"], "COMMENT_ADDED",
            {"actor": ids["author"], "mentions": [ids["reviewer"]],
             "target": {"type": "timeline", "id": "tl-1"}},
        )
    assert created == 1
    mentioned = _rows_for(ids["reviewer"])
    assert [row["kind"] for row in mentioned] == [ns.KIND_COMMENT_MENTION]
    # nobody else got one
    assert _rows_for(ids["owner"]) == []
    assert _rows_for(ids["author"]) == []


def test_export_events_notify_the_job_creator():
    ids = _seed_workspace()
    export_id = _seed_export(ids, created_by="reviewer")
    for event_kind, expected in (
        ("EXPORT_COMPLETED", ns.KIND_EXPORT_COMPLETED),
        ("EXPORT_FAILED", ns.KIND_EXPORT_FAILED),
    ):
        with session_scope() as s:
            created = ns.on_event(
                s, ids["ws"], event_kind,
                {"actor": ids["author"], "export_id": export_id},
            )
        assert created == 1, event_kind
    kinds = {row["kind"] for row in _rows_for(ids["reviewer"])}
    assert kinds == {ns.KIND_EXPORT_COMPLETED, ns.KIND_EXPORT_FAILED}
    assert _rows_for(ids["author"]) == []


def test_unknown_event_kind_is_a_noop():
    ids = _seed_workspace()
    with session_scope() as s:
        assert ns.on_event(s, ids["ws"], "SOMETHING_ELSE", {"actor": ids["owner"]}) == 0
    assert _rows_for(ids["owner"]) == []


def test_notify_rejects_unknown_kind():
    ids = _seed_workspace()
    with session_scope() as s, pytest.raises(ValueError):
        ns.notify(s, ids["ws"], ids["owner"], "not.a.kind", {})


# ---------------------------------------------------------------------------
# API: own rows only
# ---------------------------------------------------------------------------


def _api_user(ws_id, email_role):
    """Register a user directly in a workspace and return (id, headers)."""
    from app.core.security import create_access_token
    from app.models import User, WorkspaceMember

    tag = uuid.uuid4().hex[:8]
    email = f"{email_role}{tag}@test.local"
    with session_scope() as s:
        user = User(email=email, password_hash="x")
        s.add(user)
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws_id, user_id=user.id, role="member"))
        user_id = user.id
    return user_id, {"Authorization": f"Bearer {create_access_token(user_id)}"}


def test_notifications_api_returns_own_rows_only(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, _, headers = _register_through_api(client)
    mine_id, mine = _api_user(ws_id, "mine")
    other_id, other = _api_user(ws_id, "other")

    with session_scope() as s:
        ns.notify(s, ws_id, mine_id, ns.KIND_REVIEW_ASSIGNED, {"review_id": "r1"})
        ns.notify(s, ws_id, mine_id, ns.KIND_COMMENT_MENTION, {"comment_id": "c1"})
        ns.notify(s, ws_id, other_id, ns.KIND_REVIEW_ASSIGNED, {"review_id": "r2"})

    body = client.get(f"/api/v1/workspaces/{ws_id}/notifications", headers=mine).json()
    assert body["count"] == 2
    assert {item["kind"] for item in body["items"]} == {
        ns.KIND_REVIEW_ASSIGNED, ns.KIND_COMMENT_MENTION
    }
    # the other user's row is invisible
    assert all(item["user_id"] == mine_id for item in body["items"])
    assert len(client.get(f"/api/v1/workspaces/{ws_id}/notifications",
                          headers=other).json()["items"]) == 1

    # unread_only narrows it
    unread = client.get(
        f"/api/v1/workspaces/{ws_id}/notifications?unread_only=true", headers=mine
    ).json()
    assert unread["count"] == 2
    assert unread["unread"] == 2


def _register_through_api(client):
    email = f"nh{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], data["user"]["id"], {
        "Authorization": f"Bearer {data['access_token']}"
    }


def test_mark_read_and_read_all(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, _, _ = _register_through_api(client)
    mine_id, mine = _api_user(ws_id, "mine")
    other_id, other = _api_user(ws_id, "other")

    with session_scope() as s:
        first = ns.notify(s, ws_id, mine_id, ns.KIND_REVIEW_ASSIGNED, {"n": 1})
        ns.notify(s, ws_id, mine_id, ns.KIND_COMMENT_MENTION, {"n": 2})
        theirs = ns.notify(s, ws_id, other_id, ns.KIND_REVIEW_ASSIGNED, {"n": 3})
    first_id = first["id"]

    marked = client.post(
        f"/api/v1/workspaces/{ws_id}/notifications/{first_id}/read", headers=mine
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["unread"] == 1

    # idempotent: reading an already-read notification still succeeds
    again = client.post(
        f"/api/v1/workspaces/{ws_id}/notifications/{first_id}/read", headers=mine
    )
    assert again.status_code == 200

    # somebody else's notification is 404, never 403
    stolen = client.post(
        f"/api/v1/workspaces/{ws_id}/notifications/{theirs['id']}/read", headers=mine
    )
    assert stolen.status_code == 404, stolen.text

    all_read = client.post(
        f"/api/v1/workspaces/{ws_id}/notifications/read-all", headers=mine
    )
    assert all_read.status_code == 200, all_read.text
    assert all_read.json()["unread"] == 0
    # read-all did not touch the other user's inbox
    assert client.get(
        f"/api/v1/workspaces/{ws_id}/notifications", headers=other
    ).json()["unread"] == 1


def test_notifications_workspace_isolation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_a, _, _ = _register_through_api(client)
    ws_b, _, headers_b = _register_through_api(client)
    b_id, b_headers = _api_user(ws_b, "binb")

    with session_scope() as s:
        ns.notify(s, ws_b, b_id, ns.KIND_EXPORT_COMPLETED, {"export_id": "e1"})

    # the row exists in B ...
    assert len(client.get(f"/api/v1/workspaces/{ws_b}/notifications",
                          headers=b_headers).json()["items"]) == 1
    # ... and A cannot read it by guessing the path
    assert client.get(f"/api/v1/workspaces/{ws_a}/notifications",
                      headers=b_headers).status_code == 403


def test_recipient_resolution_never_crosses_workspaces():
    """A foreign user named in an event payload gets NO inbox row.

    Recipient resolution is a WRITE path, so workspace isolation has to
    hold here too: ``ids["outsider"]`` is a real user (it owns a
    different workspace), and the review/comment/export rows it is
    smuggled in through belong to a workspace it is not a member of.
    """
    ids = _seed_workspace()
    review_id = _seed_review(ids, assignees=("reviewer",))
    export_id = _seed_export(ids, created_by="reviewer")

    # 1. named directly in the payload. The legitimate members reached by
    # the same event (review creator / export creator) still get their
    # rows -- what must never happen is the OUTSIDER getting one.
    for kind, payload in (
        ("REVIEW_ASSIGNED", {"assignee_user_id": ids["outsider"]}),
        ("COMMENT_ADDED", {"mentions": [ids["outsider"]]}),
        ("EXPORT_COMPLETED", {"created_by": ids["outsider"]}),
    ):
        with session_scope() as s:
            ns.on_event(
                s, ids["ws"], kind,
                {"actor": ids["author"], "review_id": review_id,
                 "export_id": export_id, **payload},
            )
        assert _rows_for(ids["outsider"]) == [], (kind, "a non-member was notified")

    # 2. smuggled through a real review whose assignee is the outsider
    from app.models import ReviewAssignment

    with session_scope() as s:
        s.add(ReviewAssignment(review_id=review_id, user_id=ids["outsider"],
                               assigned_by=ids["owner"]))
    with session_scope() as s:
        assert ns.on_event(
            s, ids["ws"], "APPROVED", {"actor": ids["author"], "review_id": review_id}
        ) == 1  # only the review creator
    assert _rows_for(ids["outsider"]) == []

    # 3. the outsider cannot even read an inbox they were never given
    from app.db import SessionLocal
    from app.models import Notification

    session = SessionLocal()
    try:
        leaked = session.query(Notification).filter(
            Notification.workspace_id == ids["ws"],
            Notification.user_id == ids["outsider"],
        ).count()
    finally:
        session.close()
    assert leaked == 0


def test_recipients_from_another_workspace_row_resolve_to_nothing():
    """A review id from workspace B must not name recipients in A."""
    ids = _seed_workspace()
    other = _seed_workspace()

    # a review that lives in the OTHER workspace, naming our members
    from app.models import Review

    with session_scope() as s:
        s.add(Review(workspace_id=other["ws"], target_type="timeline",
                     target_id="tl-x", title="foreign", state="IN_REVIEW",
                     created_by=other["owner"]))
    foreign_review = _last_review_id(other["ws"])

    with session_scope() as s:
        assert ns.on_event(
            s, ids["ws"], "APPROVED",
            {"actor": ids["author"], "review_id": foreign_review},
        ) == 0
    for who in ("owner", "reviewer", "author", "outsider"):
        assert _rows_for(ids[who]) == [], who


def _last_review_id(workspace_id):
    from app.models import Review

    with session_scope() as s:
        rows = (
            s.query(Review)
            .filter(Review.workspace_id == workspace_id)
            .order_by(Review.created_at.desc())
            .all()
        )
        return rows[0].id


# ---------------------------------------------------------------------------
# contracts 10 details the mapping tests above must not paper over
# ---------------------------------------------------------------------------


def test_export_receipt_reaches_the_creator_lane_x_style():
    """Lane X fans out with the NOTIFICATION spelling and actor = creator.

    Both choices used to drop the row: the map only knew the ledger kind
    (``EXPORT_COMPLETED``), and self-suppression silenced the one
    recipient the contract names for export receipts. This is the real
    emission shape from ``engine/exporter/jobs.py::_notify``.
    """
    ids = _seed_workspace()
    export_id = _seed_export(ids, created_by="reviewer")

    with session_scope() as s:
        created = ns.on_event(
            s, ids["ws"], "export.completed",
            {"actor": ids["reviewer"], "export_id": export_id, "checksum": "ab"},
        )
    assert created == 1
    rows = _rows_for(ids["reviewer"])
    assert [row["kind"] for row in rows] == [ns.KIND_EXPORT_COMPLETED]
    assert rows[0]["payload"]["export_id"] == export_id
    # the ledger spelling still maps to the same row
    with session_scope() as s:
        assert ns.on_event(
            s, ids["ws"], "EXPORT_FAILED",
            {"actor": ids["reviewer"], "export_id": export_id},
        ) == 1
    assert {row["kind"] for row in _rows_for(ids["reviewer"])} == {
        ns.KIND_EXPORT_COMPLETED, ns.KIND_EXPORT_FAILED
    }
    # and nobody else is pulled in
    assert _rows_for(ids["author"]) == []


def test_list_is_newest_first_even_when_timestamps_tie(tmp_path, monkeypatch):
    """Two rows written in the same clock tick keep their append order."""
    client = _client(tmp_path, monkeypatch)
    ws_id, _, headers = _register_through_api(client)
    me_id, me = _api_user(ws_id, "tie")

    with session_scope() as s:
        for index in range(3):
            ns.notify(s, ws_id, me_id, ns.KIND_REVIEW_ASSIGNED, {"n": index})

    items = client.get(
        f"/api/v1/workspaces/{ws_id}/notifications", headers=me
    ).json()["items"]
    assert [item["payload"]["n"] for item in items] == [2, 1, 0]
    times = [item["created_at"] for item in items]
    assert times == sorted(times, reverse=True)


def test_notifications_api_exposes_no_create_route(tmp_path, monkeypatch):
    """Inbox rows are written by the service only -- no POST on the collection."""
    client = _client(tmp_path, monkeypatch)
    spec = client.app.openapi()
    base = "/api/v1/workspaces/{workspace_id}/notifications"
    assert base in spec["paths"], sorted(spec["paths"])
    assert set(spec["paths"][base]) == {"get"}, sorted(spec["paths"][base])
    assert set(spec["paths"].get(f"{base}/read-all", {})) == {"post"}
    assert set(spec["paths"].get(f"{base}/{{notification_id}}/read", {})) == {"post"}


def test_notifications_500_is_generic_and_logged(tmp_path, monkeypatch, caplog):
    """Unexpected faults answer a short generic detail -- never the exception."""
    import logging

    client = _client(tmp_path, monkeypatch)
    ws_id, _, headers = _register_through_api(client)
    from app.api.v1 import notifications as api_mod

    def boom(*_args, **_kwargs):
        raise RuntimeError("inbox exploded secret=letmein")

    monkeypatch.setattr(api_mod.notifications_service, "list_notifications", boom)
    with caplog.at_level(logging.ERROR, logger="ymoney.collab"):
        r = client.get(f"/api/v1/workspaces/{ws_id}/notifications", headers=headers)
    assert r.status_code == 500, r.text
    assert r.json() == {"detail": "internal error"}
    assert "letmein" not in r.text
    assert "RuntimeError" not in r.text
    assert any(
        record.name == "ymoney.collab"
        and "notifications route failed" in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


# ---------------------------------------------------------------------------
# emission wiring (sibling lanes R and X own the call sites)
# ---------------------------------------------------------------------------


def test_reviews_lane_emit_writes_ledger_and_inbox_row():
    """Lane R's ``_emit`` must reach BOTH the ledger and this inbox."""
    ids = _seed_workspace()
    review_id = _seed_review(ids, assignees=("reviewer",))
    # both lanes have landed (contracts 1 lane map), so a missing symbol is
    # a genuine wiring regression, not an expected state to skip over
    from app.engine.collab import reviews as reviews_mod
    from app.models import EventLog, Review

    assert callable(getattr(reviews_mod, "_emit", None)), "reviews._emit vanished"
    with session_scope() as s:
        review = s.get(Review, review_id)
    reviews_mod._emit(
        ids["ws"], "REVIEW_ASSIGNED", "Reviewer assigned",
        actor=ids["owner"], review=review,
    )

    assert [row["kind"] for row in _rows_for(ids["reviewer"])] == [
        ns.KIND_REVIEW_ASSIGNED
    ]
    # the review creator is a named recipient of an assignment ...
    assert [row["kind"] for row in _rows_for(ids["author"])] == [
        ns.KIND_REVIEW_ASSIGNED
    ]
    # ... and the actor (the owner who assigned) is never notified
    assert _rows_for(ids["owner"]) == []
    with session_scope() as s:
        kinds = [
            row.kind
            for row in s.query(EventLog)
            .filter(EventLog.workspace_id == ids["ws"],
                    EventLog.kind == "REVIEW_ASSIGNED")
            .all()
        ]
    assert kinds == ["REVIEW_ASSIGNED"]


def test_export_lane_notify_writes_the_creator_inbox_row():
    """Lane X's ``_notify`` must reach this inbox (it speaks its own dialect)."""
    ids = _seed_workspace()
    export_id = _seed_export(ids, created_by="reviewer")
    from app.engine.exporter import jobs as jobs_mod
    from app.models import ExportJob

    assert callable(getattr(jobs_mod, "_notify", None)), "exporter._notify vanished"
    with session_scope() as s:
        row = s.get(ExportJob, export_id)
    jobs_mod._notify(ids["ws"], "export.completed", row, {"checksum": "abc"})

    assert [r["kind"] for r in _rows_for(ids["reviewer"])] == [
        ns.KIND_EXPORT_COMPLETED
    ]
    assert _rows_for(ids["author"]) == []
