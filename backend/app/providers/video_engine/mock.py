"""MockVideoEngine — development-only simulation.

Clearly marked as non-production: it never produces real video files.
It writes a small JSON artifact describing what would have been rendered,
so downstream pipeline stages (QC, publishing mocks, analytics) can run
end-to-end offline. Implements the full engine interface for parity.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from loguru import logger

from app.providers.video_engine.base import (
    CAPABILITIES,
    STATE_COMPLETE,
    STATE_NOT_FOUND,
    BaseVideoEngine,
    RenderHandle,
    RenderRequest,
    RenderStatus,
)

MOCK_DIR = Path("data/mock_videos")


class MockVideoEngine(BaseVideoEngine):
    engine_name = "mock"

    def __init__(self):
        MOCK_DIR.mkdir(parents=True, exist_ok=True)

    def health(self) -> bool:
        return True

    def version(self) -> str | None:
        return "mock-1.0"

    def submit(self, req: RenderRequest) -> RenderHandle:
        task_id = f"mock-{uuid.uuid4().hex[:12]}"
        artifact = {
            "task_id": task_id,
            "engine": self.engine_name,
            "mock": True,
            "request_hash": req.request_hash(),
            "request": {
                "subject": req.subject,
                "script_chars": len(req.script),
                "keywords": req.keywords,
                "aspect_ratio": req.aspect_ratio,
                "voice_name": req.voice_name,
                "language": req.language,
            },
            "output_files": [f"{task_id}/final-1.mp4"],
            "started_at": time.time(),
        }
        out_dir = MOCK_DIR / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "render_spec.json").write_text(json.dumps(artifact, indent=2))
        logger.info(f"[MOCK VIDEO ENGINE] simulated render {task_id}")
        return RenderHandle(engine_task_id=task_id, engine=self.engine_name)

    def status(self, handle: RenderHandle) -> RenderStatus:
        path = MOCK_DIR / handle.engine_task_id / "render_spec.json"
        if not path.exists():
            return RenderStatus(state=STATE_NOT_FOUND)
        return RenderStatus(
            state=STATE_COMPLETE,
            progress=100,
            videos=[f"{handle.engine_task_id}/final-1.mp4"],
        )

    def cancel_job(self, handle: RenderHandle) -> bool:
        return False  # mock completes instantly; nothing to cancel

    def delete_job(self, handle: RenderHandle) -> bool:
        d = MOCK_DIR / handle.engine_task_id
        if d.exists():
            import shutil

            shutil.rmtree(d, ignore_errors=True)
            return True
        return False

    def get_video_url(self, handle: RenderHandle) -> str | None:
        st = self.status(handle)
        return st.videos[0] if st.videos else None

    def fetch_video_bytes(self, url_or_path: str) -> bytes:
        name = Path(url_or_path).name or "artifact"
        p = MOCK_DIR / name.replace(".mp4", ".json") if name.endswith(".mp4") else MOCK_DIR / name
        if not p.exists():
            p = MOCK_DIR / "render_spec.json"
        return p.read_bytes()

    def estimate_cost(self, req: RenderRequest) -> float:
        return 0.0  # mock renders cost nothing — honestly zero

    def get_capabilities(self) -> set[str]:
        caps = set(CAPABILITIES)
        caps.update({"BATCH_GENERATION", "PORTRAIT", "LANDSCAPE", "SQUARE"})
        return caps
