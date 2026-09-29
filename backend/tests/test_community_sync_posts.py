"""Work 09 — post-scoped comment sync (parent integration tests).

Facebook / Instagram / TikTok / LinkedIn / X comment APIs are scoped to a
single post (no channel-wide read exists). The sync engine must iterate the
account's published posts with an opaque, resumable cursor instead of
attempting a bare ``list_comments(account, cursor, limit)`` call that those
providers honestly reject.

Everything runs against in-test duck-typed provider fakes (Lane A's
``app.providers.social`` contract). No network, no real providers.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

# ---------------------------------------------------------------------------
# provider fakes (post-scoped variant of Lane B's contract)
# ---------------------------------------------------------------------------


@dataclass
class CommentItem:
    remote_id: str
    post_remote_id: str = ""
    text: str = ""
    author_remote_id: str = "u-1"
    author_name: str = "User One"
    created_at: datetime | None = None
    parent_remote_id: str = ""
    thread_id: str = "thr"
    kind: str = ""


@dataclass
class FakePage:
    items: list
    next_cursor: object = None


def _comment(remote_id: str, post_id: str, **kwargs) -> CommentItem:
    kwargs.setdefault("text", f"comment {remote_id}")
    return CommentItem(remote_id=remote_id, post_remote_id=post_id, **kwargs)


class PostScopedProvider:
    """Requires ``post_remote_id`` (raises without it, like Graph/Business/
    tweet-replies APIs) and serves a scripted page map per post+cursor.

    ``fail_after`` (1-based call count) simulates a mid-run crash.
    """

    comments_require_post = True

    def __init__(self, script):
        # script: {post_id: {cursor: FakePage}}
        self.script = script
        self.calls: list[tuple[str, object]] = []
        self.fail_after: int | None = None

    def list_comments(self, account, *, cursor=None, limit=50, post_remote_id=""):
        self.calls.append((post_remote_id, cursor))
        if not str(post_remote_id or "").strip():
            raise ValueError("post_remote_id is required")
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise RuntimeError("boom mid-run")
        return self.script.get(post_remote_id, {}).get(cursor, FakePage([], None))

    def list_mentions(self, account, *, cursor=None, limit=50):
        return FakePage([], None)


class ChannelProvider:
    """Channel-wide provider (no ``comments_require_post`` flag): the plain
    cursor path must be used — its signature rejects ``post_remote_id``."""

    def __init__(self, script):
        self.script = script
        self.calls: list[object] = []

    def list_comments(self, account, *, cursor=None, limit=50):
        self.calls.append(cursor)
        return self.script.get(cursor, FakePage([], None))

    def list_mentions(self, account, *, cursor=None, limit=50):
        return FakePage([], None)


# ---------------------------------------------------------------------------
# seeding helpers
# ---------------------------------------------------------------------------


def _seed_workspace(db) -> str:
    from app.models import Workspace

    ws = Workspace(name="Post WS", slug=f"ws-{os.urandom(4).hex()}", niche="other")
    db.add(ws)
    db.commit()
    return ws.id


def _seed_account(db, workspace_id: str, platform: str = "facebook"):
    from app.models import SocialAccount

    account = SocialAccount(
        workspace_id=workspace_id,
        platform=platform,
        display_name=f"{platform}-main",
        access_token_enc="enc",
    )
    db.add(account)
    db.commit()
    return account


def _seed_post(
    db,
    workspace_id: str,
    account,
    remote_post_id: str,
    created: datetime,
    *,
    bound_account=None,
    platform: str = "facebook",
):
    """``bound_account=None`` means unbound; pass ``account`` to bind."""
    from app.models import PublishedPost

    post = PublishedPost(
        workspace_id=workspace_id,
        # published_posts has a UNIQUE (video_id, platform) constraint and the
        # test DB persists across a session -> suffix keeps seeds independent
        video_id=f"vid-{remote_post_id}-{os.urandom(4).hex()}",
        platform=platform,
        account_id=(bound_account.id if bound_account else None),
        remote_post_id=remote_post_id,
        created_at=created,
    )
    db.add(post)
    db.commit()
    return post


def _interactions(db, workspace_id: str, account_id: str):
    from sqlalchemy import select

    from app.models import SocialInteraction

    return list(
        db.scalars(
            select(SocialInteraction).where(
                SocialInteraction.workspace_id == workspace_id,
                SocialInteraction.account_id == account_id,
            )
        ).all()
    )


def _state(db, workspace_id: str, account):
    from sqlalchemy import select

    from app.models import CommunitySyncState

    return db.scalar(
        select(CommunitySyncState).where(
            CommunitySyncState.workspace_id == workspace_id,
            CommunitySyncState.account_id == account.id,
        )
    )


# ---------------------------------------------------------------------------
# 1. post iteration + linkage + binding rules
# ---------------------------------------------------------------------------


def test_post_scoped_iterates_published_posts_and_links(db_session, workspace_with_user):
    from app.engine.community.sync import sync_account

    ws = workspace_with_user["workspace"]
    ws2 = _seed_workspace(db_session)
    account = _seed_account(db_session, ws, "facebook")
    other = _seed_account(db_session, ws, "facebook")

    # explicit distinct created_at -> deterministic newest-first scan order
    _seed_post(db_session, ws, account, "p-old", datetime(2026, 9, 1, 9, 0, 0))
    _seed_post(db_session, ws, account, "p-mid", datetime(2026, 9, 1, 10, 0, 0))
    _seed_post(db_session, ws, account, "p-new", datetime(2026, 9, 1, 11, 0, 0))
    _seed_post(  # bound to a DIFFERENT account -> must never be scanned
        db_session, ws, other, "p-other", datetime(2026, 9, 1, 12, 0, 0),
        bound_account=other,
    )
    _seed_post(  # other workspace -> must never be scanned
        db_session, ws2, account, "p-foreign", datetime(2026, 9, 1, 13, 0, 0),
        bound_account=account,
    )

    fake = PostScopedProvider(
        {
            "p-new": {None: FakePage([_comment("c1", "p-new"), _comment("c2", "p-new")])},
            "p-mid": {None: FakePage([_comment("c3", "p-mid")])},
            "p-old": {None: FakePage([_comment("c4", "p-old")])},
            # present in the script but must never be requested:
            "p-other": {None: FakePage([_comment("x1", "p-other")])},
            "p-foreign": {None: FakePage([_comment("f1", "p-foreign")])},
        }
    )

    out = sync_account(db_session, ws, account, {"facebook": fake})
    assert out["ok"] is True, out
    assert out["error"] == ""
    assert out["ingested"] == 4
    assert out["duplicates"] == 0

    # only this account's + unbound posts were scanned, always with a post id
    scanned = [post for post, _ in fake.calls]
    assert scanned == ["p-new", "p-mid", "p-old"]
    assert all(scanned), "channel-wide (empty post_remote_id) call attempted"

    rows = {r.remote_id: r for r in _interactions(db_session, ws, account.id)}
    assert set(rows) == {"c1", "c2", "c3", "c4"}
    assert rows["c1"].post_remote_id == "p-new"
    assert rows["c4"].post_remote_id == "p-old"

    # publication linkage for each ingested comment
    from sqlalchemy import select

    from app.models import PublishedPost

    p_new = db_session.scalar(
        select(PublishedPost).where(
            PublishedPost.workspace_id == ws,
            PublishedPost.remote_post_id == "p-new",
        )
    )
    assert rows["c1"].published_post_id == p_new.id

    # full scan finished -> cursor reset so the next run rescans fresh
    assert _state(db_session, ws, account).cursors_json.get("comments") == ""


# ---------------------------------------------------------------------------
# 2. crash mid-post -> resume exactly where it stopped, no duplicates
# ---------------------------------------------------------------------------


def test_post_scoped_crash_resumes_mid_post_without_duplicates(
    db_session, workspace_with_user
):
    from app.engine.community.sync import _post_cursor_decode, sync_account

    ws = workspace_with_user["workspace"]
    account = _seed_account(db_session, ws, "facebook")
    _seed_post(db_session, ws, account, "p1", datetime(2026, 9, 1, 10, 0, 0))
    _seed_post(db_session, ws, account, "p2", datetime(2026, 9, 1, 9, 0, 0))

    fake = PostScopedProvider(
        {
            "p1": {
                None: FakePage([_comment("a1", "p1")], next_cursor="pc1"),
                "pc1": FakePage([_comment("a2", "p1")]),
            },
            "p2": {None: FakePage([_comment("b1", "p2")])},
        }
    )
    fake.fail_after = 2  # crash on the SECOND call: p1 page 2

    out1 = sync_account(db_session, ws, account, {"facebook": fake})
    assert out1["ok"] is False
    assert "boom mid-run" in out1["error"]

    # page 1 of p1 committed its row AND its cursor before the crash
    rows = {r.remote_id for r in _interactions(db_session, ws, account.id)}
    assert rows == {"a1"}
    state = _state(db_session, ws, account)
    index, saved_post, page_cursor = _post_cursor_decode(state.cursors_json["comments"])
    assert (index, saved_post, page_cursor) == (0, "p1", "pc1")

    # failure recorded -> backoff would skip; force bypasses (as everywhere)
    out_skip = sync_account(db_session, ws, account, {"facebook": fake})
    assert out_skip["skipped"] is True and out_skip["reason"] == "backoff"

    fake.calls.clear()
    fake.fail_after = None  # provider healed
    out2 = sync_account(db_session, ws, account, {"facebook": fake}, force=True)
    assert out2["ok"] is True, out2

    # resume: first call continues on p1's saved page cursor, then p2
    assert fake.calls[0] == ("p1", "pc1")
    assert ("p2", None) in fake.calls
    assert ("p1", None) not in fake.calls  # p1 page 1 NOT refetched

    rows = {r.remote_id for r in _interactions(db_session, ws, account.id)}
    assert rows == {"a1", "a2", "b1"}  # no duplicates, nothing lost
    assert out2["duplicates"] == 0
    assert _state(db_session, ws, account).cursors_json.get("comments") == ""


# ---------------------------------------------------------------------------
# 3. idempotent rescan after a completed run
# ---------------------------------------------------------------------------


def test_post_scoped_rescan_is_idempotent(db_session, workspace_with_user):
    from app.engine.community.sync import sync_account

    ws = workspace_with_user["workspace"]
    account = _seed_account(db_session, ws, "facebook")
    _seed_post(db_session, ws, account, "p1", datetime(2026, 9, 1, 10, 0, 0))

    fake = PostScopedProvider({"p1": {None: FakePage([_comment("r1", "p1")])}})

    out1 = sync_account(db_session, ws, account, {"facebook": fake})
    assert out1["ok"] is True and out1["ingested"] == 1
    assert len(_interactions(db_session, ws, account.id)) == 1

    out2 = sync_account(db_session, ws, account, {"facebook": fake}, force=True)
    assert out2["ok"] is True
    assert out2["duplicates"] >= 1  # remote id dedup, no crash
    assert out2["ingested"] == 0
    assert len(_interactions(db_session, ws, account.id)) == 1  # row count held


# ---------------------------------------------------------------------------
# 4. no published posts -> honest no-op, provider never called
# ---------------------------------------------------------------------------


def test_post_scoped_without_published_posts_is_not_a_failure(
    db_session, workspace_with_user
):
    from app.engine.community.sync import sync_account

    ws = workspace_with_user["workspace"]
    account = _seed_account(db_session, ws, "facebook")

    fake = PostScopedProvider({})
    out = sync_account(db_session, ws, account, {"facebook": fake})
    assert out["ok"] is True, out
    assert out["error"] == ""
    assert out["ingested"] == 0
    assert fake.calls == []  # nothing to scan -> provider untouched
    state = _state(db_session, ws, account)
    assert state.consecutive_failures == 0


# ---------------------------------------------------------------------------
# 5. stale/foreign cursor -> safe restart (idempotent rescan)
# ---------------------------------------------------------------------------


def test_post_scoped_stale_cursor_restarts_safely(db_session, workspace_with_user):
    from app.engine.community.sync import sync_account

    ws = workspace_with_user["workspace"]
    account = _seed_account(db_session, ws, "facebook")
    _seed_post(db_session, ws, account, "p1", datetime(2026, 9, 1, 10, 0, 0))

    # cursor points at an index/post that no longer exists
    from app.engine.community.sync import _post_cursor_encode

    fake = PostScopedProvider({"p1": {None: FakePage([_comment("z1", "p1")])}})

    # prime the state row with a stale cursor
    sync_account(db_session, ws, account, {"facebook": PostScopedProvider({})})
    state = _state(db_session, ws, account)
    state.cursors_json = {"comments": _post_cursor_encode(99, "gone-post", "gc")}
    db_session.commit()

    out = sync_account(db_session, ws, account, {"facebook": fake}, force=True)
    assert out["ok"] is True, out
    assert out["ingested"] == 1
    assert fake.calls[0] == ("p1", None)  # restarted from the top
    assert len(_interactions(db_session, ws, account.id)) == 1


# ---------------------------------------------------------------------------
# 6. channel-wide providers keep the plain cursor path
# ---------------------------------------------------------------------------


def test_channel_wide_provider_keeps_plain_cursor_path(db_session, workspace_with_user):
    from app.engine.community.sync import sync_account

    ws = workspace_with_user["workspace"]
    account = _seed_account(db_session, ws, "youtube")

    fake = ChannelProvider(
        {None: FakePage([_comment("w1", "vid-1")], next_cursor="cc1"),
         "cc1": FakePage([_comment("w2", "vid-1")])}
    )
    out = sync_account(db_session, ws, account, {"youtube": fake})
    assert out["ok"] is True, out
    assert out["ingested"] == 2
    # ChannelProvider's signature rejects post_remote_id -> reaching here
    # proves the branch never passed post context to channel-wide providers.
    assert fake.calls == [None, "cc1"]
    assert _state(db_session, ws, account).cursors_json.get("comments") == ""
