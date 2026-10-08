"""Release harness contracts: failures cannot be promoted to release evidence."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from release_support import clean_env, junit_counts, run_logged
from gen_route_release_matrix import DIMENSIONS as ROUTE_MATRIX_DIMENSIONS, validate as validate_route_matrix


def test_release_env_excludes_provider_and_database_state(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-do-not-copy")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///developer.db")
    monkeypatch.setenv("YMONEY_TEST_POSTGRES", "postgresql://developer")
    monkeypatch.setenv("PATH", "synthetic-path")
    monkeypatch.setenv("PROGRAMFILES", "synthetic-system-applications")
    env = clean_env()
    assert env["PATH"] == "synthetic-path"
    assert env["PROGRAMFILES"] == "synthetic-system-applications"
    assert "OPENAI_API_KEY" not in env and "DATABASE_URL" not in env
    assert env["YMONEY_TEST_POSTGRES"] == ""
    # The gate must not diverge from dev config: forcing TELEGRAM_ENABLED=false
    # broke the mocked telegram notify/suppress tests, and the gate carries no
    # bot token so nothing can reach the network either way.
    assert "TELEGRAM_ENABLED" not in env


@pytest.mark.parametrize("body", ["", '<testcase name="t"><failure/></testcase>',
                                  '<testcase name="t"><error/></testcase>',
                                  '<testcase name="t"><skipped/></testcase>'])
def test_release_junit_refuses_empty_failed_or_skipped_pg_report(tmp_path, body):
    xml = tmp_path / "result.xml"
    xml.write_text(f"<testsuite>{body}</testsuite>")
    with pytest.raises(RuntimeError, match="unqualified"):
        junit_counts(xml, no_skips=True)


def test_release_junit_counts_tests_and_requires_actual_media_execution(tmp_path):
    xml = tmp_path / "result.xml"
    xml.write_text('<testsuite><testcase name="render[20]"/><testcase name="other"/></testsuite>')
    assert junit_counts(xml, required=("render",)) == {
        "tests": 2, "failures": 0, "errors": 0, "skipped": 0}
    with pytest.raises(RuntimeError, match="did not execute"):
        junit_counts(xml, required=("absent",))
    xml.write_text('<testsuite><testcase name="render[20]"><skipped/></testcase></testsuite>')
    with pytest.raises(RuntimeError, match="did not execute"):
        junit_counts(xml, required=("render",))


def test_release_process_preserves_stderr_full_output_and_failure(tmp_path):
    log = tmp_path / "failed.log"
    code = run_logged([sys.executable, "-c",
                       "import sys; print('synthetic-transient-password'); "
                       "print('full stderr detail', file=sys.stderr); sys.exit(7)"],
                      cwd=tmp_path, env=clean_env(), log=log,
                      redact=("synthetic-transient-password",))
    assert code == 7
    text = log.read_text()
    assert "full stderr detail" in text and "[REDACTED]" in text
    assert "synthetic-transient-password" not in text
    assert json.loads(log.with_suffix(".result.json").read_text())["exit_code"] == 7


def test_route_matrix_rejects_zero_endpoint_coverage():
    route = {"path": "/test"}
    for dimension in ROUTE_MATRIX_DIMENSIONS:
        route[dimension] = {"endpoints": 0, "declared": 0} if dimension == "contract" else True

    failures = validate_route_matrix([route], [{"path": "/test"}])

    assert any("`contract.endpoints` must be positive" in failure for failure in failures)


def test_slice_runner_propagates_failure_and_reports_partial_scope(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("release_slices_test", SCRIPTS / "run_backend_slices.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tests = tmp_path / "backend/tests"
    tests.mkdir(parents=True)
    (tests / "test_a.py").write_text("def test_a(): pass\n")
    (tests / "test_b.py").write_text("def test_b(): pass\n")
    output = tmp_path / "evidence"
    monkeypatch.setattr(module, "REPO", tmp_path)
    monkeypatch.setattr(sys, "argv", ["slices", "2", "0", "--output", str(output)])
    def fail(command, **kwargs):
        xml = Path(command[command.index("--junitxml") + 1])
        xml.write_text('<testsuite><testcase name="t"><failure>full error</failure></testcase></testsuite>')
        return 1
    monkeypatch.setattr(module, "run_logged", fail)
    assert module.main() == 1
    summary = json.loads((output / "summary.json").read_text())
    assert summary["failed"] is True and summary["partial"] is True
    assert len(summary["results"]) == 1
    assert summary["results"][0]["exit_code"] == 1
    assert summary["totals"] == {"tests": 1, "failures": 1, "errors": 0, "skipped": 0}


@pytest.mark.parametrize("argv", [["slices", "0"], ["slices", "1", "99"]])
def test_slice_runner_rejects_invalid_slice_selection(monkeypatch, argv):
    import run_backend_slices
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as error:
        run_backend_slices.main()
    assert error.value.code == 2


def test_generated_artifact_gate_checks_checkout_drift_before_repeat_determinism(
        tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "release_artifact_gate", SCRIPTS / "verify_generated_artifacts.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "REPO", tmp_path)
    marker = tmp_path / "frontend/node_modules/vitest/vitest.mjs"
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    calls = []

    def succeed(command, **kwargs):
        calls.append((command, kwargs))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(module.subprocess, "run", succeed)
    monkeypatch.setattr(sys, "argv", ["verify_generated_artifacts.py"])

    assert module.main() == 0
    assert len(calls) == 2
    assert all("--check" in command for command, _ in calls)
    assert all("--pipeline" in command for command, _ in calls)


def test_ci_receipt_binds_sha_totals_and_complete_file_hashes(tmp_path, monkeypatch):
    import write_release_receipt as receipts
    sha = "1" * 40
    def git(*args):
        return {("rev-parse", "HEAD"): sha, ("rev-parse", "HEAD^{tree}"): "2" * 40,
                ("status", "--porcelain", "--untracked-files=no"): ""}[args]
    monkeypatch.setattr(receipts, "git_value", git)
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-token-must-not-appear")
    monkeypatch.setenv("GITHUB_REPOSITORY", "synthetic-owner/synthetic-repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    (tmp_path / "full.log").write_text("complete synthetic test output\n")
    (tmp_path / "tests.xml").write_text('<testsuite><testcase name="a"/><testcase name="b"/></testsuite>')
    receipt, code = receipts.write_receipt(evidence=tmp_path, job="Backend fast tests",
                                           status="success", sha=sha, require_junit=True)
    assert code == 0 and receipt["checkout_matches_github_sha"] is True
    assert receipt["github_sha"] == sha and receipt["junit_totals"]["tests"] == 2
    assert receipt["job_display_name"] == "Backend fast tests"
    assert len(receipt["evidence_files"]) == 2
    assert receipt["evidence_files"][0]["sha256"] == receipts.file_sha256(tmp_path / "full.log")
    assert receipt["run_url"].endswith("/synthetic-owner/synthetic-repo/actions/runs/123")
    assert "synthetic-token-must-not-appear" not in (tmp_path / "receipt.json").read_text()
    # Re-running cannot accidentally hash the previous receipt into itself.
    second, code = receipts.write_receipt(evidence=tmp_path, job="Backend fast tests",
                                         status="success", sha=sha, require_junit=True)
    assert code == 0 and second == receipt


@pytest.mark.parametrize("status,sha,body,expected", [
    ("failure", "1" * 40, None, 0),
    ("success", "1" * 40, None, 1),
    ("success", "3" * 40, '<testcase name="a"/>', 1),
    ("success", "short-sha", '<testcase name="a"/>', 1),
    ("success", "1" * 40, '<testcase name="a"><failure/></testcase>', 1),
    ("success", "1" * 40, "malformed XML", 1),
])
def test_ci_receipt_survives_failure_but_rejects_false_success(tmp_path, monkeypatch,
                                                            status, sha, body, expected):
    import write_release_receipt as receipts
    monkeypatch.setattr(receipts, "git_value", lambda *args: "1" * 40 if args[:1] == ("rev-parse",) else "")
    if body is not None:
        (tmp_path / "tests.xml").write_text(f"<testsuite>{body}</testsuite>" if body.startswith("<") else body)
    receipt, code = receipts.write_receipt(evidence=tmp_path, job="Synthetic test gate",
                                           status=status, sha=sha, require_junit=True)
    assert code == expected
    assert (tmp_path / "receipt.json").exists()
    assert receipt["job_status_before_receipt"] == status
    assert bool(receipt["receipt_errors"]) == bool(expected)
