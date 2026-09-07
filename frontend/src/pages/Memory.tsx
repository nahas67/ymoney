import { useCallback, useEffect, useState } from "react";
import { wsApi } from "../lib/api";
import { AsyncSection, Badge, Card, PageHeader, Tabs, useToast } from "../components/ui";

const TYPE_TABS = [
  { key: "", label: "All" },
  { key: "semantic", label: "Semantic" },
  { key: "episodic", label: "Episodic" },
  { key: "strategic", label: "Strategic" },
  { key: "preference", label: "Preference" },
  { key: "short_term", label: "Short-term" },
];

const TYPE_COLORS: Record<string, string> = {
  semantic: "info",
  episodic: "neutral",
  strategic: "success",
  preference: "warning",
  short_term: "error",
};

/**
 * Memory Center — the persistent memory store (spec #25).
 * Targeted retrieval only: filtering by type/query; never a raw dump.
 */
export default function Memory() {
  const [tab, setTab] = useState("");
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { push } = useToast();

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const params = new URLSearchParams();
      if (tab) params.set("type", tab);
      if (query.trim()) params.set("q", query.trim());
      params.set("limit", "50");
      const r = await wsApi.get(`/memory?${params.toString()}`);
      setItems(r.items ?? []);
    } catch (e: any) { setError(e.message); } finally { setLoading(false); }
  }, [tab, query]);

  useEffect(() => { load(); }, [load]);

  async function remove(id: string) {      try {
      await wsApi.del(`/memory/${id}`);
      push("success", "Memory deleted");
      load();
    } catch (e: any) { push("error", e.message); }
  }

  return (
    <div className="page">
      <PageHeader
        title="Memory"
        subtitle="What YMONEY remembers — learned patterns, episode history, and preferences. Filtered views only; the store is never dumped."
      />
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 16, flexWrap: "wrap" }}>
        <Tabs
          tabs={TYPE_TABS.map((t) => ({ key: t.key, label: t.label }))}
          active={tab}
          onChange={setTab}
        />
        <input
          className="input"
          placeholder="Search content…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          style={{ maxWidth: 260 }}
        />
      </div>
      <AsyncSection data={items} loading={loading} error={error} onRetry={load}
        empty="No memories yet"
        emptyHint="Learned patterns appear here after the Learning Agent processes measured posts.">
        {(data) => (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {data.map((m) => (
              <Card key={m.id}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start" }}>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6, flexWrap: "wrap" }}>
                      <Badge tone={(TYPE_COLORS[m.type] ?? "neutral") as any}>{m.type}</Badge>
                      {m.scope && <Badge tone="neutral">{m.scope}</Badge>}
                      <span style={{ fontSize: 12, color: "var(--text-tertiary)" }}>
                        {m.source} · confidence {Math.round((m.confidence ?? 0) * 100)}% · {m.created_at?.slice(0, 10)}
                        {m.expires_at ? " · expires" : ""}
                      </span>
                    </div>
                    <div style={{ fontSize: 14, lineHeight: 1.45 }}>{m.content}</div>
                  </div>
                  <button className="btn btn-sm btn-danger" onClick={() => remove(m.id)} title="Delete memory">✕</button>
                </div>
              </Card>
            ))}
          </div>
        )}
      </AsyncSection>
    </div>
  );
}
