"""Test doubles for production-only integrations (shared)."""

from __future__ import annotations

import json as _json

from app.providers.analytics import PostStats


class FakeLLMTransport:
    """Deterministic OpenAI-compatible responder keyed by prompt intent."""

    def __init__(self):
        self.calls: list[dict] = []

    def post(self, url, *, headers=None, json=None, timeout=None, **kw):  # noqa: ARG002
        self.calls.append(json or {})
        body = json or {}
        msgs = " ".join(m.get("content", "") for m in body.get("messages", []))
        low = msgs.lower()

        if "researcher" in low:
            payload = {
                "summary": "Deterministic research summary.",
                "key_facts": ["fact one", "fact two"],
                "angles": ["angle a"],
                "visual_keywords": ["chart", "finance"],
                "cautions": [],
                "claims": [
                    {"claim": "claim one", "status": "LIKELY", "confidence": 0.7, "basis": "test"},
                    {"claim": "claim two", "status": "VERIFIED", "confidence": 0.8, "basis": "test"},
                ],
            }
            text = _json.dumps(payload)
        elif "strategist" in low:
            payload = {
                "angle": "deterministic angle",
                "target_audience": "testers",
                "format": "talking head",
                "duration_seconds": 30,
                "hook_type": "question",
                "tone": "neutral",
                "cta": "follow",
                "platforms": ["youtube"],
                "aspect_ratio": "9:16",
                "rationale": "test",
            }
            text = _json.dumps(payload)
        elif "metadata" in low or "platform-optimized" in low:
            payload = {
                "youtube": {"title": "Test Title", "description": "desc",
                            "hashtags": ["#test"], "keywords": ["test"]},
            }
            text = _json.dumps(payload)
        elif "quality reviewer" in low:
            payload = {
                "hook": 85, "story": 85, "retention": 85, "pacing": 85, "audio": 85,
                "captions": 85, "visual_relevance": 85, "originality": 85,
                "accuracy": 85, "safety": 95, "caption_readability": 90,
                "brand_consistency": 85, "platform_fit": 90,
                "notes": "",
            }
            text = _json.dumps(payload)
        else:
            # script writer / generic: hook marker + CTA so QC passes deterministically
            text = ("What if one small change to your daily routine could completely reshape your results "
                    "in less than thirty days without extra tools or complicated systems? "
                    "Here are the three concrete steps people actually use, why each one works, "
                    "and the single mistake that quietly kills progress for almost everyone who tries this. "
                    "Follow for more.")

        return _HTTPResponse(200, {"choices": [{"message": {"content": text}}],
                                   "usage": {"prompt_tokens": 10, "completion_tokens": 10}})


class _HTTPResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeEngine:
    """Instant-completion engine double for pipeline tests."""

    engine_name = "fake-engine"

    def __init__(self):
        self.submissions = []

    def health(self):
        return True

    def submit(self, req):
        from app.providers.video_engine.base import RenderHandle

        tid = f"task-{len(self.submissions) + 1}"
        self.submissions.append((tid, req.request_hash()))
        return RenderHandle(engine_task_id=tid, engine=self.engine_name)

    def status(self, handle):
        from app.providers.video_engine.base import STATE_COMPLETE, RenderStatus

        return RenderStatus(state=STATE_COMPLETE, progress=100,
                            videos=[f"{handle.engine_task_id}/final-1.mp4"])

    def cancel_job(self, handle):
        return False

    def delete_job(self, handle):
        return True

    def get_video_url(self, handle):
        return f"{handle.engine_task_id}/final-1.mp4"

    def fetch_video_bytes(self, ref):
        return b"fake-video-bytes"

    def estimate_cost(self, req):
        return 0.01

    def get_capabilities(self):
        return {"VIDEO_GENERATION"}

    def version(self):
        return "test"


class FakePublisher:
    """Records successful publishes; used to keep tests network-free."""

    def __init__(self):
        self.published: list[tuple[str, str]] = []

    def publish(self, video_path, meta, account):
        self.published.append((video_path, meta.title))
        from app.providers.publishers.base import PublishResult

        return PublishResult(success=True, remote_post_id=f"fake-{len(self.published)}",
                             remote_url="http://fake/{id}")


class FakeAnalyticsProvider:
    platform = "fake"

    def fetch_stats(self, post, account):
        return PostStats(views=1000, likes=100, comments=10)

