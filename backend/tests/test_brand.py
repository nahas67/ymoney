"""White-label brand kit tests (Work 01) + BrandDNA / inheritance / policy /
consistency-verifier coverage (Work 08 Lane A)."""
from __future__ import annotations

import io
import os


def _register(client):
    import os

    email = f"brand{os.urandom(4).hex()}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["workspace"]["id"]


def test_brand_defaults_and_invalid_accent_fallback():
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    headers, ws_id = _register(client)

    r = client.get(f"/api/v1/workspaces/{ws_id}/brand", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["brand"] == {"app_name": "", "accent": "#22c55e", "logo_path": ""}

    r = client.put(f"/api/v1/workspaces/{ws_id}/settings", headers=headers,
                   json={"settings": {"brand": {"app_name": "Acme Shorts", "accent": "not-a-color"}}})
    assert r.status_code == 200, r.text
    r = client.get(f"/api/v1/workspaces/{ws_id}/brand", headers=headers)
    brand = r.json()["brand"]
    assert brand["app_name"] == "Acme Shorts"
    assert brand["accent"] == "#22c55e"  # invalid falls back, never breaks chrome

    r = client.put(f"/api/v1/workspaces/{ws_id}/settings", headers=headers,
                   json={"settings": {"brand": {"accent": "#f59e0b"}}})
    assert r.status_code == 200
    assert client.get(f"/api/v1/workspaces/{ws_id}/brand", headers=headers).json()["brand"]["accent"] == "#f59e0b"


def test_logo_upload_serve_and_validation():
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    headers, ws_id = _register(client)

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 200
    r = client.post(f"/api/v1/workspaces/{ws_id}/brand/logo", headers=headers,
                    files={"file": ("logo.png", io.BytesIO(png), "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["logo_path"].endswith("logo.png")

    brand = client.get(f"/api/v1/workspaces/{ws_id}/brand", headers=headers).json()["brand"]
    assert brand["logo_path"].endswith("logo.png")

    token = headers["Authorization"].split(" ", 1)[1]
    r = client.get(f"/api/v1/workspaces/{ws_id}/brand/logo/file?token={token}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"

    r = client.get(f"/api/v1/workspaces/{ws_id}/brand/logo/file?token=bad")
    assert r.status_code == 401

    r = client.post(f"/api/v1/workspaces/{ws_id}/brand/logo", headers=headers,
                    files={"file": ("evil.exe", io.BytesIO(b"x"), "application/octet-stream")})
    assert r.status_code == 400

    big = b"0" * (2 * 1024 * 1024 + 1)
    r = client.post(f"/api/v1/workspaces/{ws_id}/brand/logo", headers=headers,
                    files={"file": ("big.png", io.BytesIO(big), "image/png")})
    assert r.status_code == 413


def test_settings_rejects_raw_safety_bypass():
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    headers, ws_id = _register(client)

    # safety subtree must go through the validated endpoint, not the merge
    r = client.put(f"/api/v1/workspaces/{ws_id}/settings", headers=headers,
                   json={"settings": {"safety": {"daily_budget_usd": -5}}})
    assert r.status_code == 422, r.text

    # legit keys still merge fine
    r = client.put(f"/api/v1/workspaces/{ws_id}/settings", headers=headers,
                   json={"settings": {"brand": {"app_name": "Ok"}}})
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["brand"]["app_name"] == "Ok"


# ===========================================================================
# Work 08 Lane A: BrandDNA -> inheritance -> effective policy -> verifier
# ===========================================================================


def _mk_workspace(db_session, **settings):
    from app.models import Workspace

    ws = Workspace(
        name="Brand WS",
        slug=f"bws-{os.urandom(4).hex()}",
        niche="money",
        settings_json=dict(settings),
    )
    db_session.add(ws)
    db_session.flush()
    return ws.id


def _mk_brand(db_session, ws_id, dna=None, *, name="Acme", is_default=False):
    from app.models import Brand, BrandDNARow

    brand = Brand(workspace_id=ws_id, name=name, is_default=is_default)
    db_session.add(brand)
    db_session.flush()
    if dna is not None:
        db_session.add(
            BrandDNARow(workspace_id=ws_id, brand_id=brand.id, dna_json=dict(dna))
        )
        db_session.flush()
    return brand.id


def _mk_campaign(db_session, ws_id):
    from app.models import Campaign

    row = Campaign(workspace_id=ws_id, name=f"camp-{os.urandom(3).hex()}")
    db_session.add(row)
    db_session.flush()
    return row.id


def _mk_content(db_session, ws_id, campaign_id=None):
    from app.models import ContentItem

    row = ContentItem(workspace_id=ws_id, topic="money tips", campaign_id=campaign_id)
    db_session.add(row)
    db_session.flush()
    return row.id


def test_inheritance_precedence(db_session):
    """workspace < brand < campaign < content < platform; last wins; provenance."""
    from app.engine.brand import resolve_effective_policy, set_override

    ws1 = _mk_workspace(db_session, brand_defaults={
        "tone": "ws-tone",
        "brand_colors": {"primary": "#111111"},
        "caption_style": {"size": 20},
    })
    _mk_brand(db_session, ws1, {
        "tone": "brand-tone",
        "colors": {"primary": "#222222"},
        "caption_style": {"size": 40},
        "platform_overrides": {"tiktok": {"tone": "platform-tone"}},
    })
    cid = _mk_campaign(db_session, ws1)
    content_id = _mk_content(db_session, ws1, cid)
    set_override(db_session, ws1, "campaign", cid, {"tone": "campaign-tone"})
    set_override(db_session, ws1, "content", content_id, {"tone": "content-tone"})

    # level 1: workspace defaults (workspace without any brand row)
    ws2 = _mk_workspace(db_session, brand_defaults={"tone": "ws2-tone"})
    only_ws = resolve_effective_policy(db_session, ws2)
    assert only_ws.tone == "ws2-tone"
    assert only_ws.provenance["tone"] == "workspace"

    # level 2: BrandDNA
    brand_only = resolve_effective_policy(db_session, ws1)
    assert brand_only.tone == "brand-tone"
    assert brand_only.provenance["tone"] == "brand"
    # provenance-aware pick: brand `colors` beats workspace `brand_colors`
    assert brand_only.brand_colors == ["#222222"]
    assert brand_only.provenance["brand_colors"] == "brand"
    assert brand_only.caption_style["size"] == 40

    # level 3: campaign override
    with_campaign = resolve_effective_policy(db_session, ws1, campaign_id=cid)
    assert with_campaign.tone == "campaign-tone"
    assert with_campaign.provenance["tone"] == "campaign"
    assert with_campaign.caption_style["size"] == 40  # campaign set only `tone`

    # level 4: content override
    with_content = resolve_effective_policy(
        db_session, ws1, campaign_id=cid, content_id=content_id
    )
    assert with_content.tone == "content-tone"
    assert with_content.provenance["tone"] == "content"

    # level 5: platform override (applied last, wins over everything)
    full = resolve_effective_policy(
        db_session, ws1, campaign_id=cid, content_id=content_id, platform="tiktok"
    )
    assert full.tone == "platform-tone"
    assert full.provenance["tone"] == "platform"
    assert full.caption_style["size"] == 40
    assert full.provenance["caption_style"] == "brand"

    # a caller-supplied (artifact-level) patch lands at the content level
    flat = resolve_effective_policy(
        db_session, ws1, campaign_id=cid, overrides={"tone": "flat-tone"}
    )
    assert flat.tone == "flat-tone"
    assert flat.provenance["tone"] == "content"


def test_campaign_override_isolation(db_session):
    """A campaign override NEVER mutates the stored BrandDNA document."""
    from sqlalchemy import select

    from app.engine.brand import resolve_effective_policy, set_override
    from app.models import BrandDNARow

    ws = _mk_workspace(db_session, brand_defaults={"tone": "ws-tone"})
    _mk_brand(db_session, ws, {"tone": "brand-tone", "forbidden_phrases": ["brand-secret"]})
    cid = _mk_campaign(db_session, ws)
    set_override(db_session, ws, "campaign", cid,
                 {"tone": "campaign-tone", "forbidden_phrases": ["campaign-claim"]})

    policy = resolve_effective_policy(db_session, ws, campaign_id=cid)
    assert policy.tone == "campaign-tone"
    assert policy.forbidden_phrases == ["campaign-claim"]

    stored = db_session.scalar(
        select(BrandDNARow).where(BrandDNARow.workspace_id == ws)
    )
    assert stored is not None
    assert dict(stored.dna_json)["tone"] == "brand-tone"
    assert dict(stored.dna_json)["forbidden_phrases"] == ["brand-secret"]

    # ad-hoc `overrides=` path leaves the stored DNA untouched too
    adhoc = resolve_effective_policy(
        db_session, ws, overrides={"tone": "adhoc", "forbidden_phrases": ["adhoc-claim"]}
    )
    assert adhoc.tone == "adhoc"
    assert adhoc.forbidden_phrases == ["adhoc-claim"]
    db_session.refresh(stored)
    assert dict(stored.dna_json)["tone"] == "brand-tone"
    assert dict(stored.dna_json)["forbidden_phrases"] == ["brand-secret"]


def test_platform_override(db_session):
    from app.engine.brand import resolve_effective_policy

    ws = _mk_workspace(db_session, brand_defaults={"tone": "ws-tone"})
    _mk_brand(db_session, ws, {
        "tone": "brand-tone",
        "caption_style": {"size": 40},
        "forbidden_phrases": ["brand-bad"],
        "platform_overrides": {
            "tiktok": {
                "tone": "tt-tone",
                "caption_style": {"size": 75},
                "forbidden_phrases": ["tiktok-bad"],
            }
        },
    })

    tt = resolve_effective_policy(db_session, ws, platform="tiktok")
    assert tt.tone == "tt-tone"
    assert tt.caption_style["size"] == 75
    assert tt.forbidden_phrases == ["tiktok-bad"]
    assert tt.provenance["tone"] == "platform"
    assert tt.provenance["forbidden_phrases"] == "platform"
    assert tt.platform == "tiktok"

    # alias-normalized platform token resolves to the same fragment
    alias = resolve_effective_policy(db_session, ws, platform="TikTok")
    assert alias.tone == "tt-tone"

    # a platform without a fragment keeps the brand values
    yt = resolve_effective_policy(db_session, ws, platform="youtube")
    assert yt.tone == "brand-tone"
    assert yt.caption_style["size"] == 40
    assert yt.forbidden_phrases == ["brand-bad"]
    assert yt.provenance["forbidden_phrases"] == "brand"


def test_hard_constraint_preservation(db_session):
    """Empty values never erase inherited hard constraints; non-empty sets win."""
    from app.engine.brand import resolve_effective_policy, set_override

    ws = _mk_workspace(db_session, brand_defaults={
        "forbidden_phrases": ["ws-claim"],
        "required_disclaimers": ["Sponsored content"],
        "logo_safe_zone": {"top": 0.2, "right": 0.1, "bottom": 0.3, "left": 0.1},
        "approved_voices": ["voice-ws"],
    })
    _mk_brand(db_session, ws, {
        "tone": "brand-tone",
        "platform_overrides": {"tiktok": {"tone": "tt-tone"}},
    })
    cid = _mk_campaign(db_session, ws)
    content_id = _mk_content(db_session, ws, cid)
    set_override(db_session, ws, "campaign", cid, {
        "tone": "campaign-tone",
        "forbidden_phrases": [],
        "required_disclaimers": [],
        "logo_safe_zone": {},
        "approved_voices": [],
    })
    set_override(db_session, ws, "content", content_id,
                 {"forbidden_phrases": ["content-claim"]})

    policy = resolve_effective_policy(
        db_session, ws, campaign_id=cid, content_id=content_id, platform="tiktok"
    )
    # a non-empty set at a higher level wins for forbidden phrases ...
    assert policy.forbidden_phrases == ["content-claim"]
    # ... but empty attempts at campaign level erased nothing
    assert policy.required_disclaimers == ["Sponsored content"]
    assert policy.logo_safe_zone == {"top": 0.2, "right": 0.1, "bottom": 0.3, "left": 0.1}
    assert policy.approved_voices == ["voice-ws"]
    assert policy.tone == "tt-tone"  # platform is still applied last

    hard = policy.hard_constraints_dict()
    assert hard["forbidden_phrases"] == ["content-claim"]
    assert hard["required_disclaimers"] == ["Sponsored content"]
    assert hard["logo_safe_zone"]["bottom"] == 0.3
    assert hard["approved_voices"] == ["voice-ws"]
    assert policy.provenance["required_disclaimers"] == "workspace"
    assert policy.provenance["approved_voices"] == "workspace"
    assert policy.provenance["forbidden_phrases"] == "content"


def test_forbidden_phrase_detection_is_deterministic(db_session):
    """Forbidden phrases fail from metadata alone -- no LLM in the loop."""
    from app.engine.brand import resolve_effective_policy, verify_artifact

    ws = _mk_workspace(db_session, brand_defaults={
        "forbidden_phrases": ["guaranteed income"],
    })
    policy = resolve_effective_policy(db_session, ws)

    bad = verify_artifact(
        db_session, ws, "script",
        {"script": "This is GUARANTEED INCOME for everyone."},
        policy=policy,
    )
    assert bad.status == "FAIL"
    assert bad.checks["forbidden_phrases"]["status"] == "fail"
    assert "guaranteed income" in bad.checks["forbidden_phrases"]["detail"].lower()
    assert bad.authoritative is True
    # SHADOW semantic review is advisory only and can never change the verdict
    assert bad.semantic.get("authoritative") is False
    assert bad.semantic.get("mode") == "SHADOW"
    assert bad.effective_config_id == policy.effective_config_id

    good = verify_artifact(
        db_session, ws, "script", {"script": "steady money tips"}, policy=policy
    )
    assert good.status == "PASS"
    assert good.checks["forbidden_phrases"]["status"] == "pass"


def test_required_disclaimer_enforced(db_session):
    from app.engine.brand import resolve_effective_policy, verify_artifact

    ws = _mk_workspace(db_session, brand_defaults={
        "required_disclaimers": ["Sponsored content"],
    })
    policy = resolve_effective_policy(db_session, ws)

    missing = verify_artifact(
        db_session, ws, "script", {"script": "buy this now"}, policy=policy
    )
    assert missing.status == "FAIL"
    assert missing.checks["required_disclaimers"]["status"] == "fail"

    present = verify_artifact(
        db_session, ws, "script",
        {"script": "buy this now - Sponsored content"},
        policy=policy,
    )
    assert present.checks["required_disclaimers"]["status"] == "pass"
    assert present.status == "PASS"


def test_workspace_brand_isolation(db_session):
    import pytest

    from app.engine.brand import (
        BrandNotFound,
        brand_layer,
        get_effective_config,
        resolve_effective_policy,
    )

    ws1 = _mk_workspace(db_session, brand_defaults={"tone": "ws1-tone"})
    ws2 = _mk_workspace(db_session, brand_defaults={"tone": "ws2-tone"})
    brand1 = _mk_brand(db_session, ws1, {"tone": "brand1-tone"})
    campaign1 = _mk_campaign(db_session, ws1)

    p1 = resolve_effective_policy(db_session, ws1)
    p2 = resolve_effective_policy(db_session, ws2)
    assert p1.tone == "brand1-tone"
    assert p2.tone == "ws2-tone"
    assert p2.provenance["tone"] == "workspace"

    # cross-workspace references are rejected, never leaked
    with pytest.raises(BrandNotFound):
        resolve_effective_policy(db_session, ws2, brand_id=brand1)
    with pytest.raises(BrandNotFound):
        brand_layer(db_session, ws2, brand1)
    with pytest.raises(BrandNotFound):
        resolve_effective_policy(db_session, ws2, campaign_id=campaign1)
    with pytest.raises(BrandNotFound):
        get_effective_config(db_session, ws2, p1.effective_config_id)


def test_effective_config_snapshot_persisted_and_reproducible(db_session):
    from sqlalchemy import func, select

    from app.engine.brand import (
        EffectiveCreativePolicy,
        get_effective_config,
        resolve_effective_policy,
    )
    from app.models import BrandEffectiveConfig

    ws = _mk_workspace(db_session, brand_defaults={"tone": "ws-tone"})
    _mk_brand(db_session, ws, {"tone": "brand-tone", "forbidden_phrases": ["bad-claim"]})
    cid = _mk_campaign(db_session, ws)

    p1 = resolve_effective_policy(db_session, ws, campaign_id=cid, platform="tiktok")
    p2 = resolve_effective_policy(db_session, ws, campaign_id=cid, platform="tiktok")

    # one row per resolution, both snapshot the identical effective config
    assert p1.effective_config_id and p2.effective_config_id
    assert p1.effective_config_id != p2.effective_config_id
    rows = db_session.scalar(
        select(func.count())
        .select_from(BrandEffectiveConfig)
        .where(BrandEffectiveConfig.workspace_id == ws)
    )
    assert rows >= 2

    snap = get_effective_config(db_session, ws, p1.effective_config_id)
    snap2 = get_effective_config(db_session, ws, p2.effective_config_id)
    assert snap["effective"] == snap2["effective"]
    assert snap["dna_version"] == snap2["dna_version"] == p1.dna_version
    assert snap["subject"] == {"subject_type": "campaign", "subject_id": cid}
    assert snap["platform"] == "tiktok"
    assert snap["provenance"]["tone"] == "brand"

    # identical inputs -> identical policy (volatile id excluded)
    d1 = {k: v for k, v in p1.as_dict().items() if k != "effective_config_id"}
    d2 = {k: v for k, v in p2.as_dict().items() if k != "effective_config_id"}
    assert d1 == d2

    # the snapshot alone reproduces the exact policy
    rebuilt = EffectiveCreativePolicy.build(
        snap["effective"], snap["provenance"], platform="tiktok", workspace_id=ws
    )
    assert rebuilt.tone == p1.tone == "brand-tone"
    assert rebuilt.forbidden_phrases == p1.forbidden_phrases
    assert rebuilt.provenance == p1.provenance
    assert rebuilt.hard_constraints_dict() == p1.hard_constraints_dict()


def test_verifier_status_rollup(db_session):
    """PASS / PASS_WITH_WARNINGS / REVIEW_REQUIRED / FAIL from metadata only."""
    from app.engine.brand import resolve_effective_policy, verify_artifact

    ws = _mk_workspace(db_session, brand_defaults={
        "forbidden_phrases": ["risk-free"],
        "brand_colors": {"primary": "#ff6600"},
        "vocabulary": {"preferred": ["revenue"], "avoid": ["cash grab"]},
        "approved_voices": ["voice-1"],
    })
    policy = resolve_effective_policy(db_session, ws)

    # PASS: on-brand palette, approved voice, preferred term present
    clean = verify_artifact(db_session, ws, "script", {
        "script": "Our revenue grew steadily this quarter.",
        "voice_id": "voice-1",
        "palette": ["#ff6600", "#ffffff"],
    }, policy=policy)
    assert clean.status == "PASS", clean.to_dict()

    # FAIL: forbidden phrase (deterministic substring scan)
    banned = verify_artifact(db_session, ws, "script", {
        "script": "risk-free returns every time",
        "voice_id": "voice-1",
    }, policy=policy)
    assert banned.status == "FAIL"

    # FAIL: unapproved voice
    voice = verify_artifact(db_session, ws, "script", {
        "script": "Our revenue grew.",
        "voice_id": "voice-rogue",
    }, policy=policy)
    assert voice.status == "FAIL"
    assert voice.checks["voice_approval"]["status"] == "fail"

    # REVIEW_REQUIRED: off-brand color token
    offbrand = verify_artifact(db_session, ws, "script", {
        "script": "Our revenue grew.",
        "voice_id": "voice-1",
        "palette": ["#3355ff"],
    }, policy=policy)
    assert offbrand.status == "REVIEW_REQUIRED"

    # PASS_WITH_WARNINGS: preferred terminology unused, nothing violated
    weak = verify_artifact(db_session, ws, "script", {
        "script": "some plain copy",
        "voice_id": "voice-1",
    }, policy=policy)
    assert weak.status == "PASS_WITH_WARNINGS"
    assert weak.checks["terminology"]["status"] == "warning"


# ---------------------------------------------------------------------------
# brand API (workspace-scoped; cross-workspace ids are 404)
# ---------------------------------------------------------------------------


def test_brands_api_dna_editor_and_isolation():
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    headers, ws_id = _register(client)

    base = f"/api/v1/workspaces/{ws_id}/brands"

    # create with DNA (invalid hex is rejected up-front)
    r = client.post(base, headers=headers, json={
        "name": "Acme", "is_default": True,
        "dna": {"tone": "acme-tone", "forbidden_phrases": ["scam"],
                "colors": {"primary": "#abc123"}},
    })
    assert r.status_code == 201, r.text
    brand = r.json()["brand"]
    brand_id = brand["id"]
    assert brand["dna"]["tone"] == "acme-tone"
    assert brand["is_default"] is True

    r = client.post(base, headers=headers,
                    json={"name": "Bad", "dna": {"colors": {"primary": "not-a-color"}}})
    assert r.status_code == 422, r.text

    # list + detail carry the DNA editor payload
    listed = client.get(base, headers=headers).json()["brands"]
    assert any(b["id"] == brand_id for b in listed)
    detail = client.get(f"{base}/{brand_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["brand"]["dna"]["forbidden_phrases"] == ["scam"]

    # PUT replaces the DNA document
    r = client.put(f"{base}/{brand_id}", headers=headers,
                   json={"dna": {"tone": "acme-v2", "forbidden_phrases": ["scam"]}})
    assert r.status_code == 200, r.text
    assert r.json()["brand"]["dna"]["tone"] == "acme-v2"
    r = client.put(f"{base}/{brand_id}", headers=headers, json={"status": "weird"})
    assert r.status_code == 422

    # effective policy endpoint: provenance + snapshot id
    r = client.get(f"{base}/effective?platform=tiktok", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["policy"]["tone"] == "acme-v2"
    assert body["provenance"]["tone"] == "brand"
    assert body["hard_constraints"]["forbidden_phrases"] == ["scam"]
    assert body["effective_config_id"]
    assert body["dna_version"].startswith("sha256:")

    # presets endpoint (empty until Lane C registers builtins)
    r = client.get(f"{base}/presets", headers=headers)
    assert r.status_code == 200
    assert r.json()["presets"] == []

    # verifier endpoint: forbidden phrase fails deterministically
    r = client.post(f"{base}/{brand_id}/verify", headers=headers,
                    json={"artifact_kind": "script",
                          "artifact": {"script": "guaranteed scam-free money"}})
    assert r.status_code == 200, r.text
    report = r.json()["report"]
    assert report["status"] == "FAIL"
    assert report["checks"]["forbidden_phrases"]["status"] == "fail"

    # cross-workspace: another workspace cannot see this brand (404, not 403)
    headers2, ws2 = _register(client)
    r = client.get(f"/api/v1/workspaces/{ws2}/brands/{brand_id}", headers=headers2)
    assert r.status_code == 404, r.text
    r = client.get(f"/api/v1/workspaces/{ws2}/brands", headers=headers)
    assert r.status_code == 403  # ws1 token has no membership on ws2

    # legacy white-label route still answers beside the new surface
    r = client.get(f"/api/v1/workspaces/{ws_id}/brand", headers=headers)
    assert r.status_code == 200
    assert "brand" in r.json()


def test_brand_assets_link_and_unlink():
    from fastapi.testclient import TestClient

    from app.db import session_scope
    from app.main import create_app
    from app.models import MediaAsset

    client = TestClient(create_app(), raise_server_exceptions=False)
    headers, ws_id = _register(client)
    headers2, ws2_id = _register(client)

    def _mk_asset(workspace_id, key):
        with session_scope() as s:
            asset = MediaAsset(workspace_id=workspace_id, type="image",
                               origin="upload", storage_key=key)
            s.add(asset)
            s.flush()
            return asset.id

    own_asset = _mk_asset(ws_id, "brand/logo.png")
    foreign_asset = _mk_asset(ws2_id, "brand/other.png")

    base = f"/api/v1/workspaces/{ws_id}/brands"
    r = client.post(base, headers=headers, json={"name": "LogoBrand"})
    assert r.status_code == 201, r.text
    brand_id = r.json()["brand"]["id"]

    # link a MediaAsset ref (bytes never leave storage)
    r = client.post(f"{base}/{brand_id}/assets", headers=headers,
                    json={"media_asset_id": own_asset, "asset_role": "logo",
                          "label": "primary"})
    assert r.status_code == 200, r.text
    asset_id = r.json()["asset"]["id"]
    assert r.json()["asset"]["media_asset_id"] == own_asset

    # duplicate link and bad role are rejected
    r = client.post(f"{base}/{brand_id}/assets", headers=headers,
                    json={"media_asset_id": own_asset, "asset_role": "logo"})
    assert r.status_code == 409
    r = client.post(f"{base}/{brand_id}/assets", headers=headers,
                    json={"media_asset_id": own_asset, "asset_role": "header"})
    assert r.status_code == 422

    # unknown + cross-workspace media assets are 404
    r = client.post(f"{base}/{brand_id}/assets", headers=headers,
                    json={"media_asset_id": "nope", "asset_role": "logo"})
    assert r.status_code == 404
    r = client.post(f"{base}/{brand_id}/assets", headers=headers,
                    json={"media_asset_id": foreign_asset, "asset_role": "watermark"})
    assert r.status_code == 404

    # detail shows the linked ref
    detail = client.get(f"{base}/{brand_id}", headers=headers).json()["brand"]
    assert [a["id"] for a in detail["assets"]] == [asset_id]

    # unlink (role + asset), then the link is gone
    r = client.delete(f"{base}/{brand_id}/assets?media_asset_id={own_asset}"
                      f"&asset_role=logo", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["removed"] == 1
    r = client.delete(f"{base}/{brand_id}/assets?media_asset_id={own_asset}",
                      headers=headers)
    assert r.status_code == 404
