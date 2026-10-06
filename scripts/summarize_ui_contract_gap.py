"""Summarise the UI contract audit by owning router area.

Work 16.5.3 §1: the closure work needs to be scheduled by area, and a single
number like "172 undeclared" is not actionable on its own.
"""

import collections
import json
from pathlib import Path

AUDIT = Path(__file__).resolve().parents[1] / "docs" / "UI_CONTRACT_AUDIT.json"

data = json.loads(AUDIT.read_text(encoding="utf-8"))

by_area = collections.defaultdict(lambda: collections.Counter())
for call in data["calls"]:
    if call["class"] != "UNDECLARED_JSON":
        continue
    segs = [s for s in call["specPath"].split("/") if s and not s.startswith("{")]
    area = segs[3] if len(segs) > 3 else "/".join(segs[2:])
    by_area[area][call["method"]] += 1

print(f"{'area':26} {'GET':>4} {'POST':>5} {'PUT':>4} {'PATCH':>6} {'DEL':>4} {'tot':>5}")
print("-" * 60)
totals = collections.Counter()
for area, counts in sorted(by_area.items(), key=lambda kv: -sum(kv[1].values())):
    total = sum(counts.values())
    totals.update(counts)
    print(
        f"  /{area:24} {counts['GET']:4} {counts['POST']:5} {counts['PUT']:4} "
        f"{counts['PATCH']:6} {counts['DELETE']:4} {total:5}"
    )
print("-" * 60)
print(
    f"  {'TOTAL':24} {totals['GET']:4} {totals['POST']:5} {totals['PUT']:4} "
    f"{totals['PATCH']:6} {totals['DELETE']:4} {sum(totals.values()):5}"
)