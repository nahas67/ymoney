"""Media intelligence boundary: vision analysis of produced video.

VisionAnalysisProvider inspects finished renders and returns structured
scene/audio/subtitle/safety evidence. QC folds this evidence into its
scores. A mock provider keeps the pipeline free-first; real providers
plug in behind the same interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SceneEvidence:
    """Per-scene observations from a render inspection."""

    index: int
    duration_seconds: float = 0.0
    visual_matches_script: bool = False
    on_screen_text: str = ""
    black_frames: bool = False
    corrupted: bool = False


@dataclass
class VisionAnalysis:
    """Structured result of inspecting a finished render."""

    provider: str
    is_mock: bool
    scenes: list[SceneEvidence] = field(default_factory=list)
    audio_present: bool = False
    silent_sections: list[tuple[float, float]] = field(default_factory=list)
    subtitles_aligned: bool = False
    visual_relevance: float = 0.0
    notes: str = ""

    def summary(self) -> str:
        flags = []
        if not self.audio_present:
            flags.append("no audio track")
        if self.silent_sections:
            flags.append(f"{len(self.silent_sections)} silent section(s)")
        if not self.subtitles_aligned:
            flags.append("subtitles misaligned")
        if any(s.black_frames for s in self.scenes):
            flags.append("black frames detected")
        if any(s.corrupted for s in self.scenes):
            flags.append("corrupted frames detected")
        return "; ".join(flags) if flags else "render inspection clean"


class VisionAnalysisProvider(ABC):
    """Inspection boundary for produced video."""

    name: str = "base"
    is_mock: bool = False

    @abstractmethod
    def analyze(
        self,
        *,
        video_path: str,
        script: str,
        scene_count: int,
        expected_duration: float,
    ) -> VisionAnalysis:
        """Inspect a finished render and return structured evidence."""
        ...


class MockVisionAnalysisProvider(VisionAnalysisProvider):
    """Development-only inspection. Clearly labeled; derives evidence from
    the render request metadata, never claims real frame analysis."""

    name = "mock"
    is_mock = True

    def analyze(
        self,
        *,
        video_path: str,
        script: str,
        scene_count: int,
        expected_duration: float,
    ) -> VisionAnalysis:
        scene_duration = expected_duration / max(scene_count, 1)
        scenes = [
            SceneEvidence(
                index=i,
                duration_seconds=scene_duration,
                visual_matches_script=True,
                black_frames=False,
                corrupted=False,
            )
            for i in range(scene_count)
        ]
        return VisionAnalysis(
            provider=self.name,
            is_mock=True,
            scenes=scenes,
            audio_present=True,
            silent_sections=[],
            subtitles_aligned=True,
            visual_relevance=72.0,
            notes="[MOCK] inspection derived from render metadata, not frames",
        )


_default_provider: VisionAnalysisProvider | None = None


def get_vision_provider() -> VisionAnalysisProvider:
    global _default_provider
    if _default_provider is None:
        # Real evidence when ffprobe is available; mock only as labeled fallback.
        try:
            from app.providers.vision_ffprobe import FFprobeVisionProvider

            _default_provider = FFprobeVisionProvider()
        except Exception:
            _default_provider = MockVisionAnalysisProvider()
    return _default_provider


def set_vision_provider(provider: VisionAnalysisProvider) -> None:
    global _default_provider
    _default_provider = provider
