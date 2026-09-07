"""MoneyPrinterTurbo HTTP adapter.

The ONLY place in YMONEY that knows MoneyPrinterTurbo's API shape:
  POST /api/v1/videos            -> {data: {task_id}}
  GET  /api/v1/tasks/{id}        -> {data: {state, progress, videos[], error, failed_stage}}
  DELETE /api/v1/tasks/{id}      -> delete task + files
  GET  /tasks/<file>             -> static output files
     MPT raw states: -1 failed | 1 complete | 4 processing

Everything crossing into YMONEY domain is normalized (see base.py constants).
"""

from __future__ import annotations

import time

import httpx
from loguru import logger

from app.core.config import settings
from app.providers.video_engine.base import (
    CAPABILITIES,
    STATE_COMPLETE,
    STATE_FAILED,
    STATE_NOT_FOUND,
    STATE_PROCESSING,
    BaseVideoEngine,
    RenderHandle,
    RenderRequest,
    RenderStatus,
    VideoEngineError,
    VideoEngineRequestInvalid,
    VideoEngineUnavailable,
)

MPT_STATE_FAILED = -1
MPT_STATE_COMPLETE = 1
MPT_STATE_PROCESSING = 4


def normalize_mpt_state(raw: int | None) -> str:
    """Map MoneyPrinterTurbo numeric task state into YMONEY normalized states."""
    if raw == MPT_STATE_COMPLETE:
        return STATE_COMPLETE
    if raw == MPT_STATE_FAILED:
        return STATE_FAILED
    if raw == MPT_STATE_PROCESSING:
        return STATE_PROCESSING
    return STATE_NOT_FOUND


class MoneyPrinterTurboAdapter(BaseVideoEngine):
    engine_name = "moneyprinterturbo"

    def __init__(self, base_url: str | None = None, timeout: int | None = None):
        self.base_url = (base_url or settings.mpt_base_url).rstrip("/")
        self.timeout = timeout or settings.mpt_timeout_seconds

    # -- transport -----------------------------------------------------------

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=30)

    @staticmethod
    def _translate_http_error(exc: httpx.HTTPError) -> VideoEngineError:
        """Map transport/HTTP failures into safe, typed engine errors.
        Never include server paths or response bodies with secrets."""
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
            return VideoEngineUnavailable(f"video engine unreachable at {settings.mpt_base_url}")
        if status is None:
            return VideoEngineUnavailable(f"video engine network failure: {type(exc).__name__}")
        if status == 429:
            return VideoEngineUnavailable("engine queue full (429); retry later")
        if status in (401, 403):
            return VideoEngineRequestInvalid(f"engine authentication failed ({status})")
        if status == 404:
            return VideoEngineRequestInvalid("engine endpoint not found — check MONEYPRINTERTURBO_BASE_URL")
        if 400 <= status < 500:
            return VideoEngineRequestInvalid(f"engine rejected request ({status})")
        return VideoEngineUnavailable(f"engine server error ({status})")

    # -- lifecycle -----------------------------------------------------------

    def health(self) -> bool:
        try:
            with self._client() as client:
                resp = client.get("/api/v1/tasks", params={"page": 1, "page_size": 1})
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def version(self) -> str | None:
        try:
            with self._client() as client:
                resp = client.get("/openapi.json")
                if resp.status_code == 200:
                    return str(resp.json().get("info", {}).get("version") or "") or None
        except httpx.HTTPError:
            pass
        return None

    def list_recent_tasks(self, limit: int = 30) -> list[dict]:
        """Recent tasks incl. their subject — enables orphan reconciliation."""
        try:
            with self._client() as client:
                resp = client.get("/api/v1/tasks", params={"page": 1, "page_size": min(limit, 100)})
            resp.raise_for_status()
            tasks = resp.json().get("data", {}).get("tasks", [])
            out = []
            for t in tasks:
                subject = ((t.get("params") or {}).get("video_subject") or "")
                out.append({
                    "task_id": t.get("task_id"),
                    "subject": subject,
                    "state": normalize_mpt_state(t.get("state")),
                    "progress": int(t.get("progress") or 0),
                })
            return out
        except httpx.HTTPError as exc:
            logger.warning(f"list_recent_tasks failed: {exc}")
            return []

    @staticmethod
    def translate_request(req: RenderRequest) -> dict:
        """YMONEY normalized request -> MoneyPrinterTurbo VideoParams."""
        aspect = {"9:16": "9:16", "16:9": "16:9", "1:1": "1:1"}.get(req.aspect_ratio)
        if aspect is None:
            raise VideoEngineRequestInvalid(f"unsupported aspect ratio: {req.aspect_ratio}")
        body = {
            "video_subject": req.subject,
            "video_script": req.script,
            "video_terms": ", ".join(req.keywords[:8]) if req.keywords else "",
            "video_aspect": aspect,
            "voice_name": req.voice_name,
            "voice_volume": req.voice_volume,
            "voice_rate": req.voice_rate,
            "video_language": req.language or "",
            "subtitle_enabled": "true" if req.subtitle_enabled else "false",
            "subtitle_position": req.subtitle_position,
            "bgm_type": req.bgm_type,
            "bgm_file": req.bgm_file,
            "bgm_volume": req.bgm_volume,
            "video_clip_duration": max(1, int(req.clip_duration)),
            "video_count": max(1, req.video_count),
            "n_threads": 2,
        }
        return body

    def submit(self, req: RenderRequest) -> RenderHandle:
        body = self.translate_request(req)
        try:
            with self._client() as client:
                resp = client.post("/api/v1/videos", json=body, timeout=60)
            if resp.status_code == 429:
                raise VideoEngineUnavailable("engine queue full; retry later")
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != 200:
                raise VideoEngineRequestInvalid(f"engine rejected task: {data.get('message')}")
            task_id = data["data"]["task_id"]
            logger.info(f"MPT task submitted: {task_id} (hash {req.request_hash()})")
            return RenderHandle(engine_task_id=task_id, engine=self.engine_name)
        except VideoEngineError:
            raise
        except httpx.HTTPError as exc:
            raise self._translate_http_error(exc) from exc

    def status(self, handle: RenderHandle) -> RenderStatus:
        try:
            with self._client() as client:
                resp = client.get(f"/api/v1/tasks/{handle.engine_task_id}", timeout=30)
            resp.raise_for_status()
            data = resp.json().get("data", {})
        except httpx.HTTPError as exc:
            raise self._translate_http_error(exc) from exc

        if not data:
            return RenderStatus(state=STATE_NOT_FOUND)
        state = normalize_mpt_state(data.get("state"))
        raw_videos = data.get("videos") or []
        videos = [self._localize(v) for v in raw_videos if v]
        return RenderStatus(
            state=state,
            progress=int(data.get("progress") or 0),
            videos=videos,
            failed_stage=data.get("failed_stage", "") or "",
            error=data.get("error", "") or "",
        )

    def cancel_job(self, handle: RenderHandle) -> bool:
        """MPT has no cancel endpoint; deletion of a running task is refused by
        the engine, so cancellation = mark abandoned upstream. Returns False to
        signal 'not cancellable remotely'."""
        st = self.status(handle)
        if st.state == STATE_COMPLETE:
            return self.delete_job(handle)
        return False

    def delete_job(self, handle: RenderHandle) -> bool:
        try:
            with self._client() as client:
                resp = client.delete(f"/api/v1/tasks/{handle.engine_task_id}", timeout=30)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def get_video_url(self, handle: RenderHandle) -> str | None:
        st = self.status(handle)
        if st.state == STATE_COMPLETE and st.videos:
            return st.videos[0]
        return None

    def fetch_video_bytes(self, url_or_path: str) -> bytes:
        url = url_or_path
        if not url.startswith(("http://", "https://")):
            url = f"{self.base_url}/tasks/{url_or_path.lstrip('/')}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    return b"".join(chunk for chunk in resp.iter_bytes(chunk_size=1 << 20))
        except httpx.HTTPError as exc:
            raise self._translate_http_error(exc) from exc

    # -- introspection ---------------------------------------------------------

    def estimate_cost(self, req: RenderRequest) -> float:
        """Honest estimate only: a configurable flat per-render estimate.
        MPT exposes no monetary cost; this is flagged is_estimate upstream."""
        return float(getattr(settings, "mpt_estimated_render_cost_usd", 0.02))

    def get_capabilities(self) -> set[str]:
        caps = set(CAPABILITIES)
        caps.update({"BATCH_GENERATION", "LOCAL_ASSETS", "STOCK_FOOTAGE",
                     "PORTRAIT", "LANDSCAPE", "SQUARE"})
        return caps

    # -- helpers ---------------------------------------------------------------

    def wait_until_done(self, handle: RenderHandle, poll_interval: float = 5.0,
                        on_progress=None) -> RenderStatus:
        deadline = time.time() + self.timeout
        last_progress = -1
        while time.time() < deadline:
            st = self.status(handle)
            if st.is_terminal:
                return st
            if on_progress and st.progress != last_progress:
                on_progress(st)
                last_progress = st.progress
            time.sleep(poll_interval)
        raise VideoEngineUnavailable(f"render timed out after {self.timeout}s")

    def save_video(self, handle: RenderHandle, dest_dir) -> object:
        st = self.wait_until_done(handle)
        if st.state != STATE_COMPLETE or not st.videos:
            raise VideoEngineError(f"no output video: {st.error or st.state}")
        data = self.fetch_video_bytes(st.videos[0])
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = dest_dir / f"{handle.engine_task_id}.mp4"
        out.write_bytes(data)
        logger.info(f"saved rendered video: {out} ({out.stat().st_size} bytes)")
        return out

    @staticmethod
    def _localize(video_ref: str) -> str:
        if video_ref.startswith(("http://", "https://")):
            return video_ref
        ref = video_ref
        for prefix in ("/tasks/", "tasks/"):
            if ref.startswith(prefix):
                ref = ref[len(prefix):]
                break
        return ref

