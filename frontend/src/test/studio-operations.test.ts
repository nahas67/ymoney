/* Studio operation tests (Work 16.5.1 §10, §15).
 *
 * §10 is explicit that the timeline ENGINE must not be rebuilt: the canonical
 * `timelineAdapter` op vocabulary and `inverseOps` undo/redo have to survive, and
 * "no UI-only canonical timeline" is an automatic failure.
 *
 * A UI-only timeline is easy to ship by accident -- a component that keeps its own
 * `ops` array and its own undo stack looks like the editor and quietly diverges
 * from every Work 02/13.1 guarantee. The only reliable guard is to assert the
 * delegation structurally, so these tests read the Studio shell's source and
 * assert what it does and does NOT contain.
 */

import { describe, it, expect } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "..");

const EDITOR = readFileSync(join(SRC, "features", "studio", "StudioEditor.tsx"), "utf-8");
const STUDIO = readFileSync(join(SRC, "features", "studio", "Studio.tsx"), "utf-8");
const ADAPTER = resolve(SRC, "editor", "adapters", "timelineAdapter.ts");

describe("Studio delegates to the canonical engine", () => {
  it("the canonical adapter still exists and is the one imported", () => {
    expect(existsSync(ADAPTER)).toBe(true);
    expect(EDITOR).toMatch(
      /from\s+"\.\.\/\.\.\/editor\/adapters\/timelineAdapter"/,
    );
  });

  it("renders the canonical editor rather than a lookalike", () => {
    expect(EDITOR).toMatch(/import CanonicalEditor from "\.\.\/\.\.\/pages\/Editor"/);
    expect(EDITOR).toMatch(/<CanonicalEditor\s*\/>/);
  });

  it("reads the timeline through the adapter's own helpers", () => {
    // TRACK_ORDER / TRACK_FAMILY / sortedTracks are the canonical ordering. If
    // the shell sorted tracks itself the two would disagree the moment the
    // adapter changed.
    for (const symbol of ["TRACK_FAMILY", "TRACK_ORDER", "sortedTracks"]) {
      expect(EDITOR, `${symbol} is not imported from the adapter`).toContain(symbol);
    }
  });

  it("keeps the route param name the canonical editor reads", () => {
    // pages/Editor does `const { timelineId } = useParams()`. If the new route
    // renamed the segment, the engine would mount and then load nothing.
    const canonical = readFileSync(join(SRC, "pages", "Editor.tsx"), "utf-8");
    expect(canonical).toContain("const { timelineId } = useParams()");

    const registry = readFileSync(join(SRC, "routes", "registry.ts"), "utf-8");
    expect(registry).toContain("/studio/:timelineId");
  });
});

describe("Studio does not fork the timeline", () => {
  it("holds no local op store", () => {
    // A component-owned op array is the signature of a forked timeline.
    expect(EDITOR).not.toMatch(/const\s+ops\s*(=|:\s*useState)/);
    expect(EDITOR).not.toMatch(/useState[^\n]*\[\s*\]\s*as\s+TimelineOp/);
    expect(EDITOR).not.toMatch(/TimelineOp\[\]/);
  });

  it("implements no undo/redo of its own", () => {
    // inverseOps must stay in the adapter. A local undo stack is the duplicate.
    expect(EDITOR).not.toMatch(/\bundoStack\b/);
    expect(EDITOR).not.toMatch(/\bredoStack\b/);
    expect(EDITOR).not.toMatch(/const\s+undo\s*=/);
    expect(EDITOR).not.toMatch(/const\s+redo\s*=/);
  });

  it("calls inverseOps nowhere -- it does not compute inverses itself", () => {
    expect(EDITOR).not.toMatch(/inverseOps\s*\(/);
  });

  it("does not import the adapter's mutating entry points", () => {
    // Read-only helpers only. If the shell could apply ops it would be a second
    // writer, and two writers is how a stale write becomes silent data loss.
    expect(EDITOR).not.toMatch(
      /import\s*\{[^}]*\b(applyOps|applyOp|commitOps|reduceOps)\b[^}]*\}\s*from\s*"[^"]*timelineAdapter"/,
    );
  });
});

describe("Studio shell states the required layout", () => {
  it("declares the three-pane-over-timeline arrangement", () => {
    const combined = `${STUDIO}\n${EDITOR}`;
    expect(combined).toMatch(/Assets|Scenes/);
    expect(combined).toMatch(/Preview/i);
    expect(combined).toMatch(/Inspector/i);
    expect(combined).toMatch(/Timeline/);
  });

  it("documents that the preserved behaviour is the engine's, not reimplemented", () => {
    expect(EDITOR).toMatch(/inverseOps/);
    expect(EDITOR).toMatch(/none of it reimplemented here/);
  });
});