"""Publishing provider layer.

Publisher implementations use official platform APIs. Real publishing is
gated on connected accounts (OAuth credentials stored encrypted). When
MOCK_PUBLISHING=true or an account is missing, the MockPublisher records a
clearly-labeled local result instead of pretending to publish.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class PublishMetadata:
    title: str
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    privacy: str = "public"
    extra: dict = field(default_factory=dict)


@dataclass
class PublishResult:
    success: bool
    remote_post_id: str = ""
    remote_url: str = ""
    error: str = ""
    retryable: bool = False


class PublisherError(Exception):
    pass


class BasePublisher(ABC):
    platform: str = "base"

    @abstractmethod
    def publish(self, video_path: str, meta: PublishMetadata, account: dict) -> PublishResult:
        """account carries decrypted tokens + external ids."""
        ...
