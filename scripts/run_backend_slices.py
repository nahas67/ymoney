"""Portable backend slices with full logs, JUnit, totals and nonzero failure exit.

Run using the freshly synced backend Python. Positional arguments retain the
old interface: number of slices, optionally one zero-based slice index.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from release_support import REPO, clean_env, junit_counts, run_logged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slices", type=int, nargs="?", default=8)
    parser.add_argument("only", type=int, nargs="?")
    parser.add_argument("--output", type=Path,
                        default=Path(tempfile.gettempdir()) / "ymoney-release-backend")
    parser.add_argument("--marker", default="not live and not slow")
    args = parser.parse_args()
    files = sorted((REPO / "backend/tests").rglob("test_*.py"))
    if not 1 <= args.slices <= len(files):
        parser.error("slices must be between 1 and the number of test files")
    if args.only is not None and not 0 <= args.only < args.slices:
        parser.error("only must be a zero-based slice index")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    failed = False
    for index in range(args.slices):
        if args.only is not None and index != args.only:
            continue
        selected = files[index::args.slices]
        xml = args.output / f"slice-{index}.xml"
        xml.unlink(missing_ok=True)
        code = run_logged([sys.executable, str(REPO / "scripts/release_pytest.py"), *map(str, selected),
                           "-m", args.marker, "-ra", "--tb=long", "--junitxml", str(xml)],
                          cwd=REPO / "backend", env=clean_env(),
                          log=args.output / f"slice-{index}.log")
        result = {"slice": index, "exit_code": code,
                  "files": [p.relative_to(REPO).as_posix() for p in selected]}
        try:
            counts = junit_counts(xml, qualify=False)
            result["counts"] = counts
            failed |= not counts["tests"] or bool(counts["failures"] or counts["errors"])
        except (OSError, RuntimeError, ValueError, ET.ParseError) as exc:
            result["report_error"] = str(exc)
            failed = True
        failed |= code != 0
        results.append(result)
        totals = {key: sum(r.get("counts", {}).get(key, 0) for r in results)
                  for key in ("tests", "failures", "errors", "skipped")}
        (args.output / "summary.json").write_text(
            json.dumps({"slices_requested": args.slices, "partial": args.only is not None,
                        "failed": bool(failed), "totals": totals,
                        "results": results}, indent=2) + "\n", encoding="utf-8")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
