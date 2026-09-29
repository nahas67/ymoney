"""Work 09 Lane C — community policy gate: autonomy, escalation, limits,
workspace isolation, provider send path and receipt verification.

Everything runs against in-test provider fakes (Lane A's ``reply_to_comment``
contract) and in-process drafts — no network.
"""
from __future__ import annotations

import uuid

import pytest

# ---------------------------------------------------------------------------
# seeding helpers
# ---------------------------------------------------------------------------


def _mk_workspace(db, settings_json=None) -> str:
    from app.models import Workspace

    ws = Workspace(name="Policy WS", slug=f"pws-{uuid.uuid4().hex[:8]}",
                   niche="money", settings_json=dict(settings_json or {}))
    db.add(ws)
    db.flush()
    return ws.id


def _set_community(db, ws_id: str, **community) -> str:
    """Merge ``settings_json["community"]`` (the autonomy settings surface)."""
    from app.models import Workspace

    ws = db.get(Workspace, ws_id)
    settings = dict(ws.settings_json or {})
    settings["community"] = community
    ws.settings_json = settings
    db.flush()
    return ws_id


def _set_brand_defaults(db, ws_id: str, **defaults) -> str:
    """Effective-policy surface: ``settings_json["brand_defaults"]``."""
    from app.models import Workspace

    ws = db.get(Workspace, ws_id)
    settings = dict(ws.settings_json or {})
    merged = dict(settings.get("brand_defaults") or {})
    merged.update(defaults)
    settings["brand_defaults"] = merged
    ws.settings_json = settings
    db.flush()
    return ws_id


def _mk_account(db, ws_id: str, platform: str = "youtube"):
    from app.models import SocialAccount

    account = SocialAccount(workspace_id=ws_id, platform=platform,
                            display_name=f"{platform}-main",
                            access_token_enc="enc-at-rest")
    db.add(account)
    db.flush()
    return account


def _mk_interaction(db, ws_id: str, *, account_id: str, text: str = "Nice video!",
                    labels: list[str] | None = None, platform: str = "youtube",
                    conversation_id: str | None = None, remote_id: str | None = None,
                    status: str = "classified"):
    from app.models.community import SocialInteraction

    row = SocialInteraction(
        workspace_id=ws_id,
        platform=platform,
        account_id=account_id,
        remote_id=remote_id or f"r-{uuid.uuid4().hex[:12]}",
        text=text,
        status=status,
        conversation_id=conversation_id,
        classifications_json=[
            {"label": lb, "provider": "deterministic", "model": "",
             "confidence": 1.0, "evidence": "seeded", "source": "rule"}
            for lb in (labels or [])
        ],
    )
    db.add(row)
    db.flush()
    return row


def _prior_sent(db, ws_id: str, *, account_id: str, conversation_id: str = "conv-prior",
                text: str = "An earlier reply from us."):
    """A sent action for ANOTHER interaction (limits/duplicate fixtures)."""
    from app.models.base import utcnow
    from app.models.community import CommunityAction

    other = _mk_interaction(db, ws_id, account_id=account_id,
                            text="an earlier comment", labels=["POSITIVE"],
                            conversation_id=conversation_id, status="replied")
    action = CommunityAction(
        workspace_id=ws_id, interaction_id=other.id,
        conversation_id=conversation_id, account_id=account_id,
        platform="youtube", action_type="REPLY", mode="LOW_RISK_AUTO",
        state="sent", origin="auto", draft_text=text, final_text=text,
        remote_reply_id="prior-remote-1", sent_at=utcnow(),
    )
    db.add(action)
    db.flush()
    return other, action


# ---------------------------------------------------------------------------
# provider fakes (Lane A contract)
# ---------------------------------------------------------------------------


class RecordingProvider:
    """Lane A signature: ``reply_to_comment(self, account, remote_id, text)``.

    ``account`` is the ORM SocialAccount row (the provider decrypts inside).
    """

    def __init__(self, *, mock: bool = False):
        self.calls: list[dict] = []
        self.mock = mock

    def reply_to_comment(self, account, remote_id, text):
        self.calls.append({"account": account, "remote_id": remote_id,
                           "text": text})
        if self.mock:
            return {"mock": True, "remote_reply_id": f"mock-{len(self.calls)}"}
        return {
            "remote_reply_id": f"reply-{len(self.calls)}",
            "account_id": account.id,
            "platform": account.platform,
        }


class LegacyProvider:
    """Pre-fix provider style: ``reply_to_comment(comment_id, text)``."""

    def __init__(self):
        self.calls: list[dict] = []

    def reply_to_comment(self, comment_id, text):
        self.calls.append({"comment_id": comment_id, "text": text})
        return {"remote_reply_id": f"legacy-{len(self.calls)}"}


def _patch_provider(monkeypatch, provider=None):
    from app.engine.community import policy as policy_mod

    provider = provider if provider is not None else RecordingProvider()
    monkeypatch.setattr(policy_mod, "_get_provider", lambda platform: provider)
    return provider


# ---------------------------------------------------------------------------
# 5. autonomy modes
# ---------------------------------------------------------------------------


def test_defaults_to_draft_only_and_never_sends(db_session, workspace_with_user,
                                                monkeypatch):
    from sqlalchemy import select

    from app.engine.community.policy import plan_reply
    from app.models.community import CommunityAction

    ws = workspace_with_user["workspace"]
    provider = _patch_provider(monkeypatch)
    account = _mk_account(db_session, ws)
    row = _mk_interaction(db_session, ws, account_id=account.id)

    action = plan_reply(db_session, ws, row.id, text="Thanks for watching!",
                        labels=["POSITIVE"], auto=True)

    assert action is not None
    assert action.state == "draft" and action.mode == "DRAFT_ONLY"
    assert action.action_type == "REPLY"
    sent = db_session.scalars(
        select(CommunityAction).where(
            CommunityAction.workspace_id == ws,
            CommunityAction.state == "sent",
        )).all()
    assert sent == []
    assert provider.calls == []


def test_approval_required_draft_approve_send_and_reject(db_session,
                                                         workspace_with_user,
                                                         monkeypatch):
    from app.engine.community.policy import approve_action, plan_reply, reject_action, send_action

    ws = workspace_with_user["workspace"]
    provider = _patch_provider(monkeypatch)
    account = _mk_account(db_session, ws)

    _set_community(db_session, ws, mode="APPROVAL_REQUIRED")
    first = _mk_interaction(db_session, ws, account_id=account.id)
    action = plan_reply(db_session, ws, first.id, text="Happy to help!",
                        labels=["POSITIVE"], auto=True)
    assert action.state == "pending_approval"
    assert action.action_type == "APPROVAL_REQUEST"

    # agent may not push a pending action out on its own
    blocked = send_action(db_session, ws, action.id, "agent", is_auto=True)
    assert blocked["sent"] is False and blocked["reason"] == "awaiting_approval"

    approved = approve_action(db_session, ws, action.id, "user-1")
    assert approved.state == "approved" and approved.approval_user_id == "user-1"

    sent = send_action(db_session, ws, action.id, "operator", is_auto=False)
    assert sent["sent"] is True and sent["state"] == "sent"
    assert len(provider.calls) == 1
    assert sent["remote_reply_id"].startswith("reply-")
    assert action.verification_json["verification_status"] == "VERIFIED"

    # reject path
    second = _mk_interaction(db_session, ws, account_id=account.id)
    queued = plan_reply(db_session, ws, second.id, text="Another reply.",
                        labels=["POSITIVE"], auto=True)
    assert queued.state == "pending_approval"
    rejected = reject_action(db_session, ws, queued.id, "user-2", "not on brand")
    assert rejected.state == "rejected"
    refused = send_action(db_session, ws, queued.id, "operator", is_auto=False)
    assert refused["sent"] is False and refused["reason"] == "state_rejected"
    assert len(provider.calls) == 1  # only the approved send went out


def test_low_risk_auto_sends_only_allowed_classes(db_session, workspace_with_user,
                                                  monkeypatch):
    from app.engine.community.policy import plan_reply

    ws = workspace_with_user["workspace"]
    provider = _patch_provider(monkeypatch)
    account = _mk_account(db_session, ws)
    _set_community(db_session, ws, mode="LOW_RISK_AUTO",
                   auto_reply_classes=["POSITIVE"])

    allowed = _mk_interaction(db_session, ws, account_id=account.id,
                              text="Love this, great work!")
    sent_action = plan_reply(db_session, ws, allowed.id,
                             text="Thanks so much, glad you loved it!",
                             labels=["POSITIVE"], auto=True)
    assert sent_action.state == "sent"
    assert sent_action.mode == "LOW_RISK_AUTO"
    assert sent_action.origin == "auto"
    assert len(provider.calls) == 1

    disallowed = _mk_interaction(db_session, ws, account_id=account.id,
                                 text="Please add a dark mode, suggestion.")
    draft = plan_reply(db_session, ws, disallowed.id,
                       text="Thanks for the suggestion — passing it along.",
                       labels=["FEEDBACK"], auto=True)
    assert draft.state == "draft"
    assert draft.action_type == "REPLY"
    assert len(provider.calls) == 1  # the disallowed class never sent


def test_disabled_blocks_agent_actions_human_still_drafts(db_session,
                                                          workspace_with_user,
                                                          monkeypatch):
    from app.engine.community.policy import plan_reply

    ws = workspace_with_user["workspace"]
    provider = _patch_provider(monkeypatch)
    account = _mk_account(db_session, ws)
    _set_community(db_session, ws, mode="DISABLED")

    row = _mk_interaction(db_session, ws, account_id=account.id)
    assert plan_reply(db_session, ws, row.id, text="Thanks!",
                      labels=["POSITIVE"], auto=True) is None

    human = plan_reply(db_session, ws, row.id, text="Thanks!",
                       labels=["POSITIVE"], auto=False, actor="operator")
    assert human is not None
    assert human.state == "draft" and human.origin == "human_edited"
    assert provider.calls == []


# ---------------------------------------------------------------------------
# 6. hard-bypass escalation (overrides every mode, incl. LOW_RISK_AUTO)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,labels,rule", [
    ("I want a full refund for last month's charge.", ["POSITIVE"], "refund_request"),
    ("Keep this up and our lawyer will file a lawsuit.", ["POSITIVE"], "legal_claim"),
    ("My account was hacked and someone changed my password.",
     ["POSITIVE"], "account_security"),
    ("I am reporting you to the regulator for discrimination.",
     ["POSITIVE"], "sensitive_complaint"),
    ("This guarantees guaranteed returns with zero risk for my stocks.",
     ["POSITIVE"], "securities"),
    ("You people are idiots, absolute garbage.", ["ABUSE"], "abuse_label"),
])
def test_hard_bypass_escalation_overrides_low_risk_auto(db_session,
                                                        workspace_with_user,
                                                        monkeypatch,
                                                        text, labels, rule):
    from sqlalchemy import select

    from app.engine.community.policy import approve_action, plan_reply
    from app.models.community import CommunityAction

    ws = workspace_with_user["workspace"]
    provider = _patch_provider(monkeypatch)
    account = _mk_account(db_session, ws)
    _set_community(db_session, ws, mode="LOW_RISK_AUTO",
                   auto_reply_classes=["POSITIVE", "ABUSE"])

    row = _mk_interaction(db_session, ws, account_id=account.id, text=text)
    action = plan_reply(db_session, ws, row.id, text=text, labels=labels, auto=True)

    assert action.action_type == "ESCALATE"
    assert action.state == "pending_approval"
    assert action.error.startswith("escalated:")
    assert rule in action.error or rule in (action.error or "")
    assert row.status == "escalated"
    assert provider.calls == []
    # scoped to THIS workspace: earlier tests in the session also commit
    # sent rows (shared sqlite), which says nothing about this escalation.
    sent = db_session.scalars(
        select(CommunityAction).where(
            CommunityAction.state == "sent",
            CommunityAction.workspace_id == ws)).all()
    assert sent == []
    with pytest.raises(Exception) as exc:
        approve_action(db_session, ws, action.id, "user-1")
    assert "escalation" in str(exc.value).lower()


def test_inbound_comment_is_scanned_before_auto_send(db_session,
                                                      workspace_with_user,
                                                      monkeypatch):
    """The hard-bypass promise covers the INBOUND comment too: with a clean
    generated body, an auto-send to 'I want a refund' is still refused
    (escalation_inbound) — while the documented human escape hatch stays open."""
    from app.engine.community.policy import plan_reply, send_action

    ws = workspace_with_user["workspace"]
    provider = _patch_provider(monkeypatch)
    account = _mk_account(db_session, ws)
    _set_community(db_session, ws, mode="LOW_RISK_AUTO",
                   auto_reply_classes=["POSITIVE"])

    row = _mk_interaction(db_session, ws, account_id=account.id,
                          text="I want a full refund for last month's charge.",
                          labels=["POSITIVE"])
    action = plan_reply(db_session, ws, row.id, text="Happy to help!",
                        labels=["POSITIVE"], auto=True)

    # outbound body is clean, but the inbound comment trips the gate
    assert action.state == "draft" and not provider.calls
    assert action.error.startswith("escalation_inbound:")
    assert "refund_request" in action.error

    # human-initiated send is the documented escape hatch (not gated on the
    # inbound scan) and goes through the provider for real
    result = send_action(db_session, ws, action.id, "operator", is_auto=False)
    assert result["sent"] is True, result
    assert action.state == "sent"
    assert len(provider.calls) == 1


def test_human_send_appends_required_disclaimers(db_session,
                                                 workspace_with_user,
                                                 monkeypatch):
    """LOW-7: an operator-typed send (created outside plan_reply, exactly like
    the inbox reply/send routes) carries required disclosures — the stored
    final_text matches the text that actually goes to the provider."""
    from app.engine.community.policy import send_action
    from app.models.community import CommunityAction, SocialInteraction

    ws = workspace_with_user["workspace"]
    _set_brand_defaults(db_session, ws, required_disclaimers=["Sponsored content"])
    provider = _patch_provider(monkeypatch)
    account = _mk_account(db_session, ws)
    row = _mk_interaction(db_session, ws, account_id=account.id)

    # built directly (the API route path), so no plan_reply pre-applies them
    action = CommunityAction(
        workspace_id=ws, interaction_id=row.id, conversation_id=None,
        account_id=account.id, platform="youtube",
        action_type="REPLY", mode="DRAFT_ONLY", state="draft",
        origin="human_edited", draft_text="Thanks for watching!",
    )
    db_session.add(action)
    db_session.flush()

    result = send_action(db_session, ws, action.id, "operator", is_auto=False)
    assert result["sent"] is True, result
    assert action.final_text == "Thanks for watching! Sponsored content"
    assert provider.calls[0]["text"] == action.final_text

    stored = db_session.get(SocialInteraction, row.id)
    assert stored is not None  # interaction untouched apart from status


# ---------------------------------------------------------------------------
# 7. limits
# ---------------------------------------------------------------------------


def test_daily_cap_blocks_auto_send(db_session, workspace_with_user, monkeypatch):
    from app.engine.community.policy import plan_reply

    ws = workspace_with_user["workspace"]
    provider = _patch_provider(monkeypatch)
    account = _mk_account(db_session, ws)
    _set_community(db_session, ws, mode="LOW_RISK_AUTO",
                   auto_reply_classes=["POSITIVE"], daily_cap=1,
                   rate_per_10min=100, cooldown_seconds=0)
    _prior_sent(db_session, ws, account_id=account.id)

    row = _mk_interaction(db_session, ws, account_id=account.id,
                          conversation_id="conv-target")
    action = plan_reply(db_session, ws, row.id, text="Thanks for the kind words!",
                        labels=["POSITIVE"], auto=True)
    assert action.state == "draft" and action.error == "daily_cap"
    assert provider.calls == []


def test_rate_cap_blocks_auto_send(db_session, workspace_with_user, monkeypatch):
    from app.engine.community.policy import plan_reply

    ws = workspace_with_user["workspace"]
    provider = _patch_provider(monkeypatch)
    account = _mk_account(db_session, ws)
    _set_community(db_session, ws, mode="LOW_RISK_AUTO",
                   auto_reply_classes=["POSITIVE"], daily_cap=100,
                   rate_per_10min=1, cooldown_seconds=0)
    _prior_sent(db_session, ws, account_id=account.id)

    row = _mk_interaction(db_session, ws, account_id=account.id,
                          conversation_id="conv-target")
    action = plan_reply(db_session, ws, row.id, text="Thanks for the kind words!",
                        labels=["POSITIVE"], auto=True)
    assert action.state == "draft" and action.error == "rate_per_10min"
    assert provider.calls == []


def test_cooldown_blocks_auto_send(db_session, workspace_with_user, monkeypatch):
    from app.engine.community.policy import plan_reply

    ws = workspace_with_user["workspace"]
    provider = _patch_provider(monkeypatch)
    account = _mk_account(db_session, ws)
    _set_community(db_session, ws, mode="LOW_RISK_AUTO",
                   auto_reply_classes=["POSITIVE"], daily_cap=100,
                   rate_per_10min=100, cooldown_seconds=3600)
    _prior_sent(db_session, ws, account_id=account.id,
                conversation_id="conv-active")

    row = _mk_interaction(db_session, ws, account_id=account.id,
                          conversation_id="conv-active")
    action = plan_reply(db_session, ws, row.id, text="Thanks for the kind words!",
                        labels=["POSITIVE"], auto=True)
    assert action.state == "draft" and action.error == "cooldown"
    assert provider.calls == []


def test_emergency_disable_blocks_auto_but_human_escape_hatch_works(
        db_session, workspace_with_user, monkeypatch):
    from app.engine.community.policy import plan_reply, send_action

    ws = workspace_with_user["workspace"]
    provider = _patch_provider(monkeypatch)
    account = _mk_account(db_session, ws)
    _set_community(db_session, ws, mode="LOW_RISK_AUTO",
                   auto_reply_classes=["POSITIVE"], emergency_disable=True)

    row = _mk_interaction(db_session, ws, account_id=account.id)
    action = plan_reply(db_session, ws, row.id, text="Thanks for watching!",
                        labels=["POSITIVE"], auto=True)
    assert action.state == "draft" and action.error == "emergency_disable"
    assert provider.calls == []

    # documented escape hatch: an operator pressing send bypasses VOLUME limits
    human = send_action(db_session, ws, action.id, "operator", is_auto=False)
    assert human["sent"] is True
    assert len(provider.calls) == 1


def test_duplicate_reply_prevention(db_session, workspace_with_user, monkeypatch):
    from app.engine.community.policy import plan_reply, send_action
    from app.models.community import CommunityAction

    ws = workspace_with_user["workspace"]
    provider = _patch_provider(monkeypatch)
    account = _mk_account(db_session, ws)

    # (a) one sent reply per interaction
    first = _mk_interaction(db_session, ws, account_id=account.id)
    sent_a = plan_reply(db_session, ws, first.id, text="Thanks for watching!",
                        labels=["POSITIVE"], auto=False, actor="operator")
    result = send_action(db_session, ws, sent_a.id, "operator", is_auto=True)
    assert result["sent"] is True

    second = CommunityAction(
        workspace_id=ws, interaction_id=first.id, account_id=account.id,
        platform="youtube", action_type="REPLY", mode="DRAFT_ONLY",
        state="draft", draft_text="A second attempt at the same comment.",
    )
    db_session.add(second)
    db_session.flush()
    dup = send_action(db_session, ws, second.id, "operator", is_auto=True)
    assert dup["sent"] is False and dup["reason"] == "duplicate_reply"
    assert len(provider.calls) == 1

    # (b) near-identical text inside the same conversation
    _prior_sent(db_session, ws, account_id=account.id,
                conversation_id="conv-dupe",
                text="Thanks for watching, glad you enjoyed the video!")
    twin = _mk_interaction(db_session, ws, account_id=account.id,
                           conversation_id="conv-dupe")
    twin_action = CommunityAction(
        workspace_id=ws, interaction_id=twin.id, conversation_id="conv-dupe",
        account_id=account.id, platform="youtube", action_type="REPLY",
        mode="DRAFT_ONLY", state="draft",
        draft_text="Thanks for watching! Glad you enjoyed the video.",
    )
    db_session.add(twin_action)
    db_session.flush()
    near = send_action(db_session, ws, twin_action.id, "operator", is_auto=True)
    assert near["sent"] is False and near["reason"] == "duplicate_text"
    assert len(provider.calls) == 1


# ---------------------------------------------------------------------------
# 9. workspace isolation
# ---------------------------------------------------------------------------


def test_workspace_isolation_denies_foreign_interaction_and_action(
        db_session, workspace_with_user, monkeypatch):
    from app.engine.community.classify import classify_interaction
    from app.engine.community.policy import (
        CommunityPolicyError,
        approve_action,
        plan_reply,
        send_action,
    )
    from app.models.community import SocialInteraction

    ws_a = workspace_with_user["workspace"]
    ws_b = _mk_workspace(db_session)
    provider = _patch_provider(monkeypatch)
    account = _mk_account(db_session, ws_a)

    row = _mk_interaction(db_session, ws_a, account_id=account.id)
    action = plan_reply(db_session, ws_a, row.id, text="Thanks!",
                        labels=["POSITIVE"], auto=False, actor="operator")

    with pytest.raises(CommunityPolicyError):
        plan_reply(db_session, ws_b, row.id, text="sneaky draft",
                   labels=["POSITIVE"], auto=True)
    with pytest.raises(CommunityPolicyError):
        approve_action(db_session, ws_b, action.id, "user-x")
    with pytest.raises(CommunityPolicyError):
        send_action(db_session, ws_b, action.id, "operator", is_auto=False)

    # readers never see the foreign row either
    assert classify_interaction(db_session, ws_b, row.id)["found"] is False
    reread = db_session.get(SocialInteraction, row.id)
    assert reread.status != "escalated" and reread.workspace_id == ws_a
    assert provider.calls == []


# ---------------------------------------------------------------------------
# 10. mock vs live verification + 15. account fix
# ---------------------------------------------------------------------------


def test_mock_receipt_never_verifies_as_live(db_session, workspace_with_user,
                                             monkeypatch):
    from app.engine.community.policy import plan_reply, send_action

    ws = workspace_with_user["workspace"]
    provider = _patch_provider(monkeypatch, RecordingProvider(mock=True))
    account = _mk_account(db_session, ws)
    _set_community(db_session, ws, mode="LOW_RISK_AUTO",
                   auto_reply_classes=["POSITIVE"])

    row = _mk_interaction(db_session, ws, account_id=account.id)
    action = plan_reply(db_session, ws, row.id, text="Thanks for watching!",
                        labels=["POSITIVE"], auto=True)
    assert action.state == "sent"
    assert action.is_mock is True
    verification = action.verification_json
    assert verification["verification_status"] != "VERIFIED"
    assert verification["verification_status"] == "NOT_VERIFIED"
    failed = {c["name"] for c in verification["checks"] if not c["passed"]}
    assert "live_proof" in failed

    # sending the same action again stays idempotent and still not VERIFIED
    again = send_action(db_session, ws, action.id, "operator", is_auto=False)
    assert again["sent"] is False and again["reason"] == "already_sent"
    assert action.verification_json["verification_status"] == "NOT_VERIFIED"
    assert len(provider.calls) == 1


def test_live_four_proof_receipt_verifies(db_session, workspace_with_user,
                                          monkeypatch):
    from app.engine.community.policy import plan_reply

    ws = workspace_with_user["workspace"]
    _patch_provider(monkeypatch)  # live receipts (call count unused here)
    account = _mk_account(db_session, ws)
    _set_community(db_session, ws, mode="LOW_RISK_AUTO",
                   auto_reply_classes=["POSITIVE"])

    row = _mk_interaction(db_session, ws, account_id=account.id)
    action = plan_reply(db_session, ws, row.id, text="Thanks for watching!",
                        labels=["POSITIVE"], auto=True)

    assert action.state == "sent"
    assert action.is_mock is False
    assert action.remote_reply_id == "reply-1"
    verification = action.verification_json
    assert verification["verification_status"] == "VERIFIED"
    checks = {c["name"]: c["passed"] for c in verification["checks"]}
    for name in ("provider_receipt", "remote_reply_id", "action_row_persisted",
                 "account_platform_match"):
        assert checks.get(name) is True, (name, verification["checks"])


def test_provider_receives_the_social_account_orm_row(db_session,
                                                      workspace_with_user,
                                                      monkeypatch):
    """Bug fix: Lane A's (account, remote_id, text) signature gets the row."""
    from app.engine.community.policy import plan_reply, send_action
    from app.models import SocialAccount

    ws = workspace_with_user["workspace"]
    provider = _patch_provider(monkeypatch)
    account = _mk_account(db_session, ws)

    row = _mk_interaction(db_session, ws, account_id=account.id)
    action = plan_reply(db_session, ws, row.id, text="Thanks for watching!",
                        labels=["POSITIVE"], auto=False, actor="operator")
    result = send_action(db_session, ws, action.id, "operator", is_auto=False)

    assert result["sent"] is True, result
    call = provider.calls[0]
    assert isinstance(call["account"], SocialAccount)
    assert call["account"].id == account.id
    assert call["account"].workspace_id == ws
    assert call["remote_id"] == row.remote_id
    assert call["text"] == action.final_text
    # the row is the ORM object itself, not a serialized secret bag
    assert "enc-at-rest" not in str(call)
    assert result["receipt"]["account_id"] == account.id


def test_legacy_provider_signature_still_works(db_session, workspace_with_user,
                                               monkeypatch):
    from app.engine.community.policy import plan_reply, send_action

    ws = workspace_with_user["workspace"]
    provider = _patch_provider(monkeypatch, LegacyProvider())
    account = _mk_account(db_session, ws)

    row = _mk_interaction(db_session, ws, account_id=account.id)
    action = plan_reply(db_session, ws, row.id, text="Thanks for watching!",
                        labels=["POSITIVE"], auto=False, actor="operator")
    result = send_action(db_session, ws, action.id, "operator", is_auto=False)

    assert result["sent"] is True
    assert provider.calls[0] == {"comment_id": row.remote_id,
                                 "text": action.final_text}


def test_account_lookup_is_workspace_scoped(db_session, workspace_with_user,
                                            monkeypatch):
    """Foreign / missing SocialAccount → honest CommunityPolicyError."""
    from app.engine.community.policy import CommunityPolicyError, plan_reply, send_action

    ws_a = workspace_with_user["workspace"]
    ws_b = _mk_workspace(db_session)
    provider = _patch_provider(monkeypatch)

    foreign_account = _mk_account(db_session, ws_b)

    # (a) action in ws-A points at ws-B's account row
    row_foreign = _mk_interaction(db_session, ws_a, account_id=foreign_account.id)
    action_foreign = plan_reply(db_session, ws_a, row_foreign.id,
                                text="Thanks!", labels=["POSITIVE"],
                                auto=False, actor="operator")
    with pytest.raises(CommunityPolicyError) as exc_foreign:
        send_action(db_session, ws_a, action_foreign.id, "operator",
                    is_auto=False)
    assert "account not found" in str(exc_foreign.value)
    assert provider.calls == []

    # (b) action points at an account id that does not exist at all
    row_missing = _mk_interaction(db_session, ws_a, account_id="no-such-account")
    action_missing = plan_reply(db_session, ws_a, row_missing.id,
                                text="Thanks!", labels=["POSITIVE"],
                                auto=False, actor="operator")
    with pytest.raises(CommunityPolicyError) as exc_missing:
        send_action(db_session, ws_a, action_missing.id, "operator",
                    is_auto=False)
    assert "account not found" in str(exc_missing.value)
    assert provider.calls == []
    assert action_foreign.state == "draft"
    assert action_missing.state == "draft"


# ---------------------------------------------------------------------------
# 5b. forced-approval strict mode (Work 10 Section 0)
# ---------------------------------------------------------------------------


def test_strict_approval_blocks_draft_send_until_approved(db_session,
                                                          workspace_with_user,
                                                          monkeypatch):
    """APPROVAL_REQUIRED + ``strict_approval``: no draft/pending escape hatch."""
    from sqlalchemy import select

    from app.engine.community.autonomy import load_autonomy
    from app.engine.community.policy import approve_action, plan_reply, send_action
    from app.models import AuditLog, Workspace

    ws = workspace_with_user["workspace"]
    provider = _patch_provider(monkeypatch)
    account = _mk_account(db_session, ws)
    _set_community(db_session, ws, mode="APPROVAL_REQUIRED", strict_approval=True)

    cfg = load_autonomy(db_session.get(Workspace, ws))
    assert cfg.strict_approval is True
    assert cfg.mode_for("youtube") == "APPROVAL_REQUIRED"

    # (a) human-created draft (the Work 09 escape hatch) is refused
    row_a = _mk_interaction(db_session, ws, account_id=account.id)
    draft = plan_reply(db_session, ws, row_a.id, text="Happy to help!",
                       labels=["POSITIVE"], auto=False, actor="operator")
    assert draft.state == "draft"
    refused = send_action(db_session, ws, draft.id, "operator", is_auto=False)
    assert refused["sent"] is False
    assert refused["reason"] == "approval_required_strict"
    assert refused["state"] == "draft"  # state unchanged — approve can proceed
    assert draft.error == "approval_required_strict"
    assert provider.calls == []  # never reached the provider

    audits = list(db_session.scalars(
        select(AuditLog).where(AuditLog.workspace_id == ws,
                               AuditLog.resource_id == draft.id)).all())
    assert any(a.detail_json.get("reason") == "approval_required_strict"
               for a in audits), "strict refusal must be audited"

    # approve → the normal flow then works (approve clears the error)
    approved = approve_action(db_session, ws, draft.id, "user-1")
    assert approved.state == "approved" and approved.error == ""
    sent = send_action(db_session, ws, draft.id, "operator", is_auto=False)
    assert sent["sent"] is True and sent["state"] == "sent"
    assert len(provider.calls) == 1

    # (b) agent-queued pending actions also require approval in strict mode
    row_b = _mk_interaction(db_session, ws, account_id=account.id)
    queued = plan_reply(db_session, ws, row_b.id, text="Another reply.",
                        labels=["POSITIVE"], auto=True)
    assert queued.state == "pending_approval"
    human_push = send_action(db_session, ws, queued.id, "operator", is_auto=False)
    assert human_push["sent"] is False
    assert human_push["reason"] == "approval_required_strict"
    assert len(provider.calls) == 1


def test_strict_approval_defaults_off_and_only_bites_approval_mode(
        db_session, workspace_with_user, monkeypatch):
    """Work 09 semantics unchanged by default; strict never touches other modes."""
    from app.engine.community.policy import plan_reply, send_action

    ws = workspace_with_user["workspace"]
    provider = _patch_provider(monkeypatch)
    account = _mk_account(db_session, ws)

    # (a) strict_approval absent → default off: human draft send still works
    _set_community(db_session, ws, mode="APPROVAL_REQUIRED")
    row_a = _mk_interaction(db_session, ws, account_id=account.id)
    draft = plan_reply(db_session, ws, row_a.id, text="Happy to help!",
                       labels=["POSITIVE"], auto=False, actor="operator")
    assert draft.state == "draft"
    sent = send_action(db_session, ws, draft.id, "operator", is_auto=False)
    assert sent["sent"] is True
    assert len(provider.calls) == 1

    # (b) strict ON but mode LOW_RISK_AUTO → the auto-send path is unaffected
    _set_community(db_session, ws, mode="LOW_RISK_AUTO",
                   auto_reply_classes=["POSITIVE"], strict_approval=True)
    row_b = _mk_interaction(db_session, ws, account_id=account.id,
                            text="Love this, great work!")
    auto = plan_reply(db_session, ws, row_b.id,
                      text="Thanks so much, glad you loved it!",
                      labels=["POSITIVE"], auto=True)
    assert auto.state == "sent"
    assert len(provider.calls) == 2


def test_strict_approval_malformed_setting_fails_conservative(db_session):
    """Non-bool strict_approval never widens silently — it warns + stays off."""
    from app.engine.community.autonomy import load_autonomy
    from app.models import Workspace

    ws = _mk_workspace(db_session, {"community": {"mode": "APPROVAL_REQUIRED",
                                                  "strict_approval": "yes"}})
    cfg = load_autonomy(db_session.get(Workspace, ws))
    assert cfg.strict_approval is False
    assert any("strict_approval" in w for w in cfg.warnings)
