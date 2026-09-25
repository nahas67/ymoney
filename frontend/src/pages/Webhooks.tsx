import { useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, ConfirmButton, Empty, Field, Modal, PageHeader, toast } from "../components/ui";
import { fmtAgo } from "../lib/format";

export default function Webhooks() {
  const list = useFetch(() => wsApi.get("/webhooks"), []);
  const [open, setOpen] = useState(false);
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState<string[]>(["publish.failed", "quality.failed"]);
  const [secret, setSecret] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const catalog: string[] = (list.data as any)?.events ?? [
    "cycle.completed", "publish.failed", "quality.failed", "review.required",
    "budget.exceeded", "safety.autopause", "repurpose.completed",
  ];

  function toggle(ev: string) {
    setEvents((l) => (l.includes(ev) ? l.filter((e) => e !== ev) : [...l, ev]));
  }

  async function subscribe() {
    setBusy(true);
    try {
      const r = await wsApi.post("/webhooks", { url, events });
      setSecret(r.secret);
      setOpen(false);
      setUrl("");
      list.reload();
      toast("Subscribed — copy the signing secret", "success");
    } catch (e: any) {
      toast(e.message, "error", "Subscribe failed");
    } finally {
      setBusy(false);
    }
  }

  async function ping(id: string) {
    try {
      const r = await wsApi.post(`/webhooks/${id}/test`, {});
      toast(r.enqueued ? "Signed ping queued — check your endpoint" : "Ping not queued", r.enqueued ? "success" : "warning");
    } catch (e: any) {
      toast(e.message, "error", "Ping failed");
    }
  }

  async function remove(id: string) {
    try {
      await wsApi.del(`/webhooks/${id}`);
      toast("Subscription deleted", "warning");
      list.reload();
    } catch (e: any) {
      toast(e.message, "error", "Delete failed");
    }
  }

  const items: any[] = (list.data as any)?.items ?? [];

  return (
    <div className="space-y-4">
      <PageHeader title="Webhooks" subtitle="Signed event POSTs with queued retries — no polling loops in your automations."
        actions={<button className="btn-primary !text-xs" onClick={() => setOpen(true)}>+ Subscribe</button>} />
      {secret && (
        <Card style={{ borderColor: "var(--warn)" }}>
          <b className="text-[13.5px]">Signing secret — verify X-YM-Signature with it, then store it away</b>
          <div className="font-mono text-[13px] mt-2 break-all px-2.5 py-1.5 rounded-lg" style={{ background: "var(--bg-inset)" }}>{secret}</div>
          <button className="btn-ghost !text-xs mt-2" onClick={() => setSecret(null)}>Done</button>
        </Card>
      )}
      {!items.length ? (
        <Card><Empty title="No subscriptions" hint="Get cycle.completed, publish.failed, quality.failed and friends pushed to your URL." /></Card>
      ) : (
        <div className="grid md:grid-cols-2 gap-3">
          {items.map((s: any) => (
            <Card key={s.id}>
              <div className="font-mono text-[12.5px] break-all">{s.url}</div>
              <div className="flex gap-1.5 flex-wrap mt-2">
                {(s.events ?? []).map((e: string) => <Badge key={e} tone="info">{e}</Badge>)}
              </div>
              <div className="flex items-center gap-2 mt-3">
                <Badge tone={s.active ? "success" : "muted"}>{s.active ? "active" : "paused"}</Badge>
                <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>{fmtAgo(s.created_at)}</span>
                <span className="ml-auto flex gap-2">
                  <button className="btn-ghost !text-xs" onClick={() => ping(s.id)}>Ping</button>
                  <ConfirmButton onConfirm={() => remove(s.id)} confirmText="Delete?">Delete</ConfirmButton>
                </span>
              </div>
            </Card>
          ))}
        </div>
      )}
      <Modal open={open} onClose={() => setOpen(false)} title="Subscribe URL">
        <Field label="Endpoint URL" hint="https required (localhost http allowed for dev)."><input className="input font-mono" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://your-server.com/hooks/ymoney" /></Field>
        <Field label="Events">
          <div className="flex gap-1.5 flex-wrap">
            {catalog.map((e) => (
              <button key={e} className={`chip${events.includes(e) ? " on" : ""}`} onClick={() => toggle(e)}>{e}</button>
            ))}
          </div>
        </Field>
        <div className="flex justify-end gap-2 mt-4">
          <button className="btn-ghost !text-xs" onClick={() => setOpen(false)}>Cancel</button>
          <button className="btn-primary !text-xs" disabled={busy || !url || !events.length} onClick={subscribe}>{busy ? "…" : "Subscribe"}</button>
        </div>
      </Modal>
    </div>
  );
}
