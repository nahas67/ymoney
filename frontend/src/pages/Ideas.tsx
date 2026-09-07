import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { wsApi } from "../lib/api";
import { AsyncSection, Badge, Card, PageHeader, Tabs, fmtDate } from "../components/ui";

/** Ideas = opportunities the system (or you) flagged for production. */
export default function Ideas() {
  const [tab, setTab] = useState("queue");
  const [opps, setOpps] = useState<any[]>([]);
  const [items, setItems] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const nav = useNavigate();

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [o, c] = await Promise.all([
        wsApi.get("/opportunities?limit=200"),
        wsApi.get("/content?limit=100"),
      ]);
      setOpps(o.items ?? []);
      setItems(c.items ?? []);
    } catch (e: any) { setError(e.message); } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function act(id: string, action: "select" | "skip") {
    if (action === "select") await wsApi.post(`/opportunities/${id}/select`);
    else await wsApi.post(`/opportunities/${id}/skip`, { reason: "archived by user" });
    load();
  }

  const ideaQueue = opps.filter((o) => !o.selected && o.score >= 52);
  const drafts = items.filter((i) => i.status === "IDEA");
  const rejected = items.filter((i) => ["SKIPPED", "FAILED"].includes(i.status));
  const selectedOpps = opps.filter((o) => o.selected);

  return (
    <div className="space-y-5">
      <PageHeader
        title="Ideas"
        subtitle="Everything waiting for a decision — from discovery or from you."
      />
      <Tabs
        tabs={[
          { key: "queue", label: "Idea queue", count: ideaQueue.length },
          { key: "drafts", label: "Drafts", count: drafts.length },
          { key: "selected", label: "In pipeline", count: selectedOpps.length },
          { key: "rejected", label: "Rejected / failed", count: rejected.length },
        ]}
        active={tab}
        onChange={setTab}
      />

      <AsyncSection
        data={tab === "drafts" ? drafts : tab === "rejected" ? rejected : tab === "selected" ? selectedOpps : ideaQueue}
        loading={loading}
        error={error}
        onRetry={load}
        empty={tab === "queue" ? "No pending ideas" : "Nothing here"}
        emptyHint={tab === "queue" ? "Discovery fills this list on every cycle." : undefined}
      >
        {(list) =>
          tab === "queue" || tab === "selected" ? (
            <Card pad={false} className="overflow-x-auto">
              <table className="table">
                <thead><tr><th>Topic</th><th>Score</th><th>Lifecycle</th><th>Source</th><th></th></tr></thead>
                <tbody>
                  {(list as any[]).map((o) => (
                    <tr key={o.id}>
                      <td className="max-w-[380px]"><span className="line-clamp-1">{o.topic}</span></td>
                      <td className="font-mono">{Number(o.score).toFixed(0)}</td>
                      <td><Badge tone="neutral">{(o.lifecycle ?? "unknown").toLowerCase()}</Badge></td>
                      <td style={{ color: "var(--text-muted)" }}>{o.source}</td>
                      <td className="text-right whitespace-nowrap">
                        {!o.selected && (
                          <>
                            <button className="btn-ghost !py-1 !px-2 text-xs" onClick={() => act(o.id, "skip")}>Archive</button>
                            <button className="btn-primary !py-1 !px-2 text-xs ml-1.5" onClick={() => act(o.id, "select")}>Produce</button>
                          </>
                        )}
                        {o.selected && <Badge tone="success">in pipeline</Badge>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          ) : (
            <Card pad={false} className="overflow-x-auto">
              <table className="table">
                <thead><tr><th>Topic</th><th>Status</th><th>Error</th><th>Created</th><th></th></tr></thead>
                <tbody>
                  {(list as any[]).map((c) => (
                    <tr key={c.id} className="cursor-pointer hover:bg-zinc-500/5" onClick={() => nav(`/studio/${c.id}`)}>
                      <td className="max-w-[320px] line-clamp-1">{c.topic}</td>
                      <td><Badge tone={c.status === "FAILED" ? "error" : "neutral"}>{c.status.toLowerCase()}</Badge></td>
                      <td className="max-w-[220px] truncate text-[12px]" style={{ color: "var(--text-muted)" }}>{c.error}</td>
                      <td className="text-[12px]" style={{ color: "var(--text-muted)" }}>{fmtDate(c.created_at)}</td>
                      <td className="text-right text-xs">Open →</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )
        }
      </AsyncSection>
    </div>
  );
}

