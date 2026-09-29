/* CommandRenderer — schema-driven generative UI (Work 08, Lane D).
 *
 * This is the INTERNAL alternative to a json-render style library (OSS
 * decision recorded in docs/oss/OSS_COMPONENTS.md: we ship our own whitelist
 * renderer instead of adding a dependency that evaluates generated JSON).
 *
 * Safety contract (mirrors POST /creative/validate-schema on the backend):
 *   * only the APPROVED_COMPONENTS names below may ever render — the catalog's
 *     `component` field is looked up in COMPONENT_MAP and nothing else;
 *   * an unknown component name renders a rejected placeholder (never executed,
 *     never mounted) — the document is inert data either way;
 *   * NO dangerouslySetInnerHTML, NO eval/new Function, NO dynamic import of
 *     generated code, NO `new Function`-style template strings;
 *   * values are displayed as text nodes only (React escapes them).
 *
 * The catalog itself comes from GET /creative/catalog, which is the entire
 * contract the generative UI may consume (approved command types + component
 * descriptors + hard constraints).
 */
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { wsApi } from "../lib/api";
import { Badge } from "./ui";

/** The ONLY component names the generative UI may render (backend contract). */
export const APPROVED_COMPONENTS = [
  "SceneInspector",
  "HookComparison",
  "VariantCard",
  "BrandCheck",
  "CaptionControl",
  "VoiceSelector",
  "AssetCandidate",
  "TimelineJump",
  "CostEstimate",
  "ApplyChange",
] as const;

export type ApprovedComponentName = (typeof APPROVED_COMPONENTS)[number];

export type CatalogField = {
  name?: string;
  type?: string;
  component?: string;
  description?: string;
  options?: string[];
  required?: boolean;
  default?: unknown;
};

export type CatalogCommand = {
  type: string;
  description?: string;
  risk?: string;
  auto_apply?: boolean;
  reversible?: boolean;
  scopes?: string[];
  affected_artifacts?: string[];
  fields?: CatalogField[];
};

export type CreativeCatalog = {
  commands?: CatalogCommand[];
  components?: string[];
  scopes?: string[];
  statuses?: string[];
  platforms?: string[];
  supported_aspects?: string[];
  hard_constraints?: Record<string, string[]>;
  auto_apply?: { enabled?: boolean; types?: string[]; note?: string };
  policy_source?: string;
  workspace_id?: string;
};

type RenderContext = {
  field?: CatalogField;
  value?: unknown;
  estimate?: { rerender_seconds?: number; cost_usd?: number; heuristic?: string };
  hardConstraints?: Record<string, string[]>;
  onJump?: (target: Record<string, unknown>) => void;
};

/* ------------------------------------------------------------------ */
/* value helpers — text only, React escapes everything                  */
/* ------------------------------------------------------------------ */

function fmt(value: unknown): string {
  if (value == null) return "—";
  if (Array.isArray(value)) return value.length ? value.map((v) => fmt(v)).join(", ") : "—";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return "—";
    }
  }
  return String(value);
}

function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline gap-2 py-1" style={{ borderBottom: "var(--seam)" }}>
      <span className="font-mono text-[11px] uppercase tracking-wide shrink-0" style={{ color: "var(--text-faint)", minWidth: 120 }}>
        {label}
      </span>
      <span className="text-[12.5px] min-w-0 break-words flex-1">{children}</span>
      {hint && <span className="text-[11px]" style={{ color: "var(--text-faint)" }}>{hint}</span>}
    </div>
  );
}

function Chips({ items }: { items: unknown }) {
  const list = Array.isArray(items) ? items : items == null || items === "" ? [] : [items];
  if (!list.length) return <span style={{ color: "var(--text-faint)" }}>—</span>;
  return (
    <span className="flex flex-wrap gap-1.5">
      {list.map((it, i) => (
        <span key={i} className="chip !text-[11.5px] !py-0.5">{fmt(it)}</span>
      ))}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* the approved component map (the whitelist)                           */
/* ------------------------------------------------------------------ */

function SceneInspector({ field, value }: RenderContext) {
  const desc = field?.description ?? "Scene target";
  if (Array.isArray(value)) return <Chips items={value} />;
  if (typeof value === "string" && value) return <span>{value}</span>;
  return <span style={{ color: "var(--text-faint)" }}>{desc}</span>;
}

function HookComparison({ field, value }: RenderContext) {
  if (typeof value === "string" && value) {
    return (
      <span className="rounded-md px-2 py-1 inline-block" style={{ background: "var(--bg-subtle)", border: "var(--seam)" }}>
        “{value}”
      </span>
    );
  }
  return <span style={{ color: "var(--text-faint)" }}>{field?.description ?? "replacement hook"}</span>;
}

function VariantCard({ field, value }: RenderContext) {
  if (value == null || value === "") return <span style={{ color: "var(--text-faint)" }}>{field?.description ?? "variant"}</span>;
  return <Badge tone="info">{fmt(value)}</Badge>;
}

function BrandCheck({ field, value, hardConstraints }: RenderContext) {
  const presets = hardConstraints?.brand_presets ?? [];
  const name = fmt(value);
  const known = !presets.length || presets.some((p) => p.toLowerCase() === name.toLowerCase());
  return (
    <span className="flex items-center gap-2 flex-wrap">
      <span className="font-mono">{name}</span>
      <Badge tone={known ? "success" : "error"}>{known ? "brand preset ok" : "not configured"}</Badge>
      {presets.length > 0 && <span className="text-[11px]" style={{ color: "var(--text-faint)" }}>available: {presets.join(", ")}</span>}
      {field?.required && <Badge tone="muted">required</Badge>}
    </span>
  );
}

function CaptionControl({ field, value }: RenderContext) {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return (
      <span className="flex flex-wrap gap-1.5">
        {Object.entries(value as Record<string, unknown>).map(([k, v]) => (
          <span key={k} className="chip !text-[11.5px] !py-0.5 font-mono">{k}={fmt(v)}</span>
        ))}
      </span>
    );
  }
  if (value == null || value === "") return <span style={{ color: "var(--text-faint)" }}>{field?.description ?? "caption style"}</span>;
  return <span className="font-mono">{fmt(value)}</span>;
}

function VoiceSelector({ value, hardConstraints }: RenderContext) {
  const id = fmt(value);
  const approved = hardConstraints?.approved_voices ?? [];
  if (id === "—" || !id) return <span style={{ color: "var(--text-faint)" }}>no voice selected</span>;
  const ok = !approved.length || approved.some((v) => v.toLowerCase() === id.toLowerCase());
  return (
    <span className="flex items-center gap-2 flex-wrap">
      <span className="font-mono">{id}</span>
      <Badge tone={ok ? "success" : "error"}>{ok ? "approved voice" : "not in approved_voices"}</Badge>
    </span>
  );
}

function AssetCandidate({ field, value }: RenderContext) {
  if (value == null || value === "") return <span style={{ color: "var(--text-faint)" }}>{field?.description ?? "asset ref"}</span>;
  return (
    <span className="flex items-center gap-2">
      <span className="font-mono">{fmt(value)}</span>
      <Badge tone="muted">workspace asset ref</Badge>
    </span>
  );
}

function TimelineJump({ value, onJump, field }: RenderContext) {
  const id = fmt(value);
  if (id === "—" || !id) return <span style={{ color: "var(--text-faint)" }}>{field?.description ?? "timeline target"}</span>;
  return (
    <span className="flex items-center gap-2 flex-wrap">
      <span className="font-mono text-[12px]">{id}</span>
      {onJump && (
        <button className="btn-ghost !text-[11px] !py-0.5" onClick={() => onJump({ field: field?.name, value })}>
          Jump →
        </button>
      )}
    </span>
  );
}

function CostEstimate({ value, estimate, field }: RenderContext) {
  const seconds = estimate?.rerender_seconds;
  const cost = estimate?.cost_usd;
  return (
    <span className="flex items-center gap-2 flex-wrap">
      <span className="font-mono">{field?.name === "seconds" ? `${fmt(value)}s` : fmt(value)}</span>
      {seconds != null && <Badge tone="info">~{seconds}s rerender</Badge>}
      {cost != null && <Badge tone="warning">~${cost} est.</Badge>}
      {!estimate && <span className="text-[11px]" style={{ color: "var(--text-faint)" }}>preview to estimate</span>}
    </span>
  );
}

function ApplyChange({ field, value }: RenderContext) {
  if (value == null || value === "") return <span style={{ color: "var(--text-faint)" }}>{field?.description ?? "change payload"}</span>;
  return <span>{fmt(value)}</span>;
}

/** The whitelist. Keys MUST stay exactly equal to APPROVED_COMPONENTS. */
export const COMPONENT_MAP: Record<ApprovedComponentName, (ctx: RenderContext) => ReactNode> = {
  SceneInspector,
  HookComparison,
  VariantCard,
  BrandCheck,
  CaptionControl,
  VoiceSelector,
  AssetCandidate,
  TimelineJump,
  CostEstimate,
  ApplyChange,
};

export function isApprovedComponent(name: unknown): name is ApprovedComponentName {
  return typeof name === "string" && (APPROVED_COMPONENTS as readonly string[]).includes(name);
}

/** Anything outside the map: shown as rejected, never executed/mounted. */
export function RejectedComponent({ name, path }: { name: unknown; path?: string }) {
  return (
    <span
      className="inline-flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-[12px] font-mono"
      style={{ border: "1px dashed var(--danger)", color: "var(--danger)", background: "var(--danger-dim)" }}
      title="Component is not in the approved catalog — it is never rendered or executed."
    >
      ⛔ rejected component “{String(name)}” {path ? `at ${path}` : ""} — not in approved catalog (never executed)
    </span>
  );
}

/** Look up one catalog field descriptor and render ONLY via the whitelist. */
export function renderComponent(name: unknown, ctx: RenderContext, path?: string): ReactNode {
  if (isApprovedComponent(name)) {
    const Comp = COMPONENT_MAP[name];
    return <Comp {...ctx} />;
  }
  return <RejectedComponent name={name} path={path} />;
}

export function CommandFieldRow({ field, ...ctx }: RenderContext & { field: CatalogField }) {
  const label = field.name ?? "field";
  return (
    <Row label={label} hint={field.required ? "required" : undefined}>
      {field.component ? (
        renderComponent(field.component, { ...ctx, field })
      ) : (
        <span className="font-mono">{fmt(ctx.value)}</span>
      )}
    </Row>
  );
}

/**
 * Render one typed command's payload through the catalog's field descriptors.
 * Unknown payload keys render as plain inert data rows (no component).
 */
export function CommandRenderer({
  command,
  catalog,
  estimate,
  onJump,
}: {
  command: Record<string, any>;
  catalog: CreativeCatalog | null;
  estimate?: RenderContext["estimate"];
  onJump?: (target: Record<string, unknown>) => void;
}) {
  const entry = (catalog?.commands ?? []).find((c) => c.type === command?.type);
  const specs = entry?.fields ?? [];
  const seen = new Set<string>();
  const rows: ReactNode[] = [];

  for (const spec of specs) {
    const key = spec.name;
    if (!key || !(key in command) || ["type", "scope", "target", "note"].includes(key)) continue;
    seen.add(key);
    rows.push(
      <CommandFieldRow
        key={key}
        field={spec}
        value={command[key]}
        estimate={estimate}
        hardConstraints={catalog?.hard_constraints}
        onJump={onJump}
      />
    );
  }
  // payload keys the catalog did not describe: plain data rows (still inert)
  for (const [key, value] of Object.entries(command ?? {})) {
    if (["type", "scope", "target", "note", "fields"].includes(key) || seen.has(key)) continue;
    if (value == null) continue;
    rows.push(
      <Row key={key} label={key}>
        <span className="font-mono">{fmt(value)}</span>
      </Row>
    );
  }
  return <div className="mt-1">{rows}</div>;
}

/**
 * Walk an arbitrary generative-UI document node: only nodes naming an approved
 * `component` render through the whitelist; everything else renders as data.
 */
export function SchemaNode({ node, path = "$", onJump }: { node: unknown; path?: string; onJump?: RenderContext["onJump"] }): ReactNode {
  if (node == null) return null;
  if (Array.isArray(node)) {
    return (
      <div className="space-y-1.5">
        {node.map((item, i) => (
          <SchemaNode key={i} node={item} path={`${path}[${i}]`} onJump={onJump} />
        ))}
      </div>
    );
  }
  if (typeof node !== "object") {
    return <div className="text-[12.5px] font-mono break-words">{fmt(node)}</div>;
  }
  const obj = node as Record<string, unknown>;
  const comp = "component" in obj ? obj.component : "widget" in obj ? obj.widget : undefined;
  if (comp !== undefined) {
    if (!isApprovedComponent(comp)) return <RejectedComponent name={comp} path={path} />;
    return <>{renderComponent(comp, { value: obj.value ?? obj.default, field: obj as CatalogField, onJump }, path)}</>;
  }
  return (
    <div className="rounded-lg p-2.5" style={{ border: "var(--seam)", background: "var(--bg-inset)" }}>
      <div className="font-mono text-[10.5px] mb-1" style={{ color: "var(--text-faint)" }}>{path}</div>
      <div className="space-y-1">
        {Object.entries(obj).map(([k, v]) =>
          v != null && typeof v === "object" ? (
            <SchemaNode key={k} node={v} path={`${path}.${k}`} onJump={onJump} />
          ) : (
            <Row key={k} label={k}>
              <span className="font-mono">{fmt(v)}</span>
            </Row>
          )
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* catalog fetch (useFetch-compatible, cached per mount)                */
/* ------------------------------------------------------------------ */

export function useCreativeCatalog(): { catalog: CreativeCatalog | null; loading: boolean; error: string } {
  const [catalog, setCatalog] = useState<CreativeCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    wsApi
      .get("/creative/catalog")
      .then((r: CreativeCatalog) => {
        if (alive) setCatalog(r);
      })
      .catch((e: any) => {
        if (alive) setError(e?.message ?? "catalog unavailable");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);
  return { catalog, loading, error };
}
