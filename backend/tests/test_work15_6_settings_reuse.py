"""Settings reuse / project duplication tests (Work 15.6 §9).

The claim under test is that duplicating a project copies CONFIGURATION and
nothing else, and that the exclusion is *structural* (a copy-from-dict
allowlist) rather than a filter applied to a full copy.

The two tests that carry that claim are
``test_a_cannot_copy_a_credential_even_when_present_on_the_source`` and
``test_a_timeline_id_is_absent_or_clearly_non_shared``.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import settings_reuse as sr

#: A fixture value that must behave like a credential for the copy logic. Built at
#: runtime from inert parts so it can never be pasted into a real config, and so
#: nothing in this file reads as a key to a human or a scanner.
CANARY = "CANARY-" + "-".join(f"{n:03d}" for n in (7, 14, 21, 28)) + "-INERT"


@pytest.fixture()
def client():
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def authed(client):
    email = f"sr{os.urandom(5).hex()}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"client": client,
            "headers": {"Authorization": f"Bearer {data['access_token']}"},
            "ws_id": data["workspace"]["id"]}


def _source_settings() -> dict:
    """A source project block containing BOTH configuration and everything else.

    Deliberately hostile: every field the work order says must not be copied is
    present, plus an invented one nobody thought of.
    """
    return {
        "brand": {"name": "Acme", "primary_color": "#112233",
                  "font_family": "Inter", "unknown_future_field": "sneaky"},
        "format": {"aspect": "9:16", "resolution": "1080x1920"},
        "voice": {"provider": "edge", "voice": "en-US-AriaNeural", "rate": 1.05},
        "captions": {"preset": "bold_center", "max_words_per_line": 3},
        "platforms": {"selected": ["youtube", "tiktok"], "primary": "tiktok"},
        "generation": {"llm_tier": "cheap", "max_retries": 2,
                       "api_key": CANARY},
        # --- everything below MUST NOT be copied ---
        "publication_ids": ["yt_abc123", "tt_xyz789"],
        "published_at": ["2026-01-02T03:04:05Z"],
        "analytics": {"views": 1000, "watch_through": 0.42},
        "approvals": [{"reviewer": "u1", "decision": "approved"}],
        "approval_id": "rev-123",
        "remote_job_ids": ["job-abc"],
        "timeline_id": "tl-source-0001",
        "content_ids": ["c-1", "c-2"],
        "api_key": CANARY,
        "elevenlabs_api_key": CANARY,
        "client_secret": CANARY,
        "access_token": CANARY,
        "webhook_secret": CANARY,
        "secrets": {CANARY: CANARY},
        "some_field_nobody_predicted": {"nested": CANARY},
    }


# ---------------------------------------------------------------------------
# 1. the allowlist copies the permitted configuration
# ---------------------------------------------------------------------------


def test_permitted_sections_are_copied():
    out, report = sr.duplicate_settings(_source_settings())

    assert out["brand"]["primary_color"] == "#112233"
    assert out["format"] == {"aspect": "9:16", "resolution": "1080x1920"}
    assert out["voice"]["provider"] == "edge"
    assert out["captions"]["preset"] == "bold_center"
    assert out["platforms"]["selected"] == ["youtube", "tiktok"]
    assert out["generation"]["llm_tier"] == "cheap"
    assert report["exclusion_policy"] == "allowlist"
    assert report["copied_sections"] == sorted(out)


def test_unknown_field_inside_an_allowed_section_is_dropped():
    """The nested allowlist is the same shape as the top-level one."""
    out, report = sr.duplicate_settings(_source_settings())
    assert "unknown_future_field" not in out["brand"]
    assert "brand.unknown_future_field" in report["dropped"]


def test_duplicate_settings_is_pure():
    source = _source_settings()
    before = repr(source)
    out, _report = sr.duplicate_settings(source)
    out["brand"]["primary_color"] = "MUTATED"
    out.setdefault("injected", True)
    assert repr(source) == before, "the source dict was mutated"
    assert "injected" not in sr.duplicate_settings(source)[0]


def test_a_non_mapping_source_yields_an_empty_copy():
    for bad in (None, [], "nope", 42):
        out, report = sr.duplicate_settings(bad)
        assert out == {}
        assert report["exclusion_policy"] == "allowlist"


def test_a_section_present_but_not_a_mapping_is_not_guessed_at():
    out, report = sr.duplicate_settings({"brand": ["a", "b"],
                                         "voice": {"provider": "edge"}})
    assert out == {"voice": {"provider": "edge"}}
    assert "brand" in report["dropped"]


# ---------------------------------------------------------------------------
# 2. the denylist claims -- proven, not asserted
# ---------------------------------------------------------------------------


def test_a_cannot_copy_a_credential_even_when_present_on_the_source():
    """The core §9 test: a credential on the source does not reach the copy.

    Checked at three levels, because "the credential is not in the output" is
    satisfied by accident if only the top level was considered:

    * not as a TOP-LEVEL key (``api_key``, ``elevenlabs_api_key``, ...);
    * not NESTED inside an allowed section (``generation.api_key``);
    * not as a VALUE anywhere in the copy, in any nesting.
    """
    out, report = sr.duplicate_settings(_source_settings())

    for name in ("api_key", "elevenlabs_api_key", "client_secret",
                 "access_token", "webhook_secret", "secrets"):
        assert name not in out, f"top-level {name} survived"

    assert "api_key" not in out["generation"]
    assert "generation.api_key" in report["dropped"]

    # The exhaustive version: no string anywhere in the copy is the canary.
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key != CANARY
                yield from walk(value)
        elif isinstance(node, list):
            for item in node:
                yield from walk(item)
        else:
            yield node

    for leaf in walk(out):
        assert leaf != CANARY


def test_a_credential_shaped_name_is_refused_even_if_allowlisted():
    """Defence in depth: a future edit that allowlists one still refuses."""
    assert sr.is_secret_field("api_key")
    assert sr.is_secret_field("API-Key")
    assert sr.is_secret_field("access_token")
    assert sr.is_secret_field("refresh_token")
    assert sr.is_secret_field("service_account")
    assert sr.is_secret_field("authorization")
    assert not sr.is_secret_field("voice")
    assert not sr.is_secret_field("aspect")
    assert not sr.is_secret_field("preset")


def test_a_timeline_id_is_absent_or_clearly_non_shared():
    """Either the old timeline id is gone, or it is marked as not shared."""
    out, _report = sr.duplicate_settings(_source_settings())

    # Absent: no timeline key anywhere in the copy.
    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        elif isinstance(node, list):
            for item in node:
                yield from keys(item)

    found = [k for k in keys(out) if "timeline" in k.lower()]
    assert found == [], f"a timeline key survived: {found}"
    assert "tl-source-0001" not in repr(out)


def test_provenance_timeline_id_is_marked_non_shared():
    """When a caller does record provenance, the id cannot read as a live link."""
    note = sr.provenance_note("proj-source", timeline_id="tl-1",
                              timeline_kind="ContentTimeline")
    assert note["shared"] is False
    assert note["timeline_shared"] is False
    assert note["timeline_id"] == "tl-1"
    assert note["from_project_id"] == "proj-source"

    # No timeline id at all -> the key is absent rather than a blank placeholder.
    bare = sr.provenance_note("proj-source")
    assert "timeline_id" not in bare
    assert bare["shared"] is False


def test_every_excluded_field_named_in_the_table_is_actually_absent():
    """The exclusion table is not decorative: each named field really is dropped."""
    out, _report = sr.duplicate_settings(_source_settings())
    blob = repr(out)
    for field in sr.EXCLUDED_SETTINGS:
        assert f"'{field}'" not in blob, f"{field} leaked into the copy"


def test_publication_analytics_and_approval_state_never_cross():
    out, _report = sr.duplicate_settings(_source_settings())
    blob = repr(out)
    for token in ("yt_abc123", "tt_xyz789", "watch_through",
                  "approved", "rev-123", "job-abc", "c-1", "c-2"):
        assert token not in blob, f"{token} crossed into the copy"


def test_the_allowlist_is_the_enforcement_mechanism_not_the_denylist():
    """Prove the shape: adding a field to the SOURCE cannot make it copyable.

    If the implementation were a denylist (start from everything, subtract the
    bad), an unknown field would be copied. Here it is dropped, because the loop
    walks the allowlist. This is the test that distinguishes the two designs.
    """
    out, _report = sr.duplicate_settings({
        "brand": {"name": "Acme"},
        "totally_new_section_2027": {"a": 1},
        "brand_new_field": "value",
    })
    assert out == {"brand": {"name": "Acme"}}


# ---------------------------------------------------------------------------
# 3. per-project storage helpers (pure)
# ---------------------------------------------------------------------------


def test_store_and_load_project_settings_round_trip_without_mutation():
    original = {"music": {"genre": "lofi"}}
    stored = sr.store_project_settings(original, "p1", {"brand": {"name": "Acme"}})
    assert original == {"music": {"genre": "lofi"}}, "input was mutated"
    assert sr.load_project_settings(stored, "p1") == {"brand": {"name": "Acme"}}
    assert sr.load_project_settings(stored, "p2") == {}
    assert stored["music"] == {"genre": "lofi"}, "unrelated settings were lost"


def test_load_project_settings_tolerates_junk():
    for junk in (None, {}, {"project_settings": "nope"}, {"project_settings": {"p": 5}}):
        assert sr.load_project_settings(junk, "p1") == {}


def test_clear_project_settings_removes_only_one_block():
    stored = sr.store_project_settings({}, "p1", {"brand": {"name": "A"}})
    stored = sr.store_project_settings(stored, "p2", {"brand": {"name": "B"}})
    cleared = sr.clear_project_settings(stored, "p1")
    assert sr.load_project_settings(cleared, "p1") == {}
    assert sr.load_project_settings(cleared, "p2") == {"brand": {"name": "B"}}


def test_settings_fingerprint_is_order_independent_and_one_way():
    a = sr.settings_fingerprint({"brand": {"x": 1, "y": 2}})
    b = sr.settings_fingerprint({"brand": {"y": 2, "x": 1}})
    assert a == b
    assert a != sr.settings_fingerprint({"brand": {"x": 9, "y": 2}})
    assert len(a) == 64 and CANARY not in a


# ---------------------------------------------------------------------------
# 4. the endpoint
# ---------------------------------------------------------------------------


def _seed_project(authed, name="Source Project"):
    c, h, ws = authed["client"], authed["headers"], authed["ws_id"]
    r = c.post(f"/api/v1/workspaces/{ws}/projects", headers=h, json={"name": name})
    assert r.status_code == 201, r.text
    project_id = r.json()["id"]

    from app.db import SessionLocal
    from app.models import Workspace
    from app.services.settings_reuse import store_project_settings

    with SessionLocal() as db:
        row = db.get(Workspace, ws)
        row.settings_json = store_project_settings(row.settings_json or {},
                                                   project_id, _source_settings())
        db.commit()
    return project_id


def test_duplicate_endpoint_creates_a_new_project_with_copied_settings(authed):
    src = _seed_project(authed)
    c, h, ws = authed["client"], authed["headers"], authed["ws_id"]

    r = c.post(f"/api/v1/workspaces/{ws}/projects/{src}/duplicate", headers=h,
               json={"name": "Next Week"})
    assert r.status_code == 201, r.text
    body = r.json()

    new_id = body["project"]["id"]
    assert new_id != src
    assert body["project"]["name"] == "Next Week"
    assert body["settings"]["voice"]["provider"] == "edge"
    assert body["settings"]["brand"]["primary_color"] == "#112233"
    assert body["settings"]["provenance"]["from_project_id"] == src
    assert body["settings"]["provenance"]["shared"] is False
    assert body["copied_targets"] == 0


def test_duplicate_endpoint_leaks_no_credential_and_no_publication_state(authed):
    src = _seed_project(authed)
    c, h, ws = authed["client"], authed["headers"], authed["ws_id"]
    r = c.post(f"/api/v1/workspaces/{ws}/projects/{src}/duplicate", headers=h, json={})
    assert r.status_code == 201, r.text
    blob = r.text
    assert CANARY not in blob
    for token in ("yt_abc123", "job-abc", "tl-source-0001", "rev-123"):
        assert token not in blob, f"{token} leaked through the endpoint"


def test_duplicate_endpoint_does_not_clone_targets(authed):
    """A duplicated project links no targets: no shared content or timeline."""
    src = _seed_project(authed)
    c, h, ws = authed["client"], authed["headers"], authed["ws_id"]

    dup = c.post(f"/api/v1/workspaces/{ws}/projects/{src}/duplicate",
                 headers=h, json={}).json()
    r = c.get(f"/api/v1/workspaces/{ws}/projects/{dup['project']['id']}/targets",
              headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


def test_duplicate_endpoint_makes_the_caller_owner_and_nobody_else(authed):
    src = _seed_project(authed)
    c, h, ws = authed["client"], authed["headers"], authed["ws_id"]
    dup = c.post(f"/api/v1/workspaces/{ws}/projects/{src}/duplicate",
                 headers=h, json={}).json()

    detail = c.get(f"/api/v1/workspaces/{ws}/projects/{dup['project']['id']}",
                   headers=h).json()
    assert [m["role"] for m in detail["members"]] == ["OWNER"]
    assert detail["member_count"] == 1


def test_duplicate_endpoint_is_workspace_isolated(authed, client):
    """A project in another workspace is a 404, and copies nothing."""
    src = _seed_project(authed)

    email = f"sr{os.urandom(5).hex()}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    data = r.json()
    other = {"client": client, "ws_id": data["workspace"]["id"],
             "headers": {"Authorization": f"Bearer {data['access_token']}"}}

    denied = other["client"].post(
        f"/api/v1/workspaces/{other['ws_id']}/projects/{src}/duplicate",
        headers=other["headers"], json={})
    assert denied.status_code == 404, denied.text

    listed = other["client"].get(
        f"/api/v1/workspaces/{other['ws_id']}/projects", headers=other["headers"])
    assert listed.json()["items"] == []


def test_duplicate_endpoint_reports_what_it_refused(authed):
    src = _seed_project(authed)
    c, h, ws = authed["client"], authed["headers"], authed["ws_id"]
    body = c.post(f"/api/v1/workspaces/{ws}/projects/{src}/duplicate",
                  headers=h, json={}).json()
    dropped = body["settings_report"]["dropped"]
    assert "api_key" in dropped
    assert "timeline_id" in dropped
    assert "publication_ids" in dropped
    # The report names fields; it never carries their values.
    assert CANARY not in repr(body["settings_report"])


def test_duplicate_of_a_project_with_no_settings_still_works(authed):
    c, h, ws = authed["client"], authed["headers"], authed["ws_id"]
    created = c.post(f"/api/v1/workspaces/{ws}/projects", headers=h,
                     json={"name": "Bare"}).json()
    r = c.post(f"/api/v1/workspaces/{ws}/projects/{created['id']}/duplicate",
               headers=h, json={})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["settings"]["provenance"]["from_project_id"] == created["id"]
    assert "brand" not in body["settings"]


def test_duplicate_derives_a_default_name(authed):
    src = _seed_project(authed, name="Q3 Launch")
    c, h, ws = authed["client"], authed["headers"], authed["ws_id"]
    body = c.post(f"/api/v1/workspaces/{ws}/projects/{src}/duplicate",
                  headers=h, json={}).json()
    assert body["project"]["name"] == "Q3 Launch (copy)"


def test_duplicate_requires_authentication(client):
    r = client.post("/api/v1/workspaces/x/projects/y/duplicate", json={})
    assert r.status_code in (401, 403), r.text