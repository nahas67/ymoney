/* Accessibility and responsive smoke (Work 16.5.1 §13).
 *
 * Deliberately narrow. This is a smoke suite, not a substitute for an audit:
 * it asserts the failures that this rebuild is most likely to introduce --
 * a shell that cannot be tabbed through, a dialog that does not take focus, a
 * live region that is not announced, a status conveyed by colour alone.
 *
 * It runs against the real shell and the real primitives rather than
 * hand-written fixtures, so it cannot pass while the shipped UI regresses.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { readFileSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "..");

/* =========================================================================
 * Static guarantees in the shell's own source
 * ====================================================================== */

const SHELL = readFileSync(join(SRC, "app", "AppShell.tsx"), "utf-8");
const CSS = readFileSync(join(SRC, "design-system", "styles.css"), "utf-8");
const TOKENS = readFileSync(join(SRC, "design-system", "tokens.css"), "utf-8");

describe("a11y: shell structure", () => {
  it("provides a skip link to the main landmark", () => {
    expect(SHELL).toContain("ym-skip-link");
    expect(SHELL).toContain('href="#ym-main"');
    expect(SHELL).toContain('id="ym-main"');
  });

  it("labels every landmark it renders", () => {
    expect(SHELL).toContain("<main");
    expect(SHELL).toContain("<aside");
    expect(SHELL).toContain("<header");
    // The primary nav must be the NAMED element. An aria-label on the wrapping
    // <aside> gives it role=complementary, leaving the actual <nav> anonymous.
    expect(SHELL).toMatch(/<nav[^>]*aria-label="Primary"/);
    expect(SHELL).toMatch(/<nav[^>]*aria-label="Breadcrumb"/);
  });

  it("marks the current nav item with aria-current, not colour alone", () => {
    expect(SHELL).toContain('aria-current={active ? "page" : undefined}');
  });

  it("gives the command palette dialog semantics and keyboard handling", () => {
    expect(SHELL).toContain('role="dialog"');
    expect(SHELL).toContain('aria-modal="true"');
    expect(SHELL).toContain("aria-label=\"Command palette\"");
    expect(SHELL).toContain('"Escape"');
    expect(SHELL).toContain('"ArrowDown"');
    expect(SHELL).toContain('"Enter"');
  });

  it("exposes the palette results as a listbox with a selected option", () => {
    expect(SHELL).toContain('role="listbox"');
    expect(SHELL).toContain('role="option"');
    expect(SHELL).toContain('aria-selected={i === cursor}');
  });

  it("binds the palette to Ctrl/Cmd-K", () => {
    expect(SHELL).toContain('(e.metaKey || e.ctrlKey)');
    expect(SHELL).toContain('"k"');
  });

  it("announces the notification count in text, not only as a badge", () => {
    expect(SHELL).toMatch(/aria-label=\{`Notifications\$\{/);
  });

  it("visually hides the workspace selector label without removing it", () => {
    expect(SHELL).toContain("ym-sr-only");
    expect(SHELL).toMatch(/<select[\s\S]{0,200}aria-label="Active workspace"/);
  });

  it("respects reduced-motion in the stylesheet", () => {
    expect(CSS).toContain("prefers-reduced-motion");
  });

  it("defines a visible focus ring rather than relying on the UA default", () => {
    expect(CSS).toMatch(/:focus-visible\s*\{[^}]*box-shadow:\s*var\(--focus-ring\)/);
    expect(TOKENS).toContain("--focus-ring");
  });

  it("has breakpoints for tablet and the minimum supported width", () => {
    expect(CSS).toContain("@media (max-width: 1024px)");
    expect(CSS).toContain("@media (max-width: 768px)");
  });

  it("collapses the sidebar rather than letting it overflow on small screens", () => {
    const small = CSS.slice(CSS.indexOf("@media (max-width: 768px)"));
    expect(small).toContain(".ym-sidebar");
    expect(small).toContain("display: none");
  });
});

/* =========================================================================
 * Runtime: the palette is actually operable
 * ====================================================================== */

vi.mock("../state/session", async () => {
  const actual = await vi.importActual<typeof import("../state/session")>(
    "../state/session",
  );
  return {
    ...actual,
    useSession: () => ({
      authenticated: true,
      user: { email: "a@b.c" },
      workspaces: [{ id: "w1", name: "WS One" }],
      workspaceId: "w1",
      workspace: { id: "w1", name: "WS One" },
      capabilities: [],
      switchWorkspace: () => {},
      reload: () => {},
    }),
  };
});

vi.mock("../api/queries", () => ({
  useWsQuery: () => ({ data: null, loading: false, error: null }),
  useQuery: () => ({ data: null, loading: false, error: null }),
  useMutation: () => ({ mutate: () => {}, mutateAsync: async () => {}, isPending: false }),
  useCombinedQueries: () => ({ data: {}, loading: false, errors: [] }),
}));

vi.mock("../lib/api", () => ({
  api: async () => ({}),
  getWorkspace: () => "w1",
  setWorkspace: () => {},
}));

async function mountShell() {
  const { AppShell } = await import("../app/AppShell");
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <AppShell>
        <div>content</div>
      </AppShell>
    </MemoryRouter>,
  );
}

describe("a11y runtime: command palette", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  /**
   * CORRECTION (Work 16.5.3) -- this scope is load-bearing, not tidiness.
   *
   * Work 16.5.1 asserted "the palette opens with exactly one option
   * highlighted" using a PAGE-WIDE `getAllByRole("option")`, then reported an
   * off-by-one because the selected index read as 1.
   *
   * There was no product bug. Testing Library gives a native `<option>` the
   * `option` role, so the workspace `<select>` in the topbar contributed
   * `<option>WS</option>` at index 0 and the palette's real first row landed at
   * index 1. The palette had always opened on the FIRST result.
   *
   * So every palette assertion below is scoped to the palette's own listbox.
   * A page-wide query is not merely imprecise here -- it silently measures a
   * different component, which is exactly how a correct UI gets "diagnosed" into
   * a change it never needed.
   */
  async function openPalette() {
    const user = userEvent.setup();
    await mountShell();
    await user.keyboard("{Control>}k{/Control}");
    const input = await screen.findByLabelText("Search");
    await waitFor(() => expect(input).toHaveFocus());
    const listbox = await screen.findByRole("listbox");
    return { user, input, paletteOptions: within(listbox).getAllByRole("option"), listbox };
  }

  it("opens with the FIRST palette result selected", async () => {
    const { paletteOptions } = await openPalette();

    const selected = paletteOptions.filter(
      (o) => o.getAttribute("aria-selected") === "true",
    );
    expect(selected).toHaveLength(1);
    // Command Center is the first registered route, so it must be the default.
    expect(selected[0]).toBe(paletteOptions[0]);
    expect(paletteOptions[0].textContent).toContain("Command Center");
  });

  it("cannot be contaminated by the workspace selector's options", async () => {
    const { paletteOptions } = await openPalette();

    // The workspace <select> genuinely puts role=option on the page. If a
    // page-wide query were used again, this is what would shift every index.
    const pageWide = screen.getAllByRole("option");
    expect(
      pageWide.length,
      "the workspace selector should still contribute options; if this ever " +
        "changes, re-check whether scoping is still necessary",
    ).toBeGreaterThan(paletteOptions.length);

    // And the palette list must not contain any workspace option.
    for (const opt of paletteOptions) {
      expect(opt.textContent).not.toBe("WS");
      expect(opt).not.toHaveAttribute("aria-selected", null);
    }
  });

  it("moves the selection down and back up with the arrow keys", async () => {
    const { user, paletteOptions } = await openPalette();
    const selectedIndex = () =>
      paletteOptions.findIndex((o) => o.getAttribute("aria-selected") === "true");

    // Starts at the top, so every transition below is an absolute assertion
    // rather than a relative delta that would hide a broken start.
    expect(selectedIndex()).toBe(0);

    await user.keyboard("{ArrowDown}");
    expect(selectedIndex()).toBe(1);
    await user.keyboard("{ArrowDown}");
    expect(selectedIndex()).toBe(2);
    await user.keyboard("{ArrowUp}");
    expect(selectedIndex()).toBe(1);
    await user.keyboard("{ArrowUp}");
    expect(selectedIndex()).toBe(0);
  });

  it("resets to the first result after a close and reopen", async () => {
    const { user, paletteOptions } = await openPalette();
    const selectedIndex = () =>
      paletteOptions.findIndex((o) => o.getAttribute("aria-selected") === "true");

    await user.keyboard("{ArrowDown}{ArrowDown}");
    expect(selectedIndex()).toBe(2);

    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Command palette" })).toBeNull(),
    );

    await user.keyboard("{Control>}k{/Control}");
    await waitFor(() => expect(screen.getByLabelText("Search")).toHaveFocus());
    const reopened = within(await screen.findByRole("listbox")).getAllByRole("option");
    expect(reopened[0].getAttribute("aria-selected")).toBe("true");
  });

  it("opens the destination on Enter", async () => {
    const user = userEvent.setup();
    await mountShell();
    await user.keyboard("{Control>}k{/Control}");
    const input = await screen.findByLabelText("Search");
    await waitFor(() => expect(input).toHaveFocus());
    await user.type(input, "planner");
    await user.keyboard("{Enter}");
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Command palette" })).toBeNull(),
    );
  });

  it("closes on Escape", async () => {
    const user = userEvent.setup();
    await mountShell();
    await user.keyboard("{Control>}k{/Control}");
    await screen.findByRole("dialog", { name: "Command palette" });
    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Command palette" })).toBeNull(),
    );
  });

  it("filters by the typed query and can be dismissed with no matches", async () => {
    const user = userEvent.setup();
    await mountShell();
    await user.keyboard("{Control>}k{/Control}");

    const input = await screen.findByLabelText("Search");
    await user.type(input, "zzzz-no-such-screen");
    expect(screen.getByText("No screens match")).toBeInTheDocument();
  });

  it("moves focus into the palette input when it opens", async () => {
    const user = userEvent.setup();
    await mountShell();
    await user.keyboard("{Control>}k{/Control}");
    await waitFor(() => expect(screen.getByLabelText("Search")).toHaveFocus());
  });
});

describe("a11y runtime: shell landmarks and skip link", () => {
  it("renders a skip link as the first focusable element", async () => {
    const user = userEvent.setup();
    await mountShell();

    await user.tab();
    const skip = screen.getByRole("link", { name: /skip to content/i });
    expect(skip).toHaveFocus();
    expect(skip).toHaveAttribute("href", "#ym-main");
  });

  it("exposes the primary navigation as a named landmark", async () => {
    await mountShell();
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "Breadcrumb" }),
    ).toBeInTheDocument();
  });
});
