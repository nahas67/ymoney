import { useCallback, useEffect, useState } from "react";
import { wsApi } from "../lib/api";
import {
  AsyncSection, Badge, Card, Modal, PageHeader, Tabs, useToast,
} from "../components/ui";

const LIFECYCLE_TABS = [
  { key: "RISING", label: "Rising" },
  { key: "EMERGING", label: "Emerging" },
  { key: "PEAK", label: "Peak" },
  { key: "EVERGREEN", label: "Evergreen" },
  { key: "DECLINING", label: "Declining" },
  { key: "UNKNOWN", label: "All" },
];

export default function Trends() {
  const [tab, setTab] = useState("RISING");
  const [items, setItems] = useState<any[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<any | null>(null);
  const [cursor, setCursor] = useState(0);
  const { push } = useToast();

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await wsApi.get(`/opportunities?limit=100`);
      setTotal(r.total ?? 0);
      let list: any[] = r.items ?? [];
      if (tab !== "UNKNOWN") list = list.filter((o) => (o.lifecycle ?? "UNKNOWN") === tab);
      setItems(list);
      setCursor(0);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [tab]);

  useEffect(() => { load(); }, [load]);

  // keyboard navigation: ↑/↓ move, Enter opens, Esc handled by Modal
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (selected || items.length === 0) return;
      if ((e.target as HTMLElement)?.tagName === "INPUT") return;
      if (e.key === "ArrowDown") { e.preventDefault(); setCursor((c) => Math.min(items.length - 1, c + 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); setCursor((c) => Math.max(0, c - 1)); }
      else if (e.key === "Enter") { e.preventDefault(); setSelected(items[cursor]); }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items, cursor, selected]);

  async function act(opportunityId: string, action: "select" | "skip") {
    try {
      if (action === "select") {
        await wsApi.post(`/opportunities/${opportunityId}/select`);
        push("success", "Idea created — it will be produced in an upcoming cycle or via Composer.");
      } else {
        await wsApi.post(`/opportunities/${opportunityId}/skip`, { reason: "dismissed by user" });
        push("info", "Trend ignored.");
      }
      setSelected(null);
      load();
    } catch (e: any) {
      push("error", e.message);
    }
  }

  const counts: Record<string, number> = {};
  // counts come from the unfiltered fetch on first load; approximate by tab switch
  return (
    <div className="space-y-5">
      <PageHeader
        title="Trend Center"
        subtitle="Live opportunities with explainable scores. YMONEY picks automatically — you can force or ignore."
        actions={<Badge tone="neutral">{total} tracked</Badge>}
      />

      <Tabs tabs={LIFECYCLE_TABS} active={tab} onChange={setTab} />

      <AsyncSection
        data={items}
        loading={loading}
        error={error}
        onRetry={load}
        empty="No trends in this stage"
        emptyHint="Run the autopilot to refresh discovery, or switch lifecycle tab."
      >
        {(list) => (
          <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-4">
            {list.map((o, i) => (
              <TrendCard
                key={o.id}
                opp={o}
                focused={i === cursor}
                onOpen={() => setSelected(o)}
                onHover={() => setCursor(i)}
              />
            ))}
          </div>
        )}
      </AsyncSection>

      <p className="text-[11px]" style={{ color: "var(--text-muted)" }}>
        ↑↓ navigate · Enter open · click a card for the score breakdown
      </p>

      <Modal open={!!selected} onClose={() => setSelected(null)} wide>
        {selected && <TrendDetail opp={selected} onAct={act} />}
      </Modal>
    </div>
  );
}

function TrendCard({ opp, focused, onOpen, onHover }: {
  opp: any; focused: boolean; onOpen: () => void; onHover: () => void;
}) {
  const lc = opp.lifecycle ?? "UNKNOWN";
  const score = Number(opp.score);
  return (
    <button
      className="card p-4 text-left w-full transition-colors"
      style={focused ? { borderColor: "var(--accent, #10b981)", borderWidth: 1.5 } : undefined}
      onClick={onOpen}
      onMouseEnter={onHover}
    >
      <div className="flex items-start justify-between gap-2 mb-2">
        <Badge tone={
          lc === "EMERGING" ? "success" : lc === "RISING" ? "info"
          : lc === "DECLINING" ? "error" : lc === "PEAK" ? "warning" : "neutral"
        }>{lc.toLowerCase()}</Badge>
        <span className="font-mono text-sm font-semibold">{score.toFixed(0)}</span>
      </div>
      {/* score bar — communicates the number visually, no decoration */}
      <div className="h-1 rounded-full mb-2" style={{ background: "var(--bg-subtle)" }}>
        <div
          className="h-1 rounded-full"
          style={{
            width: `${Math.max(2, Math.min(100, score))}%`,
            background: score >= 70 ? "var(--accent, #10b981)" : score >= 45 ? "#3b82f6" : "#94a3b8",
          }}
        />
      </div>
      <p className="text-sm font-medium leading-snug line-clamp-2">{opp.topic}</p>
      <div className="mt-2.5 flex items-center justify-between text-[11px]" style={{ color: "var(--text-muted)" }}>
        <span className="flex items-center gap-1.5">
          {opp.source}
          {opp.velocity != null && <VelocityBadge velocity={opp.velocity} />}
        </span>
        <span>{new Date(opp.created_at).toLocaleDateString()}</span>
      </div>
      {opp.selected && <div className="mt-2"><Badge tone="success">selected</Badge></div>}
    </button>
  );
}

function VelocityBadge({ velocity }: { velocity: number | null | undefined }) {
  if (velocity == null) return null;
  const pct = Math.round(velocity * 100);
  const tone = pct >= 70 ? "success" : pct >= 35 ? "info" : "neutral";
  const label = pct >= 70 ? "fast" : pct >= 35 ? "steady" : "slow";
  return (
    <Badge tone={tone as any}>
      {label} · {pct}
    </Badge>
  );
}

function TrendDetail({ opp, onAct }: { opp: any; onAct: (id: string, a: "select" | "skip") => void }) {
  const comps: Record<string, any> = opp.components ?? {};
  return (
    <div className="space-y-5">
      <div>
        <div className="flex items-center justify-between mb-1">
          <h2 className="text-lg font-semibold leading-snug">{opp.topic}</h2>
          <span className="font-mono text-xl font-bold">{Number(opp.score).toFixed(0)}</span>
        </div>
        <p className="text-xs" style={{ color: "var(--text-muted)" }}>
          source: {opp.source} · detected {new Date(opp.created_at).toLocaleString()} · lifecycle {opp.lifecycle}
          {opp.velocity != null && <> · velocity {Math.round(opp.velocity * 100)}/100</>}
        </p>
        {opp.source_url && (
          <a
            href={opp.source_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs underline inline-flex items-center gap-1 mt-1"
            style={{ color: "var(--accent, #10b981)" }}
          >
            View source ↗
          </a>
        )}
      </div>

      <div>
        <h3 className="text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: "var(--text-muted)" }}>
          Why this score?
        </h3>
        <div className="space-y-1">
          {Object.entries(comps).map(([key, c]: [string, any]) => (
            <div key={key} className="flex items-center justify-between text-[13px]">
              <div className="min-w-0">
                <span className="capitalize">{key.replace(/_/g, " ")}</span>
                <span className="block text-[11px]" style={{ color: "var(--text-muted)" }}>{c.reason}</span>
              </div>
              <div className="text-right shrink-0 ml-3">
                <span className={`font-mono ${c.score >= 50 ? "" : "text-red-500"}`}>{Number(c.score).toFixed(0)}</span>
                <span className="block text-[10px]" style={{ color: "var(--text-muted)" }}>
                  ×{c.weight} · {(c.confidence * 100).toFixed(0)}%
                </span>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="flex gap-2 pt-2 border-t" style={{ borderColor: "var(--border)" }}>
        {!opp.selected ? (
          <button className="btn-primary" onClick={() => onAct(opp.id, "select")}>Create content</button>
        ) : (
          <Badge tone="success">already selected</Badge>
        )}
        <button className="btn-outline" onClick={() => onAct(opp.id, "skip")}>Ignore</button>
        {opp.source_url && (
          <a
            className="btn-outline"
            href={opp.source_url}
            target="_blank"
            rel="noopener noreferrer"
          >
            Evidence ↗
          </a>
        )}
      </div>
    </div>
  );
}

