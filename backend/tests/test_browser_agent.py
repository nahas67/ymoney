"""Lane C browser-agent tests: guards, stale abort, caps, evidence shape."""

from __future__ import annotations

import pytest

from app.engine.intelligence.browser import (
    BlockedActionError,
    BrowserAction,
    BrowserIntelligenceAgent,
    BrowserPolicy,
    CancellationToken,
    CancelledError,
    DomainDeniedError,
    RecordingBackend,
    StaleStateError,
    evidence_to_research_brief,
)

PAGES = {
    "https://example.com/a": {
        "title": "Page A",
        "text": "alpha content here",
        "elements": [{"id": "more", "kind": "link", "label": "more"}],
        "data": {"topic": "alpha"},
    },
    "https://example.com/b": {"title": "Page B", "text": "beta content here"},
}


def _agent(**policy_kw) -> BrowserIntelligenceAgent:
    policy = BrowserPolicy(allowed_domains=["example.com"], **policy_kw)
    return BrowserIntelligenceAgent(policy=policy, backend=RecordingBackend(PAGES))


def test_stale_state_aborts_with_reason():
    agent = _agent()
    obs = agent.run_action(BrowserAction(kind="navigate", target="https://example.com/a"), None)
    assert obs.state_hash
    with pytest.raises(StaleStateError, match="stale state"):
        agent.run_action(
            BrowserAction(kind="extract", target="main", expected_state_hash="deadbeef"),
            obs,
        )
    # matching hash proceeds
    obs2 = agent.run_action(
        BrowserAction(kind="extract", target="main", expected_state_hash=obs.state_hash),
        obs,
    )
    assert obs2.url == "https://example.com/a"


def test_domain_deny_wins_over_allow():
    policy = BrowserPolicy(allowed_domains=["example.com"],
                           denied_domains=["sub.example.com"])
    agent = BrowserIntelligenceAgent(policy=policy, backend=RecordingBackend(PAGES))
    with pytest.raises(DomainDeniedError):
        agent.run_action(BrowserAction(kind="navigate", target="https://sub.example.com/x"), None)
    with pytest.raises(DomainDeniedError):  # default-deny
        agent.run_action(BrowserAction(kind="navigate", target="https://other.com/x"), None)
    obs = agent.run_action(BrowserAction(kind="navigate", target="https://example.com/a"), None)
    assert obs.title == "Page A"


def test_login_form_purchase_blocked_by_default():
    agent = _agent()
    obs = agent.run_action(BrowserAction(kind="navigate", target="https://example.com/a"), None)
    for kind in ("login", "form_submit", "purchase"):
        with pytest.raises(BlockedActionError):
            agent.run_action(BrowserAction(kind=kind, target="x"), obs)


def test_step_cap_aborts_run():
    agent = _agent(max_steps=1)  # one url needs navigate+extract = 2 steps
    result = agent.run("goal", ["https://example.com/a"])
    assert result["status"] == "ABORTED"
    assert "max_steps" in result["stop_reason"]


def test_cost_cap_aborts_run():
    agent = _agent(cost_limit_usd=0.0001)
    result = agent.run("goal", ["https://example.com/a"])
    assert result["status"] == "ABORTED"
    assert "cost" in result["stop_reason"]


def test_timeout_aborts_run():
    agent = _agent(timeout_seconds=0.0)
    result = agent.run("goal", ["https://example.com/a"])
    assert result["status"] == "ABORTED"
    assert "timeout" in result["stop_reason"]


def test_cancellation_aborts_run():
    agent = _agent()
    token = CancellationToken()
    token.cancel()
    with pytest.raises(CancelledError):
        token.check()
    result = agent.run("goal", ["https://example.com/a"], cancel=token)
    assert result["status"] == "CANCELLED"


def test_secret_sanitizer_redacts_extracts():
    pages = {"https://example.com/s": {"title": "S", "text": "key sk-abcdef1234567890 done"}}
    agent = BrowserIntelligenceAgent(
        policy=BrowserPolicy(allowed_domains=["example.com"]),
        backend=RecordingBackend(pages),
    )
    result = agent.run("goal", ["https://example.com/s"])
    assert result["status"] == "COMPLETED"
    blob = result["evidence"]["extracts"][0]["text"]
    assert "sk-abcdef1234567890" not in blob
    assert "[REDACTED]" in blob


def test_research_evidence_shape_matches_brief():
    agent = _agent()
    result = agent.run("money tips", ["https://example.com/a", "https://example.com/b"])
    assert result["status"] == "COMPLETED"
    ev = result["evidence"]
    assert {"url", "title", "retrieved_at", "extracts", "action_trace"} <= set(ev)
    brief = evidence_to_research_brief(ev, "money tips")
    for key in ("summary", "key_facts", "angles", "visual_keywords", "cautions",
                "claims", "factual_confidence", "fact_status", "sources"):
        assert key in brief, f"missing brief key: {key}"
    assert len(brief["sources"]) == 2
    assert all(c["status"] in ("VERIFIED", "LIKELY", "UNCERTAIN", "CONFLICTING")
               for c in brief["claims"])
    empty = evidence_to_research_brief(
        {"url": "", "title": "", "retrieved_at": "", "extracts": [], "action_trace": []}, "t")
    assert empty["fact_status"] == "INSUFFICIENT"
