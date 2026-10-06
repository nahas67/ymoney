"""Rewrite the caption-panel tests to seed BEFORE navigating.

The editor loads the canonical document once on mount, so a caption clip created
after `gotoRoute` is invisible to it. Fixing it in the spec rather than adding a
reload, because "reload until the fixture appears" is exactly the habit that hides
real ordering bugs.
"""

from pathlib import Path

SPEC = Path(__file__).resolve().parents[1] / "frontend" / "e2e" / "studio-interactions.spec.ts"

OLD = """    await authenticate(page, account);
    await gotoRoute(page, `/studio/${timelineId}`);
    const { caption, block, reachability } = await selectCaptionClip(page, request);"""

NEW = """    // SEED BEFORE NAVIGATING.
    //
    // The editor loads the canonical document ONCE on mount. A caption clip
    // created after that is invisible to it until a reload, so the block simply
    // was not on the page -- a test-ordering bug that reads exactly like a UI
    // defect. Adding a reload instead would hide the ordering mistake.
    const caption = await seedCaptionClip(request);
    await authenticate(page, account);
    await gotoRoute(page, `/studio/${timelineId}`);
    const block = clipBlock(page, caption.label).first();
    await block.waitFor({ state: "visible", timeout: 15_000 });
    const reachability = await block.evaluate((el) => {
      const r = el.getBoundingClientRect();
      const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
      return hit === el || el.contains(hit);
    });"""


def main() -> int:
    text = SPEC.read_text(encoding="utf-8")
    if OLD not in text:
        print("pattern not found -- nothing changed")
        return 1
    count = text.count(OLD)
    SPEC.write_text(text.replace(OLD, NEW), encoding="utf-8")
    print(f"replaced {count} occurrence(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
