"""Metric honesty: UNAVAILABLE IS NOT ZERO (Work 16.5.7 §8/§11).

Cross-cutting evidence for the invariant declared in ``app.core.honesty``:
every metric the UI shows is MEASURED, DERIVED or UNAVAILABLE, and never an
invented ``0``.

THE FOUR CLAIMS EACH BLOCK PROVES
--------------------------------
1. An EMPTY workspace yields ``null`` for every audited field, not ``0``.
2. A workspace with genuinely zero-valued rows yields ``0`` -- this is the
   guard against over-correcting into "everything is null", which is the same
   dishonesty with the sign flipped: it would make "measured zero" and "never
   measured" indistinguishable in the other direction.
3. An ``UNKNOWN_EXPOSURE`` cost row (money possibly spent, amount unpriceable)
   makes a money total unknown, never ``$0.00``.
4. A real spend of exactly ``0.00`` recorded explicitly still yields ``0.0``.

Plus the frontend contract: each changed endpoint still publishes a schema and
the changed field is NULLABLE there, so a TS ``number`` cannot quietly become a
lie at the type level.

Evidence is real: every claim below is an HTTP response from the real app with
a real workspace and real ORM rows, seeded through the repo's own session.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from app.models import (
    CostEntry,
    PostMetric,
    PublishedPost,
)
from app.models.base import utcnow
from app.services import cost as cost_service

REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """A real registered workspace behind a real TestClient.

    Follows the pattern every other API test in this repo uses
    (``test_archive.py`` et al): ``create_app()`` then ``POST /auth/register``,
    so the token, the membership and the workspace row are all genuine.
    """
    from fastapi.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    r = client.post(
        "/api/v1/auth/register",
        json={"email": f"hon{uuid.uuid4().hex[:8]}@test.local", "password": "supersecret123"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    yield {
        "client": client,
        "ws": data["workspace"]["id"],
        "headers": {"Authorization": f"Bearer {data['access_token']}"},
    }
    client.close()


def _get(api, path):
    r = api["client"].get(path, headers=api["headers"])
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:400]}"
    return r.json()


def _publish(ws_id: str, *, platform="youtube", title="p", session=None):
    from app.db import session_scope

    def _make(s):
        row = PublishedPost(
            workspace_id=ws_id,
            video_id=f"vid-{os.urandom(6).hex()}",
            platform=platform,
            title=title,
            published_at=utcnow(),
        )
        s.add(row)
        s.flush()
        return row.id

    if session is not None:
        return _make(session)
    with session_scope() as s:
        return _make(s)


def _snapshot(post_id: str, **kwargs):
    from app.db import session_scope

    with session_scope() as s:
        s.add(PostMetric(post_id=post_id, **kwargs))


def _cost(ws_id: str, amount, *, category="llm", detail=None, provider="p"):
    from app.db import session_scope

    with session_scope() as s:
        s.add(
            CostEntry(
                workspace_id=ws_id,
                category=category,
                amount_usd=amount,
                provider=provider,
                detail_json=detail or {},
            )
        )


# ---------------------------------------------------------------------------
# §1 the vocabulary itself
# ---------------------------------------------------------------------------


def test_vocabulary_is_declared_in_code_not_in_a_docstring():
    assert cost_service.PROVENANCE_CLASSES == ("MEASURED", "DERIVED", "UNAVAILABLE")


def test_the_audit_shares_this_vocabulary_rather_than_restating_it():
    """The audit imports the vocabulary from HERE.

    An audit that declares its own copy of the rules is auditing its own beliefs.
    If the two drift, one of them is decoration. Run as a SUBPROCESS because the
    script mutates ``sys.path`` and holds module state at import time; loading it
    in-process would leak both into the rest of the suite.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "audit_analytics_honesty.py")],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "MEASURED" in result.stdout and "UNAVAILABLE" in result.stdout

    audit = json.loads(
        (REPO / "docs" / "ANALYTICS_HONESTY_AUDIT.json").read_text(encoding="utf-8")
    )
    assert sorted(audit["vocabulary"]) == sorted(cost_service.PROVENANCE_CLASSES)


# ---------------------------------------------------------------------------
# §2 the arithmetic primitives
# ---------------------------------------------------------------------------


def test_measured_sum_separates_absent_from_zero():
    assert cost_service.measured_sum([]) is None
    assert cost_service.measured_sum([0, 0]) == 0
    assert cost_service.measured_sum([1, 2]) == 3


def test_derived_ratio_is_none_not_zero_for_an_empty_denominator():
    assert cost_service.derived_ratio(0, 0) is None
    assert cost_service.derived_ratio(3, 0) is None
    assert cost_service.derived_ratio(0, 7) == 0.0  # a REAL measured zero rate
    assert cost_service.derived_ratio(3, 7) == pytest.approx(3 / 7)


def test_derived_mean_ignores_unweighted_pairs():
    # (0.0, 0) is "no weight" -- it must not drag the mean to zero.
    assert cost_service.derived_mean([(0.5, 10), (0.0, 0)]) == pytest.approx(0.5)
    assert cost_service.derived_mean([]) is None
    assert cost_service.derived_mean([(0.25, 2000), (0.5, 1000)]) == pytest.approx(1000 / 3000)


def test_money_total_is_unknown_for_an_empty_or_unpriceable_ledger():
    class Row:
        def __init__(self, amount, detail=None):
            self.amount_usd = amount
            self.detail_json = detail or {}

    assert cost_service.money_total([]) == (None, 0)
    assert cost_service.money_total([Row(0.0)]) == (0.0, 0)          # real $0.00
    assert cost_service.money_total([Row(1.25), Row(0.5)]) == (1.75, 0)
    assert cost_service.money_total(
        [Row(1.25), Row(0.0, {"exposure_unknown": True})]
    ) == (None, 1)
    assert cost_service.money_total(
        [Row(0.0, {"cost_outcome": cost_service.UNKNOWN_EXPOSURE_MARKER})]
    ) == (None, 1)


# ---------------------------------------------------------------------------
# §3 an EMPTY workspace is null everywhere, not 0
# ---------------------------------------------------------------------------


def test_empty_workspace_overview_is_null_not_zero(api):
    body = _get(api, f"/api/v1/workspaces/{api['ws']}/analytics/overview")

    # Every engagement total is UNAVAILABLE: no snapshot was ever summed.
    for key in ("views", "likes", "comments", "shares", "followers_gained"):
        assert body["totals"][key] is None, f"totals.{key} invented a 0"
    # The ledger is empty, so total spend is UNKNOWN-but-zero-observed.
    assert body["cost_total_usd"] is None
    assert body["cost_total_unknown_exposure_rows"] == 0
    # COUNTS stay real numbers: a COUNT over an empty table IS a measured zero.
    assert body["posts_published"] == 0
    assert body["content_items"] == 0
    assert body["best_post"] is None
    assert body["per_platform"] == {}


def test_published_post_without_a_snapshot_reports_null_metrics(api):
    """`/publishing/posts` used to report `views: 0` for an unmeasured post."""
    _publish(api["ws"], title="never measured")
    item = _get(api, f"/api/v1/workspaces/{api['ws']}/publishing/posts")["items"][0]
    assert item["metrics"] == {
        "views": None,
        "likes": None,
        "comments": None,
        "completion_rate": None,
    }


def test_empty_workspace_costs_summary_is_null_not_zero_dollars(api):
    body = _get(api, f"/api/v1/workspaces/{api['ws']}/costs")
    assert body["spent_last_24h_usd"] is None, "an empty ledger is not $0.00 spent"
    assert body["spent_last_24h_unknown_exposure_rows"] == 0
    assert body["last_24h_by_category"] == {}
    # A BUDGET GATE, not a measurement: it stays a real boolean.
    assert isinstance(body["within_budget"], bool)


def test_empty_workspace_cost_intelligence_still_fabricates_a_zero(api):
    """NOT FIXED — OUT OF SCOPE. Asserted as a FAILING behaviour on purpose.

    `safety.py` is outside this lane's permitted file set, so `/costs/intelligence`
    still reports `$0.0000` of total cost for an empty ledger. This test asserts
    the CURRENT (wrong) shape so the gap is visible in CI rather than invisible,
    and so that when a follow-up lane fixes `safety.py` this test fails loudly and
    gets inverted — the same treatment `test_rollup_short_without_metrics_is_zero`
    received in `test_campaign_analytics.py`.
    """
    body = _get(api, f"/api/v1/workspaces/{api['ws']}/costs/intelligence")
    assert body["total_cost_usd"] == 0.0, (
        "safety.py is now honest: an empty ledger reports None, not $0.00. "
        "Invert this test and drop the audit's `NOT FIXED — OUT OF SCOPE` note "
        "for /costs/intelligence."
    )
    assert body["per_cycle_usd"] is None  # denominator is genuinely zero
    assert body["per_publication_usd"] is None
    assert body["totals"]["views"] == 0, (
        "safety.py's `views = 0` seed was removed: unmeasured views now report "
        "None. Invert this test with the line above."
    )


def test_empty_workspace_agent_stats_are_null_not_zero(api):
    body = _get(api, f"/api/v1/workspaces/{api['ws']}/agents")
    assert body["items"], "the agent catalog is not empty"
    sample = body["items"][0]
    assert sample["runs"] == 0, "a COUNT of AgentRun rows is a measured 0"
    assert sample["failure_rate"] is None, "0/0 is not a 0% failure rate"
    assert sample["total_cost_usd"] is None, "no runs is not $0.00 of spend"
    assert sample["avg_duration_ms"] is None


def test_empty_workspace_breakdown_buckets_are_null(api):
    """A published-but-unmeasured post creates a bucket with no average."""
    _publish(api["ws"], title="no snapshot here")
    rows = _get(api, f"/api/v1/workspaces/{api['ws']}/analytics/breakdowns")["by_topic"]
    assert rows, "the unmeasured post must still create its bucket"
    row = rows[0]
    assert row["posts"] == 0, "posts counts MEASURED posts, and none is measured"
    assert row["total_views"] is None
    assert row["avg_views"] is None
    assert row["engagement_pct"] is None
    assert row["engagement_samples"] == 0


def test_empty_rollup_is_all_none():
    from app.engine.campaign import analytics as ca

    assert ca.empty_rollup() == {
        "views": None, "watch_time": None, "likes": None, "comments": None,
        "shares": None, "saves": None, "engagement_rate": None, "completion": None,
    }


# ---------------------------------------------------------------------------
# §4 a REAL measured zero is still zero
# ---------------------------------------------------------------------------


def test_snapshots_recording_zero_views_report_zero_not_unavailable(api):
    post_id = _publish(api["ws"], title="genuinely zero")
    _snapshot(post_id, views=0, likes=0, comments=0, shares=0, saves=0,
              completion_rate=0.0)

    body = _get(api, f"/api/v1/workspaces/{api['ws']}/analytics/overview")
    assert body["totals"]["views"] == 0, "a measured zero must stay 0"
    assert body["totals"]["likes"] == 0
    assert body["per_platform"]["youtube"]["views"] == 0
    assert body["per_platform"]["youtube"]["posts"] == 1


def test_published_post_with_zero_snapshot_reports_zero_metrics(api):
    post_id = _publish(api["ws"], title="zero but measured")
    _snapshot(post_id, views=0, likes=0, comments=0)
    item = _get(api, f"/api/v1/workspaces/{api['ws']}/publishing/posts")["items"][0]
    assert item["metrics"]["views"] == 0
    assert item["metrics"]["likes"] == 0


def test_real_zero_spend_of_explicitly_recorded_zero_yields_zero(api):
    """A CostEntry written at exactly 0.0 is a real measurement of $0.00."""
    _cost(api["ws"], 0.0, category="storage", detail={"cost_outcome": "ACTUAL"})

    body = _get(api, f"/api/v1/workspaces/{api['ws']}/costs")
    assert body["spent_last_24h_usd"] == 0.0
    assert body["last_24h_by_category"] == {"storage": 0.0}
    assert body["spent_last_24h_unknown_exposure_rows"] == 0

    # `/costs/intelligence` is OUT OF SCOPE (safety.py) and still reports $0.00
    # for this; only `/costs` is asserted here. The gap is recorded in the audit
    # and pinned by test_empty_workspace_cost_intelligence_still_fabricates_a_zero.


def test_real_zero_spend_flows_through_the_overview_total(api):
    _cost(api["ws"], 0.0, detail={"cost_outcome": "ACTUAL"})
    assert _get(api, f"/api/v1/workspaces/{api['ws']}/analytics/overview")[
        "cost_total_usd"
    ] == 0.0


def test_agent_run_that_actually_cost_zero_reports_zero_spend(api):
    from app.db import session_scope
    from app.models import AgentRun

    with session_scope() as s:
        s.add(
            AgentRun(
                workspace_id=api["ws"], agent_key="trend_hunter", status="SUCCESS",
                duration_ms=10, cost_usd=0.0,
            )
        )
    item = next(
        i for i in _get(api, f"/api/v1/workspaces/{api['ws']}/agents")["items"]
        if i["key"] == "trend_hunter"
    )
    assert item["runs"] == 1
    assert item["failure_rate"] == 0.0, "0 failures out of 1 real run IS 0%"
    assert item["total_cost_usd"] == 0.0
    assert item["avg_duration_ms"] == 10


# ---------------------------------------------------------------------------
# §5 an UNKNOWN_EXPOSURE row makes money UNKNOWN
# ---------------------------------------------------------------------------


def test_unknown_exposure_row_alone_makes_the_total_unknown(api):
    _cost(api["ws"], 0.0, detail={"exposure_unknown": True,
                                   "cost_outcome": "UNKNOWN_EXPOSURE"})
    body = _get(api, f"/api/v1/workspaces/{api['ws']}/costs")
    assert body["spent_last_24h_usd"] is None, "an unpriceable exposure is not $0.00"
    assert body["spent_last_24h_unknown_exposure_rows"] == 1


def test_unknown_exposure_row_next_to_real_spend_is_still_unknown(api):
    """The dangerous case: priced rows sum to a confident, too-small number."""
    _cost(api["ws"], 1.25, category="llm")
    _cost(api["ws"], 0.5, category="video")
    _cost(api["ws"], 0.0, category="video",
          detail={"exposure_unknown": True, "cost_outcome": "UNKNOWN_EXPOSURE"})

    body = _get(api, f"/api/v1/workspaces/{api['ws']}/costs")
    assert body["spent_last_24h_usd"] is None, (
        "reporting $1.75 here would under-report real money as a smaller, "
        "confident number -- worse than reporting nothing"
    )
    assert body["spent_last_24h_unknown_exposure_rows"] == 1
    # The per-category breakdown remains a real measurement of the PRICED rows,
    # with the unpriceable row excluded rather than added as 0.
    assert body["last_24h_by_category"] == {"llm": 1.25, "video": 0.5}
    # The GATE stays conservative: unknown spend eats headroom, it does not
    # grant it, so within_budget is still a real boolean.
    assert isinstance(body["within_budget"], bool)


def test_unknown_exposure_makes_the_overview_total_unknown(api):
    _cost(api["ws"], 2.0)
    _cost(api["ws"], 0.0, detail={"exposure_unknown": True})
    body = _get(api, f"/api/v1/workspaces/{api['ws']}/analytics/overview")
    assert body["cost_total_usd"] is None
    assert body["cost_total_unknown_exposure_rows"] == 1


def test_book_unknown_exposure_writes_a_row_the_totals_refuse_to_price(api):
    """End to end through the real billing helper, not a hand-built row."""
    from app.db import session_scope
    from app.models import CostEntry as _CostEntry

    cost_service.track_cost(api["ws"], "llm", 1.0, provider="p")
    entry_id = cost_service.book_unknown_exposure(
        api["ws"], category="video", provider="p", detail={"reason": "response lost"}
    )
    with session_scope() as s:
        row = s.get(_CostEntry, entry_id)
        assert row is not None, "the row must exist; an unknown exposure is not dropped"
        assert cost_service.is_unknown_exposure(row)

    body = _get(api, f"/api/v1/workspaces/{api['ws']}/costs")
    assert body["spent_last_24h_usd"] is None
    assert body["spent_last_24h_unknown_exposure_rows"] == 1


def test_campaign_cost_summary_refuses_an_unknown_exposure_row(api):
    """`campaigns.py::_cost_summary` summed `amount_usd or 0.0` over its rows."""
    from app.api.v1.campaigns import _cost_summary
    from app.db import session_scope

    _cost(api["ws"], 3.0, detail={"campaign_id": "camp-1"})
    _cost(api["ws"], 0.0, detail={"campaign_id": "camp-1", "exposure_unknown": True})

    with session_scope() as s:
        out = _cost_summary(s, "camp-1", api["ws"])
        assert out["total_usd"] is None
        assert out["unknown_exposure_entries"] == 1
        assert out["priced_entries"] == 1
        assert out["entries"] == 2

        # A campaign with no rows at all: unknown, not $0.
        empty = _cost_summary(s, "camp-none", api["ws"])
        assert empty["total_usd"] is None
        assert empty["entries"] == 0

    _cost(api["ws"], 3.0, detail={"campaign_id": "camp-2"})
    with session_scope() as s:
        priced = _cost_summary(s, "camp-2", api["ws"])
    assert priced["total_usd"] == 3.0, "a fully priced campaign totals normally"


# ---------------------------------------------------------------------------
# §6 NULL completion_rate is not a zero completion
# ---------------------------------------------------------------------------


def test_completion_rate_cannot_be_null_because_the_column_forbids_it(api):
    """NOT FIXED — OUT OF SCOPE. The fabrications live in the STORAGE layer.

    `_accumulate` now skips a row whose `completion_rate` is NULL rather than
    averaging it as a zero, so the weighted mean is ready. But
    `PostMetric.completion_rate` is still `NOT NULL DEFAULT 0.0` and
    `PostStats.completion_rate` still defaults to `0.0`, and BOTH files are
    outside this lane's permitted write set — so no NULL can reach the code, and
    every platform that cannot compute a completion rate still stores "nobody
    finished the video".

    Asserted as the CURRENT shape so the gap is visible, and so a follow-up lane
    that makes the column nullable trips this test and gets to invert it.
    """
    from app.db import session_scope
    from app.engine.campaign import analytics as ca
    from app.providers.analytics import PostStats

    assert PostStats().completion_rate == 0.0, (
        "PostStats.completion_rate now defaults to None (providers/analytics is "
        "now nullable). Update PostMetric.completion_rate too, then INVERT this "
        "test to assert that an unreported rate is excluded from the mean."
    )
    post_id = _publish(api["ws"])
    _snapshot(post_id, views=1000, likes=0, comments=0, shares=0, saves=0,
              completion_rate=0.5)
    other = _publish(api["ws"])
    # The only way to write "unreported" today is the column default, which is
    # exactly the fabrication this test documents.
    _snapshot(other, views=1000, likes=0, comments=0, shares=0, saves=0)
    with session_scope() as s:
        rows = ca.latest_metrics(s, [post_id, other])
        assert rows[other].completion_rate == 0.0, (
            "an omitted completion_rate now stays NULL; the weighted mean "
            "excludes it. Invert this test and the line above."
        )
        out = ca._accumulate([rows[post_id], rows[other]])
    # Both rows are weighted, so the stored 0.0 for the unreported post pulls the
    # mean down -- the honest statement is that this number is not yet knowable.
    assert out["views"] == 2000
    assert out["completion"] == pytest.approx(0.25)


def test_a_rollup_over_no_snapshots_reports_no_completion_at_all(api):
    """The part that IS fixed: an unmeasured rollup has no completion figure."""
    from app.db import session_scope
    from app.engine.campaign import analytics as ca

    post_id = _publish(api["ws"])
    with session_scope() as s:
        rows = ca.latest_metrics(s, [post_id])
        out = ca._accumulate([rows.get(post_id)] if rows.get(post_id) else [])
    assert out["completion"] is None
    assert out["views"] is None
    assert out["engagement_rate"] is None


def test_unmeasured_duration_is_its_own_compare_bucket_not_a_short_video(api):
    """`_group_key` filed a post of unknown duration under "<25s"."""
    from app.api.v1.performance import _group_key

    class _Post:
        pass

    assert _group_key("duration", _Post(), None, {}, {}) == "unknown"
    assert _group_key("duration", _Post(), None, {"duration_seconds": 30.0}, {}) == "25-40s"


# ---------------------------------------------------------------------------
# §7 the frontend contract: a nullable field must be nullable in the schema
# ---------------------------------------------------------------------------

AUDITED_SCHEMAS = {
    "ApiV1WorkspacesWorkspaceAnalyticsOverview4": ["cost_total_usd"],
    "Totals5": ["views", "likes", "comments", "shares", "followers_gained"],
    "CostSummaryOut": ["spent_last_24h_usd"],
}


def _openapi() -> dict:
    path = REPO / "frontend" / "src" / "api" / "openapi.json"
    if not path.exists():
        pytest.skip("openapi.json not generated")
    return json.loads(path.read_text(encoding="utf-8"))


def test_every_audited_endpoint_still_publishes_a_schema():
    """A widened field is only honest if the contract says so.

    Without this, a backend returning ``null`` would be described to TypeScript
    as ``number`` and the next ``.toFixed()`` would invent a value in the
    browser.
    """
    paths = _openapi()["paths"]
    for path, method in (
        ("/api/v1/workspaces/{workspace_id}/analytics/overview", "get"),
        ("/api/v1/workspaces/{workspace_id}/analytics/breakdowns", "get"),
        ("/api/v1/workspaces/{workspace_id}/costs", "get"),
        ("/api/v1/workspaces/{workspace_id}/publishing/posts", "get"),
    ):
        assert path in paths, f"{path} vanished from the published spec"
        schema = (
            paths[path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        )
        assert schema.get("$ref"), f"{path} publishes no response schema"


def test_agents_endpoints_publish_no_schema_and_that_is_a_known_gap():
    """`/agents` and `/agents/{key}` publish NO response schema.

    Recorded rather than asserted-in-passing: the audit changes
    `failure_rate` / `total_cost_usd` on both, so the nullability is asserted in
    `test_analytics_honesty.py`'s module and in the frontend types, but the
    published spec cannot carry it until the endpoint joins the contract corpus
    (`scripts/gen_response_contracts.py`). A test that quietly skipped them would
    have let this look covered; one that fails would block the honesty fix on
    unrelated work. This states the gap so it stays visible.
    """
    paths = _openapi()["paths"]
    for path in (
        "/api/v1/workspaces/{workspace_id}/agents",
        "/api/v1/workspaces/{workspace_id}/agents/{agent_key}",
    ):
        schema = paths[path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {}, (
            f"{path} now publishes a schema — update this test and the audit's "
            f"`schema: null` entry for it rather than leaving a stale gap note"
        )


def test_every_widened_field_is_nullable_in_the_published_schema():
    """A null the runtime sends must be a null the contract admits.

    Without this the fix is half cosmetic: the backend returns null, the contract
    says `number`, and a consumer that trusts the contract invents a value in the
    browser. Contracts attach with `responses=`, which documents and does not
    filter, so the API answer is right either way — which is precisely why this
    needs asserting rather than assuming.

    Every audited endpoint's schema is checked, not just the ones that once
    lagged, because a field can be narrowed back without anyone noticing.
    """
    schemas = _openapi()["components"]["schemas"]
    non_nullable = []
    for name, fields in AUDITED_SCHEMAS.items():
        assert name in schemas, f"{name} is not published; a null cannot be typed"
        props = schemas[name]["properties"]
        for field in fields:
            assert field in props, f"{name}.{field} vanished from the schema"
            spec = props[field]
            nullable = spec.get("type") == "null" or "null" in str(spec.get("anyOf", ""))
            if not nullable:
                non_nullable.append(f"{name}.{field}")
    assert not non_nullable, (
        "these fields can be null at runtime but the published schema is "
        f"non-nullable: {sorted(non_nullable)}"
    )


def test_no_contract_lag_remains_because_the_schemas_caught_up():
    """The published schema admits null everywhere the runtime can send one.

    The contract lag is CLOSED: `generated.py` and `responses.py` now declare
    every widened field nullable, and the unknown-exposure counts are published.
    Asserted rather than skipped so the two cannot drift apart again — and so a
    future narrowing of the schema (someone tidying the annotations back to
    `int`) fails here with a name instead of shipping a lie.

    Every audited endpoint's schema is checked, not just the ones that once
    lagged, because a field can regress without anyone noticing the change.
    """
    schemas = _openapi()["components"]["schemas"]
    missing = []
    for name, fields in AUDITED_SCHEMAS.items():
        assert name in schemas, f"{name} is not published"
        for field in fields:
            spec = schemas[name]["properties"][field]
            nullable = spec.get("type") == "null" or "null" in str(spec.get("anyOf", ""))
            if not nullable:
                missing.append(f"{name}.{field}")
    assert not missing, (
        "these runtime-nullable fields are non-nullable in the published "
        f"schema: {sorted(missing)}"
    )

    audit = json.loads(
        (REPO / "docs" / "ANALYTICS_HONESTY_AUDIT.json").read_text(encoding="utf-8")
    )
    stale = [
        f"{f['schema']}.{f['schema_field']}"
        for f in audit["fields"]
        if f.get("contract_lag")
    ]
    assert not stale, (
        f"the audit still records contract lags the schema has closed: {stale}"
    )
    assert audit["counts"]["findings"] == 0, (
        f"the audit reports {audit['counts']['findings']} live finding(s); "
        f"run scripts/audit_analytics_honesty.py and see the table"
    )


def test_cost_summary_out_declares_the_unknown_row_count():
    """The field the UI reads to explain a missing total must be published.

    A field the frontend reads while the contract omits it is a field a strict
    consumer may filter out — and the count is the whole reason the total can be
    rendered as UNAVAILABLE *with a reason* rather than a bare dash.
    """
    props = _openapi()["components"]["schemas"]["CostSummaryOut"]["properties"]
    assert "spent_last_24h_unknown_exposure_rows" in props


def test_frontend_types_admit_null_for_the_widened_fields():
    src = (REPO / "frontend" / "src" / "features" / "analytics" / "Analytics.tsx").read_text(
        encoding="utf-8"
    )
    assert "cost_total_usd: number | null" in src, (
        "AnalyticsOverview.cost_total_usd can be null now; the TS type must say so"
    )
    assert "views: number | null" in src, (
        "OverviewTotals.views can be null now; a TS `number` would be a lie"
    )
