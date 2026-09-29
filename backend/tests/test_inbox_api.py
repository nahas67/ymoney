"""Work 09 Lane D: unified social inbox API (backend/app/api/v1/inbox.py).

Covers the full endpoint surface with the repo's API-test fixture style
(register → workspace-scoped bearer → TestClient):

  * platforms registry (or the honest fallback shape)
  * interaction filters (platform/status/unread/search/classification/priority)
    + keyset pagination cursor
  * interaction detail + conversation thread ordering
  * read toggle, bulk triage cap (>100 ids → 422)
  * reply draft creation, classify/draft/approve/send engine surface
  * RBAC (viewer 403 on POST, member 403 on PUT autonomy, admin 200)
  * workspace isolation (foreign ids → 404, foreign mount → 403)
  * autonomy validation + settings_json["community"] roundtrip
  * sync job enqueue, analytics / opportunities / insights shapes
  * dual mount: /workspaces/{id}/inbox/... == /inbox/?workspace_id=...

Cross-lane engine functions (app.engine.community.*) are resolved by the API
lazily; the community lanes are landed, so these tests assert the STRICT
surface — a 200 with a real engine result — and importlib.util.find_spec is
kept only to prove that a genuinely absent module would surface honestly.
"""
from __future__ import annotations

import importlib.util
import uuid

from app.api.v1 import inbox as inbox_mod

# ---------------------------------------------------------------------------
# fixtures / helpers (mirror tests/test_work01_api.py + test_work02_api.py)
# ---------------------------------------------------------------------------


def _register(client, email=None):
    email = email or f"ib{uuid.uuid4().hex[:8]}@test.local"
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


def _seed(ws_id):
    """One conversation holding: unread QUESTION (classified), read POSITIVE,
    a spam row, a root→child→grandchild chain (thread order) and an open
    opportunity. Returns their ids in creation order."""
    from datetime import datetime, timedelta

    from app.db import session_scope
    from app.models import (
        CommunityOpportunity,
        Conversation,
        SocialAccount,
        SocialInteraction,
    )
    from app.models.base import utcnow

    # explicit, strictly increasing created_at values: the Windows clock can
    # tick at ~15ms so wall-clock stamps would tie and make ordering flaky.
    # t0 sits two days in the past so every seed always falls inside the
    # analytics 30-day window (a fixed calendar date would eventually expire
    # and silently redden the metrics assertions).
    t0 = utcnow() - timedelta(days=2)
    stamp = {"i": 0}

    def when() -> datetime:
        stamp["i"] += 1
        return t0 + timedelta(seconds=stamp["i"])

    with session_scope() as s:
        # a REAL workspace-scoped account row: send paths load it through
        # policy._load_account, which honestly refuses when it is missing.
        account_id = f"acc-{uuid.uuid4().hex[:8]}"
        s.add(SocialAccount(
            id=account_id, workspace_id=ws_id, platform="youtube",
            display_name="YT main", access_token_enc="enc-at-rest",
        ))
        conv = Conversation(
            workspace_id=ws_id, account_id=account_id, thread_key="thread-1",
            platform="youtube", title="How do I make money online?",
            last_interaction_at=when(),
        )
        s.add(conv)
        s.flush()
        conv_id = conv.id

        rows: dict[str, SocialInteraction] = {}

        def add(key: str, **kw) -> None:
            row = SocialInteraction(workspace_id=ws_id, created_at=when(),
                                    account_id=account_id, **kw)
            s.add(row)
            s.flush()
            rows[key] = row

        add("question", platform="youtube", remote_id="r-1",
            text="How do I make money online?", conversation_id=conv_id,
            status="unread", priority="normal",
            classifications_json=[{"label": "QUESTION", "confidence": 0.9}])
        add("positive", platform="youtube", remote_id="r-2",
            text="This changed my life, thank you!", conversation_id=conv_id,
            status="read", classifications_json=[{"label": "POSITIVE"}])
        add("spam", platform="reddit", remote_id="r-3",
            text="buy cheap followers now", conversation_id=conv_id,
            status="spam", moderation_state="review")
        add("root", platform="youtube", remote_id="c-1",
            text="root of the thread", conversation_id=conv_id, status="read")
        add("child", platform="youtube", remote_id="c-2",
            text="replying to root", conversation_id=conv_id, status="read",
            parent_interaction_id=rows["root"].id)
        add("grand", platform="youtube", remote_id="c-3",
            text="and a reply to the reply", conversation_id=conv_id,
            status="unread", parent_interaction_id=rows["child"].id)
        # the conversation's latest touch follows its newest interaction
        conv.last_interaction_at = rows["grand"].created_at

        opp = CommunityOpportunity(
            workspace_id=ws_id, opportunity_type="content_request",
            title="More shorts please", detail="3 people asked",
            evidence_count=3, confidence="medium", state="open",
        )
        s.add(opp)
        s.flush()
        opp_id = opp.id

        ids = {k: v.id for k, v in rows.items()}
        ids["conversation"] = conv_id
        ids["opportunity"] = opp_id
        ids["account"] = account_id
        return ids


def _base(ws_id):
    return f"/api/v1/workspaces/{ws_id}/inbox"


def _module_present(*candidates) -> bool:
    """True when at least one candidate module the API resolves lazily exists."""
    return any(importlib.util.find_spec(mod) is not None for mod, _ in candidates)


def _patch_social_provider(monkeypatch):
    """Record-only social-provider double (test-only; production keeps the
    real official-API layer). Exercises the whole policy send gate for real
    while the network call itself is recorded."""
    from app.engine.community import policy as policy_mod
    from app.providers.social.base import Receipt

    class _RecordingSocial:
        def __init__(self):
            self.calls: list[dict] = []

        def reply_to_comment(self, account, remote_id, text):
            self.calls.append({"remote_id": remote_id, "text": text})
            return Receipt(remote_reply_id=f"rr-{uuid.uuid4().hex[:8]}",
                           raw={"id": "remote", "text": text}, mock=True)

    provider = _RecordingSocial()
    monkeypatch.setattr(policy_mod, "_get_provider", lambda platform: provider)
    return provider


# ---------------------------------------------------------------------------
# platforms
# ---------------------------------------------------------------------------


def test_platforms_endpoint_registry_or_honest_fallback(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    _seed(ws)

    r = client.get(f"{_base(ws)}/platforms", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body.get("available"), bool)
    assert isinstance(body.get("platforms"), list)
    assert isinstance(body.get("capabilities"), dict)
    if body["available"]:
        # Lane A registry is present → real specs, never a fabricated empty
        assert body["platforms"], "registry reported available with zero specs"
        assert isinstance(body.get("observed"), list)
        assert "youtube" in body["observed"], "seeded platforms must be observed"
    else:
        # honest fallback carries the reason, never silent emptiness
        assert body.get("error")


# ---------------------------------------------------------------------------
# interactions: filters + pagination
# ---------------------------------------------------------------------------


def test_interactions_filters_and_validation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    ids = _seed(ws)
    base = _base(ws)

    def items(params=""):
        r = client.get(f"{base}/interactions{params}", headers=h)
        assert r.status_code == 200, r.text
        return [i["id"] for i in r.json()["items"]]

    # platform
    assert set(items("?platform=reddit")) == {ids["spam"]}
    # status + unread
    assert set(items("?status=unread")) == {ids["question"], ids["grand"]}
    assert set(items("?unread=true")) == set(items("?status=unread"))
    assert ids["positive"] in items("?unread=false")
    assert ids["question"] not in items("?unread=false")
    # search (text, case-insensitive)
    assert items("?search=changed%20my%20life") == [ids["positive"]]
    assert items("?search=MAKE%20MONEY") == [ids["question"]]
    assert items("?search=nothing-matches-this") == []
    # classification (JSON post-filter)
    assert set(items("?classification=QUESTION")) == {ids["question"]}
    assert set(items("?classification=POSITIVE")) == {ids["positive"]}
    assert items("?classification=LEAD") == []
    # priority + conversation scope
    assert ids["question"] in items("?priority=normal")
    assert set(items(f"?conversation_id={ids['conversation']}")) >= {
        ids["question"], ids["positive"],
    }
    # invalid enum values are 422, never a silent ignore
    for bad in ("?status=nope", "?priority=nope", "?classification=NOPE"):
        assert client.get(f"{base}/interactions{bad}", headers=h).status_code == 422, bad
    # limit bounds
    assert client.get(f"{base}/interactions?limit=0", headers=h).status_code == 422
    assert client.get(f"{base}/interactions?limit=101", headers=h).status_code == 422

    # DTO shape the frontend depends on
    r = client.get(f"{base}/interactions?limit=1", headers=h)
    row = r.json()["items"][0]
    for key in ("id", "platform", "status", "unread", "priority", "classifications",
                "text", "created_at", "conversation_id"):
        assert key in row, key
    assert isinstance(row["unread"], bool)


def test_interactions_pagination_cursor(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    _seed(ws)
    base = _base(ws)

    seen: list[str] = []
    cursor = None
    pages = 0
    while pages < 10:
        q = "?limit=2" + (f"&cursor={cursor}" if cursor else "")
        r = client.get(f"{base}/interactions{q}", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        seen.extend(i["id"] for i in body["items"])
        pages += 1
        cursor = body.get("next_cursor")
        if not cursor:
            break

    assert len(seen) == len(set(seen)), "cursor pages overlapped"
    assert len(seen) == 6, f"expected every seeded interaction, got {len(seen)}"

    # keyset order is created_at desc
    r = client.get(f"{base}/interactions?limit=100", headers=h)
    stamps = [i["created_at"] for i in r.json()["items"]]
    assert stamps == sorted(stamps, reverse=True)
    assert [i["id"] for i in r.json()["items"]] == seen[:6]

    # a corrupted cursor is a 422, not a 500
    assert client.get(f"{base}/interactions?cursor=@@@@", headers=h).status_code == 422


# ---------------------------------------------------------------------------
# detail + conversation threads
# ---------------------------------------------------------------------------


def test_interaction_detail_thread_and_actions(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.models import CommunityAction

    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    ids = _seed(ws)
    base = _base(ws)

    with session_scope() as s:
        act = CommunityAction(
            workspace_id=ws, interaction_id=ids["grand"],
            conversation_id=ids["conversation"], platform="youtube",
            action_type="REPLY", state="draft", origin="ai",
            draft_text="Here is how to start.", brand_check_json={"status": "PASS"},
        )
        s.add(act)
        s.flush()
        act_id = act.id

    r = client.get(f"{base}/interactions/{ids['grand']}", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["interaction"]["id"] == ids["grand"]
    # ancestors oldest→newest, oldest first
    assert [t["id"] for t in body["thread"]] == [ids["root"], ids["child"]]
    assert body["thread"][0]["created_at"] <= body["thread"][1]["created_at"]
    assert [a["id"] for a in body["actions"]] == [act_id]
    assert body["actions"][0]["badge"] == "AI DRAFT"
    assert body["actions"][0]["brand_check"] == {"status": "PASS"}
    assert body["linked_publication"] is None

    # missing id → 404 inside this workspace
    assert client.get(f"{base}/interactions/does-not-exist", headers=h).status_code == 404


def test_conversation_list_and_thread_ordering(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    ids = _seed(ws)
    base = _base(ws)

    r = client.get(f"{base}/conversations", headers=h)
    assert r.status_code == 200, r.text
    convs = r.json()["items"]
    assert len(convs) == 1
    conv = convs[0]
    assert conv["id"] == ids["conversation"]
    assert conv["unread_count"] == 2  # question + grandchild
    assert conv["last_interaction"]["id"] == ids["grand"]
    assert conv["platform"] == "youtube"
    assert conv["participant_name"] == ""

    r = client.get(f"{base}/conversations/{ids['conversation']}", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    got = [i["id"] for i in body["interactions"]]
    assert got == [ids["question"], ids["positive"], ids["spam"],
                   ids["root"], ids["child"], ids["grand"]], "thread must be created_at asc"
    assert body["conversation"]["id"] == ids["conversation"]

    # unread filter on the list (this conversation still has unread rows)
    r = client.get(f"{base}/conversations?unread=true", headers=h)
    assert [c["id"] for c in r.json()["items"]] == [ids["conversation"]]
    r = client.get(f"{base}/conversations?platform=reddit", headers=h)
    assert r.json()["items"] == []

    assert client.get(f"{base}/conversations/nope", headers=h).status_code == 404


# ---------------------------------------------------------------------------
# read toggle + bulk triage
# ---------------------------------------------------------------------------


def test_read_toggle_and_spam_never_flips(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    ids = _seed(ws)
    base = _base(ws)

    r = client.post(f"{base}/interactions/{ids['question']}/read", headers=h, json={"read": True})
    assert r.status_code == 200, r.text
    assert r.json()["interaction"]["status"] == "read"
    assert r.json()["interaction"]["unread"] is False

    # idempotent second call keeps the workflow state
    r = client.post(f"{base}/interactions/{ids['question']}/read", headers=h, json={"read": True})
    assert r.status_code == 200
    assert r.json()["interaction"]["status"] == "read"

    # unread again
    r = client.post(f"{base}/interactions/{ids['question']}/read", headers=h, json={"read": False})
    assert r.json()["interaction"]["status"] == "unread"

    # spam is never pulled back into the unread queue
    r = client.post(f"{base}/interactions/{ids['spam']}/read", headers=h, json={"read": False})
    assert r.status_code == 200
    assert r.json()["interaction"]["status"] == "spam"

    assert client.post(f"{base}/interactions/nope/read", headers=h,
                       json={"read": True}).status_code == 404


def test_bulk_action_cap_and_errors(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    ids = _seed(ws)
    base = _base(ws)

    # cap: >100 ids is a 422 before any row is touched
    r = client.post(f"{base}/bulk", headers=h,
                    json={"ids": [f"id-{i}" for i in range(101)], "action": "read"})
    assert r.status_code == 422, r.text

    # empty list / unknown action are 422 as well
    assert client.post(f"{base}/bulk", headers=h,
                       json={"ids": [], "action": "read"}).status_code == 422
    assert client.post(f"{base}/bulk", headers=h,
                       json={"ids": [ids["question"]], "action": "explode"}).status_code == 422

    r = client.post(f"{base}/bulk", headers=h,
                    json={"ids": [ids["question"], ids["grand"]], "action": "read"})
    assert r.status_code == 200, r.text
    assert r.json() == {"action": "read", "requested": 2, "updated": 2}

    # spam marks status + moderation trail
    r = client.post(f"{base}/bulk", headers=h,
                    json={"ids": [ids["positive"]], "action": "spam"})
    assert r.status_code == 200
    assert r.json()["updated"] == 1
    r = client.get(f"{base}/interactions/{ids['positive']}", headers=h)
    assert r.json()["interaction"]["status"] == "spam"
    assert r.json()["interaction"]["moderation_state"] == "review"

    # a foreign id makes the whole batch a 404 (no partial application)
    r = client.post(f"{base}/bulk", headers=h,
                    json={"ids": [ids["question"], "00000000-0000-0000-0000-000000000000"],
                          "action": "read"})
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# reply drafts + AI surface (classify / draft / approve / send)
# ---------------------------------------------------------------------------


def test_reply_draft_creation_and_validation(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.models import Conversation

    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    ids = _seed(ws)
    base = _base(ws)

    r = client.post(f"{base}/conversations/{ids['conversation']}/reply", headers=h,
                    json={"text": "Thanks for watching — more shorts coming!"})
    assert r.status_code == 200, r.text
    body = r.json()
    action = body["action"]
    assert action["state"] == "draft"
    assert action["origin"] == "human_edited"  # operator typed it → provenance
    assert action["badge"] == "HUMAN EDITED"
    assert action["action_type"] == "REPLY"
    assert action["platform"] == "youtube"
    assert not action["final_text"]
    assert body["interaction"]["id"] == ids["grand"]  # anchored to newest

    # explicit anchor on an interaction of ANOTHER conversation → 404
    with session_scope() as s:
        lonely = Conversation(workspace_id=ws, account_id="acc-2",
                              thread_key="thread-2", platform="reddit")
        s.add(lonely)
        s.flush()
        lonely_id = lonely.id
    r = client.post(f"{base}/conversations/{lonely_id}/reply", headers=h,
                    json={"text": "hi", "interaction_id": ids["question"]})
    assert r.status_code == 404, r.text

    # empty text and missing conversation
    r = client.post(f"{base}/conversations/{ids['conversation']}/reply", headers=h,
                    json={"text": "   "})
    assert r.status_code == 422, r.text
    assert client.post(f"{base}/conversations/nope/reply", headers=h,
                       json={"text": "hi"}).status_code == 404

    # the queued draft is visible in the actions list
    r = client.get(f"{base}/actions?state=draft", headers=h)
    assert r.status_code == 200
    assert action["id"] in [a["id"] for a in r.json()["items"]]


def test_classify_endpoint_persists_labels(tmp_path, monkeypatch):
    assert _module_present(*inbox_mod._CLASSIFY_FN), \
        "community classification module missing — cross-lane contract broken"

    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    ids = _seed(ws)
    base = _base(ws)

    r = client.post(f"{base}/interactions/{ids['question']}/classify", headers=h, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["interaction"]["id"] == ids["question"]
    assert isinstance(body["classifications"], list)
    assert body["classifications"], "classifier returned no labels"
    # persisted: a fresh read carries the labels
    r2 = client.get(f"{base}/interactions/{ids['question']}", headers=h)
    assert r2.json()["interaction"]["classifications"]


def test_draft_endpoint_honest_surface(tmp_path, monkeypatch):
    assert _module_present(*inbox_mod._DRAFT_FN), \
        "community drafter missing — cross-lane contract broken"

    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    ids = _seed(ws)
    base = _base(ws)

    r = client.post(f"{base}/interactions/{ids['question']}/draft", headers=h,
                    json={"regenerate": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"]["state"] == "draft"
    assert body["action"]["draft_text"].strip()
    assert body["action"]["origin"] in ("ai", "human_edited")
    assert "brand_check" in body["action"]
    assert body["interaction"]["id"] == ids["question"]


def test_actions_approve_reject_send_surface(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.models import CommunityAction

    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    ids = _seed(ws)
    base = _base(ws)
    # real account row comes from _seed; patch only the network-bound
    # provider resolution so the full policy gate still runs for real
    provider = _patch_social_provider(monkeypatch)

    r = client.post(f"{base}/conversations/{ids['conversation']}/reply", headers=h,
                    json={"text": "Original draft text"})
    assert r.status_code == 200, r.text
    act_id = r.json()["action"]["id"]

    # --- approve: engine-gated. The policy approve_action(db, ws, …) must
    # run for real: a strict 200 advances the state, never a silent engine
    # error swallowed by an (500, 503) escape hatch.
    r = client.post(f"{base}/actions/{act_id}/approve", headers=h, json={})
    assert r.status_code == 200, r.text
    assert r.json()["action"]["state"] == "approved"
    assert r.json()["action"]["approval_user_id"]

    # --- reject: pure state machine, no engine involved
    r = client.post(f"{base}/actions/{act_id}/reject", headers=h, json={})
    assert r.status_code == 200, r.text
    assert r.json()["action"]["state"] == "rejected"
    assert r.json()["action"]["rejected_user_id"]

    # --- terminal states are refused before any engine call
    with session_scope() as s:
        sent = CommunityAction(
            workspace_id=ws, interaction_id=ids["question"],
            conversation_id=ids["conversation"], platform="youtube",
            action_type="REPLY", state="sent", origin="auto",
            draft_text="already gone", final_text="already gone",
            remote_reply_id="rr-1",
        )
        s.add(sent)
        s.flush()
        sent_id = sent.id
    assert client.post(f"{base}/actions/{sent_id}/reject", headers=h,
                       json={}).status_code == 409
    assert client.post(f"{base}/actions/{sent_id}/send", headers=h,
                       json={}).status_code == 409

    # --- send: validation happens before the engine
    with session_scope() as s:
        fresh = CommunityAction(
            workspace_id=ws, interaction_id=ids["positive"],
            conversation_id=ids["conversation"], platform="youtube",
            account_id=ids["account"],
            action_type="REPLY", state="draft", origin="ai",
            draft_text="draft to edit",
        )
        s.add(fresh)
        s.flush()
        fresh_id = fresh.id
    assert client.post(f"{base}/actions/{fresh_id}/send", headers=h,
                       json={"text": " "}).status_code == 422
    assert client.post(f"{base}/actions/nope/send", headers=h, json={}).status_code == 404

    # --- send: a human edit is persisted BEFORE the policy gate runs, so a
    # refusal never loses the typing (origin flips to human_edited). The
    # provider double records the call — a strict 200 proves the whole gate
    # (brand → limits → account → claim → send → verify) ran end-to-end.
    r = client.post(f"{base}/actions/{fresh_id}/send", headers=h,
                    json={"text": "Edited by the operator"})
    assert r.status_code == 200, r.text
    assert r.json()["action"]["state"] == "sent"
    assert r.json()["action"]["final_text"] == "Edited by the operator"
    assert len(provider.calls) == 1, "policy gate never reached the provider"

    r = client.get(f"{base}/actions?interaction_id={ids['positive']}", headers=h)
    stored = next(a for a in r.json()["items"] if a["id"] == fresh_id)
    assert stored["final_text"] == "Edited by the operator"
    assert stored["origin"] == "human_edited"
    assert stored["badge"] == "HUMAN EDITED"

    # --- unknown action ids are 404, in-workspace
    assert client.post(f"{base}/actions/00000000-0000-0000-0000-000000000000/approve",
                       headers=h, json={}).status_code == 404


def test_reply_send_true_runs_the_full_policy_gate(tmp_path, monkeypatch):
    """send=true on the reply route must clear the SAME gate as /actions/send:
    brand → limits → account row → claim → provider → verify (strict 200)."""
    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    ids = _seed(ws)
    base = _base(ws)
    provider = _patch_social_provider(monkeypatch)

    r = client.post(f"{base}/conversations/{ids['conversation']}/reply",
                    headers=h, json={"text": "Instant reply to the thread",
                                     "send": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"]["state"] == "sent"
    assert body["action"]["final_text"] == "Instant reply to the thread"
    assert body["action"]["origin"] == "human_edited"
    assert body["action"]["remote_reply_id"]
    assert len(provider.calls) == 1, "policy gate never reached the provider"


def test_send_claim_blocks_a_parallel_sender_and_expires(tmp_path, monkeypatch):
    """Double-send TOCTOU: a live claim refuses the second sender; a stale
    claim (crashed sender) expires so retries are never locked out forever."""
    from datetime import timedelta

    from app.db import session_scope
    from app.models import CommunityAction
    from app.models.base import utcnow

    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    ids = _seed(ws)
    base = _base(ws)
    _patch_social_provider(monkeypatch)

    with session_scope() as s:
        action = CommunityAction(
            workspace_id=ws, interaction_id=ids["positive"],
            conversation_id=ids["conversation"], platform="youtube",
            account_id=ids["account"],
            action_type="REPLY", state="draft", origin="ai",
            draft_text="claim race draft",
        )
        s.add(action)
        s.flush()
        action_id = action.id

    # a LIVE claim (a parallel sender owns this row right now) → refusal
    with session_scope() as s:
        row = s.get(CommunityAction, action_id)
        row.send_claimed_at = utcnow()
    r = client.post(f"{base}/actions/{action_id}/send", headers=h,
                    json={"text": "claim race draft"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "send_in_progress"

    # a STALE claim (past SEND_CLAIM_TTL) → the retry goes through for real
    with session_scope() as s:
        row = s.get(CommunityAction, action_id)
        row.send_claimed_at = utcnow() - timedelta(seconds=130)
    r = client.post(f"{base}/actions/{action_id}/send", headers=h,
                    json={"text": "claim race draft"})
    assert r.status_code == 200, r.text
    assert r.json()["action"]["state"] == "sent"
    assert r.json()["action"]["final_text"] == "claim race draft"


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


def test_rbac_viewer_member_admin(tmp_path, monkeypatch):
    from app.models import WorkspaceMember

    client = _client(tmp_path, monkeypatch)
    ws, h_owner = _register(client)
    ids = _seed(ws)
    base = _base(ws)

    h_viewer = _make_user(client, ws, WorkspaceMember.ROLE_VIEWER)
    h_member = _make_user(client, ws, WorkspaceMember.ROLE_MEMBER)
    h_admin = _make_user(client, ws, WorkspaceMember.ROLE_ADMIN)

    # viewers read everything
    for path in ("/platforms", "/interactions", "/conversations", "/actions",
                 "/analytics", "/opportunities", "/insights", "/autonomy"):
        r = client.get(f"{base}{path}", headers=h_viewer)
        assert r.status_code == 200, (path, r.text)

    # …but never mutate (POST → 403) — every member-level write, including
    # the AI/action gates (dependency resolves before the handler runs, so
    # dummy ids are fine here)
    calls = (
        ("read", client.post(f"{base}/interactions/{ids['question']}/read",
                             headers=h_viewer, json={"read": True})),
        ("bulk", client.post(f"{base}/bulk", headers=h_viewer,
                             json={"ids": [ids["question"]], "action": "read"})),
        ("reply", client.post(f"{base}/conversations/{ids['conversation']}/reply",
                              headers=h_viewer, json={"text": "nope"})),
        ("sync", client.post(f"{base}/sync", headers=h_viewer, json={})),
        ("classify", client.post(f"{base}/interactions/{ids['question']}/classify",
                                 headers=h_viewer, json={})),
        ("draft", client.post(f"{base}/interactions/{ids['question']}/draft",
                              headers=h_viewer, json={})),
        ("approve", client.post(f"{base}/actions/{ids['question']}/approve",
                                headers=h_viewer, json={})),
        ("reject", client.post(f"{base}/actions/{ids['question']}/reject",
                               headers=h_viewer, json={})),
        ("send", client.post(f"{base}/actions/{ids['question']}/send",
                             headers=h_viewer, json={"text": "nope"})),
        ("convert", client.post(f"{base}/opportunities/{ids['opportunity']}/convert",
                                headers=h_viewer, json={})),
        ("dismiss", client.post(f"{base}/opportunities/{ids['opportunity']}/dismiss",
                                headers=h_viewer, json={})),
    )
    for name, r in calls:
        assert r.status_code == 403, (name, r.status_code, r.text)

    # members mutate triage, but cannot touch autonomy
    r = client.post(f"{base}/interactions/{ids['question']}/read",
                    headers=h_member, json={"read": True})
    assert r.status_code == 200, r.text
    r = client.put(f"{base}/autonomy", headers=h_member, json={"mode": "LOW_RISK_AUTO"})
    assert r.status_code == 403, r.text

    # admins own the autonomy config
    r = client.put(f"{base}/autonomy", headers=h_admin, json={"mode": "LOW_RISK_AUTO"})
    assert r.status_code == 200, r.text
    assert r.json()["autonomy"]["mode"] == "LOW_RISK_AUTO"
    # …and so does the owner
    r = client.put(f"{base}/autonomy", headers=h_owner, json={"mode": "DRAFT_ONLY"})
    assert r.status_code == 200, r.text

    # no token at all → 401
    assert client.get(f"{base}/interactions").status_code == 401


# ---------------------------------------------------------------------------
# workspace isolation
# ---------------------------------------------------------------------------


def test_workspace_isolation_foreign_ids_404(tmp_path, monkeypatch):
    from app.models import WorkspaceMember

    client = _client(tmp_path, monkeypatch)
    ws_a, h_a = _register(client)
    ids = _seed(ws_a)
    base_a = _base(ws_a)

    # a queued action in workspace A
    r = client.post(f"{base_a}/conversations/{ids['conversation']}/reply",
                    headers=h_a, json={"text": "A-side draft"})
    assert r.status_code == 200, r.text
    action_a = r.json()["action"]["id"]

    ws_b, h_b = _register(client)
    base_b = _base(ws_b)

    # workspace B sees none of A's rows — 404, never 403/200 with leakage
    checks = (
        ("interaction", client.get(f"{base_b}/interactions/{ids['question']}", headers=h_b)),
        ("conversation", client.get(f"{base_b}/conversations/{ids['conversation']}", headers=h_b)),
        ("reject", client.post(f"{base_b}/actions/{action_a}/reject", headers=h_b, json={})),
        ("send", client.post(f"{base_b}/actions/{action_a}/send", headers=h_b, json={})),
        ("read", client.post(f"{base_b}/interactions/{ids['question']}/read",
                             headers=h_b, json={"read": True})),
        ("dismiss", client.post(f"{base_b}/opportunities/{ids['opportunity']}/dismiss",
                                headers=h_b, json={})),
    )
    for name, r in checks:
        assert r.status_code == 404, (name, r.status_code, r.text)

    # lists are scoped: B's inbox is empty, A still has its rows
    r = client.get(f"{base_b}/interactions?limit=100", headers=h_b)
    assert r.status_code == 200 and r.json()["items"] == []
    r = client.get(f"{base_a}/interactions?limit=100", headers=h_a)
    assert ids["question"] in [i["id"] for i in r.json()["items"]]

    # a B user cannot even enter A's workspace mount (403, no existence hint)
    r = client.get(f"{base_a}/interactions", headers=h_b)
    assert r.status_code == 403, r.text

    # a member of A only is 403 inside B's mount too
    h_stranger = _make_user(client, ws_a, WorkspaceMember.ROLE_MEMBER)
    r = client.get(f"{_base(ws_b)}/interactions", headers=h_stranger)
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# autonomy config
# ---------------------------------------------------------------------------


def test_autonomy_validation_and_roundtrip(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.models import Workspace

    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    base = _base(ws)

    r = client.get(f"{base}/autonomy", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["autonomy"]["mode"] == "DRAFT_ONLY"  # conservative default
    assert "DRAFT_ONLY" in body["modes"] and "LOW_RISK_AUTO" in body["modes"]
    assert "QUESTION" in body["classes"]
    assert body["defaults"]["caps"]["daily_replies"] == 20

    # invalid mode / unknown class / duplicate class / bad caps → 422
    assert client.put(f"{base}/autonomy", headers=h,
                      json={"mode": "SKYROCKET"}).status_code == 422
    assert client.put(f"{base}/autonomy", headers=h,
                      json={"classes": ["NOT_A_LABEL"]}).status_code == 422
    assert client.put(f"{base}/autonomy", headers=h,
                      json={"classes": ["QUESTION", "QUESTION"]}).status_code == 422
    # (True is coerced to 1 by pydantic before the endpoint, so it is a valid
    # positive cap; zero/negative/blank names are the API's own 422 surface.)
    for caps in ({"daily_replies": 0}, {"daily_replies": -1}, {"": 5}):
        assert client.put(f"{base}/autonomy", headers=h,
                          json={"caps": caps}).status_code == 422, caps

    # valid roundtrip persists into settings_json["community"]
    r = client.put(f"{base}/autonomy", headers=h, json={
        "mode": "APPROVAL_REQUIRED",
        "classes": ["QUESTION", "LEAD"],
        "caps": {"daily_replies": 7, "hourly_replies": 3},
    })
    assert r.status_code == 200, r.text
    assert r.json()["autonomy"]["mode"] == "APPROVAL_REQUIRED"
    assert r.json()["autonomy"]["classes"] == ["QUESTION", "LEAD"]
    assert r.json()["autonomy"]["caps"]["daily_replies"] == 7

    with session_scope() as s:
        stored = s.get(Workspace, ws).settings_json["community"]
    assert stored["mode"] == "APPROVAL_REQUIRED"
    assert stored["classes"] == ["QUESTION", "LEAD"]
    assert stored["caps"]["daily_replies"] == 7
    assert stored["caps"]["hourly_replies"] == 3

    # a partial update merges, it does not wipe the stored config
    r = client.put(f"{base}/autonomy", headers=h, json={"mode": "DRAFT_ONLY"})
    assert r.status_code == 200
    with session_scope() as s:
        stored = s.get(Workspace, ws).settings_json["community"]
    assert stored["mode"] == "DRAFT_ONLY"
    assert stored["classes"] == ["QUESTION", "LEAD"]

    # GET reflects it
    r = client.get(f"{base}/autonomy", headers=h)
    assert r.json()["autonomy"]["mode"] == "DRAFT_ONLY"

    # the API's stored alias shape must REACH THE GATE: load_autonomy reads
    # caps.daily_replies / hourly_replies natively (parser alias bridge), so
    # the config the gate enforces is the config the operator set.
    from app.engine.community.autonomy import load_autonomy

    with session_scope() as s:
        cfg = load_autonomy(s.get(Workspace, ws))
    assert cfg.daily_cap == 7
    assert cfg.rate_per_10min == 3
    assert sorted(cfg.auto_reply_classes) == ["LEAD", "QUESTION"]


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def test_sync_enqueues_job_row(tmp_path, monkeypatch):
    from app.db import session_scope
    from app.models import Job, WorkspaceMember

    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    base = _base(ws)

    r = client.post(f"{base}/sync", headers=h, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "COMMUNITY_SYNC"
    assert body["payload"] == {}
    job_id = body["job_id"]
    assert job_id

    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job is not None, "enqueue did not persist a job row"
        assert job.type == "COMMUNITY_SYNC"
        assert job.workspace_id == ws
        assert job.status == "QUEUED"

    # scoped payload
    r = client.post(f"{base}/sync", headers=h, json={"platform": "youtube"})
    assert r.status_code == 200
    assert r.json()["payload"] == {"platform": "youtube"}
    with session_scope() as s:
        job = s.get(Job, r.json()["job_id"])
        assert job.payload == {"platform": "youtube"}

    # viewer cannot enqueue (POST → 403)
    h_viewer = _make_user(client, ws, WorkspaceMember.ROLE_VIEWER)
    assert client.post(f"{base}/sync", headers=h_viewer, json={}).status_code == 403


# ---------------------------------------------------------------------------
# analytics / opportunities / insights
# ---------------------------------------------------------------------------


def test_analytics_opportunities_insights_shapes(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    ids = _seed(ws)
    base = _base(ws)

    # analytics: consistent envelope whether or not the metrics engine resolves
    r = client.get(f"{base}/analytics", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body.get("available"), bool)
    assert isinstance(body.get("kpis"), list)
    if body["available"]:
        assert isinstance(body.get("metrics"), dict)
        assert isinstance(body.get("totals"), dict)
        assert body["metrics"].get("interactions", 0) >= 6
    else:
        assert body.get("error")

    # opportunities: list + state filter + DTO shape
    r = client.get(f"{base}/opportunities", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["id"] for i in items] == [ids["opportunity"]]
    opp = items[0]
    for key in ("opportunity_type", "title", "evidence_count", "confidence",
                "state", "source_interaction_ids", "created_at"):
        assert key in opp, key
    assert opp["state"] == "open"
    assert client.get(f"{base}/opportunities?state=nope", headers=h).status_code == 422
    assert client.get(f"{base}/opportunities?state=dismissed",
                      headers=h).json()["items"] == []

    r = client.post(f"{base}/opportunities/{ids['opportunity']}/dismiss", headers=h, json={})
    assert r.status_code == 200, r.text
    assert r.json()["opportunity"]["state"] == "dismissed"

    # convert: engine may or may not be resolvable → 200 or honest 503,
    # never a crash page
    from app.db import session_scope
    from app.models import CommunityOpportunity

    with session_scope() as s:
        row = CommunityOpportunity(
            workspace_id=ws, opportunity_type="partnership",
            title="Sponsor inbound", detail="brand wants a deal",
            evidence_count=1, confidence="low", state="open",
        )
        s.add(row)
        s.flush()
        conv_opp = row.id
    r = client.post(f"{base}/opportunities/{conv_opp}/convert", headers=h, json={})
    assert r.status_code == 200, r.text
    assert r.json()["opportunity"]["state"] == "converted"
    assert isinstance(r.json()["result"], dict)
    assert client.post(f"{base}/opportunities/nope/convert", headers=h,
                       json={}).status_code == 404

    # insights: items + suggestions envelope, engine genuinely resolved
    r = client.get(f"{base}/insights", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body.get("items"), list)
    assert isinstance(body.get("suggestions"), list)
    assert body["suggestions_available"] is True, body.get("error")
    for insight in body["items"]:
        for key in ("topic", "evidence_count", "confidence", "state"):
            assert key in insight, key
    assert client.get(f"{base}/insights?limit=0", headers=h).status_code == 422


# ---------------------------------------------------------------------------
# dual mount
# ---------------------------------------------------------------------------


def test_dual_mount_query_param_parity(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    ids = _seed(ws)

    # canonical ws-scoped mount
    path_r = client.get(f"/api/v1/workspaces/{ws}/inbox/interactions", headers=h)
    assert path_r.status_code == 200, path_r.text
    path_ids = [i["id"] for i in path_r.json()["items"]]

    # literal contract mount (?workspace_id=)
    q_r = client.get(f"/api/v1/inbox/interactions?workspace_id={ws}", headers=h)
    assert q_r.status_code == 200, q_r.text
    assert [i["id"] for i in q_r.json()["items"]] == path_ids
    assert ids["question"] in path_ids

    r = client.get(f"/api/v1/inbox/platforms?workspace_id={ws}", headers=h)
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["available"], bool)

    r = client.get(f"/api/v1/inbox/autonomy?workspace_id={ws}", headers=h)
    assert r.status_code == 200
    assert r.json()["autonomy"]["mode"] == "DRAFT_ONLY"

    # without workspace_id the query mount cannot resolve a workspace → 422
    assert client.get("/api/v1/inbox/interactions", headers=h).status_code == 422


def test_classify_write_time_label_validation(tmp_path, monkeypatch):
    """Work 10 Section 0: unknown labels are dropped at WRITE time.

    The read-time ``labels_of`` filter stays authoritative for display; this
    test proves a misbehaving engine can never persist an out-of-vocabulary
    label to ``classifications_json``.
    """
    from app.models.community import CLASSIFICATION_LABELS

    client = _client(tmp_path, monkeypatch)
    ws, h = _register(client)
    ids = _seed(ws)
    base = _base(ws)

    # a misbehaving engine: valid label + junk string + junk dict + valid dict
    def _fake_fn(*candidates):
        return lambda **kwargs: [
            "POSITIVE",
            "NOT_A_LABEL",
            {"label": "FAKE_LABEL", "confidence": 0.99},
            {"label": "SUPPORT", "confidence": 0.8},
        ]

    monkeypatch.setattr(inbox_mod, "_engine_fn", _fake_fn)
    r = client.post(f"{base}/interactions/{ids['question']}/classify",
                    headers=h, json={})
    assert r.status_code == 200, r.text

    def _labels(entries):
        return [e["label"] if isinstance(e, dict) else e for e in entries]

    assert _labels(r.json()["classifications"]) == ["POSITIVE", "SUPPORT"]

    # a fresh read confirms only vocabulary labels were persisted
    r2 = client.get(f"{base}/interactions/{ids['question']}", headers=h)
    stored = _labels(r2.json()["interaction"]["classifications"])
    assert "NOT_A_LABEL" not in stored and "FAKE_LABEL" not in stored
    assert all(str(label).upper() in set(CLASSIFICATION_LABELS)
               for label in stored)
