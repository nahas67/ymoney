"""Segmentation / mask storage / background composition gates (Work 12 Lane F).

Contracts §16 rows owned by this file: **segmentation masks** (contracts §9) plus
the §12 background tools. The environment under test has NO ML packages, so the
production path is asserted through its honest-unavailable verdict while the
ENGINE is exercised end to end with a deterministic backend double -- the same
code path ``impl/sam2_segmentation.py`` drives.

What is locked here:

* a mask is a FILE: the ``mask_assets`` row carries a reference + geometry +
  checksum and provably NO raw mask bytes, while the referenced file exists with
  a matching sha256;
* the engine is provider-independent (two differently-shaped doubles drive it);
* every composition reports ``unavailable`` with a reason when no backend exists,
  and writes a NEW derived asset with full lineage when one does -- the original
  bytes are byte-identical before and after;
* masks never cross a workspace boundary;
* the four routes answer honestly (UNAVAILABLE run, not a fake success) and
  404 a foreign id.

Fast tests use synthetic frame descriptors (the engine hands paths to the
backend and never reads pixels itself); real-ffmpeg media is ``@pytest.mark.slow``.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.engine.intel import registry as intel_registry
from app.engine.intel import segmentation as seg
from app.engine.intel.impl import sam2_segmentation as adapter
from tests import media_intel_fixtures as fx

FRAME_W, FRAME_H = 128, 64
#: a 48x32 rectangle inside 128x64 -> exactly 0.1875 of the frame
BOX = (16, 8, 48, 32)


# ---------------------------------------------------------------------------
# backend doubles (tests only -- the production path stays honestly unavailable)
# ---------------------------------------------------------------------------


class FixedBoxBackend(seg.MaskBackend):
    """One rectangle per requested label. Never touches the pixels."""

    key = "double_fixed_box"
    model_version = "double-1"

    def __init__(self, rect=BOX, *, available=True, reason="") -> None:
        self.rect = rect
        self.available = bool(available)
        self.reason = reason
        self.calls = 0

    def health(self) -> tuple[bool, str]:
        return (self.available, self.reason if not self.available else "")

    def masks_for_frame(self, frame, *, labels, progress=None, should_cancel=None,
                        deadline=None) -> list[seg.RegionMask]:
        self.calls += 1
        x, y, w, h = self.rect
        return [
            seg.mask_from_runs(
                seg.rect_runs(x, y, w, h, frame.width, frame.height),
                label=label,
                frame=frame,
                score=0.91,
            )
            for label in labels
        ]


class SplitLabelsBackend(FixedBoxBackend):
    """A DIFFERENT backend shape: one PERSON region + one smaller OBJECT region."""

    key = "double_split_labels"
    model_version = "double-2"

    def masks_for_frame(self, frame, *, labels, progress=None, should_cancel=None,
                        deadline=None) -> list[seg.RegionMask]:
        self.calls += 1
        person = seg.mask_from_runs(
            seg.rect_runs(*BOX, frame.width, frame.height),
            label="PERSON", frame=frame, score=0.88,
        )
        obj = seg.mask_from_runs(
            seg.rect_runs(96, 40, 16, 16, frame.width, frame.height),
            label="OBJECT", frame=frame, score=0.55,
        )
        return [person, obj]


def _frames(count: int = 2) -> list[seg.MaskFrame]:
    return [
        seg.MaskFrame(index=i, t_s=round(i / 2.0, 4), path=f"frame_{i}.png",
                      width=FRAME_W, height=FRAME_H)
        for i in range(count)
    ]


def _run_segment(workspace_id, backend, *, frames=None, fmt="PNG", params=None):
    return seg.segment(
        workspace_id=workspace_id,
        storage_path="unused-by-the-double.mp4",
        backend=backend,
        frames=list(frames if frames is not None else _frames()),
        params={"format": fmt, **dict(params or {})},
        run_id="run-test",
    )


# ---------------------------------------------------------------------------
# engine: provider-independent, honest about availability
# ---------------------------------------------------------------------------


def test_two_different_backends_drive_the_same_engine(tmp_path, monkeypatch):
    """Different pixel producers, one engine: records share a shape."""
    monkeypatch.chdir(tmp_path)
    ws = "ws-engine"

    rect_backend = FixedBoxBackend()
    split_backend = SplitLabelsBackend()
    rect = _run_segment(ws, rect_backend, frames=_frames(1))
    split = _run_segment(ws, split_backend, frames=_frames(1))

    for outcome in (rect, split):
        assert outcome["ok"] is True, outcome
        assert outcome["unavailable"] is False
        assert outcome["masks"], outcome
        for record in outcome["masks"]:
            assert record.storage_key.startswith(seg.MASK_DIR)
            assert record.width == FRAME_W and record.height == FRAME_H
            assert record.checksum and len(record.checksum) == 64
            assert record.file_size > 0
            assert seg.resolve_mask_path(ws, record.storage_key).exists()
        assert outcome["metrics"]["provider_key"] == outcome["masks"][0].provider_key

    assert [r.label for r in rect["masks"]] == ["PERSON"]
    assert sorted(r.label for r in split["masks"]) == ["OBJECT", "PERSON"]
    assert rect_backend.calls == 1 and split_backend.calls == 1
    assert rect["metrics"]["provider_key"] != split["metrics"]["provider_key"]


def test_area_ratio_is_measured_from_the_runs_not_supplied():
    frame = seg.MaskFrame(index=0, t_s=0.0, path="f.png", width=FRAME_W, height=FRAME_H)
    mask = seg.mask_from_runs(seg.rect_runs(*BOX, FRAME_W, FRAME_H),
                              label="PERSON", frame=frame, score=1.0)
    assert mask.area == 48 * 32
    assert mask.area_ratio == pytest.approx(48 * 32 / (FRAME_W * FRAME_H), abs=1e-6)
    assert mask.bbox == (16.0, 8.0, 48.0, 32.0)


def test_out_of_bounds_runs_are_rejected_not_clipped():
    frame = seg.MaskFrame(index=0, t_s=0.0, path="f.png", width=16, height=16)
    with pytest.raises(seg.SegmentationError):
        seg.mask_from_runs([(0, 999)], label="PERSON", frame=frame, width=16, height=16)


def test_unknown_label_and_format_are_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(seg.SegmentationError):
        _run_segment("ws-x", FixedBoxBackend(), params={"labels": ["DOG"]})
    with pytest.raises(seg.SegmentationError):
        _run_segment("ws-x", FixedBoxBackend(), fmt="TIFF")


def test_engine_is_unavailable_with_a_reason_when_no_backend(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    intel_registry.clear_cache()
    # nothing installed in this venv, so the registry resolves nothing
    provider, reasons = intel_registry.resolve("segmentation")
    assert provider is None
    assert reasons, "an unresolvable capability must carry a reason"

    backend, reason = seg.resolve_backend(None)
    assert backend is None and reason.strip()

    outcome = seg.segment(workspace_id="ws-x", storage_path="nope.mp4", frames=_frames(1))
    assert outcome["ok"] is False
    assert outcome["unavailable"] is True
    assert outcome["reason"].strip()
    assert outcome["masks"] == []


def test_a_backend_that_reports_unavailable_is_not_used(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dead = FixedBoxBackend(available=False, reason="torch not installed (ModuleNotFoundError)")
    outcome = _run_segment("ws-dead", dead)
    assert outcome["ok"] is False
    assert outcome["unavailable"] is True
    assert "torch" in outcome["reason"]
    assert dead.calls == 0, "an unavailable backend must not be asked for masks"


# ---------------------------------------------------------------------------
# mask serialisation
# ---------------------------------------------------------------------------


def test_both_mask_formats_are_files_with_stable_bytes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    frame = seg.MaskFrame(index=3, t_s=1.5, path="f.png", width=FRAME_W, height=FRAME_H)
    mask = seg.mask_from_runs(seg.rect_runs(*BOX, FRAME_W, FRAME_H),
                              label="PERSON", frame=frame, score=0.5)

    png = seg.encode_mask(mask, "PNG")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert png == seg.encode_mask(mask, "PNG"), "PNG bytes must be deterministic"
    # IHDR carries the true geometry right after the signature
    assert int.from_bytes(png[16:20], "big") == FRAME_W
    assert int.from_bytes(png[20:24], "big") == FRAME_H

    payload = json.loads(seg.encode_mask(mask, "RLE_JSON"))
    assert payload["width"] == FRAME_W and payload["height"] == FRAME_H
    assert payload["runs"] == [[start, length] for start, length in mask.runs]
    assert payload["bbox"] == [16.0, 8.0, 48.0, 32.0]


def test_mask_from_binary_reads_a_provider_grid():
    frame = seg.MaskFrame(index=0, t_s=0.0, path="f.png", width=4, height=2)
    grid = [[0, 1, 1, 0], [0, 0, 1, 0]]
    mask = seg.mask_from_binary(grid, label="PERSON", frame=frame)
    assert mask.runs == ((1, 2), (6, 1))
    assert mask.area == 3
    assert mask.area_ratio == pytest.approx(3 / 8)


def test_mask_file_key_must_stay_inside_the_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    frame = seg.MaskFrame(index=0, t_s=0.0, path="f.png", width=FRAME_W, height=FRAME_H)
    mask = seg.mask_from_runs(seg.rect_runs(*BOX, FRAME_W, FRAME_H),
                              label="PERSON", frame=frame)
    with pytest.raises(seg.SegmentationError):
        seg.write_mask(mask, workspace_id="", fmt="PNG")


# ---------------------------------------------------------------------------
# mask STORAGE proof: the row is a reference, the file is the mask
# ---------------------------------------------------------------------------


def test_mask_row_holds_a_reference_and_the_file_matches_the_checksum(
    db_session, workspace_with_user, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    ws = workspace_with_user["workspace"]
    outcome = _run_segment(ws, FixedBoxBackend(), frames=_frames(2))
    assert outcome["ok"] is True

    asset_id, run_id = _seed_run(db_session, ws)
    items = seg.persist_masks(
        db_session, workspace_id=ws, run_id=run_id, input_asset_id=asset_id,
        records=outcome["masks"],
    )
    db_session.commit()
    assert len(items) == len(outcome["masks"]) == 2

    from app.models import MaskAsset, MediaAsset

    rows = db_session.query(MaskAsset).filter(MaskAsset.run_id == run_id).all()
    assert len(rows) == 2
    for row, record in zip(rows, outcome["masks"], strict=False):
        # -- the row is a REFERENCE + geometry + checksum, never pixels ------
        for column in MaskAsset.__table__.columns:
            value = getattr(row, column.name)
            assert not isinstance(value, (bytes, bytearray, memoryview)), column.name
            if isinstance(value, str):
                assert len(value) <= 128, (column.name, len(value))
        assert row.mask_asset_id, "mask_assets must reference the written file"
        assert row.format == "PNG"
        assert (row.width, row.height) == (FRAME_W, FRAME_H)
        assert row.provider_key == "double_fixed_box"
        assert row.model_version == "double-1"
        assert row.checksum == record.checksum and len(row.checksum) == 64
        assert 0.0 < float(row.area_ratio) <= 1.0

        # -- the FILE exists and its sha256 is exactly the stored checksum ---
        file_asset = db_session.get(MediaAsset, row.mask_asset_id)
        path = seg.resolve_mask_path(ws, file_asset.storage_key)
        assert path is not None and path.exists()
        assert path.stat().st_size > 0
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == row.checksum
        assert int(file_asset.file_size) == path.stat().st_size

        dto = seg.mask_row_dto(row)
        assert set(dto) == {
            "id", "run_id", "workspace_id", "input_asset_id", "kind", "mask_asset_id",
            "format", "width", "height", "area_ratio", "provider_key",
            "model_version", "checksum",
        }


def test_mask_storage_is_workspace_scoped(db_session, workspace_with_user,
                                          tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ws = workspace_with_user["workspace"]
    other = "ws-someone-else"
    outcome = _run_segment(ws, FixedBoxBackend(), frames=_frames(1))
    record = outcome["masks"][0]

    mine = seg.resolve_mask_path(ws, record.storage_key)
    assert mine is not None and mine.exists()
    assert str(mine).replace("\\", "/").endswith(f"{ws}/{record.storage_key}")
    # another workspace can only ever reach ITS OWN directory with that key
    theirs = seg.resolve_mask_path(other, record.storage_key)
    assert theirs is not None
    assert str(theirs).replace("\\", "/").endswith(f"/{other}/{record.storage_key}")
    assert not theirs.exists(), "one workspace's key must not read another's file"
    # traversal / absolute / empty keys are refused outright
    assert seg.resolve_mask_path(ws, "../escape/mask.png") is None
    assert seg.resolve_mask_path(ws, f"C:/abs/{ws}/x.png") is None
    assert seg.resolve_mask_path(ws, "") is None
    assert seg.resolve_mask_path("", record.storage_key) is None


def _seed_run(db_session, workspace_id: str, asset=None) -> tuple[str, str]:
    """A real asset + real run so every FK in the mask rows resolves."""
    from app.models import MediaAsset
    from app.services import media_intel_runs as runs

    if asset is None:
        asset = MediaAsset(workspace_id=workspace_id, type="video", origin="upload",
                           storage_key="in.mp4", checksum="sum-src")
        db_session.add(asset)
        db_session.flush()
    dto = runs.create_run(db_session, workspace_id, kind="segmentation", asset=asset,
                          provider_key="double_fixed_box", model_version="double-1",
                          params={"format": "PNG", "fps": 2.0})
    run = runs.get_run(db_session, workspace_id, dto["id"])
    db_session.commit()
    return asset.id, run.id


# ---------------------------------------------------------------------------
# the real adapter: honest unavailability + the one PERMITTED license
# ---------------------------------------------------------------------------


def test_sam2_health_is_unavailable_in_ci_with_a_reason():
    from app.engine.intel.base import safe_health

    provider = adapter.Sam2SegmentationProvider()
    health = provider.health()          # never raises, by contract
    assert health.available is False
    assert health.reason.strip(), "an unavailable verdict without a reason is a lie"
    assert health.detail.get("remediation")
    # the registry's defensive wrapper agrees
    assert safe_health(provider).available is False


def test_sam2_is_the_only_fully_cleared_backend():
    """Apache-2.0 code AND ungated Apache-2.0 checkpoints -> PERMITTED."""
    info = adapter.Sam2SegmentationProvider().license_info()
    assert info.code_license == "Apache-2.0"
    assert info.code_license_url.startswith("https://")
    assert info.model_license == "Apache-2.0"
    assert info.model_gated is False
    assert info.commercial_use == "PERMITTED"
    assert info.audited_on == "2026-09-29"
    assert "docs/oss/MEDIA_INTEL_LICENSES.md" in info.notes

    listing = adapter.Sam2SegmentationProvider().to_dict()
    assert listing["license"]["commercial_use"] == "PERMITTED"
    assert listing["resources"]["gpu"] is True


def test_sam2_module_imports_without_torch_or_sam2():
    """contracts §0: the adapter is importable with zero ML packages."""
    import importlib.util

    for name in ("torch", "sam2"):
        assert importlib.util.find_spec(name) is None, f"{name} unexpectedly installed"
    assert adapter.PROVIDER is adapter.Sam2SegmentationProvider
    assert adapter.PROVIDER.kind == "segmentation"
    assert adapter.PROVIDER.key == "sam2_segmentation"


def test_segmentation_capability_is_dark_in_ci():
    intel_registry.clear_cache()
    provider, reasons = intel_registry.resolve("segmentation", commercial_mode=True)
    assert provider is None
    assert any("sam2" in key for key in reasons), reasons


# ---------------------------------------------------------------------------
# composition: honest unavailable + derived-only lineage (fast part)
# ---------------------------------------------------------------------------


def test_every_composition_is_unavailable_without_a_backend(db_session,
                                                            workspace_with_user,
                                                            tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ws = workspace_with_user["workspace"]
    asset_id, _run_id = _seed_run(db_session, ws)

    outcomes = [
        seg.background_blur(db_session, workspace_id=ws, asset_id=asset_id),
        seg.background_replace(db_session, workspace_id=ws, asset_id=asset_id,
                               background_path="bg.png"),
        seg.subject_crop(db_session, workspace_id=ws, asset_id=asset_id),
    ]
    for outcome in outcomes:
        payload = outcome.to_dict()
        assert payload["ok"] is False, payload
        assert payload["unavailable"] is True, payload
        assert payload["reason"].strip(), payload
        assert payload["asset"] is None and payload["mask"] is None
    # nothing was written for THIS workspace: no derived asset row
    from app.models import MediaAsset

    derived = db_session.query(MediaAsset).filter(
        MediaAsset.workspace_id == ws, MediaAsset.parent_asset_id.isnot(None)
    ).all()
    assert derived == []


def test_composition_refuses_a_foreign_asset(db_session, workspace_with_user,
                                             tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ws = workspace_with_user["workspace"]
    foreign_ws = "ws-not-mine"
    asset_id, _run_id = _seed_run(db_session, ws)

    outcome = seg.background_blur(
        db_session, workspace_id=foreign_ws, asset_id=asset_id, backend=FixedBoxBackend()
    )
    assert outcome.ok is False
    assert outcome.unavailable is False, "a missing asset is not an unavailable capability"
    assert "not found" in outcome.reason


def test_subject_crop_rejects_a_malformed_aspect(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    asset_id, _run_id = _seed_run(db_session, ws)
    with pytest.raises(seg.SegmentationError):
        seg.subject_crop(db_session, workspace_id=ws, asset_id=asset_id,
                         aspect="tall", backend=FixedBoxBackend())


def test_tracked_overlay_needs_geometry(db_session, workspace_with_user, tmp_path,
                                        monkeypatch):
    monkeypatch.chdir(tmp_path)
    ws = workspace_with_user["workspace"]
    asset_id, _run_id = _seed_run(db_session, ws)
    outcome = seg.tracked_overlay(db_session, workspace_id=ws, asset_id=asset_id, boxes=[])
    assert outcome.ok is False
    assert "geometry" in outcome.reason


# ---------------------------------------------------------------------------
# routes (contracts §14)
# ---------------------------------------------------------------------------


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.api.v1.media_intel_visual import visual_router
    from app.main import create_app

    app = create_app()
    paths = {getattr(r, "path", "") for r in app.routes}
    if not any(p.endswith("/media-intel/masks") for p in paths):
        # not mounted yet (the orchestrator mounts it at integration); mounting it
        # on the real app keeps auth/rbac/db identical to production
        app.include_router(visual_router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


def _register(client):
    import uuid

    email = f"seg{uuid.uuid4().hex[:8]}@test.local"
    response = client.post("/api/v1/auth/register",
                           json={"email": email, "password": "supersecret123"})
    assert response.status_code == 200, response.text
    data = response.json()
    return data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _asset(client, ws_id, headers, key="in.mp4") -> str:
    response = client.post(
        f"/api/v1/workspaces/{ws_id}/assets/media",
        json={"type": "video", "origin": "upload", "storage_key": key,
              "mime_type": "video/mp4"},
        headers=headers,
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def test_post_masks_reports_unavailable_instead_of_faking_success(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    asset_id = _asset(client, ws_id, headers)

    response = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/masks",
                           json={"asset_id": asset_id}, headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["run"]["status"] == "UNAVAILABLE"
    assert payload["unavailable"] is True
    assert payload["reason"].strip()
    assert payload["items"] == []
    assert payload["provider_reasons"], "the capability must say why it is dark"

    listing = client.get(f"/api/v1/workspaces/{ws_id}/media-intel/masks/{payload['run']['id']}",
                         headers=headers)
    assert listing.status_code == 200
    assert listing.json()["items"] == []


def test_mask_route_404s_a_foreign_run_and_asset(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_a, headers_a = _register(client)
    ws_b, headers_b = _register(client)
    asset_b = _asset(client, ws_b, headers_b)

    # a run created in workspace B is invisible from workspace A
    run_b = client.post(f"/api/v1/workspaces/{ws_b}/media-intel/masks",
                        json={"asset_id": asset_b}, headers=headers_b).json()["run"]
    assert run_b["status"] == "UNAVAILABLE"
    assert client.get(f"/api/v1/workspaces/{ws_a}/media-intel/masks/{run_b['id']}",
                      headers=headers_a).status_code == 404
    assert client.get(f"/api/v1/workspaces/{ws_b}/media-intel/masks/{run_b['id']}",
                      headers=headers_b).status_code == 200

    # a non-member cannot even address workspace B
    denied = client.post(f"/api/v1/workspaces/{ws_b}/media-intel/masks",
                         json={"asset_id": asset_b}, headers=headers_a)
    assert denied.status_code in (403, 404), denied.text

    # a run of another kind is not a mask run
    asset_a = _asset(client, ws_a, headers_a)
    created = client.post(f"/api/v1/workspaces/{ws_a}/media-intel/masks",
                          json={"asset_id": asset_a}, headers=headers_a).json()
    assert created["run"]["kind"] == "segmentation"
    other = client.post(f"/api/v1/workspaces/{ws_a}/media-intel/active-speaker",
                        json={"asset_id": asset_a}, headers=headers_a).json()
    assert other["run"]["kind"] == "active_speaker"
    assert client.get(f"/api/v1/workspaces/{ws_a}/media-intel/masks/{other['run']['id']}",
                      headers=headers_a).status_code == 404
    assert client.get(f"/api/v1/workspaces/{ws_a}/media-intel/masks/does-not-exist",
                      headers=headers_a).status_code == 404


def test_mask_route_validates_its_body(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    asset_id = _asset(client, ws_id, headers)
    bad = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/masks",
                      json={"asset_id": asset_id, "mask_format": "TIFF"},
                      headers=headers)
    assert bad.status_code == 422, bad.text


def test_active_speaker_route_answers_unresolved_without_guessing(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    asset_id = _asset(client, ws_id, headers)

    response = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/active-speaker",
                           json={"asset_id": asset_id}, headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["run"]["status"] == "COMPLETED"
    assert payload["items"]
    for item in payload["items"]:
        assert item["status"] == "UNRESOLVED"
        assert item["reason"] in {
            "no_diarization", "no_face_track", "ambiguous_tie", "low_overlap",
            "low_confidence",
        }
        assert item["face_track_id"] is None
        assert item["confidence"] is None

    listing = client.get(
        f"/api/v1/workspaces/{ws_id}/media-intel/active-speaker/{payload['run']['id']}",
        headers=headers,
    )
    assert listing.status_code == 200
    assert listing.json()["unresolved"] >= 1


def test_background_route_belongs_to_the_reframe_lane(tmp_path, monkeypatch):
    """Lane G serves ``POST /background``; this router must not shadow it.

    A second registration of the same path would silently win, so the lane that
    owns it keeps it and only exposes the composition ENGINE.
    """
    from app.api.v1.media_intel_visual import visual_router

    assert not any(
        getattr(route, "path", "").endswith("/background") for route in visual_router.routes
    )
    from app.engine.intel import segmentation as engine

    for name in ("background_blur", "background_replace", "subject_crop", "tracked_overlay"):
        assert callable(getattr(engine, name)), name


# ---------------------------------------------------------------------------
# slow: real ffmpeg composition (contracts §12)
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(not fx.fixture_paths_available(), reason="ffmpeg not installed")
def test_background_blur_derives_a_new_asset_and_keeps_the_original(
    db_session, workspace_with_user, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    ws = workspace_with_user["workspace"]
    video = fx.test_pattern_mp4(tmp_path / "moving.mp4", seconds=2.0, fps=10)
    asset_id = _register_media(db_session, ws, video, key="in/moving.mp4")
    _asset_row, run_id = _seed_run(db_session, ws)

    before = hashlib.sha256(video.read_bytes()).hexdigest()
    outcome = seg.background_blur(
        db_session, workspace_id=ws, asset_id=asset_id, backend=MovingBoxBackend(),
        radius=24, run_id=run_id, mask_t_s=0.0,
    )
    assert outcome.ok is True, outcome.to_dict()
    derived = outcome.asset
    assert derived and derived["parent_asset_id"] == asset_id
    assert derived["id"] != asset_id

    from app.models import MediaAsset

    row = db_session.get(MediaAsset, derived["id"])
    assert row.parent_asset_id == asset_id
    assert row.derivation_json["operation"] == "background_blur"
    assert row.derivation_json["input_asset_id"] == asset_id
    assert row.meta_json["parent_asset_id"] == asset_id
    assert row.meta_json["derived"] is True
    out_path = seg.resolve_mask_path(ws, row.storage_key)
    assert out_path is not None and out_path.exists()
    assert seg.sha256_file(out_path) == row.checksum

    # derived-only: the ORIGINAL bytes are byte-identical after the operation
    assert hashlib.sha256(video.read_bytes()).hexdigest() == before

    # MEASURED effect: the double's matte sits over the CENTRE of the frame, and
    # the fixture's box is still at x=0 on the first frame -- so the box is
    # background there and must come out blurred (lower peak, same energy).
    source_frame = fx.video_frame_luma(video, fps=4)[0]
    derived_frame = fx.video_frame_luma(out_path, fps=4)[0]
    assert derived_frame["max_luma"] < source_frame["max_luma"] - 5, (
        source_frame, derived_frame
    )
    assert derived_frame["mean_luma"] == pytest.approx(source_frame["mean_luma"], abs=1.5)


@pytest.mark.slow
@pytest.mark.skipif(not fx.fixture_paths_available(), reason="ffmpeg not installed")
def test_background_replace_and_subject_crop_are_derived_too(
    db_session, workspace_with_user, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    ws = workspace_with_user["workspace"]
    video = fx.test_pattern_mp4(tmp_path / "static.mp4", seconds=1.0, fps=10,
                                moving_box=False)
    asset_id = _register_media(db_session, ws, video, key="in/static.mp4")
    _asset_row, run_id = _seed_run(db_session, ws)
    background = fx.test_pattern_mp4(tmp_path / "bg.mp4", seconds=1.0, fps=10,
                                     width=320, height=180, moving_box=False)

    replaced = seg.background_replace(
        db_session, workspace_id=ws, asset_id=asset_id, backend=MovingBoxBackend(),
        background_path=str(background), run_id=run_id, mask_t_s=0.0,
    )
    assert replaced.ok is True, replaced.to_dict()
    assert replaced.asset["parent_asset_id"] == asset_id

    cropped = seg.subject_crop(
        db_session, workspace_id=ws, asset_id=asset_id, backend=MovingBoxBackend(),
        aspect="9:16", pad_ratio=0.2, run_id=run_id, mask_t_s=0.0,
    )
    assert cropped.ok is True, cropped.to_dict()
    assert cropped.asset["id"] not in {asset_id, replaced.asset["id"]}

    width, height = seg.probe_geometry(
        seg.resolve_mask_path(ws, cropped.asset["storage_key"])
    )
    assert height > width, "a 9:16 crop must be taller than wide"
    # the crop follows the MASK, not the frame centre: 48x48 box + 20 % slack
    # widened to 9:16 is ~33x58, nowhere near the whole 320x180 frame
    assert 20 < width < 120 and 30 < height < 120, (width, height)


@pytest.mark.slow
@pytest.mark.skipif(not fx.fixture_paths_available(), reason="ffmpeg not installed")
def test_tracked_overlay_draws_inside_its_time_window(
    db_session, workspace_with_user, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    ws = workspace_with_user["workspace"]
    video = fx.test_pattern_mp4(tmp_path / "overlay.mp4", seconds=2.0, fps=10)
    asset_id = _register_media(db_session, ws, video, key="in/overlay.mp4")
    _asset_row, _run_id = _seed_run(db_session, ws)

    outcome = seg.tracked_overlay(
        db_session, workspace_id=ws, asset_id=asset_id,
        boxes=[{"start_s": 0.0, "end_s": 0.4, "x": 10, "y": 10, "w": 40, "h": 40}],
    )
    assert outcome.ok is True, outcome.to_dict()
    path = seg.resolve_mask_path(ws, outcome.asset["storage_key"])
    luma = [f["mean_luma"] for f in fx.video_frame_luma(path, fps=4)]
    assert len(luma) >= 4
    assert max(luma[:2]) > min(luma[2:])


class MovingBoxBackend(seg.MaskBackend):
    """Double whose rectangle matches the fixture's drawn box (deterministic)."""

    key = "double_moving_box"
    model_version = "double-1"

    def health(self) -> tuple[bool, str]:
        return True, ""

    def masks_for_frame(self, frame, *, labels, progress=None, should_cancel=None,
                        deadline=None) -> list[seg.RegionMask]:
        size = 48
        left = (frame.width - size) // 2
        top = (frame.height - size) // 2
        return [
            seg.mask_from_runs(
                seg.rect_runs(left, top, size, size, frame.width, frame.height),
                label=label, frame=frame, score=0.9,
            )
            for label in labels
        ]


def _register_media(db_session, workspace_id: str, path, *, key: str) -> str:
    """Register a real file as a MediaAsset row and put its bytes in storage."""
    from pathlib import Path

    from app.models import MediaAsset
    from app.services.storage import STORAGE_ROOT

    target = Path(STORAGE_ROOT) / workspace_id / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(path.read_bytes())
    row = MediaAsset(workspace_id=workspace_id, type="video", origin="upload",
                     storage_key=key, mime_type="video/mp4",
                     checksum=hashlib.sha256(target.read_bytes()).hexdigest())
    db_session.add(row)
    db_session.commit()
    return row.id
