import { useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, Field, Modal, PageHeader, Section, statusTone } from "../components/ui";
import { fmtDate } from "../lib/format";

export default function Campaigns() {
  const list = useFetch(() => wsApi.get("/campaigns"), []);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [platforms, setPlatforms] = useState<string[]>(["youtube", "tiktok"]);
  const [detail, setDetail] = useState<any>(null);

  async function create() {
    if (!name.trim()) return;
    try {
      await wsApi.post("/campaigns", { name, goal, platforms });
      setName("");
      setGoal("");
      setOpen(false);
      list.reload();
    } catch (e: any) {
      alert(e.message);
    }
  }

  function toggle(p: string) {
    setPlatforms(platforms.includes(p) ? platforms.filter((x) => x !== p) : [...platforms, p]);
  }

  return (
    <div className="space-y-4">
      <PageHeader title="Campaigns" subtitle="Group content under goals with platform targets."
        actions={<button className="btn-primary !text-xs" onClick={() => setOpen(true)}>+ New campaign</button>} />
      <Section data={(list.data as any)?.items} loading={list.loading} error={list.error} onRetry={list.reload}
        empty="No campaigns" emptyHint="Campaigns group videos under a goal; the autopilot can target them.">
        {(rows) => (
          <div className="grid md:grid-cols-2 gap-3">
            {rows.map((c: any) => (
              <Card key={c.id} className="cursor-pointer" style={{ padding: 15 }} >
                <div onClick={async () => setDetail(await wsApi.get(`/campaigns/${c.id}`))}>
                  <div className="flex gap-2 items-center mb-1">
                    <b className="text-[14px]">{c.name}</b>
                    <Badge tone={statusTone(c.status)}>{c.status}</Badge>
                  </div>
                  <div className="text-[12.5px] line-clamp-2" style={{ color: "var(--text-muted)" }}>{c.goal || "—"}</div>
                  <div className="flex gap-1.5 mt-2 flex-wrap">
                    {(c.platforms ?? []).map((p: string) => <Badge key={p} tone="muted">{p}</Badge>)}
                  </div>
                </div>
              </Card>
            ))}
          </div>
        )}
      </Section>
      <Modal open={open} onClose={() => setOpen(false)} title="New campaign">
        <Field label="Name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Goal"><textarea className="textarea" rows={2} value={goal} onChange={(e) => setGoal(e.target.value)} /></Field>
        <Field label="Platforms">
          <div className="flex gap-2 flex-wrap">
            {["youtube", "tiktok", "facebook", "instagram"].map((p) => (
              <button key={p} className={platforms.includes(p) ? "btn-primary !text-xs" : "btn-outline !text-xs"} onClick={() => toggle(p)}>{p}</button>
            ))}
          </div>
        </Field>
        <button className="btn-primary !text-xs" onClick={create}>Create</button>
      </Modal>
      <Modal open={!!detail} onClose={() => setDetail(null)} title={detail?.name ?? "Campaign"}>
        {detail && (
          <div className="text-[13px] space-y-1.5">
            <div style={{ color: "var(--text-muted)" }}>{detail.goal}</div>
            <div>Content items: <b className="font-mono">{detail.progress?.content_items ?? 0}</b> · published: <b className="font-mono">{detail.progress?.published ?? 0}</b></div>
            <div className="font-mono text-[12px]" style={{ color: "var(--text-faint)" }}>
              {detail.starts_at ? fmtDate(detail.starts_at) : "no start"} → {detail.ends_at ? fmtDate(detail.ends_at) : "open ended"}
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}
