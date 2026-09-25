"""Mock publisher — development only.

Records the publish intent locally and returns success with a pseudo id.
Never touches any real platform. Clearly labeled in PublishedPost.is_mock.
"""

from __future__ import annotations

import time

from app.providers.publishers.base import BasePublisher, PublishMetadata, PublishResult


class MockPublisher(BasePublisher):
    platform = "mock"

    def publish(self, video_path: str = "", meta: PublishMetadata | None = None,
                account: dict | None = None, **kwargs) -> PublishResult:
        time.sleep(0.05)  # simulate latency
        _ = (video_path, meta, account, kwargs)
        post_id = f"mock-{int(time.time()*1000)}"
        return PublishResult(
            success=True,
            remote_post_id=post_id,
            remote_url=f"mock://posts/{post_id}",
        )
