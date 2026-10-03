"""Ad-hoc contention probe: does the rollup hook widen the reservation window?

Not part of the suite. Run from backend/:  python -m pytest / this file.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="w16-probe-")
os.environ["DATABASE_URL"] = f"sqlite:///{(Path(_TMP) / 'p.db').as_posix()}"
os.environ["YMONEY_SECRET_KEY"] = "probe-secret-key-not-real-1234567890"

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.db import session_scope  # noqa: E402
from app.migrations.runner import run_migrations  # noqa: E402
from app.models import Workspace  # noqa: E402
from app.services import cost as cost_mod  # noqa: E402

with session_scope() as s:
    run_migrations(s)

with session_scope() as s:
    ws = Workspace(name="probe", slug="probe-ws", niche="x")
    s.add(ws)
    s.flush()
    ws.settings_json = {"safety": {"daily_budget_usd": 10 ** 9,
                                   "per_video_budget_usd": 10 ** 9}}
    s.commit()
    WS = ws.id

ROUNDS = 12
THREADS = 16


def main() -> None:
    locked = 0
    granted = 0
    started = time.perf_counter()
    for round_no in range(ROUNDS):
        def take(_i: int, tag: str = f"r{round_no}") -> str:
            try:
                r = cost_mod.reserve_spend(WS, 0.01, category=tag,
                                          provider="probe")
                return "ok"
            except cost_mod.BudgetExceededError:
                return "refused"
            except Exception as exc:  # noqa: BLE001
                return f"{type(exc).__name__}: {exc}"

        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            results = list(pool.map(take, range(THREADS)))
        for outcome in results:
            if outcome == "ok":
                granted += 1
            elif outcome.startswith("OperationalError"):
                locked += 1
    elapsed = time.perf_counter() - started
    print(f"rounds={ROUNDS} threads={THREADS} attempts={ROUNDS * THREADS} "
          f"granted={granted} LOCKED={locked} elapsed={elapsed:.1f}s")


if __name__ == "__main__":
    main()
