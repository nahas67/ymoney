"""Work 09 Lane C — community agent coverage.

Classification provenance, lead opportunities, brand-aware drafting (Work 08
effective policy), budgeted thread context, moderation verdicts, workspace
isolation, mock-vs-live reply verification, insights, analytics, jobs
registration, provider account handoff and the ``community_manager`` run.

Everything runs on in-test provider fakes (Lane A's ``reply_to_comment``
contract), in-test brand fixtures and the deterministic conftest LLM
(``MOCK_LLM=true``) — no network.
"""
from __future__ import annotations

import json
import uuid

import pytest

# ---------------------------------------------------------------------------
# seeding helpers (mirror test_community_policy.py)
# ---------------------------------------------------------------------------


def _mk_workspace(db, settings=None) -> str:
    from app.models import Workspace

    ws = Workspace(name="Community Agent WS", slug=f"cas-{uuid.uuid4().hex[:8]}",
                   niche="money", settings_json=dict(settings or {}))
    db.add(ws)
    db.flush()
    return ws.id


def _set_community(db, ws_id: str, **community) -> str:
    """Merge ``settings_json["community"]`` (the autonomy surface)."""
    from app.models import Workspace

    ws = db.get(Workspace, ws_id)
    settings = dict(ws.settings_json or {})
    settings["community"] = community
    ws.settings_json = settings
    db.flush()
    return ws_id


def _set_brand_defaults(db, ws_id: str, **defaults) -> str:
    """Work 08 effective-policy surface: ``settings_json["brand_defaults"]``."""
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
                    status: str = "classified", parent_interaction_id: str | None = None,
                    published_post_id: str | None = None,
                    author_name: str = "viewer"):
    from app.models.community import SocialInteraction

    row = SocialInteraction(
        workspace_id=ws_id,
        platform=platform,
        account_id=account_id,
        remote_id=remote_id or f"r-{uuid.uuid4().hex[:12]}",
        text=text,
        status=status,
        author_name=author_name,
        conversation_id=conversation_id,
        parent_interaction_id=parent_interaction_id,
        published_post_id=published_post_id,
        classifications_json=[
            {"label": lb, "provider": "deterministic", "model": "",
             "confidence": 1.0, "evidence": "seeded", "source": "rule"}
            for lb in (labels or [])
        ],
    )
    db.add(row)
    db.flush()
    return row


def _mk_post(db, ws_id: str, *, platform: str = "youtube", title: str = "Money tips"):
    """PublishedPost has UNIQUE (video_id, platform) — video_id is suffixed."""
    from app.models import PublishedPost

    post = PublishedPost(workspace_id=ws_id, video_id=f"vid-{uuid.uuid4().hex}",
                         platform=platform, title=title,
                         remote_post_id=f"rp-{uuid.uuid4().hex[:8]}")
    db.add(post)
    db.flush()
    return post


# ---------------------------------------------------------------------------
# provider fakes (Lane A contract)
# ---------------------------------------------------------------------------


class RecordingProvider:
    """``reply_to_comment(account, remote_id, text)`` — ``account`` is the ORM row."""

    def __init__(self, *, mock: bool = False):
        self.calls: list[dict] = []
        self.mock = mock

    def reply_to_comment(self, account, remote_id, text):
        self.calls.append({"account": account, "remote_id": remote_id, "text": text})
        if self.mock:
            return {"mock": True, "remote_reply_id": f"mock-{len(self.calls)}"}
        return {"remote_reply_id": f"reply-{len(self.calls)}",
                "account_id": account.id, "platform": account.platform}


def _patch_provider(monkeypatch, provider=None):
    from app.engine.community import policy as policy_mod

    provider = provider if provider is not None else RecordingProvider()
    monkeypatch.setattr(policy_mod, "_get_provider", lambda platform: provider)
    return provider


def _patch_replies(monkeypatch, replies: list[str]) -> list[str]:
    """Replace draft's single LLM seam with a deterministic candidate list."""
    from app.engine.community import draft as draft_mod

    systems: list[str] = []

    def _fake(system: str, user: str, workspace_id: str) -> str:
        systems.append(system)
        return replies[min(len(systems) - 1, len(replies) - 1)]

    monkeypatch.setattr(draft_mod, "_llm_reply", _fake)
    return systems


def _job_ctx(ws_id: str, payload: dict | None = None):
    from app.services.jobs import JobContext

    return JobContext(job_id=None, type="community_test", workspace_id=ws_id,
                      cycle_id=None, payload=dict(payload or {}), attempt=1,
                      cancelled=lambda: False)


# ---------------------------------------------------------------------------
# 1. classification: multi-label + provenance + question + spam
# ---------------------------------------------------------------------------


class TestClassification:
    def test_multi_label_persists_provenance_and_question(
            self, db_session, workspace_with_user):
        from app.engine.community.classify import classify_interaction
        from app.models.community import SocialInteraction

        ws = workspace_with_user["workspace"]
        account = _mk_account(db_session, ws)
        row = _mk_interaction(
            db_session, ws, account_id=account.id,
            text="How much does the Pro plan cost? I love this and want to buy it.")

        outcome = classify_interaction(db_session, ws, row.id)
        assert outcome["found"] is True

        # multi-label, typed vocabulary, stable priority ordering
        assert set(("QUESTION", "LEAD", "POSITIVE")) <= set(outcome["labels"])
        assert len(outcome["labels"]) >= 2
        assert outcome["priority"] == "high"
        assert outcome["sentiment"] == "positive"
        assert outcome["product_question"] is True
        assert outcome["purchase_intent"] is True

        # question detection landed on the row
        stored = db_session.get(SocialInteraction, row.id)
        assert stored.is_question is True
        assert stored.intent in ("LEAD", "QUESTION")
        assert stored.classifications_json

        # every stored entry carries provider + model + confidence + evidence
        for entry in stored.classifications_json:
            assert {"label", "provider", "model", "confidence",
                    "evidence", "source"} <= set(entry)
            assert entry["label"] in outcome["labels"]
            assert str(entry["provider"]).strip()
            assert entry["source"] in ("rule", "ai")
            assert 0.0 <= float(entry["confidence"]) <= 1.0
            assert str(entry["evidence"]).strip()

    def test_spam_detection_is_deterministic_and_sets_spam_status(
            self, db_session, workspace_with_user):
        from app.engine.community.classify import classify_interaction, detect_spam
        from app.models.community import SocialInteraction

        ws = workspace_with_user["workspace"]
        account = _mk_account(db_session, ws)
        text = ("click here for free money "
                "https://a.example/x https://b.example/y https://c.example/z")

        detected = detect_spam(text)
        assert detected["is_spam"] is True
        assert detected["provider"] == "deterministic"
        assert detected["reasons"]

        row = _mk_interaction(db_session, ws, account_id=account.id, text=text,
                              status="unread")
        outcome = classify_interaction(db_session, ws, row.id)
        assert outcome["is_spam"] is True
        assert "SPAM" in outcome["labels"]
        assert outcome["spam"]["reasons"]

        stored = db_session.get(SocialInteraction, row.id)
        assert stored.status == "spam"
        spam_entries = [e for e in stored.classifications_json
                        if e.get("label") == "SPAM"]
        assert spam_entries and spam_entries[0]["evidence"]

    def test_sensitive_traits_are_never_inferred_or_stored(
            self, db_session, workspace_with_user):
        from app.engine.community.classify import (
            SENSITIVE_TRAIT_RE,
            classify_interaction,
            classify_text,
        )
        from app.models.community import SocialInteraction

        ws = workspace_with_user["workspace"]
        account = _mk_account(db_session, ws)
        text = "As a muslim and a conservative I really love this video!"

        traits = {m.group(0).lower() for m in SENSITIVE_TRAIT_RE.finditer(text)}
        assert {"muslim", "conservative"} <= traits  # fixture really is sensitive

        row = _mk_interaction(db_session, ws, account_id=account.id, text=text)
        outcome = classify_interaction(db_session, ws, row.id)
        assert outcome["found"] is True
        assert "POSITIVE" in outcome["labels"]  # the innocuous label still lands

        # (a) nothing persisted on the row carries a sensitive trait
        stored = db_session.get(SocialInteraction, row.id)
        stored_blob = json.dumps(stored.classifications_json).lower()
        for trait in traits:
            assert trait not in stored_blob
        assert all(e["label"] not in traits for e in stored.classifications_json)

        # (b) the pure output exposes no trait keys and no raw trait text
        out = classify_text(text)
        assert not ({"traits", "religion", "ethnicity", "sexuality", "politics",
                     "demographics", "personal_traits"} & set(out))
        out_blob = json.dumps(out).lower()
        for trait in traits:
            assert trait not in out_blob


# ---------------------------------------------------------------------------
# 2. lead detection → CommunityOpportunity
# ---------------------------------------------------------------------------


def test_lead_detection_creates_single_source_low_confidence_opportunity(
        db_session, workspace_with_user):
    from app.engine.community.opportunity import detect_for_interaction
    from app.models.community import CommunityOpportunity

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    row = _mk_interaction(db_session, ws, account_id=account.id, labels=["LEAD"],
                          text="I want to buy the pro plan, how much is it?")

    opp = detect_for_interaction(db_session, ws, row, labels=["LEAD"])
    assert opp is not None
    assert opp.workspace_id == ws
    assert opp.opportunity_type == "purchase_intent"
    assert opp.evidence_count == 1
    assert list(opp.source_interaction_ids) == [row.id]
    assert opp.confidence == "low"
    assert opp.state == "open"

    # idempotent: the same interaction never double-counts as evidence
    again = detect_for_interaction(db_session, ws, row, labels=["LEAD"])
    assert again.id == opp.id
    db_session.flush()
    stored = db_session.get(CommunityOpportunity, opp.id)
    assert stored.evidence_count == 1
    assert list(stored.source_interaction_ids) == [row.id]

    # non-opportunity interactions never create rows
    plain = _mk_interaction(db_session, ws, account_id=account.id,
                            text="great video as always")
    assert detect_for_interaction(db_session, ws, plain, labels=["POSITIVE"]) is None


# ---------------------------------------------------------------------------
# 3. brand-aware drafting (draft.py + Work 08 effective policy)
# ---------------------------------------------------------------------------


def test_forbidden_phrase_triggers_redraft_and_records_the_hit(
        db_session, workspace_with_user, monkeypatch):
    from app.engine.community import draft as draft_mod

    ws = workspace_with_user["workspace"]
    _set_brand_defaults(db_session, ws,
                        forbidden_phrases=["guaranteed income"],
                        tone="professional")
    account = _mk_account(db_session, ws)
    row = _mk_interaction(db_session, ws, account_id=account.id,
                          text="Does this really pay out?")

    systems = _patch_replies(monkeypatch, [
        "GUARANTEED INCOME for everyone, no risk!",
        "Great question — pricing and plan details live on our site.",
    ])
    result = draft_mod.generate_reply(db_session, ws, row, labels=["QUESTION"])

    assert result["blocked"] is False
    assert result["reason"] == ""
    assert "GUARANTEED INCOME" not in result["text"]
    assert result["brand_check"]["status"] == "pass"
    assert result["brand_check"]["forbidden_hits"] == []
    assert result["brand_check"]["attempts"] == 2

    # the hit itself is recorded on attempt 1, and the re-draft was told
    first, second = result["attempts"]
    assert first["forbidden_hits"] == ["guaranteed income"]
    assert second["forbidden_hits"] == []
    assert len(systems) == 2
    assert "guaranteed income" in systems[1].lower()  # called out to the model

    # required disclosure / tone come from the same effective policy
    assert result["tone"] == "professional"
    assert result["effective_config_id"]


def test_repeated_forbidden_phrases_block_the_draft(
        db_session, workspace_with_user, monkeypatch):
    from app.engine.community import draft as draft_mod

    ws = workspace_with_user["workspace"]
    _set_brand_defaults(db_session, ws, forbidden_phrases=["guaranteed income"])
    account = _mk_account(db_session, ws)
    row = _mk_interaction(db_session, ws, account_id=account.id,
                          text="Is this a real income opportunity?")

    systems = _patch_replies(monkeypatch, [
        "This is guaranteed income for everyone.",
        "Still 100% guaranteed income, no risk at all.",
    ])
    result = draft_mod.generate_reply(db_session, ws, row, labels=["QUESTION"])

    assert result["blocked"] is True
    assert result["reason"] == "forbidden_phrase"
    assert result["text"] == ""
    assert result["brand_check"]["status"] == "blocked"
    assert len(systems) == 2  # hard reject → re-draft once → block


def test_plan_reply_blocked_action_records_brand_check(
        db_session, workspace_with_user):
    from app.engine.community.policy import plan_reply
    from app.models.community import CommunityAction

    ws = workspace_with_user["workspace"]
    _set_brand_defaults(db_session, ws, forbidden_phrases=["guaranteed income"])
    account = _mk_account(db_session, ws)
    row = _mk_interaction(db_session, ws, account_id=account.id)

    action = plan_reply(db_session, ws, row.id,
                        text="This is guaranteed income for everyone.",
                        labels=["POSITIVE"], auto=True)

    assert action.state == "blocked"
    assert action.error == "forbidden_phrase"
    assert action.draft_text == ""
    check = dict(action.brand_check_json or {})
    assert check["status"] == "blocked"
    assert check["attempts"] >= 1
    assert check["effective_config_id"]
    assert action.action_type == "REPLY"
    assert db_session.get(CommunityAction, action.id).state == "blocked"


def test_blocked_action_brand_check_json_records_the_forbidden_phrase(
        db_session, workspace_with_user):
    from app.engine.community.policy import plan_reply

    ws = workspace_with_user["workspace"]
    _set_brand_defaults(db_session, ws, forbidden_phrases=["guaranteed income"])
    account = _mk_account(db_session, ws)
    row = _mk_interaction(db_session, ws, account_id=account.id)

    action = plan_reply(db_session, ws, row.id,
                        text="This is guaranteed income for everyone.",
                        labels=["POSITIVE"], auto=True)

    assert action.state == "blocked"
    check = dict(action.brand_check_json or {})
    assert check["status"] == "blocked"
    hits = [str(h).lower() for h in check.get("forbidden_hits") or []]
    assert "guaranteed income" in hits


def test_required_disclosure_and_tone_applied_from_effective_policy(
        db_session, workspace_with_user, monkeypatch):
    from app.engine.community import draft as draft_mod

    ws = workspace_with_user["workspace"]
    _set_brand_defaults(db_session, ws,
                        required_disclaimers=["Sponsored content"],
                        tone="professional")
    account = _mk_account(db_session, ws)
    row = _mk_interaction(db_session, ws, account_id=account.id,
                          text="Which plan should I pick?")

    _patch_replies(monkeypatch, ["Thanks for asking — the plans page has the answer."])
    result = draft_mod.generate_reply(db_session, ws, row, labels=["QUESTION"])

    assert result["blocked"] is False
    assert result["text"].strip().endswith("Sponsored content")
    check = result["brand_check"]
    assert check["required_disclaimers"] == ["Sponsored content"]
    assert check["missing_disclaimers"] == []
    assert check["tone"] == "professional"
    assert check["status"] == "pass"


# ---------------------------------------------------------------------------
# 4. thread context (budgeted)
# ---------------------------------------------------------------------------


def test_thread_context_includes_parent_chain_and_publication_metadata(
        db_session, workspace_with_user):
    from app.engine.community.draft import build_context

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    conv = f"conv-{uuid.uuid4().hex[:8]}"
    root = _mk_interaction(db_session, ws, account_id=account.id,
                           text="root comment about the pricing tiers",
                           conversation_id=conv)
    post = _mk_post(db_session, ws, title="Ten money tips")
    child = _mk_interaction(db_session, ws, account_id=account.id,
                            text="so how much does it really cost?",
                            conversation_id=conv, parent_interaction_id=root.id,
                            published_post_id=post.id)

    ctx = build_context(db_session, ws, child, max_tokens=800)
    block = ctx["prompt_block"]

    assert "THREAD PARENT 1" in block
    assert "root comment about the pricing tiers" in block
    assert "PUBLISHED POST" in block
    assert "Ten money tips" in block
    assert "BRAND CONSTRAINTS" in block


def test_thread_context_is_bounded_and_never_includes_full_history(
        db_session, workspace_with_user):
    from app.engine.community.draft import build_context
    from app.engine.intelligence.context_budget import estimate_tokens

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    conv = f"conv-{uuid.uuid4().hex[:8]}"
    for i in range(40):
        _mk_interaction(db_session, ws, account_id=account.id,
                        conversation_id=conv, remote_id=f"hist-{i:03d}-"
                        f"{uuid.uuid4().hex[:6]}",
                        text=f"history-{i:03d} " + "filler words " * 40)
    child = _mk_interaction(db_session, ws, account_id=account.id,
                            conversation_id=conv, text="What should I do next?")

    ctx = build_context(db_session, ws, child, max_tokens=400)

    # bounded by the ContextBudgetManager, never the raw conversation
    assert ctx["max_tokens"] == 400
    assert ctx["metrics"]["max_tokens"] == 400
    assert isinstance(ctx["kept"], list) and isinstance(ctx["references"], list)
    assert estimate_tokens(ctx["prompt_block"]) <= 400
    assert ctx["prompt_block"].count("history-") <= 8  # tail only, not 40 messages
    assert "history-000" not in ctx["prompt_block"]
    assert ctx["policy"] is not None
    assert ctx["policy"].workspace_id in ("", ws)

    # …and it is a real bounded context, not an empty one: the pinned comment,
    # at least one actual thread message and a measured token count must be
    # present (budget() must keep content, not just cap it).
    assert "CURRENT COMMENT" in ctx["prompt_block"]
    assert "THREAD MESSAGE:" in ctx["prompt_block"]
    assert "history-" in ctx["prompt_block"]
    assert ctx["metrics"]["raw_tokens"] > 0
    assert ctx["kept"], "budget() dropped every context item"


# ---------------------------------------------------------------------------
# 5. moderation
# ---------------------------------------------------------------------------


def test_deterministic_spam_verdict_records_reason_and_evidence(
        db_session, workspace_with_user):
    from app.engine.community.classify import classify_interaction
    from app.engine.community.moderation import moderate_interaction
    from app.models.community import MODERATION_VERDICTS, SocialInteraction

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    row = _mk_interaction(db_session, ws, account_id=account.id, status="unread",
                          text=("click here for free money "
                                "https://a.example/x https://b.example/y "
                                "https://c.example/z"))

    assert classify_interaction(db_session, ws, row.id)["is_spam"] is True
    outcome = moderate_interaction(db_session, ws, row.id)

    assert outcome["found"] is True
    assert outcome["verdict"] in MODERATION_VERDICTS
    assert outcome["verdict"] != "ALLOW"
    assert any(e["rule"] in ("spam_label", "capability_gate") for e in outcome["entries"])
    for entry in outcome["entries"]:
        assert {"verdict", "rule", "reason", "evidence", "provider"} <= set(entry)
        assert str(entry["reason"]).strip()
    assert any(str(e["evidence"]).strip() for e in outcome["entries"])

    stored = db_session.get(SocialInteraction, row.id)
    assert stored.moderation_json
    assert stored.moderation_state in ("hidden", "review")


def test_uncertain_semantic_signal_only_ever_reviews(
        db_session, workspace_with_user):
    from types import SimpleNamespace

    from app.engine.community.moderation import (
        ALLOW,
        BLOCK,
        HIDE,
        REVIEW,
        moderate_interaction,
    )
    from app.models.community import SocialInteraction

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    row = _mk_interaction(db_session, ws, account_id=account.id, labels=[],
                          text="hmm not sure what I think about this one")

    class _Engine:
        def classify(self, payload):  # noqa: ARG002 - advisory seam
            return ({"label": "REMOVE", "confidence": 0.9,
                     "reason": "semantic judgment is uncertain"},
                    SimpleNamespace(actual_provider="llm-test", model="test-model"))

    outcome = moderate_interaction(db_session, ws, row.id, engine=_Engine())
    assert outcome["found"] is True
    assert outcome["verdict"] == REVIEW
    assert outcome["verdict"] not in (HIDE, BLOCK, ALLOW)
    assert outcome["rule"] == "semantic_uncertainty"
    assert outcome["semantic"]["provider"] == "llm-test"
    entry = next(e for e in outcome["entries"] if e["rule"] == "semantic_uncertainty")
    assert entry["verdict"] == REVIEW
    assert entry["provider"] == "llm-test"

    stored = db_session.get(SocialInteraction, row.id)
    assert stored.moderation_state == "review"


def test_capability_unavailable_downgrades_to_review(
        db_session, workspace_with_user, monkeypatch):
    from app.engine.community import moderation as mod

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    row = _mk_interaction(db_session, ws, account_id=account.id, labels=["ABUSE"],
                          text="you are idiots, absolute clown")

    monkeypatch.setattr(
        mod, "supports_capability",
        lambda platform, capability="DELETE_COMMENT": (False, "capability unavailable"))

    outcome = mod.moderate_interaction(db_session, ws, row.id)
    assert outcome["verdict"] == mod.REVIEW
    assert outcome["rule"] == "capability_gate"
    assert "capability unavailable" in outcome["reason"]
    assert any(e["rule"] == "abuse_label" for e in outcome["entries"])
    gate = next(e for e in outcome["entries"] if e["rule"] == "capability_gate")
    assert gate["provider"] == "registry"
    assert outcome["moderation_state"] == "review"


# ---------------------------------------------------------------------------
# 6. workspace isolation (draft / classify / approve)
# ---------------------------------------------------------------------------


def test_foreign_workspace_denied_for_draft_classify_and_approve(
        db_session, workspace_with_user, monkeypatch):
    from app.engine.community.classify import classify_interaction
    from app.engine.community.policy import CommunityPolicyError, approve_action, plan_reply

    ws_a = workspace_with_user["workspace"]
    ws_b = _mk_workspace(db_session)
    _patch_provider(monkeypatch)
    account = _mk_account(db_session, ws_a)

    row = _mk_interaction(db_session, ws_a, account_id=account.id)
    action = plan_reply(db_session, ws_a, row.id, text="Thanks for watching!",
                        labels=["POSITIVE"], auto=False, actor="operator")

    # draft: the foreign workspace may not create a draft for ws-A's comment
    with pytest.raises(CommunityPolicyError):
        plan_reply(db_session, ws_b, row.id, text="sneaky draft",
                   labels=["POSITIVE"], auto=True)

    # classify: reported as not found, never read or written
    foreign = classify_interaction(db_session, ws_b, row.id)
    assert foreign["found"] is False
    assert foreign["interaction_id"] == row.id

    # approve: the foreign workspace may not see the action
    with pytest.raises(CommunityPolicyError):
        approve_action(db_session, ws_b, action.id, "user-x")

    # nothing changed for ws-A
    assert classify_interaction(db_session, ws_a, row.id)["found"] is True
    assert approve_action(db_session, ws_a, action.id, "user-a").state == "approved"


# ---------------------------------------------------------------------------
# 7. mock vs live receipt verification (verifier.community_reply)
# ---------------------------------------------------------------------------


def test_mock_receipt_never_verifies(db_session, workspace_with_user):
    from app.engine.intelligence.verifier import check_reply
    from app.models.community import CommunityAction

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    row = _mk_interaction(db_session, ws, account_id=account.id)

    # (a) receipt explicitly labelled mock
    mock_action = CommunityAction(
        workspace_id=ws, interaction_id=row.id, account_id=account.id,
        platform="youtube", action_type="REPLY", state="sent",
        final_text="Thanks!", remote_reply_id="mock-1",
        provider_receipt_json={"mock": True, "remote_reply_id": "mock-1"},
        is_mock=True, sent_at=_utcnow())
    db_session.add(mock_action)
    db_session.flush()

    receipt = dict(mock_action.provider_receipt_json)
    execution, status, checks = check_reply(mock_action, receipt, account.id, "youtube")
    assert execution == "COMPLETED"
    assert status != "VERIFIED"
    assert status == "NOT_VERIFIED"
    failed = {c["name"] for c in checks if not c["passed"]}
    assert "live_proof" in failed

    # (b) receipt looks complete but the action itself is mock-labelled
    execution, status, checks = check_reply(
        mock_action, {"remote_reply_id": "mock-1"}, account.id, "youtube")
    assert status == "NOT_VERIFIED"
    assert {c["name"] for c in checks if not c["passed"]} >= {"live_proof"}


def test_four_proof_live_receipt_verifies(db_session, workspace_with_user):
    from app.engine.intelligence.verifier import CompletionContract, check_reply, verify
    from app.models.community import CommunityAction

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    row = _mk_interaction(db_session, ws, account_id=account.id)
    receipt = {"remote_reply_id": "reply-1", "account_id": account.id,
               "platform": "youtube"}

    action = CommunityAction(
        workspace_id=ws, interaction_id=row.id, account_id=account.id,
        platform="youtube", action_type="REPLY", state="sent",
        final_text="Thanks for watching!", remote_reply_id="reply-1",
        provider_receipt_json=dict(receipt), is_mock=False, sent_at=_utcnow())
    db_session.add(action)
    db_session.flush()

    execution, status, checks = check_reply(action, dict(receipt), account.id, "youtube")
    assert execution == "COMPLETED"
    assert status == "VERIFIED"
    by_name = {c["name"]: c["passed"] for c in checks}
    for name in ("provider_receipt", "remote_reply_id", "action_row_persisted",
                 "account_platform_match"):
        assert by_name.get(name) is True, (name, checks)

    # the same contract through verify() (ledger append)
    record = verify(db_session, ws,
                    CompletionContract(kind="community_reply", subject_id=action.id))
    assert record.kind == "community_reply"
    assert record.verification_status == "VERIFIED"
    assert record.execution_status == "COMPLETED"


def _utcnow():
    from app.models.base import utcnow

    return utcnow()


# ---------------------------------------------------------------------------
# 8. insights
# ---------------------------------------------------------------------------


def _insight_texts() -> tuple[str, str, str]:
    return ("How do I set up the API integration?",
            "how do i set up the api integration",
            "SET UP THE API INTEGRATION please")


def test_two_same_topic_interactions_stay_low_confidence(
        db_session, workspace_with_user):
    from app.engine.community.insight import record_insight
    from app.models.community import CommunityInsight

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    first_text, second_text, _ = _insight_texts()

    one = _mk_interaction(db_session, ws, account_id=account.id, text=first_text)
    two = _mk_interaction(db_session, ws, account_id=account.id, text=second_text)

    insight = record_insight(db_session, ws, one)
    assert insight is not None
    assert insight.evidence_count == 1
    assert insight.confidence == "low"

    record_insight(db_session, ws, two)
    db_session.flush()
    stored = db_session.get(CommunityInsight, insight.id)
    assert stored.evidence_count == 2
    assert list(stored.source_interaction_ids) == [one.id, two.id]
    assert stored.confidence == "low"  # below the evidence threshold (3)


def test_third_interaction_grows_evidence_with_source_ids(
        db_session, workspace_with_user):
    from app.engine.community.insight import record_insight
    from app.models.community import CommunityInsight

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    texts = _insight_texts()

    rows = [_mk_interaction(db_session, ws, account_id=account.id, text=t)
            for t in texts]
    insight = None
    for row in rows:
        insight = record_insight(db_session, ws, row)
    assert insight is not None
    db_session.flush()

    stored = db_session.get(CommunityInsight, insight.id)
    assert stored.evidence_count == 3
    assert list(stored.source_interaction_ids) == [r.id for r in rows]
    assert stored.confidence == "medium"  # threshold reached, still labelled
    assert stored.state == "new"


def test_content_feedback_suggestions_carry_interaction_ids(
        db_session, workspace_with_user):
    from app.engine.community.insight import (
        community_content_feedback,
        normalize_topic,
        record_insight,
    )

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    first_text, second_text, _ = _insight_texts()

    one = _mk_interaction(db_session, ws, account_id=account.id, text=first_text)
    two = _mk_interaction(db_session, ws, account_id=account.id, text=second_text)
    record_insight(db_session, ws, one)
    record_insight(db_session, ws, two)

    topic_key, _topic = normalize_topic(first_text)
    suggestions = community_content_feedback(db_session, ws)
    match = [s for s in suggestions if s["topic_key"] == topic_key]
    assert len(match) == 1
    suggestion = match[0]

    assert set(suggestion["interaction_ids"]) == {one.id, two.id}
    assert suggestion["evidence_count"] == 2
    assert suggestion["threshold"] == 3
    assert suggestion["low_confidence"] is True
    assert suggestion["meets_threshold"] is False
    assert suggestion["suggested_action"] == "watch"
    assert suggestion["suggested_followup"]
    assert suggestion["sample_text"]
    assert suggestion["confidence"] == "low"


# ---------------------------------------------------------------------------
# 9. analytics
# ---------------------------------------------------------------------------


def test_compute_community_metrics_returns_documented_keys(
        db_session, workspace_with_user):
    from app.engine.community.analytics import compute_community_metrics
    from app.engine.community.classify import classify_interaction

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    row = _mk_interaction(db_session, ws, account_id=account.id,
                          text="How do I start?")
    assert classify_interaction(db_session, ws, row.id)["is_question"] is True

    metrics = compute_community_metrics(db_session, ws, days=30)
    expected = {"workspace_id", "window_days", "since", "interactions", "comments",
                "questions", "spam", "reply_rate", "replies_sent", "response_time",
                "sentiment", "content_requests", "lead_signals", "moderation",
                "by_content"}
    assert expected <= set(metrics)
    assert metrics["workspace_id"] == ws
    assert metrics["window_days"] == 30
    assert metrics["interactions"] >= 1
    assert metrics["comments"] == metrics["interactions"]
    assert metrics["questions"] >= 1
    assert set(metrics["sentiment"]) == {"positive", "negative", "neutral", "mixed"}
    assert set(metrics["moderation"]) == {"allowed", "review", "hidden",
                                          "blocked", "pending"}
    assert {"avg_seconds", "median_seconds", "samples"} <= set(metrics["response_time"])
    assert isinstance(metrics["by_content"], list)


def test_content_level_grouping_via_published_post_link(
        db_session, workspace_with_user):
    from app.engine.community.analytics import compute_community_metrics
    from app.engine.community.classify import classify_interaction

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    post = _mk_post(db_session, ws, title="Budget basics")
    row = _mk_interaction(db_session, ws, account_id=account.id,
                          text="Can you make a video on budgeting? how do I start?",
                          published_post_id=post.id)
    outcome = classify_interaction(db_session, ws, row.id)
    assert outcome["is_question"] is True
    assert "CONTENT_REQUEST" in outcome["labels"]

    metrics = compute_community_metrics(db_session, ws, days=30)
    buckets = [b for b in metrics["by_content"] if b["published_post_id"] == post.id]
    assert len(buckets) == 1
    bucket = buckets[0]
    assert bucket["platform"] == "youtube"
    assert bucket["interactions"] >= 1
    assert bucket["questions"] >= 1
    assert bucket["content_requests"] >= 1
    assert bucket["samples"]
    assert row.id  # linkage row exists for the bucket


# ---------------------------------------------------------------------------
# 10. CommunityManagerAgent
# ---------------------------------------------------------------------------


def test_community_manager_is_registered_in_the_agents_registry():
    from app.engine.agents.community import CommunityManagerAgent
    from app.engine.agents.registry import AGENT_META, AGENTS

    assert AGENTS["community_manager"] is CommunityManagerAgent
    assert AGENT_META["community_manager"].key == "community_manager"
    assert AGENT_META["community_manager"].title == "Community Manager"


def test_triage_run_persists_agent_runs_row(db_session, workspace_with_user):
    from app.db import session_scope
    from app.engine.agents.registry import AGENTS
    from app.models import AgentRun

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    row = _mk_interaction(db_session, ws, account_id=account.id, status="unread",
                          text="Love this video, thanks for sharing!")
    # handlers/agents run in their own sessions: commit the fixture first
    db_session.commit()

    agent = AGENTS["community_manager"]()
    result = agent.triage(_job_ctx(ws), interaction_ids=[row.id])

    assert result["classified"] == 1
    assert result["moderated"] == 1
    assert result["batch"] == [row.id]
    assert "triaged 1 interaction" in result["summary"]

    with session_scope() as s:
        runs = s.query(AgentRun).filter(
            AgentRun.workspace_id == ws,
            AgentRun.agent_key == "community_manager").all()
        assert runs, "BaseAgent.execute must record an agent_runs row"
        run = runs[0]
        assert run.status == "COMPLETED"
        assert run.task_type == "community_triage"
        assert run.error == ""
        steps = (run.steps_json or {}).get("steps") or []
        assert steps and {st["step"] for st in steps} >= {
            "resolve_batch", "classify", "moderate", "draft_gate"}


# ---------------------------------------------------------------------------
# 11. jobs registration
# ---------------------------------------------------------------------------


def test_register_community_jobs_is_idempotent_and_registers_handlers():
    from app.engine.community import jobs as community_jobs
    from app.services import jobs as jobs_service

    community_jobs.register_community_jobs()
    community_jobs.register_community_jobs()  # repeat/reload must not raise

    for job_type in ("INTERACTION_CLASSIFY", "COMMUNITY_DRAFT", "COMMUNITY_ACTION"):
        assert job_type in jobs_service._handlers, job_type
    assert jobs_service._handlers["INTERACTION_CLASSIFY"] is \
        community_jobs.handle_interaction_classify
    assert jobs_service._handlers["COMMUNITY_DRAFT"] is community_jobs.handle_community_draft
    assert jobs_service._handlers["COMMUNITY_ACTION"] is community_jobs.handle_community_action


def test_community_job_handlers_execute_in_process(
        db_session, workspace_with_user, monkeypatch):
    """All three community handlers run their REAL logic end-to-end — honest
    counts, persisted state and the policy-gated send — not just their
    registration. Only the network-bound provider is a recording double."""
    from app.engine.community import jobs as community_jobs
    from app.models.community import CommunityAction, SocialInteraction
    from app.services import jobs as jobs_service

    ws = workspace_with_user["workspace"]
    account = _mk_account(db_session, ws)
    community_jobs.register_community_jobs()

    # --- INTERACTION_CLASSIFY: labels + moderation outcome, honestly counted
    row = _mk_interaction(db_session, ws, account_id=account.id,
                          status="unread", text="Nice video, thanks a lot!")
    q = _mk_interaction(db_session, ws, account_id=account.id,
                        status="unread", text="How do I get started with this?")
    # handlers/agents run in their own sessions: commit the fixture first
    db_session.commit()

    out = jobs_service._handlers["INTERACTION_CLASSIFY"](jobs_service.JobContext(
        job_id="job-iclass-exec", type="INTERACTION_CLASSIFY",
        workspace_id=ws, cycle_id=None,
        payload={"interaction_ids": [row.id]}, attempt=1,
        cancelled=lambda: False))
    assert out["ok"] is True
    assert out["classified"] == 1 and out["moderated"] == 1
    assert out["skipped"] == 0
    assert out["results"][0]["found"] is True
    assert out["results"][0]["labels"], "classify job returned no labels"
    assert out["results"][0]["moderation"]

    # --- COMMUNITY_DRAFT: plan_reply under DRAFT_ONLY → draft, never sent
    out = jobs_service._handlers["COMMUNITY_DRAFT"](jobs_service.JobContext(
        job_id="job-cdraft-exec", type="COMMUNITY_DRAFT",
        workspace_id=ws, cycle_id=None,
        payload={"interaction_ids": [q.id]}, attempt=1,
        cancelled=lambda: False))
    assert out["ok"] is True
    assert out["drafted"] == 1 and out["sent"] == 0
    assert out["actions"][0]["state"] == "draft"

    # handlers used their own session_scope — re-read through a FRESH one:
    # db_session committed with expire_on_commit=False, so its identity map
    # still holds the pre-classify row and get() would return that stale copy
    from app.db import session_scope

    with session_scope() as fresh:
        saved = fresh.get(SocialInteraction, row.id)
        assert saved.classifications_json, "classify job did not persist labels"
        draft_action = fresh.query(CommunityAction).filter(
            CommunityAction.workspace_id == ws,
            CommunityAction.interaction_id == q.id).one()
        assert draft_action.state == "draft"
        assert draft_action.draft_text.strip()
        action_id = draft_action.id

    # --- COMMUNITY_ACTION: approve + send through the real policy gate
    provider = _patch_provider(monkeypatch)
    out = jobs_service._handlers["COMMUNITY_ACTION"](jobs_service.JobContext(
        job_id="job-caction-app", type="COMMUNITY_ACTION",
        workspace_id=ws, cycle_id=None,
        payload={"action_id": action_id, "op": "approve", "user_id": "tester"},
        attempt=1, cancelled=lambda: False))
    assert out["ok"] is True, out
    assert out["result"]["state"] == "approved"

    out = jobs_service._handlers["COMMUNITY_ACTION"](jobs_service.JobContext(
        job_id="job-caction-send", type="COMMUNITY_ACTION",
        workspace_id=ws, cycle_id=None,
        payload={"action_id": action_id, "op": "send", "actor": "operator"},
        attempt=1, cancelled=lambda: False))
    assert out["ok"] is True, out
    assert len(provider.calls) == 1, "send job never reached the provider"

    with session_scope() as fresh:
        sent = fresh.get(CommunityAction, action_id)
        assert sent.state == "sent"
        assert sent.remote_reply_id

    # a COMMUNITY_ACTION job without workspace_id fails honestly (no cross-ws)
    with pytest.raises(Exception):
        jobs_service._handlers["COMMUNITY_ACTION"](jobs_service.JobContext(
            job_id="job-caction-badws", type="COMMUNITY_ACTION",
            workspace_id=None, cycle_id=None,
            payload={"action_id": action_id, "op": "approve"},
            attempt=1, cancelled=lambda: False))


# ---------------------------------------------------------------------------
# 12. account handoff (SocialAccount ORM row to the provider)
# ---------------------------------------------------------------------------


def test_auto_send_passes_social_account_orm_row_to_provider(
        db_session, workspace_with_user, monkeypatch):
    from app.engine.community.policy import plan_reply
    from app.models import SocialAccount

    ws = workspace_with_user["workspace"]
    provider = _patch_provider(monkeypatch)
    account = _mk_account(db_session, ws)
    _set_community(db_session, ws, mode="LOW_RISK_AUTO",
                   auto_reply_classes=["POSITIVE"])

    row = _mk_interaction(db_session, ws, account_id=account.id,
                          text="Love this, great work!")
    action = plan_reply(db_session, ws, row.id,
                        text="Thanks so much, glad you loved it!",
                        labels=["POSITIVE"], auto=True)

    assert action.state == "sent"
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert isinstance(call["account"], SocialAccount)
    assert call["account"].id == account.id
    assert call["account"].workspace_id == ws
    assert call["remote_id"] == row.remote_id
    assert call["text"] == action.final_text
    # the ORM row itself, never a serialized secret bag
    assert "enc-at-rest" not in str(call)
    assert action.provider_receipt_json["account_id"] == account.id
    assert action.verification_json["verification_status"] == "VERIFIED"

