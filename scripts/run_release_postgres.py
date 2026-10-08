"""Provision a NEW ephemeral local PostgreSQL container; prove then delete it.

No DSN argument is accepted. A random port/password and no mounted volume make
it impossible for this gate to connect to the developer's database by accident.
"""
from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path

from release_support import REPO, clean_env, junit_counts, run_logged

PG_TESTS = [
    "tests/test_work16_postgres_semantics.py",
    "tests/test_work16_1_postgres_lane.py",
    "tests/test_work16_1_schema_parity.py",
    "tests/test_work16_1_json.py",
    "tests/test_work16_1_queue.py",
    "tests/test_work16_1_reconciliation.py",
    "tests/test_work16_1_gpu_admission.py",
    "tests/test_work16_distributed_jobs.py::test_0035_round_trips_on_postgresql",
    "tests/test_work16_distributed_jobs.py::test_concurrent_claims_on_postgresql_are_exactly_once",
    "tests/test_work16_distributed_jobs.py::test_pg_connectivity_probe",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # The shipped set is derived from the source tree, never a magic number: a
    # hardcoded count rots on the next migration and fails a correct tree (it
    # once claimed 39 while the tree ships 38 digit-prefixed modules plus
    # __init__.py, which the glob below correctly excludes).
    actual = sorted(p.name for p in (REPO / "backend/app/migrations/versions").glob("[0-9]*.py"))
    preflight = {"migration_modules": actual, "module_count": len(actual),
                 "qualified": len(actual) > 0}
    (output / "preflight.json").write_text(json.dumps(preflight, indent=2) + "\n")
    if not actual:
        raise RuntimeError("no migration modules found; "
                           "no PostgreSQL container or database was created")
    suffix = secrets.token_hex(8)
    container = f"ymoney-release-{suffix}"
    database = f"ymoney_release_{suffix}"
    password = secrets.token_urlsafe(48)
    env = clean_env()
    env["POSTGRES_PASSWORD"] = password
    env["SECRET_KEY"] = secrets.token_urlsafe(48)
    # Pass password by environment NAME, never argv/logs or committed YAML.
    try:
        code = run_logged(["docker", "run", "--detach", "--name", container,
                           "--publish", "127.0.0.1::5432", "--env", "POSTGRES_PASSWORD",
                           "--env", f"POSTGRES_DB={database}", "postgres:16.13"],
                          cwd=REPO, env=env, log=output / "provision.log", redact=(password,))
        if code:
            return code
        mapping = subprocess.check_output(["docker", "port", container, "5432/tcp"],
                                          env=env, text=True).strip()
        port = int(mapping.rsplit(":", 1)[1])
        dsn = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"
        import psycopg
        deadline = time.monotonic() + 90
        while True:
            try:
                with psycopg.connect(dsn, connect_timeout=2) as conn:
                    assert conn.execute("SELECT 1").fetchone()[0] == 1
                break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("disposable PostgreSQL did not become ready") from None
        env["DATABASE_URL"] = f"postgresql+psycopg://postgres:{password}@127.0.0.1:{port}/{database}"
        env["YMONEY_TEST_POSTGRES"] = dsn
        env["YMONEY_RELEASE_POSTGRES_GATE"] = "disposable-container"
        code = run_logged([sys.executable, str(REPO / "scripts/release_pg_proof.py"), str(output)],
                          cwd=REPO / "backend", env=env, log=output / "migration-startup.log",
                          redact=(password, env["SECRET_KEY"]))
        if code:
            return code
        xml = output / "postgres.xml"
        xml.unlink(missing_ok=True)
        code = run_logged([sys.executable, str(REPO / "scripts/release_pytest.py"), *PG_TESTS,
                           "-m", "not live and not slow", "-ra", "--tb=long", "--junitxml", str(xml)],
                          cwd=REPO / "backend", env=env, log=output / "postgres-tests.log",
                          redact=(password, env["SECRET_KEY"]))
        if code:
            return code
        counts = junit_counts(xml, no_skips=True)
        (output / "counts.json").write_text(json.dumps(counts, indent=2) + "\n")
        return 0
    finally:
        # This exact random name belongs only to this invocation. Never remove
        # someone else's container, enumerate databases, or mount host PG data.
        run_logged(["docker", "logs", container], cwd=REPO, env=env,
                   log=output / "postgres-server.log", redact=(password,))
        cleanup = run_logged(["docker", "rm", "--force", "--volumes", container],
                             cwd=REPO, env=env, log=output / "cleanup.log", redact=(password,))
        if cleanup:
            raise RuntimeError(f"disposable PostgreSQL cleanup failed: {container}")


if __name__ == "__main__":
    raise SystemExit(main())
