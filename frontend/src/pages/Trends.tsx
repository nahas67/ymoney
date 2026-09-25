import { useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, Modal, PageHeader, ScoreBar, SearchInput, Section, Tabs, lifecycleTone, toast } from "../components/ui";

export default function Trends() {
  const [filter, setFilter] = useState("available");
  const [sort, setSort] = useState<"score" | "virality">("score");
  const [q, setQ] = useState("");
  const opps = useFetch(() => wsApi.get(`/opportunities?status=${filter}&limit=100`), [filter]);
  const [open, setOpen] = useState<any>(null);
  const [busy, setBusy] = useState("");

  async function act(o: any, kind: "select" | "skip") {
    setBusy(o.id + kind);
    try {
      if (kind === "select") await wsApi.post(`/opportunities/${o.id}/select`);
      else await wsApi.post(`/opportunities/${o.id}/skip`, { reason: "archived by operator" });
      toast(kind === "select" ? "Sent to production" : "Skipped", kind === "select" ? "success" : "warning");
      opps.reload();
      setOpen(null);
    } catch (e: any) {
      toast(e.message, "error", "Action failed");
    } finally {
      setBusy("");
    }
  }

  const items: any[] = [...((opps.data as any)?.items ?? [])]
    .filter((o: any) => !q || (o.topic ?? "").toLowerCase().includes(q.toLowerCase()))
    .sort((a, b) =>
      sort === "virality" ? (b.virality ?? 0) - (a.virality ?? 0) : b.score - a.score
    );

  return (
    <div className="space-y-4">
      <PageHeader title="Trend Center" subtitle="Scored opportunities with lifecycle, virality breakout score and full evidence. Produce the best, skip the rest."
        actions={
          <div className="flex gap-1.5">
            <button className={`tab ${sort === "score" ? "active" : ""}`} onClick={() => setSort("score")}>Top score</button>
            <button className={`tab ${sort === "virality" ? "active" : ""}`} onClick={() => setSort("virality")}>Top virality</button>
          </div>
        } />
      <Tabs tabs={[
        { key: "available", label: "Available" }, { key: "selected", label: "Selected" }, { key: "skipped", label: "Skipped" },
      ]} active={filter} onChange={setFilter} />
      <div className="max-w-[300px] mb-3"><SearchInput value={q} onChange={setQ} placeholder="Filter topics…" /></div>
      <Section data={items} loading={opps.loading} error={opps.error} onRetry={opps.reload}
        empty="No opportunities here" emptyHint="Run the autopilot FIND stage or wait for the next discovery refresh.">
        {(list) => (
          <div className="grid md:grid-cols-2 gap-3">
            {list.map((o: any) => (
              <Card key={o.id} className="card-hover cursor-pointer" style={{ padding: 15 }} >
                <div onClick={() => setOpen(o)}>
                  <div className="flex gap-2 items-center mb-1.5 flex-wrap">
                    <Badge tone={lifecycleTone(o.lifecycle)}>{o.lifecycle}</Badge>
                    <Badge tone="muted">{o.source}</Badge>
                    {o.selected && <Badge tone="success">selected</Badge>}
                  </div>
                  <div className="font-medium text-[14px] leading-snug mb-2">{o.topic}</div>
                  <div className="flex items-center gap-4 flex-wrap">
                    <span className="flex items-center gap-1.5 text-[12px]" style={{ color: "var(--text-muted)" }}>Score <ScoreBar value={o.score} /></span>
                    <span className="flex items-center gap-1.5 text-[12px]" style={{ color: "var(--text-muted)" }}>🔥 <b className="font-mono">{(o.virality ?? 0).toFixed(0)}</b></span>
                    {o.velocity != null && <span className="font-mono text-[11.5px]" style={{ color: "var(--text-faint)" }}>vel {(o.velocity * 100).toFixed(0)}%</span>}
                  </div>
                </div>
                <div className="flex gap-2 mt-3">
                  {!o.selected && !o.skipped_reason && (
                    <>
                      <button className="btn-primary !text-xs !py-1.5" disabled={busy === o.id + "select"} onClick={() => act(o, "select")}>Produce</button>
                      <button className="btn-ghost !text-xs !py-1.5" disabled={busy === o.id + "skip"} onClick={() => act(o, "skip")}>Skip</button>
                    </>
                  )}
                  {o.source_url && <a className="btn-ghost !text-xs !py-1.5 ml-auto no-underline" style={{ color: "var(--info)" }} href={o.source_url} target="_blank" rel="noreferrer">Evidence ↗</a>}
                </div>
              </Card>
            ))}
          </div>
        )}
      </Section>

      <Modal open={!!open} onClose={() => setOpen(null)} title="Score breakdown" wide>
        {open && (
          <div className="space-y-3">
            <div className="font-medium">{open.topic}</div>
            <div className="flex gap-2 flex-wrap">
              <Badge tone={lifecycleTone(open.lifecycle)}>{open.lifecycle}</Badge>
              <Badge tone="info">virality {(open.virality ?? 0).toFixed(0)}</Badge>
              <Badge tone="muted">confidence {Math.round((open.confidence ?? 0) * 100)}%</Badge>
              <Badge tone="muted">{open.recommendation}</Badge>
            </div>
            {Object.entries(open.components ?? {}).map(([k, v]: any) => (
              <div key={k} className="py-1.5" style={{ borderBottom: "var(--seam)" }}>
                <div className="flex justify-between text-[13px] mb-1">
                  <span className="font-mono">{k}</span>
                  <ScoreBar value={v.score} />
                </div>
                <div className="text-[12px]" style={{ color: "var(--text-muted)" }}>{v.reason} · {v.source} · conf {v.confidence}</div>
              </div>
            ))}
            <div className="flex gap-2 pt-1">
              <button className="btn-primary !text-xs" onClick={() => act(open, "select")}>Produce this topic</button>
              <button className="btn-ghost !text-xs" onClick={() => act(open, "skip")}>Skip</button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}
