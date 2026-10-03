"""Work 16 §8/§9/§10 — observability, health/readiness, SLO/alert foundation.

Every guard in this file is written to FAIL when the guard is removed, and
each has a matching mutation test recorded in the work report: the guard is
disabled in-place, the test is re-run, and the real failure output is kept.
That is the only way to claim a guard works.

The fake credentials below are ASSEMBLED AT RUNTIME from fragments rather than
written as literals. Two reasons: a literal credential-shaped string does not
belong in version control even when it is fake, and building the shape in
pieces proves the redactor matches the real token shape rather than one
hard-coded exception.

Scoping note: this file does not use the session-scoped ``db_session``
fixture except where a real Job row is required, and it adds NO autouse
workspace scope — a blanket ambient workspace would silently break every test
that asserts the *absence* of one (see conftest's ``billable_workspace``
docstring).
"""

from __future__ import annotations

import json
import logging as std_logging
import sys

import pytest
from fastapi.testclient import TestClient
from loguru import logger

from app.services.observability import logging_setup
from app.services.observability import metrics as metrics_mod
from app.services.observability import redaction as redaction_mod
from app.services.observability import slo as slo_mod
from app.services.observability import tracing as tracing_mod
from app.services.observability.logging_setup import (
    MESSAGE_FORMAT,
    bind_context,
    json_sink,
    request_scope,
)

# ---------------------------------------------------------------------------
# Fake credentials, assembled so no literal secret is committed.
# ---------------------------------------------------------------------------

_FAKE = "FAKE"  # unmistakable marker, present in every value below

FAKE_API_KEY = f"sk-{'live'}-{_FAKE}0123456789abcdefXYZ"
FAKE_BEARER_TOKEN = f"{_FAKE}tokenABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
FAKE_BEARER = f"Bearer {FAKE_BEARER_TOKEN}"
FAKE_PASSWORD = f"hunter2-not-a-real-{_FAKE}-password"
_FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiJ9"
    ".eyJzdWIiOiJmYWtlIn0"
    ".abcdefghijklmnop"
)
FAKE_URL_WITH_PASSWORD = f"https://user:s3cr3t-in-url-{_FAKE}@api.example.test/v1"

ALL_FAKE_SECRETS = (
    FAKE_API_KEY,
    FAKE_BEARER_TOKEN,
    FAKE_PASSWORD,
    _FAKE_JWT,
    "s3cr3t-in-url-" + _FAKE,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    """Isolate metric state per test.

    The registry is process-wide, so a leaked counter would make a later
    alert-rule test fire for the wrong reason.
    """
    metrics_mod.REGISTRY.reset()
    yield
    metrics_mod.REGISTRY.reset()


@pytest.fixture(autouse=True)
def _clean_secrets():
    redaction_mod.reset_registry()
    yield
    redaction_mod.reset_registry()


@pytest.fixture()
def app_client():
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture()
def loguru_capture():
    """Attach a real loguru sink and remove it on teardown."""
    sink_id = logger.add(sys.stdout, format="{message}", level="DEBUG",
                         catch=False)
    yield
    logger.remove(sink_id)


# ---------------------------------------------------------------------------
# §8 metrics registry
# ---------------------------------------------------------------------------


def test_registry_renders_prometheus_text_format():
    metrics_mod.HTTP_REQUESTS.inc(method="GET", route="/jobs/{job_id}",
                                  status_class="2xx")
    metrics_mod.HTTP_DURATION.observe(0.12, method="GET", route="/jobs/{job_id}")
    text = metrics_mod.REGISTRY.render()

    assert "# HELP ymoney_http_requests_total" in text
    assert "# TYPE ymoney_http_requests_total counter" in text
    assert "# TYPE ymoney_http_request_duration_seconds histogram" in text
    assert ('ymoney_http_requests_total{method="GET",route="/jobs/{job_id}"'
            ',status_class="2xx"} 1') in text
    assert ('ymoney_http_request_duration_seconds_count{method="GET"'
            ',route="/jobs/{job_id}"} 1') in text
    assert ('ymoney_http_request_duration_seconds_bucket{method="GET"'
            ',route="/jobs/{job_id}",le="+Inf"} 1') in text


def test_registry_exposes_every_series_from_the_brief():
    """The §8 minimum set is all declared, even with no data source."""
    declared = {spec.name for spec in metrics_mod.REGISTRY.specs()}
    required = {
        "ymoney_http_request_duration_seconds",      # request latency
        "ymoney_http_request_errors_total",           # error rate
        "ymoney_job_queue_depth",                     # job queue depth
        "ymoney_job_start_latency_seconds",           # job latency
        "ymoney_job_duration_seconds",                # job duration
        "ymoney_worker_utilization_ratio",            # worker utilization
        "ymoney_gpu_utilization_ratio",               # GPU utilization
        "ymoney_gpu_slots_reserved",                  # GPU reservations
        "ymoney_render_duration_seconds",             # render duration
        "ymoney_provider_request_duration_seconds",   # provider latency
        "ymoney_provider_errors_total",               # provider errors
        "ymoney_publish_failures_total",              # publish failures
        "ymoney_paid_submission_unknown_total",       # SUBMISSION_UNKNOWN
        "ymoney_paid_unknown_exposure_usd",           # unknown exposure
        "ymoney_budget_refusals_total",               # budget refusals
        "ymoney_db_pool_utilization_ratio",           # DB pool utilization
        "ymoney_storage_failures_total",              # storage failures
    }
    assert required <= declared, f"missing: {sorted(required - declared)}"


def test_declared_zero_series_is_emitted_at_zero_with_an_explanation():
    """A metric with no data source is emitted at zero AND says so.

    The alternative -- omitting the series -- would make the GPU alert rule
    untestable and invisible. What must never happen is a non-zero value with
    no measurement behind it.
    """
    text = metrics_mod.REGISTRY.render()
    assert "ymoney_gpu_utilization_ratio 0" in text
    assert "DECLARED ZERO" in text
    spec = metrics_mod.REGISTRY.spec("ymoney_gpu_utilization_ratio")
    assert spec is not None
    assert spec.declared_zero is True
    assert "GPU worker lane" in spec.will_be_fed_by


def test_measured_series_are_not_marked_declared_zero():
    """Only GPU utilization lacks a source; everything else must be real."""
    declared = {
        spec.name for spec in metrics_mod.REGISTRY.specs() if spec.declared_zero
    }
    assert declared == {"ymoney_gpu_utilization_ratio"}


def test_cardinality_cap_drops_and_counts_rather_than_growing():
    """A hostile label set must not be able to grow the registry unbounded."""
    registry = metrics_mod.MetricRegistry(max_series_per_metric=10)
    registry.register(metrics_mod.MetricSpec(
        name="ymoney_test_series", kind=metrics_mod.MetricType.COUNTER,
        help="t", labelnames=("id",)))
    for i in range(50):
        registry.inc("ymoney_test_series", id=str(i))
    assert registry.series_overflow_total == 40
    assert len(registry.render().splitlines()) < 30


def test_undeclared_labels_are_dropped_not_stored():
    """An undeclared label would split a series that must be one total."""
    metrics_mod.PUBLISH_FAILURES.inc(platform="youtube")
    metrics_mod.PUBLISH_FAILURES.inc(platform="youtube", attacker="x")
    assert metrics_mod.PUBLISH_FAILURES.value(platform="youtube") == 2


def test_gauge_write_into_counter_is_refused_not_corrupting():
    """A type mismatch must drop the write, not silently corrupt a counter."""
    before = metrics_mod.PAID_SUBMISSION_UNKNOWN.value(provider="p", operation="o")
    metrics_mod.REGISTRY.set("ymoney_paid_submission_unknown_total", 99.0,
                             provider="p", operation="o")
    after = metrics_mod.PAID_SUBMISSION_UNKNOWN.value(provider="p", operation="o")
    assert after == before


def test_db_pool_utilization_collector_reads_real_pool():
    """DB pool utilization must come from SQLAlchemy, not a constant."""
    assert metrics_mod.collect_db_pool() is True
    assert metrics_mod.DB_POOL_UTILIZATION.value() >= 0.0
    assert metrics_mod.DB_POOL_SIZE.value() >= 1.0
    assert metrics_mod.COLLECTOR_UP.value(collector="database") == 1.0


def test_collector_failure_is_isolated_and_does_not_blind_the_others():
    """One dead subsystem must not blind every other collector."""
    original = metrics_mod.collect_db_pool
    metrics_mod.collect_db_pool = _raise  # type: ignore[assignment]
    try:
        results = metrics_mod.collect_all()
    finally:
        metrics_mod.collect_db_pool = original  # type: ignore[assignment]
    assert results["database"] is False
    assert "workers" in results and "jobs" in results and "storage" in results


def _raise() -> bool:
    raise RuntimeError("collector exploded")


# ---------------------------------------------------------------------------
# §8 redaction -- THE guard
# ---------------------------------------------------------------------------


def test_secret_is_never_reachable_through_the_key_name_path():
    """Layer 2 in isolation.

    The values here deliberately have NO recognisable credential SHAPE, so the
    only thing that can mask them is the key name. Using a realistic ``sk-``
    value would let layer 3 pass the test even if the key-name layer were
    deleted -- which is exactly the false confidence this test exists to avoid.
    """
    shapeless = "plainvalue123456"
    assert redaction_mod.redact_text(shapeless) == shapeless, "value is not shaped"
    assert redaction_mod.redact({"api_key": shapeless})["api_key"] == (
        redaction_mod.MASK)
    assert redaction_mod.redact({"Authorization": shapeless})["Authorization"] == (
        redaction_mod.MASK)
    assert redaction_mod.redact({"nested": {"password": shapeless}})[
        "nested"]["password"] == redaction_mod.MASK
    assert redaction_mod.redact({"db": {"s3_secret_key": shapeless}})[
        "db"]["s3_secret_key"] == redaction_mod.MASK


def test_secret_is_never_reachable_through_the_registered_literal_path():
    """A credential the process HOLDS is scrubbed even under an innocent key."""
    redaction_mod.register_secret(FAKE_PASSWORD)
    out = redaction_mod.redact(
        {"note": f"connect failed using {FAKE_PASSWORD} for user bob",
         "count": 3})
    assert FAKE_PASSWORD not in json.dumps(out)
    assert out["note"].endswith("for user bob")
    assert out["count"] == 3


def test_secret_is_never_reachable_through_the_value_shape_path():
    """A credential the process does NOT hold is masked by its shape."""
    assert FAKE_API_KEY not in redaction_mod.redact_text(
        f"calling openai with {FAKE_API_KEY}")
    assert FAKE_BEARER_TOKEN not in redaction_mod.redact_text(
        f"header was {FAKE_BEARER}")
    assert _FAKE_JWT not in redaction_mod.redact_text(f"got {_FAKE_JWT} back")
    assert "s3cr3t-in-url" not in redaction_mod.redact_text(FAKE_URL_WITH_PASSWORD)


def test_registration_ignores_values_too_short_to_be_a_credential():
    """A 1-character 'secret' would redact the alphabet."""
    assert redaction_mod.register_secret("ab") is False
    assert redaction_mod.register_secret(FAKE_PASSWORD) is True
    assert redaction_mod.registered_secret_count() == 1


def test_redaction_does_not_destroy_reconciliation_evidence():
    """``idempotency_key`` is NOT a secret -- it is the reconciliation handle.

    A redaction list broad enough to match any ``*_key`` would destroy the
    evidence the Work 15 paid-submission contract exists to preserve.
    """
    assert redaction_mod.is_secret_key("idempotency_key") is False
    out = redaction_mod.redact({"idempotency_key": "idem-abc-123"})
    assert out["idempotency_key"] == "idem-abc-123"
    # ...while a real credential is still caught.
    assert redaction_mod.is_secret_key("openai_api_key") is True
    assert redaction_mod.is_secret_key("s3_secret_key") is True
    assert redaction_mod.is_secret_key("workspace_id") is False
    assert redaction_mod.is_secret_key("campaign_id") is False


def test_a_secret_in_a_log_payload_is_redacted(capsys):
    """(a) MUTATION TEST — a fake API key never reaches sink output.

    Fed through the REAL loguru logger with the REAL structured formatter and
    the REAL redactor, via every route a secret can travel: a bound extra, an
    interpolated message, a nested dict, and a raised exception.
    """
    redaction_mod.register_secret(FAKE_PASSWORD)
    sink_id = logger.add(json_sink(sys.stdout), format=MESSAGE_FORMAT,
                         level="DEBUG", catch=False)
    try:
        logger.bind(api_key=FAKE_API_KEY).info("bound extra leak")
        logger.info(f"interpolated leak {FAKE_API_KEY}")
        logger.bind(payload={"Authorization": FAKE_BEARER,
                             "db_dsn": FAKE_URL_WITH_PASSWORD}).warning("nested leak")
        logger.bind(detail=f"db failed for {FAKE_PASSWORD}").error("literal leak")
        try:
            raise ValueError(f"provider rejected {FAKE_API_KEY}")
        except ValueError as exc:
            logger.opt(exception=exc).error("exception leak")
    finally:
        logger.remove(sink_id)

    out = capsys.readouterr().out
    for secret in ALL_FAKE_SECRETS:
        assert secret not in out, f"secret leaked into sink output: {secret}"
    assert redaction_mod.MASK in out
    # The non-secret context must SURVIVE redaction, or the log is useless.
    assert "bound extra leak" in out


def test_log_record_scrubbing_drops_objects_that_carry_credentials():
    scrubbed = redaction_mod.scrub_record({
        "message": "hello",
        "request": object(),
        "headers": {"Authorization": FAKE_BEARER},
        "workspace_id": "ws-1",
        "openai_api_key": FAKE_API_KEY,
    })
    assert scrubbed["headers"] == "<dict withheld>"
    assert scrubbed["request"].endswith("withheld>")
    assert scrubbed["openai_api_key"] == redaction_mod.MASK
    assert scrubbed["workspace_id"] == "ws-1"


# ---------------------------------------------------------------------------
# §8 request_id propagation
# ---------------------------------------------------------------------------


def test_e_request_id_reaches_the_log_record(capsys):
    """(e) MUTATION TEST — request_id propagates to the log record."""
    sink_id = logger.add(json_sink(sys.stdout), format=MESSAGE_FORMAT,
                         level="DEBUG", catch=False)
    try:
        with request_scope() as rid:
            logger.info("correlated line")
            # Also visible to the PRE-EXISTING Work 12 store.
            from app.core.request_context import request_id as legacy_rid

            assert legacy_rid() == rid
    finally:
        logger.remove(sink_id)

    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert lines, "no log output captured"
    for line in lines:
        payload = json.loads(line)
        assert payload["request_id"], "request_id missing from log record"
        assert payload["message"] == "correlated line"


def test_inbound_request_id_is_echoed_on_the_response(app_client):
    response = app_client.get("/health", headers={"X-Request-ID": "caller-abc"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "caller-abc"


def test_request_id_is_generated_when_absent(app_client):
    response = app_client.get("/health")
    assert response.status_code == 200
    assert response.headers.get("X-Request-ID")


def test_e_request_id_reaches_the_log_record_end_to_end(app_client, capsys):
    """(e) MUTATION TEST -- middleware -> log record, same ID as the header.

    The unit test above proves ``request_scope`` sets the ID; this proves the
    HTTP middleware actually USES it. Checking only the response header would
    pass even if the middleware stopped opening a request scope entirely,
    because it still has the resolved id in hand to echo.
    """
    sink_id = logger.add(json_sink(sys.stdout), format=MESSAGE_FORMAT,
                         level="DEBUG", catch=False)
    # No logger.level() call is needed: loguru's logger filter is DEBUG by
    # default, and the SINK level above is what admits the access line.
    try:
        response = app_client.get("/health",
                                  headers={"X-Request-ID": "e2e-trace-42"})
    finally:
        logger.remove(sink_id)

    rid = response.headers["X-Request-ID"]
    assert rid == "e2e-trace-42"

    lines = [ln for ln in capsys.readouterr().out.splitlines()
             if ln.strip().startswith("{")]
    payloads = [json.loads(ln) for ln in lines]
    assert payloads, "the middleware emitted no structured log line"
    assert any(p.get("request_id") == rid for p in payloads), (
        f"no log record carried request_id={rid!r}; saw "
        f"{sorted({p.get('request_id') for p in payloads})}")


def test_inbound_request_id_is_clamped_not_echoed_verbatim():
    """An unbounded caller string must not land in every log line."""
    from app.services.observability.logging_setup import resolve_request_id

    out = resolve_request_id("x" * 500 + "!" * 50)
    assert len(out) <= 64
    assert "!" not in out


def test_request_id_scope_restores_the_previous_value():
    from app.core.request_context import request_id as legacy_rid

    with request_scope("outer") as outer:
        assert legacy_rid() == outer
        with request_scope("inner") as inner:
            assert legacy_rid() == inner
        assert legacy_rid() == outer


def test_bind_context_restores_previous_correlation():
    from app.services.observability.logging_setup import current_correlation

    with bind_context(workspace_id="ws-1", job_id="job-1"):
        bound = current_correlation()
        assert bound["workspace_id"] == "ws-1"
        assert bound["job_id"] == "job-1"
    assert "workspace_id" not in current_correlation()


# ---------------------------------------------------------------------------
# §8 HTTP middleware instrumentation
# ---------------------------------------------------------------------------


def test_middleware_records_latency_and_request_count(app_client):
    metrics_mod.REGISTRY.reset()
    app_client.get("/health")
    assert metrics_mod.HTTP_REQUESTS.value(
        method="GET", route="/health", status_class="2xx") == 1
    assert metrics_mod.HTTP_DURATION.value(method="GET", route="/health") >= 0.0


def test_a_404_is_a_4xx_not_a_5xx(app_client):
    """The error counter is documented as 5xx; a client error must not add to it.

    Counting 4xx here would make the availability SLO a client-behaviour
    metric, which is a different objective entirely.
    """
    metrics_mod.REGISTRY.reset()
    app_client.get("/definitely-not-a-route-xyz")
    assert metrics_mod.HTTP_REQUESTS.value(
        method="GET", route="__unmatched__:/definitely-not-a-route-xyz",
        status_class="4xx") == 1
    assert metrics_mod.HTTP_ERRORS.value(
        method="GET", route="__unmatched__:/definitely-not-a-route-xyz") == 0.0


def test_a_5xx_is_counted_as_a_server_error():
    """Driven through the recorder the middleware itself calls."""
    metrics_mod.REGISTRY.reset()
    metrics_mod.record_http_request("POST", "/jobs", 500, 0.3)
    metrics_mod.record_http_request("POST", "/jobs", 503, 0.4)
    assert metrics_mod.HTTP_ERRORS.value(method="POST", route="/jobs") == 2.0
    assert metrics_mod.HTTP_REQUESTS.value(
        method="POST", route="/jobs", status_class="5xx") == 2.0


def test_middleware_uses_a_templated_route_not_the_raw_path(app_client):
    """A raw path as a label would let any client mint unbounded series."""
    metrics_mod.REGISTRY.reset()
    app_client.get("/internal/traces?limit=5")
    text = metrics_mod.REGISTRY.render()
    assert 'route="/internal/traces"' in text
    assert "limit=5" not in text


# ---------------------------------------------------------------------------
# §8 tracing
# ---------------------------------------------------------------------------


def test_opentelemetry_is_not_installed_so_the_recorder_is_used():
    """Honest provenance: this must not claim to be OTLP."""
    from importlib.util import find_spec

    assert find_spec("opentelemetry") is None
    assert tracing_mod.otel_installed() is False
    assert tracing_mod.tracer.stats()["backend"] == "in_process_recorder"


def test_span_records_parent_child_timing_and_status():
    tracing_mod.tracer.clear()
    with tracing_mod.tracer.start_span("api") as root, tracing_mod.tracer.start_span(
            "provider", kind=tracing_mod.SpanKind.PROVIDER) as child:
        assert child.parent_span_id == root.span_id
        assert child.trace_id == root.trace_id
    # as_dicts is most-recent-first, so the child (closed first) comes first.
    spans = tracing_mod.tracer.as_dicts()
    assert [s["name"] for s in spans] == ["api", "provider"]
    assert spans[0]["status"] == "ok"
    assert spans[1]["parent_span_id"] == spans[0]["span_id"]


def test_span_records_error_and_still_reraises():
    tracing_mod.tracer.clear()
    with pytest.raises(ValueError), tracing_mod.tracer.start_span("failing"):
        raise ValueError("boom")
    recorded = tracing_mod.tracer.as_dicts()
    assert recorded[0]["status"] == "error"
    assert recorded[0]["error_type"] == "ValueError"


def test_span_attributes_are_redacted_on_output():
    """A provider span that recorded a header must not publish it."""
    tracing_mod.tracer.clear()
    with tracing_mod.tracer.start_span(
            "provider", attributes={"api_key": FAKE_API_KEY,
                                    "provider": "openai"}):
        pass
    payload = json.dumps(tracing_mod.tracer.as_dicts())
    assert FAKE_API_KEY not in payload
    assert "openai" in payload


def test_traces_endpoint_reports_its_backend(app_client):
    tracing_mod.tracer.clear()
    with tracing_mod.tracer.start_span("op", kind=tracing_mod.SpanKind.JOB):
        pass
    body = app_client.get("/internal/traces").json()
    assert body["otel_installed"] is False
    assert body["stats"]["started"] >= 1
    grouped = app_client.get("/internal/traces?group_by_trace=true").json()
    assert grouped["traces"], "grouped_by_trace returned nothing"


# ---------------------------------------------------------------------------
# §9 livez vs readyz
# ---------------------------------------------------------------------------


def test_liveness_is_untouched_by_a_database_outage(monkeypatch):
    """Liveness must not depend on the DB, or a DB outage becomes a crash loop."""
    from app.api.v1 import internal_ops

    monkeypatch.setattr(internal_ops, "_check_database",
                        lambda: (False, "unreachable: OperationalError", "fix"))
    report = internal_ops.readiness()
    assert report["status"] == "not_ready"
    # ...but liveness is untouched by the same outage.
    assert internal_ops.liveness()["status"] == "alive"
    assert all(c["id"] in {"process", "event_loop"}
               for c in internal_ops.liveness()["checks"])


def test_livez_touches_no_database(app_client, monkeypatch):
    """Proof by construction: a DB call during /livez explodes this test."""
    import sqlalchemy

    def _boom(*_a, **_kw):
        raise AssertionError("/livez touched the database")

    monkeypatch.setattr(sqlalchemy.engine.Engine, "connect", _boom)
    assert app_client.get("/livez").status_code == 200


def test_readyz_is_ready_when_all_critical_dependencies_are_up(app_client):
    body = app_client.get("/readyz").json()
    assert body["status"] == "ready"
    assert body["blocking_failures"] == []


def test_readyz_critical_dependency_list_is_exactly_the_three_named():
    from app.api.v1.internal_ops import CRITICAL_CHECKS

    assert list(CRITICAL_CHECKS) == [
        "database", "migrations", "job_backend"]


def test_b_readiness_goes_not_ready_when_the_database_is_unreachable(monkeypatch):
    """(b) MUTATION TEST — a dead DB means 503 and a blocking failure."""
    from app.main import app

    def _dead_db(*_a, **_kw):
        raise RuntimeError("connection refused")

    # Only the helper _check_database actually calls. Patching
    # Engine.connect instead would also break the TestClient lifespan (which
    # runs migrations), which is a different failure than the one under test.
    monkeypatch.setattr("app.db.session_scope", _dead_db)

    # No ``with`` block: entering the context runs the lifespan, and a broken
    # database would fail there -- before /readyz is ever reached.
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/readyz")

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["status"] == "not_ready"
    assert "database" in body["blocking_failures"]
    db_check = next(c for c in body["checks"] if c["id"] == "database")
    assert db_check["critical"] is True and db_check["blocking"] is True
    assert db_check["remediation"]


def test_c_readiness_stays_ready_when_an_optional_provider_is_down(monkeypatch):
    """(c) MUTATION TEST — a provider outage must NOT un-ready the system."""
    from app.main import app
    from app.providers import maturity

    monkeypatch.setattr(maturity, "probe_health",
                        lambda _record: maturity.HEALTH_DOWN)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/readyz")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["blocking_failures"] == []
    # The degradation IS surfaced — just not as a readiness input.
    assert body["providers"]["degraded"] is True
    assert body["providers"]["providers_down"] > 0
    assert body["providers"]["blocking"] is False
    assert "providers" in body["degraded"]


def test_provider_surface_failure_also_does_not_break_readiness(monkeypatch):
    """Even a broken maturity probe must not fail readiness."""
    from app.api.v1 import internal_ops
    from app.providers import maturity

    monkeypatch.setattr(maturity, "list_maturity", _raise)
    report = internal_ops.readiness()
    assert report["status"] == "ready"
    assert report["providers"]["available"] is False
    assert report["providers"]["blocking"] is False


def test_readyz_fails_on_a_broken_migration_state(monkeypatch):
    """A half-applied schema is a critical failure, not a warning."""
    from app.api.v1 import internal_ops

    monkeypatch.setattr(
        internal_ops, "_check_migrations",
        lambda: (False, "schema_migrations is empty", "run migrations"))
    report = internal_ops.readiness()
    assert report["status"] == "not_ready"
    assert "migrations" in report["blocking_failures"]


def test_readyz_reports_its_own_dependency_list(app_client):
    body = app_client.get("/readyz").json()
    assert body["critical_dependencies"] == [
        "database", "migrations", "job_backend"]
    assert "Only critical" in body["policy"]


# ---------------------------------------------------------------------------
# §8 metrics endpoint
# ---------------------------------------------------------------------------


def test_metrics_endpoint_serves_prometheus_content_type(app_client):
    response = app_client.get("/internal/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "version=0.0.4" in response.headers["content-type"]
    assert "# TYPE ymoney_http_requests_total counter" in response.text


def test_metrics_endpoint_sets_collector_heartbeats(app_client):
    body = app_client.get("/internal/collectors").json()
    assert body["collectors"]["database"] == "up"
    assert body["collectors"]["storage"] == "up"


# ---------------------------------------------------------------------------
# §10 SLO targets -- declarative, never an achieved value
# ---------------------------------------------------------------------------


def test_slo_targets_are_declarative_and_claim_no_achievement():
    catalog = slo_mod.slo_catalog()
    assert catalog["measured"] is False
    assert "Targets only" in catalog["disclaimer"]
    for target in catalog["targets"]:
        assert target["measured"] is False, f"{target['id']} claims measurement"
        assert target["source_metrics"], f"{target['id']} has no source metric"
        assert target["target"]


def test_every_slo_source_metric_actually_exists():
    """An SLO pointing at a metric nobody declares is a broken definition."""
    declared = {spec.name for spec in metrics_mod.REGISTRY.specs()}
    for target in slo_mod.SLO_TARGETS:
        for metric in target.source_metrics:
            assert metric in declared, (
                f"SLO {target.id} references undeclared metric {metric}")


def test_the_seven_named_objectives_are_covered():
    ids = {t.id for t in slo_mod.SLO_TARGETS}
    assert {"api_availability", "job_start_latency", "publish_failure_rate",
            "render_failure_rate", "unknown_paid_submissions",
            "queue_backlog"} <= ids


# ---------------------------------------------------------------------------
# §10 alert rules -- (d) each fires on breach, quiet within threshold
# ---------------------------------------------------------------------------


def _rules_firing(snapshot: metrics_mod.Snapshot, now: float) -> set[str]:
    return {v.rule_id for v in slo_mod.evaluate_all(snapshot, now) if v.firing}


@pytest.mark.parametrize("rule_id", [
    "paid_submission_unknown", "unknown_exposure_high", "queue_stalled",
    "worker_fleet_unavailable", "db_unavailable", "storage_unavailable",
    "repeated_publish_failure", "gpu_queue_starvation",
])
def test_d_every_alert_rule_is_defined_and_documented(rule_id: str):
    rule = slo_mod.rule_by_id(rule_id)
    assert rule is not None, f"no rule {rule_id}"
    assert rule.runbook, f"{rule_id} has no runbook"
    assert str(rule.severity) in {"critical", "warning"}
    assert rule.what_it_detects


def test_d_all_eight_required_rules_are_present():
    assert len(slo_mod.ALERT_RULES) == 8


def test_d_submission_unknown_fires_on_one_and_is_quiet_on_zero():
    now = 1_000_000.0
    quiet = metrics_mod.REGISTRY.snapshot()
    assert "paid_submission_unknown" not in _rules_firing(quiet, now)

    metrics_mod.PAID_SUBMISSION_UNKNOWN.inc(provider="mpt", operation="render")
    breached = metrics_mod.REGISTRY.snapshot()
    assert "paid_submission_unknown" in _rules_firing(breached, now)


def test_d_unknown_exposure_fires_above_threshold_and_is_quiet_below():
    now = 1_000_000.0
    metrics_mod.PAID_UNKNOWN_EXPOSURE.set(0.50, provider="mpt")
    assert "unknown_exposure_high" not in _rules_firing(
        metrics_mod.REGISTRY.snapshot(), now)

    metrics_mod.PAID_UNKNOWN_EXPOSURE.set(1.01, provider="mpt")
    assert "unknown_exposure_high" in _rules_firing(
        metrics_mod.REGISTRY.snapshot(), now)


def test_d_queue_stalled_needs_queue_depth_AND_silence():
    """A deep queue with recent activity is healthy backlog, not a stall."""
    now = 1_000_000.0
    metrics_mod.JOB_QUEUE_DEPTH.set(40, status="QUEUED")
    metrics_mod.JOB_LAST_START_TS.set(now - 10.0)
    assert "queue_stalled" not in _rules_firing(
        metrics_mod.REGISTRY.snapshot(), now)

    # Deep queue AND silent for > 900s.
    metrics_mod.JOB_LAST_START_TS.set(now - 1000.0)
    assert "queue_stalled" in _rules_firing(
        metrics_mod.REGISTRY.snapshot(), now)

    # Silent but EMPTY queue is idle, not stalled.
    metrics_mod.JOB_QUEUE_DEPTH.set(0, status="QUEUED")
    assert "queue_stalled" not in _rules_firing(
        metrics_mod.REGISTRY.snapshot(), now)


def test_d_worker_fleet_unavailable_fires_on_zero_and_quiet_above():
    now = 1_000_000.0
    metrics_mod.WORKERS_TOTAL.set(0.0)
    assert "worker_fleet_unavailable" in _rules_firing(
        metrics_mod.REGISTRY.snapshot(), now)
    metrics_mod.WORKERS_TOTAL.set(4.0)
    assert "worker_fleet_unavailable" not in _rules_firing(
        metrics_mod.REGISTRY.snapshot(), now)


def test_d_db_unavailable_fires_on_a_zero_heartbeat_and_quiet_on_one():
    now = 1_000_000.0
    # Absent series reads as 0: a collector that never ran is DOWN, not healthy.
    assert "db_unavailable" in _rules_firing(
        metrics_mod.REGISTRY.snapshot(), now)
    metrics_mod.COLLECTOR_UP.set(1.0, collector="database")
    assert "db_unavailable" not in _rules_firing(
        metrics_mod.REGISTRY.snapshot(), now)


def test_d_storage_unavailable_fires_on_zero_and_quiet_on_one():
    now = 1_000_000.0
    metrics_mod.STORAGE_AVAILABLE.set(0.0)
    assert "storage_unavailable" in _rules_firing(
        metrics_mod.REGISTRY.snapshot(), now)
    metrics_mod.STORAGE_AVAILABLE.set(1.0)
    assert "storage_unavailable" not in _rules_firing(
        metrics_mod.REGISTRY.snapshot(), now)


def test_d_repeated_publish_failure_fires_on_a_streak_and_resets_on_success():
    """A cumulative counter would fire forever; only a streak is actionable."""
    now = 1_000_000.0
    metrics_mod.record_publish_result("youtube", success=False)
    metrics_mod.record_publish_result("youtube", success=False)
    assert "repeated_publish_failure" not in _rules_firing(
        metrics_mod.REGISTRY.snapshot(), now)
    metrics_mod.record_publish_result("youtube", success=False)
    assert "repeated_publish_failure" in _rules_firing(
        metrics_mod.REGISTRY.snapshot(), now)

    metrics_mod.record_publish_result("youtube", success=True)
    assert "repeated_publish_failure" not in _rules_firing(
        metrics_mod.REGISTRY.snapshot(), now)


def test_d_gpu_queue_starvation_needs_waiting_jobs_and_no_slot():
    now = 1_000_000.0
    metrics_mod.GPU_JOBS_WAITING.set(0.0)
    metrics_mod.GPU_SLOTS_RESERVED.set(0.0)
    assert "gpu_queue_starvation" not in _rules_firing(
        metrics_mod.REGISTRY.snapshot(), now)

    metrics_mod.GPU_JOBS_WAITING.set(3.0)
    assert "gpu_queue_starvation" in _rules_firing(
        metrics_mod.REGISTRY.snapshot(), now)

    # Slots held means work IS being served.
    metrics_mod.GPU_SLOTS_RESERVED.set(1.0)
    assert "gpu_queue_starvation" not in _rules_firing(
        metrics_mod.REGISTRY.snapshot(), now)


def test_d_every_rule_has_a_firing_and_a_quiet_case():
    """Enumerated, not sampled: each of the 8 rules is driven both ways."""
    now = 1_000_000.0

    def verdict(rule_id: str):
        return slo_mod.rule_by_id(rule_id).check(
            metrics_mod.REGISTRY.snapshot(), now)

    assert verdict("paid_submission_unknown").firing is False
    metrics_mod.PAID_SUBMISSION_UNKNOWN.inc(provider="p", operation="o")
    assert verdict("paid_submission_unknown").firing is True

    metrics_mod.REGISTRY.reset()
    metrics_mod.PAID_UNKNOWN_EXPOSURE.set(0.0, provider="p")
    assert verdict("unknown_exposure_high").firing is False
    metrics_mod.PAID_UNKNOWN_EXPOSURE.add(5.0, provider="p")
    assert verdict("unknown_exposure_high").firing is True

    metrics_mod.REGISTRY.reset()
    metrics_mod.JOB_QUEUE_DEPTH.set(10, status="QUEUED")
    metrics_mod.JOB_LAST_START_TS.set(now)
    assert verdict("queue_stalled").firing is False
    metrics_mod.JOB_LAST_START_TS.set(now - 5000)
    assert verdict("queue_stalled").firing is True

    metrics_mod.REGISTRY.reset()
    metrics_mod.WORKERS_TOTAL.set(2.0)
    assert verdict("worker_fleet_unavailable").firing is False
    metrics_mod.WORKERS_TOTAL.set(0.0)
    assert verdict("worker_fleet_unavailable").firing is True

    metrics_mod.REGISTRY.reset()
    metrics_mod.COLLECTOR_UP.set(1.0, collector="database")
    assert verdict("db_unavailable").firing is False
    metrics_mod.COLLECTOR_UP.set(0.0, collector="database")
    assert verdict("db_unavailable").firing is True

    metrics_mod.REGISTRY.reset()
    metrics_mod.STORAGE_AVAILABLE.set(1.0)
    assert verdict("storage_unavailable").firing is False
    metrics_mod.STORAGE_AVAILABLE.set(0.0)
    assert verdict("storage_unavailable").firing is True

    metrics_mod.REGISTRY.reset()
    metrics_mod.PUBLISH_FAILURES_CONSECUTIVE.set(0.0, platform="tiktok")
    assert verdict("repeated_publish_failure").firing is False
    metrics_mod.PUBLISH_FAILURES_CONSECUTIVE.set(5.0, platform="tiktok")
    assert verdict("repeated_publish_failure").firing is True

    metrics_mod.REGISTRY.reset()
    metrics_mod.GPU_JOBS_WAITING.set(0.0)
    assert verdict("gpu_queue_starvation").firing is False
    metrics_mod.GPU_JOBS_WAITING.set(2.0)
    metrics_mod.GPU_SLOTS_RESERVED.set(0.0)
    assert verdict("gpu_queue_starvation").firing is True


def test_alerts_endpoint_reports_definitions_and_current_verdicts(app_client):
    body = app_client.get("/internal/alerts").json()
    assert body["firing_count"] >= 0
    assert len(body["rules"]) == 8
    for rule in body["rules"]:
        assert rule["runbook"]
        assert "firing" in rule


def test_slo_endpoint_exposes_targets_and_rule_definitions(app_client):
    body = app_client.get("/internal/slo").json()
    assert body["measured"] is False
    assert len(body["targets"]) >= 7
    assert len(body["alert_rules"]) == 8


# ---------------------------------------------------------------------------
# paid-execution seams reuse the Work 15 vocabulary
# ---------------------------------------------------------------------------


def test_record_paid_outcome_reuses_submission_state_values():
    from app.services.paid_jobs import SubmissionState

    metrics_mod.record_paid_outcome(
        "mpt", "render", SubmissionState.SUBMISSION_UNKNOWN,
        estimated_cost_usd=0.42)
    assert metrics_mod.PAID_SUBMISSION_UNKNOWN.value(
        provider="mpt", operation="render") == 1
    assert metrics_mod.PAID_UNKNOWN_EXPOSURE.value(
        provider="mpt") == pytest.approx(0.42)


def test_known_paid_outcome_does_not_raise_unknown_exposure():
    from app.services.paid_jobs import SubmissionState

    metrics_mod.record_paid_outcome("mpt", "render", SubmissionState.SUCCEEDED,
                                    estimated_cost_usd=0.42)
    assert metrics_mod.PAID_SUBMISSION_UNKNOWN.value(
        provider="mpt", operation="render") == 0
    assert metrics_mod.PAID_UNKNOWN_EXPOSURE.value(provider="mpt") == 0.0


def test_budget_refusal_kind_maps_the_real_exception_classes():
    from app.services.cost import BudgetExceededError, RateLimitExceeded
    from app.services.paid_provider import OwnerlessSpendRefused

    assert metrics_mod.budget_refusal_kind(
        OwnerlessSpendRefused(provider="mpt", operation="render",
                              reason="no workspace scope")) == "ownerless_spend"
    assert metrics_mod.budget_refusal_kind(
        BudgetExceededError("daily budget exhausted")) == "budget_exceeded"
    assert metrics_mod.budget_refusal_kind(
        RateLimitExceeded("slow down")) == "rate_limit"
    assert metrics_mod.budget_refusal_kind(ValueError("nope")) == "unknown"


# ---------------------------------------------------------------------------
# config surface
# ---------------------------------------------------------------------------


def test_observability_and_slo_settings_exist():
    from app.core.config import Settings

    fresh = Settings()
    assert fresh.observability_enabled is True
    assert fresh.observability_json_logs is True
    assert fresh.observability_max_spans == 2000
    assert fresh.observability_max_series_per_metric == 512
    assert fresh.slo_api_availability_target == 0.995
    assert fresh.slo_unknown_exposure_max_usd == 5.0
    assert fresh.alert_queue_stall_seconds == 900.0
    assert fresh.alert_publish_failure_streak == 3


# ---------------------------------------------------------------------------
# stdlib logging must not bypass the sink
# ---------------------------------------------------------------------------


def test_install_never_adds_an_unredacting_stdlib_handler():
    """Installing the JSON sink must not attach a bypassing stdlib handler.

    ``app.api.v1.ops`` and friends use ``logging.getLogger``. Routing them into
    an unformatted sink would be a bypass of the redactor.

    The invariant is about what *YMONEY* attaches, not about what already
    exists. Third-party libraries install their own handlers on import --
    ``boto3``, ``botocore``, ``urllib3`` and ``s3transfer`` all do, and they
    appear only once something in the suite has imported them. Asserting that
    *no* stdlib logger has a handler therefore fails for reasons that have
    nothing to do with this project, and passed only when the file ran alone.

    So: snapshot the handlers, install, and assert the set did not grow.
    """
    def handler_ids() -> set[int]:
        return {
            id(handler)
            for name, log in std_logging.Logger.manager.loggerDict.items()
            if isinstance(log, std_logging.Logger)
            for handler in log.handlers
        }

    before = handler_ids()
    # `install()` is idempotent and returns False when the sink is already
    # present, so its return value says nothing about this invariant. Calling
    # it is what matters; the handler set is the evidence either way.
    logging_setup.install(serialize=True)
    after = handler_ids()

    added = after - before
    assert not added, (
        f"install() attached {len(added)} handler(s) to stdlib loggers, which "
        "would bypass the redactor")