"""Rewrite Studio test 14 (line-range edit).

16.5.6 asserted the transition control was ABSENT -- true of the dead code, and it
failed loudly if the control ever appeared without being driven. 16.5.7 moved the
block into `TransitionEditor` (mounted in the Inspector under the visual-track
gate), so interaction 14 must now be DRIVEN:

    select a visual clip with an adjacent clip -> control visible -> set a
    transition -> GET the canonical timeline -> persisted -> reload -> still there

plus the refusal case for an overlay track.

The old block runs from the `/* -- 14: transitions` marker to the closing `});`
immediately before the `/* -- 16` marker.
"""

from pathlib import Path

SPEC = Path(__file__).resolve().parents[1] / "frontend" / "e2e" / "studio-interactions.spec.ts"

NEW = '''/* -- 14: transitions ----------------------------------------------------
 * The control used to be DEAD: the block sat behind `track !== "caption"`
 * inside a panel that mounts only for `sel.track === "caption"`. It now lives in
 * `TransitionEditor`, mounted in the Inspector under the visual-track gate, because
 * the renderer's transition is a video cross-dissolve.
 *
 * The load-bearing half is the SERVER read: a transition that renders in the DOM
 * but never reached `POST /operations` would pass every DOM-only assertion.
 * --------------------------------------------------------------------- */
test("14. a transition on a visual clip is driven and persisted by the server", async ({
  page,
  request,
}) => {
  // TWO adjacent clips on the VIDEO track: `find_adjacent_pair` refuses any pair
  // that does not meet, so one clip would offer nothing to transition into.
  const a = await seedClipOnTrack(request, account, timelineId, "video", "E2E First", 4);
  const b = await seedClipOnTrack(request, account, timelineId, "video", "E2E Second", 4);
  expect(b.start).toBeCloseTo(a.start + 4, 1);

  await authenticate(page, account);
  await gotoRoute(page, `/studio/${timelineId}`);
  await clipBlock(page, a.label).first().click();

  const add = page.getByLabel("Add transition");
  await expect(add, "the transition control is unreachable on a visual clip").toBeVisible({
    timeout: 15_000,
  });

  // Options come from `GET /captions/transitions`, fetched on mount.
  await expect
    .poll(async () => add.locator("option").count(), { timeout: 15_000 })
    .toBeGreaterThan(1);
  await add.selectOption({ index: 1 });

  const after = await expectServer(
    page,
    request,
    account,
    timelineId,
    "the transition reached the server",
    (doc) => Boolean(clipById(doc, "video", a.clipId).transition),
  );
  const stored = clipById(after, "video", a.clipId).transition as Record<string, unknown>;
  expect(String(stored.from_item)).toBe(a.clipId);
  expect(String(stored.to_item)).toBe(b.clipId);
  expect(Number(stored.duration)).toBeGreaterThan(0);

  // Reload: re-derived from the server, not from a cache.
  await page.reload({ waitUntil: "domcontentloaded" });
  await clipBlock(page, a.label).first().click();
  await expect(page.getByLabel("Transition type")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByLabel("Transition duration seconds")).toBeVisible();

  const finalDoc = await readTimeline(request, account, timelineId);
  expect(clipById(finalDoc, "video", a.clipId).transition).toBeTruthy();
});

test("14b. an overlay track still refuses transitions, legibly", async ({
  page,
  request,
}) => {
  const overlay = await seedClipOnTrack(request, account, timelineId, "caption", "E2E Overlay");

  await authenticate(page, account);
  await gotoRoute(page, `/studio/${timelineId}`);
  await clipBlock(page, overlay.label).first().click();
  await clipBlock(page, overlay.label).scrollIntoViewIfNeeded();

  // Refused, and SAYS SO. The renderer's transition is a video cross-dissolve, so
  // a caption track has none -- and the panel must not silently omit the control,
  // because an absent control reads as "not implemented" rather than "not
  // applicable".
  await expect(page.getByText(/NOT_AVAILABLE/)).toBeVisible({ timeout: 15_000 });
  await expect(
    page.getByText(/cross-dissolve renders on visual tracks only/),
  ).toBeVisible();
  await expect(page.getByLabel("Add transition")).toHaveCount(0);

  // And nothing was written.
  const doc = await readTimeline(request, account, timelineId);
  expect(clipById(doc, "caption", overlay.clipId).transition).toBeFalsy();
});

'''


def main() -> int:
    lines = SPEC.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, l in enumerate(lines) if l.startswith("/* -- 14: transitions"))
    end = next(i for i, l in enumerate(lines) if i > start and l.startswith("/* -- 16"))
    print(f"replacing lines {start + 1}..{end} ({end - start} lines)")
    SPEC.write_text("".join(lines[:start]) + NEW + "".join(lines[end:]), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())