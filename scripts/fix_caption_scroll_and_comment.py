"""Scroll the clip block into view before probing pointer reachability.

`document.elementFromPoint` returns null for coordinates outside the viewport, so
probing a row below the fold reports "not clickable" for a block that is perfectly
clickable once scrolled to. The caption track is the fifth row of the timeline, so
every caption-panel interaction hit this.

The overlay comment written earlier claimed the caption overlay was the observed
interceptor. It was not -- the boolean was false because the point was off-screen.
`pointer-events: none` on a full-width preview overlay is still correct (a preview
must never be an input target), but the comment must not assert a cause that was
never observed.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "frontend" / "e2e" / "studio-interactions.spec.ts"
EDITOR = REPO / "frontend" / "src" / "pages" "Editor.tsx".replace(" ", "")

OLD_REACH = """    const block = clipBlock(page, caption.label).first();
    await block.waitFor({ state: "visible", timeout: 15_000 });"""

NEW_REACH = """    const block = clipBlock(page, caption.label).first();
    await block.waitFor({ state: "visible", timeout: 15_000 });
    // The caption track is the FIFTH row of the timeline, so it starts below the
    // fold. `elementFromPoint` returns null outside the viewport, which reads as
    // "not clickable" for a block that is fine once scrolled to.
    await block.scrollIntoViewIfNeeded();"""

OLD_EDITOR = """              // `pointer-events: none` is load-bearing, not cosmetic.
              //
              // This overlay previews the caption ON TOP of the track area, and it
              // spans the full width (`left-0 right-0`). Without the opt-out it
              // is the topmost element over the caption track's own clip blocks,
              // so `document.elementFromPoint` at a caption clip's centre returns
              // the overlay: the block is present, painted, and UNCLICKABLE.
              //
              // The consequence is that a caption clip cannot be selected, which
              // means the entire caption/motion/keyframe/effect panel -- which
              // mounts only for `sel.track === "caption"` -- is unreachable for a
              // user. A preview layer that blocks editing of the thing it previews
              // is a defect, and `pointer-events: none` is the fix rather than
              // reordering the z-index: the preview must never be an input target.
"""

NEW_EDITOR = """              // `pointer-events: none`: a preview layer must never be an input
              // target. This overlay spans the full width of the track area
              // (`left-0 right-0`) and sits above the caption track's own clip
              // blocks, so with the default it can swallow a click aimed at a
              // caption clip -- and a caption clip is the only way to reach the
              // caption/motion/keyframe/effect panel at all.
              //
              // NOTE ON EVIDENCE: the browser suite did NOT prove this overlay
              // was intercepting anything. It reported the block unclickable, and
              // the real cause was that the block was below the fold, so
              // `elementFromPoint` returned null. This is still the right
              // property for a pure preview, but it is defence in depth, not a
              // fix for an observed failure, and the comment says so rather than
              // claiming a cause that was never seen.
"""


def main() -> int:
    spec = SPEC.read_text(encoding="utf-8")
    n = spec.count(OLD_REACH)
    if n:
        spec = spec.replace(OLD_REACH, NEW_REACH)
    SPEC.write_text(spec, encoding="utf-8")
    print(f"spec: scrolled-into-view added to {n} place(s)")

    editor = EDITOR.read_text(encoding="utf-8")
    if OLD_EDITOR in editor:
        EDITOR.write_text(editor.replace(OLD_EDITOR, NEW_EDITOR), encoding="utf-8")
        print("editor: overlay comment corrected")
    else:
        print("editor: comment pattern not found -- left alone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
