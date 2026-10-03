"""Real render measurements for docs/RENDER_BENCHMARKS.md (Work 16 §6).

Not a test -- a measurement harness. Run it:

    cd backend
    .venv\\Scripts\\python -m scripts.bench_render

Every number it prints was produced by the run that printed it. Synthetic
sources only (`ffmpeg -f lavfi testsrc`), so nothing here calls a paid
provider or spends money. Per-render CPU and peak RSS come from ffmpeg's own
`-benchmark` output, which is why they are attributed to ffmpeg and not to this
process: the Python side is a pipe.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def machine() -> dict:
    info = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_logical": None,
        "ffmpeg": None,
    }
    try:  # Windows / Linux; absent on some containers, and that is fine
        info["cpu_logical"] = len(__import__("os").sched_getaffinity(0))
    except (AttributeError, OSError):  # noqa: PERF203 - diagnostics only
        info["cpu_logical"] = None
    if shutil.which("ffmpeg"):
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True,
                             text=True, timeout=20).stdout or ""
        info["ffmpeg"] = out.splitlines()[0] if out else "unknown"
    try:
        name = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Processor).Name"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        if name:
            info["cpu_model"] = name
    except Exception:  # noqa: BLE001 - diagnostics only
        pass
    return info


def measure(tmp_root: Path, duration: int, **kw) -> dict:
    """Reuse the SAME code path the test suite exercises, so the document and
    the tests cannot drift apart."""
    from tests.test_work16_render_pipeline import measure_render

    return measure_render(tmp_root, duration=duration, **kw)


def main() -> int:
    tmp_root = Path(sys.argv[1] if len(sys.argv) > 1 else
                    Path("data/bench_render"))
    print(json.dumps({"machine": machine()}, indent=2))
    rows = []
    for label, duration in (("short", 20), ("5min", 300),
                            ("30min", 1800), ("60min", 3600)):
        print(f"--- rendering {label} ({duration}s) ---", flush=True)
        started = time.monotonic()
        row = measure(tmp_root, duration=duration)
        if row["render_returncode"] != 0:  # pragma: no cover - honesty gate
            print(f"!! {label} render FAILED rc={row['render_returncode']} "
                  f"after {row['render_attempts']} attempts; not reporting "
                  f"a number that was not produced", flush=True)
        row["label"] = label
        row["total_harness_seconds"] = round(time.monotonic() - started, 3)
        rows.append(row)
        print(json.dumps(row, indent=2), flush=True)
    print("\n=== SUMMARY ===")
    for row in rows:
        print(f"{row['label']:>6} | {row['duration_s']:>5}s media | "
              f"render wall {row['render_wall_s']:>8.3f}s | "
              f"cpu {row['render_cpu_s']:>7.3f}s | "
              f"maxrss {row['render_maxrss_mib']:>7.1f} MiB | "
              f"artifact {row['artifact_bytes'] / 1048576:>7.2f} MiB | "
              f"store {row['store_seconds']:>6.3f}s "
              f"({row['store_mb_per_s']} MB/s) | "
              f"realtime x{row['realtime_factor']}")
    print(f"\nJSON:\n{json.dumps(rows, indent=2)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())