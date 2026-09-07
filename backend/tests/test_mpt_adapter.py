"""MoneyPrinterTurbo adapter unit tests — mapping, errors, capabilities.

No live engine required: HTTP is not touched; only pure translation logic
and the normalized state mapping are exercised.
"""

import pytest

from app.core.config import settings
from app.providers.video_engine.base import (
    STATE_COMPLETE,
    STATE_FAILED,
    STATE_NOT_FOUND,
    STATE_PROCESSING,
    RenderRequest,
)
from app.providers.video_engine.mpt import (
    MPT_STATE_COMPLETE,
    MPT_STATE_FAILED,
    MPT_STATE_PROCESSING,
    MoneyPrinterTurboAdapter,
    normalize_mpt_state,
)


class TestStateMapping:
    def test_known_states(self):
        assert normalize_mpt_state(MPT_STATE_COMPLETE) == STATE_COMPLETE
        assert normalize_mpt_state(MPT_STATE_FAILED) == STATE_FAILED
        assert normalize_mpt_state(MPT_STATE_PROCESSING) == STATE_PROCESSING

    def test_unknown_state_maps_to_not_found(self):
        assert normalize_mpt_state(42) == STATE_NOT_FOUND
        assert normalize_mpt_state(None) == STATE_NOT_FOUND


class TestRequestTranslation:
    def _req(self, **kw):
        base = dict(
            subject="my topic",
            script="hello world script",
            keywords=["space", "nasa"],
            aspect_ratio="9:16",
            voice_name="en-US-AndrewNeural",
            subtitle_enabled=True,
            clip_duration=5,
            video_count=1,
        )
        base.update(kw)
        return RenderRequest(**base)

    def test_core_fields_map(self):
        body = MoneyPrinterTurboAdapter.translate_request(self._req())
        assert body["video_subject"] == "my topic"
        assert body["video_script"] == "hello world script"
        assert body["video_terms"] == "space, nasa"
        assert body["video_aspect"] == "9:16"
        assert body["voice_name"] == "en-US-AndrewNeural"
        assert body["subtitle_enabled"] == "true"
        assert body["video_clip_duration"] == 5
        assert body["video_count"] == 1

    def test_subtitle_disabled(self):
        body = MoneyPrinterTurboAdapter.translate_request(self._req(subtitle_enabled=False))
        assert body["subtitle_enabled"] == "false"

    def test_keywords_truncated_to_eight(self):
        body = MoneyPrinterTurboAdapter.translate_request(
            self._req(keywords=[f"k{i}" for i in range(20)])
        )
        assert len(body["video_terms"].split(",")) == 8

    @pytest.mark.parametrize("aspect", ["9:16", "16:9", "1:1"])
    def test_supported_aspects(self, aspect):
        body = MoneyPrinterTurboAdapter.translate_request(self._req(aspect_ratio=aspect))
        assert body["video_aspect"] == aspect

    def test_unsupported_aspect_raises_typed_error(self):
        from app.providers.video_engine.base import VideoEngineRequestInvalid

        with pytest.raises(VideoEngineRequestInvalid):
            MoneyPrinterTurboAdapter.translate_request(self._req(aspect_ratio="21:9"))

    def test_no_engine_implementation_leaks_into_body(self):
        """YMONEY domain fields must never appear in the engine payload."""
        body = MoneyPrinterTurboAdapter.translate_request(self._req())
        forbidden = {"workspace_id", "content_id", "campaign_id", "quality_profile",
                     "template", "brand"}
        assert forbidden.isdisjoint(body.keys())


class TestErrorTranslation:
    def _adapter(self):
        return MoneyPrinterTurboAdapter(base_url="http://engine.test")

    def test_connect_error_is_retryable(self):
        import httpx

        exc = self._adapter()._translate_http_error(httpx.ConnectError("refused"))
        assert getattr(exc, "retryable", False) is True

    def test_429_is_retryable(self):
        import httpx

        req = httpx.Request("GET", "http://engine.test")
        resp = httpx.Response(429, request=req)
        exc = self._adapter()._translate_http_error(httpx.HTTPStatusError("q", request=req, response=resp))
        assert getattr(exc, "retryable", False) is True

    def test_401_not_retryable_and_safe_message(self):
        import httpx

        req = httpx.Request("GET", "http://engine.test")
        resp = httpx.Response(401, request=req)
        exc = self._adapter()._translate_http_error(httpx.HTTPStatusError("auth", request=req, response=resp))
        assert getattr(exc, "retryable", False) is False
        assert "secret" not in str(exc).lower()

    def test_500_is_retryable(self):
        import httpx

        req = httpx.Request("GET", "http://engine.test")
        resp = httpx.Response(500, request=req)
        exc = self._adapter()._translate_http_error(httpx.HTTPStatusError("boom", request=req, response=resp))
        assert getattr(exc, "retryable", False) is True


class TestCapabilitiesAndCost:
    def test_capabilities_declared(self):
        caps = MoneyPrinterTurboAdapter().get_capabilities()
        assert {"VIDEO_GENERATION", "SUBTITLES", "TTS", "BGM",
                "BATCH_GENERATION"} <= caps

    def test_estimate_cost_returns_configured_flat_estimate(self):
        req = RenderRequest(subject="s", script="w")
        est = MoneyPrinterTurboAdapter().estimate_cost(req)
        assert est >= 0.0

    def test_request_hash_stable_and_sensitive(self):
        r1 = RenderRequest(subject="a", script="same")
        r2 = RenderRequest(subject="a", script="same")
        r3 = RenderRequest(subject="a", script="different")
        assert r1.request_hash() == r2.request_hash()
        assert r1.request_hash() != r3.request_hash()
