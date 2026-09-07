import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { wsApi } from "../lib/api";
import { AsyncSection, Badge, Card, PageHeader, Tabs, fmtDate } from "../components/ui";

const STATUS_TABS = [
  { key: "all", label: "All" },
  { key: "QC", label: "In QC" },
  { key: "APPROVED", label: "Approved" },
  { key: "PUBLISHED", label: "Published" },
  { key: "LEARNED", label: "Learned" },
  { key: "FAILED", label: "Failed" },
];

export default function Studio() {
  const [tab, setTab] = useState("all");
  const [search, setSearch] = useState("");
  const [items, setItems] = useState<any[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [patterns, setPatterns] = useState<any[]>([]);
  const nav = useNavigate();

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const params = new URLSearchParams({ limit: "100" });
      if (tab !== "all") params.set("status", tab);
      if (search) params.set("search", search);
      const r = await wsApi.get(`/content?${params}`);
      setItems(r.items ?? []);
      setTotal(r.total ?? 0);
    } catch (e: any) { setError(e.message); } finally { setLoading(false); }
  }, [tab, search]);

  useEffect(() => {
    const t = setTimeout(load, search ? 300 : 0);
    return () => clearTimeout(t);
  }, [load, search]);

  // Learning patterns power the predicted-performance hints
  useEffect(() => {
    wsApi.get("/analytics/patterns").then((r) => setPatterns(r.items ?? [])).catch(() => {});
  }, []);

  const bestPattern = patterns
    .filter((p) => p.confidence !== "low" && p.improvement_pct > 0)
    .sort((a, b) => b.improvement_pct - a.improvement_pct)[0];

  async function act(id: string, action: string) {
    await wsApi.post(`/content/${id}/actions`, { action });
    load();
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Content Studio"
        subtitle={`${total} item(s) in the library — every artifact the system produced.`}
        actions={
          <input
            className="input !w-56"
            placeholder="Search topics…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Search content"
          />
        }
      />

      <Tabs tabs={STATUS_TABS.map((t) => ({ ...t, count: undefined }))} active={tab} onChange={setTab} />

      {/* Predicted-performance insight banner (Sprint 2 #8) */}
      {bestPattern && (
        <div className="rounded-xl border px-4 py-3 flex items-center gap-3 text-[13px]"
             style={{ borderColor: "var(--accent)", background: "var(--accent-dim)" }}>
          <span aria-hidden style={{ color: "var(--accent)" }}>◈</span>
          <p style={{ color: "var(--text)" }}>
            <b>Learning insight:</b> {bestPattern.description}
          </p>
          <Badge tone="success">+{bestPattern.improvement_pct.toFixed(0)}% · n={bestPattern.sample_size}</Badge>
        </div>
      )}

      <AsyncSection
        data={items}
        loading={loading}
        error={error}
        onRetry={load}
        empty="No content matches"
        emptyHint="Adjust filters or run the autopilot to produce content."
      >
        {(list) => (
          <Card pad={false} className="overflow-x-auto">
            <table className="table">
              <thead>
                <tr><th>Topic</th><th>Status</th><th>QC</th><th>Engine</th><th>Variants</th><th>Created</th><th></th></tr>
              </thead>
              <tbody>
                {(list as any[]).map((c) => (
                  <tr key={c.id} className="cursor-pointer hover:bg-zinc-500/5" onClick={() => nav(`/studio/${c.id}`)}>
                    <td className="max-w-[320px]"><span className="line-clamp-1 font-medium">{c.topic}</span></td>
                    <td><StatusBadge status={c.status} /></td>
                    <td>
                      {c.video?.quality != null ? (
                        <Badge tone={c.video.quality_passed ? "success" : "error"}>
                          {Math.round(c.video.quality)}
                        </Badge>
                      ) : "—"}
                    </td>
                    <td style={{ color: "var(--text-muted)" }}>
                      {c.video?.engine ?? "—"}
                      {c.video?.engine === "mock" && <Badge tone="warning">mock</Badge>}
                    </td>
                    <td>{c.variants_count}</td>
                    <td className="text-[12px]" style={{ color: "var(--text-muted)" }}>{fmtDate(c.created_at, false)}</td>
                    <td className="text-right whitespace-nowrap">
                      {c.status === "QC" && (
                        <>
                          <button className="btn-ghost !py-1 !px-2 text-xs" onClick={(e) => { e.stopPropagation(); act(c.id, "reject"); }}>Reject</button>
                          <button className="btn-primary !py-1 !px-2 text-xs ml-1.5" onClick={(e) => { e.stopPropagation(); act(c.id, "approve"); }}>Approve</button>
                        </>
                      )}
                      {["FAILED", "APPROVED"].includes(c.status) && (
                        <button className="btn-outline !py-1 !px-2 text-xs" onClick={(e) => { e.stopPropagation(); act(c.id, "retry"); }}>Retry</button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        )}
      </AsyncSection>
    </div>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const good = ["LEARNED", "PUBLISHED", "APPROVED"].includes(status);
  const bad = ["FAILED", "SKIPPED"].includes(status);
  return (
    <Badge tone={good ? "success" : bad ? "error" : status === "PUBLISHED" ? "info" : "neutral"}>
      {status.toLowerCase()}
    </Badge>
  );
}

