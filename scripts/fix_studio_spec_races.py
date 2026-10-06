"""Two remaining Studio-spec fixes.

1. `13. moving a keyframe` read the clip's keyframes straight after clicking
   "+ keyframe", with no wait for the debounced autosave to reach the server. It
   therefore saw zero keyframes and failed on a timing race, not a product fault.

2. `18. a rejected operation` seeked to `max - 0.5` on the Seek range. That is
   outside the clip only if the timeline is longer than the clip; the assertion
   then also counted clips globally. It now seeks to a point derived from the
   clip's own end (clamped to the control's real maximum) and asserts the clip-id
   SET is unchanged -- which is what "the server refused the op" actually means.
"""

from pathlib import Path

SPEC = Path(__file__).resolve().parents[1] / "frontend" / "e2e" / "studio-interactions.spec.ts"

OLD_13 = """    await page.getByRole("button", { name: "+ keyframe" }).click();
    const doc1 = await readTimeline(request, account, timelineId);
    const kfs = clipById(doc1, "caption", caption.clipId).keyframes as
      | Record<string, unknown>[]
      | undefined;
    expect(kfs?.length ?? 0).toBeGreaterThan(0);"""

NEW_13 = """    await page.getByRole("button", { name: "+ keyframe" }).click();
    // WAIT FOR THE SERVER. The autosave is debounced, so reading straight after
    // the click races it and reports zero keyframes -- a test bug that looks
    // exactly like "adding a keyframe does nothing".
    const doc1 = await expectServer(
      page,
      request,
      account,
      timelineId,
      "the keyframe reached the server",
      (doc) => {
        const kfs = clipById(doc, "caption", caption.clipId).keyframes;
        return Array.isArray(kfs) && kfs.length > 0;
      },
    );
    const kfs = clipById(doc1, "caption", caption.clipId).keyframes as
      | Record<string, unknown>[]
      | undefined;"""

OLD_18 = """  const seek = page.getByLabel("Seek");
  const max = Number(await seek.getAttribute("max"));
  await seek.fill(String(max - 0.5));
  await page.getByRole("button", { name: /Split @/ }).click();"""

NEW_18 = """  // Put the playhead clearly OUTSIDE the clip, derived from the clip itself and
  // clamped to the control's real maximum. A hardcoded value only lands outside
  // the clip by accident.
  const seek = page.getByLabel("Seek");
  const max = Number(await seek.getAttribute("max"));
  const outside = Math.min(Number(clipById(before, "video", clip.clipId).start) + 4 + 1, max);
  await seek.fill(String(outside));
  await page.getByRole("button", { name: /Split @/ }).click();"""

OLD_18B = """  const after = await readTimeline(request, account, timelineId);
  expect(clipsOn(after, "video").every((c) => c.id === clip.clipId)).toBe(true);
  expect(clipsOn(after, "video")).toHaveLength(1);
  expect(timelineVersion(after)).toBe(versionBefore);"""

NEW_18B = """  // "The server refused the op" means the clip-id SET is identical and the
  // version never advanced. Comparing sets rather than counts is what makes this
  // an assertion about refusal rather than about how many clips happen to exist.
  const after = await readTimeline(request, account, timelineId);
  const ids = (doc: Record<string, unknown>) =>
    clipsOn(doc, "video")
      .map((c) => String(c.id))
      .sort();
  expect(ids(after)).toEqual(ids(before));
  expect(timelineVersion(after)).toBe(versionBefore);"""


def main() -> int:
    text = SPEC.read_text(encoding="utf-8")
    for old, new, label in (
        (OLD_13, NEW_13, "13 keyframe wait"),
        (OLD_18, NEW_18, "18 seek target"),
        (OLD_18B, NEW_18B, "18 set comparison"),
    ):
        if old in text:
            text = text.replace(old, new)
            print(f"applied: {label}")
        else:
            print(f"SKIPPED (not found): {label}")
    SPEC.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
