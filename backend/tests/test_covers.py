"""Cover variants compare: generate candidates, pick one as the poster frame."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.providers import clips as clips_mod


def _has_ffmpeg() -> bool:
    return clips_mod.ffmpeg_available()


def _make_video(dest: Path, seconds: int = 30) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc=size=640x960:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(dest)],
        capture_output=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode()[:300]


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg not installed")
def test_extract_covers_returns_indexed_frames(tmp_path, monkeypatch):
    from app.services.storage import LocalStorage

    monkeypatch.chdir(tmp_path)
    src = tmp_path / "clip.mp4"
    _make_video(src)
    covers = LocalStorage().extract_covers(str(src), [1.0, 10.0, 20.0])
    assert len(covers) == 3
    assert [c["index"] for c in covers] == [0, 1, 2]
    for c in covers:
        assert Path(c["path"]).exists()
    # missing ffmpeg-safe path: unknown file yields []
    assert LocalStorage().extract_covers(str(tmp_path / "nope.mp4"), [1.0]) == []


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg not installed")
def test_cover_compare_and_pick_flow(tmp_path, monkeypatch):
    import uuid

    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.db import session_scope
    from app.main import create_app
    from app.models import ContentItem, Video, VideoVariant

    client = TestClient(create_app(), raise_server_exceptions=False)
    email = f"cov{uuid.uuid4().hex[:8]}@test.local"
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    ws_id = data["workspace"]["id"]

    media_dir = Path("data/videos") / ws_id
    media_dir.mkdir(parents=True, exist_ok=True)
    _make_video(media_dir / "final.mp4")

    with session_scope() as s:
        item = ContentItem(workspace_id=ws_id, topic="cover test")
        s.add(item)
        s.flush()
        variant = VideoVariant(content_item_id=item.id, label="v1", script="x" * 20, selected=True)
        s.add(variant)
        s.flush()
        video = Video(variant_id=variant.id, workspace_id=ws_id, engine="ffmpeg_avatar",
                      status="READY", file_path=str(media_dir / "final.mp4"))
        s.add(video)
        s.flush()
        vid = video.id

    r = client.post(f"/api/v1/workspaces/{ws_id}/videos/{vid}/covers",
                    headers=headers, json={"count": 3})
    assert r.status_code == 200, r.text
    covers = r.json()["covers"]
    assert len(covers) == 3
    assert all({"index", "path", "at_seconds", "url"} <= set(c) for c in covers)

    # serve a candidate
    r = client.get(f"/api/v1/workspaces/{ws_id}/videos/{vid}/covers/1/file", headers=headers)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"

    # pick candidate 2 as the poster
    r = client.post(f"/api/v1/workspaces/{ws_id}/videos/{vid}/thumbnail",
                    headers=headers, json={"cover_index": 2})
    assert r.status_code == 200, r.text
    assert r.json()["thumbnail_path"].endswith(".cover-2.jpg")

    # poster serves the picked candidate
    r = client.get(f"/api/v1/workspaces/{ws_id}/videos/{vid}/thumbnail", headers=headers)
    assert r.status_code == 200

    # unknown candidate index 404s
    r = client.post(f"/api/v1/workspaces/{ws_id}/videos/{vid}/thumbnail",
                    headers=headers, json={"cover_index": 9})
    assert r.status_code == 404

    # browser-tag access via ?token= (no Authorization header)
    token = data["access_token"]
    r = client.get(f"/api/v1/workspaces/{ws_id}/videos/{vid}/covers/1/file?token={token}")
    assert r.status_code == 200
    r = client.get(f"/api/v1/workspaces/{ws_id}/videos/{vid}/file?token={token}")
    assert r.status_code == 200
    r = client.get(f"/api/v1/workspaces/{ws_id}/videos/{vid}/file?token=bad")
    assert r.status_code == 401
    r = client.get(f"/api/v1/workspaces/{ws_id}/videos/{vid}/file")
    assert r.status_code == 401

    shutil.rmtree(tmp_path / "data", ignore_errors=True)
