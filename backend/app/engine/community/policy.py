"""Policy gate + send path for community actions (Work 09, Lane C).

Architecture: CommunityManager → Authorized Action → **Policy Gate** →
Platform Provider → Secret Resolver.

The gate is deterministic. AI/decision judgments never override auth, RBAC,
workspace isolation, budgets, publishing authorization, compliance,
idempotency or DB invariants — those are enforced here, in code that does not
consult any model.

Send path rules
---------------
* ``plan_reply`` creates the CommunityAction according to the workspace
  autonomy mode (DRAFT_ONLY → draft, APPROVAL_REQUIRED → pending_approval,
  LOW_RISK_AUTO → draft then immediate agent send for explicitly allowed
  classes only, DISABLED → no agent action at all).
* The hard-bypass escalation gate (financial/legal/refund/security/
  sensitive-complaint/uncertain-claim/securities/abuse) overrides EVERY mode
  including LOW_RISK_AUTO and produces an ESCALATE action.
* Agents never receive raw OAuth secrets: the send path resolves the
  workspace-scoped ``SocialAccount`` ORM row (or fails with
  ``account not found``) and passes ONLY that row — never a decrypted token —
  to the provider layer, which decrypts credentials internally.
* Limits (auto sends): emergency disable → rate cap → duplicate → cooldown →
  daily cap. Human-initiated sends bypass volume limits (an operator
  explicitly pressing "send" is the documented escape hatch for emergency
  disable) but NEVER bypass brand, escalation, duplicate or state checks.
* Every outgoing action writes an AuditLog row and is verified through the
  CompletionVerifier ``community_reply`` kind.
"""

from __future__ import annotations

import difflib
import inspect
from datetime import timedelta
from typing import Any

from app.engine.community import draft as draft_mod
from app.engine.community.autonomy import (
    AutonomyConfig,
    escalation_details,
    escalation_required,
    load_autonomy,
)
from app.engine.community.classify import labels_of
from app.models.base import utcnow
from app.models.community import CommunityAction, SocialInteraction

DAILY_WINDOW = timedelta(days=1)
RATE_WINDOW = timedelta(minutes=10)
# double-send claim: a crashed sender's claim expires so retries aren't stuck
SEND_CLAIM_TTL = timedelta(seconds=120)
DUPLICATE_SIMILARITY = 0.9
TERMINAL_UNSENDABLE = ("sent", "rejected", "blocked", "stale")


class CommunityPolicyError(Exception):
    """Isolation violation or invalid state transition on the send path."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ws_id(ws: Any) -> str:
    return getattr(ws, "id", None) or str(ws or "")


def _load_workspace_settings(db, ws_id: str):
    from app.models import Workspace

    return db.get(Workspace, ws_id)


def autonomy_for(db, ws_id: str, platform: str = "") -> AutonomyConfig:
    return load_autonomy(_load_workspace_settings(db, ws_id), platform)


def _load_interaction(db, ws_id: str, interaction_id: str) -> SocialInteraction:
    row = db.get(SocialInteraction, interaction_id)
    if row is None or row.workspace_id != ws_id:
        raise CommunityPolicyError(f"interaction {interaction_id} not found")
    return row


def _load_action(db, ws_id: str, action_id: str) -> CommunityAction:
    row = db.get(CommunityAction, action_id)
    if row is None or row.workspace_id != ws_id:
        raise CommunityPolicyError(f"action {action_id} not found")
    return row


def _load_account(db, ws_id: str, account_id: str):
    """Workspace-scoped SocialAccount row for the send path.

    Returns the ORM row ONLY — tokens/decrypted secrets never leave this
    lookup. A missing row (including another workspace's account) raises an
    honest ``CommunityPolicyError`` instead of silently sending.
    """
    from sqlalchemy import select

    from app.models import SocialAccount

    row = db.scalar(
        select(SocialAccount)
        .where(SocialAccount.id == (account_id or ""),
               SocialAccount.workspace_id == ws_id)
    )
    if row is None:
        raise CommunityPolicyError(
            f"account not found: {account_id or '(none linked)'} "
            f"in workspace {ws_id}")
    return row


def _existing_action(db, ws_id: str, interaction_id: str) -> CommunityAction | None:
    from sqlalchemy import select

    rows = db.scalars(
        select(CommunityAction)
        .where(
            CommunityAction.workspace_id == ws_id,
            CommunityAction.interaction_id == interaction_id,
        )
        .order_by(CommunityAction.created_at.asc())
    ).all()
    for row in rows:
        if row.action_type in ("REPLY", "ESCALATE", "APPROVAL_REQUEST") \
                and row.state not in ("rejected", "failed", "stale"):
            return row
    return None


def _new_action(db, interaction, *, action_type: str, mode: str, state: str,
                draft_text: str = "", origin: str = "ai",
                brand_check: dict | None = None, error: str = "") -> CommunityAction:
    action = CommunityAction(
        workspace_id=interaction.workspace_id,
        interaction_id=interaction.id,
        conversation_id=interaction.conversation_id,
        account_id=interaction.account_id or "",
        platform=interaction.platform or "",
        action_type=action_type,
        mode=mode,
        state=state,
        origin=origin,
        draft_text=(draft_text or "")[:20000],
        brand_check_json=dict(brand_check or {}),
        error=error[:2000],
    )
    db.add(action)
    db.flush()
    return action


def _audit(db, *, ws_id: str, actor: str, action_id: str, event: str,
           detail: dict) -> None:
    from app.models import AuditLog

    user_id = actor if actor and actor not in ("auto", "agent", "system") else None
    db.add(AuditLog(
        workspace_id=ws_id,
        user_id=user_id,
        action=f"community.action.{event}",
        resource_type="community_action",
        resource_id=action_id,
        detail_json=dict(detail),
    ))
    db.flush()


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------

def plan_reply(db, ws: Any, interaction_id: str, *, text: str = "",
               labels: list[str] | None = None, actor: str = "agent",
               auto: bool = True, max_tokens: int = 1200) -> CommunityAction | None:
    """Create (or return) the community action for one interaction.

    Returns ``None`` only when the agent is not allowed to act at all
    (mode DISABLED). The returned action's ``state`` encodes the outcome:
    draft / pending_approval / approved / blocked (brand) / sent (LOW_RISK_AUTO).
    """
    ws_id = _ws_id(ws)
    interaction = _load_interaction(db, ws_id, interaction_id)
    cfg = autonomy_for(db, ws_id, interaction.platform)
    mode = cfg.mode_for(interaction.platform)

    if mode == "DISABLED" and auto:
        return None  # no agent actions while disabled

    label_values = labels if labels is not None else labels_of(interaction)

    existing = _existing_action(db, ws_id, interaction.id)
    if existing is not None:
        if existing.state == "draft" and auto and mode == "LOW_RISK_AUTO":
            _maybe_auto_send(db, ws, existing, cfg, label_values, actor=actor)
        return existing

    # -- draft text ------------------------------------------------------
    brand_check: dict = {}
    if text:
        policy = draft_mod.resolve_policy(
            db, ws_id, platform=interaction.platform,
            campaign_id=getattr(interaction, "campaign_id", None))
        hits = draft_mod.forbidden_hits(policy, text)
        body = draft_mod.apply_disclaimers(policy, text) if not hits else ""
        # On a hit the body is blanked for the send path; brand-check the
        # original text so brand_check_json / the audit trail keep the
        # offending phrases instead of recomputing them from "".
        brand_check = draft_mod.brand_check(
            policy, text if hits else body, blocked=bool(hits))
        blocked = bool(hits)
    else:
        generated = draft_mod.generate_reply(
            db, ws_id, interaction, labels=label_values, max_tokens=max_tokens)
        body = generated["text"]
        brand_check = generated["brand_check"]
        blocked = bool(generated["blocked"])

    if blocked or not body:
        action = _new_action(
            db, interaction, action_type="REPLY", mode=mode, state="blocked",
            draft_text="", brand_check=brand_check,
            error="forbidden_phrase" if blocked else "empty_draft",
        )
        _audit(db, ws_id=ws_id, actor=actor, action_id=action.id, event="blocked",
               detail={"reason": action.error, "mode": mode,
                       "forbidden_hits": brand_check.get("forbidden_hits", [])})
        return action

    # -- hard bypass: escalation overrides every mode ---------------------
    escalate, reason = escalation_required(body, label_values)
    if escalate:
        action = _new_action(
            db, interaction, action_type="ESCALATE", mode=mode,
            state="pending_approval", draft_text=body, brand_check=brand_check,
            error=f"escalated:{reason}",
        )
        interaction.status = "escalated"
        db.flush()
        _audit(db, ws_id=ws_id, actor=actor, action_id=action.id,
               event="escalated", detail={"reason": reason,
                                          "mode": mode,
                                          "labels": label_values,
                                          "triggers": escalation_details(
                                              body, label_values)})
        return action

    # -- mode → state ------------------------------------------------------
    if mode == "APPROVAL_REQUIRED" and auto:
        state, action_type = "pending_approval", "APPROVAL_REQUEST"
    else:
        state, action_type = "draft", "REPLY"
    origin = "ai" if auto else "human_edited"
    action = _new_action(db, interaction, action_type=action_type, mode=mode,
                         state=state, draft_text=body, origin=origin,
                         brand_check=brand_check)
    if interaction.status in ("unread", "read", "classified"):
        interaction.status = "drafted"
    db.flush()

    if auto and mode == "LOW_RISK_AUTO":
        _maybe_auto_send(db, ws, action, cfg, label_values, actor="auto")
    return action


def _maybe_auto_send(db, ws, action: CommunityAction, cfg: AutonomyConfig,
                     labels: list[str], *, actor: str = "auto") -> dict:
    """LOW_RISK_AUTO: send only explicitly allowed, non-escalated classes."""
    allowed, why = cfg.allows_auto(action.platform, labels)
    if not allowed:
        return {"sent": False, "reason": why}
    escalate, reason = escalation_required(
        action.final_text or action.draft_text, labels)
    if escalate:
        action.action_type = "ESCALATE"
        action.state = "pending_approval"
        action.error = f"escalated:{reason}"
        db.flush()
        return {"sent": False, "reason": f"escalated:{reason}"}
    return send_action(db, ws, action.id, actor, is_auto=True)


# ---------------------------------------------------------------------------
# approval
# ---------------------------------------------------------------------------

def approve_action(db, ws: Any, action_id: str, user_id: str) -> CommunityAction:
    """Workspace-scoped approval; a foreign workspace cannot see the action."""
    ws_id = _ws_id(ws)
    action = _load_action(db, ws_id, action_id)
    if action.state not in ("draft", "pending_approval"):
        raise CommunityPolicyError(
            f"action {action_id} is {action.state} and cannot be approved")
    if action.action_type == "ESCALATE":
        raise CommunityPolicyError("escalations require human resolution, not approval")
    action.state = "approved"
    action.approval_user_id = str(user_id or "")
    action.error = ""
    db.flush()
    _audit(db, ws_id=ws_id, actor=user_id, action_id=action.id, event="approved",
           detail={"previous_mode": action.mode})
    return action


def reject_action(db, ws: Any, action_id: str, user_id: str,
                  reason: str = "") -> CommunityAction:
    ws_id = _ws_id(ws)
    action = _load_action(db, ws_id, action_id)
    if action.state == "sent":
        raise CommunityPolicyError("a sent action cannot be rejected")
    action.state = "rejected"
    action.rejected_user_id = str(user_id or "")
    action.error = f"rejected:{reason}"[:500] if reason else "rejected"
    db.flush()
    _audit(db, ws_id=ws_id, actor=user_id, action_id=action.id, event="rejected",
           detail={"reason": reason[:200]})
    return action


# ---------------------------------------------------------------------------
# limits (deterministic)
# ---------------------------------------------------------------------------

def _sent_since(db, ws_id: str, since) -> list[CommunityAction]:
    from sqlalchemy import select

    return list(db.scalars(
        select(CommunityAction).where(
            CommunityAction.workspace_id == ws_id,
            CommunityAction.state == "sent",
            CommunityAction.sent_at >= since,
        )
    ).all())


def _normalize(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _duplicate_reason(db, action: CommunityAction, text: str) -> str:
    """Idempotency: one sent reply per interaction, no near-identical repeats."""
    from sqlalchemy import select

    sent = db.scalars(
        select(CommunityAction).where(
            CommunityAction.workspace_id == action.workspace_id,
            CommunityAction.interaction_id == action.interaction_id,
            CommunityAction.state == "sent",
            CommunityAction.id != action.id,
        )
    ).all()
    if sent:
        return "duplicate_reply"

    conversation_id = action.conversation_id
    if not conversation_id:
        return ""
    recent = db.scalars(
        select(CommunityAction).where(
            CommunityAction.workspace_id == action.workspace_id,
            CommunityAction.conversation_id == conversation_id,
            CommunityAction.state == "sent",
        )
    ).all()
    mine = _normalize(text)
    for other in recent:
        theirs = _normalize(other.final_text or other.draft_text)
        if not theirs or not mine:
            continue
        if theirs == mine:
            return "duplicate_text"
        if difflib.SequenceMatcher(None, mine, theirs).ratio() >= DUPLICATE_SIMILARITY:
            return "duplicate_text"
    return ""


def check_limits(db, ws: Any, action: CommunityAction, cfg: AutonomyConfig | None = None,
                 *, is_auto: bool, text: str = "") -> tuple[bool, str]:
    """Deterministic pre-send gate. Returns (ok, reason)."""
    ws_id = _ws_id(ws) or action.workspace_id
    if cfg is None:
        cfg = autonomy_for(db, ws_id, action.platform)

    body = text or action.final_text or action.draft_text

    # hard bypass re-checked at send time (defense in depth)
    labels = labels_for_action(db, action)
    escalate, reason = escalation_required(body, labels)
    if escalate:
        return False, f"escalation:{reason}"
    if is_auto and action.interaction_id:
        # the INBOUND comment is adversarial input too: an auto-send must
        # clear it, so "I want a refund" never gets an auto-reply even when
        # the generated body avoids every trigger word (hard-bypass promise).
        inbound = db.get(SocialInteraction, action.interaction_id)
        if (inbound is not None and inbound.workspace_id == ws_id
                and (inbound.text or "").strip()):
            escalate, reason = escalation_required(inbound.text, labels)
            if escalate:
                return False, f"escalation_inbound:{reason}"

    dup = _duplicate_reason(db, action, body)
    if dup:
        return False, dup

    if action.action_type == "ESCALATE":
        return False, "escalation_pending"

    if not is_auto:
        # human-initiated send: volume limits are the documented escape hatch
        # (emergency disable blocks AGENT sends only) — duplicates above and
        # brand/state checks below still apply.
        return True, ""

    if cfg.emergency_disable:
        return False, "emergency_disable"
    if cfg.daily_cap:
        sent_day = len(_sent_since(db, ws_id, utcnow() - DAILY_WINDOW))
        if sent_day >= cfg.daily_cap:
            return False, "daily_cap"
    if cfg.rate_per_10min:
        sent_rate = len(_sent_since(db, ws_id, utcnow() - RATE_WINDOW))
        if sent_rate >= cfg.rate_per_10min:
            return False, "rate_per_10min"
    if cfg.cooldown_seconds and action.conversation_id:
        from sqlalchemy import select

        recent = db.scalars(
            select(CommunityAction).where(
                CommunityAction.workspace_id == ws_id,
                CommunityAction.conversation_id == action.conversation_id,
                CommunityAction.state == "sent",
                CommunityAction.id != action.id,
            ).order_by(CommunityAction.sent_at.desc())
        ).first()
        if recent is not None and recent.sent_at is not None:
            age = (utcnow() - recent.sent_at).total_seconds()
            if age < cfg.cooldown_seconds:
                return False, "cooldown"
    return True, ""


def labels_for_action(db, action: CommunityAction) -> list[str]:
    row = db.get(SocialInteraction, action.interaction_id)
    return labels_of(row) if row is not None else []


# ---------------------------------------------------------------------------
# provider send
# ---------------------------------------------------------------------------

def _get_provider(platform: str):
    """Lazy provider resolution (Lane A). Decrypts nothing — ids only."""
    try:
        from app.providers.social import get_provider  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - sibling lane may not be landed yet
        return None
    try:
        return get_provider(platform)
    except Exception:  # noqa: BLE001
        return None


def _receipt_dict(receipt: Any) -> dict:
    import dataclasses

    if isinstance(receipt, dict):
        return dict(receipt)
    if dataclasses.is_dataclass(receipt) and not isinstance(receipt, type):
        return dataclasses.asdict(receipt)
    for attr in ("to_dict", "as_dict", "model_dump"):
        fn = getattr(receipt, attr, None)
        if callable(fn):
            try:
                value = fn()
                if isinstance(value, dict):
                    return value
            except Exception:  # noqa: BLE001
                pass
    if hasattr(receipt, "__dict__"):
        return {k: v for k, v in vars(receipt).items() if not k.startswith("_")}
    return {"value": str(receipt)[:500]}


def _call_reply(provider: Any, *, comment_id: str, text: str, account_id: str,
                workspace_id: str, platform: str, account: Any = None) -> Any:
    """Call provider.reply_to_comment with only the parameters it accepts.

    ``account`` is the workspace-scoped ``SocialAccount`` ORM row (Lane A's
    providers take ``(account, remote_id, text)`` and decrypt tokens
    internally). The row — never a decrypted secret — is part of
    ``candidates`` so signature-matched providers receive it under ``account``.
    """
    fn = getattr(provider, "reply_to_comment", None)
    if not callable(fn):
        raise CommunityPolicyError("provider does not support reply_to_comment")
    candidates = {
        "comment_id": comment_id,
        "text": text,
        "reply_text": text,
        "body": text,
        "account_id": account_id,
        "workspace_id": workspace_id,
        "platform": platform,
        "remote_id": comment_id,
        "account": account,
    }
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(comment_id, text)
    params = signature.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        kwargs = dict(candidates)
    else:
        kwargs = {k: v for k, v in candidates.items() if k in params}
    positional = [candidates[p.name] for p in params.values()
                  if p.kind == inspect.Parameter.POSITIONAL_ONLY and p.name in candidates]
    try:
        return fn(*positional, **kwargs)
    except TypeError:
        # provider with unexpected parameter names: fall back to the two-
        # argument convention (comment_id, text) before giving up.
        return fn(comment_id, text)


def _remote_reply_id(receipt: dict) -> str:
    for key in ("remote_reply_id", "reply_id", "new_reply_id", "id"):
        value = receipt.get(key)
        if value:
            return str(value)
    return ""


def send_action(db, ws: Any, action_id: str, actor: str, *,
                is_auto: bool = False) -> dict:
    """Policy gate → provider → persisted proof + audit + verification."""
    from app.engine.intelligence.verifier import check_reply

    ws_id = _ws_id(ws)
    action = _load_action(db, ws_id, action_id)

    if action.state == "sent":
        return {"sent": False, "reason": "already_sent", "state": action.state,
                "action_id": action.id, "remote_reply_id": action.remote_reply_id}
    if action.state in ("rejected", "blocked", "stale"):
        return {"sent": False, "reason": f"state_{action.state}",
                "state": action.state, "action_id": action.id}
    if action.state == "pending_approval" and is_auto:
        return {"sent": False, "reason": "awaiting_approval",
                "state": action.state, "action_id": action.id}
    if action.state not in ("draft", "approved", "pending_approval", "failed"):
        return {"sent": False, "reason": f"state_{action.state}",
                "state": action.state, "action_id": action.id}

    cfg = autonomy_for(db, ws_id, action.platform)
    # Forced-approval strict mode (Work 10): when the workspace opts in,
    # APPROVAL_REQUIRED accepts ONLY an explicitly approved action — the
    # human draft/pending send escape hatch stays closed until approval.
    # Mode/strict checks run before any text mutation; state is unchanged so
    # the normal approve → send flow still works after this refusal.
    if (
        cfg.strict_approval
        and cfg.mode_for(action.platform) == "APPROVAL_REQUIRED"
        and action.state != "approved"
    ):
        action.error = "approval_required_strict"
        db.flush()
        _audit(db, ws_id=ws_id, actor=actor, action_id=action.id,
               event="blocked",
               detail={"reason": "approval_required_strict",
                       "state": action.state, "is_auto": is_auto})
        return {"sent": False, "reason": "approval_required_strict",
                "state": action.state, "action_id": action.id}
    body = (action.final_text or action.draft_text or "").strip()
    if not body:
        action.state = "blocked"
        action.error = "empty_draft"
        db.flush()
        return {"sent": False, "reason": "empty_draft", "state": action.state,
                "action_id": action.id}

    # brand hard constraints apply to the exact text that would go out
    policy = draft_mod.resolve_policy(db, ws_id, platform=action.platform)
    # a human-typed send must carry required disclosures too — append what is
    # missing and persist, so stored final_text matches the text that goes out
    disclosed = draft_mod.apply_disclaimers(policy, body)
    if disclosed != body:
        body = disclosed
        action.final_text = disclosed
        db.flush()
    hits = draft_mod.forbidden_hits(policy, body)
    if hits:
        action.state = "blocked"
        action.error = "forbidden_phrase"
        action.brand_check_json = draft_mod.brand_check(policy, body, blocked=True)
        db.flush()
        _audit(db, ws_id=ws_id, actor=actor, action_id=action.id, event="blocked",
               detail={"reason": "forbidden_phrase", "forbidden_hits": hits})
        return {"sent": False, "reason": "forbidden_phrase",
                "state": action.state, "action_id": action.id,
                "forbidden_hits": hits}

    ok, reason = check_limits(db, ws, action, cfg, is_auto=is_auto, text=body)
    if not ok:
        action.error = reason
        db.flush()
        _audit(db, ws_id=ws_id, actor=actor, action_id=action.id,
               event="blocked", detail={"reason": reason, "is_auto": is_auto})
        return {"sent": False, "reason": reason, "state": action.state,
                "action_id": action.id}

    provider = _get_provider(action.platform)
    if provider is None:
        action.state = "failed"
        action.error = f"provider unavailable for {action.platform}"
        db.flush()
        _audit(db, ws_id=ws_id, actor=actor, action_id=action.id, event="failed",
               detail={"reason": action.error})
        return {"sent": False, "reason": "provider_unavailable",
                "state": action.state, "action_id": action.id}

    interaction = db.get(SocialInteraction, action.interaction_id)
    if interaction is None or interaction.workspace_id != ws_id:
        raise CommunityPolicyError("interaction missing for action")

    # workspace-scoped account row: providers take the ORM row (they decrypt
    # tokens internally); a foreign/missing account fails honestly here.
    account_id = action.account_id or interaction.account_id or ""
    account = _load_account(db, ws_id, account_id)

    # atomic claim (double-send TOCTOU): the claim COMMITS before the provider
    # call, so a parallel sender for the same row sees send_claimed_at and
    # backs off. A crashed sender's claim expires after SEND_CLAIM_TTL.
    from sqlalchemy import or_, update

    claimed = db.execute(
        update(CommunityAction)
        .where(
            CommunityAction.id == action.id,
            CommunityAction.workspace_id == ws_id,
            or_(
                CommunityAction.send_claimed_at.is_(None),
                CommunityAction.send_claimed_at < utcnow() - SEND_CLAIM_TTL,
            ),
        )
        .values(send_claimed_at=utcnow())
    )
    db.commit()
    if int(getattr(claimed, "rowcount", 0) or 0) != 1:
        return {"sent": False, "reason": "send_in_progress",
                "state": action.state, "action_id": action.id}

    try:
        raw = _call_reply(
            provider,
            comment_id=interaction.remote_id,
            text=body,
            account_id=account_id,
            workspace_id=ws_id,
            platform=action.platform,
            account=account,
        )
    except Exception as exc:  # noqa: BLE001 - provider failure must not crash
        action.state = "failed"
        action.error = f"provider error: {type(exc).__name__}: {exc}"[:500]
        action.send_claimed_at = None  # release so a retry is not locked out
        db.commit()
        _audit(db, ws_id=ws_id, actor=actor, action_id=action.id, event="failed",
               detail={"reason": action.error[:200]})
        return {"sent": False, "reason": "provider_error",
                "state": action.state, "action_id": action.id,
                "error": action.error}

    receipt = _receipt_dict(raw)
    remote_reply_id = _remote_reply_id(receipt)
    if not remote_reply_id:
        action.state = "failed"
        action.error = "provider returned no remote reply id"
        action.provider_receipt_json = receipt
        action.send_claimed_at = None  # release so a retry is not locked out
        db.commit()
        _audit(db, ws_id=ws_id, actor=actor, action_id=action.id, event="failed",
               detail={"reason": action.error})
        return {"sent": False, "reason": "no_remote_reply_id",
                "state": action.state, "action_id": action.id}

    is_mock = bool(receipt.get("mock") or receipt.get("is_mock")) or bool(
        getattr(action, "is_mock", False))
    action.state = "sent"
    action.final_text = body
    action.origin = "auto" if is_auto else "human_edited"
    action.provider_receipt_json = receipt
    action.remote_reply_id = remote_reply_id
    action.is_mock = is_mock
    action.sent_at = utcnow()
    action.error = ""
    interaction.status = "replied"
    db.flush()

    execution, verification, checks = check_reply(action, receipt,
                                                  action.account_id,
                                                  action.platform)
    action.verification_json = {
        "execution_status": execution,
        "verification_status": verification,
        "checks": checks,
        "is_mock": is_mock,
        "checked_at": utcnow().isoformat(),
    }
    _audit(db, ws_id=ws_id, actor=actor, action_id=action.id, event="sent",
           detail={
               "platform": action.platform,
               "account_id": action.account_id,
               "remote_reply_id": remote_reply_id,
               "is_mock": is_mock,
               "is_auto": is_auto,
               "mode": action.mode,
               "verification_status": verification,
           })
    db.commit()  # persist the sent proof atomically; caller commits are no-ops
    return {
        "sent": True,
        "reason": "",
        "state": action.state,
        "action_id": action.id,
        "remote_reply_id": remote_reply_id,
        "is_mock": is_mock,
        "receipt": receipt,
        "verification_status": verification,
    }


__all__ = [
    "CommunityPolicyError",
    "DUPLICATE_SIMILARITY",
    "approve_action",
    "autonomy_for",
    "check_limits",
    "labels_for_action",
    "plan_reply",
    "reject_action",
    "send_action",
]
