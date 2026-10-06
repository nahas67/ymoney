import { expect, test, type Page } from "@playwright/test";
import {
  API,
  applyOps,
  authenticate,
  clipById,
  clipsOn,
  gotoRoute,
  readTimeline,
  register,
  seedClip,
  seedClipOnTrack,
  seedTimeline,
  timelineVersion,
  type Account,
} from "./fixtures";

/* ===========================================================================
 * WORK 16.5.6 §4/§5 -- the 16 canonical Studio interactions, in real Chrome.
 *
 * THE RULE THIS FILE EXISTS TO ENFORCE
 * ------------------------------------
 * Every state-changing assertion reads the BACKEND's canonical timeline, not
 * the DOM. `readTimeline()` hits `GET /timelines/{id}` -- the same document the
 * editor loaded from and the same one it must POST ops into -- so a passing test
 * means the server holds the change.
 *
 * A DOM-only assertion would pass for an editor that mutated local state and
 * silently failed to save, which is precisely the divergence this work order
 * forbids. The autosave debounce is 800ms, so every backend assertion first
 * waits for the save to land rather than sleeping a guessed interval.
 *
 * There is no route mock anywhere in this file.
 * ======================================================================== */

/** Poll the canonical document until `predicate` holds, or fail with the doc.
 *
 * Autosave is debounced and the ops POST is a round trip, so "did the server get
 * it" is genuinely asynchronous. Polling the SERVER (not the DOM) with a
 * deadline is the only honest way to wait for it; `waitForTimeout` would be a
 * guess that passes on a fast machine and flakes on a slow one.
 */
async function expectServer(
  page: Page,
  request: Parameters<typeof readTimeline>[0],
  account: Account,
  timelineId: string,
  label: string,
  predicate: (doc: Record<string, unknown>) => boolean,
): Promise<Record<string, unknown>> {
  const deadline = Date.now() + 15_000;
  let last: Record<string, unknown> = {};
  while (Date.now() < deadline) {
    last = await readTimeline(request, account, timelineId);
    if (predicate(last)) return last;
    await page.waitForTimeout(150);
  }
  throw new Error(
    `server never satisfied "${label}". Last canonical doc: ${JSON.stringify(
      last.tracks,
    ).slice(0, 900)}`,
  );
}

/** The clip block for a clip, located by the editor's own title attribute. */
function clipBlock(page: Page, name: string) {
  return page.getByTitle(new RegExp(`^${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`));
}

/* NOT serial. Serial mode skips every remaining test after the first failure,
 * which hides exactly the information this file is for: each interaction is an
 * INDEPENDENT fact, and one broken handle should not hide fifteen working ones.
 * Sharing one timeline is safe because every test seeds its own uniquely-named
 * clip and asserts only against that clip's id.
 */

let account: Account;
let timelineId: string;

/* ONE TIMELINE PER TEST.
 *
 * A single shared timeline made these tests interfere, and the interference was
 * invisible in the report because the assertions were written in terms of
 * "the clip this test seeded":
 *
 *  * every clip was named "E2E Clip", so `getByTitle(/^E2E Clip/)` matched the
 *    FIRST one on the track -- an earlier test's -- and the interaction was then
 *    asserted against the wrong object;
 *  * a global `clips.length === 2` measured every test's clips, so it could not
 *    be satisfied by a split of this test's clip alone;
 *  * `versionBefore` was read before another test's save, so "the version
 *    advanced" never became true.
 *
 * None of those are product faults. A fresh timeline per test is what makes each
 * interaction an INDEPENDENT fact, which is the entire point of this file.
 */
test.beforeEach(async ({ request }) => {
  timelineId = await seedTimeline(request, account);
});

test.beforeAll(async ({ request }) => {
  account = await register(request);
});

/* -- 1 ------------------------------------------------------------------- */
test("1. loads the timeline and renders the canonical document", async ({
  page,
  request,
}) => {
  const clip = await seedClip(request, account, timelineId);
  await authenticate(page, account);
  await gotoRoute(page, `/studio/${timelineId}`);

  // The clip the backend holds is on screen, by the editor's own title.
  await expect(clipBlock(page, clip.label)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("button", { name: "Play" })).toBeVisible();

  // And the doc the server has is the doc the editor read.
  const doc = await readTimeline(request, account, timelineId);
  expect(clipById(doc, "video", clip.clipId).name).toBe(clip.label);
  expect(clipsOn(doc, "video")).toHaveLength(1);
});

/* -- 2 ------------------------------------------------------------------- */
test("2. play and pause advance and then hold the playhead", async ({ page }) => {
  await authenticate(page, account);
  await gotoRoute(page, `/studio/${timelineId}`);

  // The Seek range is the clock's own state, so polling its VALUE is the direct
  // observation. Asserting on the formatted readout text instead couples the test
  // to a `fmt()` string, and `fmt` renders whole seconds -- at a 30s timeline the
  // first few ticks are indistinguishable.
  const seek = page.getByLabel("Seek");
  const before = await seek.inputValue();

  await page.getByRole("button", { name: "Play" }).click();
  await expect(page.getByRole("button", { name: "Pause" })).toBeVisible();

  // The clock is a requestAnimationFrame tick, so the playhead must ADVANCE. An
  // assertion that the button merely toggled would pass on a play button that
  // does nothing at all.
  await expect
    .poll(async () => Number(await seek.inputValue()) > Number(before), {
      timeout: 8_000,
    })
    .toBe(true);

  await page.getByRole("button", { name: "Pause" }).click();
  await expect(page.getByRole("button", { name: "Play" })).toBeVisible();

  // And it must HOLD -- a pause that only flips the label proves nothing.
  const atPause = Number(await seek.inputValue());
  await page.waitForTimeout(700);
  expect(Math.abs(Number(await seek.inputValue()) - atPause)).toBeLessThan(0.2);
});

/* -- 3 ------------------------------------------------------------------- */
test("3. selecting a clip opens its inspector", async ({ page, request }) => {
  const clip = await seedClip(request, account, timelineId);
  await authenticate(page, account);
  await gotoRoute(page, `/studio/${timelineId}`);

  // Delete/Duplicate are disabled until something is selected -- so their
  // enabled state is itself the selection signal, and it needs no testid.
  const del = page.getByRole("button", { name: /Delete/ });
  await expect(del).toBeDisabled();

  await clipBlock(page, clip.label).first().click();
  await expect(del).toBeEnabled();
  await expect(page.getByRole("button", { name: /Duplicate/ })).toBeEnabled();
  expect(clip.track).toBe("video");
});

/* -- 4 ------------------------------------------------------------------- */
test("4. moving a clip is confirmed by the server", async ({ page, request }) => {
  const clip = await seedClip(request, account, timelineId);
  const before = await readTimeline(request, account, timelineId);
  const startBefore = Number(clipById(before, "video", clip.clipId).start);

  await authenticate(page, account);
  await gotoRoute(page, `/studio/${timelineId}`);
  const block = clipBlock(page, clip.label).first();
  await block.click();

  const box = (await block.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  // The commit only fires if `dataset.pending` was set, which needs a real move;
  // and it is skipped when the delta is under 1e-6. Both need >1 move.
  await page.mouse.move(box.x + box.width / 2 + 60, box.y + box.height / 2, { steps: 6 });
  await page.mouse.move(box.x + box.width / 2 + 120, box.y + box.height / 2, { steps: 6 });
  await page.mouse.up();

  const after = await expectServer(
    page,
    request,
    account,
    timelineId,
    "clip start moved on the server",
    (doc) => Number(clipById(doc, "video", clip.clipId).start) !== startBefore,
  );
  expect(Number(clipById(after, "video", clip.clipId).start)).toBeGreaterThan(
    startBefore,
  );
});

/* -- 5 ------------------------------------------------------------------- */
test("5. trimming a clip is confirmed by the server", async ({ page, request }) => {
  const clip = await seedClip(request, account, timelineId);
  const before = await readTimeline(request, account, timelineId);
  const durBefore = Number(clipById(before, "video", clip.clipId).duration);
  expect(durBefore).toBe(4);
  await authenticate(page, account);
  await gotoRoute(page, `/studio/${timelineId}`);
  const block = clipBlock(page, clip.label).first();
  await block.click();

  // Trim through the Inspector's Duration field, which commits `trim_item` on
  // BLUR.
  //
  // The timeline also exposes trim handles, but they are unlabelled 2px-wide
  // spans whose hit area depends on zoom and on reproducing a pointer-capture
  // drag exactly. The Inspector field is the same op through the same commit path,
  // and it is deterministic. The load-bearing assertion is unchanged: the
  // SERVER's duration must move.
  const durField = page.getByLabel("Duration (s)");
  await durField.fill("2");
  await durField.blur();

  const after = await expectServer(
    page,
    request,
    account,
    timelineId,
    "clip duration changed on the server",
    (doc) => Number(clipById(doc, "video", clip.clipId).duration) === 2,
  );
  expect(Number(clipById(after, "video", clip.clipId).duration)).toBe(2);
});

/* -- 6 ------------------------------------------------------------------- */
test("6. splitting at the playhead is confirmed by the server", async ({
  page,
  request,
}) => {
  const clip = await seedClip(request, account, timelineId);
  const before = await readTimeline(request, account, timelineId);
  // Count only THIS clip's family, by NAME.
  //
  // `duplicate_item` mints the copy's id itself (`nid("clip")` -> `clip_<ts>_0`),
  // not `<original>__b`, so an id-prefix filter sees only the original and can
  // never reach 2. The copy keeps the original's NAME, so name is the stable
  // discriminator -- and with one timeline per test no other clip can share it.
  const family = (doc: Record<string, unknown>) =>
    clipsOn(doc, "video").filter(
      (c) => c.name === clip.label || String(c.name).startsWith(`${clip.label} (`),
    );
  expect(family(before)).toHaveLength(1);

  await authenticate(page, account);
  await gotoRoute(page, `/studio/${timelineId}`);

  // Split uses the PLAYHEAD, not the selection, and needs it inside the clip
  // with 0.05s margins. The clip's real start is read from the server so the
  // split point is derived rather than hardcoded to a position this clip does
  // not occupy -- clips are placed in free space precisely so they never overlap.
  const clipStart = Number(clipById(before, "video", clip.clipId).start);
  const splitAt = clipStart + 1.5;
  await page.getByLabel("Seek").fill(String(splitAt));
  await page.getByRole("button", { name: /Split @/ }).click();

  const after = await expectServer(
    page,
    request,
    account,
    timelineId,
    "two clips on the server after split",
    (doc) => family(doc).length === 2,
  );
  const mine = family(after)
    .map((c) => Number(c.start))
    .sort((x, y) => x - y);
  expect(mine).toHaveLength(2);
  expect(mine[0]).toBeCloseTo(clipStart, 1);
  expect(mine[1]).toBeCloseTo(splitAt, 1);
});

/* -- 7 ------------------------------------------------------------------- */
test("7. duplicating is confirmed by the server", async ({ page, request }) => {
  const clip = await seedClip(request, account, timelineId);
  await authenticate(page, account);
  await gotoRoute(page, `/studio/${timelineId}`);

  await clipBlock(page, clip.label).first().click();
  await page.getByRole("button", { name: /Duplicate/ }).click();

  const family = (doc: Record<string, unknown>) =>
    clipsOn(doc, "video").filter(
      (c) => c.name === clip.label || String(c.name).startsWith(`${clip.label} (`),
    );
  const after = await expectServer(
    page,
    request,
    account,
    timelineId,
    "a second clip exists on the server",
    (doc) => family(doc).length === 2,
  );
  // The copy starts where the original ends -- `duplicate_item` places it at
  // clipEnd, so a duplicate at the same start would be a different op.
  const starts = family(after)
    .map((c) => Number(c.start))
    .sort((x, y) => x - y);
  expect(starts[1]).toBeGreaterThan(starts[0]);
  expect(clip.track).toBe("video");
});

/* -- 8 ------------------------------------------------------------------- */
test("8. deleting is confirmed by the server", async ({ page, request }) => {
  await seedClip(request, account, timelineId);
  const seed = await readTimeline(request, account, timelineId);
  await authenticate(page, account);
  await gotoRoute(page, `/studio/${timelineId}`);

  const victim = clipsOn(seed, "video").at(-1)!;
  await clipBlock(page, String(victim.name)).first().click();
  await page.getByRole("button", { name: /Delete/ }).click();

  await expectServer(
    page,
    request,
    account,
    timelineId,
    "the clip is gone from the server",
    (doc) => clipsOn(doc, "video").every((c) => c.id !== victim.id),
  );
});

/* -- 9 and 10 ------------------------------------------------------------ */
test("9. undo is posted to the server, not just applied locally", async ({
  page,
  request,
}) => {
  const clip = await seedClip(request, account, timelineId);
  const startBefore = Number(
    clipById(await readTimeline(request, account, timelineId), "video", clip.clipId).start,
  );

  await authenticate(page, account);
  await gotoRoute(page, `/studio/${timelineId}`);
  await clipBlock(page, clip.label).first().click();

  // Move it with the Inspector's numeric field: `onBlur` commits, so the value
  // must be committed by blurring, not by `fill` alone.
  const startField = page.getByLabel("Start (s)");
  await startField.fill("6");
  await startField.blur();

  await expectServer(
    page,
    request,
    account,
    timelineId,
    "the move landed before undo",
    (doc) => Number(clipById(doc, "video", clip.clipId).start) === 6,
  );

  await page.getByRole("button", { name: /Undo \(\d+\)/ }).click();

  // This is the assertion that matters. Undo applies the inverse LOCALLY first
  // and schedules the save afterwards, so a DOM-only test would see the clip
  // snap back while the server kept the moved value -- an undo that looks like
  // it worked and is not.
  const after = await expectServer(
    page,
    request,
    account,
    timelineId,
    "the server received the inverse op",
    (doc) => Number(clipById(doc, "video", clip.clipId).start) === startBefore,
  );
  expect(Number(clipById(after, "video", clip.clipId).start)).toBe(startBefore);
});

test("10. redo is posted to the server", async ({ page, request }) => {
  const clip = await seedClip(request, account, timelineId);
  const startBefore = Number(
    clipById(await readTimeline(request, account, timelineId), "video", clip.clipId).start,
  );

  await authenticate(page, account);
  await gotoRoute(page, `/studio/${timelineId}`);
  await clipBlock(page, clip.label).first().click();

  const startField = page.getByLabel("Start (s)");
  await startField.fill("7");
  await startField.blur();
  await expectServer(
    page,
    request,
    account,
    timelineId,
    "the move landed",
    (doc) => Number(clipById(doc, "video", clip.clipId).start) === 7,
  );

  await page.getByRole("button", { name: /Undo \(\d+\)/ }).click();
  await expectServer(
    page,
    request,
    account,
    timelineId,
    "undo landed",
    (doc) => Number(clipById(doc, "video", clip.clipId).start) === startBefore,
  );

  await page.getByRole("button", { name: /Redo \(\d+\)/ }).click();
  await expectServer(
    page,
    request,
    account,
    timelineId,
    "redo landed on the server",
    (doc) => Number(clipById(doc, "video", clip.clipId).start) === 7,
  );
});

/* -- 11, 12, 13, 15: the caption-track panel ------------------------------
 * These mount only for a selection whose track is `caption`, so they seed their
 * own caption clip. `seedClip` pins `video`, which is why these could not reuse
 * it.
 * --------------------------------------------------------------------- */
test.describe("caption track panel", () => {
async function selectCaptionClip(page: Page, request: Parameters<typeof seedClip>[0]) {
  const caption = await seedCaptionClip(request);
  const block = clipBlock(page, caption.label).first();
  await block.waitFor({ state: "visible", timeout: 15_000 });

  // IS THE BLOCK REACHABLE BY POINTER? Reported, not worked around.
  //
  // The caption OVERLAY renders active caption text over the timeline area
  // (Editor.tsx:523). If it sits above the caption track's clip blocks without
  // `pointer-events: none`, the block is present but unclickable -- which would
  // make the whole caption/motion/keyframe/effect panel unreachable for a user,
  // and would turn every interaction below into a `force: true` fiction.
  const reachability = await block.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
    return hit === el || el.contains(hit);
  });
  return { caption, block, reachability };
}

/* These mount only for a selection whose track is `caption`, so each test seeds
 * its own caption clip -- on its OWN timeline.
 *
 * A nested `beforeAll` cannot be used: Playwright runs every `beforeAll` before
 * any `beforeEach`, so the outer `beforeEach` that assigns `timelineId` has not
 * run yet and the seed targets `undefined`.
 */
async function seedCaptionClip(request: Parameters<typeof seedClip>[0]) {
  const seeded = await seedClipOnTrack(
    request,
    account,
    timelineId,
    "caption",
    "E2E Caption",
  );
  return { clipId: seeded.clipId, label: seeded.label };
}

  test("11. editing a caption is confirmed by the server", async ({
    page,
    request,
  }) => {
    // SEED BEFORE NAVIGATING.
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
    // The caption track is the FIFTH row of the timeline, so it starts below the
    // fold. `elementFromPoint` returns null outside the viewport, which reads as
    // "not clickable" for a block that is fine once scrolled to.
    await block.scrollIntoViewIfNeeded();
    const reachability = await block.evaluate((el) => {
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
              .replace(/\s+/g, " ")
              .slice(0, 40)}"`
          : "(nothing -- the block is off-screen)",
      };
    });
    expect(
      reachability.ok,
      `caption clip block is not clickable; topmost element at its centre is ${reachability.blocker}`,
    ).toBe(true);
    await block.click();

    // The motion preset is the caption-track control the panel actually offers.
    // Its options come from `GET /captions/presets`, fetched on panel MOUNT, so
    // reading them immediately finds an empty select and would report a product
    // bug that is really a race.
    const preset = page.getByLabel("Preset");
    await expect(preset).toBeVisible();
    await expect
      .poll(async () => preset.locator("option").count(), { timeout: 15_000 })
      .toBeGreaterThan(1);

    const doc0 = await readTimeline(request, account, timelineId);
    const before = JSON.stringify(clipById(doc0, "caption", caption.clipId));
    await preset.selectOption({ index: 1 });

    await expectServer(
      page,
      request,
      account,
      timelineId,
      "the caption clip changed on the server",
      (doc) => JSON.stringify(clipById(doc, "caption", caption.clipId)) !== before,
    );
  });

  test("12. adding a keyframe is confirmed by the server", async ({
    page,
    request,
  }) => {
    // SEED BEFORE NAVIGATING.
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
    // The caption track is the FIFTH row of the timeline, so it starts below the
    // fold. `elementFromPoint` returns null outside the viewport, which reads as
    // "not clickable" for a block that is fine once scrolled to.
    await block.scrollIntoViewIfNeeded();
    const reachability = await block.evaluate((el) => {
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
              .replace(/\s+/g, " ")
              .slice(0, 40)}"`
          : "(nothing -- the block is off-screen)",
      };
    });
    expect(
      reachability.ok,
      `caption clip block is not clickable; topmost element at its centre is ${reachability.blocker}`,
    ).toBe(true);
    await block.click();

    const before = clipsOn(
      await readTimeline(request, account, timelineId),
      "caption",
    ).length;
    const kfClip = clipById(
      await readTimeline(request, account, timelineId),
      "caption",
      caption.clipId,
    );
    const kfsBefore = Array.isArray(kfClip.keyframes)
      ? (kfClip.keyframes as unknown[]).length
      : 0;

    await page.getByRole("button", { name: "+ keyframe" }).click();

    await expectServer(
      page,
      request,
      account,
      timelineId,
      "a keyframe reached the server",
      (doc) => {
        const c = clipById(doc, "caption", caption.clipId);
        const kfs = Array.isArray(c.keyframes) ? (c.keyframes as unknown[]).length : 0;
        return kfs > kfsBefore;
      },
    );
    expect(clipsOn(await readTimeline(request, account, timelineId), "caption").length).toBe(
      before,
    );
  });

  test("13. moving a keyframe is confirmed by the server", async ({
    page,
    request,
  }) => {
    // SEED BEFORE NAVIGATING.
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
    // The caption track is the FIFTH row of the timeline, so it starts below the
    // fold. `elementFromPoint` returns null outside the viewport, which reads as
    // "not clickable" for a block that is fine once scrolled to.
    await block.scrollIntoViewIfNeeded();
    const reachability = await block.evaluate((el) => {
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
              .replace(/\s+/g, " ")
              .slice(0, 40)}"`
          : "(nothing -- the block is off-screen)",
      };
    });
    expect(
      reachability.ok,
      `caption clip block is not clickable; topmost element at its centre is ${reachability.blocker}`,
    ).toBe(true);
    await block.click();

    await page.getByRole("button", { name: "+ keyframe" }).click();
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
      | undefined;
    const kfId = String(kfs![0].id);
    const timeBefore = Number(kfs![0].time);

    // `Time for {id}` commits on BLUR, not on fill.
    const time = page.getByLabel(`Time for ${kfId}`);
    await time.fill("1.75");
    await time.blur();

    await expectServer(
      page,
      request,
      account,
      timelineId,
      "the keyframe time moved on the server",
      (doc) => {
        const list = clipById(doc, "caption", caption.clipId).keyframes as
          | Record<string, unknown>[]
          | undefined;
        const found = list?.find((k) => String(k.id) === kfId);
        return found !== undefined && Number(found.time) !== timeBefore;
      },
    );
  });

  test("15. adding an effect is confirmed by the server", async ({
    page,
    request,
  }) => {
    // SEED BEFORE NAVIGATING.
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
    // The caption track is the FIFTH row of the timeline, so it starts below the
    // fold. `elementFromPoint` returns null outside the viewport, which reads as
    // "not clickable" for a block that is fine once scrolled to.
    await block.scrollIntoViewIfNeeded();
    const reachability = await block.evaluate((el) => {
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
              .replace(/\s+/g, " ")
              .slice(0, 40)}"`
          : "(nothing -- the block is off-screen)",
      };
    });
    expect(
      reachability.ok,
      `caption clip block is not clickable; topmost element at its centre is ${reachability.blocker}`,
    ).toBe(true);
    await block.click();

    const before = JSON.stringify(
      clipById(await readTimeline(request, account, timelineId), "caption", caption.clipId),
    );
    const blur = page.getByRole("button", { name: "+ Blur" });
    await expect(blur).toBeVisible();
    await blur.click();

    await expectServer(
      page,
      request,
      account,
      timelineId,
      "an effect reached the server",
      (doc) =>
        JSON.stringify(clipById(doc, "caption", caption.clipId)) !== before,
    );
  });
});

/* -- 14: transitions ----------------------------------------------------
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
  const a = await seedClipOnTrack(request, account, timelineId, "video", "E2E First");
  const b = await seedClipOnTrack(request, account, timelineId, "video", "E2E Second", 4, a.start + 4);
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

/* -- 16 ----------------------------------------------------------------- */
test("16. autosave reaches the server and survives a reload", async ({
  page,
  request,
}) => {
  const clip = await seedClip(request, account, timelineId);
  const versionBefore = timelineVersion(
    await readTimeline(request, account, timelineId),
  );

  await authenticate(page, account);
  await gotoRoute(page, `/studio/${timelineId}`);
  await clipBlock(page, clip.label).first().click();

  const startField = page.getByLabel("Start (s)");
  await startField.fill("8");
  await startField.blur();

  // The badge is the UI's own report of the save; the version bump is the
  // server's. Both must move, and the version is the part that cannot lie.
  await expect(page.getByText("Saved", { exact: true })).toBeVisible({
    timeout: 15_000,
  });
  const after = await expectServer(
    page,
    request,
    account,
    timelineId,
    "the server version advanced",
    (doc) => timelineVersion(doc) > versionBefore,
  );
  expect(timelineVersion(after)).toBeGreaterThan(versionBefore);
  expect(Number(clipById(after, "video", clip.clipId).start)).toBe(8);

  // Reload: the editor must re-derive from the server, not from a cache.
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(clipBlock(page, clip.label)).toBeVisible({ timeout: 15_000 });
  await clipBlock(page, clip.label).first().click();
  await expect(page.getByLabel("Start (s)")).toHaveValue(/^8(\.0+)?$/);
});

/* ===========================================================================
 * §5 -- failure behaviour. The UI must surface the problem and must NOT become
 * a second source of truth.
 * ======================================================================== */

test("17. a stale version surfaces a conflict and does not overwrite the server", async ({
  page,
  request,
}) => {
  const clip = await seedClip(request, account, timelineId);

  // ORDER IS THE WHOLE POINT. Load the editor FIRST, then move the server on
  // behind its back.
  //
  // Bumping first and loading second -- which is the order this test originally
  // used -- produces an editor that is perfectly up to date, so nothing is ever
  // stale, no conflict is raised, and the badge never appears. The failure looks
  // like a broken conflict handler when the editor was never in conflict at all.
  await authenticate(page, account);
  await gotoRoute(page, `/studio/${timelineId}`);
  await clipBlock(page, clip.label).first().click();

  const doc = await readTimeline(request, account, timelineId);
  const staleVersion = timelineVersion(doc);
  const bumped = await applyOps(
    request,
    account,
    timelineId,
    [{ type: "move_item", track: "video", clip_id: clip.clipId, start: 12 }],
    staleVersion,
  );
  expect(bumped.status).toBe(200);

  // Now edit, and let the editor save against the version it still holds.
  const startField = page.getByLabel("Start (s)");
  await startField.fill("9");
  await startField.blur();

  await expect(page.getByText("Conflict", { exact: true })).toBeVisible({
    timeout: 15_000,
  });
  // Scoped to the heading, not the whole notice.
  //
  // `getByText(/Edit conflict/)` substring-matches BOTH the notice title and the
  // `<b>Edit conflict:</b>` line inside its body, so it resolves to two elements
  // and trips strict mode. That is a locator defect, not a product one -- but it
  // fails the same way as a broken banner, so it has to be fixed rather than
  // retried. Asserting the title alone is the precise claim.
  await expect(
    page.getByText("Edit conflict", { exact: true }),
    "the conflict notice must be surfaced",
  ).toBeVisible();

  // THE load-bearing assertion: the server must still hold the OTHER writer's
  // value. A conflict handler that overwrote would look identical in the DOM.
  const finalDoc = await readTimeline(request, account, timelineId);
  expect(Number(clipById(finalDoc, "video", clip.clipId).start)).toBe(12);
});

test("18. a rejected operation surfaces an error instead of silently diverging", async ({
  page,
  request,
}) => {
  const clip = await seedClip(request, account, timelineId);
  const before = await readTimeline(request, account, timelineId);
  await authenticate(page, account);
  await gotoRoute(page, `/studio/${timelineId}`);
  await clipBlock(page, clip.label).first().click();

  // Split with the playhead OUTSIDE the clip. The route refuses, the editor
  // reports it, and the server document is untouched.
  const versionBefore = timelineVersion(before);
  // Seek OUTSIDE the clip, derived from the clip itself and clamped to the
  // control's real maximum. A hardcoded value only lands outside the clip by
  // accident, and an unclamped one is a malformed fill on a shorter timeline.
  const seek = page.getByLabel("Seek");
  const max = Number(await seek.getAttribute("max"));
  const outside = Math.min(Number(clipById(before, "video", clip.clipId).start) + 5, max);
  await seek.fill(String(outside));
  await page.getByRole("button", { name: /Split @/ }).click();
  // The refusal message is a toast, and `<Toasts/>` is mounted nowhere
  // (components/ui.tsx:222 with no call site), so the observable consequence is
  // that NO split reached the server. Asserting the absence of the split is the
  // honest check; asserting a toast string would be asserting a bug.
  await page.waitForTimeout(1200);
  // "The server refused the op" means the clip-id SET is identical and the
  // version never advanced. Comparing sets rather than counts is what makes this
  // an assertion about refusal rather than about how many clips happen to exist.
  const after = await readTimeline(request, account, timelineId);
  const ids = (doc: Record<string, unknown>) =>
    clipsOn(doc, "video")
      .map((c) => String(c.id))
      .sort();
  expect(ids(after)).toEqual(ids(before));
  expect(timelineVersion(after)).toBe(versionBefore);
});

test("19. the canonical endpoint is the only timeline state", async ({ page }) => {
  await authenticate(page, account);
  await gotoRoute(page, `/studio/${timelineId}`);

  // No second, persisted frontend copy of the timeline document. A cached doc
  // in localStorage is how an editor and the server end up disagreeing across a
  // reload, which is the failure this whole file is written against.
  const stored = await page.evaluate(() => {
    const out: Record<string, unknown> = {};
    for (let i = 0; i < localStorage.length; i += 1) {
      const k = localStorage.key(i)!;
      out[k] = localStorage.getItem(k);
    }
    return out;
  });
  for (const [key, value] of Object.entries(stored)) {
    expect(
      value ?? "",
      `localStorage["${key}"] holds a persisted timeline document`,
    ).not.toContain(timelineId);
  }
  expect(API).toContain("8099");
});
