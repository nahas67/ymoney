"""Outbound webhooks: signed event POSTs with queue-backed retries.

Subscriptions pick event kinds; delivery runs as `webhook.dispatch` jobs so a
slow/dead receiver never blocks the pipeline (exponential backoff, then DEAD
with a `job.dead` event). 4xx (except 429) is terminal — no pointless retries.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlparse

from loguru import logger
from sqlalchemy import select

from app.core import security
from app.db import session_scope
from app.models import WebhookSubscription
from app.services import jobs

# Plan names mapped to real kinds: qc.failed -> quality.failed,
# budget.pause -> safety.autopause + budget.exceeded.
WEBHOOK_EVENTS: tuple[str, ...] = (
    "cycle.completed",
    "cycle.failed",
    "publish.done",
    "publish.failed",
    "publish.skipped",
    "quality.failed",
    "quality.passed",
    "review.required",
    "budget.exceeded",
    "budget.warning",
    "safety.autopause",
    "repurpose.completed",
    "campaign.created",
    "campaign.derivation_started",
    "campaign.short_created",
    "campaign.variant_created",
    "campaign.ready",
    "campaign.scheduled",
    "campaign.completed",
    "campaign.failed",
    "webhook.test",
    # Work 11 (Lane X): export lifecycle. The allowlist silently drops
    # unknown kinds, so a subscribed customer would never hear about a
    # finished (or failed) export without these entries.
    "EXPORT_CREATED",
    "EXPORT_COMPLETED",
    "EXPORT_FAILED",
    # Work 11 (Lane L): collaboration + enterprise-ops kinds. Same
    # rationale as the export block above — the allowlist silently drops
    # unknown kinds, so subscribers would never hear about a project
    # being created, reviewed, archived or swept.
    "PROJECT_CREATED",
    "PROJECT_UPDATED",
    "PROJECT_MEMBER_ADDED",
    "PROJECT_MEMBER_REMOVED",
    "PROJECT_TRANSFERRED",
    "TIMELINE_EDITED",
    "VERSION_CREATED",
    "COMMENT_ADDED",
    "REVIEW_REQUESTED",
    "CHANGES_REQUESTED",
    "APPROVED",
    "PUBLISHED",
    "ARCHIVE_CREATED",
    "RETENTION_SWEEP",
    "REVIEW_ASSIGNED",
    "REVISION_REQUESTED",
    "RETENTION_POLICY_UPDATED",
    # Work 11 (Lane R, reported by the reviews audit): the reviews engine
    # also emits these — without entries the allowlist would silently
    # drop them from webhook fan-out.
    "REVIEW_CANCELLED",
    "REVIEW_REJECTED",
    "COMMENT_RESOLVED",
    "COMMENT_REOPENED",
    "REVISION_UPDATED",
)

_SIGNATURE_HEADER = "X-YM-Signature"
_TIMEOUT_SECONDS = 15.0


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(24)


def validate_url(url: str) -> str:
    """Normalized URL or raises ValueError. http only for loopback (dev/test)."""
    cleaned = (url or "").strip()
    try:
        parts = urlparse(cleaned)
    except ValueError as exc:
        raise ValueError(f"invalid webhook URL: {exc}") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("webhook URL must be http(s):// with a host")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("plain http webhooks are dev-only (localhost); use https")
    return cleaned


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def enqueue_for_event(
    workspace_id: str | None,
    kind: str,
    event_id: str,
    message: str = "",
    level: str = "info",
    source: str = "",
    data: dict | None = None,
    created_at: str = "",
) -> int:
    """Fan out one event to matching subscriptions. Returns jobs enqueued.

    Best-effort: never raises — telemetry must not break the pipeline.
    """
    if not workspace_id or kind not in WEBHOOK_EVENTS:
        return 0
    try:
        with session_scope() as s:
            subs = s.scalars(
                select(WebhookSubscription).where(
                    WebhookSubscription.workspace_id == workspace_id,
                    WebhookSubscription.active.is_(True),
                )
            ).all()
            targets = [w for w in subs if kind in (w.events_json or [])]
            payloads = [
                {
                    "sub_id": w.id,
                    "event_id": event_id,
                    "kind": kind,
                    "message": message,
                    "level": level,
                    "source": source,
                    "data": data or {},
                    "created_at": created_at,
                }
                for w in targets
            ]
        n = 0
        for p in payloads:
            job_id = jobs.enqueue(
                "webhook.dispatch",
                p,
                workspace_id=workspace_id,
                priority=100,
                max_retries=5,
                idempotency_key=f"wh-{event_id}-{p['sub_id']}",
            )
            if job_id:
                n += 1
        return n
    except Exception as exc:  # pragma: no cover — fan-out is best-effort
        logger.warning(f"webhook fan-out failed: {type(exc).__name__}")
        return 0


@jobs.handler("webhook.dispatch")
def dispatch_webhook(ctx: jobs.JobContext) -> dict:
    """POST the signed event. Raise on retryable failure; return terminal otherwise."""
    import httpx

    p = ctx.payload or {}
    sub_id, kind = p.get("sub_id", ""), p.get("kind", "")
    secret = ""
    url = (p.get("url") or "").strip()
    if url:
        # Per-job delivery (no subscription row): caller-supplied URL already
        # validated at intake; secret arrives encrypted, exactly like subscriptions.
        try:
            secret = security.decrypt_secret(p["secret_enc"]) if p.get("secret_enc") else ""
        except Exception:
            return {"delivered": False, "terminal": True, "reason": "corrupt secret"}
    else:
        with session_scope() as s:
            sub = s.get(WebhookSubscription, sub_id)
            # webhook.test pings verify URL+secret regardless of the event filter
            if sub is None or not sub.active or (kind != "webhook.test" and kind not in (sub.events_json or [])):
                return {"delivered": False, "terminal": True, "reason": "subscription gone"}
            url = sub.url
            try:
                secret = security.decrypt_secret(sub.secret_enc)
            except Exception:
                return {"delivered": False, "terminal": True, "reason": "corrupt secret"}
    body = json.dumps(
        {
            "event": kind,
            "delivery_id": f"{p.get('event_id', '')}:{sub_id}",
            "workspace_id": ctx.workspace_id,
            "created_at": p.get("created_at", ""),
            "message": p.get("message", ""),
            "level": p.get("level", "info"),
            "source": p.get("source", ""),
            "data": p.get("data", {}),
        },
        separators=(",", ":"),
    ).encode()
    started = time.perf_counter()
    delivery = f"{p.get('event_id', '')}:{sub_id or 'once'}"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "YMONEY-webhook/1.0",
        "X-YM-Event": kind,
        "X-YM-Delivery": delivery,
    }
    if secret:
        headers[_SIGNATURE_HEADER] = sign(secret, body)
    try:
        resp = httpx.post(url, content=body, headers=headers, timeout=_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"webhook delivery error: {type(exc).__name__}") from exc
    latency_ms = int((time.perf_counter() - started) * 1000)
    if 200 <= resp.status_code < 300:
        return {"delivered": True, "status": resp.status_code, "latency_ms": latency_ms}
    if resp.status_code == 429 or resp.status_code >= 500:
        raise RuntimeError(f"webhook retryable HTTP {resp.status_code}")
    return {"delivered": False, "terminal": True, "status": resp.status_code, "latency_ms": latency_ms}
