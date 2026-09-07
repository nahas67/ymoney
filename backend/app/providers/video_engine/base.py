"""Video engine abstraction.

YMONEY communicates with video production through this interface only.
MoneyPrinterTurbo is integrated as an HTTP adapter; a mock engine exists
for development. Swapping engines requires no changes elsewhere.

Normalized task states (never expose raw engine state upstream):
    QUEUED | PROCESSING | COMPLETE | FAILED | NOT_FOUND | CANCELLED
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


# Normalized lifecycle states every adapter must map into.
STATE_QUEUED = "queued"
STATE_PROCESSING = "processing"
STATE_COMPLETE = "complete"
STATE_FAILED = "failed"
STATE_NOT_FOUND = "not_found"
STATE_CANCELLED = "cancelled"


@dataclass
class RenderRequest:
    """YMONEY's normalized generation request.

    Carries WHAT to produce, not HOW any specific engine works internally.
    """

    subject: str
    script: str
    keywords: list[str] = field(default_factory=list)
    aspect_ratio: str = "9:16"  # 9:16 | 16:9 | 1:1
    language: str = ""          # empty = engine default/auto
    # edge-tts voice name (must be valid for the engine's TTS provider)
    voice_name: str = "en-US-AndrewNeural"
    voice_volume: float = 1.0
    voice_rate: float = 1.0
    subtitle_enabled: bool = True
    subtitle_position: str = "bottom"  # bottom|top|center
    bgm_type: str = "random"
    bgm_file: str = ""
    bgm_volume: float = 0.2
    clip_duration: int = 5      # seconds per material clip
    video_count: int = 1
    # Tenant context is carried to local background workers. It is deliberately
    # excluded from request_hash: it changes credential scope, not render output.
    workspace_id: str = ""

    def request_hash(self) -> str:
        """Stable hash of semantic request content for idempotency checks."""
        import hashlib
        import json

        canonical = json.dumps(
            {
                "subject": self.subject,
                "script": self.script,
                "keywords": sorted(self.keywords),
                "aspect": self.aspect_ratio,
                "voice": self.voice_name,
                "voice_rate": self.voice_rate,
                "voice_volume": self.voice_volume,
                "subtitle": self.subtitle_enabled,
                "subtitle_position": self.subtitle_position,
                "bgm_type": self.bgm_type,
                "bgm_file": self.bgm_file,
                "bgm_volume": self.bgm_volume,
                "clip_duration": self.clip_duration,
                "count": self.video_count,
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]


@dataclass
class RenderHandle:
    engine_task_id: str
    engine: str


@dataclass
class RenderStatus:
    state: str  # normalized: see STATE_* constants
    progress: int = 0
    videos: list[str] = field(default_factory=list)  # relative refs or URLs
    error: str = ""
    failed_stage: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.state in (STATE_COMPLETE, STATE_FAILED, STATE_NOT_FOUND, STATE_CANCELLED)


class VideoEngineError(Exception):
    pass


class VideoEngineUnavailable(VideoEngineError):
    """Engine unreachable/unhealthy — retryable."""

    retryable = True


class VideoEngineRequestInvalid(VideoEngineError):
    """Bad configuration/request — NOT retryable."""

    retryable = False


CAPABILITIES = {
    "VIDEO_GENERATION",
    "SUBTITLES",
    "TTS",
    "BGM",
}


class BaseVideoEngine(ABC):
    engine_name: str = "base"

    # -- lifecycle -----------------------------------------------------------

    @abstractmethod
    def health(self) -> bool:
        ...

    @abstractmethod
    def submit(self, req: RenderRequest) -> RenderHandle:
        """Create a generation job. Must be idempotency-safe upstream."""
        ...

    @abstractmethod
    def status(self, handle: RenderHandle) -> RenderStatus:
        ...

    @abstractmethod
    def cancel_job(self, handle: RenderHandle) -> bool:
        """Best-effort cancellation. Returns False when unsupported/terminal."""
        ...

    @abstractmethod
    def delete_job(self, handle: RenderHandle) -> bool:
        """Delete an existing engine task (and its files where supported)."""
        ...

    @abstractmethod
    def get_video_url(self, handle: RenderHandle) -> str | None:
        """Primary output reference once complete; None otherwise."""
        ...

    @abstractmethod
    def fetch_video_bytes(self, url_or_path: str) -> bytes:
        ...

    # -- introspection ---------------------------------------------------------

    @abstractmethod
    def estimate_cost(self, req: RenderRequest) -> float:
        """Best-effort monetary ESTIMATE in USD; 0.0 when unknown.
        Never invent precision that does not exist."""
        ...

    @abstractmethod
    def get_capabilities(self) -> set[str]:
        ...

    def version(self) -> str | None:
        return None

    def list_recent_tasks(self, limit: int = 30) -> list[dict]:
        """Best-effort listing of recent engine tasks for orphan reconciliation.
        Each item: {task_id, subject, state(normalized), progress}.
        Engines that cannot support this return []."""
        return []
