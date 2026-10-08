"""Provider media-search result cache: correctness around credentials, TTLs,
interrupted writes and concurrency.

A media/provider search is a *remote request whose answer is not the media*.
Caching it is safe only when four separate claims hold, and each one is a real
failure mode someone already shipped:

1. **The cache key excludes the API key.** A key authenticates; it does not
   change a public catalog's result set. Including it would fragment the cache
   per rotation and write a secret into a persisted digest input. The key is the
   canonical-JSON sha256 of the *result-affecting* parameters only -- the same
   shape ``services/media_intel_runs.py::cache_key`` already uses for
   ``media_intel_cache`` (``UNIQUE (workspace_id, cache_key)`` + a sha256
   provenance key), so the two caches agree on what a key means.

2. **Orphaned temp files are reclaimed.** A save writes to a same-directory
   temp file and publishes with ``os.replace``. Ctrl+C, a container stop or a
   power loss skip Python's cleanup handlers, so the temp file survives with no
   owner. It never matches the published-file pattern, so a TTL sweep over the
   cache pattern alone would skip it forever and it would accumulate for the
   life of the volume. :func:`sweep_media_cache` therefore recognises *this
   module's own* temp names explicitly.

3. **A write is atomic.** Same-directory temp + ``flush`` + ``fsync`` +
   ``os.replace``. A reader sees the complete old file or the complete new file,
   never a truncated one.

4. **TTL is measured on mtime, and a NEGATIVE age is invalid.** Clock skew or a
   file restored from a backup can carry a future mtime. Treating ``now - mtime
   < 0`` as "infinitely fresh" is a bug, not safety, so a future-dated entry is
   dropped and refetched.

Two more guards that only exist because the alternative is silently wrong:

* **An EMPTY result is never cached.** ``[]`` overloads "the catalog genuinely
  has no match" with "the request failed". Caching a transient fault for a full
  TTL blocks every later attempt with the same terms for a day. Only a
  non-empty, well-formed result is stored.
* **A credential-bound signed URL is never cached.** Such a URL embeds a key
  and an expiry; replaying it later either leaks the credential or 403s. The
  donor hard-disables Coverr for exactly this reason; here it is a general
  detector over the item payload, plus a provider denylist for providers known
  to sign.

Cross-process integrity comes from the temp+replace dance; in-process
concurrency comes from **256 bounded lock shards** (never a lock per keyword,
which would grow without bound) and a double-checked read so N concurrent tasks
asking for the same terms share ONE remote request.

Ported from MoneyPrinterTurbo 1.3.7 (MIT, Copyright (c) 2024 Harry). The donor's
online material-search cache supplied the key/TTL/temp-reclaim/atomic-write
shapes; the signed-URL rule is generalised and the workspace scoping, empty-
result and skew handling are re-derived for YMONEY's storage layout.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote, unquote_plus, urlsplit

from app.services.media_intel_runs import canonical_params

logger = logging.getLogger("ymoney.intel")

#: Search results go stale fast enough that a day is generous; the TTL is the
#: ceiling on how long a wrong answer can survive, not a freshness target.
MEDIA_CACHE_TTL_SECONDS = 24 * 60 * 60
#: Bumped when the on-disk payload shape changes; an older file is discarded
#: rather than parsed with the wrong schema.
CACHE_FORMAT_VERSION = 2
#: The sweep is O(dir) so it runs on a low-frequency timer, not per search.
CACHE_SWEEP_INTERVAL_SECONDS = 60 * 60
#: An orphaned temp younger than this may belong to a writer that is still
#: alive, so it is left alone.
ORPHAN_MIN_AGE_SECONDS = 60 * 60

#: How far an mtime may sit in the future before it stops being a rounding
#: artefact and is treated as real clock skew. Filesystem timestamp granularity
#: is coarser than the system clock, so a file created "now" routinely reports
#: an mtime a fraction of a millisecond ahead. A whole second of headroom is
#: generous for that and still far below any meaningful skew.
MTIME_FUTURE_TOLERANCE_SECONDS = 1.0

#: A published cache file: the sha256 cache key plus the format suffix.
_CACHE_FILE_PATTERN = re.compile(r"^[0-9a-f]{64}\.json$")
#: This module's own in-progress writes: ".<cache key stem>-<random>.tmp".
#: A SIGKILL between mkstemp and os.replace leaves these behind permanently,
#: so they must be recognised by NAME -- they never match the published pattern.
_CACHE_TEMP_FILE_PATTERN = re.compile(r"^\.[0-9a-f]{64}-[a-z0-9_]+\.tmp$")
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_MAX_NESTED_URL_DECODE_PASSES = 8

#: 256 shards. Bounded memory (no lock per keyword) while still collapsing the
#: N-workers-same-keyword case onto one shard, with a double-checked read.
_CACHE_LOCKS: tuple[threading.Lock, ...] = tuple(threading.Lock() for _ in range(256))
_sweep_state_lock = threading.Lock()
_last_sweep_monotonic: float | None = None

#: Providers whose download URLs are credential-bound signed URLs. They are
#: refused as cache WRITES; an entry left by an older version is also removed on
#: read so a stale signed URL cannot be served.
SIGNED_URL_PROVIDERS: frozenset[str] = frozenset({"coverr"})

#: Parameter names that mark a URL as signed / credential-bearing. Matched
#: case-insensitively against query parameters and OAuth-style URL fragments.
_SIGNED_QUERY_KEYS: frozenset[str] = frozenset({
    "amz-signature", "x-amz-signature", "x-amz-credential", "x-amz-algorithm",
    "x-goog-signature", "x-goog-credential", "gcp-signature", "signature",
    "sig", "token", "access_token", "refresh_token", "oauth_token", "id_token", "jwt", "auth", "authorization",
    "key", "apikey", "api_key", "policy", "expires", "se", "st", "sk", "sp",
    "hdnts", "x-amz-date", "x-amz-expires", "download_token", "stoken",
})

_MALFORMED_PERCENT_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")


def _normalize_parameter_name(name: str) -> str:
    """Normalize parameter-name casing and separators before credential checks."""
    value = str(name or "").strip()
    # Split both ordinary camelCase and acronym-to-word boundaries, then apply
    # the same underscore convention used by the credential hints.
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return value.lower().replace("-", "_")


_NORMALIZED_SIGNED_QUERY_KEYS = frozenset(
    _normalize_parameter_name(key) for key in _SIGNED_QUERY_KEYS
)


class MediaCacheError(RuntimeError):
    """Raised only for programming errors; cache I/O failures degrade to None."""


# ---------------------------------------------------------------------------
# 1. cache key -- result-affecting params ONLY, never the credential
# ---------------------------------------------------------------------------

#: Parameters that authenticate a request and therefore MUST NOT reach the key.
#: Their presence is not an error (the caller may pass a whole params dict); the
#: key simply ignores them, exactly as the donor ignored its configured API key.
_CREDENTIAL_PARAM_HINTS: tuple[str, ...] = (
    "api_key", "apikey", "api-key", "key", "token", "access_token", "secret",
    "secret_key", "client_secret", "password", "authorization", "auth",
    "bearer", "credential", "credentials", "session", "jwt",
)

#: The fields that actually change what a provider returns.
RESULT_AFFECTING_FIELDS: tuple[str, ...] = (
    "provider", "search_term", "minimum_duration", "aspect", "media_type",
    "per_page", "locale", "orientation", "model_version",
)


def is_credential_param(name: str) -> bool:
    """True when a parameter name carries a secret rather than a result."""
    key = _normalize_parameter_name(name)
    if not key:
        return False
    if key in _CREDENTIAL_PARAM_HINTS:
        return True
    return any(key.endswith(f"_{hint}") or key.startswith(f"{hint}_")
               for hint in _CREDENTIAL_PARAM_HINTS)


def result_affecting_params(params: dict | None) -> dict:
    """Drop every credential-bearing key from a caller-supplied params dict.

    Keeps only keys on :data:`RESULT_AFFECTING_FIELDS`, so a caller can hand
    over its whole request config -- including the API key -- and still get a
    key that identifies the *result*, not the identity that fetched it.
    """
    source = params if isinstance(params, dict) else {}
    kept: dict = {}
    # Known result fields first, so the digest input is stable and readable.
    for field in RESULT_AFFECTING_FIELDS:
        if field in source:
            kept[field] = source[field]
    # Any other non-credential field still changes the answer, so it stays;
    # anything that looks like a secret is removed regardless of its name.
    for field, value in source.items():
        name = str(field)
        if name in kept or is_credential_param(name):
            continue
        kept[name] = value
    return kept


def cache_key(params: dict | None) -> str:
    """sha256 over the canonical JSON of the result-affecting params.

    Same construction as ``media_intel_runs.cache_key``: canonical JSON with
    sorted keys and fixed separators, then sha256, so key ORDER in the input
    dict can never change the digest and the digest is a fixed 64 chars.
    """
    return hashlib.sha256(
        canonical_params(result_affecting_params(params)).encode("utf-8")
    ).hexdigest()


def cache_path(cache_dir: Path, params: dict | None) -> Path:
    """The published file for ``params``. The key -- never the term -- names it."""
    return Path(cache_dir) / f"{cache_key(params)}.json"


def lock_for(params: dict | None) -> threading.Lock:
    """One of 256 shards for these params: N workers, ONE remote request."""
    digest = cache_key(params)
    return _CACHE_LOCKS[int(digest[:8], 16) % len(_CACHE_LOCKS)]


# ---------------------------------------------------------------------------
# 6. credential-bound signed URLs are never cached
# ---------------------------------------------------------------------------


def is_signed_url(url: str) -> bool:
    """True when a URL carries a credential/expiry in its query, fragment or userinfo.

    A signed URL is single-use by construction: the signature authorises one
    fetch and expires. Replaying it from a cache is either a 403 or a leak of
    the embedded credential into a persisted file.
    """
    raw = str(url or "").strip()
    if not raw:
        return True
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return True
    if parsed.scheme not in ("http", "https"):
        return True
    if parsed.username is not None or parsed.password is not None:
        return True

    def has_credential_key(params: str) -> bool:
        for pair in params.split("&"):
            raw_key = pair.split("=", 1)[0].strip()
            if not raw_key:
                continue
            # Treat malformed escapes and invalid UTF-8 as unsafe rather than
            # allowing an undecodable credential name into the persistent cache.
            if _MALFORMED_PERCENT_ESCAPE.search(raw_key):
                return True
            try:
                key = unquote_plus(raw_key, errors="strict").strip()
            except UnicodeDecodeError:
                return True
            normalized_key = _normalize_parameter_name(key)
            if (key.lower() in _SIGNED_QUERY_KEYS
                    or normalized_key in _NORMALIZED_SIGNED_QUERY_KEYS
                    or is_credential_param(key)):
                return True
        return False

    if has_credential_key(parsed.query):
        return True

    # Browsers and OAuth providers also place tokens in fragments. A fragment
    # may be a hash route with a query after `?`, including an encoded `%3F`.
    decoded_fragment = unquote(parsed.fragment)
    candidates = {parsed.fragment, decoded_fragment}
    for candidate in tuple(candidates):
        candidates.update(
            candidate[index + 1:]
            for index, char in enumerate(candidate)
            if char == "?"
        )
    if any(has_credential_key(candidate) for candidate in candidates):
        return True
    return False


def provider_disables_cache(provider: str) -> bool:
    """True for providers whose results must never touch disk (signed URLs)."""
    return str(provider or "").strip().lower() in SIGNED_URL_PROVIDERS


def public_page_url(value: str) -> str:
    """Strip query + userinfo from a PUBLIC page URL, or return "".

    A source-page reference is provenance and is worth keeping; its query string
    frequently carries a tracking token, which is not.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    host = parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}{parsed.path}"


def _contains_signed_url(value: object) -> bool:
    """Find credential-bearing URLs nested or repeatedly encoded in metadata.

    Decoding is bounded to avoid unbounded work on hostile cache data. A value
    that remains encoded beyond the limit is rejected conservatively rather
    than persisted without knowing whether another layer hides a signed URL.
    """
    if isinstance(value, dict):
        return any(_contains_signed_url(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_signed_url(child) for child in value)
    if not isinstance(value, str):
        return False

    candidates = {value, unquote(value), unquote_plus(value)}
    for candidate in candidates:
        text = candidate
        for depth in range(_MAX_NESTED_URL_DECODE_PASSES + 1):
            for match in _URL_IN_TEXT.finditer(text):
                url = match.group().rstrip(".,;:!?)\\]}")
                if is_signed_url(url):
                    return True

            decoded = unquote(text)
            if decoded == text:
                break
            if depth == _MAX_NESTED_URL_DECODE_PASSES:
                return True
            text = decoded
    return False


def _has_unsafe_source_info(value: object) -> bool:
    """Reject legacy metadata with secret-shaped keys or URLs at any depth."""
    if isinstance(value, dict):
        return any(
            is_credential_param(str(key)) or _has_unsafe_source_info(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_has_unsafe_source_info(child) for child in value)
    return _contains_signed_url(value)


# ---------------------------------------------------------------------------
# 3./4. payload validation, TTL on mtime, atomic write
# ---------------------------------------------------------------------------


def _cache_age(path: Path, now: float) -> float | None:
    """Seconds since mtime, or ``None`` when the file cannot be inspected."""
    try:
        return float(now) - path.stat().st_mtime
    except OSError:
        return None


def is_fresh(path: Path, now: float, ttl_seconds: float = MEDIA_CACHE_TTL_SECONDS) -> bool:
    """True only when the entry is present AND its mtime age is in [0, ttl).

    A NEGATIVE age (future mtime from clock skew or a restored backup) is
    invalid, not fresh. Age is measured on mtime, so a rewrite refreshes the
    entry without touching the payload.
    """
    age = _cache_age(path, now)
    if age is None:
        return False
    return 0 <= age < float(ttl_seconds)


def remove_entry(path: Path) -> bool:
    """Best-effort delete of ONE entry; never raises into the search path."""
    try:
        Path(path).unlink(missing_ok=True)
        return True
    except OSError as exc:
        logger.warning("media cache: failed to remove %s (%s)", Path(path).name, exc)
        return False


def read_media_cache(cache_dir: Path, params: dict | None, *,
                     now: float | None = None,
                     ttl_seconds: float = MEDIA_CACHE_TTL_SECONDS
                     ) -> list[dict] | None:
    """Return the cached items, or ``None`` for a miss.

    ``None`` means "ask the provider". An EMPTY list is never returned: a stored
    empty result is not a valid cache entry (see :func:`write_media_cache`), and
    a payload that fails validation is deleted rather than returned, so a
    corrupt file can never masquerade as "no matches".
    """
    moment = time.time() if now is None else float(now)
    path = cache_path(cache_dir, params)
    provider = str((params or {}).get("provider") or "")
    if provider_disables_cache(provider):
        remove_entry(path)
        return None
    if not is_fresh(path, moment, ttl_seconds):
        remove_entry(path)
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        # A read failure is NOT proof of corruption -- on Windows a concurrent
        # publish makes the entry temporarily unreadable. Treat it as a miss and
        # do NOT delete: destroying a valid entry on a transient error would turn
        # a sharing hiccup into silent cache loss.
        logger.warning("media cache: entry %s not readable (%s)", path.name, exc)
        return None
    except ValueError as exc:
        logger.warning("media cache: corrupt entry %s (%s)", path.name, exc)
        remove_entry(path)
        return None
    if not isinstance(payload, dict) or payload.get("version") != CACHE_FORMAT_VERSION:
        remove_entry(path)
        return None
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        # Defensive: an empty/stale-shape entry is discarded, never served.
        remove_entry(path)
        return None
    for item in items:
        if not isinstance(item, dict) or not str(item.get("url") or "").strip():
            remove_entry(path)
            return None
        item_provider = str(item.get("provider") or "")
        if (is_signed_url(item["url"]) or provider_disables_cache(item_provider)
                or _contains_signed_url(item)
                or _has_unsafe_source_info(item.get("source_info"))):
            remove_entry(path)
            return None
    return items


def _atomic_write(path: Path, payload: dict) -> None:
    """Serialise to a same-dir temp file, fsync it, then ``os.replace``.

    ``os.replace`` is atomic within a filesystem, so a concurrent reader sees
    either the complete previous file or the complete new one -- never the
    half-written temp. ``fsync`` before the replace means the bytes survive a
    power loss, so a cache hit after a crash is never a truncated payload.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # delete=False is required: the temp file must outlive this function so it
    # can be fsync-ed and then moved into place by os.replace. It IS closed via
    # the `with handle:` block below -- the temporary path is what escapes.
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed explicitly below
        mode="w", encoding="utf-8", dir=target.parent,
        prefix=f".{target.stem}-", suffix=".tmp", delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        _publish_replace(temp_path, target)
    except BaseException:
        remove_entry(temp_path)
        raise


#: Windows denies replacing a file while another handle has it open for
#: reading, so a concurrent reader can make one replace fail with a sharing
#: violation. That is transient, not corruption: retry briefly, then give up.
_REPLACE_ATTEMPTS = 20
_REPLACE_BACKOFF_SECONDS = 0.005


def _publish_replace(temp_path: Path, target: Path) -> None:
    """``os.replace`` with a bounded retry for a transient sharing violation."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(temp_path, target)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS * (attempt + 1))


def write_media_cache(cache_dir: Path, params: dict | None,
                      items: Iterable[dict]) -> bool:
    """Cache a NON-EMPTY, credential-free result set. Returns True when stored.

    Refuses, with a reason logged rather than an exception:

    * an EMPTY or all-invalid item list (5) -- a transient provider fault must
      not be pinned for a whole TTL;
    * a provider on :data:`SIGNED_URL_PROVIDERS` (6);
    * ANY item whose URL is credential-bound or signed (6) -- a single signed
      item would put a live credential in a persisted file, so the whole write
      is refused rather than filtered to a partial set.
    """
    provider = str((params or {}).get("provider") or "")
    if provider_disables_cache(provider):
        logger.info("media cache: provider %r issues signed URLs; not caching", provider)
        return False

    serialised: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url or is_signed_url(url):
            if url:
                logger.info("media cache: refusing a signed/credentialed URL from %r", provider)
            return False
        entry = {k: v for k, v in item.items() if k != "source_info"}
        source_info = item.get("source_info")
        if isinstance(source_info, dict) and source_info:
            # Keep provenance, but only the public form of any page URL.
            safe_source_info = {
                k: (public_page_url(v) if k in ("source_page", "profile_page") else v)
                for k, v in source_info.items()
                if not is_credential_param(k)
            }
            if _has_unsafe_source_info(safe_source_info):
                logger.info("media cache: refusing credential-bearing source metadata from %r", provider)
                return False
            entry["source_info"] = safe_source_info
        if _contains_signed_url(entry):
            logger.info("media cache: refusing credential-bearing metadata from %r", provider)
            return False
        serialised.append(entry)

    if not serialised:
        # Guard 5: an empty result is a fault signal, not an answer.
        logger.info("media cache: empty result from %r is never cached", provider)
        return False

    sweep_media_cache(cache_dir, force=False)
    _atomic_write(cache_path(cache_dir, params),
                  {"version": CACHE_FORMAT_VERSION, "items": serialised})
    logger.info("media cache: stored %d items for provider %r", len(serialised), provider)
    return True


def search_media_cached(cache_dir: Path, params: dict | None,
                        fetch, *, now: float | None = None,
                        ttl_seconds: float = MEDIA_CACHE_TTL_SECONDS) -> list[dict]:
    """Cached search with a double-checked read around one remote call.

    First read is lock-free. On a miss the shard is taken and the cache is read
    AGAIN: if a sibling worker stored the answer while we waited, that answer is
    used and no request is made. Only the task that still misses calls ``fetch``
    and writes -- so N concurrent identical searches cost ONE remote request.
    """
    cached = read_media_cache(cache_dir, params, now=now, ttl_seconds=ttl_seconds)
    if cached is not None:
        return cached

    with lock_for(params):
        cached = read_media_cache(cache_dir, params, now=now, ttl_seconds=ttl_seconds)
        if cached is not None:
            return cached
        items = list(fetch() or [])
        # A failed/empty fetch returns [] and is NOT written: the next call tries
        # again instead of replaying the fault from cache.
        write_media_cache(cache_dir, params, items)
        return items


# ---------------------------------------------------------------------------
# 2./7. orphaned temp reclamation, with re-validation immediately before unlink
# ---------------------------------------------------------------------------


def _revalidate_before_unlink(path: Path, cache_dir: Path, name: str) -> bool:
    """Re-check at execution time that ``path`` is still OUR file to delete.

    A directory scan is a snapshot: by the time deletion runs, the entry may
    have been replaced, renamed, or turned into a symlink pointing somewhere
    else. Deleting on the scan's word alone would remove a live cache entry or
    follow a link out of the managed directory. So the name pattern, the parent
    directory, the symlink check and the type are all re-checked HERE, and only
    then is ``unlink`` called.
    """
    target = Path(path)
    if target.name != name:
        return False
    if not (_CACHE_FILE_PATTERN.fullmatch(name) or _CACHE_TEMP_FILE_PATTERN.fullmatch(name)):
        return False
    # A link must never be unlinked: unlinking one removes the link, but any
    # path resolved from it escapes the managed directory.
    if target.is_symlink():
        return False
    try:
        if target.resolve().parent != Path(cache_dir).resolve():
            return False
        if not target.is_file():
            return False
    except OSError:
        return False
    return True


def sweep_media_cache(cache_dir: Path, *, now: float | None = None,
                      force: bool = False,
                      ttl_seconds: float = MEDIA_CACHE_TTL_SECONDS) -> dict:
    """Reclaim expired entries AND orphaned temp files. Returns a result dict.

    Two name patterns are collected, and they are reclaimed under the SAME age
    rule so a temp file still being written is never deleted out from under its
    writer:

    * ``<sha256>.json``  -- a published entry past its TTL;
    * ``.<sha256>-*.tmp`` -- an interrupted write. This is the high-value half:
      a SIGKILL between ``mkstemp`` and ``os.replace`` leaves no owner, no
      matching published name and no TTL bookkeeping, so it would otherwise
      accumulate for the life of the volume.

    Only files matching those exact patterns are touched -- an operator's own
    files in the directory are left alone. ``force`` is for tests/explicit
    maintenance; the normal path is rate-limited to one scan per hour so a busy
    search loop does not pay a linear directory walk every time.
    """
    moment = time.time() if now is None else float(now)
    directory = Path(cache_dir)

    global _last_sweep_monotonic
    monotonic_now = time.monotonic()
    with _sweep_state_lock:
        if (not force and _last_sweep_monotonic is not None
                and monotonic_now - _last_sweep_monotonic < CACHE_SWEEP_INTERVAL_SECONDS):
            return {"deleted": 0, "orphans": 0, "failed": 0, "skipped": True}
        _last_sweep_monotonic = monotonic_now

    deleted = orphans = failed = 0
    try:
        entries = os.scandir(directory)
    except OSError as exc:
        logger.warning("media cache: cannot scan %s (%s)", directory, exc)
        return {"deleted": 0, "orphans": 0, "failed": 0, "skipped": False}

    with entries:
        for entry in entries:
            name = entry.name
            is_orphan = bool(_CACHE_TEMP_FILE_PATTERN.fullmatch(name))
            if not (is_orphan or _CACHE_FILE_PATTERN.fullmatch(name)):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                age = float(moment) - entry.stat(follow_symlinks=False).st_mtime
            except OSError as exc:
                logger.warning("media cache: cannot stat %s (%s)", name, exc)
                failed += 1
                continue
            # An orphan is judged by the ORPHAN grace period, NOT the TTL: an
            # interrupted write has no publication time and no reader, so waiting
            # a full TTL for it to expire would keep it for another day. A
            # published entry uses the TTL. Either way a NEGATIVE age (future
            # mtime from skew) is not fresh and IS reclaimed.
            threshold = ORPHAN_MIN_AGE_SECONDS if is_orphan else float(ttl_seconds)
            # A file written a moment ago can carry an mtime a hair in the
            # FUTURE: filesystem timestamp granularity is coarser than the
            # system clock, so `now - mtime` lands a fraction of a microsecond
            # below zero. Treating that as "invalid" would delete a temp file
            # out from under a writer that is still running -- the exact
            # outcome the orphan grace period exists to prevent. Clamp only the
            # sub-second band; a larger negative age is genuine clock skew or a
            # restored backup and stays invalid.
            if -MTIME_FUTURE_TOLERANCE_SECONDS <= age < 0:
                age = 0.0
            if 0 <= age < threshold:
                continue
            # Re-validate against the live filesystem, then delete.
            if not _revalidate_before_unlink(Path(entry.path), directory, name):
                failed += 1
                continue
            if remove_entry(Path(entry.path)):
                orphans += int(is_orphan)
                deleted += 1
            else:
                failed += 1

    if deleted or failed:
        logger.info("media cache sweep: deleted=%d orphans=%d failed=%d",
                    deleted, orphans, failed)
    return {"deleted": deleted, "orphans": orphans, "failed": failed, "skipped": False}


def cache_stats(cache_dir: Path) -> dict:
    """Counts + total bytes, split by entry kind. Never raises."""
    directory = Path(cache_dir)
    stats = {"entries": 0, "entry_bytes": 0, "orphans": 0, "orphan_bytes": 0}
    try:
        scanner = os.scandir(directory)
    except OSError:
        return stats
    with scanner:
        for entry in scanner:
            is_orphan = bool(_CACHE_TEMP_FILE_PATTERN.fullmatch(entry.name))
            if not (is_orphan or _CACHE_FILE_PATTERN.fullmatch(entry.name)):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            if is_orphan:
                stats["orphans"] += 1
                stats["orphan_bytes"] += size
            else:
                stats["entries"] += 1
                stats["entry_bytes"] += size
    return stats


__all__ = [
    "CACHE_FORMAT_VERSION",
    "CACHE_SWEEP_INTERVAL_SECONDS",
    "MEDIA_CACHE_TTL_SECONDS",
    "ORPHAN_MIN_AGE_SECONDS",
    "RESULT_AFFECTING_FIELDS",
    "SIGNED_URL_PROVIDERS",
    "MediaCacheError",
    "cache_key",
    "cache_path",
    "cache_stats",
    "is_credential_param",
    "is_fresh",
    "is_signed_url",
    "lock_for",
    "provider_disables_cache",
    "public_page_url",
    "read_media_cache",
    "remove_entry",
    "result_affecting_params",
    "search_media_cached",
    "sweep_media_cache",
    "write_media_cache",
]
