"""Speech intelligence: word alignment, diarization, anonymous speakers (Lane B).

Contracts §16 rows owned by this file:

===========================  ==================================================
word alignment               provider doubles + the real persist path
diarization                  provider doubles, gated/unavailable paths
speaker mapping              maximal temporal overlap, gaps stay NULL
no sensitive inference       the response vocabulary itself is asserted
provider unavailable        all three providers report a reason when absent
speech activity is not a     activity rows carry speaker_id = NULL, always
  speaker
cache reuse                  the runs service cache returns the prior run
workspace isolation          a foreign id reads as nothing at all
Work 07 dubbing consumes     a real ``DubbingPlan`` built from the mapping
  the mapping
===========================  ==================================================

The environment under test has ZERO ML packages, so the two model providers can
only ever be exercised through their honest-unavailable path here. The
word/speaker LOGIC is proven with deterministic test doubles injected through the
adapters' own ``backend_loader`` seam -- production construction takes no
arguments, so no fake ships in the app.

``@pytest.mark.slow`` marks the one test that runs the real ffmpeg VAD path over a
real lavfi fixture.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.engine.intel import alignment as engine
from app.engine.intel import registry as intel_registry
from app.engine.intel.base import (
    COMMERCIAL_PERMITTED,
    COMMERCIAL_PROHIBITED,
    COMMERCIAL_REVIEW_REQUIRED,
    COMMERCIAL_UNVERIFIED,
    IntelRequest,
    ProviderResult,
    ProviderUnavailable,
    safe_health,
)
from app.engine.intel.impl import ffmpeg_speech_activity as vad
from app.engine.intel.impl import pyannote_diarization as pyannote
from app.engine.intel.impl import whisperx_alignment as whisperx
from app.models import MediaAsset, MediaIntelRun, User, Workspace, WorkspaceMember
from app.services import media_intel_runs as runs_service

# ---------------------------------------------------------------------------
# deterministic provider doubles (TESTS ONLY -- the app ships no fakes)
# ---------------------------------------------------------------------------

#: two turns, three words; the word at ~6.0 s falls in a gap on purpose
TWO_SPEAKER_TURNS = [
    {"speaker": "A", "start_s": 0.0, "end_s": 3.0},
    {"speaker": "B", "start_s": 3.0, "end_s": 5.0},
    {"speaker": "A", "start_s": 7.0, "end_s": 9.0},
]

FOUR_WORDS = [
    {"word": "hello", "start_s": 0.10, "end_s": 0.40, "confidence": 0.94},
    {"word": "there", "start_s": 0.40, "end_s": 0.90, "confidence": 0.91},
    {"word": "welcome", "start_s": 3.10, "end_s": 3.80, "confidence": 0.88},
    {"word": "back", "start_s": 7.10, "end_s": 7.50, "confidence": 0.85},
]

#: a segment-level ASR answer: real timings, NO word level
SEGMENT_LEVEL_ASR = [
    {"word": "hello there", "start_s": 0.10, "end_s": 0.90},
    {"word": "welcome back", "start_s": 3.10, "end_s": 7.50},
]


class FakeAsrBackend:
    """Word-level (or deliberately segment-level) ASR double."""

    def __init__(self, words, *, words_available: bool = True, reason: str = "") -> None:
        self._words = list(words)
        self._available = bool(words_available)
        self._reason = reason
        self.calls: list[str] = []

    def version(self) -> str:
        return "fake-asr-1"

    def align(self, path: str) -> dict:
        self.calls.append(str(path))
        return {
            "language": "en",
            "words": [dict(row) for row in self._words],
            "words_available": self._available,
            "reason": self._reason,
        }


class FakeDiarizerBackend:
    """Speaker-turn double carrying RAW provider labels."""

    def __init__(self, turns) -> None:
        self._turns = [dict(row) for row in turns]
        self.calls: list[str] = []

    def version(self) -> str:
        return "fake-pyannote-1"

    def diarize(self, path: str) -> list[dict]:
        self.calls.append(str(path))
        return [dict(row) for row in self._turns]


class UnavailableProvider:
    """A provider that is installed but refuses, like a gated model with no token."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def run(self, *_args, **_kwargs):
        raise ProviderUnavailable(self._reason)


def _alignment(backend: FakeAsrBackend):
    return whisperx.WhisperXAlignmentProvider(
        backend_loader=lambda _lang, _params: backend
    )


def _diarizer(backend: FakeDiarizerBackend, model: str = ""):
    """A pyannote provider whose gated-stack gate is satisfied by the double.

    ``unavailable_reason`` is the adapter's own seam: in production it is the
    real probe (no ``pyannote.audio`` package, no HF token). A test double is
    allowed to serve a request without a gated install; the app ships no fake.
    """
    kwargs = {"model": model} if model else {}
    return pyannote.PyannoteDiarizationProvider(
        backend_loader=lambda _model, _token, _params: backend,
        unavailable_reason=lambda _model, _params: "",
        **kwargs
    )


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _ws(db, workspace_with_user) -> Workspace:
    return db.get(Workspace, workspace_with_user["workspace"])


def _asset(db, workspace: Workspace, *, name: str = "source.wav",
           body: bytes = b"not-really-audio") -> MediaAsset:
    """A workspace asset whose storage key really resolves to a local file.

    ``storage_key`` is WORKSPACE-relative (``validate_storage_key`` convention),
    so the bytes live at ``<cwd>/data/videos/<workspace_id>/<name>`` -- the same
    boundary the product enforces, so the provider really receives a path.
    """
    from app.services.storage import STORAGE_ROOT

    path = Path(STORAGE_ROOT) / workspace.id / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    row = MediaAsset(
        workspace_id=workspace.id, type="audio", origin="upload", storage_key=name,
        checksum=f"sum-{uuid.uuid4().hex[:8]}",
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture()
def media_db(db_session, workspace_with_user, tmp_path, monkeypatch):
    """A workspace, an asset backed by a real file, and the speech engine."""
    monkeypatch.chdir(tmp_path)
    ws = _ws(db_session, workspace_with_user)
    asset = _asset(db_session, ws)
    return {"db": db_session, "ws": ws, "asset": asset}


def _align(media_db, backend: FakeAsrBackend, **kwargs):
    return engine.align_words(
        media_db["db"], media_db["ws"], media_db["asset"],
        provider=_alignment(backend), **kwargs
    )


def _diarize(media_db, backend: FakeDiarizerBackend, **kwargs):
    return engine.diarize(
        media_db["db"], media_db["ws"], media_db["asset"],
        provider=_diarizer(backend),
        include_speech_activity=kwargs.pop("include_speech_activity", False),
        **kwargs
    )


def _latest_run_id(db, workspace_id: str) -> str:
    return db.scalars(
        select(MediaIntelRun.id)
        .where(MediaIntelRun.workspace_id == workspace_id)
        .order_by(MediaIntelRun.created_at.desc())
        .limit(1)
    ).first() or ""


# ---------------------------------------------------------------------------
# word alignment (contracts §4)
# ---------------------------------------------------------------------------


def test_align_words_persists_only_genuinely_aligned_words(media_db):
    """Word rows land with their timings; ``speaker_id`` is NULL pre-diarization."""
    backend = FakeAsrBackend(FOUR_WORDS)
    payload = _align(media_db, backend)

    assert payload["words_available"] is True
    assert payload["word_count"] == 4
    assert [w["word"] for w in payload["words"]] == ["hello", "there", "welcome", "back"]
    assert payload["words"][0]["start_s"] == 0.1
    assert payload["words"][0]["end_s"] == 0.4
    assert payload["words"][0]["confidence"] == 0.94
    # NO speaker yet: alignment never invents one
    assert all(w["speaker_id"] is None for w in payload["words"])
    assert payload["run"]["status"] == "COMPLETED"
    assert payload["run"]["kind"] == "alignment"
    # the REAL file reached the provider, not an empty path
    assert len(backend.calls) == 1
    assert backend.calls[0].endswith("source.wav")


def test_segment_level_asr_never_invents_words(media_db):
    """No word timings => ``words_available=false`` + a reason + ZERO rows."""
    backend = FakeAsrBackend(
        SEGMENT_LEVEL_ASR, words_available=False,
        reason="the ASR backend returned segment-level timing only",
    )
    payload = _align(media_db, backend)

    assert payload["words_available"] is False
    assert payload["word_count"] == 0
    assert payload["words"] == []
    assert "segment-level" in payload["reason"]
    assert payload["run"]["status"] == "COMPLETED"
    assert engine.list_words(media_db["db"], media_db["ws"].id) == []


def test_word_normalisation_drops_tokens_without_both_timings():
    """A half-timed token is dropped, never interpolated."""
    words, note = whisperx.normalise_words([
        {"word": "ok", "start_s": 1.0, "end_s": 1.5},
        {"word": "no-end", "start_s": 2.0},
        {"word": "", "start_s": 3.0, "end_s": 3.5},
        {"word": "inverted", "start_s": 5.0, "end_s": 4.0},
    ])
    assert [w["word"] for w in words] == ["ok"]
    assert "3 unaligned" in note


def test_alignment_without_a_provider_is_honestly_unavailable(media_db):
    payload = engine.align_words(
        media_db["db"], media_db["ws"], media_db["asset"], provider=None
    )
    assert payload["run"]["status"] == "UNAVAILABLE"
    assert payload["words_available"] is False
    assert payload["run"]["error_code"] == "PROVIDER_UNAVAILABLE"
    assert payload["reason"].strip()


def test_alignment_without_a_readable_media_file_is_honestly_unavailable(
    db_session, workspace_with_user, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    ws = _ws(db_session, workspace_with_user)
    ghost = MediaAsset(workspace_id=ws.id, type="audio", storage_key="missing.wav",
                       checksum="sum-ghost")
    db_session.add(ghost)
    db_session.flush()
    payload = engine.align_words(
        db_session, ws, ghost, provider=_alignment(FakeAsrBackend(FOUR_WORDS))
    )
    assert payload["run"]["status"] == "UNAVAILABLE"
    assert "storage" in payload["reason"]


# ---------------------------------------------------------------------------
# diarization + anonymous speaker identity (contracts §4/§5)
# ---------------------------------------------------------------------------


def test_diarization_stores_speaker_segments_with_anonymous_ids(media_db):
    payload = _diarize(media_db, FakeDiarizerBackend(TWO_SPEAKER_TURNS))

    assert payload["speakers_resolved"] is True
    assert payload["speakers"] == ["SPEAKER_00", "SPEAKER_01"]
    speakers = [s for s in payload["segments"] if s["kind"] == "SPEAKER"]
    assert len(speakers) == 3
    assert [s["speaker_id"] for s in speakers] == [
        "SPEAKER_00", "SPEAKER_01", "SPEAKER_00"
    ]


def test_raw_provider_label_never_reaches_the_dto(media_db):
    """``A``/``B`` are provider keys; only ``SPEAKER_nn`` may be observable."""
    payload = _diarize(media_db, FakeDiarizerBackend(TWO_SPEAKER_TURNS))
    blob = repr(payload)
    assert "SPEAKER_00" in blob
    assert "'A'" not in blob and "'B'" not in blob


def test_speaker_ids_are_assigned_by_first_appearance(media_db):
    """Whichever label speaks first becomes ``SPEAKER_00`` (contracts §5)."""
    turns = [
        {"speaker": "Z", "start_s": 0.0, "end_s": 1.0},
        {"speaker": "A", "start_s": 1.0, "end_s": 2.0},
        {"speaker": "Z", "start_s": 2.0, "end_s": 3.0},
    ]
    rows, mapping = engine.assign_speaker_ids(turns)
    assert mapping == {"Z": "SPEAKER_00", "A": "SPEAKER_01"}
    assert [r["speaker_id"] for r in rows] == [
        "SPEAKER_00", "SPEAKER_01", "SPEAKER_00"
    ]


def test_speaker_ids_are_stable_across_a_resume():
    """A retried run keeps the SAME id for the same raw label."""
    first, mapping = engine.assign_speaker_ids(TWO_SPEAKER_TURNS)
    again, _mapping = engine.merge_speaker_ids(mapping, TWO_SPEAKER_TURNS)
    assert [r["speaker_id"] for r in again] == [r["speaker_id"] for r in first]


def test_anonymous_speaker_id_shape_is_enforced():
    assert engine.anonymous_speaker_id(0) == "SPEAKER_00"
    assert engine.anonymous_speaker_id(7) == "SPEAKER_07"
    assert engine.is_anonymous_speaker_id("SPEAKER_00") is True
    for bad in ("Host", "speaker_00", "SPEAKER_0", "SPEAKER_00 x", "", None, 0):
        assert engine.is_anonymous_speaker_id(bad) is False
    with pytest.raises(engine.SpeechAlignmentError):
        engine.anonymous_speaker_id(-1)


def test_diarization_unavailable_leaves_every_speaker_null(media_db):
    """Gated/absent diarizer => no SPEAKER rows and an honest reason."""
    payload = engine.diarize(
        media_db["db"], media_db["ws"], media_db["asset"],
        provider=UnavailableProvider(pyannote.NO_TOKEN_REASON),
        include_speech_activity=False,
    )
    assert payload["run"]["status"] == "UNAVAILABLE"
    assert payload["speakers_resolved"] is False
    assert payload["segments"] == []
    assert "token" in payload["reason"]


# ---------------------------------------------------------------------------
# speech activity is NOT a speaker (contracts §4)
# ---------------------------------------------------------------------------


def test_speech_activity_rows_are_stored_without_a_speaker(media_db):
    """A VAD measurement is activity, never a dressed-up speaker."""
    db, ws, asset = media_db["db"], media_db["ws"], media_db["asset"]
    payload = _diarize(media_db, FakeDiarizerBackend(TWO_SPEAKER_TURNS))

    written = engine.store_activity_segments(
        db, ws, payload["run_id"], asset,
        [{"start_s": 0.0, "end_s": 3.0}, {"start_s": 5.0, "end_s": 8.0}],
    )
    assert written == 2
    rows = engine.list_segments(db, ws.id, run_id=payload["run_id"],
                                kind=engine.ACTIVITY_KIND)
    assert len(rows) == 2
    assert all(row["speaker_id"] is None for row in rows)
    assert all(row["kind"] == "SPEECH_ACTIVITY" for row in rows)
    # a zero-length activity run is not a row
    assert engine.store_activity_segments(
        db, ws, payload["run_id"], asset, [{"start_s": 2.0, "end_s": 2.0}]
    ) == 0


def test_the_vad_provider_seam_produces_activity_and_never_a_speaker(tmp_path):
    """Injected detector + duration probe: deterministic, activity-only."""
    provider = vad.FFmpegSpeechActivityProvider(
        silence_detector=lambda *_a, **_k: [{"start_s": 3.0, "end_s": 5.0}],
        duration_probe=lambda _p: 8.0,
    )
    result = provider.run(
        IntelRequest(workspace_id="w", asset_id="a", storage_path="x.wav"),
        progress=lambda _pct: None, should_cancel=lambda: False, deadline=None,
    )
    payload = result.artifacts["segments"]["payload"]
    assert payload["kind"] == "SPEECH_ACTIVITY"
    assert payload["segments"] == [
        {"start_s": 0.0, "end_s": 3.0}, {"start_s": 5.0, "end_s": 8.0}
    ]
    assert result.metrics["silence_count"] == 1
    assert result.metrics["speech_ratio"] == pytest.approx(0.75, abs=1e-3)
    assert provider.capabilities()["produces_speakers"] is False


def test_speech_activity_never_fills_a_words_speaker():
    """Activity rows are invisible to the word->speaker mapping."""
    words = [{"idx": 0, "word": "hi", "start_s": 0.5, "end_s": 1.0}]
    activity = [
        {"kind": "SPEECH_ACTIVITY", "speaker_id": None, "start_s": 0.0, "end_s": 9.0},
    ]
    mapped = engine.map_words_to_speakers(words, activity)
    assert mapped[0]["speaker_id"] is None
    assert mapped[0]["speaker_reason"] == "no_speaker_segment"


def test_speech_activity_complement_is_the_inverse_of_measured_silence():
    """Silence runs in, speech runs out; ``start_s == 0`` is a normal boundary."""
    silences = [{"start_s": 3.0, "end_s": 5.0}]
    assert vad.speech_activity_from_silences(silences, 8.0) == [
        {"start_s": 0.0, "end_s": 3.0},
        {"start_s": 5.0, "end_s": 8.0},
    ]
    # a media START that is silent emits no silence_start: 0 is just a boundary
    assert vad.speech_activity_from_silences([{"start_s": 0.5, "end_s": 2.0}], 5.0) == [
        {"start_s": 0.0, "end_s": 0.5},
        {"start_s": 2.0, "end_s": 5.0},
    ]
    # no duration => no invented tail
    assert vad.speech_activity_from_silences(silences, None) == []
    # everything silent => nothing to report
    assert vad.speech_activity_from_silences([{"start_s": 0.0, "end_s": 8.0}], 8.0) == []


def test_speech_activity_merges_touching_silences():
    merged = vad.clamp_silences(
        [{"start_s": 1.0, "end_s": 2.0}, {"start_s": 2.0, "end_s": 3.0},
         {"start_s": 1.5, "end_s": 2.5}, {"start_s": 5.0, "end_s": 4.0}],
        10.0,
    )
    assert merged == [{"start_s": 1.0, "end_s": 3.0}]


@pytest.mark.slow
def test_real_ffmpeg_speech_activity_over_a_real_fixture(tmp_path):
    """Real lavfi media + real ``silencedetect`` (ground truth: silence 3.0-5.0)."""
    from tests.media_intel_fixtures import ffmpeg_filter_ok, patterned_speechlike_wav

    assert ffmpeg_filter_ok(vad.MEASUREMENT_FILTER), "this slow test needs ffmpeg"
    wav = patterned_speechlike_wav(tmp_path / "speechlike.wav")
    provider = vad.FFmpegSpeechActivityProvider()
    result = provider.run(
        IntelRequest(workspace_id="w", asset_id="a", storage_path=str(wav)),
        progress=lambda _pct: None, should_cancel=lambda: False, deadline=None,
    )
    segments = result.artifacts["segments"]["payload"]["segments"]
    assert [round(s["start_s"], 3) for s in segments] == [0.0, 5.0]
    assert round(segments[-1]["end_s"], 3) == 8.0
    assert result.metrics["silence_count"] == 1
    assert result.artifacts["segments"]["payload"]["kind"] == "SPEECH_ACTIVITY"


# ---------------------------------------------------------------------------
# word -> speaker mapping (contracts §4)
# ---------------------------------------------------------------------------


SPEAKER_SEGMENTS = [
    {"kind": "SPEAKER", "speaker_id": "SPEAKER_00", "start_s": 0.0, "end_s": 3.0},
    {"kind": "SPEAKER", "speaker_id": "SPEAKER_01", "start_s": 3.0, "end_s": 5.0},
    {"kind": "SPEAKER", "speaker_id": "SPEAKER_00", "start_s": 7.0, "end_s": 9.0},
]


def test_words_map_to_the_segment_they_overlap_most():
    words = [
        {"word": "a", "start_s": 0.5, "end_s": 1.0},
        {"word": "b", "start_s": 3.5, "end_s": 4.0},
    ]
    mapped = engine.map_words_to_speakers(words, SPEAKER_SEGMENTS)
    assert [m["speaker_id"] for m in mapped] == ["SPEAKER_00", "SPEAKER_01"]
    assert mapped[0]["speaker_overlap_s"] == 0.5
    assert mapped[0]["speaker_reason"] == "max_overlap"


def test_a_word_taking_the_max_overlap_wins():
    """70 % in SPEAKER_00 / 30 % in SPEAKER_01 -> SPEAKER_00, deterministically."""
    words = [{"word": "x", "start_s": 2.3, "end_s": 3.3}]
    mapped = engine.map_words_to_speakers(words, SPEAKER_SEGMENTS)
    assert mapped[0]["speaker_id"] == "SPEAKER_00"
    assert mapped[0]["speaker_overlap_s"] == pytest.approx(0.7)


def test_words_in_a_gap_keep_a_null_speaker():
    """A word between two speakers is NOT nearest-guessed across the gap."""
    words = [
        {"idx": 0, "word": "hello", "start_s": 0.10, "end_s": 0.40},
        {"idx": 1, "word": "there", "start_s": 0.40, "end_s": 0.90},
        {"idx": 2, "word": "welcome", "start_s": 3.10, "end_s": 3.80},
        {"idx": 3, "word": "back", "start_s": 7.10, "end_s": 7.50},
    ]
    mapped = engine.map_words_to_speakers(words, SPEAKER_SEGMENTS)
    assert [m["speaker_id"] for m in mapped] == [
        "SPEAKER_00", "SPEAKER_00", "SPEAKER_01", "SPEAKER_00"
    ]
    # the 5.0-7.0 gap: the nearest segment is 1.5 s away on either side
    gap = engine.map_words_to_speakers(
        [{"word": "hmm", "start_s": 5.5, "end_s": 6.0}], SPEAKER_SEGMENTS
    )
    assert gap[0]["speaker_id"] is None
    assert gap[0]["speaker_reason"] == "no_speaker_segment"
    assert gap[0]["speaker_overlap_s"] == 0.0


def test_a_minimum_overlap_threshold_refuses_a_sliver():
    words = [{"word": "edge", "start_s": 2.95, "end_s": 3.20}]
    segments = [
        {"kind": "SPEAKER", "speaker_id": "SPEAKER_00", "start_s": 0.0, "end_s": 3.0},
    ]
    assert engine.map_words_to_speakers(words, segments)[0]["speaker_id"] == "SPEAKER_00"
    strict = engine.map_words_to_speakers(words, segments, min_overlap_s=0.5)
    assert strict[0]["speaker_id"] is None


def test_mapping_ignores_a_segment_whose_speaker_is_not_anonymous():
    words = [{"word": "x", "start_s": 1.0, "end_s": 2.0}]
    segments = [{"kind": "SPEAKER", "speaker_id": "Host", "start_s": 0.0, "end_s": 3.0}]
    assert engine.map_words_to_speakers(words, segments)[0]["speaker_id"] is None


def test_mapping_never_mutates_its_input():
    words = [{"word": "x", "start_s": 1.0, "end_s": 2.0}]
    engine.map_words_to_speakers(words, SPEAKER_SEGMENTS)
    assert words == [{"word": "x", "start_s": 1.0, "end_s": 2.0}]


def test_end_to_end_word_to_speaker_persisted_in_one_run(media_db):
    """Align + diarize + map: the contract pipeline, proven on stored rows."""
    db, ws = media_db["db"], media_db["ws"]
    alignment = _align(media_db, FakeAsrBackend(FOUR_WORDS))
    diarization = _diarize(media_db, FakeDiarizerBackend(TWO_SPEAKER_TURNS))

    words = engine.list_words(db, ws.id, run_id=alignment["run_id"])
    segments = engine.list_segments(db, ws.id, run_id=diarization["run_id"])
    assert len(words) == 4 and len(segments) == 3
    mapped = engine.map_words_to_speakers(words, segments)
    assert [m["speaker_id"] for m in mapped] == [
        "SPEAKER_00", "SPEAKER_00", "SPEAKER_01", "SPEAKER_00"
    ]


# ---------------------------------------------------------------------------
# no sensitive inference (contracts §0/§5)
# ---------------------------------------------------------------------------

#: no key of any speech DTO may contain one of these fragments
FORBIDDEN_VOCABULARY = (
    "gender", "sex", "race", "ethnic", "age", "birth", "identity", "biometric",
    "voiceprint", "demographic", "religion", "nationality", "male", "female",
)


def _keys(payload) -> set[str]:
    """Every key at any depth of a JSON-ish payload."""
    found: set[str] = set()
    stack = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                found.add(str(key).lower())
                stack.append(value)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return found


def test_speech_dto_vocabulary_contains_no_sensitive_field(media_db):
    """The SHAPE forbids a demographic field -- there is nowhere to put one."""
    db, ws = media_db["db"], media_db["ws"]
    _align(media_db, FakeAsrBackend(FOUR_WORDS))
    _diarize(media_db, FakeDiarizerBackend(TWO_SPEAKER_TURNS))
    run = runs_service.get_run(db, ws.id, _latest_run_id(db, ws.id))
    assert run is not None

    payloads = [
        engine.run_result(db, ws.id, run),
        engine.list_speakers(db, ws.id),
        engine.list_segments(db, ws.id),
        engine.list_words(db, ws.id),
        engine.list_aliases(db, ws.id),
    ]
    checked = 0
    for payload in payloads:
        for key in _keys(payload):
            checked += 1
            for bad in FORBIDDEN_VOCABULARY:
                assert bad not in key, f"sensitive vocabulary leaked: {key!r}"
    assert checked > 40, "the payload walker must actually have seen the DTO keys"


def test_the_models_module_has_no_demographic_column():
    """The tables themselves are structural proof, not just the DTO."""
    forbidden = {
        "gender", "sex", "race", "ethnicity", "age", "birth_date", "identity",
        "biometric", "voiceprint", "demographic", "religion", "nationality",
    }
    from app.models.media_intel import DiarizationSegment, MediaIntelWord, SpeakerAlias

    for model in (DiarizationSegment, MediaIntelWord, SpeakerAlias):
        columns = {str(c.name).lower() for c in model.__table__.columns}
        assert not (columns & forbidden), (model.__name__, columns & forbidden)


def test_only_operator_labels_name_a_speaker(media_db):
    """An unnamed speaker is anonymous; a named one carries the operator's text."""
    db, ws = media_db["db"], media_db["ws"]
    _diarize(media_db, FakeDiarizerBackend(TWO_SPEAKER_TURNS))

    before = engine.list_speakers(db, ws.id)
    assert [s["speaker_id"] for s in before] == ["SPEAKER_00", "SPEAKER_01"]
    assert all(s["label"] == "" and s["named"] is False for s in before)

    created = engine.create_alias(db, ws, speaker_id="SPEAKER_00", label="Host")
    assert created["label"] == "Host"
    after = engine.list_speakers(db, ws.id)
    assert after[0]["label"] == "Host" and after[0]["named"] is True
    assert after[1]["label"] == ""  # the other speaker stays anonymous


def test_alias_rejects_anything_that_is_not_an_anonymous_id(media_db):
    db, ws = media_db["db"], media_db["ws"]
    for bad in ("Host", "SPEAKER_0", "speaker_00", "SPEAKER_00x", "1"):
        with pytest.raises(engine.SpeechAlignmentError):
            engine.create_alias(db, ws, speaker_id=bad, label="x")
    with pytest.raises(engine.SpeechAlignmentError):
        engine.create_alias(db, ws, speaker_id="SPEAKER_00", label="   ")
    with pytest.raises(engine.SpeechAlignmentError):
        engine.create_alias(db, ws, speaker_id="SPEAKER_00", label="x" * 121)
    assert engine.list_aliases(db, ws.id) == []


def test_alias_records_its_author_and_upserts(media_db, workspace_with_user):
    db, ws = media_db["db"], media_db["ws"]
    user_id = str(workspace_with_user["user"])
    created = engine.create_alias(
        db, ws, speaker_id="SPEAKER_00", label="Guest", created_by=user_id
    )
    assert created["created_by"] == user_id
    # a rename upserts the same row, so no stale duplicate survives
    again = engine.create_alias(db, ws, speaker_id="SPEAKER_00", label="Co-host")
    assert again["id"] == created["id"]
    assert again["label"] == "Co-host"
    assert len(engine.list_aliases(db, ws.id)) == 1


def test_alias_scopes_prefer_the_most_specific_match(media_db):
    db, ws = media_db["db"], media_db["ws"]
    payload = _diarize(media_db, FakeDiarizerBackend(TWO_SPEAKER_TURNS))
    run_id = payload["run_id"]
    engine.create_alias(db, ws, speaker_id="SPEAKER_00", label="Global")
    scoped = engine.create_alias(db, ws, speaker_id="SPEAKER_00", label="PerRun",
                                 run_id=run_id)
    assert scoped["run_id"] == run_id
    assert engine.alias_map(db, ws.id) == {"SPEAKER_00": "PerRun"}
    assert engine.alias_map(db, ws.id, run_id="other-run") == {"SPEAKER_00": "Global"}


def test_delete_alias_is_workspace_scoped(media_db, db_session, tmp_path, monkeypatch):
    db, ws = media_db["db"], media_db["ws"]
    created = engine.create_alias(db, ws, speaker_id="SPEAKER_00", label="Host")

    other = Workspace(name="Other", slug=f"o-{uuid.uuid4().hex[:6]}", niche="AI money")
    db_session.add(other)
    db_session.flush()
    user = User(email=f"x{uuid.uuid4().hex[:6]}@test.local", password_hash="x")
    db_session.add(user)
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=other.id, user_id=user.id,
                                   role=WorkspaceMember.ROLE_OWNER))
    db_session.flush()

    assert engine.delete_alias(db, other.id, created["id"]) is False
    assert engine.delete_alias(db, ws.id, created["id"]) is True
    assert engine.list_aliases(db, ws.id) == []


# ---------------------------------------------------------------------------
# provider unavailable / license honesty (contracts §1.3/§1.4)
# ---------------------------------------------------------------------------


def test_model_providers_report_unavailable_with_a_reason():
    """Zero ML packages => whisperX + pyannote are dark, and say why."""
    for key in ("whisperx_alignment", "pyannote_diarization"):
        intel_registry.clear_cache()
        provider = intel_registry.get_provider(key)
        health = safe_health(provider)
        assert health.available is False, key
        assert health.reason.strip(), key
        assert health.detail.get("remediation"), key
        request = IntelRequest(workspace_id="w", asset_id="a", storage_path="x.wav")
        with pytest.raises(ProviderUnavailable):
            provider.run(request, progress=lambda _p: None,
                         should_cancel=lambda: False, deadline=None)


def test_pyannote_without_a_token_is_unavailable(monkeypatch):
    """Gated weights + no token => honest dark, never a fabricated speaker."""
    for name in pyannote.TOKEN_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert pyannote.resolve_token() == ""
    provider = pyannote.PyannoteDiarizationProvider()
    health = safe_health(provider)
    assert health.available is False
    assert health.reason.strip()
    assert health.detail["token_present"] is False
    if pyannote._missing_backend() is not None:
        assert health.detail["missing_module"]
    else:  # a host with the stack: the token is then the only blocker
        assert "token" in health.reason.lower()
    with pytest.raises(ProviderUnavailable):
        provider.run(
            IntelRequest(workspace_id="w", asset_id="a", storage_path="x.wav"),
            progress=lambda _p: None, should_cancel=lambda: False, deadline=None,
        )


def test_pyannote_with_a_token_but_no_stack_still_reports_the_missing_module(monkeypatch):
    monkeypatch.setenv("PYANNOTE_TOKEN", "hf_not_a_real_token")
    health = safe_health(pyannote.PyannoteDiarizationProvider())
    assert health.available is False
    assert health.detail["token_present"] is True
    assert health.reason.strip()


def test_ffmpeg_speech_activity_capability_probe_is_honest():
    provider = vad.FFmpegSpeechActivityProvider()
    health = safe_health(provider)
    assert health.detail["produces_speakers"] is False
    assert health.detail["segment_kind"] == "SPEECH_ACTIVITY"
    if health.available:
        assert health.detail["silencedetect"] is True
        assert provider.capabilities()["speaker_id"] is None
    else:
        assert health.reason.strip()


def test_whisperx_per_language_clearance_matches_the_audit():
    assert whisperx.ALIGNER_LICENSES["en"]["commercial_use"] == COMMERCIAL_PERMITTED
    for code in ("fr", "de", "es", "it"):
        entry = whisperx.ALIGNER_LICENSES[code]
        assert entry["model_license"] == "CC-BY-NC-4.0"
        assert entry["commercial_use"] == COMMERCIAL_PROHIBITED
    unknown = whisperx.language_clearance("ja")
    assert unknown["commercial_use"] == COMMERCIAL_UNVERIFIED
    assert unknown["known_audited"] is False
    assert whisperx.normalise_language("pt-BR") == "ptbr"


def test_whisperx_provider_verdict_is_review_required_not_permitted():
    info = whisperx.WhisperXAlignmentProvider().license_info()
    assert info.code_license == "BSD-2-Clause"
    assert info.commercial_use == COMMERCIAL_REVIEW_REQUIRED
    assert info.audited_on == "2026-09-29"
    assert "CC BY-NC 4.0" in info.notes
    assert "VAD" in info.notes


def test_commercial_mode_refuses_only_the_non_commercial_languages():
    report = whisperx.commercial_clearance(commercial_mode=True)
    blocked = {row["language"]: row["commercial_use"] for row in report["blocked"]}
    assert blocked == {
        "de": COMMERCIAL_PROHIBITED, "es": COMMERCIAL_PROHIBITED,
        "fr": COMMERCIAL_PROHIBITED, "it": COMMERCIAL_PROHIBITED,
    }
    assert "en" in report["allowed"]
    assert report["per_language"], "the per-language verdicts must be reported"
    # outside commercial mode nothing is blocked, but the verdicts are still shown
    lenient = whisperx.commercial_clearance(commercial_mode=False)
    assert lenient["blocked"] == []
    assert {row["commercial_use"] for row in lenient["per_language"]} == {
        COMMERCIAL_PERMITTED, COMMERCIAL_PROHIBITED
    }


def test_commercial_mode_refuses_a_prohibited_language_before_loading_a_model():
    provider = whisperx.WhisperXAlignmentProvider(
        backend_loader=lambda _lang, _params: pytest.fail("backend must not load")
    )
    with pytest.raises(ProviderUnavailable) as excinfo:
        provider.run(
            IntelRequest(workspace_id="w", asset_id="a", storage_path="x.wav",
                         params={"language": "fr", "commercial_mode": True}),
            progress=lambda _p: None, should_cancel=lambda: False, deadline=None,
        )
    assert "fr" in str(excinfo.value)
    assert COMMERCIAL_PROHIBITED in str(excinfo.value)


def test_an_unaudited_language_is_refused_in_commercial_mode():
    provider = whisperx.WhisperXAlignmentProvider(
        backend_loader=lambda _lang, _params: pytest.fail("backend must not load")
    )
    with pytest.raises(ProviderUnavailable) as excinfo:
        provider.run(
            IntelRequest(workspace_id="w", asset_id="a", storage_path="x.wav",
                         params={"language": "ja", "commercial_mode": True}),
            progress=lambda _p: None, should_cancel=lambda: False, deadline=None,
        )
    assert COMMERCIAL_UNVERIFIED in str(excinfo.value)


def test_pyannote_license_reports_gating_and_attribution():
    info = pyannote.PyannoteDiarizationProvider().license_info()
    assert info.code_license == "MIT"
    assert info.model_gated is True
    assert info.commercial_use == COMMERCIAL_REVIEW_REQUIRED
    assert "CC-BY-4.0" in info.notes
    assert "gated" in info.notes.lower()
    assert "pyannote/speaker-diarization-community-1" in pyannote.ATTRIBUTION_REQUIRED
    assert pyannote.model_license_for("pyannote/nope")["model_license"] == "UNVERIFIED"


def test_pyannote_turn_normalisation_never_invents_a_speaker():
    turns, dropped = pyannote.normalise_turns([
        {"speaker": "A", "start_s": 0.0, "end_s": 1.0},
        {"speaker": "", "start_s": 1.0, "end_s": 2.0},
        {"speaker": "B", "start_s": 2.0, "end_s": 2.0},
    ])
    assert turns == [{"speaker": "A", "start_s": 0.0, "end_s": 1.0, "confidence": None}]
    assert dropped == 2


def test_ffmpeg_speech_activity_license_is_review_required():
    info = vad.FFmpegSpeechActivityProvider().license_info()
    assert info.commercial_use == COMMERCIAL_REVIEW_REQUIRED
    assert info.model_gated is False
    assert "no model" in info.model_license
    assert "distribution" in info.notes


def test_speech_providers_are_registered_in_their_chains():
    assert intel_registry.chain_for("alignment") == ("whisperx_alignment",)
    assert intel_registry.chain_for("diarization") == ("pyannote_diarization",)
    assert intel_registry.chain_for("speech_activity") == ("ffmpeg_speech_activity",)
    for key in ("whisperx_alignment", "pyannote_diarization", "ffmpeg_speech_activity"):
        payload = intel_registry.get_provider(key).to_dict()
        assert payload["license"]["commercial_use"] in {
            COMMERCIAL_PERMITTED, COMMERCIAL_REVIEW_REQUIRED,
            COMMERCIAL_PROHIBITED, COMMERCIAL_UNVERIFIED,
        }
        assert payload["health"]["reason"] or payload["health"]["available"]


# ---------------------------------------------------------------------------
# cache reuse (contracts §3, through the runs service)
# ---------------------------------------------------------------------------


def test_second_alignment_is_a_cache_hit_and_recomputes_nothing(media_db):
    backend = FakeAsrBackend(FOUR_WORDS)
    first = _align(media_db, backend)
    assert first["cache_hit"] is False
    assert len(backend.calls) == 1

    second = _align(media_db, backend)
    assert second["cache_hit"] is True
    assert second["run_id"] == first["run_id"]
    assert second["word_count"] == 4
    assert len(backend.calls) == 1, "a cache hit must not touch the provider"

    forced = _align(media_db, backend, force=True)
    assert forced["cache_hit"] is False
    assert forced["run_id"] != first["run_id"]
    assert len(backend.calls) == 2


def test_diarization_cache_hit_returns_the_stored_segments(media_db):
    backend = FakeDiarizerBackend(TWO_SPEAKER_TURNS)
    first = _diarize(media_db, backend)
    second = _diarize(media_db, backend)
    assert second["cache_hit"] is True
    assert second["run_id"] == first["run_id"]
    assert second["speakers"] == first["speakers"]
    assert len(backend.calls) == 1


# ---------------------------------------------------------------------------
# workspace isolation (contracts §14)
# ---------------------------------------------------------------------------


def _other_workspace(db) -> Workspace:
    other = Workspace(name="Other", slug=f"o-{uuid.uuid4().hex[:6]}", niche="AI money")
    user = User(email=f"x{uuid.uuid4().hex[:6]}@test.local", password_hash="x")
    db.add_all([other, user])
    db.flush()
    db.add(WorkspaceMember(workspace_id=other.id, user_id=user.id,
                           role=WorkspaceMember.ROLE_OWNER))
    db.flush()
    return other


def test_words_segments_and_speakers_are_scoped_to_their_workspace(media_db):
    db, ws, asset = media_db["db"], media_db["ws"], media_db["asset"]
    alignment = _align(media_db, FakeAsrBackend(FOUR_WORDS))
    _diarize(media_db, FakeDiarizerBackend(TWO_SPEAKER_TURNS))
    other = _other_workspace(db)

    assert engine.list_words(db, other.id) == []
    assert engine.list_segments(db, other.id) == []
    assert engine.list_speakers(db, other.id) == []
    assert engine.list_aliases(db, other.id) == []
    assert engine.get_run(db, other.id, alignment["run_id"]) is None
    # the owner's own view is unchanged by the foreign workspace existing
    assert len(engine.list_words(db, ws.id)) == 4
    assert asset.workspace_id == ws.id


# ---------------------------------------------------------------------------
# Work 07 dubbing bridge (contracts §5 / §21)
# ---------------------------------------------------------------------------


def _mapped_words():
    words = [
        {"idx": 0, "word": "hello", "start_s": 0.10, "end_s": 0.40},
        {"idx": 1, "word": "there", "start_s": 0.40, "end_s": 0.90},
        {"idx": 2, "word": "welcome", "start_s": 3.10, "end_s": 3.80},
        {"idx": 3, "word": "back", "start_s": 7.10, "end_s": 7.50},
    ]
    return engine.map_words_to_speakers(words, SPEAKER_SEGMENTS)


def test_dubbing_cues_feed_a_real_dubbing_plan():
    """The Work 07 bridge: a real ``DubbingPlan`` built straight from the mapping."""
    from app.engine.dubbing.plan import build_plan

    labels = {"SPEAKER_00": "Host", "SPEAKER_01": "Guest"}
    cues = engine.to_dubbing_cues(_mapped_words(), labels=labels)

    assert [c["speaker_id"] for c in cues] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]
    assert cues[0]["text"] == "hello there"
    assert cues[0]["speaker"] == "Host"  # the operator label rides along as a hint
    assert cues[1]["speaker"] == "Guest"
    assert len(cues[0]["words"]) == 2
    assert cues[0]["words"][0]["start_s"] == 0.1

    voice_map = engine.dubbing_voice_map(
        cues, target_voices={"SPEAKER_00": "es-HostNeural", "SPEAKER_01": "es-GuestNeural"}
    )
    plan = build_plan(cues, "es", voice_map, source_ref="run:demo")

    assert plan.target_language == "es"
    assert plan.source_ref == "run:demo"
    assert [s.speaker_id for s in plan.speakers] == ["SPEAKER_00", "SPEAKER_01"]
    assert [s.target_voice for s in plan.speakers] == ["es-HostNeural", "es-GuestNeural"]
    assert [s.index for s in plan.segments] == [1, 2, 3]
    assert [s.speaker_id for s in plan.segments] == [
        "SPEAKER_00", "SPEAKER_01", "SPEAKER_00"
    ]
    assert plan.speaker("SPEAKER_00").language == "es"
    assert plan.needs_review is False
    # an operator NAME is never turned into a voice
    assert "Host" not in {s.target_voice for s in plan.speakers}
    assert "Guest" not in {s.target_voice for s in plan.speakers}


def test_dubbing_cue_text_survives_a_real_round_trip(media_db):
    """The persisted mapping (not a hand-built list) builds the plan."""
    from app.engine.dubbing.plan import build_plan

    db, ws = media_db["db"], media_db["ws"]
    alignment = _align(media_db, FakeAsrBackend(FOUR_WORDS))
    diarization = _diarize(media_db, FakeDiarizerBackend(TWO_SPEAKER_TURNS))
    engine.create_alias(db, ws, speaker_id="SPEAKER_00", label="Host")
    engine.create_alias(db, ws, speaker_id="SPEAKER_01", label="Guest")

    words = engine.list_words(db, ws.id, run_id=alignment["run_id"])
    segments = engine.list_segments(db, ws.id, run_id=diarization["run_id"])
    labels = engine.alias_map(db, ws.id)
    cues = engine.to_dubbing_cues(
        engine.map_words_to_speakers(words, segments), labels=labels
    )
    plan = build_plan(cues, "es", engine.dubbing_voice_map(cues), source_ref=alignment["run_id"])

    assert [c["text"] for c in cues] == ["hello there", "welcome", "back"]
    assert [s.speaker_id for s in plan.speakers] == ["SPEAKER_00", "SPEAKER_01"]
    assert plan.needs_review is True  # no voice chosen yet -> REVIEW, never invented
    assert all(seg.voice_review for seg in plan.segments)


def test_dubbing_voice_map_keeps_an_unmapped_speaker_unmapped():
    cues = engine.to_dubbing_cues(_mapped_words(), labels={"SPEAKER_00": "Host"})
    voice_map = engine.dubbing_voice_map(cues)
    assert voice_map["SPEAKER_00"] == {
        "label": "Host", "source_voice": "", "target_voice": "",
    }
    assert voice_map["SPEAKER_01"]["target_voice"] == ""


def test_unlabelled_cues_fall_back_to_the_single_speaker_default():
    """No diarization => no speaker on any word => the honest Work 07 default."""
    from app.engine.dubbing.plan import DEFAULT_SINGLE_SPEAKER, build_plan

    words = [
        {"idx": 0, "word": "hello", "start_s": 0.10, "end_s": 0.40},
        {"idx": 1, "word": "there", "start_s": 0.40, "end_s": 0.90},
    ]
    mapped = engine.map_words_to_speakers(words, [])
    assert all(m["speaker_id"] is None for m in mapped)
    # same (unresolved) speaker and no gap => ONE cue, which is the honest shape
    cues = engine.to_dubbing_cues(mapped)
    assert [c["speaker_id"] for c in cues] == [""]
    assert cues[0]["text"] == "hello there"
    plan = build_plan(cues, "es")
    assert [s.speaker_id for s in plan.speakers] == [DEFAULT_SINGLE_SPEAKER]
    assert all(seg.voice_review for seg in plan.segments)


def test_a_long_gap_breaks_the_dubbing_cue():
    words = [
        {"word": "a", "start_s": 0.0, "end_s": 0.5, "speaker_id": "SPEAKER_00"},
        {"word": "b", "start_s": 9.0, "end_s": 9.5, "speaker_id": "SPEAKER_00"},
    ]
    assert len(engine.to_dubbing_cues(words, max_gap_s=0.6)) == 2
    assert len(engine.to_dubbing_cues(words, max_gap_s=20.0)) == 1


# ---------------------------------------------------------------------------
# provider result contract
# ---------------------------------------------------------------------------


def test_provider_result_shape_is_what_the_engine_reads():
    """A hand-built result round-trips through the engine's payload reader."""
    result = ProviderResult(
        ok=True,
        artifacts={"words": {"payload": {
            "words": [{"word": "hi", "start_s": 0.0, "end_s": 0.4}],
            "words_available": True, "reason": "",
        }}},
        metrics={"word_count": 1}, warnings=[],
    )
    assert engine._payload_of(result, "words")["words_available"] is True
    assert engine._payload_of(result, "segments") == {}
    assert engine._result_warnings(result) == []


# ---------------------------------------------------------------------------
# HTTP routes (contracts §14)
# ---------------------------------------------------------------------------


def _register(client, email=None):
    email = email or f"sp{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _client(tmp_path, monkeypatch):
    """The real app plus THIS lane's router (it is mounted at integration).

    ``api_router`` carries the ``/api/v1`` prefix, so the router is included on
    the app with the same prefix here -- the path the orchestrator's
    ``api/v1/__init__.py`` edit will produce.
    """
    from starlette.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.api.v1.media_intel_speech import media_intel_speech_router
    from app.main import create_app

    app = create_app()
    app.include_router(media_intel_speech_router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


def _http_asset(client, workspace_id: str, headers: dict, name: str = "clip.wav"):
    """Register a real asset whose bytes live inside the workspace storage root."""
    from app.services.storage import STORAGE_ROOT

    path = Path(STORAGE_ROOT) / workspace_id / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not-really-audio")
    r = client.post(
        f"/api/v1/workspaces/{workspace_id}/assets/media",
        headers=headers,
        json={"type": "audio", "origin": "upload", "storage_key": name},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_alignment_route_reports_unavailable_without_a_provider(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    asset_id = _http_asset(client, ws_id, headers)

    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/alignments",
                    headers=headers, json={"asset_id": asset_id, "language": "en"})
    assert r.status_code == 200, r.text
    body = r.json()
    # the capability is dark in CI: honest, terminal, with a reason -- never a fake success
    assert body["run"]["status"] == "UNAVAILABLE"
    assert body["words_available"] is False
    assert body["word_count"] == 0
    assert body["reason"].strip()
    assert body["events"] == []

    detail = client.get(f"/api/v1/workspaces/{ws_id}/media-intel/alignments/"
                        f"{body['run_id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["run"]["id"] == body["run_id"]


def test_alignment_route_stores_words_through_the_engine(tmp_path, monkeypatch):
    """An injected provider makes the whole route path real, end to end."""
    from app.engine.intel import alignment as engine_module

    backend = FakeAsrBackend(FOUR_WORDS)
    monkeypatch.setattr(
        engine_module, "_resolve",
        lambda kind, **_kw: (_alignment(backend)
                             if kind == engine_module.ALIGNMENT_KIND else None, {}),
    )
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    asset_id = _http_asset(client, ws_id, headers)

    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/alignments",
                    headers=headers, json={"asset_id": asset_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run"]["status"] == "COMPLETED"
    assert body["words_available"] is True
    assert body["word_count"] == 4

    words = client.get(f"/api/v1/workspaces/{ws_id}/media-intel/words",
                       headers=headers)
    assert words.status_code == 200
    assert words.json()["count"] == 4
    assert words.json()["items"][0]["word"] == "hello"


def test_diarization_route_reports_unavailable_without_a_provider(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    asset_id = _http_asset(client, ws_id, headers)

    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/diarization",
                    headers=headers, json={"asset_id": asset_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run"]["kind"] == "diarization"
    assert body["run"]["status"] in {"UNAVAILABLE", "COMPLETED"}
    if body["run"]["status"] == "UNAVAILABLE":
        assert body["speakers_resolved"] is False
        assert body["reason"].strip()
    detail = client.get(f"/api/v1/workspaces/{ws_id}/media-intel/diarization/"
                        f"{body['run_id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["speakers"] == body["speakers"]


def test_speaker_alias_crud_over_http(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    asset_id = _http_asset(client, ws_id, headers)
    base = f"/api/v1/workspaces/{ws_id}/media-intel"

    created = client.post(f"{base}/speakers/aliases", headers=headers,
                          json={"speaker_id": "SPEAKER_00", "label": "Host",
                                "asset_id": asset_id})
    assert created.status_code == 200, created.text
    alias = created.json()
    assert alias["speaker_id"] == "SPEAKER_00" and alias["label"] == "Host"
    assert alias["created_by"], "the operator is recorded"

    listed = client.get(f"{base}/speakers/aliases", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["items"][0]["id"] == alias["id"]

    speakers = client.get(f"{base}/speakers", headers=headers,
                          params={"asset_id": asset_id})
    assert speakers.status_code == 200
    assert speakers.json()["items"] == []
    assert speakers.json()["anonymous"] is True

    deleted = client.delete(f"{base}/speakers/aliases/{alias['id']}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": True, "id": alias["id"],
                              "speaker_id": "SPEAKER_00"}
    assert client.get(f"{base}/speakers/aliases", headers=headers).json()["count"] == 0


def test_a_free_form_name_cannot_be_stored_as_a_speaker(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    r = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/speakers/aliases",
                    headers=headers, json={"speaker_id": "Host", "label": "Host"})
    assert r.status_code == 422, r.text
    assert "SPEAKER_00" in r.json()["detail"]


def test_foreign_ids_are_404_never_403(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_a, headers_a = _register(client)
    ws_b, headers_b = _register(client)
    asset_a = _http_asset(client, ws_a, headers_a, "a.wav")

    created = client.post(f"/api/v1/workspaces/{ws_a}/media-intel/speakers/aliases",
                          headers=headers_a,
                          json={"speaker_id": "SPEAKER_00", "label": "Host"})
    alias_id = created.json()["id"]

    # the OTHER workspace's token, on the OTHER workspace's prefix
    assert client.get(f"/api/v1/workspaces/{ws_b}/media-intel/words",
                      headers=headers_b).status_code == 200
    assert client.get(f"/api/v1/workspaces/{ws_b}/media-intel/words",
                      headers=headers_b, params={"asset_id": asset_a}).status_code == 404
    assert client.get(f"/api/v1/workspaces/{ws_b}/media-intel/speakers/aliases",
                      headers=headers_b).json()["count"] == 0
    assert client.delete(
        f"/api/v1/workspaces/{ws_b}/media-intel/speakers/aliases/{alias_id}",
        headers=headers_b,
    ).status_code == 404
    assert client.post(f"/api/v1/workspaces/{ws_b}/media-intel/alignments",
                       headers=headers_b, json={"asset_id": asset_a}).status_code == 404
    assert client.get(f"/api/v1/workspaces/{ws_b}/media-intel/alignments/does-not-exist",
                      headers=headers_b).status_code == 404


def test_route_payloads_carry_no_sensitive_vocabulary(tmp_path, monkeypatch):
    """The HTTP shape is asserted too, not just the engine dicts."""
    from app.engine.intel import alignment as engine_module

    backend = FakeAsrBackend(FOUR_WORDS)
    monkeypatch.setattr(
        engine_module, "_resolve",
        lambda kind, **_kw: (_alignment(backend)
                             if kind == engine_module.ALIGNMENT_KIND else None, {}),
    )
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    asset_id = _http_asset(client, ws_id, headers)
    base = f"/api/v1/workspaces/{ws_id}/media-intel"

    aligned = client.post(f"{base}/alignments", headers=headers,
                          json={"asset_id": asset_id}).json()
    client.post(f"{base}/speakers/aliases", headers=headers,
                json={"speaker_id": "SPEAKER_00", "label": "Host",
                      "asset_id": asset_id})
    payloads = [
        aligned,
        client.get(f"{base}/alignments/{aligned['run_id']}", headers=headers).json(),
        client.get(f"{base}/words", headers=headers).json(),
        client.get(f"{base}/speakers", headers=headers,
                   params={"asset_id": asset_id}).json(),
        client.get(f"{base}/speakers/aliases", headers=headers).json(),
    ]
    for payload in payloads:
        for key in _keys(payload):
            for bad in FORBIDDEN_VOCABULARY:
                assert bad not in key, f"sensitive vocabulary in an HTTP key: {key!r}"
    # the only human name anywhere is the operator's own label
    assert "Host" in repr(payloads)

