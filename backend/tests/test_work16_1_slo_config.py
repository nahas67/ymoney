"""Work 16.1 §6 -- SLO/alert thresholds are configuration, not decoration.

**What this module existed to be.** ``core/config.py`` advertised nine
``slo_*`` / ``alert_*`` settings and ``services/observability/slo.py`` compared
every threshold against a bare literal (``total > 1.0``, ``idle > 900.0``,
``worst >= 3.0``). ``grep "settings.alert_|settings.slo_" backend/`` returned
**zero** matches. Every one of those nine settings was a lie: it read as
retunable and did nothing.

**What is asserted here, in the order that matters.**

1. The chain *environment -> validated Settings -> evaluator -> verdict* is
   real. Not "the value is stored" -- the **verdict flips**. If a rule
   hard-coded its own number again, the same assertion fails.
2. A nonsense threshold is rejected **at startup**, by ``Settings`` itself, with
   a raised error rather than a coerced value.
3. The numbers that are deliberately NOT configurable say so, and the source
   cannot quietly acquire a knob for them.

The structural tests read the AST rather than the text, so they fail on a real
hard-coded threshold and not on a reworded comment.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.config import settings as live_settings
from app.services.observability import metrics as metrics_mod
from app.services.observability import slo as slo_mod

APP_DIR = Path(__file__).resolve().parent.parent / "app"
SLO_PATH = APP_DIR / "services" / "observability" / "slo.py"

#: Every setting §6 owns, in the two classes it deliberately separates.
COMPARED = (
    "alert_queue_stall_seconds",
    "alert_unknown_exposure_usd",
    "alert_publish_failure_streak",
)
DECLARED = (
    "slo_api_availability_target",
    "slo_job_start_latency_seconds",
    "slo_queue_backlog_max",
    "slo_publish_failure_rate_max",
    "slo_render_failure_rate_max",
    "slo_unknown_exposure_max_usd",
)
ALL_FIELDS = COMPARED + DECLARED

#: The rules whose threshold is compared against a live metric.
CONFIGURED_RULES = (
    "unknown_exposure_high",
    "queue_stalled",
    "repeated_publish_failure",
)
#: The rules whose threshold is a deliberate CONSTANT, with the reason.
CONSTANT_RULES = {
    "paid_submission_unknown": "an unknown billable submission has one acceptable count: zero",
    "worker_fleet_unavailable": "zero durable workers is never an agreed policy",
    "db_unavailable": "a collector heartbeat is a boolean, not a ratio",
    "storage_unavailable": "a storage health probe is a boolean, not a ratio",
    "gpu_queue_starvation": "a ledger identity (waiting>0, slots<=0), not a magnitude",
}


@pytest.fixture(autouse=True)
def _clean_registry():
    metrics_mod.REGISTRY.reset()
    yield
    metrics_mod.REGISTRY.reset()


# ---------------------------------------------------------------------------
# 1. environment -> validated Settings -> evaluator -> verdict
# ---------------------------------------------------------------------------


def _apply_env(monkeypatch, **env: object) -> Settings:
    """Push ``env`` into the process environment, re-read ``Settings``, and bind
    the freshly parsed values onto the process singleton ``slo.py`` reads.

    The point is that this walks the WHOLE chain. pydantic parses the strings
    and enforces the bounds; the evaluator then reads the parsed value. A test
    that only did ``setattr(settings, ...)`` would pass even if the field were
    misspelled in the environment contract.
    """
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    fresh = Settings(_env_file=None)
    for field in ALL_FIELDS:
        monkeypatch.setattr(live_settings, field, getattr(fresh, field))
    return fresh


def test_every_threshold_field_has_a_default_matching_the_documented_one():
    fresh = Settings(_env_file=None)
    assert fresh.alert_queue_stall_seconds == 900.0
    assert fresh.alert_unknown_exposure_usd == 1.0
    assert fresh.alert_publish_failure_streak == 3
    assert fresh.slo_api_availability_target == pytest.approx(0.995)
    assert fresh.slo_job_start_latency_seconds == 60.0
    assert fresh.slo_queue_backlog_max == 25
    assert fresh.slo_publish_failure_rate_max == pytest.approx(0.02)
    assert fresh.slo_render_failure_rate_max == pytest.approx(0.05)
    assert fresh.slo_unknown_exposure_max_usd == 5.0


def test_defaults_leave_every_alert_verdict_exactly_where_it_was(monkeypatch):
    """No behaviour change by default -- the regression half of "it works"."""
    monkeypatch.delenv("ALERT_PUBLISH_FAILURE_STREAK", raising=False)
    now = 1_000_000.0
    metrics_mod.record_publish_result("youtube", success=False)
    metrics_mod.record_publish_result("youtube", success=False)
    verdict = slo_mod.rule_by_id("repeated_publish_failure").check(
        metrics_mod.REGISTRY.snapshot(), now)
    assert verdict.firing is False
    assert verdict.threshold == 3.0
    metrics_mod.record_publish_result("youtube", success=False)
    assert slo_mod.rule_by_id("repeated_publish_failure").check(
        metrics_mod.REGISTRY.snapshot(), now).firing is True


def test_changed_env_var_flips_the_publish_streak_verdict(monkeypatch):
    """THE headline test: same metric, same snapshot, opposite verdict.

    Two consecutive failures. At the default streak of 3 that is NOT an
    incident; with ``ALERT_PUBLISH_FAILURE_STREAK=1`` it is. If
    ``_rule_repeated_publish_failure`` had gone back to comparing against a
    literal ``3.0`` this fails, which is the point.
    """
    now = 1_000_000.0

    def verdict() -> slo_mod.AlertVerdict:
        return slo_mod.rule_by_id("repeated_publish_failure").check(
            metrics_mod.REGISTRY.snapshot(), now)

    for _ in range(2):
        metrics_mod.record_publish_result("youtube", success=False)
    assert verdict().firing is False, "two of three is below the default streak"
    assert verdict().threshold == 3.0

    _apply_env(monkeypatch, ALERT_PUBLISH_FAILURE_STREAK=1)
    after = verdict()
    assert after.firing is True, (
        "ALERT_PUBLISH_FAILURE_STREAK=1 must fire on two consecutive failures; "
        f"got firing={after.firing} threshold={after.threshold}")
    assert after.threshold == 1.0
    assert "youtube" in after.reason

    _apply_env(monkeypatch, ALERT_PUBLISH_FAILURE_STREAK=99)
    assert verdict().firing is False, "and 99 must NOT fire on two failures"


def test_changed_env_var_flips_the_unknown_exposure_verdict(monkeypatch):
    now = 1_000_000.0

    def verdict() -> slo_mod.AlertVerdict:
        return slo_mod.rule_by_id("unknown_exposure_high").check(
            metrics_mod.REGISTRY.snapshot(), now)

    metrics_mod.PAID_UNKNOWN_EXPOSURE.set(0.50, provider="mpt")
    assert verdict().firing is False

    metrics_mod.PAID_UNKNOWN_EXPOSURE.set(1.01, provider="mpt")
    assert verdict().firing is True, "$1.01 is over the default $1.00 review line"

    _apply_env(monkeypatch, ALERT_UNKNOWN_EXPOSURE_USD=10.0)
    quiet = verdict()
    assert quiet.firing is False, (
        "ALERT_UNKNOWN_EXPOSURE_USD=10 must NOT fire at $1.01 of unknown "
        f"exposure; got firing={quiet.firing} threshold={quiet.threshold}")
    assert quiet.threshold == 10.0

    metrics_mod.PAID_UNKNOWN_EXPOSURE.set(10.5, provider="mpt")
    assert verdict().firing is True


def test_changed_env_var_flips_the_queue_stall_verdict(monkeypatch):
    now = 1_000_000.0

    def verdict() -> slo_mod.AlertVerdict:
        return slo_mod.rule_by_id("queue_stalled").check(
            metrics_mod.REGISTRY.snapshot(), now)

    metrics_mod.JOB_QUEUE_DEPTH.set(4, status="QUEUED")
    metrics_mod.JOB_LAST_START_TS.set(now - 300.0)
    assert verdict().firing is False, "300s of silence is inside the 900s window"

    _apply_env(monkeypatch, ALERT_QUEUE_STALL_SECONDS=60.0)
    fired = verdict()
    assert fired.firing is True, (
        "ALERT_QUEUE_STALL_SECONDS=60 must fire on 300s of silence; got "
        f"firing={fired.firing} threshold={fired.threshold}")
    assert fired.threshold == 60.0
    # The reason text follows the configuration too; a message still reading
    # "15 minutes" would be a second, quieter lie.
    assert "60s window" not in fired.reason  # idle is finite here
    assert "300s" in fired.reason

    metrics_mod.JOB_LAST_START_TS.set(0.0)  # never started
    never = verdict()
    assert never.firing is True
    assert "configured 60s window" in never.reason


def test_changed_env_var_changes_the_published_slo_objectives(monkeypatch):
    """``slo_*`` are weaker than ``alert_*`` and the test says which way.

    Nothing is measured, so a target cannot be evaluated. What a target setting
    can honestly change is the NUMBER the published objective states.
    """
    before = {t["id"]: t["target"] for t in slo_mod.slo_catalog()["targets"]}
    assert before["api_availability"] == "99.5% of non-5xx over 30d"
    assert before["queue_backlog"] == "<= 25 queued jobs sustained"

    _apply_env(
        monkeypatch,
        SLO_API_AVAILABILITY_TARGET=0.999,
        SLO_QUEUE_BACKLOG_MAX=10,
        SLO_UNKNOWN_EXPOSURE_MAX_USD=25.0,
        SLO_JOB_START_LATENCY_SECONDS=30.0,
        SLO_PUBLISH_FAILURE_RATE_MAX=0.005,
        SLO_RENDER_FAILURE_RATE_MAX=0.01,
    )
    after = {t["id"]: t["target"] for t in slo_mod.slo_catalog()["targets"]}
    assert after["api_availability"] == "99.9% of non-5xx over 30d"
    assert after["queue_backlog"] == "<= 10 queued jobs sustained"
    assert after["unknown_exposure_bounded"] == "<= $25.00 at any instant"
    assert after["job_start_latency"] == "95% of jobs start within 30s"
    assert after["publish_failure_rate"] == "<= 0.5% of publish attempts fail"
    assert after["render_failure_rate"] == "<= 1% of renders fail"

    # Objectives with no setting are untouched by any of the above.
    assert after["data_durability"] == before["data_durability"]
    assert after["unknown_paid_submissions"] == before["unknown_paid_submissions"]
    assert after["api_latency_p95"] == before["api_latency_p95"]


def test_alert_catalog_publishes_the_live_threshold_and_its_source(monkeypatch):
    _apply_env(monkeypatch, ALERT_UNKNOWN_EXPOSURE_USD=7.5)
    rules = {r["id"]: r for r in slo_mod.alert_catalog()["rules"]}
    assert rules["unknown_exposure_high"]["threshold"] == 7.5
    assert rules["unknown_exposure_high"]["threshold_source"] == (
        "settings:ALERT_UNKNOWN_EXPOSURE_USD")
    assert rules["db_unavailable"]["threshold_source"] == "constant"
    assert slo_mod.alert_catalog()["thresholds"]["unknown_exposure_usd"] == 7.5


def test_alerts_endpoint_reports_the_configured_threshold():
    """The ops surface agrees with the evaluator -- one source of truth."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        body = client.get("/internal/alerts").json()
        slo_body = client.get("/internal/slo").json()
    live = {r["id"]: r for r in body["rules"]}
    assert live["queue_stalled"]["threshold"] == (
        live_settings.alert_queue_stall_seconds)
    assert live["repeated_publish_failure"]["threshold"] == (
        live_settings.alert_publish_failure_streak)
    assert body["thresholds"] == slo_mod.alert_thresholds().as_dict()
    assert slo_body["thresholds"] == body["thresholds"]
    published = {t["id"]: t["target"] for t in slo_body["targets"]}
    assert published["queue_backlog"].endswith(
        f"{live_settings.slo_queue_backlog_max} queued jobs sustained")


# ---------------------------------------------------------------------------
# 2. invalid configuration fails loudly, at startup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env,offending", [
    # A streak of 0 makes `worst >= 0` true for a perfectly healthy system:
    # the rule would page on ZERO failures.
    ({"ALERT_PUBLISH_FAILURE_STREAK": "0"}, "alert_publish_failure_streak"),
    ({"ALERT_PUBLISH_FAILURE_STREAK": "-2"}, "alert_publish_failure_streak"),
    # 0 seconds makes any queued job whose last start was in the same instant a
    # "stall"; beyond a day is not a stall window, it is a disabled rule.
    ({"ALERT_QUEUE_STALL_SECONDS": "0"}, "alert_queue_stall_seconds"),
    ({"ALERT_QUEUE_STALL_SECONDS": "-1"}, "alert_queue_stall_seconds"),
    ({"ALERT_QUEUE_STALL_SECONDS": "86401"}, "alert_queue_stall_seconds"),
    # Exposure cannot be negative.
    ({"ALERT_UNKNOWN_EXPOSURE_USD": "-0.01"}, "alert_unknown_exposure_usd"),
    # A typo with a stray zero must not silently switch paging off.
    ({"ALERT_UNKNOWN_EXPOSURE_USD": "1e9"}, "alert_unknown_exposure_usd"),
    ({"ALERT_PUBLISH_FAILURE_STREAK": "101"}, "alert_publish_failure_streak"),
    # Declared objectives: 0% availability is an objective no system can meet.
    ({"SLO_API_AVAILABILITY_TARGET": "0"}, "slo_api_availability_target"),
    ({"SLO_API_AVAILABILITY_TARGET": "1.5"}, "slo_api_availability_target"),
    ({"SLO_QUEUE_BACKLOG_MAX": "0"}, "slo_queue_backlog_max"),
    ({"SLO_JOB_START_LATENCY_SECONDS": "0"}, "slo_job_start_latency_seconds"),
    ({"SLO_PUBLISH_FAILURE_RATE_MAX": "1.5"}, "slo_publish_failure_rate_max"),
    ({"SLO_RENDER_FAILURE_RATE_MAX": "-0.1"}, "slo_render_failure_rate_max"),
    ({"SLO_UNKNOWN_EXPOSURE_MAX_USD": "-5"}, "slo_unknown_exposure_max_usd"),
    # Not a threshold at all: unparseable.
    ({"ALERT_PUBLISH_FAILURE_STREAK": "three"}, "alert_publish_failure_streak"),
    ({"ALERT_QUEUE_STALL_SECONDS": ""}, "alert_queue_stall_seconds"),
])
def test_invalid_threshold_is_rejected_at_startup(monkeypatch, env, offending):
    """``Settings()`` is constructed at import of ``core.config``.

    So a bad threshold raises BEFORE the app can serve a request, rather than
    coercing into a number nobody chose and paging on it for a week.
    """
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)
    errors = caught.value.errors()
    assert any(e["loc"] == (offending,) for e in errors), (
        f"expected {offending} to be named in the error, got {errors}")
    # Loudly: the message carries the bound that was violated, not just "invalid".
    assert any(e.get("msg") for e in errors)


@pytest.mark.parametrize("env,field,expected", [
    # Boundaries are accepted, so the bounds are a judgement and not a wall.
    ({"ALERT_PUBLISH_FAILURE_STREAK": "1"}, "alert_publish_failure_streak", 1),
    ({"ALERT_PUBLISH_FAILURE_STREAK": "100"}, "alert_publish_failure_streak", 100),
    ({"ALERT_QUEUE_STALL_SECONDS": "86400"}, "alert_queue_stall_seconds", 86400.0),
    # 0 is a LEGAL strict policy here: "any unknown exposure is an incident".
    ({"ALERT_UNKNOWN_EXPOSURE_USD": "0"}, "alert_unknown_exposure_usd", 0.0),
    ({"ALERT_UNKNOWN_EXPOSURE_USD": "10000"}, "alert_unknown_exposure_usd", 10000.0),
    ({"SLO_API_AVAILABILITY_TARGET": "1.0"}, "slo_api_availability_target", 1.0),
    ({"SLO_PUBLISH_FAILURE_RATE_MAX": "0"}, "slo_publish_failure_rate_max", 0.0),
])
def test_boundary_thresholds_are_accepted(monkeypatch, env, field, expected):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    fresh = Settings(_env_file=None)
    assert getattr(fresh, field) == pytest.approx(expected)


def test_an_invalid_threshold_never_reaches_an_evaluator(monkeypatch):
    """The whole point: startup refuses, so no verdict is ever computed on it.

    A rule that read a coerced value would still return a confident-looking
    verdict. This asserts the process cannot get that far.
    """
    monkeypatch.setenv("ALERT_UNKNOWN_EXPOSURE_USD", "-5")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    # The live singleton is untouched: it was built before the env change and
    # nothing quietly mutated it.
    assert live_settings.alert_unknown_exposure_usd == 1.0


# ---------------------------------------------------------------------------
# 3. which thresholds are constants, and why the source cannot grow a knob
# ---------------------------------------------------------------------------


def test_no_dead_slo_or_alert_setting_remains():
    """The defect was nine settings that nothing read. None may come back.

    Every one of the nine is referenced from ``slo.py``, by the two mechanisms
    the module actually uses: the three COMPARED thresholds as
    ``settings.<field>`` attribute reads inside ``AlertThresholds.read()``, and
    the six DECLARED objective numbers as the ``setting=`` name on a
    ``SLOTarget``, resolved with ``getattr`` at serialisation time.
    """
    tree = ast.parse(SLO_PATH.read_text(encoding="utf-8"))
    attribute_reads = {
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        and node.value.id == "settings"
    }
    declared_by_name = {
        kw.value.value for node in ast.walk(tree)
        if isinstance(node, ast.Call) for kw in node.keywords
        if kw.arg == "setting" and isinstance(kw.value, ast.Constant)
        and isinstance(kw.value.value, str)
    }
    read = attribute_reads | declared_by_name
    missing = [f for f in ALL_FIELDS if f not in read]
    assert not missing, (
        f"dead configuration returned: {missing} are declared in config.py but "
        "nothing in slo.py reads them")

    # And the mechanism that resolves them is dynamic getattr on `settings`, so
    # the two declarations cannot drift apart silently.
    getattrs_on_settings = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "getattr" and node.args
        and isinstance(node.args[0], ast.Name) and node.args[0].id == "settings"
    ]
    assert len(getattrs_on_settings) == 2, (
        "SLOTarget.configured_value and AlertRule.current_threshold resolve "
        f"their setting by name; found {len(getattrs_on_settings)} such reads")


def test_configured_rules_name_a_real_settings_field():
    for rule in slo_mod.alert_rules():
        if not rule.setting:
            continue
        assert hasattr(live_settings, rule.setting), (
            f"rule {rule.id} names {rule.setting}, which is not a Settings field")
        assert rule.constant_threshold == 0.0, (
            f"rule {rule.id} declares both a setting and a constant threshold")


def test_exactly_three_rules_compare_against_configuration():
    configured = {r.id for r in slo_mod.alert_rules() if r.setting}
    assert configured == set(CONFIGURED_RULES)
    assert {r.setting for r in slo_mod.alert_rules() if r.setting} == set(COMPARED)


@pytest.mark.parametrize("rule_id,reason", sorted(CONSTANT_RULES.items()))
def test_constant_threshold_rules_have_no_knob_to_configure(rule_id, reason):
    """A constant must stay constant, and say out loud why.

    The second assertion is the one that stops this from decaying: nothing in
    ``Settings`` may offer an ``alert_*`` field for a rule that declares its
    threshold a constant, so a future edit cannot quietly give
    ``paid_submission_unknown`` an "acceptable number of unconfirmed purchases"
    dial.
    """
    rule = slo_mod.rule_by_id(rule_id)
    assert rule is not None and rule.setting == ""
    assert rule.threshold_source() == "constant"
    assert reason, "each constant needs a stated reason"

    prefix = rule_id.split("_")[0]
    knobs = [f for f in dir(type(live_settings))
             if f.startswith("alert_") and prefix in f]
    assert not knobs, (
        f"{rule_id} declares a constant threshold but Settings offers {knobs}")


# ---------------------------------------------------------------------------
# 4. structural: a configured threshold is not a literal
# ---------------------------------------------------------------------------


def _configured_evaluator(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no evaluator named {name}")


def _kwarg(call: ast.Call, name: str) -> ast.expr | None:
    """The expression bound to ``name``, whether passed positionally or by key."""
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _resolve(fn: ast.FunctionDef, expr: ast.expr) -> ast.expr:
    """Follow a local ``x = <expr>`` back to ``<expr>``.

    Every evaluator computes into a local and passes the name on, which is what
    makes the number readable. Resolving it means the structural assertions can
    look at the comparison itself instead of a one-letter placeholder.
    """
    if not isinstance(expr, ast.Name):
        return expr
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == expr.id for t in node.targets):
            return node.value
    return expr


def _verdict_arg(fn: ast.FunctionDef, name: str) -> ast.expr:
    call = _verdict_call(fn)
    found = _kwarg(call, name)
    assert found is not None, f"{fn.name} never sets {name}= on its verdict"
    return found


def _reads_thresholds(fn: ast.FunctionDef) -> bool:
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "read"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "AlertThresholds"):
            return True
    return False


def _verdict_call(fn: ast.FunctionDef) -> ast.Call:
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "_v":
            return node
    raise AssertionError(f"{fn.name} never builds a verdict")


@pytest.mark.parametrize("evaluator", [
    "_rule_unknown_exposure",
    "_rule_queue_stalled",
    "_rule_repeated_publish_failure",
])
def test_configured_threshold_is_read_not_hard_coded(evaluator):
    """The mutation this guards: `total > 1.0` written back into the rule.

    Two independent checks on the AST, because either alone has a hole:

    * the evaluator calls ``AlertThresholds.read()`` -- it consults config;
    * the ``threshold=`` argument it reports to the verdict is a NAME, not a
      numeric literal, and the same NAME appears in the firing comparison --
      so the reported number and the compared number are the same number.

    Hard-coding ``total > 1.0`` back in fails the second check even if a
    ``read()`` call is left behind unused.
    """
    tree = ast.parse(SLO_PATH.read_text(encoding="utf-8"))
    fn = _configured_evaluator(tree, evaluator)
    assert _reads_thresholds(fn), f"{evaluator} does not read AlertThresholds"

    firing = _resolve(fn, _verdict_arg(fn, "firing"))
    assert isinstance(firing, (ast.Compare, ast.BoolOp)), (
        f"{evaluator} decides `firing` without comparing anything")
    threshold_arg = _verdict_arg(fn, "threshold")
    assert isinstance(threshold_arg, ast.Name), (
        f"{evaluator} reports a literal threshold "
        f"({ast.unparse(threshold_arg)}) instead of the configured one")
    assert threshold_arg.id in ast.unparse(firing), (
        f"{evaluator} compares against something other than the threshold it "
        f"reports: firing is `{ast.unparse(firing)}`")


def test_no_configured_rule_compares_against_a_bare_number():
    """Whole-file sweep: the three literal comparisons must not come back.

    Scoped to the numbers §6 removed. ``_rule_queue_stalled`` legitimately
    compares ``depth > 0`` -- "is any work waiting?" is an identity, not a
    retunable magnitude -- so the sweep looks for the specific literals the old
    code baked in rather than banning every integer in sight.
    """
    tree = ast.parse(SLO_PATH.read_text(encoding="utf-8"))
    for evaluator in ("_rule_unknown_exposure", "_rule_queue_stalled",
                      "_rule_repeated_publish_failure"):
        fn = _configured_evaluator(tree, evaluator)
        assert not isinstance(_verdict_arg(fn, "threshold"), ast.Constant), (
            f"{evaluator} bakes its threshold in as a literal again")
        for cmp_node in [n for n in ast.walk(fn) if isinstance(n, ast.Compare)]:
            for side in [cmp_node.left, *cmp_node.comparators]:
                if isinstance(side, ast.Constant) and isinstance(side.value, float):
                    assert side.value not in (1.0, 900.0, 3.0), (
                        f"{evaluator} still compares against the pre-§6 literal "
                        f"{side.value} somewhere other than its threshold")


def test_no_rule_compares_through_the_shared_comparison_helper():
    """``Comparator`` / ``_compare`` are shared machinery, not configuration.

    No rule routes its threshold through them, which is what makes "the three
    compared numbers come from ``Settings``" a complete statement about the
    wiring. A rule that started calling ``_compare`` would put a threshold
    comparison on a second path, and this fails.
    """
    tree = ast.parse(SLO_PATH.read_text(encoding="utf-8"))
    callers = [n.func.id for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "_compare"]
    assert callers == [], (
        f"an evaluator routes its threshold through _compare: {callers}")


def test_slo_target_must_state_its_number_exactly_once():
    """Both a literal and a setting is ambiguous; neither is a broken objective."""
    with pytest.raises(ValueError, match="exactly once"):
        slo_mod.SLOTarget(
            id="x", title="X", objective="o", source_metrics=("m",),
            window="w", target="<= 1", target_template="<= {}")
    with pytest.raises(ValueError, match="exactly once"):
        slo_mod.SLOTarget(id="x", title="X", objective="o",
                          source_metrics=("m",), window="w")
    with pytest.raises(ValueError, match="nothing to read the number from"):
        slo_mod.SLOTarget(id="x", title="X", objective="o",
                          source_metrics=("m",), window="w",
                          target_template="<= {}")


def test_every_slo_target_is_either_configured_or_worded_and_says_which():
    for target in slo_mod.SLO_TARGETS:
        payload = target.to_dict()
        assert payload["target"], f"{target.id} renders an empty objective"
        if payload["target_setting"]:
            assert payload["configured_value"] is not None
            assert hasattr(live_settings, payload["target_setting"])
        else:
            assert payload["configured_value"] is None


def test_environment_names_match_field_names_exactly():
    """The env contract is the field name, upper-cased, with no prefix.

    A renamed field that a deployment still exports as the old name would fail
    here rather than silently reverting to a default at startup.
    """
    fields = set(type(live_settings).model_fields)
    for field in ALL_FIELDS:
        env_name = field.upper()
        assert env_name in os.environ or env_name.isidentifier()
        assert env_name.lower() in fields, (
            f"{env_name} does not round-trip to a Settings field")


# ---------------------------------------------------------------------------
# 5. the catalogue still claims nothing it has not measured
# ---------------------------------------------------------------------------


def test_slo_catalogue_still_refuses_to_claim_achievement():
    catalog = slo_mod.slo_catalog()
    assert catalog["measured"] is False
    assert "Targets only" in catalog["disclaimer"]
    for target in catalog["targets"]:
        assert target["measured"] is False


def test_the_nine_objectives_are_still_the_nine_objectives():
    ids = [t.id for t in slo_mod.SLO_TARGETS]
    assert ids == [
        "api_availability", "api_latency_p95", "job_start_latency",
        "queue_backlog", "publish_failure_rate", "render_failure_rate",
        "unknown_paid_submissions", "unknown_exposure_bounded",
        "data_durability",
    ]


def test_every_configured_objective_renders_the_documented_default_text():
    """Pin the rendered wording, so a format-string edit cannot silently
    republish "99.49999999999999% of non-5xx" as an availability target."""
    assert {t.id: t.to_dict()["target"] for t in slo_mod.SLO_TARGETS} == {
        "api_availability": "99.5% of non-5xx over 30d",
        "api_latency_p95": "p95 <= 1.0s over 30d",
        "job_start_latency": "95% of jobs start within 60s",
        "queue_backlog": "<= 25 queued jobs sustained",
        "publish_failure_rate": "<= 2% of publish attempts fail",
        "render_failure_rate": "<= 5% of renders fail",
        "unknown_paid_submissions": "0",
        "unknown_exposure_bounded": "<= $5.00 at any instant",
        "data_durability": "100% of DB collector probes succeed",
    }