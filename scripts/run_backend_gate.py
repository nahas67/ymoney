"""Run the complete fast or offline slow/media lane with durable evidence."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from release_support import REPO, clean_env, junit_counts, run_logged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lane", choices=("fast", "slow"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    env = clean_env()
    for binary in ("ffmpeg", "ffprobe"):
        if not shutil.which(binary):
            raise RuntimeError(f"required media binary missing: {binary}")
        code = run_logged([binary, "-version"], cwd=REPO, env=env,
                          log=output / f"{binary}.log")
        if code:
            return code
    xml = output / f"{args.lane}.xml"
    xml.unlink(missing_ok=True)
    marker = "not live and not slow" if args.lane == "fast" else "slow and not live"
    code = run_logged([sys.executable, str(REPO / "scripts/release_pytest.py"), "tests",
                       "-m", marker, "-ra", "--tb=long", "--junitxml", str(xml)],
                      cwd=REPO / "backend", env=env, log=output / f"{args.lane}.log")
    if code:
        return code
    required = () if args.lane == "fast" else (
        "test_render_scaling_short_5min_30min_60min", "test_ffmpeg_pipe_is_streamed_not_buffered")
    counts = junit_counts(xml, required=required)
    (output / "counts.json").write_text(json.dumps(counts, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
