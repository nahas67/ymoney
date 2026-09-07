"""Normalized trend model + emerging-trend detection.

Every provider emits `TrendCandidate`s that get classified into a lifecycle:
EMERGING / RISING / PEAK / DECLINING / EVERGREEN based on velocity/volume
signals and provider hints. Classification feeds opportunity scoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field

LIFECYCLES = ("EMERGING", "RISING", "PEAK", "DECLINING", "EVERGREEN", "UNKNOWN")


def classify_lifecycle(
    *,
    velocity_hint: float | None,
    volume_hint: float | None,
    news_count: int = 0,
    source: str = "unknown",
) -> tuple[str, float]:
    """Classify a trend's lifecycle; returns (lifecycle, confidence 0..1).

    Heuristics (documented, deterministic):
    - high velocity + low absolute volume          -> EMERGING (early signal)
    - high velocity + high volume                  -> RISING
    - high volume + moderate/low velocity          -> PEAK
    - low velocity + high volume + heavy coverage  -> DECLINING
    - steady mid signals from evergreen sources    -> EVERGREEN
    Confidence scales with the number of available signals.
    """
    v = velocity_hint
    vol = volume_hint
    confidence = 0.3
    if v is not None:
        confidence += 0.2
    if vol is not None:
        confidence += 0.2
    if news_count:
        confidence += 0.1

    v = v if v is not None else None
    vol = vol if vol is not None else None

    if v is None and vol is None:
        return "UNKNOWN", min(confidence, 0.35)

    hi_velocity = (v or 0) >= 0.7
    mid_velocity = 0.3 <= (v or 0) < 0.7
    lo_velocity = (v or 0) < 0.3
    hi_volume = (vol or 0) >= 0.6
    lo_volume = (vol or 0) < 0.3

    if hi_velocity and lo_volume:
        return "EMERGING", min(confidence, 0.85)
    if hi_velocity and not lo_volume:
        return "RISING", min(confidence, 0.9)
    if (mid_velocity or lo_velocity) and hi_volume and news_count >= 2:
        return "PEAK" if mid_velocity else "DECLINING", min(confidence, 0.8)
    if lo_velocity and hi_volume:
        return "DECLINING", min(confidence, 0.75)
    if mid_velocity:
        return "RISING", min(confidence, 0.7)
    # evergreen sources produce steady mid-signal topics
    if source in ("mock",) or (v == 0.5 and vol == 0.5):
        return "EVERGREEN", min(confidence, 0.6)
    return "EVERGREEN", min(confidence, 0.55)


@dataclass
class NormalizedTrend:
    """Common structure every trend source normalizes into."""

    topic: str
    source: str
    keywords: list[str] = field(default_factory=list)
    velocity: float | None = None      # 0..1 momentum
    engagement: float | None = None    # 0..1
    volume: float | None = None        # 0..1 search/mention volume
    region: str = ""
    language: str = ""
    category: str = ""
    source_url: str = ""
    raw_metadata: dict = field(default_factory=dict)
    lifecycle: str = "UNKNOWN"
    classification_confidence: float = 0.3
    confidence: float = 0.5            # source-reported confidence in the datum

    def finalize(self) -> "NormalizedTrend":
        self.lifecycle, self.classification_confidence = classify_lifecycle(
            velocity_hint=self.velocity,
            volume_hint=self.volume,
            news_count=len(self.raw_metadata.get("news") or []),
            source=self.source,
        )
        return self


def keywords_from_topic(topic: str, limit: int = 6) -> list[str]:
    import re as _re

    words = [w for w in _re.findall(r"[a-zA-Z0-9]+", topic or "") if len(w) > 2]
    return words[:limit]
