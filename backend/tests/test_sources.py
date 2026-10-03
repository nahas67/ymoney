"""Work 10 Lane C — engine/sources: catalog, adapters, SOURCE_SYNC.

No live network anywhere: HTTP goes through respx mocks against literal-IP
URLs (so ``_assert_public_host`` never needs DNS), and sync runs use
config-driven fake connectors registered against the REAL registry, so the
production ``create_connector`` -> ``handle_source_sync`` wiring is what
gets exercised.

Session discipline mirrors tests/test_community_sync.py: the handler uses
its own ``session_scope``, so the test session rolls back to a fresh
snapshot before every read.
"""

from __future__ import annotations

import io
import os
from datetime import datetime

import httpx
import pytest
import respx
from sqlalchemy import select

from app.engine.sources import (
    CONNECTOR_CATALOG,
    SourceError,
    create_connector,
    describe_connector,
    enqueue_source_sync,
    register_source_jobs,
)
from app.engine.sources import registry as registry_mod
from app.engine.sources import sync as sync_mod
from app.engine.sources.adapters.local import LocalConnector
from app.engine.sources.adapters.s3 import S3Connector
from app.engine.sources.adapters.url import _assert_public_host, fetch_url, validate_config_url
from app.engine.sources.adapters.youtube import FEED_BASE, extract_channel_id, parse_video_feed
from app.engine.sources.base import (
    MAX_SOURCE_BYTES,
    SourceConfigError,
    SourceConnector,
    SourceDocumentDoc,
    sha256_text,
)
from app.models import Job, MediaAsset, SourceDocument, Workspace
from app.models import SourceConnector as SourceConnectorRow
from app.services import jobs as jobs_service

# literal public IP (example.com's) — validate_url/SSRF checks run, DNS never does
PUBLIC_HOST = "https://93.184.216.34"

_CATALOG_ONLY = (
    "google_drive",
    "dropbox",
    "onedrive",
    "zoom",
    "riverside",
    "twitch",
    "vimeo",
    "loom",
)


# ---------------------------------------------------------------------------
# test doubles: config-driven fake connectors plugged into the real registry
# ---------------------------------------------------------------------------


def _page_doc(remote_id, title="Doc", checksum="c-1", content="", mime="text/plain"):
    """JSON-serializable fake document (lives in connector config)."""
    return {
        "remote_id": remote_id,
        "title": title,
        "checksum": checksum,
        "content": content,
        "mime": mime,
    }


class _FakeDocsConnector(SourceConnector):
    """``pages`` maps cursor -> ``{docs, next, fail}`` (all JSON round-trippable)."""

    kind = "fake_docs"
    snapshot = True

    def connect(self) -> None:
        if not isinstance(self.config.get("pages"), dict):
            raise SourceConfigError("fake connector requires 'pages' in config")

    def list(self, *, cursor=None):
        self.connect()
        key = str(cursor or "")
        page = (self.config.get("pages") or {}).get(key)
        if page is None:
            raise SourceError(f"fake connector has no page for cursor {key!r}")
        if page.get("fail"):
            raise SourceError(str(page["fail"]))
        docs = [
            SourceDocumentDoc(
                remote_id=str(d["remote_id"]),
                title=str(d.get("title", "")),
                mime_type=str(d.get("mime", "")),
                content=str(d.get("content", "")),
                checksum=str(d.get("checksum", "")),
            )
            for d in page.get("docs", [])
        ]
        return docs, str(page.get("next") or "")

    def fetch(self, remote_id):
        raise SourceError("fake connector does not implement fetch")


class _FakeStreamConnector(_FakeDocsConnector):
    """Same pages config, but non-snapshot (absence proves nothing)."""

    kind = "fake_stream"
    snapshot = False


@pytest.fixture()
def fake_kinds(monkeypatch):
    for kind in ("fake_docs", "fake_stream"):
        monkeypatch.setitem(
            registry_mod.CONNECTOR_CATALOG,
            kind,
            {
                "kind": kind,
                "implemented": True,
                "requires_credentials": False,
                "title": kind,
                "blurb": "test-only fake connector",
            },
        )
    monkeypatch.setitem(registry_mod._IMPLEMENTATIONS, "fake_docs", _FakeDocsConnector)
    monkeypatch.setitem(registry_mod._IMPLEMENTATIONS, "fake_stream", _FakeStreamConnector)


def _ctx(workspace_id, connector_id, cancelled=lambda: False):
    return jobs_service.JobContext(
        job_id=f"job-source-sync-{os.urandom(4).hex()}",
        type="SOURCE_SYNC",
        workspace_id=workspace_id,
        cycle_id=None,
        payload={"connector_id": connector_id},
        attempt=1,
        cancelled=cancelled,
    )


def _handler():
    return jobs_service._handlers["SOURCE_SYNC"]


def _seed_connector(db, workspace_id, kind, config):
    row = SourceConnectorRow(
        workspace_id=workspace_id,
        kind=kind,
        name=f"{kind}-{os.urandom(3).hex()}",
        status="AVAILABLE",
        config_json=config,
    )
    db.add(row)
    db.commit()
    return row.id


def _docs(db, workspace_id=None, connector_id=None):
    stmt = select(SourceDocument)
    if workspace_id is not None:
        stmt = stmt.where(SourceDocument.workspace_id == workspace_id)
    if connector_id is not None:
        stmt = stmt.where(SourceDocument.connector_id == connector_id)
    db.rollback()  # handler committed through its own session — refresh snapshot
    return list(db.scalars(stmt).all())


def _connector(db, row_id):
    db.rollback()
    return db.get(SourceConnectorRow, row_id)


def _second_workspace(db):
    ws = Workspace(name="WS B", slug=f"ws-{os.urandom(4).hex()}", niche="second")
    db.add(ws)
    db.flush()
    db.commit()
    return ws.id


# ---------------------------------------------------------------------------
# 1. catalog honesty
# ---------------------------------------------------------------------------


def test_catalog_lists_all_13_kinds_with_honest_flags():
    assert len(CONNECTOR_CATALOG) == 13
    implemented = {k for k, v in CONNECTOR_CATALOG.items() if v["implemented"]}
    assert implemented == {"local", "url", "rss", "youtube", "s3"}
    assert CONNECTOR_CATALOG["s3"]["requires_credentials"] is True
    for kind in implemented:
        expected = kind == "s3"
        assert CONNECTOR_CATALOG[kind]["requires_credentials"] is expected
    for entry in CONNECTOR_CATALOG.values():
        assert set(entry) == {
            "kind",
            "implemented",
            "requires_credentials",
            "title",
            "blurb",
        }
        assert entry["title"] and entry["blurb"]


def test_catalog_only_kinds_refuse_creation():
    for kind in _CATALOG_ONLY:
        assert CONNECTOR_CATALOG[kind]["implemented"] is False
        with pytest.raises(SourceError, match="not implemented yet"):
            create_connector(kind, {})


def test_unknown_kind_raises():
    with pytest.raises(SourceError, match="unknown source connector kind"):
        create_connector("not_a_kind", {})
    assert create_connector("url", {"url": f"{PUBLIC_HOST}/"}).kind == "url"


# ---------------------------------------------------------------------------
# 2. health honesty (never fake availability or docs)
# ---------------------------------------------------------------------------


def test_s3_health_unavailable_without_bucket_settings(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "s3_bucket", "")
    conn = create_connector("s3", {})
    health = conn.health()
    assert health["status"] == "UNAVAILABLE"
    assert "bucket" in health["reason"].lower()
    with pytest.raises(SourceError):  # refuses rather than faking docs
        conn.list()


def test_s3_list_paginates_and_reads_text_only(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "s3_bucket", "my-bucket")

    class _FakeS3:
        def __init__(self):
            self.list_calls = []
            self.get_calls = []

        def list_objects_v2(self, **kw):
            self.list_calls.append(kw)
            if kw.get("ContinuationToken") == "t1":
                return {
                    "Contents": [
                        {"Key": "docs/b.txt", "ContentType": "text/plain", "ETag": '"xyz"', "Size": 5}
                    ],
                    "NextContinuationToken": "",
                }
            return {
                "Contents": [
                    {"Key": "docs/a.txt", "ContentType": "text/plain", "ETag": '"abc"', "Size": 5},
                    {"Key": "media/v.mp4", "ContentType": "video/mp4", "ETag": '"def"', "Size": 9},
                    {"Key": "folder/", "ContentType": "application/x-directory", "ETag": '"g"', "Size": 0},
                ],
                "NextContinuationToken": "t1",
            }

        def get_object(self, **kw):
            self.get_calls.append(kw)
            return {"Body": io.BytesIO(b"hello"), "ContentType": "text/plain", "ETag": '"abc"'}

    fake = _FakeS3()
    monkeypatch.setattr(S3Connector, "_client", lambda self: fake)

    conn = create_connector("s3", {"prefix": "docs/"})
    docs, next_cursor = conn.list()
    assert next_cursor == "t1"
    assert [d.remote_id for d in docs] == ["docs/a.txt", "media/v.mp4"]  # folder keys skipped
    assert docs[0].title == "a.txt"
    assert docs[0].checksum == "abc"  # ETag quotes stripped
    assert docs[0].asset_reference == "s3://my-bucket/docs/a.txt"
    assert docs[0].content == "hello"
    assert docs[1].content == ""  # non-text/* never read
    assert [c["Key"] for c in fake.get_calls] == ["docs/a.txt"]
    assert fake.list_calls[0]["Prefix"] == "docs/"
    assert fake.list_calls[0]["MaxKeys"] == 100

    docs2, next_cursor2 = conn.list(cursor="t1")
    assert [d.remote_id for d in docs2] == ["docs/b.txt"]
    assert next_cursor2 == ""
    assert conn.snapshot is True


def test_youtube_health_unavailable_without_config():
    conn = create_connector("youtube", {})
    with pytest.raises(SourceError):
        conn.connect()
    health = conn.health()
    assert health["status"] == "UNAVAILABLE"
    assert health["reason"]
    # URL forms whose channel id is not extractable fail honestly — never guess
    handle = create_connector("youtube", {"channel_url": "https://www.youtube.com/@somehandle"})
    with pytest.raises(SourceError, match="channel_id"):
        handle.connect()
    with pytest.raises(SourceError):
        create_connector("youtube", {"channel_id": "not-a-channel-id"}).connect()


def test_youtube_parses_channel_feed_metadata():
    channel_id = "UC" + "a" * 22
    assert extract_channel_id({"channel_id": channel_id}) == channel_id
    assert extract_channel_id(
        {"channel_url": f"https://www.youtube.com/channel/{channel_id}"}
    ) == channel_id

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/">
  <title>Chan</title>
  <entry>
    <id>yt:video:vid123abc</id>
    <yt:videoId>vid123abc</yt:videoId>
    <yt:channelId>{channel_id}</yt:channelId>
    <title>My Video</title>
    <published>2024-05-01T12:00:00+00:00</published>
    <author><name>Creator</name></author>
    <media:group>
      <media:description>A &lt;b&gt;bold&lt;/b&gt; description</media:description>
      <media:duration seconds="42"/>
    </media:group>
  </entry>
</feed>"""
    docs = parse_video_feed(feed, channel_id=channel_id)
    assert len(docs) == 1
    doc = docs[0]
    assert doc.remote_id == "vid123abc"
    assert doc.title == "My Video"
    assert doc.author == "Creator"
    assert "bold" in doc.content
    assert doc.created_at == datetime(2024, 5, 1, 12, 0, 0)
    assert doc.meta["video_id"] == "vid123abc"
    assert doc.meta["duration_seconds"] == 42

    conn = create_connector("youtube", {"channel_id": channel_id})
    conn.connect()
    assert conn.config["channel_id"] == channel_id
    # metadata only: transcripts stay in the existing clips pipeline
    assert FEED_BASE == "https://www.youtube.com/feeds/videos.xml"
    assert f"{FEED_BASE}?channel_id={channel_id}" == (
        f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    )
    assert conn.snapshot is True


# ---------------------------------------------------------------------------
# 3. RSS parsing (mocked HTTP, literal-IP URL — no DNS, no network)
# ---------------------------------------------------------------------------


@respx.mock
def test_rss_parses_feed_as_single_snapshot_page():
    feed = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Feed</title>
    <item>
      <guid>g-1</guid>
      <title>Hello <b>World</b></title>
      <description>&lt;p&gt;Hi&lt;/p&gt;</description>
      <pubDate>Mon, 02 Jan 2006 15:04:05 GMT</pubDate>
      <dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">Ada</dc:creator>
    </item>
    <item>
      <title>Second</title>
      <link>https://93.184.216.34/s2</link>
      <pubDate>garbage-date</pubDate>
    </item>
    <item>
      <title>Third</title>
    </item>
  </channel>
</rss>"""
    respx.get(f"{PUBLIC_HOST}/feed.xml").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "application/rss+xml"},
            content=feed.encode("utf-8"),
        )
    )
    conn = create_connector("rss", {"url": f"{PUBLIC_HOST}/feed.xml"})
    docs, next_cursor = conn.list()
    assert conn.snapshot is True
    assert next_cursor == ""  # single page: upsert is the real idempotency

    by_id = {d.remote_id: d for d in docs}
    assert set(by_id) == {"g-1", f"{PUBLIC_HOST}/s2", sha256_text("Third|")}
    first = by_id["g-1"]
    assert first.title == "Hello World"  # nested markup flattened, not dropped
    assert first.content == "Hi"  # HTML stripped
    assert first.author == "Ada"
    assert first.created_at == datetime(2006, 1, 2, 15, 4, 5)
    assert first.checksum == sha256_text("Hi")
    assert by_id[f"{PUBLIC_HOST}/s2"].created_at is None  # unparseable date -> None
    assert by_id[sha256_text("Third|")].created_at is None  # no guid/link -> hashed id

    # cursor is only a skip-optimization; next_cursor stays ""
    docs2, next_cursor2 = conn.list(cursor="g-1")
    assert len(docs2) == 2 and next_cursor2 == ""


# ---------------------------------------------------------------------------
# 4. URL safety (SSRF, scheme, size, MIME)
# ---------------------------------------------------------------------------


def test_public_host_rejects_private_literals_allows_public(monkeypatch):
    from app.core.config import settings
    for host in ("127.0.0.1", "10.0.0.1", "169.254.1.1", "192.168.1.1", "::1"):
        with pytest.raises(SourceError):
            _assert_public_host(host)
    assert _assert_public_host("93.184.216.34") is None  # public literal: no DNS needed
    # W11.5 C-F1: allow_private needs the operator kill-switch, so it is refused
    # by default and only honored when the operator opts in.
    with pytest.raises(SourceError, match="operator"):
        _assert_public_host("127.0.0.1", allow_private=True)
    monkeypatch.setattr(settings, "allow_private_connectors", True)
    assert _assert_public_host("127.0.0.1", allow_private=True) is None


def test_url_config_rejects_bad_scheme_and_private_hosts(monkeypatch):
    from app.core.config import settings
    with pytest.raises(SourceError, match="http"):
        validate_config_url("ftp://93.184.216.34/x")
    with pytest.raises(SourceError):
        create_connector("url", {"url": "ftp://93.184.216.34/x"}).connect()
    with pytest.raises(SourceError):  # private literal without allow_private
        create_connector("url", {"url": "https://10.0.0.5/private"}).connect()
    with pytest.raises(SourceError):  # loopback literal without allow_private
        create_connector("url", {"url": "https://127.0.0.1/x"}).connect()
    # W11.5 C-F1: allow_private is NECESSARY but not sufficient -- the operator
    # kill-switch `allow_private_connectors` (default False) gates it, so a
    # workspace admin cannot self-serve a pivot to loopback/cloud metadata.
    monkeypatch.setattr(settings, "allow_private_connectors", True)
    create_connector("url", {"url": "https://10.0.0.5/x", "allow_private": True}).connect()
    create_connector(
        "url", {"url": "http://127.0.0.1/x", "allow_private": True}
    ).connect()
    monkeypatch.setattr(settings, "allow_private_connectors", False)
    with pytest.raises(SourceError, match="operator"):
        create_connector("url", {"url": "https://10.0.0.5/x", "allow_private": True}).connect()
    with pytest.raises(SourceError, match="operator"):
        create_connector(
            "url", {"url": "http://127.0.0.1/x", "allow_private": True}
        ).connect()
    create_connector("url", {"url": f"{PUBLIC_HOST}/x"}).connect()  # public literal


@respx.mock
def test_url_oversize_body_is_rejected():
    respx.get(f"{PUBLIC_HOST}/big").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"x" * (MAX_SOURCE_BYTES + 1),
        )
    )
    conn = create_connector("url", {"url": f"{PUBLIC_HOST}/big"})
    with pytest.raises(SourceError, match="limit"):
        conn.list()


@respx.mock
def test_fetch_url_enforces_cap_while_streaming():
    # iterator body -> no Content-Length header -> the streaming cap must fire
    respx.get(f"{PUBLIC_HOST}/stream").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=iter([b"0123", b"456789"]),
        )
    )
    with pytest.raises(SourceError, match="limit"):
        fetch_url(f"{PUBLIC_HOST}/stream", max_bytes=8)


@respx.mock
def test_url_disallowed_content_type_rejected():
    respx.get(f"{PUBLIC_HOST}/file.zip").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "application/zip"}, content=b"PK\x03\x04"
        )
    )
    conn = create_connector("url", {"url": f"{PUBLIC_HOST}/file.zip"})
    with pytest.raises(SourceError, match="content-type"):
        conn.list()


@respx.mock
def test_url_html_is_extracted_deterministically():
    html = (
        b"<html><head><title>Page T</title><script>var x=1;</script></head>"
        b"<body><h1>Hello</h1><p>World</p></body></html>"
    )
    respx.get(f"{PUBLIC_HOST}/page").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, content=html
        )
    )
    conn = create_connector("url", {"url": f"{PUBLIC_HOST}/page"})
    docs, next_cursor = conn.list()
    assert next_cursor == "" and len(docs) == 1
    doc = docs[0]
    assert doc.title == "Page T"
    assert doc.content == "Hello World"  # script dropped, whitespace collapsed
    assert doc.checksum == sha256_text("Hello World")
    assert doc.remote_id == f"{PUBLIC_HOST}/page"
    assert conn.snapshot is False
    assert conn.list()[0][0].content == "Hello World"  # deterministic across runs


# ---------------------------------------------------------------------------
# 5. local adapter: workspace scoping + path safety
# ---------------------------------------------------------------------------


def test_local_lists_only_own_workspace_assets(db_session, workspace_with_user):
    ws_a = workspace_with_user["workspace"]
    ws_b = _second_workspace(db_session)
    db_session.add_all(
        [
            MediaAsset(
                workspace_id=ws_a, type="video", origin="upload",
                storage_key="videos/a.mp4", mime_type="video/mp4", checksum="md5-a",
            ),
            MediaAsset(
                workspace_id=ws_a, type="image", origin="upload",
                storage_key="images/b.png", mime_type="image/png", checksum="",
            ),
            MediaAsset(
                workspace_id=ws_b, type="video", origin="upload",
                storage_key="videos/c.mp4", mime_type="video/mp4", checksum="md5-c",
            ),
        ]
    )
    db_session.commit()
    db_session.rollback()
    ids_a = set(
        db_session.scalars(select(MediaAsset.id).where(MediaAsset.workspace_id == ws_a)).all()
    )

    docs_a, next_cursor = LocalConnector({"workspace_id": ws_a}).list()
    assert next_cursor == ""
    assert {d.remote_id for d in docs_a} == ids_a
    assert {d.title for d in docs_a} == {"a.mp4", "b.png"}
    by_key = {d.asset_reference: d for d in docs_a}
    assert by_key["videos/a.mp4"].checksum == "md5-a"
    assert by_key["images/b.png"].checksum == ""  # checksum surfaced only when present
    assert all(d.content == "" for d in docs_a)

    docs_b, _ = LocalConnector({"workspace_id": ws_b}).list()
    assert [d.title for d in docs_b] == ["c.mp4"]  # B never sees A's assets

    assert LocalConnector({"workspace_id": ws_a}).health()["status"] == "AVAILABLE"
    assert LocalConnector().health()["status"] == "AVAILABLE"  # no network to fail
    with pytest.raises(SourceError):  # context-less list refuses, never fakes empty
        LocalConnector().list()


def test_local_refuses_keys_outside_workspace_storage(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    db_session.add_all(
        [
            MediaAsset(
                workspace_id=ws, type="other", origin="upload",
                storage_key="../evil.txt", mime_type="text/plain", checksum="x",
            ),
            MediaAsset(
                workspace_id=ws, type="other", origin="upload",
                storage_key="/etc/passwd", mime_type="text/plain", checksum="y",
            ),
            MediaAsset(
                workspace_id=ws, type="other", origin="upload",
                storage_key="ok/in.txt", mime_type="text/plain", checksum="z",
            ),
        ]
    )
    db_session.commit()
    db_session.rollback()
    conn = LocalConnector({"workspace_id": ws})
    docs, _ = conn.list()
    assert [d.asset_reference for d in docs] == ["ok/in.txt"]  # traversal + absolute refused

    evil_id = db_session.scalars(
        select(MediaAsset.id).where(MediaAsset.workspace_id == ws, MediaAsset.storage_key == "../evil.txt")
    ).one()
    with pytest.raises(SourceError):
        conn.fetch(evil_id)
    with pytest.raises(SourceError):
        conn.fetch("no-such-asset")


# ---------------------------------------------------------------------------
# 6. SOURCE_SYNC: idempotency, cursor durability, failure isolation, states
# ---------------------------------------------------------------------------


def test_sync_is_idempotent_across_runs(db_session, workspace_with_user, fake_kinds):
    ws = workspace_with_user["workspace"]
    pages = {
        "": {
            "docs": [
                _page_doc("r1", title="One", checksum="c1", content="body one"),
                _page_doc("r2", title="Two", checksum="c1", content="body two"),
            ],
            "next": "",
        }
    }
    cid = _seed_connector(db_session, ws, "fake_docs", {"pages": pages})
    handler = _handler()

    out1 = handler(_ctx(ws, cid))
    assert out1 == {"created": 2, "updated": 0, "unchanged": 0, "deleted": 0, "next_cursor": ""}
    assert len(_docs(db_session, connector_id=cid)) == 2

    out2 = handler(_ctx(ws, cid))
    assert out2["created"] == 0 and out2["unchanged"] == 2
    assert out2["updated"] == 0 and out2["deleted"] == 0
    rows = _docs(db_session, connector_id=cid)
    assert len(rows) == 2  # exactly 2 rows via select — upsert, not append
    assert all(r.state == "active" for r in rows)

    row = _connector(db_session, cid)
    assert row.last_cursor == "" and row.last_error == ""
    assert row.status == "AVAILABLE" and row.doc_count == 2
    assert row.last_sync_at is not None


def test_sync_resumes_cursor_from_db_after_interruption(
    db_session, workspace_with_user, fake_kinds
):
    ws = workspace_with_user["workspace"]
    page1 = {"": {"docs": [_page_doc("a", title="A"), _page_doc("b", title="B")], "next": "p2"}}
    cid = _seed_connector(
        db_session, ws, "fake_docs", {"pages": {**page1, "p2": {"fail": "page two unavailable"}}}
    )
    handler = _handler()

    with pytest.raises(SourceError, match="page two unavailable"):
        handler(_ctx(ws, cid))

    row = _connector(db_session, cid)
    assert row.last_cursor == "p2"  # durable resume point survives the raise
    assert row.status == "ERROR" and "page two unavailable" in row.last_error
    assert len(_docs(db_session, connector_id=cid)) == 2  # page 1 kept

    # upstream recovers; a FRESH run must read cursor "p2" FROM DB
    row.config_json = {
        "pages": {
            **page1,
            "p2": {
                "docs": [_page_doc("c", title="C"), _page_doc("d", title="D")],
                "next": "",
            },
        }
    }
    db_session.commit()

    out = handler(_ctx(ws, cid))
    assert out["created"] == 2 and out["next_cursor"] == ""
    rows = _docs(db_session, connector_id=cid)
    assert len(rows) == 4
    assert len({r.remote_id for r in rows}) == 4  # no dupes
    assert all(r.state == "active" for r in rows)  # resumed pass never mass-deletes
    row = _connector(db_session, cid)
    assert row.last_cursor == ""
    assert row.status == "AVAILABLE" and row.last_error == ""
    assert row.doc_count == 4


def test_sync_walks_all_pages_in_one_run(db_session, workspace_with_user, fake_kinds):
    ws = workspace_with_user["workspace"]
    pages = {
        "": {"docs": [_page_doc("a"), _page_doc("b")], "next": "p2"},
        "p2": {"docs": [_page_doc("c"), _page_doc("d")], "next": ""},
    }
    cid = _seed_connector(db_session, ws, "fake_docs", {"pages": pages})
    out = _handler()(_ctx(ws, cid))
    assert out == {"created": 4, "updated": 0, "unchanged": 0, "deleted": 0, "next_cursor": ""}
    rows = _docs(db_session, connector_id=cid)
    assert len(rows) == 4 and all(r.state == "active" for r in rows)
    assert _connector(db_session, cid).last_cursor == ""


def test_failure_is_isolated_to_the_failing_connector(
    db_session, workspace_with_user, fake_kinds
):
    ws = workspace_with_user["workspace"]
    cid_a = _seed_connector(db_session, ws, "fake_docs", {"pages": {"": {"fail": "A is down"}}})
    cid_b = _seed_connector(
        db_session,
        ws,
        "fake_docs",
        {"pages": {"": {"docs": [_page_doc("b1", title="B doc")], "next": ""}}},
    )
    handler = _handler()

    with pytest.raises(SourceError, match="A is down"):
        handler(_ctx(ws, cid_a))

    row_a = _connector(db_session, cid_a)
    assert row_a.status == "ERROR" and "A is down" in row_a.last_error

    out_b = handler(_ctx(ws, cid_b))  # B syncs fine afterwards...
    assert out_b["created"] == 1
    assert len(_docs(db_session, connector_id=cid_b)) == 1
    row_b = _connector(db_session, cid_b)
    assert row_b.status == "AVAILABLE" and row_b.last_error == ""  # ...unaffected


def test_config_failure_records_unavailable_status(db_session, workspace_with_user, fake_kinds):
    ws = workspace_with_user["workspace"]
    cid = _seed_connector(db_session, ws, "fake_docs", {"pages": "not-a-dict"})
    with pytest.raises(SourceError):
        _handler()(_ctx(ws, cid))
    row = _connector(db_session, cid)
    assert row.status == "UNAVAILABLE"
    assert "pages" in row.last_error


def test_snapshot_upsert_marks_updated_then_deleted(
    db_session, workspace_with_user, fake_kinds
):
    ws = workspace_with_user["workspace"]
    keep = _page_doc("y", title="Y", checksum="v1", content="keep")
    v1 = {"": {"docs": [_page_doc("x", title="T1", checksum="v1", content="body v1"), keep], "next": ""}}
    v2 = {"": {"docs": [_page_doc("x", title="T1", checksum="v2", content="body v2"), keep], "next": ""}}
    v3 = {"": {"docs": [keep], "next": ""}}
    cid = _seed_connector(db_session, ws, "fake_docs", {"pages": v1})
    handler = _handler()
    assert handler(_ctx(ws, cid))["created"] == 2

    row = _connector(db_session, cid)
    row.config_json = {"pages": v2}
    db_session.commit()
    out = handler(_ctx(ws, cid))
    assert out["updated"] == 1 and out["created"] == 0
    assert out["unchanged"] == 1 and out["deleted"] == 0
    doc_x = next(r for r in _docs(db_session, connector_id=cid) if r.remote_id == "x")
    assert doc_x.state == "updated"
    assert doc_x.checksum == "v2" and doc_x.content == "body v2"

    row = _connector(db_session, cid)
    row.config_json = {"pages": v3}
    db_session.commit()
    out = handler(_ctx(ws, cid))
    assert out["deleted"] == 1 and out["created"] == 0
    rows = {r.remote_id: r for r in _docs(db_session, connector_id=cid)}
    assert rows["x"].state == "deleted"  # history preserved, row present
    assert rows["y"].state == "active"
    assert _connector(db_session, cid).doc_count == 1


def test_nonsnapshot_connector_never_deletes(db_session, workspace_with_user, fake_kinds):
    ws = workspace_with_user["workspace"]
    p1 = {"": {"docs": [_page_doc("s1", title="S1"), _page_doc("s2", title="S2")], "next": ""}}
    p2 = {"": {"docs": [_page_doc("s2", title="S2")], "next": ""}}
    cid = _seed_connector(db_session, ws, "fake_stream", {"pages": p1})
    handler = _handler()
    assert handler(_ctx(ws, cid))["created"] == 2

    row = _connector(db_session, cid)
    row.config_json = {"pages": p2}
    db_session.commit()
    out = handler(_ctx(ws, cid))
    assert out["deleted"] == 0
    rows = {r.remote_id: r for r in _docs(db_session, connector_id=cid)}
    assert rows["s1"].state == "active"  # absence proves nothing for a stream


def test_workspace_isolation_on_documents_and_sync(
    db_session, workspace_with_user, fake_kinds
):
    ws_a = workspace_with_user["workspace"]
    ws_b = _second_workspace(db_session)
    cid_a = _seed_connector(
        db_session,
        ws_a,
        "fake_docs",
        {"pages": {"": {"docs": [_page_doc("a1"), _page_doc("a2")], "next": ""}}},
    )
    handler = _handler()
    assert handler(_ctx(ws_a, cid_a))["created"] == 2

    # A's connector + docs are invisible from B
    assert _docs(db_session, workspace_id=ws_b) == []
    db_session.rollback()
    assert list(db_session.scalars(select(SourceConnectorRow).where(
        SourceConnectorRow.workspace_id == ws_b
    ))) == []

    # B's job context against A's connector: honest skip, zero writes
    out = handler(_ctx(ws_b, cid_a))
    assert "skipped" in out and "not found" in out["skipped"]
    assert _docs(db_session, workspace_id=ws_b) == []
    assert len(_docs(db_session, connector_id=cid_a)) == 2  # A untouched


# ---------------------------------------------------------------------------
# 7. enqueue policy (active-only dedupe — never jobs idempotency_key)
# ---------------------------------------------------------------------------


def test_enqueue_dedupes_inflight_then_allows_resync(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    cid = _seed_connector(db_session, ws, "local", {})
    db_session.rollback()

    jid1 = enqueue_source_sync(db_session, ws, cid)
    db_session.rollback()
    jid2 = enqueue_source_sync(db_session, ws, cid)  # first job still QUEUED
    db_session.rollback()
    assert jid1 and jid2 == jid1  # same job id — one job only
    jobs_rows = list(
        db_session.scalars(select(Job).where(Job.type == "SOURCE_SYNC", Job.workspace_id == ws))
    )
    assert len(jobs_rows) == 1

    # completed history must not block re-syncs (the contract delta)
    db_session.get(Job, jid1).status = "COMPLETED"
    db_session.commit()
    db_session.rollback()
    jid3 = enqueue_source_sync(db_session, ws, cid)
    db_session.rollback()
    assert jid3 and jid3 != jid1
    jobs_rows = list(db_session.scalars(
        select(Job).where(Job.type == "SOURCE_SYNC", Job.workspace_id == ws)
    ))
    assert len(jobs_rows) == 2


def test_enqueue_rejects_missing_disabled_and_foreign(db_session, workspace_with_user):
    ws_a = workspace_with_user["workspace"]
    ws_b = _second_workspace(db_session)
    with pytest.raises(SourceError, match="not found"):
        enqueue_source_sync(db_session, ws_a, "no-such-connector")

    cid = _seed_connector(db_session, ws_a, "local", {})
    db_session.rollback()
    with pytest.raises(SourceError, match="not found"):  # foreign workspace filtered out
        enqueue_source_sync(db_session, ws_b, cid)

    row = db_session.get(SourceConnectorRow, cid)
    row.enabled = False
    db_session.commit()
    db_session.rollback()
    with pytest.raises(SourceError, match="disabled"):
        enqueue_source_sync(db_session, ws_a, cid)


def test_register_source_jobs_is_idempotent():
    handler = sync_mod.handle_source_sync
    register_source_jobs()
    register_source_jobs()  # second call must not raise
    assert jobs_service._handlers["SOURCE_SYNC"] is handler


# ---------------------------------------------------------------------------
# 8. redaction lives in the package, not the API
# ---------------------------------------------------------------------------


def test_describe_connector_redacts_sensitive_config(db_session, workspace_with_user):
    ws = workspace_with_user["workspace"]
    cid = _seed_connector(
        db_session,
        ws,
        "rss",
        {
            "url": f"{PUBLIC_HOST}/feed.xml",
            "api_token": "super-secret",
            "secret_key": "s3cr3t",
            "password": "pw",
            "access_credential": "cred",
            "allow_private": False,
        },
    )
    db_session.rollback()
    row = db_session.get(SourceConnectorRow, cid)
    summary = describe_connector(db_session, ws, row)
    assert set(summary["config"]) == {"url", "allow_private"}
    assert summary["has_credentials"] is True
    assert summary["implemented"] is True and summary["kind"] == "rss"
    assert summary["doc_count"] == 0 and summary["last_sync_at"] is None
    assert summary["enabled"] is True
    with pytest.raises(SourceError):  # cross-workspace rows are refused
        describe_connector(db_session, "someone-else", row)
