"""Publishing plans + preflight + scheduled publishing (Work 04, Lane B).

Flow:
  build_publishing_plan()  pure pacing/dependency computation (master first,
      shorts staggered — never all-at-once).
  preflight()              per-variant gate: decodes, aspect, duration,
      metadata, compliance, account, duplicates, approval.
  schedule_plan()          writes ScheduleEntry rows via the EXISTING
      scheduler table (idempotent; the scheduler agent owns the brains).
  publish_due()            enqueues ``campaign.publish`` jobs with stable
      idempotency keys; the handler drives the EXISTING publisher factory
      and links results back to the variant + PublishedPost.

Retry/failed paths touch only the failing variant; siblings are preserved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.engine.campaign.platforms import (
    ACCOUNT_PLATFORM,
    CAMPAIGN_PLATFORMS,
    get_profile,
    validate_against_profile,
)

#: Approval states for plan items.
APPROVAL_STATES: tuple[str, ...] = ("PENDING", "APPROVED", "REJECTED")
#: Publication states for plan items.
PUBLICATION_STATES: tuple[str, ...] = ("QUEUED", "PUBLISHED", "FAILED", "SKIPPED")

CAMPAIGN_EVENT_KINDS: tuple[str, ...] = (
    "campaign.created",
    "campaign.derivation_started",
    "campaign.short_created",
    "campaign.variant_created",
    "campaign.ready",
    "campaign.scheduled",
    "campaign.completed",
    "campaign.failed",
)


def emit_campaign_event(workspace_id: str, kind: str, message: str, **data) -> dict:
    """Persist + fan out a campaign event (webhooks reuse existing infra).

    Best-effort: never raises — telemetry must not break the pipeline
    (e.g. a second session contending a SQLite write lock in tests).
    """
    from app.services.events import record_event

    if kind not in CAMPAIGN_EVENT_KINDS:
        raise ValueError(f"unknown campaign event {kind!r}")
    try:
        return record_event(workspace_id, kind, message, source="campaign", data=data or {})
    except Exception:
        return {}


def build_publishing_plan(
    variant_refs: list[dict],
    start_date: datetime,
    interval_days: float,
    *,
    master_ref: dict | None = None,
    autonomy: str = "SEMI_AUTONOMOUS",
) -> list[dict]:
    """Compute ordered plan items with master-first dependencies and pacing.

    variant_refs: [{variant_id, platform, short_content_id}].
    The master (when given) occupies slot 0 with no dependencies; every
    short depends on the master publication (CTA URLs resolve only after
    the master is live). Slots advance by interval_days so shorts are
    distributed, never all-at-once.
    """
    for ref in variant_refs:
        if ref.get("platform") not in CAMPAIGN_PLATFORMS:
            raise ValueError(f"unsupported plan platform {ref.get('platform')!r}")
    step = max(float(interval_days or 1.0), 0.25)
    items: list[dict] = []
    slot = 0

    def _planned_at(i: int) -> str:
        return (start_date + timedelta(days=step * i)).isoformat()

    master_id = (master_ref or {}).get("variant_id") or (master_ref or {}).get("content_id") or "master"
    if master_ref is not None:
        items.append({
            "variant_id": master_id,
            "platform": (master_ref or {}).get("platform", "youtube_longform"),
            "planned_at": _planned_at(0),
            "priority": 0,
            "depends_on": [],
            "approval_state": "APPROVED",
            "publication_state": "QUEUED",
            "is_master": True,
        })
        slot = 1
    for i, ref in enumerate(variant_refs):
        items.append({
            "variant_id": ref["variant_id"],
            "platform": ref["platform"],
            "planned_at": _planned_at(slot + i),
            "priority": slot + i,
            # Master-first: shorts wait for the master publication so
            # WATCH_FULL_VIDEO CTA URLs can resolve.
            "depends_on": [master_id] if master_ref is not None else [],
            "approval_state": "APPROVED" if autonomy == "AUTONOMOUS" else "PENDING",
            "publication_state": "QUEUED",
            "is_master": False,
        })
    return items


def dependencies_satisfied(item: dict, published_ids: set[str]) -> bool:
    """True when every depends_on id has published (master-first gate)."""
    return all(d in published_ids for d in (item.get("depends_on") or []))


@dataclass
class PreflightInput:
    platform: str
    aspect: str | None = None
    duration: float | None = None
    metadata: dict = field(default_factory=dict)
    file_path: str = ""
    compliance_state: str = "pass"  # pass|review|fail
    content_hash: str = ""
    approval_satisfied: bool = True
    published_refs: list[dict] = field(default_factory=list)  # [{content_hash, platform, account_id}]
    account_id: str = ""


def default_probe(file_path: str) -> dict | None:
    """ffprobe a file; None when the file is absent or ffmpeg missing.

    Callers treat None as 'skip honestly' only when no file was supplied;
    a supplied-but-unreadable file is a hard failure.
    """
    import shutil
    import subprocess

    if not file_path:
        return None
    if shutil.which("ffprobe") is None:
        return None
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=width,height,duration", "-of", "json", file_path],
            capture_output=True, timeout=30,
        )
    except Exception:
        return {"unreadable": True}
    if proc.returncode != 0:
        return {"unreadable": True}
    import json as _json

    try:
        return _json.loads(proc.stdout.decode() or "{}")
    except Exception:
        return {"unreadable": True}


def preflight(
    inp: PreflightInput,
    *,
    probe=None,
    account_connected: bool | None = None,
    session=None,
    workspace_id: str = "",
) -> list[str]:
    """Gate a variant before scheduling/publishing. Returns issue strings."""
    issues: list[str] = []
    try:
        get_profile(inp.platform)
    except KeyError:
        return [f"unknown platform {inp.platform!r}"]
    issues.extend(validate_against_profile(inp.platform, inp.duration, inp.aspect, inp.metadata))

    # Video must decode when a file is present.
    if inp.file_path:
        result = (probe or default_probe)(inp.file_path)
        if result is None:
            issues.append("ffprobe unavailable — decode check skipped, publish blocked")
        elif result.get("unreadable"):
            issues.append(f"video does not decode: {inp.file_path}")

    # Compliance gate.
    if inp.compliance_state == "fail":
        issues.append("compliance check failed — publish blocked")
    elif inp.compliance_state == "review":
        issues.append("compliance requires HUMAN_REVIEW before publish")

    # Account connected (publishers factory path or relay).
    connected = account_connected
    if connected is None:
        connected = _account_connected(session, workspace_id, inp.platform, inp.account_id)
    if not connected:
        issues.append(f"no connected {ACCOUNT_PLATFORM.get(inp.platform, inp.platform)} account (OAuth or relay)")

    # Duplicate check: same content hash already live on platform+account.
    for ref in inp.published_refs or []:
        if (
            ref.get("platform") == inp.platform
            and ref.get("content_hash") == inp.content_hash
            and inp.content_hash
            and (not inp.account_id or ref.get("account_id") in ("", inp.account_id))
        ):
            issues.append(f"duplicate: {inp.content_hash[:12]} already published on {inp.platform}")
            break

    if not inp.approval_satisfied:
        issues.append("approval gate not satisfied")
    return issues


def _account_connected(session, workspace_id: str, platform: str, account_id: str) -> bool:
    """True when a connected SocialAccount exists or the relay is configured."""
    try:
        from app.providers.publishers.factory import relay_ready
        if relay_ready():
            return True
    except Exception:
        pass
    if session is None or not workspace_id:
        return False
    try:
        from sqlalchemy import select

        from app.models import SocialAccount

        acct_platform = ACCOUNT_PLATFORM.get(platform, platform)
        q = select(SocialAccount).where(
            SocialAccount.workspace_id == workspace_id,
            SocialAccount.platform == acct_platform,
            SocialAccount.status == "connected",
        )
        if account_id:
            q = q.where(SocialAccount.id == account_id)
        return session.scalar(q) is not None
    except Exception:
        return False


def schedule_plan(session, *, workspace_id: str, campaign_id: str, items: list[dict]) -> dict:
    """Write ScheduleEntry rows via the EXISTING scheduler table.

    Idempotent: live (PENDING/DISPATCHING/QUEUED) entries for the same
    (content, platform) are reused, never duplicated. Returns a summary.
    """
    from sqlalchemy import select

    from app.models import ScheduleEntry

    created, reused = [], []
    for item in items or []:
        short_id = str(item.get("short_content_id") or item.get("variant_id") or "")
        platform = str(item.get("platform", ""))
        run_at = item.get("run_at") or item.get("planned_at")
        if isinstance(run_at, str):
            run_at = datetime.fromisoformat(run_at)
        existing = session.scalar(
            select(ScheduleEntry).where(
                ScheduleEntry.workspace_id == workspace_id,
                ScheduleEntry.content_item_id == short_id,
                ScheduleEntry.platform == platform,
                ScheduleEntry.status.in_(["PENDING", "DISPATCHING", "QUEUED"]),
            )
        )
        if existing:
            reused.append(existing.id)
            continue
        entry = ScheduleEntry(
            workspace_id=workspace_id,
            content_item_id=short_id,
            campaign_id=campaign_id,
            platform=platform,
            run_at=run_at,
            status="PENDING",
        )
        session.add(entry)
        session.flush()
        created.append(entry.id)
    session.flush()
    return {"created": created, "reused": reused}


def publish_idempotency_key(variant_id: str, platform: str) -> str:
    return f"camp-{variant_id}-{platform}"


def publish_due(
    session,
    *,
    workspace_id: str,
    campaign_id: str,
    variant_id: str,
    platform: str,
    short_content_id: str = "",
    platform_variant_id: str = "",
    payload: dict | None = None,
) -> str | None:
    """Enqueue one ``campaign.publish`` job (idempotent). Returns job id or None."""
    from app.services import jobs as jobs_service

    body = dict(payload or {})
    body.update({
        "campaign_id": campaign_id,
        "variant_id": variant_id,
        "platform": platform,
        "short_content_id": short_content_id or variant_id,
        "platform_variant_id": platform_variant_id or variant_id,
    })
    return jobs_service.enqueue(
        "campaign.publish",
        body,
        workspace_id=workspace_id,
        priority=50,
        max_retries=3,
        idempotency_key=publish_idempotency_key(variant_id, platform),
    )


def register_publish_handlers() -> None:
    """Register durable ``campaign.*`` job handlers (idempotent import)."""
    from app.services import jobs as jobs_service

    if "campaign.publish" in jobs_service._handlers:
        return

    @jobs_service.handler("campaign.publish")
    def _run_campaign_publish(ctx) -> dict:
        from sqlalchemy import select

        from app.db import session_scope
        from app.models import PublishedPost

        p = ctx.payload or {}
        platform = p.get("platform", "")
        variant_id = p.get("variant_id", "")
        short_id = p.get("short_content_id", variant_id)
        campaign_id = p.get("campaign_id", "")
        acct_platform = ACCOUNT_PLATFORM.get(platform, platform)

        with session_scope() as s:
            account = None
            try:
                from app.models import SocialAccount

                account = s.scalar(
                    select(SocialAccount).where(
                        SocialAccount.workspace_id == ctx.workspace_id,
                        SocialAccount.platform == acct_platform,
                        SocialAccount.status == "connected",
                    )
                )
            except Exception:
                account = None
            try:
                from app.providers.publishers.factory import get_publisher

                publisher = get_publisher(acct_platform, has_account=bool(account))
            except Exception as exc:
                _mark_variant(ctx.workspace_id, variant_id, short_id, platform, "FAILED")
                raise RuntimeError(f"no publisher for {platform}: {exc}") from exc
            try:
                meta = _publish_metadata(p)
                # Call the publisher with the REAL BasePublisher signature.
                # This previously passed `metadata=`/`account_id=`, which no
                # real publisher accepts (they all take `(video_path, meta,
                # account)`) -- the call only ever "worked" because tests
                # inject a mock that swallows **kwargs, so a live publish
                # would have failed with a TypeError. The account DICT carries
                # the decrypted token, which is what the providers need; the
                # account ID is still used below for the verification check.
                from app.services.oauth_service import get_decrypted_account

                account_dict = (get_decrypted_account(account)
                                if account is not None else {})
                result = publisher.publish(
                    p.get("video_path", "") or p.get("file_path", ""),
                    meta,
                    account_dict,
                )
            except Exception as exc:
                _mark_variant(ctx.workspace_id, variant_id, short_id, platform, "FAILED")
                raise RuntimeError(f"publish failed on {platform}: {exc}") from exc
            remote_id = getattr(result, "remote_post_id", "") or getattr(result, "post_id", "") or ""
            remote_url = getattr(result, "remote_url", "") or getattr(result, "url", "") or ""

            # -- Work 14 §5/§9: classify BEFORE writing the row ------------
            # A handoff platform prepares media and a human publishes it. That
            # is recorded as HANDOFF and the variant is NOT marked PUBLISHED;
            # treating it as published is the single failure this guards.
            from app.engine.distribution.modes import (
                PublicationMode,
                classify_publication,
            )
            from app.providers.publishers.factory import HANDOFF_PLATFORMS

            handoff_required = acct_platform in HANDOFF_PLATFORMS
            mode = classify_publication(
                handoff_required=handoff_required,
                unavailable_reason="" if remote_id or handoff_required
                else "provider returned no remote id",
                remote_id=remote_id)
            handoff_payload = (getattr(result, "handoff", None)
                              or _handoff_payload_from(result))
            # Link back: PublishedPost.video_id carries the short id when no
            # render video exists (column is a plain string, no FK).
            post = s.scalar(
                select(PublishedPost).where(
                    PublishedPost.workspace_id == ctx.workspace_id,
                    PublishedPost.video_id == short_id,
                    PublishedPost.platform == acct_platform,
                )
            )
            if post is None:
                post = PublishedPost(
                    workspace_id=ctx.workspace_id or "",
                    content_item_id=short_id,
                    video_id=short_id,
                    platform=acct_platform,
                    account_id=getattr(account, "id", "") or "",
                    remote_post_id=remote_id,
                    remote_url=remote_url,
                    title=str((p.get("metadata") or {}).get("title", ""))[:300],
                    platform_variant_id=p.get("platform_variant_id") or None,
                    campaign_id=campaign_id or None,
                    publication_mode=mode.value,
                    is_mock=mode is PublicationMode.MOCK,
                    handoff_payload=handoff_payload or None,
                )
                s.add(post)
            else:
                post.remote_post_id = remote_id
                post.remote_url = remote_url
                post.publication_mode = mode.value
                post.is_mock = mode is PublicationMode.MOCK
                if handoff_payload:
                    post.handoff_payload = handoff_payload
                if p.get("platform_variant_id"):
                    post.platform_variant_id = p["platform_variant_id"]
                if campaign_id:
                    post.campaign_id = campaign_id
            s.flush()
            # A handoff is PREPARED, not published: the variant stays out of
            # PUBLISHED so nothing downstream treats it as live.
            variant_status = ("PUBLISHED" if mode.is_live
                              else "AWAITING_HANDOFF" if mode is PublicationMode.HANDOFF
                              else "FAILED")
            _mark_variant(ctx.workspace_id, variant_id, short_id, platform,
                          variant_status, published_post_id=post.id, session=s)
            # W11.5 D-F2 (HIGH): a PublishedPost row was written with no
            # verifier call, so `remote_post_id` present == success. Ledger the
            # independent check (receipt, remote id, row, account/platform,
            # single-record idempotency) so the evidence is auditable; a mock
            # publisher verifies ONLY as mock, never live.
            #
            # It MUST run on the ambient session, never a nested one: the outer
            # transaction already holds SQLite's write lock, so opening a second
            # session here and committing deadlocks ("database is locked") and
            # broke the E2E gate. Sharing `s` also makes the publication and its
            # evidence one atomic commit. Never raises: the publish succeeded.
            try:
                from app.engine.intelligence.verifier import (
                    CompletionContract,
                )
                from app.engine.intelligence.verifier import (
                    verify as verify_completion,
                )

                verify_completion(
                    s, ctx.workspace_id or "",
                    CompletionContract(kind="publication", subject_id=post.id),
                )
            except Exception as exc:  # noqa: BLE001 — never fail a publish on evidence
                emit_campaign_event(
                    ctx.workspace_id or "", "campaign.publication_unverified",
                    f"Completion verification failed to run: {exc}"[:300],
                    campaign_id=campaign_id, variant_id=variant_id,
                )
        emit_campaign_event(
            ctx.workspace_id or "", "campaign.completed",
            f"Variant {variant_id} published on {platform}",
            campaign_id=campaign_id, variant_id=variant_id, platform=platform,
            mode=mode.value,
        )
        return {"published": mode.is_live, "mode": mode.value,
                "platform": platform, "remote_post_id": remote_id,
                "requires_human": mode is PublicationMode.HANDOFF}


def _handoff_payload_from(result) -> dict:
    """Extract a handoff record from a publisher result, if it produced one.

    The handoff publisher returns the prepared-work record; anything else
    returns no payload, which is what keeps non-handoff platforms clean.
    """
    for attribute in ("handoff", "handoff_payload"):
        value = getattr(result, attribute, None)
        if isinstance(value, dict) and value:
            return value
    if not getattr(result, "remote_post_id", ""):
        # A result with no remote id that still succeeded is, by construction,
        # prepared work rather than a publication. Record that explicitly so a
        # later reader cannot infer "published" from the absence of an error.
        return {"mode": "HANDOFF", "requires_human": True,
                "instruction": getattr(result, "error", "") or
                "no remote id was returned: this is prepared work, not a "
                "publication"}
    return {}


def _publish_metadata(payload: dict):
    from app.providers.publishers.base import PublishMetadata

    meta = payload.get("metadata") or {}
    return PublishMetadata(
        title=str(meta.get("title", "") or "Untitled"),
        description=str(meta.get("description", "") or meta.get("caption", "")),
        hashtags=[str(h).lstrip("#") for h in (meta.get("hashtags") or [])],
        keywords=[str(k) for k in (meta.get("keywords") or [])],
        thumbnail_path=str(meta.get("thumbnail_path", "") or ""),
    )


def _mark_variant(workspace_id: str, variant_id: str, short_id: str, platform: str,
                  status: str, published_post_id: str | None = None,
                  session=None) -> None:
    """Best-effort variant status update; siblings untouched. Never raises.

    W11.5: pass the CALLER's session. On the success path the publish
    transaction has already flushed the PublishedPost, so it holds SQLite's
    write lock; opening a second connection here made the nested write wait out
    ``busy_timeout`` (5s), fail with "database is locked", and get swallowed by
    the ``except`` below -- leaving ``platform_variants.status`` stale at its
    pre-publish value while the publish itself reported success. The FAILED
    path escaped this only because nothing had been flushed yet.
    """
    try:
        from app.db import session_scope
        from app.engine.campaign.variants import default_store

        def _apply(s) -> None:
            store = default_store(s)
            rec = store.get(short_id, platform)
            if rec is None:
                return
            if rec.workspace_id and rec.workspace_id != (workspace_id or ""):
                return
            rec.status = status
            if published_post_id:
                rec.published_post_id = published_post_id
            store.upsert(rec)

        if session is not None:
            _apply(session)
        else:
            with session_scope() as own:
                _apply(own)
    except Exception:
        pass


def mark_plan_item(items: list[dict], variant_id: str, **updates) -> list[dict]:
    """Return items with one variant's entry updated (pure; siblings preserved)."""
    out = []
    for item in items or []:
        if item.get("variant_id") == variant_id:
            item = {**item, **updates}
        out.append(item)
    return out
