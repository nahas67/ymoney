"""Brand DNA -> generation wiring (Work 08, Lane C).

Evidence for the brand integration brief:

- BrandDNA reaches REAL generation call sites: shorts derivation (caption
  preset, required disclaimers, lineage), long-form strategy/script
  generation (prompt rules + deterministic forbidden-phrase stripping),
  the UGC pipeline (tone/vocabulary + lineage), localization (brand glossary
  survives a mangling mocked translator), covers (brand color/style hint)
  and voice casting (approved_voices filter).
- The 12-template registry + BrandDNA-over-template defaults
  (``apply_template_defaults``) and inheritance consumed at a real
  generation call site (campaign-level override -> derived short).
- Learning can NEVER override brand hard constraints: a lesson recommending
  POP captions cannot apply when BrandDNA pins ``minimal``; the blocked
  lesson is audited (``brand_blocked_lessons``) and ``applied_brand`` is set.
- Lane A's verifier is folded into QC as a ``brand`` check mapped onto
  PASS / PASS_WITH_WARNINGS / REVIEW_REQUIRED / FAIL; a verifier crash maps
  to a WARNING and never fails a cycle.
- Degradation: with the brand module missing (mid-merge) every call site
  still succeeds with ``applied_brand=False``.
- Workspace isolation: brand-linked outputs never leak across workspaces.

Every double lives in this file — the product ships no mock paths.
"""
from __future__ import annotations

import sys
import uuid
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# helpers (tests only)
# ---------------------------------------------------------------------------


def _brand(db_session, ws_id, dna, *, name="Acme", is_default=True) -> str:
    """Workspace default BrandDNA row (committed: other sessions must see it)."""
    from app.models import Brand, BrandDNARow

    brand = Brand(workspace_id=ws_id, name=name, is_default=is_default)
    db_session.add(brand)
    db_session.flush()
    db_session.add(
        BrandDNARow(workspace_id=ws_id, brand_id=brand.id, dna_json=dict(dna))
    )
    db_session.commit()
    return brand.id


def _second_workspace(db_session, **settings):
    from app.models import Workspace

    ws = Workspace(name="Brand WS 2", slug=f"bws2-{uuid.uuid4().hex[:8]}",
                   niche="money", settings_json=dict(settings))
    db_session.add(ws)
    db_session.flush()
    db_session.commit()
    return ws.id


def _ctx(ws_id: str):
    from app.services.jobs import JobContext

    return JobContext(job_id=f"j-{uuid.uuid4().hex[:8]}", type="test",
                      workspace_id=ws_id, cycle_id=None, payload={}, attempt=1,
                      cancelled=lambda: False)


class _GenCtx:
    """Lightweight context for strategist/script call sites (no job row)."""

    def __init__(self, ws_id: str):
        self.workspace_id = ws_id
        self.job_id = None
        self.cycle_id = None
        self.type = "test"
        self.payload = {}
        self.artifacts = {}


def _workspace(db_session):
    from app.models import Workspace

    ws = Workspace(name="Brand Gen WS", slug=f"bgw-{uuid.uuid4().hex[:8]}",
                   niche="money")
    db_session.add(ws)
    db_session.flush()
    db_session.commit()
    return ws.id


def _campaign(db_session, ws_id, name="Brand push"):
    from app.models import Campaign

    row = Campaign(workspace_id=ws_id, name=name, goal="subs")
    db_session.add(row)
    db_session.flush()
    db_session.commit()
    return row


def _master(db_session, ws_id, campaign_id):
    from app.engine.timeline import add_clip, create_empty
    from app.models import ContentItem, ContentTimeline, Scene

    master = ContentItem(
        workspace_id=ws_id, campaign_id=campaign_id,
        topic="master personal finance video", status="PUBLISHED",
    )
    db_session.add(master)
    db_session.flush()

    doc = create_empty(ws_id, duration_seconds=300.0, aspect="16:9")
    add_clip(doc, track="video", clip_id="v1", name="master shot",
             start=0.0, duration=300.0, source={"file": "master.mp4"})
    for i, (s, text) in enumerate([
        (0.0, "Welcome to the money masterclass today."),
        (30.0, "First rule is to pay yourself before bills."),
        (90.0, "Second rule compounds every single year."),
        (150.0, "Third rule avoids lifestyle inflation traps."),
        (210.0, "Final rule is to automate all investing."),
    ]):
        add_clip(doc, track="caption", clip_id=f"c{i}", name=text,
                 start=s, duration=20.0)
    db_session.add(ContentTimeline(
        workspace_id=ws_id, content_item_id=master.id, name="main",
        fps=30.0, duration_seconds=300.0, tracks_json=doc, version=1,
    ))
    for i, (s, e) in enumerate([(0.0, 60.0), (60.0, 120.0), (120.0, 180.0),
                                (180.0, 240.0), (240.0, 300.0)]):
        db_session.add(Scene(
            workspace_id=ws_id, content_item_id=master.id, timeline_id=None,
            chapter_id=f"ch-{i % 3}", index=i, title=f"part {i}",
            script_segment=f"master segment {i} about money rules",
            start_seconds=s, end_seconds=e,
        ))
    db_session.flush()
    db_session.commit()
    return master


def _moments(*, transcript: str = "First rule is to pay yourself before bills."):
    return [
        {"start": 28.0, "end": 58.0, "topic": "pay yourself first",
         "hook_text": "Why do paychecks vanish by Friday?",
         "cta_text": "Follow for rule two", "transcript": transcript},
    ]


def _derive_one(db_session, ws_id, campaign, master, moments=None, platforms=None):
    from app.engine.campaign.plan import build_derivation_plan
    from app.engine.campaign.shorts import derive_shorts

    plan = build_derivation_plan(
        db_session, campaign.id, master.id, platforms or ["tiktok"],
        desired_shorts=1)
    return derive_shorts(db_session, ws_id, campaign.id, master.id,
                         moments or _moments(), plan)


def _short_timeline(db_session, short):
    from app.models import ContentTimeline

    return db_session.query(ContentTimeline).filter(
        ContentTimeline.content_item_id == short.id).one()


@pytest.fixture()
def mock_tts(monkeypatch):
    """Voice-stage double: the default edge provider would reach the network."""
    from app.providers import tts as tts_mod

    monkeypatch.setattr(
        tts_mod, "get_tts_provider", lambda *a, **k: tts_mod.MockTTSProvider()
    )
    return tts_mod.MockTTSProvider


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def storage_root(tmp_path, monkeypatch):
    """Keep subtitle/TTS writes inside the test sandbox."""
    from app.services import storage

    root = tmp_path / "videos"
    monkeypatch.setattr(storage, "STORAGE_ROOT", root)
    return root


# ---------------------------------------------------------------------------
# 1. template registry: 12 templates + brand-over-template defaults
# ---------------------------------------------------------------------------


def test_template_registry_has_12_templates_and_brand_overrides_defaults():
    from app.engine.brand_templates import (
        TEMPLATE_KEYS,
        apply_template_defaults,
        get_template,
        list_templates,
        template_for,
    )

    expected = {
        "youtube_longform", "shorts", "reels", "tiktok", "documentary",
        "explainer", "educational", "ugc", "product_launch", "news",
        "talking_head", "faceless",
    }
    assert set(TEMPLATE_KEYS) == expected
    assert len(TEMPLATE_KEYS) == 12
    assert [t["key"] for t in list_templates()] == list(TEMPLATE_KEYS)
    assert get_template("nope") == {}

    # a template is creative defaults only — no pipeline, no brand
    reels = get_template("reels")
    assert reels["defaults"]["aspect_ratio"] == "9:16"
    assert reels["defaults"]["caption_preset"] == "pop"

    # BrandDNA > template: the brand's tone/caption preset win, and the
    # overridden keys are listed for audit.
    brand_policy = {"tone": "measured coach",
                    "caption_style": {"preset": "minimal"},
                    "brand_colors": ["#ff6600"]}
    merged = apply_template_defaults(brand_policy, "reels")
    assert merged["tone"] == "measured coach"
    assert merged["caption_preset"] == "minimal"
    assert merged["brand_colors"] == ["#ff6600"]
    assert set(merged["_brand_overrides"]) >= {"tone", "caption_preset",
                                               "brand_colors"}
    # no policy -> pure template defaults
    plain = apply_template_defaults(None, "reels")
    assert plain["tone"] == "playful"
    assert plain["_brand_overrides"] == []

    # platform token -> template mapping
    assert template_for(platform="tiktok") == "tiktok"
    assert template_for(platform="youtube") == "youtube_longform"
    assert template_for({"content_format": "ugc"}, default="ugc") == "ugc"


# ---------------------------------------------------------------------------
# 2. BrandDNA -> shorts (real derivation call site)
# ---------------------------------------------------------------------------


def test_branddna_applied_to_shorts(db_session):
    from app.engine.brand_templates import brand_gate, brand_variant_metadata

    ws_id = _workspace(db_session)
    _brand(db_session, ws_id, {
        "tone": "measured coach",
        "forbidden_phrases": ["get rich quick"],
        "required_disclaimers": ["Not financial advice"],
        "caption_style": {"preset": "minimal", "forbidden": ["pop"]},
        "colors": {"primary": "#ff6600"},
        "platform_overrides": {"tiktok": {"tone": "fast + punchy"}},
    })
    campaign = _campaign(db_session, ws_id)
    master = _master(db_session, ws_id, campaign.id)

    shorts = _derive_one(db_session, ws_id, campaign, master)
    assert len(shorts) == 1

    doc = _short_timeline(db_session, shorts[0]).tracks_json
    camp = doc.get("campaign") or {}
    # brand caption preset outranks the plan default style (canonical location:
    # the short doc carries its caption style under the campaign block)
    assert camp.get("caption_style") == "minimal"
    # required disclaimers land on the derived artifact
    assert camp.get("required_disclaimers") == ["Not financial advice"]
    markers = camp.get("brand") or {}
    assert markers.get("applied_brand") is True
    assert markers.get("effective_config_id")

    # gate-level detail: provenance + platform override fragment metadata
    gate = brand_gate(db_session, ws_id, campaign_id=campaign.id,
                      platform="tiktok", artifact={"content_format": "short"})
    assert gate["brand_available"] is True
    assert gate["tone"] == "fast + punchy"          # platform wins last
    assert gate["provenance"]["tone"] == "platform"
    assert gate["provenance"]["required_disclaimers"] == "brand"
    fragment = brand_variant_metadata(gate, platform="tiktok")
    assert fragment["required_disclaimers"] == ["Not financial advice"]
    assert fragment["brand_platform_overrides"] == {"tone": "fast + punchy"}
    assert fragment["applied_brand"] is True


# ---------------------------------------------------------------------------
# 3. BrandDNA -> long-form strategy/script generation
# ---------------------------------------------------------------------------


def test_branddna_applied_to_long_form(db_session, workspace_with_user):
    from app.engine.agents.creation import ScriptWriterAgent, StrategistAgent

    ws = workspace_with_user["workspace"]
    _brand(db_session, ws, {
        "tone": "seasoned educator",
        "forbidden_phrases": ["get rich quick"],
        "vocabulary": {"preferred": ["revenue"], "avoid": ["cash grab"]},
    })

    # strategist: brand rules are PROMPT INPUTS and the brand tone wins
    captured: dict = {}

    def _fake_json(system, user, **kw):
        captured["strategy_system"] = system
        return {"tone": "model-tone", "platforms": ["youtube"],
                "duration_seconds": 600, "angle": "compounding explained"}

    with patch("app.providers.llm.complete_json", side_effect=_fake_json):
        out = StrategistAgent().strategize(_GenCtx(ws), "retirement math",
                                           {"summary": "s"})
    prompt = captured["strategy_system"]
    assert "BRAND RULES" in prompt
    assert '"get rich quick"' in prompt          # forbidden phrase listed
    assert "revenue" in prompt                   # preferred vocabulary listed
    assert out["tone"] == "seasoned educator"    # brand > model
    assert out["brand"]["applied_brand"] is True
    assert out["brand"]["effective_config_id"]

    # script writer (long-form brief): deterministic post-check strips the
    # forbidden phrase and records the hit for the audit trail
    def _fake_complete(system, user, **kw):
        captured["script_system"] = system
        class _R:
            text = ("This plan is a get rich quick scheme that always works. "
                    "Here is the long-form breakdown of compounding revenue "
                    "over thirty years with concrete numbers and examples "
                    "that hold up under scrutiny.")
        return _R()

    strategy = {"duration_seconds": 600, "hook_type": "question",
                "tone": "seasoned educator"}
    with patch("app.providers.llm.complete", side_effect=_fake_complete):
        script = ScriptWriterAgent().write_script(
            _GenCtx(ws), "retirement math", strategy, {})
    assert "BRAND RULES" in captured["script_system"]
    assert "get rich quick" not in script.lower()
    assert strategy["brand_stripped"] == ["get rich quick"]


# ---------------------------------------------------------------------------
# 4. BrandDNA -> UGC pipeline
# ---------------------------------------------------------------------------


def test_branddna_applied_to_ugc(db_session, client, mock_tts):
    from tests.test_ugc import _asset, _create_project, _register

    ctx = _register(client, "ugcb")
    _brand(db_session, ctx["ws"], {
        "tone": "calm educator",
        "vocabulary": {"preferred": ["Aurora Lamp"], "avoid": ["gadget"]},
        "forbidden_phrases": ["miracle"],
    })
    asset_id = _asset(db_session, ctx["ws"])
    brief = {"topic": "Aurora Lamp", "audience": "home decor buyers",
             "product_assets": [asset_id]}   # no operator tone: brand wins

    body = _create_project(client, ctx, "PRODUCT_DEMO", brief)
    lineage = body["project"]["lineage"]

    strategy = lineage["strategy"]
    assert strategy["tone"] == "calm educator"      # resolved brand policy
    assert strategy["brand"]["applied_brand"] is True
    assert strategy["brand"]["effective_config_id"]
    # legacy workspace brand_voice fallback still appended after the policy
    brand_lineage = lineage.get("brand") or {}
    assert brand_lineage.get("applied_brand") is True
    assert body["qc"]["status"] in ("PASS", "PASS_WITH_WARNINGS",
                                    "REVIEW_REQUIRED", "FAIL")


# ---------------------------------------------------------------------------
# 5. BrandDNA -> localization glossary (mocked translator mangles the term)
# ---------------------------------------------------------------------------


def test_brand_glossary_survives_mocked_translation(
        db_session, workspace_with_user, storage_root):
    from app.engine import timeline as tl
    from app.engine.localization.pipeline import LocalizationPipeline, prepare_localizations
    from app.models import ContentItem, ContentTimeline, Scene

    ws = workspace_with_user["workspace"]
    _brand(db_session, ws, {
        "vocabulary": {"preferred": ["ProfitLadder"], "avoid": []},
        "pronunciation_rules": [{"term": "YMONEY", "pronunciation": "Y-Money"}],
    })

    # source content whose narration carries the brand term
    doc = tl.create_empty(ws, duration_seconds=6.0, fps=30.0, aspect="9:16")
    tl.add_clip(doc, track="video", clip_id="src_1", name="opening",
                start=0.0, duration=6.0, source={"asset": "source.mp4"})
    content = ContentItem(workspace_id=ws, topic="ladder strategy",
                          status="READY")
    db_session.add(content)
    db_session.flush()
    db_session.add(ContentTimeline(
        workspace_id=ws, content_item_id=content.id, name="main",
        fps=30.0, duration_seconds=6.0, tracks_json=doc, version=1))
    db_session.flush()
    for i, (start, end, text) in enumerate([
            (0.0, 3.0, "Build the ProfitLadder step by step with YMONEY."),
            (3.0, 6.0, "Then reinvest every dollar of revenue you earn.")]):
        db_session.add(Scene(workspace_id=ws, content_item_id=content.id,
                             timeline_id=None, index=i, title=f"scene {i}",
                             narration=text, script_segment=text,
                             start_seconds=start, end_seconds=end))
    db_session.commit()

    row = prepare_localizations(
        db_session, workspace_id=ws, source_content_id=content.id,
        target_languages=["es"], locales={"es": "es-ES"})[0]

    # the mocked translator DROPS the brand term entirely on purpose
    def _drops_brand(texts, target_lang, *, workspace_id="", glossary=None):
        out = []
        for t in texts:
            t = t.replace("ProfitLadder", "escalera de renta")
            t = t.replace("YMONEY", "y-money")
            t = t.replace("revenue", "ingresos")
            out.append(t)
        return out

    pipeline = LocalizationPipeline(
        db_session, localized_content_id=row.id, workspace_id=ws,
        translate_fn=_drops_brand,
        voice_fn=lambda lang, explicit="": f"{lang}-neural-1",
        tts_fn=lambda texts, voice, work_dir: _fake_tts(texts, voice, work_dir))
    pipeline.run()

    from app.models import ContentTimeline as CTL
    out_timeline = db_session.get(CTL, row.timeline_id)
    track = next(t for t in out_timeline.tracks_json["tracks"]
                 if t["kind"] == "caption")
    captions = [c["text"]["content"] for c in track["clips"]]
    # brand glossary term (BrandDNA vocabulary) was re-inserted verbatim
    assert any("ProfitLadder" in c for c in captions), captions
    repairs = dict(row.lineage_json).get("repairs") or []
    hit = next((r for r in repairs if r["term"] == "ProfitLadder"), None)
    assert hit is not None, repairs
    assert hit["kind"] == "brand"
    assert hit["action"] in ("reinserted", "replaced")

    # the brand fold is documented precedence: brand > operator > workspace
    from app.engine.localization.pipeline import brand_glossary_overlay
    overlay = brand_glossary_overlay(db_session, ws, [
        {"term": "ProfitLadder", "replacement": "brand-wins",
         "target_languages": [], "kind": "terminology",
         "case_sensitive": False, "source": "overlay"},
        {"term": "OperatorTerm", "replacement": "kept",
         "target_languages": [], "kind": "terminology",
         "case_sensitive": False, "source": "overlay"},
    ])
    by_term = {e["term"].casefold(): e for e in overlay}
    assert by_term["profitladder"]["replacement"] == "ProfitLadder"  # brand wins
    assert by_term["profitladder"]["kind"] == "brand"
    assert by_term["operatorterm"]["replacement"] == "kept"  # operator untouched


def _fake_tts(texts, voice, work_dir):
    from pathlib import Path

    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    out = []
    for i, _ in enumerate(texts):
        p = root / f"seg_{i}.wav"
        p.write_bytes(b"RIFF0000WAVE")
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# 6. inheritance consumed at a real generation call site (campaign level)
# ---------------------------------------------------------------------------


def test_inheritance_consumed_at_real_generation_call_site(db_session):
    from app.engine.brand import resolve_effective_policy, set_override
    from app.engine.brand_templates import brand_gate

    ws_id = _workspace(db_session)
    _brand(db_session, ws_id, {"tone": "brand-tone"})
    campaign = _campaign(db_session, ws_id)
    # campaign-level patch lives OUTSIDE the DNA document
    set_override(db_session, ws_id, "campaign", campaign.id,
                 {"required_disclaimers": ["Sponsored: brand deal"],
                  "forbidden_phrases": ["campaign-bad"]})
    master = _master(db_session, ws_id, campaign.id)

    policy = resolve_effective_policy(db_session, ws_id,
                                      campaign_id=campaign.id)
    assert policy.tone == "brand-tone"
    assert policy.provenance["tone"] == "brand"
    assert policy.required_disclaimers == ["Sponsored: brand deal"]
    assert policy.provenance["required_disclaimers"] == "campaign"

    # the SAME inheritance is what a real derivation consumes
    shorts = _derive_one(db_session, ws_id, campaign, master)
    doc = _short_timeline(db_session, shorts[0]).tracks_json
    camp = doc.get("campaign") or {}
    assert camp.get("required_disclaimers") == ["Sponsored: brand deal"]
    assert camp["brand"]["applied_brand"] is True
    assert camp["brand"]["effective_config_id"]

    # the stored DNA was never mutated by the campaign override
    from sqlalchemy import select

    from app.models import BrandDNARow
    row = db_session.scalar(select(BrandDNARow).where(
        BrandDNARow.workspace_id == ws_id))
    assert "required_disclaimers" not in dict(row.dna_json)

    # a workspace WITHOUT the override is untouched
    ws2 = _second_workspace(db_session)
    gate2 = brand_gate(db_session, ws2)
    assert gate2["required_disclaimers"] == []
    assert gate2["forbidden_phrases"] == []


# ---------------------------------------------------------------------------
# 7-8. learning NEVER overrides brand hard constraints (+ audit)
# ---------------------------------------------------------------------------


def _lesson_obs(metric, recommendation, *, platform="tiktok", topic="finance"):
    return {
        "metric": metric,
        "scope": {"platform": platform, "topic": topic},
        "effect": {"direction": "positive", "improvement_pct": 21.0,
                   "recommendation": recommendation,
                   "kinds": ["strategy", "script"]},
        "evidence_ids": [f"vid-{metric}-a", f"vid-{metric}-b"],
        "sample_size": 12,
    }


def _enable_learning(db_session, ws):
    from app.models import Workspace

    row = db_session.get(Workspace, ws)
    row.settings_json = {**(row.settings_json or {}), "learning_assist": True}
    db_session.commit()


def test_learning_cannot_override_hard_brand_rules(db_session,
                                                   workspace_with_user):
    from app.engine.performance import learning as lessons

    ws = workspace_with_user["workspace"]
    _enable_learning(db_session, ws)
    assert lessons.generate_lessons(db_session, ws, [
        _lesson_obs("retention", "Switch captions to POP for higher retention."),
        _lesson_obs("ctr", "Open with the payoff number."),
    ])["stored"] == 2

    # BrandDNA pins the caption preset -> the POP lesson can NEVER apply
    _brand(db_session, ws, {"caption_style": {"preset": "minimal"},
                            "tone": "measured"})

    out = lessons.apply_lessons(db_session, ws,
                                {"platform": "tiktok", "topic": "finance"},
                                {"topic": "x"}, "strategy")
    applied = out.get("applied_lessons") or []
    blocked = out.get("brand_blocked_lessons") or []
    blocked_keys = [b["pattern_key"] for b in blocked]

    rows = {r.pattern_key: r for r in lessons.matching_lessons(
        db_session, ws, {"platform": "tiktok", "topic": "finance"},
        kinds="strategy")}
    pop_key = next(k for k, r in rows.items()
                   if "POP" in str(r.evidence_json.get("effect", {}).get(
                       "recommendation", "")))
    assert pop_key not in applied
    assert pop_key in blocked_keys
    # the non-conflicting lesson still applies (filter is not a blanket drop)
    assert any(k in applied for k in rows if k != pop_key)
    assert out["applied_brand"] is True
    # protected brand keys are never writable by lessons
    assert "caption_style" in lessons.PROTECTED_KEYS
    assert {"forbidden_phrases", "required_disclaimers", "approved_voices",
            "logo_safe_zone"} <= set(lessons.PROTECTED_KEYS)


def test_brand_blocked_lesson_audit(db_session, workspace_with_user):
    from app.engine.performance import learning as lessons

    ws = workspace_with_user["workspace"]
    _enable_learning(db_session, ws)
    assert lessons.generate_lessons(db_session, ws, [
        _lesson_obs("retention", "Use POP captions everywhere for reach."),
    ])["stored"] == 1
    _brand(db_session, ws, {"caption_style": {"preset": "minimal"}})

    out = lessons.apply_lessons(db_session, ws,
                                {"platform": "tiktok", "topic": "finance"},
                                {"topic": "x"}, "strategy")
    assert out["applied_lessons"] == []
    (entry,) = out["brand_blocked_lessons"]
    assert entry["pattern_key"]
    assert "minimal" in entry["reason"] and "pop" in entry["reason"]
    assert out["applied_brand"] is True
    assert "lesson_recommendations" not in out   # nothing was applied
    # a lesson WITHOUT the brand conflict still survives the same filter
    assert lessons.generate_lessons(db_session, ws, [
        _lesson_obs("ctr", "Open with the payoff number."),
    ])["stored"] == 1
    out2 = lessons.apply_lessons(db_session, ws,
                                 {"platform": "tiktok", "topic": "finance"},
                                 {"topic": "x"}, "strategy")
    assert out2["applied_lessons"]
    assert [b["pattern_key"] for b in out2["brand_blocked_lessons"]] == [
        entry["pattern_key"]]


# ---------------------------------------------------------------------------
# 9-10. verifier folded into QC; crash -> warning, never FAIL
# ---------------------------------------------------------------------------


def test_brand_verifier_folded_into_campaign_qc(db_session):
    from app.engine.campaign.qc import campaign_qc

    ws_id = _workspace(db_session)
    _brand(db_session, ws_id, {"forbidden_phrases": ["get rich quick"],
                               "tone": "measured"})
    campaign = _campaign(db_session, ws_id)
    master = _master(db_session, ws_id, campaign.id)
    _derive_one(db_session, ws_id, campaign, master,
                moments=_moments(transcript="This is a get rich quick plan."))

    report = campaign_qc(db_session, campaign.id)
    brand = (report.get("checks") or {}).get("brand")
    assert brand is not None, "brand check must be folded into QC"
    assert brand["status"] == "fail"
    assert report["result"] == "FAIL"
    assert brand.get("effective_config_id")

    # clean corpus under the same brand: brand check passes, cycle survives
    ws2 = _workspace(db_session)
    _brand(db_session, ws2, {"forbidden_phrases": ["get rich quick"]})
    campaign2 = _campaign(db_session, ws2, "Clean push")
    master2 = _master(db_session, ws2, campaign2.id)
    _derive_one(db_session, ws2, campaign2, master2)
    report2 = campaign_qc(db_session, campaign2.id)
    brand2 = (report2.get("checks") or {}).get("brand")
    assert brand2 is not None
    assert brand2["status"] in ("pass", "warning")
    assert report2["result"] in ("PASS", "PASS_WITH_WARNINGS")


def test_brand_verifier_crash_maps_to_warning_never_fails(db_session):
    import app.engine.brand as brand_mod
    from app.engine.brand_templates import brand_qc_check

    ws_id = _workspace(db_session)
    _brand(db_session, ws_id, {"forbidden_phrases": ["get rich quick"]})

    def _boom(*a, **k):
        raise RuntimeError("verifier exploded")

    original = brand_mod.verify_artifact
    brand_mod.verify_artifact = _boom  # noqa: E800 - patched below
    try:
        check = brand_qc_check(db_session, ws_id,
                               artifact={"text": "get rich quick",
                                         "artifact_kind": "script"})
    finally:
        brand_mod.verify_artifact = original
    assert check is not None
    assert check["status"] == "warning"
    assert "exploded" in check["detail"]


# ---------------------------------------------------------------------------
# 11. degradation: brand module missing -> generation still succeeds
# ---------------------------------------------------------------------------


def test_degradation_when_brand_module_missing(db_session, monkeypatch):
    from app.engine.agents.creation import StrategistAgent
    from app.engine.brand_templates import brand_gate

    ws_id = _workspace(db_session)
    _brand(db_session, ws_id, {"tone": "brand-tone",
                               "forbidden_phrases": ["bad-claim"]})
    campaign = _campaign(db_session, ws_id)
    master = _master(db_session, ws_id, campaign.id)

    gate = brand_gate(db_session, ws_id)
    assert gate["applied_brand"] is True   # sanity: brand works BEFORE the cut

    poisoned = ("app.engine.brand", "app.engine.brand.policy",
                "app.engine.brand.inheritance", "app.engine.brand.verifier",
                "app.engine.brand_templates", "app.engine.brand_wiring")
    for name in poisoned:
        monkeypatch.setitem(sys.modules, name, None)

    # the wiring helper degrades instead of raising
    degraded = brand_gate(db_session, ws_id)
    assert degraded["brand_available"] is False
    assert degraded["applied_brand"] is False
    assert degraded["degraded"] == "brand_module_unavailable"
    assert degraded["forbidden_phrases"] == []

    # strategist still generates (lineage markers all-false)
    def _fake_json(system, user, **kw):
        assert "BRAND RULES" not in system
        return {"tone": "model-tone", "duration_seconds": 30}

    with patch("app.providers.llm.complete_json", side_effect=_fake_json):
        out = StrategistAgent().strategize(_GenCtx(ws_id), "topic", {})
    assert out["tone"] == "model-tone"
    assert out["brand"]["applied_brand"] is False

    # shorts derivation still succeeds
    shorts = _derive_one(db_session, ws_id, campaign, master)
    assert len(shorts) == 1
    camp = _short_timeline(db_session, shorts[0]).tracks_json.get("campaign") or {}
    assert camp.get("required_disclaimers", []) == []
    assert camp.get("brand", {}).get("applied_brand") is False


# ---------------------------------------------------------------------------
# 12. workspace isolation of brand-linked outputs
# ---------------------------------------------------------------------------


def test_workspace_isolation_of_brand_linked_outputs(db_session):
    from app.engine.brand_templates import brand_gate

    ws1 = _workspace(db_session)
    ws2 = _second_workspace(db_session)
    _brand(db_session, ws1, {"forbidden_phrases": ["alpha-claim"],
                             "required_disclaimers": ["WS1 disclosure"],
                             "tone": "ws1-tone"}, name="Alpha")
    _brand(db_session, ws2, {"tone": "ws2-tone"}, name="Beta")

    gate1 = brand_gate(db_session, ws1)
    gate2 = brand_gate(db_session, ws2)
    assert gate1["forbidden_phrases"] == ["alpha-claim"]
    assert gate2["forbidden_phrases"] == []
    assert gate1["required_disclaimers"] == ["WS1 disclosure"]
    assert gate2["required_disclaimers"] == []
    assert gate1["effective_config_id"] != gate2["effective_config_id"]

    # derived outputs stay inside their own workspace's brand
    c1 = _campaign(db_session, ws1, "WS1 push")
    m1 = _master(db_session, ws1, c1.id)
    c2 = _campaign(db_session, ws2, "WS2 push")
    m2 = _master(db_session, ws2, c2.id)
    s1 = _derive_one(db_session, ws1, c1, m1)[0]
    s2 = _derive_one(db_session, ws2, c2, m2)[0]
    assert s1.workspace_id == ws1 and s2.workspace_id == ws2

    camp1 = _short_timeline(db_session, s1).tracks_json.get("campaign") or {}
    camp2 = _short_timeline(db_session, s2).tracks_json.get("campaign") or {}
    assert camp1.get("required_disclaimers") == ["WS1 disclosure"]
    assert camp1["brand"]["applied_brand"] is True
    assert camp2.get("required_disclaimers", []) == []
    # ws2 has its OWN brand: it applies there too, but with its own config —
    # isolation means no ws1 field (disclaimers/tone) leaks into ws2 output.
    assert camp2.get("brand", {}).get("applied_brand") is True
    assert camp1["brand"]["effective_config_id"] != \
        camp2["brand"].get("effective_config_id")
    assert "alpha-claim" not in str(camp2.get("brand"))
    assert camp2.get("required_disclaimers") != ["WS1 disclosure"]


# ---------------------------------------------------------------------------
# 13. hook optimizer + voice + covers call sites
# ---------------------------------------------------------------------------


def test_hook_optimizer_rejects_forbidden_phrases(db_session,
                                                  workspace_with_user):
    from app.engine.agents.creation import HookOptimizerAgent

    ws = workspace_with_user["workspace"]
    _brand(db_session, ws, {"forbidden_phrases": ["get rich quick"]})

    variants = [
        {"label": "v1", "hook": "This get rich quick trick is wild",
         "script": "This get rich quick trick is wild"},
        {"label": "v2", "hook": "Why do paychecks vanish by Friday?",
         "script": "Why do paychecks vanish by Friday?"},
    ]
    ranked = HookOptimizerAgent().rank_hooks(_ctx(ws), variants)
    blocked = [v for v in ranked if v.get("brand_blocked")]
    assert len(blocked) == 1
    assert blocked[0]["label"] == "v1"
    assert blocked[0]["predicted_score"] == 0.0
    assert "get rich quick" in blocked[0]["brand_rejection_reason"]
    assert ranked[-1]["label"] == "v1"          # never wins the ranking
    assert ranked[0]["label"] == "v2"
    assert ranked[0]["brand"]["applied_brand"] is True


def test_voice_filtered_to_approved_voices(db_session,
                                           workspace_with_user):
    from app.engine.agents.voice import _brand_voice

    ws = workspace_with_user["workspace"]
    _brand(db_session, ws, {"approved_voices": ["voice-approved"]})
    # unapproved request is swapped for the first approved voice
    assert _brand_voice(ws, "voice-rogue") == "voice-approved"
    assert _brand_voice(ws, "voice-approved") == "voice-approved"
    # empty request gets pinned to the brand voice
    assert _brand_voice(ws, "") == "voice-approved"

    # no brand -> the request passes through untouched
    ws2 = _second_workspace(db_session)
    assert _brand_voice(ws2, "voice-rogue") == "voice-rogue"


def test_covers_carry_brand_colors_and_style_hint(db_session,
                                                  workspace_with_user):
    from app.engine.campaign.covers import brand_cover_hint, build_cover_spec

    ws = workspace_with_user["workspace"]
    _brand(db_session, ws, {"colors": {"primary": "#ff6600"},
                            "thumbnail_style": {"layout": "bold_text"},
                            "tone": "measured"})

    hint = brand_cover_hint(db_session, ws, platform="tiktok")
    assert hint["brand_colors"] == ["#ff6600"]
    assert hint["style_hint"] == {"layout": "bold_text"}
    assert hint["applied_brand"] is True

    spec = build_cover_spec(topic="Save 20% of every paycheck",
                            platform="tiktok", brand=hint)
    assert spec["brand"]["brand_colors"] == ["#ff6600"]
    # without a brand the spec is byte-identical to the legacy shape
    plain = build_cover_spec(topic="Save 20% of every paycheck",
                             platform="tiktok")
    assert "brand" not in plain


def test_brand_wiring_module_is_the_canonical_import_path():
    import app.engine.brand_wiring as wiring
    from app.engine import brand_templates

    for name in ("brand_gate", "lineage_markers", "brand_qc_check",
                 "lesson_conflicts_with_brand", "merge_brand_glossary",
                 "brand_cover_style", "approved_voice"):
        assert getattr(wiring, name) is getattr(brand_templates, name)
