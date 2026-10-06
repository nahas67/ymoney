"""List the endpoints still lacking a generated contract, grouped by cause."""

import json
from collections import Counter
from pathlib import Path

REPORT = Path(__file__).resolve().parents[1] / "docs" / "UI_CONTRACT_GENERATION.json"

data = json.loads(REPORT.read_text(encoding="utf-8"))
skipped = data["skipped"]

print(f"generated: {data['generated']}   skipped: {len(skipped)}")
print(f"by status: {dict(Counter(s['status'] for s in skipped))}")
print()
for s in skipped:
    print(f"  {str(s['status']):5} {s['method']:6} {s['specPath']}")