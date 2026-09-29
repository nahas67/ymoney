"""Connector adapters (Work 10 Lane C).

Explicit re-exports: ``registry`` imports every implemented adapter from
this package surface, and tests patch the registry against these classes.
Importing the package pulls each adapter module once (no cycles — the
submodules only depend on ``sources.base`` and each other).
"""

from __future__ import annotations

from app.engine.sources.adapters.local import LocalConnector
from app.engine.sources.adapters.rss import RssConnector
from app.engine.sources.adapters.s3 import S3Connector
from app.engine.sources.adapters.url import UrlConnector
from app.engine.sources.adapters.youtube import YoutubeConnector

__all__ = [
    "LocalConnector",
    "RssConnector",
    "S3Connector",
    "UrlConnector",
    "YoutubeConnector",
]
