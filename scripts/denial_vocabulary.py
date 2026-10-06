"""Enumerate the 401/403 `detail` strings the backend ACTUALLY emits.

The frontend cannot classify a permission denial without knowing this vocabulary,
and guessing it is how `"not a workspace member"` ended up unhandled. This prints
the real set, so `isPermissionDenial` is written against evidence.
"""

import collections
import pathlib
import re

PAT = re.compile(
    r"HTTPException\(\s*status_code\s*=\s*(?:status\.)?(?:HTTP_)?(401|403)[^)]*?"
    r"detail\s*=\s*f?[\"']([^\"'{][^\"']*)[\"']",
    re.S,
)

counts: collections.Counter = collections.Counter()
sites: dict[tuple[str, str], list[str]] = collections.defaultdict(list)

for path in sorted(pathlib.Path("app").rglob("*.py")):
    text = path.read_text(encoding="utf-8", errors="replace")
    for m in PAT.finditer(text):
        key = (m.group(1), m.group(2).strip())
        counts[key] += 1
        line = text.count("\n", 0, m.start()) + 1
        sites[key].append(f"{path.as_posix()}:{line}")

print(f"{'status':>6}  {'n':>3}  detail")
print("-" * 78)
for (status, detail), n in sorted(counts.items(), key=lambda x: (x[0][0], -x[1])):
    print(f"{status:>6}  x{n:<3} {detail}")
    for s in sites[(status, detail)][:2]:
        print(f"{'':>6}        {s}")

print("\n--- distinct details per status ---")
for status in ("401", "403"):
    vals = sorted({d for (s, d) in counts if s == status})
    print(f"{status}: {vals}")
