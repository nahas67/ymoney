"""Work 14 live-token tests -- SEPARATELY MARKED, honestly gated.

These hit REAL platform APIs and therefore need real credentials. Each test
carries a ``skipif`` marker naming the exact environment variables it needs, so
without them it is reported as skipped-with-a-reason rather than silently
passing. Nothing is mocked here.

Run them explicitly::

    YMONEY_LIVE=1 pytest backend/tests/test_work14_live_tokens.py -m live

Credentials come from the environment ONLY -- never a fixture file, never
hardcoded -- so this file contains no secrets.
"""

from __future__ import annotations

import os

import pytest

#: Marks the whole module. `-m live` is required to collect these in the
#: normal lanes, so a live call can never fire during a regression run.
pytestmark = pytest.mark.live

THREADS_TOKEN = "THREADS_ACCESS_TOKEN"
PINTEREST_TOKEN = "PINTEREST_ACCESS_TOKEN"
BSKY_HANDLE = "BLUESKY_HANDLE"
BSKY_APP_PW = "BLUESKY_APP_PASSWORD"
LIVE_FLAG = "YMONEY_LIVE"

_LIVE_GATE = (
    f"live credentials not configured: set {LIVE_FLAG}=1 plus the platform "
    f"token(s) to run real API calls")


def _missing(*names: str) -> str:
    """Reason string when the live gate is closed, else an empty string."""
    if os.getenv(LIVE_FLAG) != "1":
        return _LIVE_GATE
    absent = [n for n in names if not os.getenv(n)]
    return f"live credentials not configured: set {absent}" if absent else ""


needs_threads = pytest.mark.skipif(
    bool(_missing(THREADS_TOKEN)), reason=_LIVE_GATE)
needs_pinterest = pytest.mark.skipif(
    bool(_missing(PINTEREST_TOKEN)), reason=_LIVE_GATE)
needs_bluesky = pytest.mark.skipif(
    bool(_missing(BSKY_HANDLE, BSKY_APP_PW)), reason=_LIVE_GATE)


@needs_threads
def test_live_threads_text_publish():
    """POST /threads -> /threads_publish against the real Threads API."""
    from app.providers.publishers.base import PublishMetadata
    from app.providers.publishers.threads import ThreadsPublisher

    result = ThreadsPublisher().publish(
        "", PublishMetadata(title="YMONEY live check",
                            description="published by a live-token test"),
        {"access_token": os.environ[THREADS_TOKEN],
         "scopes": ["threads_basic", "threads_content_publish"],
         "settle_seconds": 0, "poll_attempts": 1, "poll_interval": 0})
    assert result.success is True, result.error
    assert result.remote_post_id, "the API returned no media id"


@needs_pinterest
def test_live_pinterest_boards_are_listed():
    """GET /v5/boards against the real Pinterest API."""
    from app.providers.publishers.pinterest import PinterestPublisher

    boards = PinterestPublisher().list_boards(
        {"access_token": os.environ[PINTEREST_TOKEN]})
    assert boards, "no boards returned for this token"
    assert all(b["id"] for b in boards)


@needs_bluesky
def test_bluesky_session_uses_a_real_app_password():
    """createSession against a real PDS, with a real app password."""
    from app.providers.publishers.bluesky import BlueskyPublisher

    session = BlueskyPublisher().create_session(
        identifier=os.environ[BSKY_HANDLE],
        app_password=os.environ[BSKY_APP_PW])
    assert session.get("accessJwt"), "no accessJwt returned"
    assert session.get("did", "").startswith("did:")


def test_snapchat_partner_path_stays_disabled_without_allowlisting():
    """No credential shape may enable the allowlist-gated path."""
    from app.providers.publishers.snapchat import partner_publish_enabled

    assert partner_publish_enabled({
        "snap_public_profile_id": "x", "access_token": "y",
        "allowlisted": True}) is False
    assert partner_publish_enabled({}) is False


def test_live_tests_require_the_explicit_flag_not_just_a_token():
    """Credentials alone must never switch a live test on."""
    if os.getenv(LIVE_FLAG) == "1" or not os.getenv(THREADS_TOKEN):
        return  # not the interesting case
    assert _missing(THREADS_TOKEN), (
        "a token is present but the live gate is closed: YMONEY_LIVE must be "
        "required, so a live call can never fire implicitly")
