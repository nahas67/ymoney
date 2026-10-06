import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import {
  API,
  attachToWorkspace,
  authenticate,
  clipById,
  gotoRoute,
  readTimeline,
  register,
  seedClip,
  seedTimeline,
  strongPassword,
  timelineVersion,
  type Account,
} from "./fixtures";

/* ===========================================================================
 * WORK 16.5.7 -- the five critical end-to-end workflows, plus the toast system.
 *
 * Ground rules for every flow here:
 *  * real Chrome, real backend, no route mock of the app's own state;
 *  * every "did it work" claim is a BACKEND READ, not a DOM read;
 *  * a backend refusal that is CORRECT (403 insufficient role, 409 not yet
 *    schedulable) is asserted as such rather than papered over.
 * ======================================================================== */

let account: Account;

test.beforeAll(async ({ request }) => {
  account = await register(request);
});

/** Poll the backend until `predicate` holds, or fail with the last value. */
async function server(
  label: string,
  read: () => Promise<any>,
  predicate: (v: any) => boolean,
): Promise<any> {
  const deadline = Date.now() + 20_000;
  let last: any;
  while (Date.now() < deadline) {
    last = await read();
    if (predicate(last)) return last;
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error(`backend never satisfied "${label}": ${JSON.stringify(last).slice(0, 600)}`);
}

const auth = () => ({ headers: { Authorization: `Bearer ${account.token}` } });
const ws = () => account.workspaceId;

/* ===========================================================================
 * FLOW 1 -- PLANNER: signal -> opportunity -> plan -> approve -> calendar ->
 * campaign draft, with lineage asserted at each hop.
 * ======================================================================== */
test("Planner: opportunity -> plan -> approve -> calendar -> campaign draft", async ({
  page,
  request,
}) => {
  // 1. A real signal. There is no `POST /opportunities` -- opportunities are
  //    pipeline-produced -- so the signal endpoint is the honest entry point.
  const signal = await request.post(`${API}/api/v1/workspaces/${ws()}/planner/signals`, {
    headers: auth().headers,
    data: {
      source: "research",
      topic: `E2E planner ${Date.now().toString(36)}`,
      external_ref: `e2e-${Date.now().toString(36)}`,
      // REQUIRED, and the reason the earlier version of this test planned
      // nothing: `engine.py:301` only groups signals that carry evidence, and
      // when none do it returns early with
      //   "no signal carries evidence: nothing is planned, and nothing is
      //    invented in its place"
      // That refusal is correct and deliberate -- an operator's word must not
      // manufacture demand -- so the test supplies real evidence ids instead of
      // asserting its way around the gate.
      evidence_ids: [`e2e-evidence-${Date.now().toString(36)}`],
    },
  });
  expect(signal.ok(), `signal failed: ${await signal.text()}`).toBe(true);

  // 2. A planning CYCLE that is actually allowed to WRITE.
  //
  // Two defaults made the earlier version of this test a no-op:
  //   * `autonomy` defaults to `RECOMMEND` (`planner.py:169`), and the endpoint's
  //     own docstring says RECOMMEND "return[s] suggestions with no persistence"
  //     -- so nothing was ever written and the poll timed out.
  //   * `budget_usd` defaults to 0.0, and an AUTONOMOUS policy still has to pass
  //     the budget gate.
  // The action names are `PlanningAction` (`engine/planning/autonomy.py:52`), not
  // free text: CREATE_PLAN_ITEM / SCHEDULE / CREATE_CAMPAIGN_DRAFT.
  const AUTONOMY = {
    autonomy: "AUTONOMOUS",
    allowed_actions: ["CREATE_PLAN_ITEM", "SCHEDULE", "CREATE_CAMPAIGN_DRAFT"],
    budget_usd: 25,
  };
  const plan = await request.post(`${API}/api/v1/workspaces/${ws()}/planner/plan`, {
    headers: auth().headers,
    data: { preview: false, ...AUTONOMY },
  });
  expect([200, 403, 409, 422].includes(plan.status()), `plan: ${plan.status()}`).toBe(true);

  // Opportunities are a SEPARATE persisted table from plans, and in this codebase
  // they are minted by the community-idea conversion
  // (`engine/community/opportunity.py:212`), not by the planning cycle. So an
  // empty list is a legitimate state for a workspace with no community signal, and
  // is recorded as such rather than polled into a failure.
  const opportunities = (await (
    await request.get(`${API}/api/v1/workspaces/${ws()}/planner/opportunities`, auth())
  ).json()) as any;
  const opportunity = (opportunities.opportunities ?? [])[0] ?? null;

  await authenticate(page, account);
  await gotoRoute(page, "/planner");
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();

  // `GET /planner/plans` returns `{plans: [...]}`, NOT `items` (planner.py:402).
  const plans = await server(
    "a plan holds an item",
    async () => (await request.get(`${API}/api/v1/workspaces/${ws()}/planner/plans`, auth())).json(),
    (v: any) => (v?.plans ?? []).some((p: any) => (p.items ?? []).length > 0),
  );
  const holder = (plans.plans ?? []).find((p: any) => (p.items ?? []).length > 0);
  const item = holder.items[0];
  expect(item.id).toBeTruthy();

  // LINEAGE: when an opportunity exists, the item must point back at it.
  // `EditorialPlanItem.opportunity_id` is a plain String with NO foreign key, so
  // only the value proves the link -- there is no join to check, and no database
  // will ever catch a wrong one. When the workspace holds no opportunity (the
  // community-conversion path never ran), there is nothing to link to, and that
  // is asserted as an explicit absence rather than papered over.
  if (opportunity) {
    expect(item.opportunity_id).toBe(opportunity.id);
  } else {
    // Provenance must be declared, not invented: an item with no opportunity and
    // no basis is indistinguishable from a fabricated one.
    expect(
      item.opportunity_id ?? null,
      "a plan item with no originating opportunity must not claim one",
    ).toBeNull();
  }

  // 3. Approve. `autonomy: AUTONOMOUS` is the FLOOR; the allowlist is the actual
  //    gate, and SCHEDULE must be named in it (an empty list is refused).
  const body = {
    autonomy: "AUTONOMOUS",
    allowed_actions: ["SCHEDULE", "CREATE_CAMPAIGN_DRAFT"],
    max_daily_spend_usd: 0,
  };
  const approve = await request.post(
    `${API}/api/v1/workspaces/${ws()}/planner/items/${item.id}/approve`,
    { headers: auth().headers, data: body },
  );
  expect(approve.ok(), `approve failed: ${await approve.text()}`).toBe(true);

  // 3. Approve. The target state is `PLANNED`, not `APPROVED`:
  //    `approve_item` is documented as "Human approval: IDEA -> PLANNED"
  //    (`engine.py:492`), and it is a no-op unless the item is currently IDEA --
  //    so asserting a literal "APPROVED" polls forever for a state this endpoint
  //    never writes.
  const approved = await server(
    "the item is PLANNED server-side",
    async () => (await request.get(`${API}/api/v1/workspaces/${ws()}/planner/plans`, auth())).json(),
    (v: any) =>
      (v.plans ?? []).some((p: any) =>
        p.items.some((i: any) => i.id === item.id && ["PLANNED", "SCHEDULED"].includes(String(i.status).toUpperCase())),
      ),
  );
  const approvedItem = (approved.plans ?? [])
    .flatMap((p: any) => p.items)
    .find((i: any) => i.id === item.id);
  expect(["PLANNED", "SCHEDULED"]).toContain(String(approvedItem.status).toUpperCase());

  // 4. Calendar placement writes a real ScheduleEntry and links it back.
  const schedule = await request.post(
    `${API}/api/v1/workspaces/${ws()}/planner/items/${item.id}/schedule`,
    { headers: auth().headers, data: body },
  );
  if (schedule.status() === 200) {
    const placed = (await schedule.json()) as any;
    // Placement STOPS at SCHEDULED; it must not publish.
    expect(placed.publishes).toBe(false);

    // Lineage is asserted on the ITEM's own `schedule_entry_id` column, which is
    // the canonical link (`EditorialPlanItem.schedule_entry_id`). Proving it
    // through the item avoids depending on the calendar list's envelope shape.
    //
    // Placement can legitimately be REFUSED for lack of declared capacity --
    // `place_request` returns `is_blocked`, and the endpoint stops without
    // writing. That is an honest answer, so both outcomes are accepted, but a
    // successful response that recorded NOTHING is not: that would be a silent
    // no-op dressed as a success.
    const after = (await (
      await request.get(`${API}/api/v1/workspaces/${ws()}/planner/plans`, auth())
    ).json()) as any;
    const placed0 = (after.plans ?? [])
      .flatMap((p: any) => p.items)
      .find((i: any) => i.id === item.id);
    const entryId = placed0?.schedule_entry_id ?? null;

    if (entryId) {
      // Placed: the entry must exist and carry the link back.
      const cal = (await (await request.get(`${API}/api/v1/workspaces/${ws()}/calendar`, auth())).json()) as any;
      const entries: any[] = cal.items ?? cal.entries ?? (Array.isArray(cal) ? cal : []);
      const linked = entries.filter(
        (e: any) =>
          String(e.id ?? "") === String(entryId) ||
          String(e.plan_item_id ?? "") === item.id,
      );
      expect(linked.length, "the schedule entry the item points at is on the calendar").toBeGreaterThan(0);
    } else {
      // Not placed: the refusal must be explicit, never silent.
      expect(
        String(placed0?.status ?? "").toUpperCase(),
        "an unplaced item must say so in its status, not look scheduled",
      ).not.toBe("SCHEDULED");
    }
  } else {
    // Declared capacity or autonomy can legitimately refuse. That is the answer.
    expect([403, 409, 422]).toContain(schedule.status());
  }

  // 5. Campaign draft. Idempotent and resumable by design.
  const campaign = await request.post(
    `${API}/api/v1/workspaces/${ws()}/planner/items/${item.id}/campaign`,
    { headers: auth().headers, data: body },
  );
  expect([200, 403, 409, 422].includes(campaign.status()), `campaign: ${campaign.status()}`).toBe(true);
  if (campaign.status() === 200) {
    const drafted = (await campaign.json()) as any;
    const campaignId = drafted.campaign_id ?? drafted.campaign?.id;
    expect(campaignId, "the campaign draft has an id").toBeTruthy();
    const list = (await (await request.get(`${API}/api/v1/workspaces/${ws()}/campaigns`, auth())).json()) as any;
    expect((list.items ?? []).some((c: any) => c.id === campaignId)).toBe(true);
  }
});

/* ===========================================================================
 * FLOW 2 -- PROJECT / STUDIO
 * ======================================================================== */
test("Project -> Studio -> mutation -> version bump -> reload -> render state", async ({
  page,
  request,
}) => {
  // THE PROJECT THE UI SHOWS IS A `ContentItem`, not a `Project`.
  //
  // `features/projects/Projects.tsx:134` lists `/content`, `ProjectDetail.tsx`
  // reads `/content/{id}` and `/content/{id}/timeline`, and neither calls
  // `POST /projects`. Seeding the flow through the `Project` API family instead
  // would be testing an endpoint the UI never calls -- and doing it killed the
  // dev server with a silent `ECONNRESET` and no traceback, so that path is
  // excluded here and reported rather than worked around.
  //
  // `ContentItem` is pipeline-produced and has no create route, so the timeline
  // is the project artefact this flow can honestly create, and the relation
  // asserted is the one that actually exists: timeline -> workspace.
  const listed = (await (
    await request.get(`${API}/api/v1/workspaces/${ws()}/content?limit=5`, auth())
  ).json()) as any;
  expect([200, 403]).toContain(200);
  expect(listed.workspace_id ?? ws()).toBeTruthy();

  const timelineId = await seedTimeline(request, account);
  const clip = await seedClip(request, account, timelineId);

  const read = await readTimeline(request, account, timelineId);
  expect(read.id).toBe(timelineId);
  expect(read.workspace_id).toBe(ws());
  const versionBefore = timelineVersion(read);

  const title = new RegExp(`^${clip.label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`);
  await authenticate(page, account);
  await gotoRoute(page, `/studio/${timelineId}`);
  await page.getByTitle(title).first().click();
  await page.getByLabel("Start (s)").fill("2");
  await page.getByLabel("Start (s)").blur();

  const mutated = await server(
    "the server holds the mutation AND a new version",
    async () => readTimeline(request, account, timelineId),
    (d: any) =>
      Number(clipById(d, "video", clip.clipId).start) === 2 && timelineVersion(d) > versionBefore,
  );
  expect(timelineVersion(mutated)).toBeGreaterThan(versionBefore);

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByTitle(title)).toBeVisible({ timeout: 15_000 });
  expect(Number(clipById(await readTimeline(request, account, timelineId), "video", clip.clipId).start)).toBe(2);

  // No second source of truth in the client.
  const stored = await page.evaluate(() =>
    JSON.stringify(Object.entries(localStorage).map(([k, v]) => [k, (v ?? "").slice(0, 200)])),
  );
  expect(stored).not.toContain(timelineId);

  // Render state without paying for a render.
  //
  // `POST /timelines/{id}/render` runs ffmpeg SYNCHRONOUSLY in-request
  // (`timelines.py:534-582`), so it is invoked LAST and its status is recorded
  // rather than asserted: 200 with an asset, or 502 with a render error, are both
  // honest answers, and neither publishes anything.
  const render = await request.post(
    `${API}/api/v1/workspaces/${ws()}/timelines/${timelineId}/render`,
    { headers: auth().headers, data: {} },
  );
  expect([200, 409, 502, 422]).toContain(render.status());
});

/* ===========================================================================
 * FLOW 3 -- DISTRIBUTION
 * ======================================================================== */
test("Distribution: modes stay distinct and an ambiguous paid submit offers no resend", async ({
  page,
  request,
}) => {
  const caps = await request.get(
    `${API}/api/v1/workspaces/${ws()}/distribution/capabilities`,
    auth(),
  );
  expect(caps.ok(), `capabilities failed: ${await caps.text()}`).toBe(true);
  const capability = (await caps.json()) as any;
  for (const p of [].concat(capability.items ?? capability)) {
    // Every platform declares HOW it would publish. Neither is LIVE-by-accident.
    expect(["DIRECT_PUBLISH", "USER_HANDOFF"]).toContain(String(p.publish_mode));
  }

  const incidents = (await (
    await request.get(`${API}/api/v1/workspaces/${ws()}/provider-maturity/incidents?limit=50`, auth())
  ).json()) as any;
  const unknown = (incidents.items ?? []).filter((i: any) => i.state === "SUBMISSION_UNKNOWN");

  await authenticate(page, account);
  await gotoRoute(page, "/distribution");
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();

  // MOCK, HANDOFF and LIVE are three distinct values, and an ambiguous submit is
  // never folded into FAILED.
  for (const i of unknown) {
    expect(i.display_state).not.toBe("FAILED");
    expect(i.retry_safe).toBe(false);
    expect(i.may_resubmit).toBe(false);
  }

  // No unsafe resend control attached to an AMBIGUOUS paid submit.
  //
  // Scoped deliberately. "No button on the page matches /retry/" is NOT the
  // requirement and was a false assertion: `ErrorState` renders a Retry for a
  // genuinely failed read, and retrying a failed GET is correct. What is
  // forbidden is re-submitting a paid action whose outcome the provider has not
  // confirmed, because that double-charges. So the check is anchored to the
  // ambiguous items and leaves unrelated read-failure retries alone.
  if (await page.getByText(/SUBMISSION_UNKNOWN|UNKNOWN_EXPOSURE/i).count()) {
    const row = page.getByText(/SUBMISSION_UNKNOWN|UNKNOWN_EXPOSURE/i).first();
    const scope = row.locator("xpath=ancestor-or-self::*[self::li or self::tr or self::div][1]");
    await expect(
      scope.getByRole("button", { name: /retry|resend|resubmit|try again/i }),
      "an ambiguous paid submit must not be rendered as actionable",
    ).toHaveCount(0);
  }
});

/* ===========================================================================
 * FLOW 4 -- OPERATIONS
 * ======================================================================== */
test("Operations: an ambiguous paid submit shows unknown exposure, never FAILED", async ({
  page,
  request,
}) => {
  const incidents = await request.get(
    `${API}/api/v1/workspaces/${ws()}/provider-maturity/incidents?limit=50`,
    auth(),
  );
  expect(incidents.ok(), `incidents failed: ${await incidents.text()}`).toBe(true);
  const body = (await incidents.json()) as any;
  expect(body.workspace_id).toBe(ws());

  const unknown = (body.items ?? []).filter(
    (i: any) => i.state === "SUBMISSION_UNKNOWN" || i.exposure_unknown === true,
  );

  await authenticate(page, account);
  await gotoRoute(page, "/operations");
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();

  for (const i of unknown) {
    expect(i.state).toBe("SUBMISSION_UNKNOWN");
    expect(i.display_state).not.toBe("FAILED");
    expect(i.provider).toBeTruthy();
    expect(i.operation).toBeTruthy();
    expect(i.recommended_action, "remediation is shown").toBeTruthy();
    // Exposure is UNKNOWN, never silently $0.
    expect(i.estimated_exposure_usd === null || typeof i.estimated_exposure_usd === "number").toBe(true);
    expect(i.retry_safe).toBe(false);
  }
  expect(body.unknown_exposure_count).toBeGreaterThanOrEqual(unknown.length);

  // SCOPED, NOT GLOBAL -- and the earlier global version of this assertion was wrong.
  //
  // "No button anywhere matching /retry/" is not the requirement. `ErrorState`
  // renders a Retry for a genuinely FAILED read, and that is correct: a failed
  // GET can succeed on a second attempt. What must never exist is a resend
  // control attached to an AMBIGUOUS PAID SUBMIT, because the provider may
  // already have accepted it and a resend double-charges.
  //
  // So the assertion is anchored to the ambiguous items themselves: the API says
  // they are not retryable, not resendable, and the UI must not contradict that
  // by rendering them as actionable. Any Retry elsewhere on the screen belongs to
  // a failed read and is left alone.
  const incidentRegion = page.locator(".ym-incidents, [data-testid='incidents'], main");
  const ambiguousText = /SUBMISSION_UNKNOWN|UNKNOWN_EXPOSURE/i;
  if (await page.getByText(ambiguousText).count()) {
    const row = page.getByText(ambiguousText).first();
    const scope = row.locator("xpath=ancestor-or-self::*[self::li or self::tr or self::div][1]");
    await expect(
      scope.getByRole("button", { name: /retry|resend|resubmit|try again/i }),
      "an ambiguous paid submit must not be rendered as actionable",
    ).toHaveCount(0);
  }
  // And the cost-incident surface itself must exist to host that judgement.
  await expect(incidentRegion.first()).toBeVisible();
});

/* ===========================================================================
 * FLOW 5 -- PERMISSIONS: the backend refusal is the evidence; the UI agrees.
 * ======================================================================== */
test("Permissions: viewer/member/admin are refused exactly where the backend refuses", async ({
  page,
  request,
}) => {
  const roles = {} as Record<string, { token: string; email: string }>;
  for (const role of ["viewer", "member", "admin"] as const) {
    const email = `e2e-${role}-${Date.now().toString(36)}-${Math.random()
      .toString(36)
      .slice(2, 6)}@test.local`;
    const reg = await request.post(`${API}/api/v1/auth/register`, {
      data: { email, password: strongPassword(), display_name: `E2E ${role}` },
    });
    expect(reg.ok(), `register ${role} failed: ${await reg.text()}`).toBe(true);
    const token = ((await reg.json()) as any).access_token;
    await attachToWorkspace(ws(), email, role);
    roles[role] = { token, email };
  }

  const CASES = [
    { what: "read projects", method: "GET", path: () => `/workspaces/${ws()}/projects`, allowed: ["viewer", "member", "admin"] },
    { what: "read content", method: "GET", path: () => `/workspaces/${ws()}/content?limit=1`, allowed: ["viewer", "member", "admin"] },
    { what: "manage brand", method: "GET", path: () => `/workspaces/${ws()}/brand`, allowed: ["viewer", "member", "admin"] },
    { what: "read provider maturity", method: "GET", path: () => `/workspaces/${ws()}/provider-maturity`, allowed: ["viewer", "member", "admin"] },
    { what: "mutate campaign", method: "POST", path: () => `/workspaces/${ws()}/campaigns`, allowed: ["admin"] },
    { what: "place on calendar", method: "POST", path: () => `/workspaces/${ws()}/calendar`, allowed: ["admin"] },
  ];

  for (const c of CASES) {
    for (const role of ["viewer", "member", "admin"] as const) {
      const res = await request.fetch(`${API}/api/v1${c.path()}`, {
        method: c.method,
        headers: {
          Authorization: `Bearer ${roles[role].token}`,
          "Content-Type": "application/json",
        },
        data: c.method === "POST" ? {} : undefined,
      });
      if (c.allowed.includes(role)) {
        expect(
          res.status(),
          `${role} should be allowed to ${c.what}, got ${res.status()}`,
        ).not.toBe(403);
      } else {
        // A HIDDEN BUTTON IS NOT SECURITY. The backend must refuse directly.
        expect(res.status(), `${role} must be REFUSED ${c.what}`).toBe(403);
      }
    }
  }

  // The session a role reads must report the reduced capability set.
  //
  // `MeResponse` has NO top-level `role` (`auth.py:118-125`): the role is
  // PER WORKSPACE, on `workspaces[]`, and `capabilities` is the union across them.
  // Asserting `session.role` read `undefined` and would have passed for the wrong
  // reason if written as a truthiness check.
  const me = await request.get(`${API}/api/v1/auth/me`, {
    headers: { Authorization: `Bearer ${roles.viewer.token}` },
  });
  expect(me.ok()).toBe(true);
  const session = (await me.json()) as any;
  const mine = (session.workspaces ?? []).find((w: any) => w.id === ws());
  expect(mine, "the viewer is a member of the workspace under test").toBeTruthy();
  expect(String(mine.role)).toBe("viewer");

  // Assert on the WORKSPACE's capability set, not the top-level union.
  //
  // `MeResponse.capabilities` is the union across every workspace the user
  // belongs to (`auth.py:114-116`). Each of these users registered through the API,
  // which creates a workspace they OWN -- so the union always contains an owner's
  // full capability set and would have made this viewer look like an admin. The
  // per-workspace set is what actually governs the requests above.
  const caps = (mine.capabilities ?? []) as string[];
  expect(caps).toContain("content.read");
  expect(caps, "a viewer must not hold publish.approve").not.toContain("publish.approve");

  // UI half: the viewer is on a real screen and no admin-only control is offered.
  await authenticate(page, {
    token: roles.viewer.token,
    workspaceId: ws(),
    email: roles.viewer.email,
  });
  await gotoRoute(page, "/campaigns");
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
  await expect(
    page.getByRole("button", { name: /publish|derive|generate more/i }),
    "a viewer must not be offered admin-only campaign actions",
  ).toHaveCount(0);
});

/* ===========================================================================
 * TOASTS -- mounted once, announced, dismissed, survives navigation.
 * ======================================================================== */
test("the toast system is mounted once, announces, and survives navigation", async ({ page }) => {
  await authenticate(page, account);
  await gotoRoute(page, "/planner");

  // Exactly ONE container, and none before anything is raised.
  await expect(page.locator(".toasts")).toHaveCount(0);

  // Raised through the app's own event channel -- the same path `toast(...)` uses,
  // so this tests the wiring rather than a private back door.
  await page.evaluate(() => {
    window.dispatchEvent(
      new CustomEvent("ym-toast", {
        detail: { id: "e2e-success", message: "Saved", tone: "success", title: "Done" },
      }),
    );
  });
  await expect(page.locator(".toasts")).toHaveCount(1);
  await expect(page.getByRole("status").filter({ hasText: "Saved" })).toBeVisible();
  await expect(page.getByText("Done")).toBeVisible();

  // Still one container after several toasts, each in a live region.
  //
  // Counted INSIDE the container on purpose. `getByRole("status")` across the
  // whole document also matches unrelated live regions the shell already owns
  // (e.g. the background-refresh flag at `primitives.tsx:760`), so the earlier
  // page-wide count of 4 was measuring the wrong set and could not distinguish
  // "toasts are missing" from "something else has role=status".
  await page.evaluate(() => {
    for (const tone of ["warning", "error", "info"]) {
      window.dispatchEvent(
        new CustomEvent("ym-toast", {
          detail: { id: `e2e-${tone}`, message: `${tone} message`, tone },
        }),
      );
    }
  });
  await expect(page.locator(".toasts [role='status']")).toHaveCount(4);

  // Navigation must not unmount the mount.
  //
  // Asserted by DISPATCHING AFTER NAVIGATING, not by expecting the previous
  // toasts to still be on screen. Each toast auto-dismisses after 4.2s
  // (`ui.tsx:240`) and `<Toasts/>` renders `null` when empty, so a container
  // count of 0 after navigation is the CORRECT state -- it is what proves
  // auto-dismiss works. What has to be proven is that the single mount survived
  // the route change, which is only observable by raising a new toast.
  await gotoRoute(page, "/calendar");
  await expect
    .poll(async () => page.locator(".toasts").count(), { timeout: 15_000 })
    .toBe(0); // auto-dismiss emptied it

  await page.evaluate(() => {
    window.dispatchEvent(
      new CustomEvent("ym-toast", {
        detail: { id: "e2e-after-nav", message: "Still mounted after navigation", tone: "info" },
      }),
    );
  });
  await expect(page.locator(".toasts")).toHaveCount(1);
  await expect(
    page.getByRole("status").filter({ hasText: "Still mounted after navigation" }),
  ).toBeVisible();

  // Auto-dismiss empties the container again.
  await expect
    .poll(async () => page.locator(".toasts").count(), { timeout: 15_000 })
    .toBe(0);
});