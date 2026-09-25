import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, Empty, PageHeader, SearchInput, Tabs, toast } from "../components/ui";
import { fmtAgo } from "../lib/format";

export default function Approvals() {
  const nav = useNavigate();
  const [tab, setTab] = useState<"QC" | "APPROVED">("QC");
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState("");
  const list = useFetch(() => wsApi.get(`/content?status=${tab}&limit=100`), [tab]);

  async function act(id: string, a: string, reason?: string) {
    setBusy(`${id}:${a}`);
    try {
      await wsApi.post(`/content/${id}/actions`, reason == null ? { action: a } : { action: a, reason });
      toast(a === "approve" ? "Approved — upload queued" : a === "reject" ? "Rejected" : `Marked ${a}`, a === "reject" ? "warning" : "success");
      list.reload();
    } catch (e: any) {
      toast(e.message, "error", "Action failed");
    } finally {
      setBusy("");
    }
  }

  function reject(id: string) {
    const reason = window.prompt("Rejection reason (stored on the item):", "");
    if (reason == null) return;
    act(id, "reject", reason);
  }

  const items: any[] = ((list.data as any)?.items ?? []).filter((c: any) =>
    !q || (c.topic ?? "").toLowerCase().includes(q.toLowerCase())
  );

  return (
    <div className="space-y-4">
      <PageHeader title="Approvals" subtitle="Human gate between QC and publish — compliance holds land here too." />
      <div className="flex flex-wrap gap-3 items-center">
        <Tabs tabs={[{ key: "QC", label: "Awaiting review" }, { key: "APPROVED", label: "Approved" }]} active={tab} onChange={setTab} />
        <div className="w-[240px] ml-auto"><SearchInput value={q} onChange={setQ} placeholder="Filter by topic…" /></div>
      </div>
      {list.loading && !list.data ? (
        <Card><div className="text-[13px]" style={{ color: "var(--text-muted)" }}>Loading queue…</div></Card>
      ) : !items.length ? (
        <Card><Empty title={tab === "QC" ? "Queue clear" : "Nothing approved yet"}
          hint={tab === "QC" ? "New QC passes and compliance holds will appear here." : "Approved items waiting for publish appear here."} /></Card>
      ) : (
        <div className="grid md:grid-cols-2 gap-3">
          {items.map((c: any) => (
            <Card key={c.id} className="card-hover">
              <div className="flex items-start gap-2.5">
                <div className="flex-1 min-w-0">
                  <div className="font-semibold text-[14px] leading-snug cursor-pointer hover:opacity-80"
                    onClick={() => nav(`/studio/${c.id}`)}>{c.topic}</div>
                  <div className="flex items-center gap-2 mt-1.5 flex-wrap">
                    <Badge tone="warning">{c.status}</Badge>
                    <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>{fmtAgo(c.created_at)}</span>
                  </div>
                  {c.error && (
                    <div className="text-[12px] mt-2 break-words" style={{ color: "var(--warn)" }}>ⓘ {c.error.slice(0, 220)}</div>
                  )}
                </div>
              </div>
              {tab === "QC" && (
                <div className="flex gap-2 mt-3 flex-wrap">
                  <button className="btn-primary !text-xs" disabled={busy === `${c.id}:approve`} onClick={() => act(c.id, "approve")}>
                    {busy === `${c.id}:approve` ? "…" : "Approve"}
                  </button>
                  <button className="btn-outline !text-xs" disabled={busy === `${c.id}:reject`} onClick={() => reject(c.id)}>
                    {busy === `${c.id}:reject` ? "…" : "Reject"}
                  </button>
                  <button className="btn-ghost !text-xs" onClick={() => nav(`/studio/${c.id}`)}>Open</button>
                </div>
              )}
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
