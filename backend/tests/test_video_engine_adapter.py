"""Video engine adapter tests: translation, normalization, capabilities,
idempotency of request hashing, and error mapping (no live engine needed)."""

import httpx
import pytest
import respx

from app.core.config import settings
from app.providers.video_engine.base import (
    STATE_COMPLETE,
    STATE_FAILED,
    STATE_NOT_FOUND,
    STATE_PROCESSING,
    RenderRequest,
)
from app.providers.video_engine.mpt import (
    MoneyPrinterTurboAdapter,
    normalize_mpt_state,
)

BASE = "http://engine.test"


def adapter() -> MoneyPrinterTurboAdapter:
    return MoneyPrinterTurboAdapter(base_url=BASE, timeout=60)


class TestStateNormalization:
    def test_matrix(self):
        assert normalize_mpt_state(1) == STATE_COMPLETE
        assert normalize_mpt_state(-1) == STATE_FAILED
        assert normalize_mpt_state(4) == STATE_PROCESSING
        assert normalize_mpt_state(None) == STATE_NOT_FOUND
        assert normalize_mpt_state(99) == STATE_NOT_FOUND


class TestRequestTranslation:
    def test_full_translation(self):
        req = RenderRequest(
            subject="topic", script="hello world",
            keywords=["a", "b"], aspect_ratio="16:9",
            voice_name="en-US-Aria", voice_rate=1.2, language="en",
            subtitle_enabled=False, subtitle_position="top",
            clip_duration=4, video_count=2,
        )
        body = MoneyPrinterTurboAdapter.translate_request(req)
        assert body["video_subject"] == "topic"
        assert body["video_script"] == "hello world"
        assert body["video_terms"] == "a, b"
        assert body["video_aspect"] == "16:9"
        assert body["voice_name"] == "en-US-Aria"
        assert body["voice_rate"] == 1.2
        assert body["video_language"] == "en"
        assert body["subtitle_enabled"] == "false"
        assert body["subtitle_position"] == "top"
        assert body["video_clip_duration"] == 4
        assert body["video_count"] == 2

    def test_invalid_aspect_rejected(self):
        from app.providers.video_engine.base import VideoEngineRequestInvalid

        with pytest.raises(VideoEngineRequestInvalid):
            MoneyPrinterTurboAdapter.translate_request(
                RenderRequest(subject="x", script="y", aspect_ratio="21:9")
            )


class TestRequestHashIdempotency:
    def test_same_semantics_same_hash(self):
        a = RenderRequest(subject="t", script="s", keywords=["b", "a"])
        b = RenderRequest(subject="t", script="s", keywords=["a", "b"])  # order-insensitive
        assert a.request_hash() == b.request_hash()

    def test_different_script_different_hash(self):
        a = RenderRequest(subject="t", script="s1")
        b = RenderRequest(subject="t", script="s2")
        assert a.request_hash() != b.request_hash()


class TestCapabilitiesAndCost:
    def test_capabilities_declared(self):
        caps = adapter().get_capabilities()
        assert {"VIDEO_GENERATION", "SUBTITLES", "TTS"} <= caps

    def test_estimate_is_honest_flat_value(self):
        req = RenderRequest(subject="t", script="s")
        est = adapter().estimate_cost(req)
        assert est == pytest.approx(settings.mpt_estimated_render_cost_usd)


class TestErrorMapping:
    def test_connection_error_is_retryable(self):
        err = MoneyPrinterTurboAdapter._translate_http_error(
            httpx.ConnectError("refused")
        )
        assert getattr(err, "retryable", False) is True

    def test_400_is_permanent(self):
        response = httpx.Response(400, request=httpx.Request("POST", BASE))
        err = MoneyPrinterTurboAdapter._translate_http_error(httpx.HTTPStatusError("bad", request=response.request, response=response))
        assert getattr(err, "retryable", False) is False

    def test_429_is_retryable(self):
        response = httpx.Response(429, request=httpx.Request("POST", BASE))
        err = MoneyPrinterTurboAdapter._translate_http_error(httpx.HTTPStatusError("q", request=response.request, response=response))
        assert getattr(err, "retryable", False) is True


@respx.mock
class TestHttpInteractions:
    def test_health_true_when_tasks_ok(self):
        respx.get(f"{BASE}/api/v1/tasks").mock(return_value=httpx.Response(200, json={"data": []}))
        assert adapter().health() is True

    def test_health_false_on_refusal(self):
        respx.get(f"{BASE}/api/v1/tasks").mock(side_effect=httpx.ConnectError("refused"))
        assert adapter().health() is False

    def test_submit_returns_handle(self):
        respx.post(f"{BASE}/api/v1/videos").mock(
            return_value=httpx.Response(200, json={"status": 200, "data": {"task_id": "abc"}})
        )
        handle = adapter().submit(RenderRequest(subject="t", script="s"))
        assert handle.engine_task_id == "abc"
        assert handle.engine == "moneyprinterturbo"

    def test_status_maps_fields(self):
        respx.get(f"{BASE}/api/v1/tasks/tid").mock(
            return_value=httpx.Response(200, json={"data": {
                "state": 4, "progress": 55, "videos": [], "error": "", "failed_stage": "",
            }})
        )
        st = adapter().status(type("H", (), {"engine_task_id": "tid", "engine": "moneyprinterturbo"})())
        assert st.state == STATE_PROCESSING
        assert st.progress == 55
        assert st.is_terminal is False

    def test_status_complete_with_videos(self):
        respx.get(f"{BASE}/api/v1/tasks/tid").mock(
            return_value=httpx.Response(200, json={"data": {
                "state": 1, "progress": 100, "videos": ["/tasks/tid/final-1.mp4"],
            }})
        )
        h = type("H", (), {"engine_task_id": "tid", "engine": "moneyprinterturbo"})()
        st = adapter().status(h)
        assert st.state == STATE_COMPLETE
        assert st.videos == ["tid/final-1.mp4"]
        assert adapter().get_video_url(h) == "tid/final-1.mp4"

    def test_delete_job(self):
        route = respx.delete(f"{BASE}/api/v1/tasks/tid").mock(return_value=httpx.Response(200))
        assert adapter().delete_job(type("H", (), {"engine_task_id": "tid"})()) is True
        assert route.called

    def test_version_from_openapi(self):
        respx.get(f"{BASE}/openapi.json").mock(
            return_value=httpx.Response(200, json={"info": {"version": "1.3.4"}})
        )
        assert adapter().version() == "1.3.4"
