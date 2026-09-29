"""Localization pipeline, QC and routes (Work 07 Lane A).

Evidence for the localization brief:

- translation lineage: the source is NEVER mutated; output lands on a NEW
  ContentItem child (derivation ``localized``) + ContentTimeline, with
  language / locale / translation_version / parent-root lineage recorded.
- deterministic glossary + pronunciation enforcement: a brand term survives a
  mocked translator that mangles it (repaired in place, reported to QC).
- literal vs localized separation: captions/voice carry the culturally adapted
  text, the literal string is kept for audit in lineage.
- number / name / URL preservation and the QC status rollup
  (PASS | PASS_WITH_WARNINGS | REVIEW_REQUIRED | FAIL).
- metadata localization: prose is translated, numbers/metrics never invented.
- cancellable runs through ``app.services.jobs``.
- workspace isolation: cross-workspace localization rows are 404.
- the produced timeline loads and saves through the editor API.

Every double lives in this file — the product ships no mock paths.
"""
from __future__ import annotations

import copy
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.engine import timeline as tl
from app.engine.localization import pipeline as pipeline_mod
from app.engine.localization import quality
from app.engine.localization.pipeline import (
    LocalizationError,
    LocalizationPipeline,
    adapt_culture,
    apply_pronunciation,
    enforce_glossary,
    glossary_applies,
    localize_metadata,
    normalize_languages,
    prepare_localizations,
)
from app.models import (
    ContentItem,
    ContentTimeline,
    GlossaryTerm,
    LocalizationQCReport,
    LocalizedContent,
    Scene,
)
from app.models.localization import QC_STATUSES

# ---------------------------------------------------------------------------
# deterministic doubles (tests only)
# ---------------------------------------------------------------------------

SOURCE_L1 = "Save 20% of every paycheck with YMONEY and follow for more."
SOURCE_L2 = "Step two: pay off $5,000 in 90 days at https://ymoney.example.com"

#: the "translator" mangles the brand term and leaves the CTA in English on
#: purpose — the pipeline must repair the brand and adapt the CTA.
ES_L1 = "Ahorra 20% de cada cheque con Y-Money y follow for more."
ES_L2 = "Paso dos: paga $5,000 en 90 días en https://ymoney.example.com"

PHRASES = {SOURCE_L1: ES_L1, SOURCE_L2: ES_L2}

#: word-level fallback so metadata prose is translated without touching
#: numbers, URLs or unknown keys
_WORDS = {
    "How": "Cómo",
    "to": "para",
    "save": "ahorrar",
    "fast": "rápido",
    "money": "dinero",
    "today": "hoy",
}

STANDARD_STRATEGY = {
    "title": "How to save $5,000 fast",
    "description": "A 90-day plan that works: https://ymoney.example.com",
    "hashtags": ["#saveMoney", "#ymoney"],
    "thumbnail_text": "Save 20% today",
    "metrics": {"views": 1200, "ctr": 0.04},
}


def _pseudo(text: str) -> str:
    out = text
    for en, tgt in _WORDS.items():
        out = re.sub(rf"\b{re.escape(en)}\b", tgt, out, flags=re.IGNORECASE)
    return out


def fake_translate(texts, target_lang, *, workspace_id="", glossary=None):
    """Index-aligned pseudo-translation; numbers/URLs are never rewritten."""
    return [PHRASES.get(t) or _pseudo(t) for t in texts]


def fake_voice(target_lang, explicit=""):
    return f"{target_lang}-neural-1"


def fake_tts(texts, voice, work_dir):
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for i, _ in enumerate(texts):
        p = work_dir / f"seg_{i}.wav"
        p.write_bytes(b"RIFF0000WAVE")
        out.append(p)
    return out


@pytest.fixture()
def storage_root(tmp_path, monkeypatch):
    """Keep subtitle/TTS writes inside the test sandbox."""
    from app.services import storage

    root = tmp_path / "videos"
    monkeypatch.setattr(storage, "STORAGE_ROOT", root)
    return root


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

def _build_source(db, ws_id, *, strategy=None):
    """Source content: one timeline, two scenes, one on-screen text clip."""
    doc = tl.create_empty(ws_id, duration_seconds=6.0, fps=30.0, aspect="9:16")
    tl.add_clip(doc, track="video", clip_id="src_1", name="opening",
                start=0.0, duration=6.0, source={"asset": "source.mp4"})
    tl.add_clip(doc, track="text", clip_id="src_txt", name="lower third",
                start=0.0, duration=3.0,
                text={"content": "How to save money"})

    content = ContentItem(workspace_id=ws_id, topic="save money fast",
                          status="READY",
                          strategy_json=dict(strategy or STANDARD_STRATEGY))
    db.add(content)
    db.flush()

    timeline = ContentTimeline(
        workspace_id=ws_id, content_item_id=content.id, name="main",
        fps=30.0, duration_seconds=6.0, tracks_json=doc, version=1)
    db.add(timeline)
    db.flush()

    scenes = []
    for i, (start, end, text) in enumerate(
            [(0.0, 3.0, SOURCE_L1), (3.0, 6.0, SOURCE_L2)]):
        scene = Scene(workspace_id=ws_id, content_item_id=content.id,
                      timeline_id=timeline.id, index=i, title=f"scene {i}",
                      narration=text, script_segment=text,
                      start_seconds=start, end_seconds=end)
        db.add(scene)
        scenes.append(scene)
    db.commit()
    return content, timeline, scenes


def _seed_glossary(db, ws_id):
    existing = db.scalar(select(GlossaryTerm).where(
        GlossaryTerm.workspace_id == ws_id, GlossaryTerm.term == "YMONEY"))
    if existing is not None:
        return existing
    row = GlossaryTerm(workspace_id=ws_id, term="YMONEY", replacement="",
                       target_languages=[], kind="brand", case_sensitive=False)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _prepare(db, ws_id, content_id, *, translation_version=1,
             seed_glossary=True):
    if seed_glossary:
        _seed_glossary(db, ws_id)
    return prepare_localizations(
        db, workspace_id=ws_id, source_content_id=content_id,
        target_languages=["es"], locales={"es": "es-ES"},
        translation_version=translation_version)[0]


def _pipeline(db, ws_id, localized_id, *, job_ctx=None, translate_fn=None):
    return LocalizationPipeline(
        db, localized_content_id=localized_id, workspace_id=ws_id,
        job_ctx=job_ctx, translate_fn=translate_fn or fake_translate,
        voice_fn=fake_voice, tts_fn=fake_tts)


def _caption_texts(doc) -> list[str]:
    track = next(t for t in doc["tracks"] if t["kind"] == "caption")
    return [c["text"]["content"] for c in track["clips"]]


def _qc_checks(report) -> dict:
    return {c["name"]: c for c in report.checks_json.get("checks", [])}


def _qc_report(db, localized_id) -> LocalizationQCReport:
    row = db.scalar(select(LocalizationQCReport).where(
        LocalizationQCReport.localized_content_id == localized_id)
        .order_by(LocalizationQCReport.created_at.desc()))
    assert row is not None, "QC report was not persisted"
    return row


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _register(client, email=None):
    email = email or f"loc{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _execute_job(ws_id, localized_ids, cancelled=lambda: False):
    """Run the registered job handler the way a worker would."""
    from app.services import jobs as jobs_service

    handler = jobs_service._handlers["localization.run"]
    ctx = jobs_service.JobContext(
        job_id=f"test-loc-{uuid.uuid4().hex[:6]}", type="localization.run",
        workspace_id=ws_id, cycle_id=None,
        payload={"localized_ids": list(localized_ids)}, attempt=1,
        cancelled=cancelled)
    return handler(ctx)


def _patch_provider_hooks(monkeypatch):
    monkeypatch.setattr(pipeline_mod, "translate_texts", fake_translate)
    monkeypatch.setattr(pipeline_mod, "choose_voice", fake_voice)
    monkeypatch.setattr(pipeline_mod, "synthesize_texts", fake_tts)


# ---------------------------------------------------------------------------
# 1. translation lineage — source preserved, new child, version recorded
# ---------------------------------------------------------------------------


def test_translation_lineage_source_preserved(db_session, workspace_with_user,
                                              storage_root):
    ws = workspace_with_user["workspace"]
    source, src_timeline, src_scenes = _build_source(db_session, ws)
    before = {
        "topic": source.topic,
        "status": source.status,
        "strategy": copy.deepcopy(source.strategy_json),
        "updated_at": source.updated_at,
        "tracks": copy.deepcopy(src_timeline.tracks_json),
    }
    src_scene_ids = [s.id for s in src_scenes]

    row = _prepare(db_session, ws, source.id, translation_version=3)
    result = _pipeline(db_session, ws, row.id).run()

    # --- source untouched -------------------------------------------------
    db_session.refresh(source)
    assert source.topic == before["topic"]
    assert source.status == before["status"]
    assert source.strategy_json == before["strategy"]
    assert source.updated_at == before["updated_at"]
    db_session.refresh(src_timeline)
    assert src_timeline.tracks_json == before["tracks"]
    src_scenes_after = db_session.scalars(select(Scene).where(
        Scene.content_item_id == source.id)).all()
    assert sorted(s.id for s in src_scenes_after) == sorted(src_scene_ids)
    assert db_session.scalars(select(ContentTimeline).where(
        ContentTimeline.content_item_id == source.id)).all().__len__() == 1

    # --- new child + new timeline ----------------------------------------
    assert row.status == "READY" and row.error == ""
    child = db_session.get(ContentItem, row.child_content_id)
    assert child is not None and child.id != source.id
    assert child.derivation_type == "localized"
    assert child.parent_content_id == source.id
    assert child.root_content_id == (source.root_content_id or source.id)
    assert child.workspace_id == ws

    out_timeline = db_session.get(ContentTimeline, row.timeline_id)
    assert out_timeline is not None
    assert out_timeline.content_item_id == child.id
    assert out_timeline.workspace_id == ws
    assert out_timeline.id != src_timeline.id

    # --- lineage / version recorded --------------------------------------
    assert row.language == "es"
    assert row.locale == "es-ES"
    assert row.translation_version == 3
    lineage = dict(row.lineage_json)
    assert lineage["source_content_id"] == source.id
    assert lineage["child_content_id"] == child.id
    assert lineage["timeline_id"] == row.timeline_id
    assert lineage["root_content_id"] == (source.root_content_id or source.id)
    assert lineage["language"] == "es"
    assert lineage["translation_version"] == 3
    assert lineage["source_language"] == "en"
    assert [s["stage"] for s in lineage["stages"]] == list(pipeline_mod.STAGES)
    assert child.strategy_json["localization"] == {
        "source_content_id": source.id,
        "language": "es",
        "locale": "es-ES",
        "translation_version": 3,
        "localized_content_id": row.id,
    }

    # --- timing preserved + drift flagged ---------------------------------
    caption_track = next(t for t in out_timeline.tracks_json["tracks"]
                         if t["kind"] == "caption")
    assert [(c["start"], c["duration"]) for c in caption_track["clips"]] == \
        [(0.0, 3.0), (3.0, 3.0)]
    timing = lineage["timing"]
    assert timing["threshold_seconds"] == pipeline_mod.TIMING_SOFT_DRIFT
    assert isinstance(timing["flagged"], list)
    assert timing["worst_drift_seconds"] != 0.0

    # --- literal vs localized separation ----------------------------------
    literal, localized = lineage["literal_texts"], lineage["localized_texts"]
    assert len(literal) == len(localized) == 2
    assert "follow for more" in literal[0]           # literal keeps source CTA
    assert "sigueme" in localized[0]                 # localized adapts to es
    assert "follow for more" not in localized[0]
    assert _caption_texts(out_timeline.tracks_json) == localized

    # --- stages + QC -------------------------------------------------------
    assert result["status"] == "READY"
    assert [s["stage"] for s in result["stages"]] == list(pipeline_mod.STAGES)
    report = _qc_report(db_session, row.id)
    assert report.workspace_id == ws
    assert report.status in QC_STATUSES
    assert result["qc_status"] == report.status
    checks = _qc_checks(report)
    assert checks["glossary_preserved"]["status"] == "pass"
    assert checks["literals_preserved"]["status"] == "pass"
    assert not [c for c in checks.values() if c["status"] == "fail"]
    # metadata localized on the child, metrics verbatim
    meta = child.strategy_json["localized_metadata"]["strategy"]
    assert meta["title"] != STANDARD_STRATEGY["title"]
    assert "$5,000" in meta["title"]
    assert meta["metrics"] == {"views": 1200, "ctr": 0.04}


# ---------------------------------------------------------------------------
# 2. glossary preservation — brand term survives a mangling translator
# ---------------------------------------------------------------------------


def test_glossary_brand_term_survives_mocked_translation(
        db_session, workspace_with_user, storage_root):
    ws = workspace_with_user["workspace"]
    source, _, _ = _build_source(db_session, ws)
    row = _prepare(db_session, ws, source.id)
    _pipeline(db_session, ws, row.id).run()

    out_timeline = db_session.get(ContentTimeline, row.timeline_id)
    captions = _caption_texts(out_timeline.tracks_json)
    # the mocked translator emitted "Y-Money"; enforcement put the brand back
    assert "YMONEY" in captions[0]
    # cue 2 keeps the source URL (which carries the brand) intact
    assert "https://ymoney.example.com" in captions[1]

    lineage = dict(row.lineage_json)
    repairs = lineage["repairs"]
    assert repairs, "brand term was not repaired"
    brand = next(r for r in repairs if r["term"] == "YMONEY")
    assert brand["action"] in ("reinserted", "replaced")
    assert brand["kind"] == "brand"
    assert brand["cue"] == 1

    report = _qc_report(db_session, row.id)
    checks = _qc_checks(report)
    assert checks["glossary_preserved"]["status"] == "pass"
    assert checks["glossary_repairs"]["status"] == "warn"
    # deterministic repairs downgrade a clean run to warnings, never FAIL
    assert report.status == "PASS_WITH_WARNINGS"

    # voice text keeps the phonetic pronunciation, captions keep the brand
    voice_track = next(t for t in out_timeline.tracks_json["tracks"]
                       if t["kind"] == "voice")
    assert len(voice_track["clips"]) == 2
    assert voice_track["clips"][0]["source"]["lang"] == "es"
    assert voice_track["clips"][0]["source"]["voice"] == "es-neural-1"


def test_glossary_enforcement_and_pronunciation_are_deterministic():
    brand = [{"term": "YMONEY", "replacement": "Y-Money", "kind": "brand",
              "target_languages": [], "case_sensitive": False}]
    # mangled occurrence rewritten in place
    text, events = enforce_glossary("Try YMONEY today", "Prueba YMONEY hoy",
                                    brand, "es")
    assert text == "Prueba Y-Money hoy"
    assert events[0]["action"] == "replaced"

    # vanished brand term re-inserted (flagged as a repair)
    text, events = enforce_glossary("Try YMONEY today", "Prueba y money hoy",
                                    brand, "es")
    assert text.endswith("Y-Money")
    assert events[0]["action"] == "reinserted"

    # terminology term that vanished is reported, never invented
    term = [{"term": "compounding", "replacement": "interés compuesto",
             "kind": "terminology", "target_languages": [],
             "case_sensitive": False}]
    text, events = enforce_glossary("compounding works", "algo distinto",
                                    term, "es")
    assert text == "algo distinto"
    assert events[0]["action"] == "missing"

    # language scoping: a fr-only term never applies to an es run
    fr_only = [{"term": "bonjour", "replacement": "", "kind": "terminology",
                "target_languages": ["fr"], "case_sensitive": False}]
    assert not glossary_applies(fr_only[0], "es")
    text, events = enforce_glossary("say bonjour", "di hola", fr_only, "es")
    assert text == "di hola" and events == []

    # pronunciation shapes TTS audio only; captions keep the real spelling
    phon = [{"term": "Autopilot", "replacement": "aw-toe-pi-lot",
             "kind": "pronunciation", "target_languages": [],
             "case_sensitive": False}]
    src = "Autopilot runs your channel"
    assert apply_pronunciation(src, phon, "es") == \
        "aw-toe-pi-lot runs your channel"
    kept, events = enforce_glossary(src, src, phon, "es")
    assert kept == src and events == []


# ---------------------------------------------------------------------------
# 3. number / name / URL preservation
# ---------------------------------------------------------------------------


def test_number_name_url_preserved_end_to_end(db_session, workspace_with_user,
                                              storage_root):
    ws = workspace_with_user["workspace"]
    source, _, _ = _build_source(db_session, ws)
    row = _prepare(db_session, ws, source.id)
    _pipeline(db_session, ws, row.id).run()

    out_timeline = db_session.get(ContentTimeline, row.timeline_id)
    captions = _caption_texts(out_timeline.tracks_json)
    joined = " ".join(captions)
    assert "20%" in joined
    assert "$5,000" in joined
    assert "90" in joined
    assert "https://ymoney.example.com" in joined
    assert "YMONEY" in joined          # name preserved (via glossary repair)

    report = _qc_report(db_session, row.id)
    checks = _qc_checks(report)
    assert checks["literals_preserved"]["status"] == "pass"
    assert checks["names_preserved"]["status"] == "pass"
    assert checks["cta_meaning"]["status"] == "pass"


def test_quality_flags_dropped_numbers_urls_and_names():
    source_cues = [
        {"index": 1, "start": 0.0, "end": 3.0, "speaker": "s1",
         "text": SOURCE_L2},
    ]
    target_cues = [
        {"index": 1, "start": 0.0, "end": 3.0, "speaker": "s1",
         "text": "Paso dos: paga en el sitio"},
    ]
    res = quality.evaluate(
        workspace_id="ws", language="es", source_cues=source_cues,
        target_cues=target_cues, glossary_entries=[],
        source_speakers=["s1"], target_speakers=["s1"],
        source_duration=3.0, localized_duration=3.0, source_language="en")
    checks = {c["name"]: c for c in res["checks"]}
    assert checks["literals_preserved"]["status"] == "review"
    assert {"5,000", "90"} <= set(checks["literals_preserved"]["numbers"])
    assert checks["literals_preserved"]["urls"] == \
        ["https://ymoney.example.com"]
    # a missing literal is a REVIEW, never a silent PASS
    assert res["status"] == "REVIEW_REQUIRED"

    # names are tracked too
    src = [{"index": 1, "start": 0.0, "end": 2.0, "speaker": "s1",
            "text": "Meet Alice to save 20% today"}]
    tgt = [{"index": 1, "start": 0.0, "end": 2.0, "speaker": "s1",
            "text": "Conoce a Bob para ahorrar 20% hoy"}]
    res2 = quality.evaluate(
        workspace_id="ws", language="es", source_cues=src, target_cues=tgt,
        glossary_entries=[], source_speakers=["s1"], target_speakers=["s1"],
        source_duration=2.0, localized_duration=2.0, source_language="en")
    checks2 = {c["name"]: c for c in res2["checks"]}
    assert checks2["names_preserved"]["status"] == "warn"
    assert checks2["names_preserved"]["names"] == ["Alice"]
    assert checks2["literals_preserved"]["status"] == "pass"

    # clean translation: every deterministic check passes (SHADOW advisory may
    # add a warning but is never authoritative)
    tgt_ok = [{"index": 1, "start": 0.0, "end": 2.0, "speaker": "s1",
               "text": "Conoce a Alice para ahorrar 20% hoy"}]
    res3 = quality.evaluate(
        workspace_id="ws", language="es", source_cues=src, target_cues=tgt_ok,
        glossary_entries=[], source_speakers=["s1"], target_speakers=["s1"],
        source_duration=2.0, localized_duration=2.0, source_language="en")
    deterministic = [c for c in res3["checks"]
                     if c["name"] != "semantic_advisory"]
    assert all(c["status"] == "pass" for c in deterministic), deterministic
    advisory = next(c for c in res3["checks"]
                    if c["name"] == "semantic_advisory")
    assert advisory["authoritative"] is False
    assert advisory["mode"] == "SHADOW"
    assert res3["status"] in ("PASS", "PASS_WITH_WARNINGS")


# ---------------------------------------------------------------------------
# 4. QC statuses: FAIL / REVIEW_REQUIRED / PASS_WITH_WARNINGS / PASS
# ---------------------------------------------------------------------------


def test_qc_status_rollup_and_unsafe_substitutions():
    assert quality.rollup_status([{"status": "pass"}]) == "PASS"
    assert quality.rollup_status([{"status": "warn"},
                                  {"status": "pass"}]) == "PASS_WITH_WARNINGS"
    assert quality.rollup_status([{"status": "review"},
                                  {"status": "warn"}]) == "REVIEW_REQUIRED"
    assert quality.rollup_status([{"status": "fail"},
                                  {"status": "review"}]) == "FAIL"
    assert set(QC_STATUSES) == {"PASS", "PASS_WITH_WARNINGS",
                                "REVIEW_REQUIRED", "FAIL"}

    res = quality.evaluate(
        workspace_id="ws", language="es",
        source_cues=[{"index": 1, "start": 0.0, "end": 3.0, "speaker": "s1",
                      "text": "Our investment grows safely"}],
        target_cues=[{"index": 1, "start": 0.0, "end": 3.0, "speaker": "s1",
                      "text": "get rich quick with your investment"}],
        glossary_entries=[], source_speakers=["s1"], target_speakers=["s1"],
        source_duration=3.0, localized_duration=3.0, source_language="en")
    checks = {c["name"]: c for c in res["checks"]}
    assert checks["unsafe_substitutions"]["status"] == "fail"
    assert "get rich quick" in checks["unsafe_substitutions"]["unsafe"]
    assert checks["unsafe_substitutions"]["banned"], \
        "banned substitution list did not fire"
    assert res["status"] == "FAIL"


def test_pipeline_qc_report_statuses(db_session, workspace_with_user,
                                     storage_root):
    ws = workspace_with_user["workspace"]

    # empty translation -> FAIL (no glossary term can rescue an empty line)
    source, _, _ = _build_source(db_session, ws)
    row = _prepare(db_session, ws, source.id, seed_glossary=False)

    def _drops_second(texts, target_lang, *, workspace_id="", glossary=None):
        out = [PHRASES.get(t) or _pseudo(t) for t in texts]
        out[-1] = ""
        return out

    _pipeline(db_session, ws, row.id, translate_fn=_drops_second).run()
    report = _qc_report(db_session, row.id)
    checks = _qc_checks(report)
    assert checks["segments_non_empty"]["status"] == "fail"
    assert report.status == "FAIL"
    # a QC failure is reported, it does not corrupt the run itself
    assert row.status == "READY"

    # dropped number -> REVIEW_REQUIRED (glossary brand in the URL is intact)
    row2 = _prepare(db_session, ws, source.id)

    def _drops_numbers(texts, target_lang, *, workspace_id="", glossary=None):
        out = [PHRASES.get(t) or _pseudo(t) for t in texts]
        out[-1] = "Paso dos: paga en el sitio https://ymoney.example.com"
        return out

    _pipeline(db_session, ws, row2.id, translate_fn=_drops_numbers).run()
    report2 = _qc_report(db_session, row2.id)
    checks2 = _qc_checks(report2)
    assert checks2["literals_preserved"]["status"] == "review"
    assert report2.status == "REVIEW_REQUIRED"


def test_metadata_localization_never_invents_metrics():
    meta = {"strategy": dict(STANDARD_STRATEGY),
            "variants": [{"title": "How to save $5,000 fast",
                          "views": 77}]}
    localized, changed = localize_metadata(meta, "es", fake_translate)

    assert changed > 0
    strategy = localized["strategy"]
    assert strategy["title"] != meta["strategy"]["title"]
    assert "$5,000" in strategy["title"]              # numbers verbatim
    assert "https://ymoney.example.com" in strategy["description"]
    assert "90" in strategy["description"]
    assert strategy["metrics"] == {"views": 1200, "ctr": 0.04}  # unknown keys
    assert all(h.startswith("#") for h in strategy["hashtags"])
    assert localized["variants"][0]["views"] == 77
    assert "$5,000" in localized["variants"][0]["title"]
    # source dict is never mutated
    assert meta["strategy"]["title"] == "How to save $5,000 fast"


def test_adapt_culture_canonicalizes_cta_without_invention():
    out = adapt_culture("Mira esto y follow for more", "es")
    assert "sigueme" in out
    assert "follow for more" not in out
    # no CTA in the text -> untouched (no invented CTA)
    plain = "Solo un dato: 20%"
    assert adapt_culture(plain, "es") == plain


# ---------------------------------------------------------------------------
# 5. cancellation through app.services.jobs
# ---------------------------------------------------------------------------


def test_localization_run_is_cancellable(db_session, workspace_with_user,
                                         storage_root):
    from app.services import jobs as jobs_service

    ws = workspace_with_user["workspace"]
    source, _, _ = _build_source(db_session, ws)
    row = _prepare(db_session, ws, source.id)
    ctx = jobs_service.JobContext(
        job_id="loc-cancel", type="localization.run", workspace_id=ws,
        cycle_id=None, payload={}, attempt=1, cancelled=lambda: True)
    pipeline = _pipeline(db_session, ws, row.id, job_ctx=ctx)

    with pytest.raises(jobs_service._Cancelled):
        pipeline.run()

    db_session.refresh(row)
    assert row.status == "CANCELLED"
    assert row.timeline_id is None
    lineage = dict(row.lineage_json)
    assert lineage.get("stages") is not None  # partial lineage still recorded


def test_prepare_rejects_unsupported_language(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    source, _, _ = _build_source(db_session, ws)
    with pytest.raises(LocalizationError, match="unsupported target language"):
        prepare_localizations(db_session, workspace_id=ws,
                              source_content_id=source.id,
                              target_languages=["klingon"])
    assert normalize_languages(["ES", "es", "fr"]) == ["es", "fr"]


# ---------------------------------------------------------------------------
# 6. workspace isolation (HTTP): cross-workspace rows are 404
# ---------------------------------------------------------------------------


def test_workspace_localization_isolation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _patch_provider_hooks(monkeypatch)
    ws1, h1 = _register(client)
    ws2, h2 = _register(client)

    from app.db import session_scope

    with session_scope() as s:
        content, _, _ = _build_source(s, ws1)
        content_id = content.id

    # glossary is workspace-owned
    r = client.post(f"/api/v1/workspaces/{ws1}/localization/glossary",
                    headers=h1, json={"term": "YMONEY", "kind": "brand"})
    assert r.status_code == 200, r.text
    term_id = r.json()["id"]

    # ws2 cannot run against ws1's content
    r = client.post(f"/api/v1/workspaces/{ws2}/localization/run", headers=h2,
                    json={"source_content_id": content_id,
                          "target_languages": ["es"]})
    assert r.status_code == 404, r.text

    # a real run in ws1
    r = client.post(f"/api/v1/workspaces/{ws1}/localization/run", headers=h1,
                    json={"source_content_id": content_id,
                          "target_languages": ["es"],
                          "locales": {"es": "es-ES"},
                          "glossary": [{"term": "YMONEY", "kind": "brand"}],
                          "translation_version": 2})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["queued"] and payload["job_id"]
    item = payload["items"][0]
    assert item["language"] == "es" and item["status"] == "PENDING"
    assert item["translation_version"] == 2
    lid = item["id"]

    out = _execute_job(ws1, [lid])
    assert out["ok"], out
    assert out["completed"] == 1 and out["failed"] == []

    # list: own workspace sees it, the other sees nothing
    r = client.get(f"/api/v1/workspaces/{ws1}/localization", headers=h1)
    assert r.status_code == 200 and r.json()["total"] == 1
    r = client.get(f"/api/v1/workspaces/{ws2}/localization", headers=h2)
    assert r.status_code == 200 and r.json()["total"] == 0

    # detail + QC are 404 for the foreign workspace
    r = client.get(f"/api/v1/workspaces/{ws2}/localization/{lid}", headers=h2)
    assert r.status_code == 404, r.text
    r = client.get(f"/api/v1/workspaces/{ws2}/localization/{lid}/qc", headers=h2)
    assert r.status_code == 404, r.text

    # owner still gets detail + QC
    r = client.get(f"/api/v1/workspaces/{ws1}/localization/{lid}", headers=h1)
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["qc"]["status"] in QC_STATUSES
    assert [s["stage"] for s in detail["stages"]] == list(pipeline_mod.STAGES)
    assert detail["lineage"]["source_content_id"] == content_id
    r = client.get(f"/api/v1/workspaces/{ws1}/localization/{lid}/qc", headers=h1)
    assert r.status_code == 200
    assert r.json()["status"] in QC_STATUSES

    # glossary isolation
    r = client.get(f"/api/v1/workspaces/{ws2}/localization/glossary", headers=h2)
    assert r.status_code == 200 and r.json()["total"] == 0
    r = client.get(f"/api/v1/workspaces/{ws1}/localization/glossary", headers=h1)
    assert r.status_code == 200 and r.json()["total"] == 1
    r = client.delete(
        f"/api/v1/workspaces/{ws2}/localization/glossary/{term_id}", headers=h2)
    assert r.status_code == 404, r.text
    r = client.delete(
        f"/api/v1/workspaces/{ws1}/localization/glossary/{term_id}", headers=h1)
    assert r.status_code == 200

    # invalid kind / language rejected up front
    r = client.post(f"/api/v1/workspaces/{ws1}/localization/glossary",
                    headers=h1, json={"term": "x", "kind": "nope"})
    assert r.status_code == 422
    r = client.post(f"/api/v1/workspaces/{ws1}/localization/run", headers=h1,
                    json={"source_content_id": content_id,
                          "target_languages": ["klingon"]})
    assert r.status_code == 422, r.text

    # one FAILED/CANCELLED row must not take the rest of the batch down
    with session_scope() as s:
        bad = s.get(LocalizedContent, lid)
        assert bad is not None and bad.workspace_id == ws1


# ---------------------------------------------------------------------------
# 7. output timeline is editor-compatible
# ---------------------------------------------------------------------------


def test_localized_timeline_passes_editor_api_validation(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _patch_provider_hooks(monkeypatch)
    ws, headers = _register(client)

    from app.db import session_scope

    with session_scope() as s:
        content, src_timeline, _ = _build_source(s, ws)
        content_id = content.id
        src_duration = src_timeline.duration_seconds

    r = client.post(f"/api/v1/workspaces/{ws}/localization/run",
                    headers=headers,
                    json={"source_content_id": content_id,
                          "target_languages": ["es"],
                          "locales": {"es": "es-ES"}})
    assert r.status_code == 200, r.text
    lid = r.json()["items"][0]["id"]

    out = _execute_job(ws, [lid])
    assert out["ok"], out
    assert out["results"][0]["segments"] == 2

    r = client.get(f"/api/v1/workspaces/{ws}/localization/{lid}",
                   headers=headers)
    assert r.status_code == 200, r.text
    detail = r.json()
    timeline_id, child_id = detail["timeline_id"], detail["child_content_id"]
    assert timeline_id and child_id
    assert detail["qc"]["status"] in QC_STATUSES
    assert isinstance(detail["warnings"], list)

    # editor load
    r = client.get(f"/api/v1/workspaces/{ws}/timelines/{timeline_id}",
                   headers=headers)
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["content_item_id"] == child_id
    assert doc["duration_seconds"] >= src_duration
    kinds = {t["kind"] for t in doc["tracks"]}
    assert kinds == set(tl.TRACK_KINDS)          # full editor track set
    tl.validate_timeline({"tracks": doc["tracks"]})  # engine-level validation

    by_kind = {t["kind"]: t for t in doc["tracks"]}
    # visual continuity: picture copied from the source
    assert len(by_kind["video"]["clips"]) == 1
    assert by_kind["video"]["clips"][0]["id"] == "src_1"
    # localized voice + captions on the preserved source windows
    assert len(by_kind["voice"]["clips"]) == 2
    assert [(c["start"], c["duration"]) for c in by_kind["caption"]["clips"]] \
        == [(0.0, 3.0), (3.0, 3.0)]
    assert [(c["start"], c["duration"]) for c in by_kind["voice"]["clips"]] \
        == [(0.0, 3.0), (3.0, 3.0)]
    assert "sigueme" in by_kind["caption"]["clips"][0]["text"]["content"]
    # on-screen text localized, window preserved
    assert len(by_kind["text"]["clips"]) == 1
    gfx = by_kind["text"]["clips"][0]
    assert (gfx["start"], gfx["duration"]) == (0.0, 3.0)
    assert gfx["text"]["content"] != "How to save money"

    # editor save round-trip (validates and persists unchanged tracks)
    r = client.put(f"/api/v1/workspaces/{ws}/timelines/{timeline_id}",
                   headers=headers,
                   json={"tracks": doc["tracks"],
                         "duration_seconds": doc["duration_seconds"]})
    assert r.status_code == 200, r.text
    saved = r.json()
    assert len(saved["tracks"]) == len(doc["tracks"])

    # invalid tracks are rejected, never persisted
    bad = [dict(t) for t in doc["tracks"]]
    bad[0]["clips"] = [
        {"id": "a", "name": "a", "start": 0.0, "duration": 5.0,
         "source": {}, "effects": []},
        {"id": "b", "name": "b", "start": 2.0, "duration": 5.0,
         "source": {}, "effects": []}]
    r = client.put(f"/api/v1/workspaces/{ws}/timelines/{timeline_id}",
                   headers=headers, json={"tracks": bad})
    assert r.status_code == 422, r.text

    # OTIO export of the localized timeline
    r = client.get(f"/api/v1/workspaces/{ws}/timelines/{timeline_id}/otio",
                   headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["OTIO_SCHEMA"] == "Timeline.1"
