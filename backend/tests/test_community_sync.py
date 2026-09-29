"""Work 09 Lane B — inbox synchronization (COMMUNITY_SYNC) tests.

Everything runs against in-test duck-typed provider fakes (Lane A's
``app.providers.social`` contract: ``list_comments``/``list_mentions`` ->
``Page(items, next_cursor)``). No network, no real providers.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

# ---------------------------------------------------------------------------
# provider fakes (duck-typed Lane A contract)
# ---------------------------------------------------------------------------


@dataclass
class CommentItem:
    """Mirror of Lane A's CommentItem (plus optional explicit kind)."""

    remote_id: str
    post_remote_id: str = ""
    text: str = ""
    author_remote_id: str = "u-1"
    author_name: str = "User One"
    created_at: datetime | None = None
    parent_remote_id: str = ""
    thread_id: str = "thread-default"
    kind: str = ""


@dataclass
class FakePage:
    items: list
    next_cursor: object = None


def _comment(remote_id: str, **kwargs) -> CommentItem:
    kwargs.setdefault("text", "hello there")
    return CommentItem(remote_id=remote_id, **kwargs)


class FakeProvider:
    """Scripted provider: cursor -> page (or an Exception to raise)."""

    def __init__(self, comments=None, mentions=None):
        self.comments = dict(comments or {})
        self.mentions = mentions if mentions is not None else FakePage([], None)
        self.comment_calls: list = []
        self.mention_calls: list = []

    def list_comments(self, account, *, cursor=None, limit=50):
        self.comment_calls.append(cursor)
        scripted = self.comments.get(cursor, FakePage([], None))
        if isinstance(scripted, BaseException):
            raise scripted
        return scripted

    def list_mentions(self, account, *, cursor=None, limit=50):
        self.mention_calls.append(cursor)
        if isinstance(self.mentions, BaseException):
            raise self.mentions
        return self.mentions


class EndlessProvider:
    """Provider that never returns next_cursor=None (safety-cap probe)."""

    def __init__(self):
        self.comment_calls: list = []

    def list_comments(self, account, *, cursor=None, limit=50):
        n = len(self.comment_calls) + 1
        self.comment_calls.append(cursor)
        return FakePage([_comment(f"end-{n}", thread_id=f"thr-{n}")], f"c{n}")

    def list_mentions(self, account, *, cursor=None, limit=50):
        return FakePage([], None)


# ---------------------------------------------------------------------------
# seeding helpers
# ---------------------------------------------------------------------------


def _seed_account(db, workspace_id: str, platform: str = "youtube"):
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


def _seed_workspace(db) -> str:
    from app.models import Workspace

    ws = Workspace(name="Other WS", slug=f"ws-{os.urandom(4).hex()}", niche="other")
    db.add(ws)
    db.commit()
    return ws.id


def _state(db, workspace_id: str, account):
    from sqlalchemy import select

    from app.models import CommunitySyncState

    return db.scalar(
        select(CommunitySyncState).where(
            CommunitySyncState.workspace_id == workspace_id,
            CommunitySyncState.account_id == account.id,
        )
    )


def _interactions(db, workspace_id: str, account_id: str | None = None):
    from sqlalchemy import select

    from app.models import SocialInteraction

    query = select(SocialInteraction).where(SocialInteraction.workspace_id == workspace_id)
    if account_id:
        query = query.where(SocialInteraction.account_id == account_id)
    return list(db.scalars(query).all())


# ---------------------------------------------------------------------------
# 1. ingestion mapping + linkage
# ---------------------------------------------------------------------------


def test_interaction_ingestion_creates_rows_with_linkage(db_session, workspace_with_user):
    from sqlalchemy import select

    from app.engine.community.sync import sync_account
    from app.models import Conversation, SocialInteraction

    ws = workspace_with_user["workspace"]
    account = _seed_account(db_session, ws, "youtube")

    comments = FakePage(
        [
            CommentItem(
                remote_id="r1", post_remote_id="p1", text="Great upload!",
                author_remote_id="u1", author_name="Ann",
                created_at=datetime(2026, 9, 1, 10, 0, 0), thread_id="thread-a",
            ),
            CommentItem(
                remote_id="r2", post_remote_id="p1", text="Thanks Ann!",
                author_remote_id="u2", author_name="Bob",
                created_at=datetime(2026, 9, 1, 10, 5, 0), thread_id="thread-a",
                parent_remote_id="r1",
            ),
            CommentItem(
                remote_id="r3", post_remote_id="p1", text="How much does this cost?",
                author_remote_id="u3", author_name="Cal",
                created_at=datetime(2026, 9, 1, 10, 10, 0), thread_id="thread-a",
            ),
        ]
    )
    mentions = FakePage(
        [
            CommentItem(
                remote_id="m1", text="hey @me look at this",
                author_remote_id="u4", author_name="Dee",
                created_at=datetime(2026, 9, 1, 11, 0, 0), thread_id="thread-mention",
            )
        ],
        None,
    )
    fake = FakeProvider({None: comments}, mentions=mentions)

    out = sync_account(db_session, ws, account, {"youtube": fake})
    assert out["ok"] is True, out
    assert out["ingested"] == 4
    assert out["duplicates"] == 0
    assert out["error"] == ""

    rows = {
        r.remote_id: r
        for r in db_session.scalars(
            select(SocialInteraction).where(
                SocialInteraction.workspace_id == ws,
                SocialInteraction.account_id == account.id,
            )
        ).all()
    }
    assert set(rows) == {"r1", "r2", "r3", "m1"}
    r1, r2, r3, m1 = rows["r1"], rows["r2"], rows["r3"], rows["m1"]

    # COMMENT defaults: status, author, post linkage fields, remote timestamp
    assert r1.kind == "COMMENT" and r1.status == "unread"
    assert r1.text == "Great upload!"
    assert r1.author_remote_id == "u1" and r1.author_name == "Ann"
    assert r1.post_remote_id == "p1" and r1.thread_id == "thread-a"
    assert r1.remote_created_at == datetime(2026, 9, 1, 10, 0, 0)
    assert r1.platform == "youtube" and r1.workspace_id == ws
    assert r1.published_post_id is None
    assert r1.content_item_id is None and r1.campaign_id is None

    # parent already ingested in the same page -> linkage
    assert r2.kind == "REPLY"
    assert r2.parent_interaction_id == r1.id

    # question heuristic
    assert r3.kind == "QUESTION" and r3.is_question is True
    assert r3.parent_interaction_id is None

    # mentions resource -> MENTION
    assert m1.kind == "MENTION" and m1.text == "hey @me look at this"

    # conversation aggregation + conversation_id linkage on the rows
    convs = db_session.scalars(
        select(Conversation).where(
            Conversation.workspace_id == ws, Conversation.account_id == account.id
        )
    ).all()
    by_key = {c.thread_key: c for c in convs}
    assert set(by_key) == {"thread-a", "thread-mention"}
    assert by_key["thread-a"].unread_count == 3
    assert by_key["thread-a"].last_interaction_at == datetime(2026, 9, 1, 10, 10, 0)
    assert r1.conversation_id == by_key["thread-a"].id
    assert m1.conversation_id == by_key["thread-mention"].id


# ---------------------------------------------------------------------------
# 2. remote-id idempotency
# ---------------------------------------------------------------------------


def test_remote_id_idempotency_counts_duplicates_without_error(
    db_session, workspace_with_user
):
    from app.engine.community.sync import sync_account

    ws = workspace_with_user["workspace"]
    account = _seed_account(db_session, ws, "youtube")

    page = FakePage(
        [
            _comment("d1", text="first"),
            _comment("d2", text="second"),
            _comment("", text="no remote id — ignored"),
        ]
    )
    fake = FakeProvider({None: page})

    out1 = sync_account(db_session, ws, account, {"youtube": fake})
    assert out1["ok"] is True and out1["ingested"] == 2
    assert out1["invalid"] == 1 and out1["duplicates"] == 0
    assert len(_interactions(db_session, ws, account.id)) == 2

    # syncing the exact same page again: no crash, dups counted, no new rows
    out2 = sync_account(db_session, ws, account, {"youtube": fake})
    assert out2["ok"] is True
    assert out2["error"] == ""
    assert out2["ingested"] == 0
    assert out2["duplicates"] == 2
    assert out2["invalid"] == 1
    assert len(_interactions(db_session, ws, account.id)) == 2

    state = _state(db_session, ws, account)
    assert state.consecutive_failures == 0
    assert state.last_error == ""


# ---------------------------------------------------------------------------
# 3. pagination + cursor resume after a mid-run crash
# ---------------------------------------------------------------------------


def test_pagination_crash_persists_only_successful_pages_and_resumes(
    db_session, workspace_with_user
):
    from app.engine.community.sync import sync_account
    from app.models.base import utcnow

    ws = workspace_with_user["workspace"]
    account = _seed_account(db_session, ws, "youtube")

    page1 = FakePage([_comment("p-a1"), _comment("p-a2")], "c1")
    page2 = FakePage([_comment("p-b1"), _comment("p-b2")], None)
    fake = FakeProvider({None: page1, "c1": RuntimeError("worker crashed")})

    out1 = sync_account(db_session, ws, account, {"youtube": fake})
    assert out1["ok"] is False
    assert out1["ingested"] == 2  # page 1 committed...
    assert "RuntimeError: worker crashed" in out1["error"]

    state = _state(db_session, ws, account)
    assert state.cursors_json == {"comments": "c1"}  # ...cursor at page 1 only
    assert state.consecutive_failures == 1
    assert state.last_error.startswith("RuntimeError")
    assert state.next_attempt_at is not None and state.next_attempt_at > utcnow()

    # backoff gate: while next_attempt_at is in the future the provider is
    # never touched
    calls_before = list(fake.comment_calls)
    out_skip = sync_account(db_session, ws, account, {"youtube": fake})
    assert out_skip["skipped"] is True and out_skip["reason"] == "backoff"
    assert fake.comment_calls == calls_before

    # forced resume from the last good cursor: no duplicates, no loss
    fake.comments["c1"] = page2
    out2 = sync_account(db_session, ws, account, {"youtube": fake}, force=True)
    assert out2["ok"] is True
    assert out2["ingested"] == 2 and out2["duplicates"] == 0

    state = _state(db_session, ws, account)
    assert state.cursors_json["comments"] == ""
    assert state.consecutive_failures == 0
    assert state.next_attempt_at is None
    assert state.last_error == ""

    remote_ids = {r.remote_id for r in _interactions(db_session, ws, account.id)}
    assert remote_ids == {"p-a1", "p-a2", "p-b1", "p-b2"}
    assert len(_interactions(db_session, ws, account.id)) == 4  # no duplicates
    assert fake.comment_calls == [None, "c1", "c1"]


# ---------------------------------------------------------------------------
# 4. provider/account failure isolation
# ---------------------------------------------------------------------------


def test_provider_failure_isolates_accounts_and_records_state(
    db_session, workspace_with_user
):
    from app.engine.community.sync import sync_workspace
    from app.models.base import utcnow

    ws = workspace_with_user["workspace"]
    account_a = _seed_account(db_session, ws, "youtube")
    account_b = _seed_account(db_session, ws, "tiktok")
    account_c = _seed_account(db_session, ws, "threads")  # not in the map

    bad = FakeProvider({None: RuntimeError("boom")})
    good = FakeProvider({None: FakePage([_comment("good-1")], None)})

    # must NOT raise even though account A's provider explodes
    out = sync_workspace(db_session, ws, {"youtube": bad, "tiktok": good})

    assert out["ok"] is False
    assert out["accounts"] == 3
    assert out["failed"] == 2
    assert out["ingested"] == 1  # account B still ingested

    # B succeeded despite A/C failing
    assert len(_interactions(db_session, ws, account_b.id)) == 1
    assert len(_interactions(db_session, ws, account_a.id)) == 0
    state_b = _state(db_session, ws, account_b)
    assert state_b.consecutive_failures == 0
    assert state_b.last_success_at is not None
    assert state_b.cursors_json["comments"] == ""

    # A's failure recorded on A's own sync state
    state_a = _state(db_session, ws, account_a)
    assert state_a.consecutive_failures == 1
    assert "boom" in state_a.last_error
    assert state_a.next_attempt_at is not None and state_a.next_attempt_at > utcnow()
    assert state_a.cursors_json == {}  # no page ever succeeded

    # unconfigured platform = honest ProviderNotConfigured failure
    state_c = _state(db_session, ws, account_c)
    assert state_c.consecutive_failures == 1
    assert "ProviderNotConfigured" in state_c.last_error
    assert state_c.next_attempt_at is not None

    # account A skips on the next run until backoff expires
    out_retry = sync_workspace(db_session, ws, {"youtube": bad, "tiktok": good})
    by_account = {r["account_id"]: r for r in out_retry["results"]}
    assert by_account[account_a.id]["skipped"] is True
    assert by_account[account_b.id]["ok"] is True  # B keeps working
    assert bad.comment_calls == [None]  # provider untouched during backoff


# ---------------------------------------------------------------------------
# 5. rate limit backoff
# ---------------------------------------------------------------------------


def test_rate_limit_backoff_sets_future_attempt_and_skips(
    db_session, workspace_with_user
):
    from app.engine.community.sync import ProviderRateLimited, sync_account
    from app.models.base import utcnow

    ws = workspace_with_user["workspace"]
    account = _seed_account(db_session, ws, "youtube")

    fake = FakeProvider({None: ProviderRateLimited("slow down", retry_after=120)})
    out = sync_account(db_session, ws, account, {"youtube": fake})
    assert out["ok"] is False
    assert "ProviderRateLimited: slow down" in out["error"]

    state = _state(db_session, ws, account)
    assert state.consecutive_failures == 1
    assert state.next_attempt_at is not None
    window = state.next_attempt_at - utcnow()
    assert timedelta(seconds=110) <= window <= timedelta(seconds=125)

    # subsequent run skips until next_attempt_at — provider never called
    out_skip = sync_account(db_session, ws, account, {"youtube": fake})
    assert out_skip["skipped"] is True and out_skip["reason"] == "backoff"
    assert len(fake.comment_calls) == 1

    # gate expires (time passes / operator clears) -> sync succeeds and resets
    state.next_attempt_at = None
    db_session.commit()
    fake.comments[None] = FakePage([_comment("ok-1")], None)
    out_ok = sync_account(db_session, ws, account, {"youtube": fake})
    assert out_ok["ok"] is True and out_ok["ingested"] == 1

    state = _state(db_session, ws, account)
    assert state.consecutive_failures == 0
    assert state.next_attempt_at is None
    assert state.last_error == ""
    assert state.last_success_at is not None


def test_backoff_delay_is_exponential_and_capped():
    from app.engine.community.sync import BACKOFF_MAX_SECONDS, _backoff_delay

    assert _backoff_delay(0) == 30
    assert _backoff_delay(1) == 30
    assert _backoff_delay(2) == 60
    assert _backoff_delay(3) == 120
    assert _backoff_delay(4) == 240
    assert _backoff_delay(17) == BACKOFF_MAX_SECONDS
    assert _backoff_delay(99) == BACKOFF_MAX_SECONDS == 3600


# ---------------------------------------------------------------------------
# 6. workspace + account isolation
# ---------------------------------------------------------------------------


def test_workspace_and_account_isolation(db_session, workspace_with_user):
    from sqlalchemy import select

    from app.engine.community.sync import sync_account
    from app.models import CommunitySyncState, SocialInteraction

    ws1 = workspace_with_user["workspace"]
    ws2 = _seed_workspace(db_session)
    account1 = _seed_account(db_session, ws1, "youtube")
    account2 = _seed_account(db_session, ws2, "youtube")

    # pre-existing row in the OTHER workspace (same remote id!)
    db_session.add(
        SocialInteraction(
            workspace_id=ws2, platform="youtube", account_id=account2.id,
            remote_id="shared-1", kind="COMMENT", text="other workspace",
        )
    )
    db_session.commit()

    fake = FakeProvider(
        {None: FakePage([_comment("shared-1", text="fresh from provider")], None)}
    )
    out = sync_account(db_session, ws1, account1, {"youtube": fake})
    assert out["ok"] is True
    assert out["ingested"] == 1 and out["duplicates"] == 0

    ws1_rows = _interactions(db_session, ws1, account1.id)
    assert [r.text for r in ws1_rows] == ["fresh from provider"]
    assert ws1_rows[0].workspace_id == ws1

    # the other workspace's row is untouched
    ws2_rows = _interactions(db_session, ws2, account2.id)
    assert len(ws2_rows) == 1
    assert ws2_rows[0].text == "other workspace"
    assert ws2_rows[0].account_id == account2.id

    # a foreign account never syncs into this workspace and leaves no state row
    calls_before = list(fake.comment_calls)
    out_bad = sync_account(db_session, ws1, account2, {"youtube": fake})
    assert out_bad["skipped"] is True
    assert out_bad["reason"] == "workspace_mismatch"
    assert fake.comment_calls == calls_before
    assert (
        db_session.scalar(
            select(CommunitySyncState).where(
                CommunitySyncState.workspace_id == ws1,
                CommunitySyncState.account_id == account2.id,
            )
        )
        is None
    )
    assert len(_interactions(db_session, ws2, account2.id)) == 1  # still untouched


# ---------------------------------------------------------------------------
# 7. publication linkage
# ---------------------------------------------------------------------------


def test_publication_linkage_sets_post_content_campaign(db_session, workspace_with_user):
    from sqlalchemy import select

    from app.engine.community.sync import sync_account
    from app.models import PublishedPost, SocialInteraction

    ws1 = workspace_with_user["workspace"]
    ws2 = _seed_workspace(db_session)
    account = _seed_account(db_session, ws1, "youtube")
    other_account = _seed_account(db_session, ws1, "youtube")
    # published_posts has UNIQUE (video_id, platform) and the session DB is
    # shared across the whole run -> suffix these seeds against polluters
    tag = os.urandom(4).hex()

    db_session.add_all(
        [
            PublishedPost(
                workspace_id=ws1, video_id=f"v1-{tag}", platform="youtube",
                account_id=account.id, remote_post_id="post-1",
                content_item_id="ci-1", campaign_id="camp-1",
            ),
            PublishedPost(  # bound to a DIFFERENT account -> must not link
                workspace_id=ws1, video_id=f"v2-{tag}", platform="youtube",
                account_id=other_account.id, remote_post_id="post-2",
                content_item_id="ci-2", campaign_id="camp-2",
            ),
            PublishedPost(  # unbound (account-less) -> linkable
                workspace_id=ws1, video_id=f"v3-{tag}", platform="youtube",
                account_id=None, remote_post_id="post-4",
                content_item_id="ci-4", campaign_id="camp-4",
            ),
            PublishedPost(  # other workspace -> must not link
                workspace_id=ws2, video_id=f"v4-{tag}", platform="youtube",
                account_id=account.id, remote_post_id="post-3",
                content_item_id="ci-3", campaign_id="camp-3",
            ),
        ]
    )
    db_session.commit()

    page = FakePage(
        [
            _comment("c1", post_remote_id="post-1"),
            _comment("c2", post_remote_id="post-2"),
            _comment("c3", post_remote_id="post-3"),
            _comment("c4", post_remote_id="post-4"),
            _comment("c5", post_remote_id="missing-post"),
            _comment("c6", post_remote_id="post-late"),
        ]
    )
    fake = FakeProvider({None: page})
    out = sync_account(db_session, ws1, account, {"youtube": fake})
    assert out["ok"] is True
    assert out["ingested"] == 6
    assert out["linked"] == 2  # c1 (account match) + c4 (unbound)

    rows = {
        r.remote_id: r
        for r in db_session.scalars(
            select(SocialInteraction).where(
                SocialInteraction.workspace_id == ws1,
                SocialInteraction.account_id == account.id,
            )
        ).all()
    }
    assert rows["c1"].published_post_id is not None
    assert rows["c1"].content_item_id == "ci-1"
    assert rows["c1"].campaign_id == "camp-1"
    assert rows["c2"].published_post_id is None  # bound to other_account
    assert rows["c3"].published_post_id is None  # other workspace
    assert rows["c4"].published_post_id is not None
    assert rows["c4"].content_item_id == "ci-4"
    assert rows["c4"].campaign_id == "camp-4"
    assert rows["c5"].published_post_id is None  # no match: keep post_remote_id
    assert rows["c5"].post_remote_id == "missing-post"
    assert rows["c6"].published_post_id is None  # post not published yet

    # publication appears later -> duplicate re-sync relinks, no new rows
    late = PublishedPost(
        workspace_id=ws1, video_id=f"v5-{tag}", platform="youtube",
        account_id=account.id, remote_post_id="post-late",
        content_item_id="ci-late", campaign_id="camp-late",
    )
    db_session.add(late)
    db_session.commit()

    out2 = sync_account(db_session, ws1, account, {"youtube": fake})
    assert out2["ok"] is True
    assert out2["ingested"] == 0 and out2["duplicates"] == 6
    assert out2["linked"] == 1

    row6 = db_session.scalar(
        select(SocialInteraction).where(
            SocialInteraction.workspace_id == ws1,
            SocialInteraction.account_id == account.id,
            SocialInteraction.remote_id == "c6",
        )
    )
    assert row6.published_post_id == late.id
    assert row6.content_item_id == "ci-late"
    assert row6.campaign_id == "camp-late"
    assert len(_interactions(db_session, ws1, account.id)) == 6  # row count held


# ---------------------------------------------------------------------------
# 8. conversation upsert
# ---------------------------------------------------------------------------


def test_conversation_upsert_unread_and_thread_uniqueness(
    db_session, workspace_with_user
):
    from sqlalchemy import select

    from app.engine.community.sync import sync_account
    from app.models import Conversation, SocialInteraction

    ws = workspace_with_user["workspace"]
    account = _seed_account(db_session, ws, "youtube")

    page_v1 = FakePage(
        [
            _comment("u1", thread_id="t1", created_at=datetime(2026, 9, 1, 10, 0)),
            _comment("u2", thread_id="t1", created_at=datetime(2026, 9, 1, 10, 5)),
        ],
        None,
    )
    fake = FakeProvider({None: page_v1})

    out1 = sync_account(db_session, ws, account, {"youtube": fake})
    assert out1["ingested"] == 2

    conv = db_session.scalar(
        select(Conversation).where(
            Conversation.workspace_id == ws,
            Conversation.account_id == account.id,
            Conversation.thread_key == "t1",
        )
    )
    assert conv is not None
    assert conv.unread_count == 2
    assert conv.last_interaction_at == datetime(2026, 9, 1, 10, 5)
    assert conv.platform == "youtube"

    # re-sync of the same page: duplicates never bump unread
    out2 = sync_account(db_session, ws, account, {"youtube": fake})
    assert out2["duplicates"] == 2 and out2["ingested"] == 0
    conv = db_session.scalar(
        select(Conversation).where(
            Conversation.workspace_id == ws,
            Conversation.account_id == account.id,
            Conversation.thread_key == "t1",
        )
    )
    assert conv.unread_count == 2

    # one genuinely new message in the same thread -> +1 unread
    page_v2 = FakePage(
        [
            _comment("u1", thread_id="t1", created_at=datetime(2026, 9, 1, 10, 0)),
            _comment("u2", thread_id="t1", created_at=datetime(2026, 9, 1, 10, 5)),
            _comment("u3", thread_id="t1", created_at=datetime(2026, 9, 1, 10, 10)),
        ],
        None,
    )
    fake.comments[None] = page_v2
    out3 = sync_account(db_session, ws, account, {"youtube": fake})
    assert out3["ingested"] == 1 and out3["duplicates"] == 2

    convs = db_session.scalars(
        select(Conversation).where(
            Conversation.workspace_id == ws,
            Conversation.account_id == account.id,
            Conversation.thread_key == "t1",
        )
    ).all()
    assert len(convs) == 1  # unique (workspace, account, thread_key) holds
    assert convs[0].unread_count == 3
    assert convs[0].last_interaction_at == datetime(2026, 9, 1, 10, 10)

    # every interaction in the thread points at that one conversation
    rows = db_session.scalars(
        select(SocialInteraction).where(
            SocialInteraction.workspace_id == ws,
            SocialInteraction.account_id == account.id,
        )
    ).all()
    assert len(rows) == 3
    assert all(r.conversation_id == convs[0].id for r in rows)


# ---------------------------------------------------------------------------
# 9. COMMUNITY_SYNC job handler registration + execution
# ---------------------------------------------------------------------------


def test_community_sync_job_handler_registered_and_runs(db_session, workspace_with_user):
    import app.engine.community.sync as sync_mod
    from app.services import jobs as jobs_service

    # module import registers the handler; re-registration is a no-op
    assert "COMMUNITY_SYNC" in jobs_service._handlers
    handler = jobs_service._handlers["COMMUNITY_SYNC"]
    assert handler is sync_mod.handle_community_sync
    sync_mod.register_community_sync_jobs()
    assert jobs_service._handlers["COMMUNITY_SYNC"] is handler

    ws = workspace_with_user["workspace"]
    account = _seed_account(db_session, ws, "youtube")

    fake = FakeProvider({None: FakePage([_comment("h1"), _comment("h2")], None)})
    ctx = jobs_service.JobContext(
        job_id="job-community-sync-test",
        type="COMMUNITY_SYNC",
        workspace_id=ws,
        cycle_id=None,
        payload={"providers": {"youtube": fake}},
        attempt=1,
        cancelled=lambda: False,
    )
    out = handler(ctx)
    assert out["ok"] is True
    assert out["ingested"] == 2
    assert out["accounts"] == 1 and out["failed"] == 0

    # handler used its own session_scope — re-read from the test session
    db_session.rollback()
    assert len(_interactions(db_session, ws, account.id)) == 2

    # a job without workspace_id fails honestly (queue retries it)
    ctx_bad = jobs_service.JobContext(
        job_id="job-community-sync-no-ws",
        type="COMMUNITY_SYNC",
        workspace_id=None,
        cycle_id=None,
        payload={},
        attempt=1,
        cancelled=lambda: False,
    )
    with pytest.raises(ValueError):
        handler(ctx_bad)


# ---------------------------------------------------------------------------
# 11. pagination safety cap
# ---------------------------------------------------------------------------


def test_pagination_safety_cap_stops_endless_provider(db_session, workspace_with_user):
    from app.engine.community.sync import MAX_PAGES_PER_RUN, sync_account

    ws = workspace_with_user["workspace"]
    account = _seed_account(db_session, ws, "youtube")
    fake = EndlessProvider()

    out = sync_account(db_session, ws, account, {"youtube": fake})
    assert out["ok"] is True
    assert out["capped"] is True
    # 50 capped comment pages + the one (empty) mentions page
    assert out["pages"] == MAX_PAGES_PER_RUN + 1
    assert len(fake.comment_calls) == MAX_PAGES_PER_RUN

    state = _state(db_session, ws, account)
    # cursor parked at the last fetched page so the next run continues
    assert state.cursors_json["comments"] == f"c{MAX_PAGES_PER_RUN}"
    assert state.consecutive_failures == 0
    assert len(_interactions(db_session, ws, account.id)) == MAX_PAGES_PER_RUN
