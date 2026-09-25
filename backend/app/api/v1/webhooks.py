"""Outbound webhook subscriptions: signed event POSTs with queued retries.

Event catalog (plan names in parentheses): cycle.completed, cycle.failed,
publish.done, publish.failed, quality.failed (qc.failed), quality.passed,
publish.skipped, review.required, budget.exceeded, budget.warning,
safety.autopause (budget.pause). Secrets are AES-encrypted at rest and shown
once at subscribe time.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core import security
from app.db import get_db
from app.models import WebhookSubscription, Workspace
from app.services.auth_service import require_workspace_role
from app.services.webhooks import WEBHOOK_EVENTS, new_secret, validate_url

router = APIRouter(prefix="/workspaces/{workspace_id}/webhooks", tags=["webhooks"])


class SubscribeBody(BaseModel):
    url: str = Field(max_length=2000)
    events: list[str] = Field(min_length=1, max_length=20)


def _public(row: WebhookSubscription) -> dict:
    return {
        "id": row.id,
        "url": row.url,
        "events": list(row.events_json or []),
        "active": bool(row.active),
        "created_at": row.created_at.isoformat() + "Z",
    }


@router.post("", summary="Subscribe a URL to workspace events (secret shown once)")
def subscribe(
    workspace_id: str,
    body: SubscribeBody,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    try:
        url = validate_url(body.url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    events = sorted({e.strip() for e in (body.events or []) if e.strip()})
    unknown = [e for e in events if e not in WEBHOOK_EVENTS]
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"unknown events: {', '.join(unknown)} — pick from {', '.join(WEBHOOK_EVENTS)}"
        )
    secret = new_secret()
    row = WebhookSubscription(
        workspace_id=ws.id, url=url, secret_enc=security.encrypt_secret(secret),
        events_json=events, active=True,
    )
    db.add(row)
    db.commit()
    from app.services.events import record_event

    record_event(ws.id, "webhook.subscribed", f"Webhook subscribed: {url} ({len(events)} events)",
                 level="info", source="security", data={"sub_id": row.id})
    return {"id": row.id, "secret": secret, **_public(row)}


@router.get("", summary="List webhook subscriptions (never secrets)")
def list_webhooks(ws: Workspace = Depends(require_workspace_role("viewer")), db=Depends(get_db)):
    rows = db.scalars(
        select(WebhookSubscription).where(WebhookSubscription.workspace_id == ws.id).order_by(WebhookSubscription.created_at.desc())
    ).all()
    return {"items": [_public(r) for r in rows], "events": list(WEBHOOK_EVENTS)}


@router.delete("/{sub_id}", summary="Delete a webhook subscription")
def delete_webhook(
    workspace_id: str,
    sub_id: str,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    row = db.get(WebhookSubscription, sub_id)
    if not row or row.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="webhook not found")
    db.delete(row)
    db.commit()
    return {"deleted": True}


@router.post("/{sub_id}/test", summary="Send a signed ping to verify URL + secret")
def test_webhook(
    workspace_id: str,
    sub_id: str,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    from app.services import jobs

    row = db.get(WebhookSubscription, sub_id)
    if not row or row.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="webhook not found")
    if not row.active:
        raise HTTPException(status_code=409, detail="subscription is paused")
    job_id = jobs.enqueue(
        "webhook.dispatch",
        {
            "sub_id": row.id,
            "event_id": f"test-{uuid.uuid4().hex[:8]}",
            "kind": "webhook.test",
            "message": "YMONEY webhook ping — verify the signature and return 2xx",
            "level": "info",
            "source": "webhooks",
            "data": {"url": row.url},
            "created_at": "",
        },
        workspace_id=ws.id,
        priority=100,
        max_retries=2,
        idempotency_key=f"wh-test-{row.id}-{uuid.uuid4().hex[:8]}",
    )
    return {"enqueued": job_id is not None, "job_id": job_id}
