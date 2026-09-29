"""Inbox synchronization engine (Work 09 §3 "Inbox Synchronization" + §2).

Durable incremental sync of platform interactions (comments / mentions /
messages) into the unified inbox:

* **Cursors** — per account + resource pagination cursors persist in
  ``CommunitySyncState.cursors_json`` (keys: ``comments`` | ``mentions`` |
  ``messages``). A cursor advances ONLY after the page it was fetched with
  has been fully ingested AND committed, so a crash mid-run loses at most
  the current (uncommitted) page; the next run resumes from the last good
  cursor. Re-scanning is always safe because ingestion is idempotent.
* **Pagination** — loops until ``next_cursor`` is exhausted or
  ``MAX_PAGES_PER_RUN`` safety cap (cursor stays at the last good page so
  the next run continues).
* **Backoff** — provider errors (including ``ProviderRateLimited``, whose
  optional ``retry_after`` is honoured) bump ``consecutive_failures`` and set
  ``next_attempt_at`` with capped exponential backoff. Runs before
  ``next_attempt_at`` are skipped without touching the provider; a success
  resets failures/next_attempt/last_error and stamps ``last_success_at``.
* **Idempotency** — (workspace, platform, account, remote_id) is unique:
  re-ingesting a remote item is counted as a duplicate no-op (pre-check plus
  a SAVEPOINT around the INSERT for races) and never raises.
* **Account isolation** — one CommunitySyncState row per account; every
  provider/account failure is recorded on that row (last_error,
  consecutive_failures) and the loop continues with the next account.
  ``sync_account`` never raises for provider-side failures.
* **Linkage** — interactions link to ``PublishedPost`` by
  (workspace, platform, post_remote_id) with an account binding check, then
  denormalize content_item_id/campaign_id; unlinked items keep post_remote_id
  and null ids. Interactions aggregate into ``Conversation`` rows keyed by
  thread_key (unread_count increments only for newly ingested rows).

Lane note: ``app.providers.social`` (Lane A) is imported LAZILY inside
functions so this module imports cleanly before that layer lands; tests
inject duck-typed provider doubles through the ``providers`` mapping.
``ProviderRateLimited`` is recognized both as the mirror class defined here
and as Lane A's class (resolved lazily by name/isinstance), so behaviour is
identical before and after Lane A lands.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import session_scope
from app.models.base import utcnow
from app.models.community import (
    INTERACTION_KINDS,
    CommunitySyncState,
    Conversation,
    SocialInteraction,
)
from app.models.content import PublishedPost
from app.models.identity import SocialAccount
from app.services import jobs as jobs_service

# resource -> provider method (Lane A cross-lane contract)
RESOURCE_METHODS: dict[str, str] = {
    "comments": "list_comments",
    "mentions": "list_mentions",
    "messages": "list_messages",
}
# resources probed by default (plus "messages" when the provider implements it)
DEFAULT_RESOURCES: tuple[str, ...] = ("comments", "mentions")

PAGE_LIMIT = 50
MAX_PAGES_PER_RUN = 50
#: Newest published posts scanned per run by post-scoped comment APIs.
MAX_POSTS_PER_RUN = 25
# Opaque resume cursor for post-scoped comment scans: three fields joined by
# the ASCII unit separator -- post index, post id, provider page cursor.
_POST_CURSOR_SEP = "\x1f"
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 3600
RATE_LIMIT_MAX_SECONDS = 6 * 3600
MAX_ERROR_LEN = 2000

# summary keys that mirror committed DB state (restored if a page fails
# before its commit, so counters never claim rows that were rolled back)
_COUNTER_KEYS = ("ingested", "duplicates", "invalid", "linked", "pages")

_QUESTION_STARTS = frozenset(
    {
        "what", "why", "how", "when", "where", "who", "which",
        "is", "are", "was", "were", "do", "does", "did",
        "can", "could", "would", "will", "should",
    }
)

_MISSING: Any = object()


class ProviderNotConfigured(Exception):
    """No social provider implementation is available for a platform."""


class ProviderRateLimited(Exception):
    """Mirror of ``app.providers.social.base.ProviderRateLimited``.

    The real Lane A class is recognized too (see ``_rate_limited_types``) so
    this module — and its tests — behave correctly before Lane A lands and
    after. ``retry_after`` (seconds) is honoured when present.
    """

    def __init__(self, message: str = "rate limited", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# small duck-typing helpers (provider pages/items may be objects or mappings)
# ---------------------------------------------------------------------------


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _page_parts(page: Any) -> tuple[list[Any], Any]:
    """Split a provider page into (items, next_cursor)."""
    if isinstance(page, Mapping):
        items = page.get("items")
        next_cursor = page.get("next_cursor")
    elif hasattr(page, "items"):
        items = page.items
        next_cursor = getattr(page, "next_cursor", None)
    elif isinstance(page, (tuple, list)) and len(page) == 2:
        items, next_cursor = page[0], page[1]
    else:
        raise TypeError(
            f"unexpected page type {type(page).__name__} "
            "(expected Page(items, next_cursor), mapping or 2-tuple)"
        )
    return list(items or []), next_cursor


def _as_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC).replace(tzinfo=None)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    return None


def _rate_limited_types() -> tuple[type[BaseException], ...]:
    """Local mirror + Lane A's real class (lazy; never fails)."""
    types: list[type[BaseException]] = [ProviderRateLimited]
    try:
        from app.providers.social.base import ProviderRateLimited as _lane_a_rl
    except Exception:  # Lane A layer not present yet
        _lane_a_rl = None
    if _lane_a_rl is not None and _lane_a_rl is not ProviderRateLimited:
        types.append(_lane_a_rl)
    return tuple(types)


def _is_rate_limited(exc: BaseException) -> bool:
    return isinstance(exc, _rate_limited_types())


def _retry_after(exc: BaseException) -> float | None:
    value = getattr(exc, "retry_after", None)
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _backoff_delay(failures: int) -> int:
    """Capped exponential backoff: 30s, 60s, 120s ... -> 3600s."""
    exp = min(max(int(failures or 1), 1) - 1, 16)
    return min(BACKOFF_BASE_SECONDS * (2**exp), BACKOFF_MAX_SECONDS)


def _error_text(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:MAX_ERROR_LEN]


# ---------------------------------------------------------------------------
# provider resolution (lazy import — Lane A contract)
# ---------------------------------------------------------------------------


def _resolve_provider(platform: str, providers: Mapping[str, Any] | None) -> Any:
    """Resolve the provider for a platform.

    * ``providers`` mapping supplied (tests / in-process injection): missing
      or ``None`` entry means honestly "not configured" — no lazy import.
    * otherwise: lazy ``app.providers.social.get_provider`` (Lane A); any
      import/resolution failure surfaces as ``ProviderNotConfigured``.
    """
    if providers is not None:
        entry = providers.get(platform, _MISSING)
        if entry is _MISSING or entry is None:
            raise ProviderNotConfigured(
                f"no social provider configured for platform '{platform}'"
            )
        return entry
    try:
        from app.providers.social import get_provider
    except Exception as exc:
        raise ProviderNotConfigured(
            f"social provider registry unavailable for '{platform}': "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    try:
        provider = get_provider(platform)
    except Exception as exc:
        raise ProviderNotConfigured(
            f"no social provider for platform '{platform}': "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if provider is None:
        raise ProviderNotConfigured(f"no social provider for platform '{platform}'")
    return provider


def _resources_for(provider: Any, resources: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    if resources:
        return tuple(resources)
    return tuple(
        r
        for r in (*DEFAULT_RESOURCES, "messages")
        if callable(getattr(provider, RESOURCE_METHODS[r], None))
    )


# ---------------------------------------------------------------------------
# sync-state bookkeeping
# ---------------------------------------------------------------------------


def _get_state(db: Session, workspace_id: str, account: Any) -> CommunitySyncState | None:
    return db.scalar(
        select(CommunitySyncState).where(
            CommunitySyncState.workspace_id == workspace_id,
            CommunitySyncState.account_id == account.id,
        )
    )


def _ensure_state(db: Session, workspace_id: str, account: Any) -> CommunitySyncState:
    state = _get_state(db, workspace_id, account)
    if state is not None:
        return state
    state = CommunitySyncState(
        workspace_id=workspace_id,
        account_id=account.id,
        platform=account.platform,
        cursors_json={},
    )
    db.add(state)
    try:
        db.flush()
        db.commit()
    except IntegrityError:
        # concurrent creator won the unique race — use its row
        db.rollback()
        state = _get_state(db, workspace_id, account)
        if state is None:  # pragma: no cover - defensive
            raise RuntimeError("community sync state could not be created") from None
    return state


def _record_failure(
    db: Session, state: CommunitySyncState, exc: BaseException, *, rate_limited: bool = False
) -> int:
    """Persist a failed attempt: last_error + exponential next_attempt_at.

    Rolls back first so a partially ingested page never commits alongside a
    cursor that claims it completed.
    """
    with contextlib.suppress(Exception):  # rollback of a broken txn
        db.rollback()
    failures = int(state.consecutive_failures or 0) + 1
    state.consecutive_failures = failures
    state.last_error = _error_text(exc)
    delay = _backoff_delay(failures)
    if rate_limited:
        retry_after = _retry_after(exc)
        if retry_after is not None:
            delay = min(retry_after, float(RATE_LIMIT_MAX_SECONDS))
    state.next_attempt_at = utcnow() + timedelta(seconds=delay)
    db.commit()
    return failures


def _record_success(db: Session, state: CommunitySyncState) -> None:
    state.last_success_at = utcnow()
    state.consecutive_failures = 0
    state.next_attempt_at = None
    state.last_error = ""
    db.commit()


def _record_failure_best_effort(
    db: Session, workspace_id: str, account: Any, exc: BaseException
) -> None:
    """Isolation belt for unexpected (non-provider) account failures."""
    try:
        db.rollback()
        state = _ensure_state(db, workspace_id, account)
        _record_failure(db, state, exc)
    except Exception:  # pragma: no cover - never let bookkeeping kill the loop
        with contextlib.suppress(Exception):
            db.rollback()


# ---------------------------------------------------------------------------
# ingestion
# ---------------------------------------------------------------------------


def _looks_like_question(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped.endswith("?"):
        return True
    first = stripped.split(None, 1)[0].lower().strip(".,!:;")
    return first in _QUESTION_STARTS


def _infer_kind(item: Any, resource: str) -> str:
    explicit = str(_field(item, "kind", "") or "").upper()
    if explicit in INTERACTION_KINDS:
        return explicit
    if resource == "messages":
        return "MESSAGE"
    if resource == "mentions":
        return "MENTION"
    if str(_field(item, "parent_remote_id", "") or ""):
        return "REPLY"
    if _looks_like_question(str(_field(item, "text", "") or "")):
        return "QUESTION"
    return "COMMENT"


def _thread_key(item: Any, remote_id: str) -> str:
    for candidate in (
        str(_field(item, "thread_id", "") or ""),
        str(_field(item, "post_remote_id", "") or ""),
        remote_id,
    ):
        if candidate:
            return candidate[:200]
    return remote_id[:200]


def _find_published_post(
    db: Session, workspace_id: str, platform: str, account_id: str, post_remote_id: str
) -> PublishedPost | None:
    """Match (workspace, platform, post_remote_id) -> publication.

    Prefers the post bound to this account, then an unbound post; a post
    explicitly bound to a DIFFERENT account or workspace never links.
    """
    if not post_remote_id:
        return None
    rows = db.scalars(
        select(PublishedPost)
        .where(
            PublishedPost.workspace_id == workspace_id,
            PublishedPost.platform == platform,
            PublishedPost.remote_post_id == post_remote_id,
        )
        .limit(20)
    ).all()
    for post in rows:
        if (post.account_id or "") == account_id:
            return post
    for post in rows:
        if not post.account_id:
            return post
    return None


def _upsert_conversation(
    db: Session,
    workspace_id: str,
    account: Any,
    *,
    thread_key: str,
    author_remote_id: str,
    author_name: str,
    text: str,
    post: PublishedPost | None,
    when: datetime,
) -> Conversation:
    conv = db.scalar(
        select(Conversation).where(
            Conversation.workspace_id == workspace_id,
            Conversation.account_id == account.id,
            Conversation.thread_key == thread_key,
        )
    )
    if conv is None:
        label = author_name or author_remote_id or "unknown"
        conv = Conversation(
            workspace_id=workspace_id,
            platform=account.platform,
            account_id=account.id,
            thread_key=thread_key,
            title=(f"{label}: {text}" if text else label)[:300],
            participant_remote_id=author_remote_id,
            participant_name=author_name,
            published_post_id=post.id if post else None,
            last_interaction_at=when,
            unread_count=1,
        )
        db.add(conv)
        db.flush()
        return conv
    conv.unread_count = int(conv.unread_count or 0) + 1
    if when and (conv.last_interaction_at is None or when > conv.last_interaction_at):
        conv.last_interaction_at = when
    if post is not None and not conv.published_post_id:
        conv.published_post_id = post.id
    if not conv.participant_remote_id and author_remote_id:
        conv.participant_remote_id = author_remote_id
        conv.participant_name = author_name
    return conv


def _ingest_item(
    db: Session,
    workspace_id: str,
    account: Any,
    item: Any,
    resource: str,
    summary: dict[str, Any],
) -> str:
    """Insert one provider item (idempotent). Updates summary counters."""
    remote_id = str(_field(item, "remote_id", "") or "")
    if not remote_id.strip():
        summary["invalid"] += 1
        return "invalid"

    platform = account.platform
    existing = db.scalar(
        select(SocialInteraction).where(
            SocialInteraction.workspace_id == workspace_id,
            SocialInteraction.platform == platform,
            SocialInteraction.account_id == account.id,
            SocialInteraction.remote_id == remote_id,
        )
    )
    if existing is not None:
        summary["duplicates"] += 1
        # best-effort relink: the post may have been published after ingest
        if not existing.published_post_id and existing.post_remote_id:
            post = _find_published_post(
                db, workspace_id, platform, account.id, existing.post_remote_id
            )
            if post is not None:
                existing.published_post_id = post.id
                existing.content_item_id = post.content_item_id
                existing.campaign_id = post.campaign_id
                summary["linked"] += 1
        return "duplicate"

    text = str(_field(item, "text", "") or "")
    post_remote_id = str(_field(item, "post_remote_id", "") or "")
    author_remote_id = str(_field(item, "author_remote_id", "") or "")[:200]
    author_name = str(_field(item, "author_name", "") or "")[:200]
    thread_id = str(_field(item, "thread_id", "") or "")[:200]
    kind = _infer_kind(item, resource)
    created_at = _as_datetime(_field(item, "created_at"))
    when = created_at or utcnow()

    post = _find_published_post(db, workspace_id, platform, account.id, post_remote_id)

    parent_interaction_id: str | None = None
    parent_remote_id = str(_field(item, "parent_remote_id", "") or "")
    if parent_remote_id:
        parent_interaction_id = db.scalar(
            select(SocialInteraction.id).where(
                SocialInteraction.workspace_id == workspace_id,
                SocialInteraction.platform == platform,
                SocialInteraction.account_id == account.id,
                SocialInteraction.remote_id == parent_remote_id,
            )
        )

    try:
        with db.begin_nested():
            row = SocialInteraction(
                workspace_id=workspace_id,
                platform=platform,
                account_id=account.id,
                remote_id=remote_id,
                kind=kind,
                text=text,
                author_remote_id=author_remote_id,
                author_name=author_name,
                parent_interaction_id=parent_interaction_id,
                thread_id=thread_id,
                post_remote_id=post_remote_id[:200],
                published_post_id=post.id if post else None,
                content_item_id=post.content_item_id if post else None,
                campaign_id=post.campaign_id if post else None,
                status="unread",
                is_question=(kind == "QUESTION"),
                remote_created_at=created_at,
            )
            db.add(row)
            db.flush()
    except IntegrityError:
        # unique (workspace, platform, account, remote_id) race — no-op
        summary["duplicates"] += 1
        return "duplicate"

    conv = _upsert_conversation(
        db,
        workspace_id,
        account,
        thread_key=_thread_key(item, remote_id),
        author_remote_id=author_remote_id,
        author_name=author_name,
        text=text,
        post=post,
        when=when,
    )
    row.conversation_id = conv.id
    summary["ingested"] += 1
    if post is not None:
        summary["linked"] += 1
    return "inserted"


# ---------------------------------------------------------------------------
# resource + account sync
# ---------------------------------------------------------------------------


def _post_cursor_encode(index: int, post_id: str, page_cursor: str) -> str:
    return f"{index}{_POST_CURSOR_SEP}{post_id}{_POST_CURSOR_SEP}{page_cursor}"


def _post_cursor_decode(raw: Any) -> tuple[int, str, str]:
    """Parse a stored post-scan cursor; anything malformed restarts at 0."""
    if not raw:
        return 0, "", ""
    parts = str(raw).split(_POST_CURSOR_SEP)
    if len(parts) != 3:
        return 0, "", ""
    try:
        return int(parts[0]), parts[1], parts[2]
    except ValueError:
        return 0, "", ""


def _post_ids_for_account(db: Session, workspace_id: str, account: Any) -> list[str]:
    """Newest published post ids whose comments this account can read.

    Mirrors :func:`_find_published_post` binding rules: this account's posts
    plus unbound posts on the same platform; posts bound to a DIFFERENT
    account or workspace never appear.
    """
    account_id = str(getattr(account, "id", "") or "")
    platform = str(getattr(account, "platform", "") or "")
    rows = db.scalars(
        select(PublishedPost)
        .where(
            PublishedPost.workspace_id == workspace_id,
            PublishedPost.platform == platform,
            PublishedPost.remote_post_id != "",
        )
        .order_by(PublishedPost.created_at.desc())
        .limit(MAX_POSTS_PER_RUN * 3)
    ).all()
    ids: list[str] = []
    for post in rows:
        bound = str(post.account_id or "")
        if bound and bound != account_id:
            continue
        ids.append(str(post.remote_post_id))
        if len(ids) >= MAX_POSTS_PER_RUN:
            break
    return ids


def _sync_comments_post_scoped(
    db: Session,
    workspace_id: str,
    account: Any,
    resource: str,
    state: CommunitySyncState,
    summary: dict[str, Any],
    cursors: dict[str, Any],
    snapshot: dict[str, Any],
    method: Any,
) -> bool:
    """Scan comments post-by-post for APIs with no channel-wide read.

    Each page is ingested and its resume cursor committed atomically (same
    transaction), so a crash resumes exactly where the last committed page
    ended. Returns True when the whole scan finished (cursor resets to ``""``
    so the next run rescans from the newest post), False when the page cap
    stopped the run early (cursor preserved for the next run).
    """
    posts = _post_ids_for_account(db, workspace_id, account)
    if not posts:
        # nothing published yet -> nothing to scan; not a failure
        return True
    index, saved_post, page_cursor = _post_cursor_decode(cursors.get(resource))
    if index >= len(posts) or (saved_post and posts[index] != saved_post):
        # post list shifted (new publish / deletion / stale state): restart
        # the scan -- idempotent ingestion makes a rescan safe.
        index, page_cursor = 0, ""
    pages = 0
    while index < len(posts) and pages < MAX_PAGES_PER_RUN:
        post_id = posts[index]
        cursor: Any = page_cursor or None
        while pages < MAX_PAGES_PER_RUN:
            page = method(
                account, cursor=cursor, limit=PAGE_LIMIT, post_remote_id=post_id
            )
            if page is None:
                raise ValueError(f"{RESOURCE_METHODS[resource]} returned no page")
            items, next_cursor = _page_parts(page)
            for item in items:
                _ingest_item(db, workspace_id, account, item, resource, summary)
            pages += 1
            summary["pages"] += 1
            cursors[resource] = _post_cursor_encode(
                index, post_id, str(next_cursor) if next_cursor else ""
            )
            state.cursors_json = dict(cursors)
            db.commit()
            snapshot.update({k: summary[k] for k in _COUNTER_KEYS})
            if not next_cursor:
                break
            cursor = next_cursor
        index += 1
        page_cursor = ""
        cursors[resource] = _post_cursor_encode(
            index, posts[index] if index < len(posts) else "", ""
        )
        state.cursors_json = dict(cursors)
        db.commit()
    exhausted = index >= len(posts)
    if exhausted:
        cursors[resource] = ""
        state.cursors_json = dict(cursors)
        db.commit()
    return exhausted


def _sync_resource(
    db: Session,
    workspace_id: str,
    account: Any,
    provider: Any,
    resource: str,
    state: CommunitySyncState,
    summary: dict[str, Any],
) -> None:
    method_name = RESOURCE_METHODS[resource]
    method = getattr(provider, method_name, None)
    if not callable(method):
        raise ProviderNotConfigured(
            f"provider for '{account.platform}' does not implement {method_name}"
        )

    cursors = dict(state.cursors_json or {})
    cursor: Any = cursors.get(resource) or None
    snapshot = {k: summary[k] for k in _COUNTER_KEYS}
    pages = 0
    exhausted = False
    try:
        if resource == "comments" and getattr(provider, "comments_require_post", False):
            # post-scoped API (Graph/Business/tweet-replies/activity comments):
            # no channel-wide read exists -- scan this account's published
            # posts one at a time with an opaque, resumable cursor.
            exhausted = _sync_comments_post_scoped(
                db, workspace_id, account, resource, state, summary, cursors,
                snapshot, method,
            )
        else:
            while pages < MAX_PAGES_PER_RUN:
                page = method(account, cursor=cursor, limit=PAGE_LIMIT)
                if page is None:
                    raise ValueError(f"{method_name} returned no page")
                items, next_cursor = _page_parts(page)
                for item in items:
                    _ingest_item(db, workspace_id, account, item, resource, summary)
                # page fully ingested -> advance + persist cursor atomically
                pages += 1
                summary["pages"] += 1
                cursors[resource] = str(next_cursor) if next_cursor else ""
                state.cursors_json = dict(cursors)
                db.commit()
                snapshot = {k: summary[k] for k in _COUNTER_KEYS}
                exhausted = not next_cursor
                if exhausted:
                    break
                cursor = next_cursor
    except Exception:
        # only counters backed by a committed page may survive the failure
        for key, value in snapshot.items():
            summary[key] = value
        raise
    if not exhausted:
        summary["capped"] = True


def _blank_account_summary(account: Any) -> dict[str, Any]:
    return {
        "account_id": str(getattr(account, "id", "") or ""),
        "platform": str(getattr(account, "platform", "") or ""),
        "ok": False,
        "skipped": False,
        "reason": "",
        "ingested": 0,
        "duplicates": 0,
        "invalid": 0,
        "linked": 0,
        "pages": 0,
        "capped": False,
        "error": "",
        "consecutive_failures": 0,
    }


def sync_account(
    db: Session,
    workspace_id: str,
    account: SocialAccount,
    providers: dict[str, Any] | None = None,
    *,
    resources: tuple[str, ...] | list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Sync one account's interactions into the inbox. NEVER raises for
    provider-side failures — they are recorded on that account's
    CommunitySyncState row and returned in the summary (failure isolation).

    ``providers`` maps platform -> provider-like object (duck-typed). When
    given, it fully replaces lazy ``get_provider`` resolution — a platform
    missing from the map is an honest ProviderNotConfigured failure.
    ``force=True`` bypasses the ``next_attempt_at`` backoff gate.
    """
    summary = _blank_account_summary(account)
    account_id = summary["account_id"]

    if not account_id or str(getattr(account, "workspace_id", workspace_id)) != workspace_id:
        summary["skipped"] = True
        summary["reason"] = "workspace_mismatch"
        summary["error"] = "account does not belong to workspace"
        return summary

    state = _ensure_state(db, workspace_id, account)
    summary["consecutive_failures"] = int(state.consecutive_failures or 0)

    if not force and state.next_attempt_at and state.next_attempt_at > utcnow():
        summary["skipped"] = True
        summary["reason"] = "backoff"
        return summary

    try:
        provider = _resolve_provider(summary["platform"], providers)
    except Exception as exc:
        summary["error"] = _error_text(exc)
        summary["consecutive_failures"] = _record_failure(
            db, state, exc, rate_limited=_is_rate_limited(exc)
        )
        return summary

    try:
        for resource in _resources_for(provider, resources):
            _sync_resource(db, workspace_id, account, provider, resource, state, summary)
    except Exception as exc:
        summary["error"] = _error_text(exc)
        summary["consecutive_failures"] = _record_failure(
            db, state, exc, rate_limited=_is_rate_limited(exc)
        )
        return summary

    _record_success(db, state)
    summary["ok"] = True
    summary["consecutive_failures"] = 0
    return summary


def sync_workspace(
    db: Session,
    workspace_id: str,
    providers: dict[str, Any] | None = None,
    *,
    account_id: str | None = None,
    force: bool = False,
    cancel_check: Callable[[], None] | None = None,
    progress: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Sync every account of a workspace with per-account failure isolation.

    A single provider/account failure updates only that account's sync state
    and never blocks the remaining accounts/platforms.
    """
    query = (
        select(SocialAccount)
        .where(SocialAccount.workspace_id == workspace_id)
        .order_by(SocialAccount.created_at, SocialAccount.id)
    )
    if account_id:
        query = query.where(SocialAccount.id == account_id)
    accounts = list(db.scalars(query).all())

    results: list[dict[str, Any]] = []
    for index, account in enumerate(accounts):
        if cancel_check is not None:
            cancel_check()  # may raise jobs_service._Cancelled
        try:
            result = sync_account(db, workspace_id, account, providers, force=force)
        except Exception as exc:  # unexpected (DB-level) failure: isolate
            result = _blank_account_summary(account)
            result["error"] = _error_text(exc)
            _record_failure_best_effort(db, workspace_id, account, exc)
            state = _get_state(db, workspace_id, account)
            result["consecutive_failures"] = (
                int(state.consecutive_failures or 0) if state is not None else 0
            )
        results.append(result)
        if progress is not None:
            progress((index + 1) / max(len(accounts), 1))

    errors = {r["account_id"]: r["error"] for r in results if r.get("error")}
    return {
        "ok": not errors,
        "workspace_id": workspace_id,
        "accounts": len(results),
        "failed": len(errors),
        "skipped": len([r for r in results if r.get("skipped")]),
        "ingested": sum(r["ingested"] for r in results),
        "duplicates": sum(r["duplicates"] for r in results),
        "invalid": sum(r["invalid"] for r in results),
        "linked": sum(r["linked"] for r in results),
        "pages": sum(r["pages"] for r in results),
        "errors": errors,
        "results": results,
    }


# ---------------------------------------------------------------------------
# COMMUNITY_SYNC job handler
# ---------------------------------------------------------------------------


def handle_community_sync(ctx: jobs_service.JobContext) -> dict[str, Any]:
    """``COMMUNITY_SYNC`` job: sync one workspace's connected accounts.

    Payload (all optional): ``workspace_id`` (fallback), ``account_id``
    (single-account filter), ``force`` (ignore backoff), ``providers``
    (platform -> provider object; in-process/test injection — production
    payloads are JSON and simply omit it, resolving via lazy get_provider).
    Returns the workspace summary; per-account provider failures never fail
    the job — they live in each account's CommunitySyncState backoff row.
    """
    payload = ctx.payload or {}
    workspace_id = str(ctx.workspace_id or payload.get("workspace_id") or "")
    if not workspace_id:
        raise ValueError("COMMUNITY_SYNC job requires workspace_id")
    raw_providers = payload.get("providers")
    providers = raw_providers if isinstance(raw_providers, Mapping) else None

    with session_scope() as db:
        result = sync_workspace(
            db,
            workspace_id,
            providers,
            account_id=str(payload.get("account_id") or "") or None,
            force=bool(payload.get("force")),
            cancel_check=lambda: jobs_service.check_cancelled(ctx),
            progress=lambda pct: ctx.report_progress(pct),
        )

    # events only AFTER the session commits (same pattern as localization)
    if result["ingested"] or not result["ok"]:
        try:
            from app.services.events import record_event

            record_event(
                workspace_id,
                "community.sync",
                f"Ingested {result['ingested']} interaction(s) across "
                f"{result['accounts']} account(s)"
                + (f"; {result['failed']} account error(s)" if result["failed"] else ""),
                level="success" if result["ok"] else "warning",
                source="community",
                data={
                    "ingested": result["ingested"],
                    "duplicates": result["duplicates"],
                    "linked": result["linked"],
                    "failed": result["failed"],
                    "accounts": result["accounts"],
                },
            )
        except Exception:  # pragma: no cover - telemetry never fails the job
            pass
    return result


def register_community_sync_jobs() -> None:
    """Register COMMUNITY_SYNC — idempotent, safe to call on repeat/reload."""
    if "COMMUNITY_SYNC" in jobs_service._handlers:
        return
    jobs_service.register_handler("COMMUNITY_SYNC", handle_community_sync)


register_community_sync_jobs()


__all__ = [
    "ProviderNotConfigured",
    "ProviderRateLimited",
    "handle_community_sync",
    "register_community_sync_jobs",
    "sync_account",
    "sync_workspace",
]
