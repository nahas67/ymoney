"""Release-hygiene scan for Work 16.5.3 §4 (unsafe casts), §14 (dead code), §15 (design system).

Prints findings only. The assertions that actually enforce these live in
`frontend/src/test/release-hygiene.test.ts`, so a regression fails the suite
rather than scrolling past in CI output.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

FRONT = Path(__file__).resolve().parents[1] / "frontend" / "src"

sources = [
    p
    for p in list(FRONT.rglob("*.ts")) + list(FRONT.rglob("*.tsx"))
    if ".test." not in p.name
]

# ---------------------------------------------------------------- §4 unsafe casts
print("=" * 78)
print("§4  UNSAFE TRANSPORT CASTS")
print("=" * 78)

# `x as SomeResponseType` where the assertion sits on an API boundary is the
# habit this work order wants gone: it converts "unvalidated" into "believed".
CAST_PATTERNS = {
    "as-unknown-then-narrow": re.compile(r"\bas\s+unknown\s+as\b"),
    "as-any": re.compile(r"\bas\s+any\b"),
    "any-generic": re.compile(r"<any>|\bany\[\]"),
    "non-null-assert": re.compile(r"\w!(\.|\)|;|\s|$)"),
}

for label, pat in CAST_PATTERNS.items():
    hits: list[str] = []
    for f in sources:
        for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pat.search(line):
                hits.append(
                    f"{f.relative_to(FRONT)}:{n}: {line.strip()[:110]}"
                )
    print(f"\n  {label}: {len(hits)}")
    for h in hits[:14]:
        print(f"    {h}")
    if len(hits) > 14:
        print(f"    ... and {len(hits) - 14} more")

# ------------------------------------------------------------ §15 design system
print()
print("=" * 78)
print("§15  OFF-SYSTEM COLOURS IN FEATURES")
print("=" * 78)
# A hex literal or an rgb() inside a feature means a colour was chosen locally
# instead of coming from the token layer, which is how two screens end up
# disagreeing about what "warning" looks like.
# `(?<!&)` is load-bearing. Without it, an HTML numeric ENTITY in a doc comment
# -- `&#123;`, `&#125;` -- matches as a hex colour, and the audit reported five
# off-system colour literals that do not exist. Every hit was a brace in a
# comment (`features/campaigns/CampaignDetail.tsx`, `features/planner/
# CalendarView.tsx`, `features/planner/Planner.tsx`).
#
# The twin assertion in `frontend/src/test/release-hygiene.test.ts` already had
# the lookbehind, which is why the suite was green while this script -- the one
# that prints the number a report quotes -- was not.
COLOUR = re.compile(r"(?<!&)#[0-9a-fA-F]{3,8}\b|rgba?\(", re.I)
off_system: list[str] = []
for f in sources:
    rel = str(f.relative_to(FRONT)).replace("\\", "/")
    if not rel.startswith("features/"):
        continue
    for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if COLOUR.search(line):
            off_system.append(f"{rel}:{n}: {line.strip()[:100]}")

print(f"\n  off-system colour literals in features/: {len(off_system)}")
for h in off_system[:20]:
    print(f"    {h}")
if len(off_system) > 20:
    print(f"    ... and {len(off_system) - 20} more")

# ---------------------------------------------------------------- §14 dead code
print()
print("=" * 78)
print("§14  ORPHANED MODULES")
print("=" * 78)


def specifiers(text: str) -> list[str]:
    out = [m.group(1) for m in re.finditer(r'from\s+"([^"]+)"', text)]
    out += [m.group(1) for m in re.finditer(r'import\(\s*"([^"]+)"\s*\)', text)]
    return out


def resolve(spec: str, importer: Path):
    if not spec.startswith("."):
        return None
    target = (importer.parent / spec).resolve()
    for cand in (target.with_suffix(".tsx"), target.with_suffix(".ts"), target):
        if cand.exists() and cand.is_file():
            return cand
    return None


referenced = set()
for f in sources:
    for spec in specifiers(f.read_text(encoding="utf-8", errors="replace")):
        r = resolve(spec, f)
        if r:
            referenced.add(r.resolve())

ENTRYPOINTS = {"main.tsx", "vite-env.d.ts", "setup.ts"}
orphans = [
    p
    for p in sources
    if p.resolve() not in referenced
    and p.name not in ENTRYPOINTS
    and not p.name.endswith(".d.ts")
]
print(f"\n  orphaned modules: {len(orphans)}")
for p in sorted(orphans):
    print(f"    {str(p.relative_to(FRONT)).replace(chr(92), '/')}  ({round(p.stat().st_size/1024,1)} KB)")

# ---------------------------------------------------------------- inventory
print()
print("=" * 78)
print("INVENTORY")
print("=" * 78)
print(f"  pages/            {len(list((FRONT / 'pages').glob('*.tsx')))}")
print(f"  features/         {len(list((FRONT / 'features').glob('*')))}")
print(f"  components/       {len(list((FRONT / 'components').rglob('*.tsx')))}")
print(f"  source modules    {len(sources)}")
print(f"  CSS files         {len(list(FRONT.rglob('*.css')))}")
for css in sorted(FRONT.rglob("*.css")):
    print(f"    {str(css.relative_to(FRONT)).replace(chr(92), '/')}  {round(css.stat().st_size/1024,1)} KB")

sizes = Counter()
for f in sources:
    sizes[str(f.relative_to(FRONT)).split("/")[0]] += f.stat().st_size
print()
for k, v in sizes.most_common():
    print(f"  {k:22} {round(v/1024):>6} KB")