"""Make the caption-block reachability diagnostic NAME the interceptor.

A boolean `false` sends you back to guessing. The identity of the topmost element
is the answer, so the assertion carries it.
"""

from pathlib import Path

SPEC = Path(__file__).resolve().parents[1] / "frontend" / "e2e" / "studio-interactions.spec.ts"

OLD = """    const reachability = await block.evaluate((el) => {
      const r = el.getBoundingClientRect();
      const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
      return hit === el || el.contains(hit);
    });
    expect(reachability, "the caption clip block is not the top element at its own centre -- the caption overlay intercepts pointer events").toBe(true);"""

NEW = """    const reachability = await block.evaluate((el) => {
      const r = el.getBoundingClientRect();
      const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
      return {
        ok: hit === el || el.contains(hit),
        // NAME THE INTERCEPTOR. A boolean that is merely false sends you
        // guessing; the element's own identity is the answer.
        blocker: hit
          ? `${hit.tagName}.${String(hit.className || "").slice(0, 70)} text="${String(
              (hit as HTMLElement).innerText ?? "",
            )
              .replace(/\\s+/g, " ")
              .slice(0, 40)}"`
          : "(nothing -- the block is off-screen)",
      };
    });
    expect(
      reachability.ok,
      `caption clip block is not clickable; topmost element at its centre is ${reachability.blocker}`,
    ).toBe(true);"""


def main() -> int:
    text = SPEC.read_text(encoding="utf-8")
    if OLD not in text:
        print("pattern not found -- nothing changed")
        return 1
    n = text.count(OLD)
    SPEC.write_text(text.replace(OLD, NEW), encoding="utf-8")
    print(f"replaced {n} occurrence(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
