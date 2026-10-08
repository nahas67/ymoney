"""Fail-loud release gate: regenerate every generated artifact twice, reject drift.

CI invokes exactly this file (see .github/workflows/ci.yml, `generated` job).
It is a thin, honest wrapper around the generator-lane pipeline owned by
scripts/gen_response_contracts.py --pipeline [--check], which regenerates all
seven release artifacts in order:

  backend/app/schemas/generated.py
  backend/app/schemas/contract_map.json
  docs/UI_CONTRACT_GENERATION.json
  frontend/src/api/openapi.json
  docs/UI_CONTRACT_AUDIT.json
  docs/UI_ROUTE_RELEASE_MATRIX.json
  docs/ANALYTICS_HONESTY_AUDIT.json

Each pass runs with --check, which snapshots the checked-in artifacts, runs the
full generator pipeline, and exits nonzero if any byte changed. The first pass
rejects stale checked-in outputs; the second proves the same clean outputs are
stable across a repeat. Any generator failure propagates as a nonzero exit;
nothing is skipped, and missing infrastructure (node_modules for the
contract-test steps) fails here instead of being silently tolerated.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    node_modules = REPO / "frontend" / "node_modules"
    if not (node_modules / "vitest" / "vitest.mjs").exists():
        print(
            "FAIL: frontend/node_modules is not installed; "
            "run `npm ci` in frontend/ before this gate "
            "(the pipeline executes the contract vitest suite).",
            file=sys.stderr,
        )
        return 1
    for pass_name in ("checkout drift", "repeat determinism"):
        extra = "--pipeline --check"
        command = [sys.executable, "scripts/gen_response_contracts.py", *extra.split()]
        print(f"RUN ({pass_name}): {' '.join(command)}", flush=True)
        result = subprocess.run(command, cwd=REPO, check=False)
        if result.returncode:
            print(
                f"FAIL: {' '.join(command)} exited {result.returncode}",
                file=sys.stderr,
            )
            return result.returncode
    print("PASS: checked-in outputs match and a repeat generation produced no diff")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
