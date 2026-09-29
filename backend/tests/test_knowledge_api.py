"""Work 10 Lane D: knowledge API (backend/app/api/v1/knowledge.py).

Covers the full 12-route surface (dual-mounted exactly like the inbox) with
the repo's API-test fixture style (register → workspace-scoped bearer →
TestClient):

  * RBAC matrix — viewer reads / member memory+promote / admin connectors
  * dual mount: /workspaces/{id}/knowledge/... == /knowledge/?workspace_id=...
  * memories — enum validation, ?type/?status/?q filters, verify UNVERIFIED →
    ACTIVE re-read from a FRESH session, disable idempotency, supersede and
    foreign/missing ids → 404 with a short detail
  * graph — EXACT ``{nodes, edges}`` shape, node_type filter, 422 on an
    unknown type, cross-workspace isolation, honest empty payload
  * sources — config never leaves the server (redaction + has_credentials),
    unknown/catalog-only/bad-config kinds → 422, sync enqueue + in-flight
    dedupe, connector-scoped documents, disconnect idempotency
  * community signals — numeric confidence, evidence_count, meets_threshold /
    low_confidence flags; promote → ``{"promoted": int, "items": [...]}``
    and idempotency across runs
  * retrieve — ``{items, metrics, ranking}`` + max_results clamp
  * calendar/response-windows — the scheduler's honesty keys verbatim
  * guarded ``_bootstrap_source_jobs`` idempotency (test_jobs_bootstrap style)
  * error hygiene — a RuntimeError behind the route is a generic 500 that
    never echoes the exception

Seeds always commit through ``session_scope`` before any HTTP call (the
handler runs in its own request session); rows written by a handler are
re-read through a NEW session, never through the request session.
"""
from __future__ import annotations

import uuid

from app.api.v1 import knowledge as knowledge_mod

# ---------------------------------------------------------------------------
# fixtures / helpers (mirror tests/test_inbox_api.py)
# ---------------------------------------------------------------------------


def _register(client, email=None):
    email = email or f"kn{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _login_headers(client, email):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _make_user(client, ws_id, role):
    """Register a fresh user, grant them `role` on ws_id, return their headers."""
    from app.db import session_scope
    from app.models import WorkspaceMember

    email = f"{role.lower()}{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    user_id = r.json()["user"]["id"]
    with session_scope() as s:
        s.add(WorkspaceMember(workspace_id=ws_id, user_id=user_id, role=role))
    return _login_headers(client, email)


def _base(ws_id):
    return f"/api/v1/workspaces/{ws_id}/knowledge"


def _flat(path=""):
    return f"/api/v1/knowledge{path}"


# ---------------------------------------------------------------------------
# seed helpers (commit before any request; repo convention: fresh-session reads)
# ---------------------------------------------------------------------------


def _seed_memory(ws_id, *, content, topic="", type="SOURCE", evidence=None):
    from app.db import session_scope
    from app.engine.knowledge.memory import GlobalMemory

    with session_scope() as s:
        return GlobalMemory.store(
            s,
            ws_id,
            type=type,
            content=content,
            topic=topic,
            evidence_ids=list(evidence) if evidence else None,
        )


def _read_memory(memory_id):
    """Fresh-session re-read of one row (never the handler's session)."""
    from app.db import session_scope
    from app.models import KnowledgeMemory

    with session_scope() as s:
        row = s.get(KnowledgeMemory, memory_id)
        return None if row is None else {"id": row.id, "status": row.status}


def _seed_graph(ws_id, *, foreign_ws=None):
    from app.db import session_scope
    from app.engine.knowledge.graph import KnowledgeGraphProvider

    with session_scope() as s:
        graph = KnowledgeGraphProvider()
        content = graph.upsert_node(
            s, ws_id, node_type="Content", ref_id="c-1", label="Video One"
        )
        topic = graph.upsert_node(s, ws_id, node_type="Topic", label="Keto Diet")
        graph.link(
            s, ws_id, from_node=content, to_node=topic, relationship="CONTENT_ABOUT_TOPIC"
        )
        if foreign_ws:
            graph.upsert_node(
                s, foreign_ws, node_type="Content", ref_id="foreign-1", label="Foreign Video"
            )


def _seed_documents(workspace_id, connector_id, rows):
    from app.db import session_scope
    from app.models import SourceDocument

    with session_scope() as s:
        for remote_id, title in rows:
            s.add(
                SourceDocument(
                    workspace_id=workspace_id,
                    connector_id=connector_id,
                    remote_id=remote_id,
                    title=title,
                    content="stored body",
                )
            )


def _seed_secret_connector(ws_id, name):
    """A row written OUTSIDE the API, holding a literal secret in its config."""
    from app.db import session_scope
    from app.models import SourceConnector

    with session_scope() as s:
        row = SourceConnector(
            workspace_id=ws_id, kind="local", name=name,
            config_json={"api_token": "SUPERSECRET123"},
        )
        s.add(row)
        s.flush()
        return row.id


# three rephrasings of ONE normalized topic → hits the evidence threshold (3)
_API_TOPIC_TEXTS = (
    "How do I set up the API integration?",
    "how do i set up the api integration",
    "SET UP THE API INTEGRATION please",
)
# a second topic with only 2 interactions → stays below the threshold
_TRIPOD_TEXTS = (
    "best tripods for filming",
    "BEST TRIPODS FOR FILMING",
)


def _seed_signals(ws_id):
    """Seed one at-threshold and one below-threshold insight (insight style)."""
    from app.db import session_scope
    from app.engine.community.insight import record_insight
    from app.models import SocialAccount
    from app.models.community import SocialInteraction

    with session_scope() as s:
        account = SocialAccount(
            workspace_id=ws_id, platform="youtube",
            display_name="yt-main", access_token_enc="enc-at-rest",
        )
        s.add(account)
        s.flush()

        def add_insight(texts):
            row = None
            for text in texts:
                interaction = SocialInteraction(
                    workspace_id=ws_id, platform="youtube", account_id=account.id,
                    remote_id=f"r-{uuid.uuid4().hex[:12]}", text=text,
                    status="classified", classifications_json=[],
                )
                s.add(interaction)
                s.flush()
                row = record_insight(s, ws_id, interaction)
            return row

        at_threshold = add_insight(_API_TOPIC_TEXTS)
        below = add_insight(_TRIPOD_TEXTS)
        return str(at_threshold.id), str(below.id)


def _register_source(client, headers, base, *, name, kind="local", config=None):
    r = client.post(
        f"{base}/sources", headers=headers,
        json={"kind": kind, "name": name, "config": config or {}},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


def test_rbac_matrix_viewer_member_admin(tmp_path, monkeypatch):
    from app.models import WorkspaceMember

    client = _client(tmp_path, monkeypatch)
    ws, _h_owner = _register(client)
    h_viewer = _make_user(client, ws, WorkspaceMember.ROLE_VIEWER)
    h_member = _make_user(client, ws, WorkspaceMember.ROLE_MEMBER)
    h_admin = _make_user(client, ws, WorkspaceMember.ROLE_ADMIN)
    base = _base(ws)

    target = _seed_memory(ws, content="rbac target memory")
    replacement = _seed_memory(ws, content="rbac replacement memory")

    # viewers read everything
    for path in ("/memories", "/graph", "/sources", "/community-signals", "/retrieve"):
        r = client.get(f"{base}{path}", headers=h_viewer)
        assert r.status_code == 200, (path, r.status_code, r.text)

    # …but never write — every memory/connector mutation (dependency resolves
    # before the handler, so dummy ids are fine here)
    calls = (
        ("create", client.post(f"{base}/memories", headers=h_viewer,
                               json={"type": "SOURCE", "content": "nope"})),
        ("verify", client.post(f"{base}/memories/{target['id']}/verify", headers=h_viewer)),
        ("disable", client.post(f"{base}/memories/{target['id']}/disable", headers=h_viewer)),
        ("supersede", client.post(f"{base}/memories/{target['id']}/supersede",
                                  headers=h_viewer,
                                  json={"replacement_id": replacement["id"]})),
        ("promote", client.post(f"{base}/promote-insights", headers=h_viewer, json={})),
        ("register", client.post(f"{base}/sources", headers=h_viewer,
                                 json={"kind": "local", "name": "viewer-src"})),
        ("sync", client.post(f"{base}/sources/dummy/sync", headers=h_viewer)),
        ("disconnect", client.post(f"{base}/sources/dummy/disconnect", headers=h_viewer)),
    )
    for name, r in calls:
        assert r.status_code == 403, (name, r.status_code, r.text)

    # members own the whole memory surface …
    r = client.post(f"{base}/memories", headers=h_member,
                    json={"type": "SOURCE", "content": "member write"})
    assert r.status_code == 200, r.text
    member_id = r.json()["id"]
    for name, r in (
        ("verify", client.post(f"{base}/memories/{member_id}/verify", headers=h_member)),
        ("supersede", client.post(
            f"{base}/memories/{member_id}/supersede", headers=h_member,
            json={"replacement_id": replacement["id"]})),
        ("disable", client.post(f"{base}/memories/{member_id}/disable", headers=h_member)),
        ("promote", client.post(f"{base}/promote-insights", headers=h_member, json={})),
    ):
        assert r.status_code == 200, (name, r.status_code, r.text)
    assert set(client.post(f"{base}/promote-insights",
                           headers=h_member, json={}).json()) >= {"promoted", "items"}

    # …and never the connector surface
    for name, r in (
        ("register", client.post(f"{base}/sources", headers=h_member,
                                 json={"kind": "local", "name": "member-src"})),
        ("sync", client.post(f"{base}/sources/dummy/sync", headers=h_member)),
        ("disconnect", client.post(f"{base}/sources/dummy/disconnect", headers=h_member)),
    ):
        assert r.status_code == 403, (name, r.status_code, r.text)

    # admins own the connectors: register 201 → sync 200 → disconnect 200 ×2
    created = _register_source(client, h_admin, base, name="admin-src")
    r = client.post(f"{base}/sources/{created['id']}/sync", headers=h_admin)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"job_id", "queued"}
    assert r.json()["queued"] is True and r.json()["job_id"]
    for _ in range(2):
        r = client.post(f"{base}/sources/{created['id']}/disconnect", headers=h_admin)
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is False

    # no token at all → 401
    assert client.get(f"{base}/memories").status_code == 401


# ---------------------------------------------------------------------------
# dual mount
# ---------------------------------------------------------------------------


def test_dual_mount_query_param_parity(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    seeded = _seed_memory(ws, content="dual mount memory")["id"]

    # canonical ws-scoped mount
    path_r = client.get(f"{_base(ws)}/memories", headers=h)
    assert path_r.status_code == 200, path_r.text
    assert set(path_r.json()) == {"items"}
    path_ids = [m["id"] for m in path_r.json()["items"]]

    # literal contract mount (?workspace_id=)
    q_r = client.get(_flat(f"/memories?workspace_id={ws}"), headers=h)
    assert q_r.status_code == 200, q_r.text
    assert set(q_r.json()) == {"items"}
    assert [m["id"] for m in q_r.json()["items"]] == path_ids
    assert seeded in path_ids

    for path in ("/graph", "/sources", "/community-signals", "/retrieve"):
        r = client.get(_flat(f"{path}?workspace_id={ws}"), headers=h)
        assert r.status_code == 200, (path, r.status_code, r.text)

    # writes work identically through the flat mount (same router, same body)
    r = client.post(_flat(f"/memories?workspace_id={ws}"), headers=h,
                    json={"type": "SOURCE", "content": "flat mount write"})
    assert r.status_code == 200, r.text
    assert r.json()["id"] not in path_ids
    r = client.post(_flat(f"/promote-insights?workspace_id={ws}"), headers=h, json={})
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"promoted", "items"}

    # the flat mount cannot resolve a workspace without the query param → 422
    assert client.get(_flat("/memories"), headers=h).status_code == 422


# ---------------------------------------------------------------------------
# memories
# ---------------------------------------------------------------------------


def test_memory_create_filters_verify_disable_supersede(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    base = _base(ws)

    # unknown type refused at the edge with a short detail
    r = client.post(f"{base}/memories", headers=h,
                    json={"type": "NOT_A_REAL_TYPE", "content": "x"})
    assert r.status_code == 422, r.text
    assert len(r.json()["detail"]) <= 180
    assert "NOT_A_REAL_TYPE" in r.json()["detail"]

    # a valid type lands as a dict carrying id + lifecycle status; a create
    # with no provenance (no source_ids/evidence_ids) is UNVERIFIED by contract
    marker = uuid.uuid4().hex
    r = client.post(f"{base}/memories", headers=h,
                    json={"type": "SOURCE", "content": f"memory {marker}",
                          "topic": f"topic {marker}", "scope": "api-test"})
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["id"] and created["status"] == "UNVERIFIED"

    # filters: ?type + ?status + ?q agree on exactly this row
    r = client.get(f"{base}/memories?type=SOURCE&status=UNVERIFIED"
                   f"&q={marker}&scope=api-test", headers=h)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"items"}
    assert [m["id"] for m in r.json()["items"]] == [created["id"]]

    # verify: UNVERIFIED → ACTIVE, re-read from a FRESH session (never the
    # handler's own session — the repo convention for handler-written rows)
    r = client.post(f"{base}/memories/{created['id']}/verify", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ACTIVE"
    assert _read_memory(created["id"])["status"] == "ACTIVE"

    # disable is idempotent and survives a fresh-session re-read
    for _ in range(2):
        r = client.post(f"{base}/memories/{created['id']}/disable", headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "DISABLED"
    assert _read_memory(created["id"])["status"] == "DISABLED"

    # supersede: missing replacement → 404 short detail, never a stack trace
    survivor = client.post(
        f"{base}/memories", headers=h,
        json={"type": "SOURCE", "content": f"survivor {marker}"},
    ).json()
    r = client.post(f"{base}/memories/{survivor['id']}/supersede", headers=h,
                    json={"replacement_id": "does-not-exist"})
    assert r.status_code == 404, r.text
    assert len(r.json()["detail"]) <= 180
    assert "Traceback" not in r.text
    assert _read_memory(survivor["id"])["status"] != "SUPERSEDED"

    # supersede is idempotent against a non-existent target too
    r = client.post(f"{base}/memories/unknown-id/supersede", headers=h,
                    json={"replacement_id": survivor["id"]})
    assert r.status_code == 404, r.text


def test_memory_foreign_ids_404(tmp_path, monkeypatch):
    from app.models import WorkspaceMember

    client = _client(tmp_path, monkeypatch)
    ws_a, h_a = _register(client)
    ws_b, _h_b = _register(client)
    base = _base(ws_a)

    foreign = _seed_memory(ws_b, content="belongs to workspace B")
    h_stranger = _make_user(client, ws_b, WorkspaceMember.ROLE_MEMBER)

    for name, r in (
        ("verify", client.post(f"{base}/memories/{foreign['id']}/verify", headers=h_a)),
        ("disable", client.post(f"{base}/memories/{foreign['id']}/disable", headers=h_a)),
        ("supersede", client.post(f"{base}/memories/{foreign['id']}/supersede",
                                  headers=h_a,
                                  json={"replacement_id": foreign["id"]})),
        ("missing", client.post(f"{base}/memories/nope-not-real/verify", headers=h_a)),
    ):
        assert r.status_code == 404, (name, r.status_code, r.text)
        assert len(r.json()["detail"]) <= 180

    # the foreign row itself is untouched by workspace A's attempts …
    assert _read_memory(foreign["id"])["status"] not in ("DISABLED", "SUPERSEDED")
    # …and its own workspace can still read it
    r = client.get(f"{_base(ws_b)}/memories", headers=h_stranger)
    assert r.status_code == 200, r.text
    assert foreign["id"] in [m["id"] for m in r.json()["items"]]


def test_memory_enum_validation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    base = _base(ws)

    r = client.get(f"{base}/memories?status=BOGUS", headers=h)
    assert r.status_code == 422, r.text
    assert len(r.json()["detail"]) <= 180

    r = client.get(f"{base}/memories?type=BOGUS", headers=h)
    assert r.status_code == 422, r.text

    # a band value from ALL_STATUSES is documented and stays 200
    r = client.get(f"{base}/memories?status=FRESH", headers=h)
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["items"], list)


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------


def test_graph_shape_filters_and_isolation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_a, h_a = _register(client)
    ws_b, _h_b = _register(client)
    _seed_graph(ws_a, foreign_ws=ws_b)

    r = client.get(f"{_base(ws_a)}/graph", headers=h_a)
    assert r.status_code == 200, r.text
    payload = r.json()
    # EXACT contract shape — two keys, no extras
    assert set(payload) == {"nodes", "edges"}
    assert isinstance(payload["nodes"], list) and payload["nodes"]
    assert isinstance(payload["edges"], list) and payload["edges"]
    assert {n["node_type"] for n in payload["nodes"]} == {"Content", "Topic"}
    assert payload["edges"][0]["relationship"] == "CONTENT_ABOUT_TOPIC"
    # cross-workspace node is absent
    assert all(n["ref_id"] != "foreign-1" for n in payload["nodes"])
    assert all(n["label"] != "Foreign Video" for n in payload["nodes"])

    # node_type filter: only that type, and no edge can survive without
    # both endpoints inside the returned node set
    r = client.get(f"{_base(ws_a)}/graph?node_type=Topic", headers=h_a)
    assert r.status_code == 200, r.text
    filtered = r.json()
    assert set(filtered) == {"nodes", "edges"}
    assert {n["node_type"] for n in filtered["nodes"]} == {"Topic"}
    assert filtered["edges"] == []

    # unknown node_type → 422 (validated before the service is touched)
    r = client.get(f"{_base(ws_a)}/graph?node_type=Idea", headers=h_a)
    assert r.status_code == 422, r.text
    assert len(r.json()["detail"]) <= 180

    # an untouched workspace still answers with both keys, honestly empty
    ws_c, h_c = _register(client)
    r = client.get(f"{_base(ws_c)}/graph", headers=h_c)
    assert r.status_code == 200, r.text
    assert r.json() == {"nodes": [], "edges": []}


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------


def test_sources_redaction_and_registration_validation(tmp_path, monkeypatch):
    from app.engine.sources import SourceConfigError

    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    base = _base(ws)

    # register a connector carrying a secret → the response must not echo it
    created = _register_source(
        client, h, base, name="media",
        config={"api_token": "SUPERSECRET123", "path": "/data"},
    )
    assert "config_json" not in created
    assert created["has_credentials"] is True
    assert created["config"] == {"path": "/data"}
    assert "SUPERSECRET123" not in repr(created)
    r = client.get(f"{base}/sources", headers=h)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"items"}
    assert "SUPERSECRET123" not in r.text
    assert "config_json" not in r.text
    row = next(x for x in r.json()["items"] if x["id"] == created["id"])
    assert row["has_credentials"] is True
    assert row["config"] == {"path": "/data"}
    assert row["enabled"] is True and row["implemented"] is True

    # a row written OUTSIDE the API is redacted by the same boundary
    seeded_id = _seed_secret_connector(ws, "out-of-band")
    r = client.get(f"{base}/sources", headers=h)
    assert r.status_code == 200, r.text
    assert "SUPERSECRET123" not in r.text
    seeded = next(x for x in r.json()["items"] if x["id"] == seeded_id)
    assert seeded["has_credentials"] is True
    assert seeded["config"] == {}
    assert seeded["enabled"] is True

    # unknown kind → 422, short detail
    r = client.post(f"{base}/sources", headers=h,
                    json={"kind": "not_a_kind", "name": "nope", "config": {}})
    assert r.status_code == 422, r.text
    assert len(r.json()["detail"]) <= 180

    # catalog-only kind → 422, honest "not implemented yet"
    r = client.post(f"{base}/sources", headers=h,
                    json={"kind": "google_drive", "name": "drive", "config": {}})
    assert r.status_code == 422, r.text
    assert "not implemented" in r.json()["detail"]

    # blank name → 422
    r = client.post(f"{base}/sources", headers=h,
                    json={"kind": "local", "name": "   ", "config": {}})
    assert r.status_code == 422, r.text

    # duplicate (kind, name) → 409
    r = client.post(f"{base}/sources", headers=h,
                    json={"kind": "local", "name": "media", "config": {}})
    assert r.status_code == 409, r.text

    # an UNIMPLEMENTED-but-configured connector registers honestly
    # UNAVAILABLE instead of fabricating a healthy state
    r = client.post(f"{base}/sources", headers=h,
                    json={"kind": "url", "name": "unconfigured-url", "config": {}})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "UNAVAILABLE"
    assert "url" in r.json()["unavailable_reason"]

    # a factory that raises maps to 422 with a single-line, ≤180-char detail
    real_create_connector = knowledge_mod.create_connector

    def _boom(*_args, **_kwargs):
        raise SourceConfigError("bad config: missing required url\n  at line 2")

    monkeypatch.setattr(knowledge_mod, "create_connector", _boom)
    r = client.post(f"{base}/sources", headers=h,
                    json={"kind": "url", "name": "boom", "config": {}})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "bad config: missing required url at line 2"
    monkeypatch.setattr(knowledge_mod, "create_connector", real_create_connector)


def test_source_sync_enqueue_dedupe_and_disconnect(tmp_path, monkeypatch):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Job, SourceConnector

    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    base = _base(ws)
    created = _register_source(client, h, base, name="sync-target")
    cid = created["id"]

    # honest enqueue shape: exactly {job_id, queued}
    r = client.post(f"{base}/sources/{cid}/sync", headers=h)
    assert r.status_code == 200, r.text
    first = r.json()
    assert set(first) == {"job_id", "queued"}
    assert first["queued"] is True and first["job_id"]

    with session_scope() as s:
        job = s.get(Job, first["job_id"])
        assert job is not None, "enqueue did not persist a job row"
        assert job.type == "SOURCE_SYNC"
        assert job.workspace_id == ws
        assert job.status == "QUEUED"
        assert job.payload == {"connector_id": cid}

    # a re-sync while that job is in flight JOINS it — no second job
    r = client.post(f"{base}/sources/{cid}/sync", headers=h)
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["job_id"] == first["job_id"]
    assert second["queued"] is False

    with session_scope() as s:
        jobs = s.scalars(
            select(Job).where(Job.type == "SOURCE_SYNC", Job.workspace_id == ws)
        ).all()
        assert len(jobs) == 1, f"dedupe produced {len(jobs)} SOURCE_SYNC jobs"

    # disconnect: idempotent + enabled False on a FRESH session re-read
    for _ in range(2):
        r = client.post(f"{base}/sources/{cid}/disconnect", headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is False
        assert r.json()["status"] == "DISABLED"
    with session_scope() as s:
        row = s.get(SourceConnector, cid)
        assert row.enabled is False and row.status == "DISABLED"

    # a disabled connector refuses to sync (409, short detail)
    r = client.post(f"{base}/sources/{cid}/sync", headers=h)
    assert r.status_code == 409, r.text
    assert len(r.json()["detail"]) <= 180

    # an unknown connector id is a scoped 404 (admin or not)
    r = client.post(f"{base}/sources/nope/sync", headers=h)
    assert r.status_code == 404, r.text
    assert len(r.json()["detail"]) <= 180
    assert client.get(f"{base}/sources/nope/documents", headers=h).status_code == 404


def test_source_documents_are_connector_scoped(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_a, h_a = _register(client)
    ws_b, h_b = _register(client)
    base_a, base_b = _base(ws_a), _base(ws_b)

    doc_a = _register_source(client, h_a, base_a, name="docs-a")
    doc_b = _register_source(client, h_a, base_a, name="docs-b")
    foreign = _register_source(client, h_b, base_b, name="docs-foreign")
    _seed_documents(ws_a, doc_a["id"], [("a1", "Alpha"), ("a2", "Beta")])
    _seed_documents(ws_a, doc_b["id"], [("b1", "Gamma")])

    r = client.get(f"{base_a}/sources/{doc_a['id']}/documents", headers=h_a)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"items"}
    items = r.json()["items"]
    assert len(items) == 2, items
    assert {d["title"] for d in items} == {"Alpha", "Beta"}
    assert all(d["connector_id"] == doc_a["id"] for d in items)
    assert "content" in items[0] and "stored body" in items[0]["content"]

    # connector B's documents never bleed into A's listing
    r = client.get(f"{base_a}/sources/{doc_b['id']}/documents", headers=h_a)
    assert [d["title"] for d in r.json()["items"]] == ["Gamma"]

    # foreign connector → 404, missing connector → 404
    assert client.get(
        f"{base_a}/sources/{foreign['id']}/documents", headers=h_a
    ).status_code == 404
    assert client.get(f"{base_a}/sources/nope/documents", headers=h_a).status_code == 404


# ---------------------------------------------------------------------------
# community signals + promote
# ---------------------------------------------------------------------------


def test_community_signals_threshold_flags(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    base = _base(ws)
    at_id, below_id = _seed_signals(ws)

    r = client.get(f"{base}/community-signals", headers=h)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"items"}
    items = r.json()["items"]
    assert len(items) >= 2, items
    by_id = {i["id"]: i for i in items}

    at = by_id[at_id]
    assert isinstance(at["confidence"], (int, float))
    assert 0.0 < float(at["confidence"]) <= 1.0
    assert isinstance(at["evidence_count"], int) and at["evidence_count"] >= 3
    assert at["meets_threshold"] is True
    assert at["low_confidence"] is False
    assert at["threshold"] >= 3
    assert at["interaction_count"] >= 3 and at["interaction_ids"]
    assert at["insight_id"] == at["id"] and at["id"]

    below = by_id[below_id]
    assert below["meets_threshold"] is False
    assert below["low_confidence"] is True
    assert isinstance(below["confidence"], (int, float))
    assert below["evidence_count"] < below["threshold"]


def test_promote_insights_idempotent(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.engine.knowledge.memory import GlobalMemory

    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    base = _base(ws)
    at_id, _below_id = _seed_signals(ws)

    r = client.post(f"{base}/promote-insights", headers=h, json={})
    assert r.status_code == 200, r.text
    first = r.json()
    assert set(first) == {"promoted", "items"}
    assert isinstance(first["promoted"], int) and first["promoted"] >= 1
    entry = next(e for e in first["items"] if e["insight_id"] == at_id)
    assert entry["promoted"] is True and entry["memory_id"]

    def _memory_count():
        with session_scope() as s:
            return len(GlobalMemory.list(s, ws, type="COMMUNITY_INSIGHT", limit=200))

    after_first = _memory_count()
    assert after_first >= 1

    # a second run must not duplicate memory rows and must stay honest
    r = client.post(f"{base}/promote-insights", headers=h, json={})
    assert r.status_code == 200, r.text
    second = r.json()
    assert set(second) == {"promoted", "items"}
    assert second["promoted"] == first["promoted"]
    assert _memory_count() == after_first, "promotion duplicated memory rows"

    # the below-threshold insight is reported, never promoted
    skipped = [e for e in second["items"] if e["promoted"] is False]
    assert skipped and all(e["memory_id"] is None for e in skipped)


# ---------------------------------------------------------------------------
# retrieve
# ---------------------------------------------------------------------------


def test_retrieve_ranking_shape_and_clamp(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    base = _base(ws)
    _seed_memory(ws, content="audience prefers short vertical tutorials",
                 topic="format")
    _seed_memory(ws, content="posting at 18:00 UTC lifts retention",
                 topic="timing")

    r = client.get(f"{base}/retrieve?task=plan+a+short", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"items", "metrics", "ranking"}
    assert isinstance(body["items"], list) and len(body["items"]) >= 2
    assert isinstance(body["metrics"], dict) and body["metrics"]["returned"] >= 2
    assert isinstance(body["ranking"]["weights"], dict) and body["ranking"]["weights"]

    # an absurd max_results is clamped, never a 422/500
    r = client.get(f"{base}/retrieve?max_results=999", headers=h)
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) <= 50
    assert set(r.json()) >= {"items", "metrics", "ranking"}


def test_empty_workspace_answers_honestly(tmp_path, monkeypatch):
    """No data → both keys with an empty list, never a fabricated row."""
    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    base = _base(ws)

    r = client.get(f"{base}/memories", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"items": []}

    r = client.get(f"{base}/sources", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"items": []}

    r = client.get(f"{base}/community-signals", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"items": []}

    r = client.post(f"{base}/promote-insights", headers=h, json={})
    assert r.status_code == 200, r.text
    assert r.json() == {"promoted": 0, "items": []}

    r = client.get(f"{base}/retrieve?task=nothing+yet", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []
    assert r.json()["metrics"]["returned"] == 0


# ---------------------------------------------------------------------------
# response windows (content.py) — the scheduler's honesty keys, verbatim
# ---------------------------------------------------------------------------


def test_response_windows_honesty_keys(tmp_path, monkeypatch):
    from app.models import WorkspaceMember

    client = _client(tmp_path, monkeypatch)
    ws, _h_owner = _register(client)
    h_viewer = _make_user(client, ws, WorkspaceMember.ROLE_VIEWER)
    url = f"/api/v1/workspaces/{ws}/calendar/response-windows"

    r = client.get(url, headers=h_viewer)
    assert r.status_code == 200, r.text
    body = r.json()
    # exact keys recommend_response_windows() returns
    assert set(body) == {
        "items", "measured", "activity", "audience_activity", "caps",
        "avoid_hours", "activity_policy", "notes",
    }
    # honesty switch: the opt-in is settings_json["schedule_automation"], so a
    # fresh workspace is a RECOMMENDATION, never an instruction
    assert body["activity_policy"] == "recommendation_only"
    assert any("schedule_automation" in n for n in body["notes"])
    assert body["measured"] is False and isinstance(body["activity"], bool)
    assert isinstance(body["avoid_hours"], list)
    assert "daily_cap" in body["caps"]
    for item in body["items"]:
        assert set(item) == {"hour", "reason", "sources"}
        assert item["sources"]

    # platform filter stays 200 and keeps the same honesty keys
    r = client.get(f"{url}?platform=youtube", headers=h_viewer)
    assert r.status_code == 200, r.text
    assert set(r.json()) == set(body)
    assert r.json()["activity_policy"] == "recommendation_only"


# ---------------------------------------------------------------------------
# bootstrap + error hygiene + openapi gate
# ---------------------------------------------------------------------------


def test_bootstrap_source_jobs_is_idempotent():
    # the app factory path imports the same module chain as the server
    import app.main  # noqa: F401
    from app.services import jobs as jobs_service

    assert "SOURCE_SYNC" in jobs_service._handlers, "SOURCE_SYNC not registered"

    before = jobs_service._handlers.get("SOURCE_SYNC")
    knowledge_mod._bootstrap_source_jobs()
    knowledge_mod._bootstrap_source_jobs()
    assert jobs_service._handlers["SOURCE_SYNC"] is before, \
        "re-running bootstrap re-registered a different handler object"


def test_error_hygiene_generic_500(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    marker = "SECRET-BOOM-9f3a"

    class _Boom:
        @staticmethod
        def list(*_args, **_kwargs):
            raise RuntimeError(f"underlying service exploded: {marker}")

    monkeypatch.setattr(knowledge_mod, "GlobalMemory", _Boom)

    r = client.get(f"{_base(ws)}/memories", headers=h)
    assert r.status_code == 500, r.text
    assert r.json() == {"detail": "internal error"}
    assert marker not in r.text
    assert "exploded" not in r.text
    assert "Traceback" not in r.text


def test_openapi_lists_every_knowledge_path(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch)  # same app-factory path as the server
    from app.main import app

    paths = list(app.openapi()["paths"])
    knowledge = [p for p in paths if "/knowledge" in p]
    assert len(knowledge) == 24, knowledge
    assert any("response-windows" in p for p in paths)
