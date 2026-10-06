"""Run the backend suite in slices, so a single environment restart cannot lose
the whole result.

The full suite takes ~17 minutes, and this session's environment has restarted
three times mid-run. Each slice is a separate short-lived process whose result is
appended to a summary file, so completed work is never repeated.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "backend" / "tests"
SUMMARY = Path(r"C:\Users\nahas\AppData\Local\Temp\opencode\backend_slices.txt")
PY = REPO / "backend" / ".venv" / "Scripts" / "python.exe"

files = sorted(p for p in TESTS.glob("test_*.py"))
SLICE = int(sys.argv[1]) if len(sys.argv) > 1 else 8
slices = [files[i::SLICE] for i in range(SLICE)]

only = int(sys.argv[2]) if len(sys.argv) > 2 else None
targets = [(i, s) for i, s in enumerate(slices) if only is None or i == only]

print(f"{len(files)} test files across {SLICE} slices")
for i, s in targets:
    names = [str(p) for p in s]
    print(f"\n--- slice {i} ({len(names)} files) ---")
    r = subprocess.run(
        [str(PY), "-m", "pytest", *names, "-q", "-p", "no:cacheprovider", "--timeout=590"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    tail = [ln for ln in r.stdout.splitlines() if "passed" in ln or "failed" in ln]
    summary = tail[-1] if tail else f"exit={r.returncode}"
    print(f"    {summary}")
    with SUMMARY.open("a", encoding="utf-8") as fh:
        fh.write(f"slice {i}: {summary}\n")
    if r.returncode != 0:
        for ln in r.stdout.splitlines():
            if ln.startswith(("FAILED", "ERROR")):
                print(f"    {ln}")
                with SUMMARY.open("a", encoding="utf-8") as fh:
                    fh.write(f"    {ln}\n")