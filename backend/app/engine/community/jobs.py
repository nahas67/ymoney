"""Durable community job handlers (Work 09, Lane C).

``api/v1/inbox.py`` imports :func:`register_community_jobs` from this module at
startup; registration is idempotent so repeat imports/reloads never raise.

Handlers are plain SYNC functions — the queue executes them through
``asyncio.to_thread`` — matching the ``COMMUNITY_SYNC`` / ``localization.run``
convention:

======================  ====================================================
``INTERACTION_CLASSIFY`` classify + moderate a bounded batch of interactions
``COMMUNITY_DRAFT``      brand-checked draft / queued approval / auto-send
``COMMUNITY_ACTION``     approve | reject | send one existing action
======================  ====================================================

Rules that hold for every handler:

* Workspace scoped. ``ctx.workspace_id`` (or the payload) is mandatory; a row
  belonging to another workspace is skipped as "not found", never read or
  written.
* Deterministic gate only. Drafting/queueing/sending goes through
  ``policy.plan_reply`` / ``policy.send_action`` — the autonomy mode,
  the hard-bypass escalation gate and the volume limits are re-checked there
  for every call, so a job payload can never widen autonomy.
* Batches are capped and cancellable (``jobs.check_cancelled`` between rows);
  one bad interaction never fails the whole job.
* Events are emitted AFTER the session commits (same pattern as sync.py).
"""

from __future__ import annotations

from typing import Any

from app.services import jobs as jobs_service

DEFAULT_BATCH = 25
MAX_BATCH = 100
DRAFT_SKIP_STATUSES = ("spam", "ignored", "replied", "escalated")
ACTION_OPS = ("approve", "reject", "send")


# ---------------------------------------------------------------------------
# payload helpers
# ---------------------------------------------------------------------------

def _require_workspace(ctx: jobs_service.JobContext) -> str:
    payload = ctx.payload or {}
    workspace_id = str(ctx.workspace_id or payload.get("workspace_id") or "")
    if not workspace_id:
        raise ValueError(f"{ctx.type} job requires workspace_id")
    return workspace_id


def _limit(payload: dict, default: int = DEFAULT_BATCH) -> int:
    try:
        value = int(payload.get("limit") or default)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, MAX_BATCH))


def _explicit_ids(payload: dict) -> list[str]:
    raw = payload.get("interaction_ids")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item) for item in raw if str(item or "").strip()][:MAX_BATCH]


def _engine(payload: dict) -> Any:
    """Optional in-process DecisionEngine (tests only; JSON never carries it)."""
    engine = payload.get("engine")
    if engine is not None and not callable(getattr(engine, "classify", None)):
        return None
    return engine


def _notify(workspace_id: str, message: str, *, level: str = "info",
            data: dict | None = None) -> None:
    """Post-commit activity event; telemetry never fails a job."""
    try:
        from app.services.events import record_event

        record_event(workspace_id, "community.job", message, level=level,
                     source="community", data=dict(data or {}))
    except Exception:  # noqa: S110 - best effort only
        pass


def _pending_ids(db, workspace_id: str, limit: int) -> list[str]:
    """Interactions still awaiting classification (oldest first, bounded)."""
    from sqlalchemy import select

    from app.models.community import SocialInteraction

    rows = db.scalars(
        select(SocialInteraction.id)
        .where(
            SocialInteraction.workspace_id == workspace_id,
            SocialInteraction.status.in_(["unread", "read"]),
        )
        .order_by(SocialInteraction.created_at.asc())
        .limit(limit)
    ).all()
    return [str(row) for row in rows]


# ---------------------------------------------------------------------------
# INTERACTION_CLASSIFY
# ---------------------------------------------------------------------------

def handle_interaction_classify(ctx: jobs_service.JobContext) -> dict[str, Any]:
    """``INTERACTION_CLASSIFY``: classify + moderate a bounded batch.

    Payload: ``interaction_ids`` (explicit batch; otherwise the oldest
    unread/read rows), ``limit`` (default 25, hard cap 100), ``engine``
    (in-process/test DecisionEngine injection — production payloads omit it).

    Returns per-row outcomes; unknown/foreign ids are reported as skipped and
    never read. Moderation always runs AFTER classification because it needs
    the labels (SPAM/ABUSE drive the destructive verdicts).
    """
    from app.db import session_scope
    from app.engine.community.classify import classify_interaction
    from app.engine.community.moderation import moderate_interaction

    payload = ctx.payload or {}
    workspace_id = _require_workspace(ctx)
    engine = _engine(payload)
    limit = _limit(payload)

    with session_scope() as db:
        ids = _explicit_ids(payload) or _pending_ids(db, workspace_id, limit)

    results: list[dict] = []
    classified = moderated = skipped = 0
    verdicts: dict[str, int] = {}
    total = len(ids)

    with session_scope() as db:
        for index, interaction_id in enumerate(ids):
            jobs_service.check_cancelled(ctx)
            outcome = classify_interaction(db, workspace_id, interaction_id,
                                           engine=engine)
            if not outcome.get("found"):
                skipped += 1
                results.append({"interaction_id": interaction_id, "found": False})
            else:
                classified += 1
                mod = moderate_interaction(db, workspace_id, interaction_id,
                                           engine=engine)
                verdict = str(mod.get("verdict") or "")
                if verdict:
                    verdicts[verdict] = verdicts.get(verdict, 0) + 1
                moderated += 1
                results.append({
                    "interaction_id": interaction_id,
                    "found": True,
                    "labels": outcome.get("labels") or [],
                    "priority": outcome.get("priority") or "normal",
                    "moderation": verdict,
                })
            ctx.report_progress((index + 1) / max(total, 1))

    summary = (f"classified {classified}/{total} interaction(s), "
               f"{moderated} moderated, {skipped} skipped")
    if classified:
        _notify(workspace_id, summary, data={
            "classified": classified, "skipped": skipped,
            "verdicts": verdicts, "job": ctx.type,
        })
    return {
        "ok": True,
        "workspace_id": workspace_id,
        "classified": classified,
        "moderated": moderated,
        "skipped": skipped,
        "verdicts": verdicts,
        "results": results,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# COMMUNITY_DRAFT
# ---------------------------------------------------------------------------

def handle_community_draft(ctx: jobs_service.JobContext) -> dict[str, Any]:
    """``COMMUNITY_DRAFT``: draft / queue / auto-send through the policy gate.

    Payload: ``interaction_ids`` (explicit batch; otherwise the oldest
    unread/read rows), ``limit``. Every row goes through
    ``policy.plan_reply``, which applies the workspace autonomy mode:

    * ``DRAFT_ONLY``        → ``draft`` (never sent)
    * ``APPROVAL_REQUIRED`` → ``pending_approval``
    * ``LOW_RISK_AUTO``     → sent only for explicitly allowed, non-escalated
      classes (hard-bypass escalation overrides the mode)
    * ``DISABLED``          → no agent action at all

    Spam / ignored / already-replied / escalated rows and rows blocked by
    moderation are skipped — the send path re-checks all of it anyway.
    """
    from app.db import session_scope
    from app.engine.community.policy import plan_reply
    from app.models.community import SocialInteraction

    payload = ctx.payload or {}
    workspace_id = _require_workspace(ctx)
    limit = _limit(payload)

    with session_scope() as db:
        ids = _explicit_ids(payload) or _pending_ids(db, workspace_id, limit)

    counts = {"drafted": 0, "queued": 0, "escalated": 0, "blocked": 0,
              "sent": 0, "disabled": 0, "skipped": 0}
    actions: list[dict] = []
    total = len(ids)

    with session_scope() as db:
        for index, interaction_id in enumerate(ids):
            jobs_service.check_cancelled(ctx)
            row = db.get(SocialInteraction, interaction_id)
            if (row is None or row.workspace_id != workspace_id
                    or row.status in DRAFT_SKIP_STATUSES
                    or row.moderation_state == "blocked"):
                counts["skipped"] += 1
                actions.append({"interaction_id": interaction_id,
                                "skipped": True})
                ctx.report_progress((index + 1) / max(total, 1))
                continue

            action = plan_reply(db, workspace_id, interaction_id, auto=True)
            if action is None:
                counts["disabled"] += 1
                actions.append({"interaction_id": interaction_id,
                                "skipped": True, "reason": "autonomy_disabled"})
            elif action.action_type == "ESCALATE":
                counts["escalated"] += 1
                actions.append({"interaction_id": interaction_id,
                                "action_id": action.id,
                                "state": action.state})
            elif action.state == "blocked":
                counts["blocked"] += 1
                actions.append({"interaction_id": interaction_id,
                                "action_id": action.id,
                                "state": action.state,
                                "reason": action.error})
            elif action.state == "sent":
                counts["sent"] += 1
                actions.append({"interaction_id": interaction_id,
                                "action_id": action.id,
                                "state": action.state})
            elif action.state in ("pending_approval", "approved"):
                counts["queued"] += 1
                actions.append({"interaction_id": interaction_id,
                                "action_id": action.id,
                                "state": action.state})
            else:
                counts["drafted"] += 1
                actions.append({"interaction_id": interaction_id,
                                "action_id": action.id,
                                "state": action.state})
            ctx.report_progress((index + 1) / max(total, 1))

    worked = total - counts["skipped"]
    summary = (f"processed {worked}/{total} interaction(s): "
               f"{counts['drafted']} draft(s), {counts['queued']} queued, "
               f"{counts['sent']} auto-sent, {counts['escalated']} escalated, "
               f"{counts['blocked']} blocked, {counts['disabled']} disabled")
    if worked:
        _notify(workspace_id, summary, data={**counts, "job": ctx.type})
    return {
        "ok": True,
        "workspace_id": workspace_id,
        **counts,
        "total": total,
        "actions": actions,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# COMMUNITY_ACTION
# ---------------------------------------------------------------------------

def handle_community_action(ctx: jobs_service.JobContext) -> dict[str, Any]:
    """``COMMUNITY_ACTION``: approve / reject / send one existing action.

    Payload: ``action_id`` (required), ``op`` (required: ``approve`` |
    ``reject`` | ``send``), ``user_id`` (approve/reject actor), ``reason``
    (reject), ``actor`` + ``auto`` (send; ``auto=true`` keeps the action on
    the agent path so volume limits apply).

    Cross-workspace ids and illegal state transitions come back as a structured
    ``ok=False`` result: they are deterministic, retrying can never fix them.
    """
    from app.db import session_scope
    from app.engine.community.policy import (
        CommunityPolicyError,
        approve_action,
        reject_action,
        send_action,
    )

    payload = ctx.payload or {}
    workspace_id = _require_workspace(ctx)
    action_id = str(payload.get("action_id") or "").strip()
    op = str(payload.get("op") or "").strip().lower()
    if not action_id:
        raise ValueError("COMMUNITY_ACTION job requires action_id")
    if op not in ACTION_OPS:
        raise ValueError(f"COMMUNITY_ACTION op must be one of {ACTION_OPS}")

    user_id = str(payload.get("user_id") or "")
    reason = str(payload.get("reason") or "")
    actor = str(payload.get("actor") or ("agent" if payload.get("auto") else "operator"))
    is_auto = bool(payload.get("auto"))

    with session_scope() as db:
        try:
            if op == "approve":
                row = approve_action(db, workspace_id, action_id, user_id)
                result: dict[str, Any] = {"ok": True, "state": row.state,
                                          "action_id": row.id}
            elif op == "reject":
                row = reject_action(db, workspace_id, action_id, user_id, reason)
                result = {"ok": True, "state": row.state, "action_id": row.id}
            else:
                sent = send_action(db, workspace_id, action_id, actor,
                                   is_auto=is_auto)
                result = {"ok": bool(sent.get("sent")),
                          "action_id": action_id, **sent}
        except CommunityPolicyError as exc:
            # isolation violation / illegal transition: deterministic, no retry
            result = {"ok": False, "action_id": action_id, "op": op,
                      "reason": str(exc)[:300]}

    if result.get("ok"):
        _notify(workspace_id,
                f"community {op}: {result.get('state') or result.get('reason') or 'ok'}",
                data={"action_id": action_id, "op": op, "auto": is_auto})
    return {
        "ok": bool(result.get("ok")),
        "workspace_id": workspace_id,
        "action_id": action_id,
        "op": op,
        "result": result,
        "summary": (f"{op} {'accepted' if result.get('ok') else 'refused'}: "
                    + str(result.get('reason') or result.get('state') or ''))[:200],
    }


# ---------------------------------------------------------------------------
# registration (idempotent)
# ---------------------------------------------------------------------------

_HANDLERS: tuple[tuple[str, Any], ...] = (
    ("INTERACTION_CLASSIFY", handle_interaction_classify),
    ("COMMUNITY_DRAFT", handle_community_draft),
    ("COMMUNITY_ACTION", handle_community_action),
)


def register_community_jobs() -> None:
    """Register every community handler — idempotent, safe on repeat/reload."""
    for job_type, fn in _HANDLERS:
        if job_type in jobs_service._handlers:
            continue
        jobs_service.register_handler(job_type, fn)


register_community_jobs()


__all__ = [
    "ACTION_OPS",
    "DEFAULT_BATCH",
    "MAX_BATCH",
    "handle_community_action",
    "handle_community_draft",
    "handle_interaction_classify",
    "register_community_jobs",
]
