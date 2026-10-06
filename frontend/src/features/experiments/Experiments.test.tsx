/** @vitest-environment jsdom */
/* Experiments — an absent result is not a null finding.
 *
 * The legacy Performance screen showed experiments next to campaign rollups and
 * let a reader conclude "no lift" from an experiment that had never been
 * analysed. Two failures are asserted against here:
 *
 *   1. an under-sampled experiment shows NO LESSON, with the count that blocked
 *      it — the same refusal as `engine/planning/feedback.py::derive_lesson`;
 *   2. an experiment with an empty `result_json` reports the result as
 *      UNAVAILABLE, never as "no effect", and never as a zero mean.
 *
 * A failed read must be an alert naming the failure, and must stay
 * distinguishable from an empty list.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const wsGet = vi.fn();
const wsPost = vi.fn();

vi.mock("../../lib/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
  wsApi: {
    get: (path: string) => wsGet(path),
    post: (path: string, body?: unknown) => wsPost(path, body),
  },
  api: vi.fn(),
}));

vi.mock("../../state/session", () => ({
  useSession: () => ({
    workspaceId: "ws-1",
    workspace: { id: "ws-1", name: "Test Workspace" },
    workspaces: [{ id: "ws-1", name: "Test Workspace" }],
    capabilities: ["content.read", "content.write", "operations.view"],
    capabilitiesKnown: true,
    switchWorkspace: () => {},
    reload: () => {},
  }),
  can: () => true,
  blockedReason: () => null,
}));

import Experiments, { type ExperimentRow } from "./Experiments";

afterEach(() => {
  cleanup();
  wsGet.mockReset();
  wsPost.mockReset();
});

/* `experiments.py::_dto` plus the `result_json` shapes `analyze_experiment`
   writes for each terminal status. */

/* Annotated so `started_at: null` widens to `string | null` — a spread that sets
   it to a string must stay assignable to the DTO the router actually returns. */
function draft(id: string, hypothesis: string): ExperimentRow {
  return {
    id,
    workspace_id: "ws-1",
    kind: "HOOK",
    hypothesis,
    control: { variant_ref: "cut-a" },
    variants: [{ variant_ref: "cut-b", descriptor: "Question hook" }],
    platform: "youtube",
    primary_metric: "views",
    secondary_metrics: [],
    minimum_sample: 60,
    status: "DRAFT",
    /* result_json default: never analysed. */
    result: {},
    confidence: "",
    started_at: null,
    ended_at: null,
    created_at: "2026-01-02T09:00:00Z",
    updated_at: "2026-01-02T09:00:00Z",
  };
}

/* INSUFFICIENT_DATA: total 4 < minimum_sample 60. */
function underSampled(id: string, hypothesis: string): ExperimentRow {
  return {
    ...draft(id, hypothesis),
    status: "INSUFFICIENT_DATA",
    confidence: "n/a",
    started_at: "2026-01-02T09:00:00Z",
    result: {
      primary_metric: "views",
      control: { variant_ref: "cut-a", n: 2, mean: 900 },
      arms: [
        {
          variant_ref: "cut-b",
          descriptor: "Question hook",
          arm: "variant_0",
          n: 2,
          mean: 1200,
          abs_diff: 300,
          rel_diff: 0.3333,
          /* No interval: n < 30/arm. */
          ci_low: null,
          ci_high: null,
          significant: false,
          reason: "n too small for a confidence interval (control n=2, variant n=2; need >=30/arm)",
        },
      ],
      winner: null,
      reason: "only 4 sample(s) across arms; need minimum_sample=60",
      minimum_sample: 60,
      total_samples: 4,
      analyzed_at: "2026-01-03T09:00:00Z",
    },
  };
}

/* COMPLETED with a real interval that excludes zero. */
function completed(id: string, hypothesis: string): ExperimentRow {
  return {
    ...draft(id, hypothesis),
    status: "COMPLETED",
    confidence: "95% CI excludes zero",
    started_at: "2026-01-02T09:00:00Z",
    ended_at: "2026-01-04T09:00:00Z",
    result: {
      primary_metric: "views",
      control: { variant_ref: "cut-a", n: 40, mean: 900 },
      arms: [
        {
          variant_ref: "cut-b",
          descriptor: "Question hook",
          arm: "variant_0",
          n: 40,
          mean: 1200,
          abs_diff: 300,
          rel_diff: 0.3333,
          ci_low: 210.5,
          ci_high: 389.5,
          significant: true,
          reason: "",
        },
      ],
      winner: "cut-b",
      reason: "variant 'cut-b' lifts views by 33.3% (95% CI excludes zero)",
      minimum_sample: 60,
      total_samples: 80,
      analyzed_at: "2026-01-04T09:00:00Z",
    },
  };
}

/* One arm with zero samples: `_mean([])` returns 0.0. */
function emptyArm(id: string, hypothesis: string): ExperimentRow {
  return {
    ...draft(id, hypothesis),
    status: "INCONCLUSIVE",
    confidence: "n/a",
    started_at: "2026-01-02T09:00:00Z",
    result: {
      primary_metric: "views",
      control: { variant_ref: "cut-a", n: 40, mean: 900 },
      arms: [
        {
          variant_ref: "cut-b",
          descriptor: "Question hook",
          arm: "variant_0",
          n: 0,
          mean: 0,
          abs_diff: -900,
          rel_diff: -1,
          ci_low: null,
          ci_high: null,
          significant: false,
          reason: "n too small for a confidence interval (control n=40, variant n=0; need >=30/arm)",
        },
      ],
      winner: null,
      reason: "at least one arm has no metric samples — comparison unmet",
      minimum_sample: 2,
      total_samples: 40,
      analyzed_at: "2026-01-04T09:00:00Z",
    },
  };
}

type Row = ExperimentRow;

function serve(items: Row[]) {
  wsGet.mockImplementation((path: string) => {
    if (path === "/experiments") return Promise.resolve({ total: items.length, items });
    const detail = items.find((i) => path === `/experiments/${i.id}`);
    if (detail) return Promise.resolve(detail);
    return Promise.resolve({});
  });
}

function fail(predicate: (p: string) => boolean, message: string) {
  wsGet.mockImplementation((path: string) =>
    predicate(path) ? Promise.reject(new Error(message)) : Promise.resolve({ total: 0, items: [] }),
  );
}

function renderExperiments() {
  return render(<Experiments />);
}

/** The `.ym-panel` whose heading is exactly `title` — panels nest, so no ids. */
function panelByTitle(title: string): HTMLElement {
  const heading = screen.getAllByText(title).find((n) => n.classList.contains("ym-panel-title"));
  if (!heading) throw new Error(`no panel titled "${title}"`);
  return heading.closest(".ym-panel") as HTMLElement;
}

/** Open a row and wait for the detail panels to mount. */
async function openExperiment(hypothesis: string) {
  await userEvent.click(await screen.findByText(hypothesis));
  // "Minimum sample" only exists in the detail panels.
  await screen.findByText("Minimum sample");
}

/** The arms table lives inside the Result panel; it is titled by its caption. */
function armsPanel(): HTMLElement {
  const caption = screen.getByText("Experiment arms");
  return caption.closest(".ym-panel") as HTMLElement;
}

/** Cells of one arm row, in the order the table renders them. */
function armCells(arm: string): string[] {
  const row = within(armsPanel()).getByText(arm).closest("tr");
  return Array.from(row?.querySelectorAll("td") ?? []).map((c) => c.textContent ?? "");
}

const COL_SAMPLE = 3;
const COL_MEAN = 4;
const COL_CI = 6;

describe("Experiments renders when data arrives", () => {
  it("shows the hypothesis and the arms", async () => {
    serve([completed("exp-1", "Question hooks lift 3s retention")]);
    renderExperiments();

    expect(await screen.findByText("Question hooks lift 3s retention")).toBeInTheDocument();
    // "Completed" is both a summary tile label and the row status badge.
    expect(screen.getAllByText("Completed").length).toBeGreaterThan(0);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("opens an experiment and shows its arms and sample", async () => {
    serve([completed("exp-1", "Question hooks lift 3s retention")]);
    renderExperiments();

    await openExperiment("Question hooks lift 3s retention");

    expect(await screen.findByText("Experiment arms")).toBeInTheDocument();
    expect(screen.getByText("control")).toBeInTheDocument();
    expect(screen.getByText("variant_0")).toBeInTheDocument();
    expect(armCells("variant_0")[COL_SAMPLE]).toMatch(/^40/);
  });

  it("reports the backend's confidence label verbatim", async () => {
    serve([completed("exp-1", "Question hooks lift 3s retention")]);
    renderExperiments();
    await openExperiment("Question hooks lift 3s retention");
    expect(screen.getAllByText("95% CI excludes zero").length).toBeGreaterThan(0);
    // No probability, p-value or confidence percentage is invented here.
    expect(document.body.textContent).not.toMatch(/p-value|p = |significance level/i);
  });
});

describe("an under-sampled experiment shows no lesson", () => {
  it("withholds the lesson and names the sample gate that blocked it", async () => {
    serve([underSampled("exp-low", "Question hooks lift 3s retention")]);
    renderExperiments();

    await openExperiment("Question hooks lift 3s retention");

    const lesson = panelByTitle("Lesson");
    await waitFor(() => expect(lesson.textContent).toMatch(/UNAVAILABLE/i));
    expect(lesson.textContent).toMatch(/below this experiment's minimum_sample of 60/i);
    expect(lesson.textContent).toMatch(/4 sample\(s\) across arms/i);
  });

  it("does not claim the under-sampled experiment has no effect", async () => {
    serve([underSampled("exp-low", "Question hooks lift 3s retention")]);
    renderExperiments();
    await openExperiment("Question hooks lift 3s retention");

    // The row-level badge says "none yet" and carries the blocking reason.
    const badge = screen
      .getAllByTitle(/below this experiment's minimum_sample of 60/i)
      .find((b) => b.textContent?.includes("none yet"));
    expect(badge).toBeDefined();
    // The Result panel reports the backend's own refusal, not a verdict.
    expect(panelByTitle("Result").textContent).toMatch(/only 4 sample\(s\) across arms/);
  });

  it("issues the lesson only for a completed analysis above the gate", async () => {
    serve([completed("exp-1", "Question hooks lift 3s retention")]);
    renderExperiments();
    await openExperiment("Question hooks lift 3s retention");

    const lesson = panelByTitle("Lesson");
    expect(lesson.textContent).toMatch(/ISSUED/);
    expect(lesson.textContent).toMatch(/33\.3%/);
    // The claim is qualified: an observation, not a forecast.
    expect(lesson.textContent).toMatch(/not\s+a forecast/i);
  });

  it("withholds the lesson for an inconclusive analysis too", async () => {
    serve([emptyArm("exp-inc", "Question hooks lift 3s retention")]);
    renderExperiments();
    await openExperiment("Question hooks lift 3s retention");

    const lesson = panelByTitle("Lesson");
    await waitFor(() => expect(lesson.textContent).toMatch(/UNAVAILABLE/i));
    expect(lesson.textContent).toMatch(/no metric samples/i);
  });
});

describe("an absent result is unavailable, not zero", () => {
  it("reports an un-analysed experiment's result as UNAVAILABLE", async () => {
    serve([draft("exp-draft", "Question hooks lift 3s retention")]);
    renderExperiments();

    await openExperiment("Question hooks lift 3s retention");

    expect(await screen.findByText(/An absent result is/i)).toBeInTheDocument();
    expect(document.body.textContent).toMatch(/has not been tested yet/i);
    const result = panelByTitle("Result");
    expect(result.textContent).toMatch(/UNAVAILABLE/);
    expect(result.textContent).toMatch(/no analysis has ever run/i);
  });

  it("renders an arm mean as UNAVAILABLE when that arm has no samples", async () => {
    serve([emptyArm("exp-inc", "Question hooks lift 3s retention")]);
    renderExperiments();
    await openExperiment("Question hooks lift 3s retention");
    const cells = armCells("variant_0");
    // The backend sent mean 0.0 for an arm with n = 0. A sample of 0 is a real
    // count; a mean of 0 is `_mean([])` and must not be painted as a figure.
    expect(cells[COL_SAMPLE]).toMatch(/^0/);
    expect(cells[COL_MEAN]).toMatch(/UNAVAILABLE/);
  });

  it("renders the confidence interval as UNAVAILABLE with the backend's reason", async () => {
    serve([underSampled("exp-low", "Question hooks lift 3s retention")]);
    renderExperiments();
    await openExperiment("Question hooks lift 3s retention");

    const row = within(armsPanel()).getByText("variant_0").closest("tr");
    const ci = row?.querySelectorAll("td")[COL_CI];
    expect(ci?.textContent).toMatch(/UNAVAILABLE/);
    expect(ci?.querySelector("[title]")?.getAttribute("title")).toMatch(/need >=30\/arm/);
  });
});

describe("a failed endpoint is an alert, never an empty list", () => {
  it("names the failure of the experiment list", async () => {
    fail((p) => p === "/experiments", "experiments backend unavailable");
    renderExperiments();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/experiments backend unavailable/i);
  });

  it("keeps the failure distinguishable from emptiness", async () => {
    fail((p) => p === "/experiments", "experiments backend unavailable");
    renderExperiments();

    await screen.findByRole("alert");
    expect(document.body.textContent).not.toMatch(/no experiment yet/i);
  });

  it("refuses to report zero experiments when the read failed", async () => {
    fail((p) => p === "/experiments", "experiments backend unavailable");
    renderExperiments();

    await screen.findByRole("alert");
    const tiles = Array.from(document.querySelectorAll(".ym-stat")).map((t) => t.textContent ?? "");
    expect(tiles.some((t) => /ExperimentsUNAVAILABLE/.test(t.replace(/\s+/g, "")))).toBe(true);
  });
});

describe("Experiments is not the Performance screen", () => {
  it("shows no performance-report section and claims nothing about channel rollups", async () => {
    serve([completed("exp-1", "Question hooks lift 3s retention")]);
    renderExperiments();

    await screen.findByText("Question hooks lift 3s retention");
    // Rollups, retention curves and platform comparisons belong to Analytics.
    expect(document.body.textContent).not.toMatch(/retention curve|platform comparison|campaign rollup/i);
  });
});
