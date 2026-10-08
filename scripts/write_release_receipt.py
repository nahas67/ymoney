"""Bind one CI job's complete evidence to its actual checkout and GITHUB_SHA.

Standard library only: this must still run when uv/npm installation failed.
Receipts contain provenance and hashes, never environment dumps or log contents.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def git_value(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(REPO), *args], text=True,
                                   encoding="utf-8").strip()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_receipt(*, evidence: Path, job: str, status: str, sha: str,
                  require_junit: bool = False) -> tuple[dict, int]:
    evidence = evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    output = evidence / "receipt.json"
    errors = []
    checkout = git_value("rev-parse", "HEAD")
    tree = git_value("rev-parse", "HEAD^{tree}")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        errors.append("expected SHA must be the full 40-character GITHUB_SHA")
    elif checkout != sha:
        errors.append("checkout HEAD does not match GITHUB_SHA")

    files = []
    reports = []
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for path in sorted(evidence.rglob("*")):
        if not path.is_file() or path == output:
            continue
        relative = path.relative_to(evidence).as_posix()
        files.append({"path": relative, "bytes": path.stat().st_size,
                      "sha256": file_sha256(path)})
        if path.suffix != ".xml":
            continue
        try:
            cases = list(ET.parse(path).getroot().iter("testcase"))
            counts = {"tests": len(cases), "failures": 0, "errors": 0, "skipped": 0}
            for case in cases:
                for element, key in (("failure", "failures"), ("error", "errors"), ("skipped", "skipped")):
                    counts[key] += int(case.find(element) is not None)
            reports.append({"path": relative, **counts})
            for key in totals:
                totals[key] += counts[key]
        except (OSError, ET.ParseError) as exc:
            errors.append(f"cannot parse JUnit {relative}: {type(exc).__name__}")

    # Earlier failed steps must still get a receipt and upload. Only a job
    # claiming success is required to have successful, nonempty test evidence.
    if status == "success":
        if require_junit and not totals["tests"]:
            errors.append("successful test job has no executed JUnit testcases")
        if totals["failures"] or totals["errors"]:
            errors.append("successful job contains failed/error JUnit testcases")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_url = f"{server}/{repository}/actions/runs/{run_id}" if repository and run_id else None
    receipt = {
        "receipt_version": 1,
        "job_display_name": job,
        "job_id": os.environ.get("GITHUB_JOB"),
        "job_status_before_receipt": status,
        "github_sha": sha,
        "checkout_sha": checkout,
        "checkout_matches_github_sha": checkout == sha,
        "checkout_git_tree": tree,
        # This is a full Git tree, NOT the parent's separately defined code-tree
        # digest. Record tracked mutations honestly instead of hiding them.
        "tracked_changes_after_job": git_value("status", "--porcelain", "--untracked-files=no").splitlines(),
        "run_id": run_id or None,
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "workflow": os.environ.get("GITHUB_WORKFLOW"),
        "run_url": run_url,
        "junit_totals": totals,
        "junit_reports": reports,
        "evidence_files": files,
        "receipt_errors": errors,
    }
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt, int(bool(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--job", required=True, help="Human-readable check name")
    parser.add_argument("--status", required=True, choices=("success", "failure", "cancelled", "skipped"))
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--require-junit", action="store_true")
    args = parser.parse_args()
    receipt, code = write_receipt(evidence=args.evidence, job=args.job, status=args.status,
                                  sha=args.sha, require_junit=args.require_junit)
    print(json.dumps({"job": args.job, "github_sha": args.sha,
                      "junit_totals": receipt["junit_totals"],
                      "receipt_errors": receipt["receipt_errors"]}, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
