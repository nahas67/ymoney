"""Work 15.5 — provider/media search cache: the guards that prevent wrong data.

Every test here targets one specific way this cache can lie:

* a credential in the key (fragmentation + secret in a persisted digest input),
* an orphaned temp file that no TTL sweep would ever collect,
* a non-atomic write leaving a truncated payload readable,
* a future mtime treated as infinitely fresh,
* an EMPTY result cached and blocking every later attempt for a full TTL,
* a credential-bound signed URL persisted and replayed,
* a delete decided from a stale directory scan,
* N concurrent identical searches issuing N remote requests.

Ported from MoneyPrinterTurbo 1.3.7 (MIT, Copyright (c) 2024 Harry); the
workspace scoping and the skew/signed-URL handling are re-derived for YMONEY.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

import pytest

from app.services import media_cache
from app.services.media_cache import (
    MEDIA_CACHE_TTL_SECONDS,
    MTIME_FUTURE_TOLERANCE_SECONDS,
    ORPHAN_MIN_AGE_SECONDS,
    cache_key,
    cache_stats,
    is_credential_param,
    is_fresh,
    is_signed_url,
    provider_disables_cache,
    public_page_url,
    read_media_cache,
    search_media_cached,
    sweep_media_cache,
    write_media_cache,
)

PARAMS = {"provider": "pexels", "search_term": "money growth",
          "minimum_duration": 5, "aspect": "9:16"}

#: Two obviously-different credentials. Neither is a real secret; they only
#: need to be distinct for the key-equality assertion to mean anything.
CRED_A = "ALPHA-" + "credential"
CRED_B = "BRAVO-" + "credential"


def _symlinks_available() -> bool:
    """Windows needs a privilege for symlink creation; probe once, declaratively."""
    with tempfile.TemporaryDirectory() as probe:
        target = Path(probe) / "t.txt"
        target.write_text("x", encoding="utf-8")
        link = Path(probe) / "l.txt"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError, AttributeError):
            return False
        return True


requires_symlinks = pytest.mark.skipif(
    not _symlinks_available(), reason="symlink creation not permitted here")


def _item(url: str = "https://cdn.test/clip.mp4", **extra) -> dict:
    return {"provider": "pexels", "url": url, "duration": 6.0,
            "source_info": {"provider": "pexels", "asset_id": "abc"}, **extra}


def _orphan(directory, key: str | None = None, age_seconds: float = 7200.0) -> str:
    """Write a temp file exactly as an interrupted save would leave one."""
    stem = key or cache_key(PARAMS)
    path = directory / f".{stem}-k3yz1a2b.tmp"
    path.write_text('{"version":2,"items":[]}', encoding="utf-8")
    mtime = time.time() - age_seconds
    os.utime(path, (mtime, mtime))
    return str(path)


# ===========================================================================
# 1. the cache key excludes the API key
# ===========================================================================


def test_cache_key_ignores_the_api_key_entirely():
    """A key authenticates; it does not change a public catalog's results."""
    with_key = dict(PARAMS, api_key=CRED_A)
    other_key = dict(PARAMS, api_key=CRED_B)
    assert cache_key(with_key) == cache_key(other_key) == cache_key(PARAMS)


def test_cache_key_still_changes_when_a_result_affecting_param_changes():
    """The exclusion must not become "ignore everything"."""
    for field, value in (("search_term", "crypto"), ("aspect", "16:9"),
                         ("minimum_duration", 12), ("provider", "pixabay")):
        assert cache_key(dict(PARAMS, **{field: value})) != cache_key(PARAMS), field


def test_cache_key_is_order_insensitive_and_fixed_length():
    """Key ORDER in the input dict must not change the digest."""
    reordered = dict(reversed(list(PARAMS.items())))
    digest = cache_key(PARAMS)
    assert cache_key(reordered) == digest
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)


@pytest.mark.parametrize("name", [
    "api_key", "API_KEY", "apikey", "token", "access_token", "secret",
    "client_secret", "password", "authorization", "pexels_api_key",
    "api-key", "credentials", "jwt", "bearer",
])
def test_credential_param_names_are_recognised(name: str):
    assert is_credential_param(name) is True


@pytest.mark.parametrize("name", ["search_term", "provider", "aspect", "orientation"])
def test_result_params_are_not_mistaken_for_credentials(name: str):
    assert is_credential_param(name) is False


def test_caller_may_pass_its_whole_config_and_the_key_stays_credential_free():
    """A whole request config in, a credential-free digest input out."""
    secret = "LEAK" + "-me-not"
    digest_input = media_cache.result_affecting_params(
        dict(PARAMS, api_key=secret, workspace="acme"))
    assert "api_key" not in digest_input
    assert digest_input["search_term"] == "money growth"
    # The secret never reaches the digest input that gets hashed.
    assert secret not in json.dumps(digest_input)


# ===========================================================================
# 2. orphaned temp-file reclamation
# ===========================================================================


def test_sweep_reclaims_an_orphaned_temp_file_from_an_interrupted_write(tmp_path):
    """SIGKILL between mkstemp and os.replace leaves no owner and no TTL record."""
    orphan = tmp_path / f".{cache_key(PARAMS)}-abcdefgh.tmp"
    orphan.write_text("half written", encoding="utf-8")
    old = time.time() - ORPHAN_MIN_AGE_SECONDS - 60
    os.utime(orphan, (old, old))

    result = sweep_media_cache(tmp_path, force=True)

    assert result["orphans"] == 1
    assert result["deleted"] == 1
    assert not orphan.exists()


def test_sweep_leaves_a_fresh_temp_file_alone(tmp_path):
    """A slow in-flight write must not be reclaimed under its own writer."""
    orphan = tmp_path / f".{cache_key(PARAMS)}-live.tmp"
    orphan.write_text("still writing", encoding="utf-8")
    assert sweep_media_cache(tmp_path, force=True)["orphans"] == 0
    assert orphan.exists()


def test_sweep_spares_a_temp_file_whose_mtime_is_a_moment_in_the_future(tmp_path):
    """Regression: a sub-second future mtime must not read as 'invalid'.

    Filesystem timestamp granularity is coarser than the system clock, so a
    temp file created 'now' can report an mtime a fraction of a microsecond
    AHEAD of ``time.time()``. The naive ``0 <= age`` freshness test then
    evaluates False and the sweep deletes a temp file belonging to a writer
    that is still running. This reproduced roughly half the time in CI on
    Windows, which is exactly the kind of bug that gets dismissed as flake.
    """
    orphan = tmp_path / f".{cache_key(PARAMS)}-racing.tmp"
    orphan.write_text("still writing", encoding="utf-8")
    # Force the artefact deterministically instead of hoping to race the clock.
    just_ahead = time.time() + 0.05
    os.utime(orphan, (just_ahead, just_ahead))

    assert sweep_media_cache(tmp_path, force=True)["orphans"] == 0
    assert orphan.exists(), "a live writer's temp file was deleted"


def test_sweep_still_reclaims_a_genuinely_future_dated_temp_file(tmp_path):
    """The tolerance must not weaken real clock-skew detection."""
    orphan = tmp_path / f".{cache_key(PARAMS)}-skewed.tmp"
    orphan.write_text("restored from a backup", encoding="utf-8")
    far_future = time.time() + 5 * MTIME_FUTURE_TOLERANCE_SECONDS
    os.utime(orphan, (far_future, far_future))

    assert sweep_media_cache(tmp_path, force=True)["orphans"] == 1
    assert not orphan.exists()


def test_sweep_does_not_delete_files_it_did_not_create(tmp_path):
    """An operator's own file in the directory is not ours to remove."""
    foreign = tmp_path / "my-notes.txt"
    foreign.write_text("keep me", encoding="utf-8")
    old = time.time() - 10 * MEDIA_CACHE_TTL_SECONDS
    os.utime(foreign, (old, old))
    sweep_media_cache(tmp_path, force=True)
    assert foreign.exists()


def test_sweep_collects_orphans_and_expired_entries_together(tmp_path):
    """Both patterns are reclaimed, and stats split them by kind."""
    write_media_cache(tmp_path, PARAMS, [_item()])
    entry = tmp_path / f"{cache_key(PARAMS)}.json"
    old = time.time() - MEDIA_CACHE_TTL_SECONDS - 60
    os.utime(entry, (old, old))
    _orphan(tmp_path, cache_key(dict(PARAMS, search_term="other")))

    before = cache_stats(tmp_path)
    assert before["entries"] == 1 and before["orphans"] == 1

    result = sweep_media_cache(tmp_path, force=True)
    assert result["deleted"] == 2 and result["orphans"] == 1
    assert cache_stats(tmp_path) == {"entries": 0, "entry_bytes": 0,
                                     "orphans": 0, "orphan_bytes": 0}


def test_sweep_is_rate_limited_unless_forced(tmp_path):
    """A busy search loop must not pay a linear directory walk every time."""
    assert sweep_media_cache(tmp_path, force=True)["skipped"] is False
    _orphan(tmp_path)
    assert sweep_media_cache(tmp_path)["skipped"] is True


# ===========================================================================
# 3. atomic write
# ===========================================================================


def test_a_write_never_leaves_a_partial_file_at_the_final_path(tmp_path):
    write_media_cache(tmp_path, PARAMS, [_item()])
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == [f"{cache_key(PARAMS)}.json"], files
    payload = json.loads((tmp_path / files[0]).read_text(encoding="utf-8"))
    assert payload["version"] == media_cache.CACHE_FORMAT_VERSION
    assert payload["items"][0]["url"].endswith(".mp4")


def test_a_failed_write_leaves_no_published_entry_and_no_temp_file(tmp_path, monkeypatch):
    """A publication failure must not publish a partial payload or leak a temp."""

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(media_cache.os, "replace", boom)
    with pytest.raises(OSError):
        write_media_cache(tmp_path, PARAMS, [_item()])
    assert not (tmp_path / f"{cache_key(PARAMS)}.json").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_the_payload_is_fsync_ed_before_it_is_published(tmp_path, monkeypatch):
    """fsync BEFORE os.replace, so a power loss cannot leave a zero-length entry.

    Asserted on the call ORDER rather than on timing: without the fsync a crash
    between the two calls publishes an entry whose bytes never reached the disk,
    and the cache would then serve a truncated payload as a hit.
    """
    order: list[str] = []
    real_fsync = media_cache.os.fsync
    real_replace = media_cache.os.replace

    def tracking_fsync(fd):
        order.append("fsync")
        return real_fsync(fd)

    def tracking_replace(src, dst):
        order.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(media_cache.os, "fsync", tracking_fsync)
    monkeypatch.setattr(media_cache.os, "replace", tracking_replace)
    write_media_cache(tmp_path, PARAMS, [_item()])
    assert order == ["fsync", "replace"], order


def test_a_concurrent_reader_never_observes_a_partial_entry(tmp_path):
    """os.replace is atomic: a reader sees the whole entry or nothing.

    On Windows a replace can transiently fail while a reader holds the file, so
    an unreadable attempt counts as "nothing", not as corruption. The invariant
    asserted here is that NO read ever returns a truncated or malformed set.
    """
    stop = threading.Event()
    observed: list[list[str]] = []
    unreadable = 0
    guard = threading.Lock()

    def reader():
        nonlocal unreadable
        while not stop.is_set():
            try:
                payload = json.loads(
                    (tmp_path / f"{cache_key(PARAMS)}.json").read_text(encoding="utf-8"))
            except OSError:
                with guard:
                    unreadable += 1
                continue
            except ValueError as exc:          # a truncated write would land here
                raise AssertionError(f"reader observed a truncated payload: {exc}") from exc
            with guard:
                observed.append([i["url"] for i in payload["items"]])

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for index in range(1, 25):
            write_media_cache(tmp_path, PARAMS,
                              [_item(f"https://cdn.test/{index}.mp4")])
            time.sleep(0.002)
    finally:
        stop.set()
        thread.join(timeout=10)

    assert observed, "reader never observed a complete entry"
    assert all(len(urls) == 1 for urls in observed), observed
    assert all(url.endswith(".mp4") for urls in observed for url in urls), observed


# ===========================================================================
# 4. TTL on mtime; a NEGATIVE age is invalid
# ===========================================================================


def test_a_future_mtime_is_invalid_not_infinitely_fresh(tmp_path):
    """Clock skew / a restored backup must NOT read as fresh forever."""
    write_media_cache(tmp_path, PARAMS, [_item()])
    entry = tmp_path / f"{cache_key(PARAMS)}.json"
    future = time.time() + 10 * 365 * 24 * 3600
    os.utime(entry, (future, future))

    assert is_fresh(entry, time.time()) is False
    assert read_media_cache(tmp_path, PARAMS) is None
    assert not entry.exists(), "an invalid entry must be dropped, not kept"


def test_an_expired_entry_is_a_miss_and_is_dropped(tmp_path):
    write_media_cache(tmp_path, PARAMS, [_item()])
    entry = tmp_path / f"{cache_key(PARAMS)}.json"
    old = time.time() - MEDIA_CACHE_TTL_SECONDS - 1
    os.utime(entry, (old, old))
    assert read_media_cache(tmp_path, PARAMS) is None
    assert not entry.exists()


def test_ttl_is_measured_on_mtime_so_a_rewrite_refreshes_the_entry(tmp_path):
    write_media_cache(tmp_path, PARAMS, [_item("https://cdn.test/a.mp4")])
    entry = tmp_path / f"{cache_key(PARAMS)}.json"
    old = time.time() - MEDIA_CACHE_TTL_SECONDS - 1
    os.utime(entry, (old, old))
    assert read_media_cache(tmp_path, PARAMS) is None

    write_media_cache(tmp_path, PARAMS, [_item("https://cdn.test/b.mp4")])
    items = read_media_cache(tmp_path, PARAMS)
    assert items is not None
    assert items[0]["url"].endswith("/b.mp4")


def test_an_older_format_version_is_discarded_not_parsed(tmp_path):
    path = tmp_path / f"{cache_key(PARAMS)}.json"
    path.write_text(json.dumps({"version": 1, "items": [_item()]}), encoding="utf-8")
    assert read_media_cache(tmp_path, PARAMS) is None
    assert not path.exists()


def test_a_corrupt_payload_is_deleted_rather_than_served_as_no_matches(tmp_path):
    path = tmp_path / f"{cache_key(PARAMS)}.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_media_cache(tmp_path, PARAMS) is None
    assert not path.exists()


# ===========================================================================
# 5. EMPTY results are NEVER cached
# ===========================================================================


def test_an_empty_result_is_never_written(tmp_path):
    assert write_media_cache(tmp_path, PARAMS, []) is False
    assert read_media_cache(tmp_path, PARAMS) is None
    assert not list(tmp_path.iterdir())


def test_an_empty_result_does_not_block_the_next_attempt(tmp_path):
    """The fault-signal overload: [] must not be pinned for a full TTL."""
    calls: list[int] = []

    def flaky():
        calls.append(1)
        return [] if len(calls) == 1 else [_item()]

    first = search_media_cached(tmp_path, PARAMS, flaky)
    assert first == []
    second = search_media_cached(tmp_path, PARAMS, flaky)

    assert [i["url"] for i in second] == ["https://cdn.test/clip.mp4"]
    assert len(calls) == 2, "the empty result was cached and replayed"


def test_an_all_invalid_result_is_not_written(tmp_path):
    assert write_media_cache(tmp_path, PARAMS, [{"url": ""}, {"no_url": 1}]) is False
    assert read_media_cache(tmp_path, PARAMS) is None


# ===========================================================================
# 6. credential-bound signed URLs are NEVER cached
# ===========================================================================


@pytest.mark.parametrize("url", [
    "https://cdn.test/clip.mp4?token=abc123",
    "https://cdn.test/clip.mp4?X-Amz-Signature=deadbeef&X-Amz-Expires=900",
    "https://cdn.test/clip.mp4?Policy=eyJ&Signature=s",
    "https://user:pass@cdn.test/clip.mp4",
    "https://cdn.test/clip.mp4?st=1234&se=1235&sp=allow",
    "https://cdn.test/clip.mp4?hdnts=exp%3D1234",
    "ftp://cdn.test/clip.mp4",
    "",
])
def test_signed_or_credentialed_urls_are_detected(url: str):
    assert is_signed_url(url) is True


@pytest.mark.parametrize("url", [
    "https://cdn.test/clip.mp4",
    "https://cdn.test/clip.mp4?quality=hd",
    "https://www.pexels.com/video/12345/",
])
def test_plain_public_urls_are_not_flagged(url: str):
    assert is_signed_url(url) is False


def test_a_signed_url_result_is_not_cached_even_alongside_good_items(tmp_path):
    """One signed item would put a live credential in a persisted file."""
    assert write_media_cache(
        tmp_path, PARAMS,
        [_item("https://cdn.test/ok.mp4"), _item("https://cdn.test/x.mp4?token=leak")],
    ) is False
    assert read_media_cache(tmp_path, PARAMS) is None
    assert not list(tmp_path.iterdir())


def test_coverr_is_never_cached_at_all(tmp_path):
    """The donor hard-disables Coverr for the same reason; it stays disabled."""
    coverr = dict(PARAMS, provider="coverr")
    assert provider_disables_cache("coverr") is True
    assert write_media_cache(tmp_path, coverr, [_item()]) is False
    assert not list(tmp_path.iterdir())


def test_a_signed_provider_result_is_never_written(tmp_path):
    """The enforcement point is the WRITE: a signed provider's items never land."""
    coverr = dict(PARAMS, provider="coverr")
    assert write_media_cache(tmp_path, coverr,
                             [{"provider": "coverr", "url": "https://cdn.test/x.mp4"}]) is False
    assert not list(tmp_path.iterdir())


def test_a_tracking_query_is_stripped_from_a_cached_source_page(tmp_path):
    """Provenance is worth keeping; a tracking token inside it is not."""
    assert write_media_cache(tmp_path, PARAMS, [_item(source_info={
        "provider": "pexels", "asset_id": "abc",
        "source_page": "https://www.pexels.com/video/9/?utm_source=cache&token=leak",
    })]) is True
    page = json.loads((tmp_path / f"{cache_key(PARAMS)}.json").read_text(
        encoding="utf-8"))["items"][0]["source_info"]["source_page"]
    assert page == "https://www.pexels.com/video/9/"


def test_public_page_url_drops_the_query_and_refuses_userinfo():
    """A tracking query is stripped; a URL carrying credentials is refused
    outright rather than rewritten into something that looks trustworthy."""
    assert public_page_url("https://x.test/a/b?token=1#frag") == "https://x.test/a/b"
    assert public_page_url("https://u:p@x.test/a/b?token=1") == ""
    assert public_page_url("not-a-url") == ""
    assert public_page_url("") == ""


# ===========================================================================
# 7. re-validate immediately before unlink; never reuse a scan as fresh truth
# ===========================================================================


def test_delete_refuses_a_path_that_escaped_the_managed_directory(tmp_path):
    """A scan snapshot is not permission: the parent is re-checked at delete."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / f".{cache_key(PARAMS)}-x.tmp"
    victim.write_text("not ours", encoding="utf-8")
    old = time.time() - ORPHAN_MIN_AGE_SECONDS - 60
    os.utime(victim, (old, old))

    assert media_cache._revalidate_before_unlink(victim, cache_dir, victim.name) is False
    assert victim.exists()


def test_delete_refuses_a_name_that_does_not_match_our_patterns(tmp_path):
    assert media_cache._revalidate_before_unlink(tmp_path / "x.txt", tmp_path, "x.txt") is False


@requires_symlinks
def test_delete_refuses_a_symlink(tmp_path):
    """Following a link out of the managed directory would delete another file."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    target = tmp_path / "precious.txt"
    target.write_text("do not delete", encoding="utf-8")
    link = cache_dir / f".{cache_key(PARAMS)}-link.tmp"
    os.symlink(target, link)
    old = time.time() - ORPHAN_MIN_AGE_SECONDS - 60
    os.utime(link, (old, old))
    assert media_cache._revalidate_before_unlink(link, cache_dir, link.name) is False
    assert target.exists()


@requires_symlinks
def test_the_sweep_leaves_a_symlinked_orphan_alone(tmp_path):
    """End-to-end: a symlink in the cache dir is never unlinked."""
    target = tmp_path / "precious.txt"
    target.write_text("do not delete", encoding="utf-8")
    link = tmp_path / f".{cache_key(PARAMS)}-link.tmp"
    os.symlink(target, link)
    old = time.time() - ORPHAN_MIN_AGE_SECONDS - 60
    os.utime(link, (old, old))
    sweep_media_cache(tmp_path, force=True)
    assert target.exists()
    assert link.is_symlink()


def test_delete_refuses_a_symlink_even_when_its_parent_looks_right(tmp_path, monkeypatch):
    """The link check is exercised WITHOUT needing OS symlink privileges.

    Windows denies ``os.symlink`` without elevation, so the OS-level cases above
    are skipped on many dev machines. Stubbing the filesystem's own answer is
    what makes this guard provable everywhere: if the ``is_symlink`` branch is
    deleted from the re-validation, this fails on any host.
    """
    entry = tmp_path / f".{cache_key(PARAMS)}-link.tmp"
    entry.write_text("payload", encoding="utf-8")
    monkeypatch.setattr(Path, "is_symlink", lambda self: True)
    assert media_cache._revalidate_before_unlink(entry, tmp_path, entry.name) is False


def test_delete_refuses_a_renamed_entry(tmp_path):
    """The name is re-checked at delete time, not trusted from the scan."""
    entry = tmp_path / f"{cache_key(PARAMS)}.json"
    entry.write_text("{}", encoding="utf-8")
    renamed = tmp_path / f".{cache_key(PARAMS)}-renamed.tmp"
    assert media_cache._revalidate_before_unlink(entry, tmp_path, renamed.name) is False


def test_delete_refuses_when_the_entry_disappeared_between_scan_and_delete(tmp_path):
    entry = tmp_path / f"{cache_key(PARAMS)}.json"
    entry.write_text("{}", encoding="utf-8")
    entry.unlink()
    assert media_cache._revalidate_before_unlink(entry, tmp_path, entry.name) is False


def test_delete_accepts_a_genuine_entry_inside_the_managed_directory(tmp_path):
    """The re-validation must not refuse a legitimate delete (false safety)."""
    entry = tmp_path / f"{cache_key(PARAMS)}.json"
    entry.write_text("{}", encoding="utf-8")
    assert media_cache._revalidate_before_unlink(entry, tmp_path, entry.name) is True


# ===========================================================================
# 8. bounded lock shards + double-checked read
# ===========================================================================


def test_lock_shards_are_bounded_and_stable():
    """256 shards, never a lock per keyword (that would grow without bound)."""
    assert len(media_cache._CACHE_LOCKS) == 256
    shard = media_cache.lock_for(PARAMS)
    assert media_cache.lock_for(dict(PARAMS)) is shard
    assert media_cache.lock_for(dict(PARAMS, search_term="other")) in media_cache._CACHE_LOCKS


def test_n_concurrent_identical_searches_issue_exactly_one_remote_request(tmp_path):
    """The whole point of the double-checked read: N workers, ONE remote call."""
    write_media_cache(tmp_path, PARAMS, [_item()])   # pre-populated
    barrier = threading.Barrier(12)
    calls: list[int] = []
    lock = threading.Lock()

    def fetch():
        with lock:
            calls.append(1)
        return [_item()]

    results: list[list[dict]] = []

    def worker():
        barrier.wait()
        results.append(search_media_cached(tmp_path, PARAMS, fetch))

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 12
    assert calls == [], "a cache hit must not trigger a remote request"
    assert all(len(r) == 1 for r in results)


def test_concurrent_misses_for_the_same_key_collapse_to_one_request(tmp_path):
    """Start with an EMPTY cache and let the races actually happen."""
    barrier = threading.Barrier(8)
    calls: list[int] = []
    lock = threading.Lock()

    def slow_fetch():
        with lock:
            calls.append(1)
        time.sleep(0.05)
        return [_item()]

    results: list[list[dict]] = []

    def worker():
        barrier.wait()
        results.append(search_media_cached(tmp_path, PARAMS, slow_fetch))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 8
    assert all(r and r[0]["url"].endswith(".mp4") for r in results)
    assert len(calls) == 1, f"expected one shared request, got {len(calls)}"


def test_different_keys_do_not_share_a_shard_lock():
    """Different terms must not serialise on each other."""
    one = media_cache.lock_for(dict(PARAMS, search_term="one"))
    two = media_cache.lock_for(dict(PARAMS, search_term="two"))
    assert one is not two


def test_missing_cache_directory_is_a_miss_not_an_error(tmp_path):
    missing = tmp_path / "does-not-exist-yet"
    assert read_media_cache(missing, PARAMS) is None


def test_stats_on_a_missing_directory_report_zero(tmp_path):
    assert cache_stats(tmp_path / "nope") == {"entries": 0, "entry_bytes": 0,
                                              "orphans": 0, "orphan_bytes": 0}


def test_round_trip_survives_a_process_style_rescan(tmp_path):
    """Re-scan at execution time: a second sweep sees the same state, not stale."""
    write_media_cache(tmp_path, PARAMS, [_item()])
    assert sweep_media_cache(tmp_path, force=True)["deleted"] == 0
    assert read_media_cache(tmp_path, PARAMS) is not None
    assert len(list(tmp_path.iterdir())) == 1