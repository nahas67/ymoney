import { useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, ConfirmButton, Empty, Field, Modal, PageHeader, Tabs, toast } from "../components/ui";

const MODULES = ["hooks", "captions", "motion"];

export default function Templates() {
  const [module, setModule] = useState("hooks");
  const list = useFetch(() => wsApi.get("/assets/templates"), []);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ module: "hooks", id: "", title: "", attribution: "", source: "", payload: '{\n  "template": "Your template with {topic}"\n}' });
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState<any>(null);

  const items: any[] = ((list.data as any)?.items ?? []).filter((t: any) => !module || t.module === module);

  async function author() {
    let payload: any;
    try {
      payload = JSON.parse(form.payload);
    } catch {
      toast("Payload is not valid JSON", "error");
      return;
    }
    setBusy(true);
    try {
      await wsApi.post("/assets/templates", {
        template: {
          module: form.module, id: form.id.trim(), version: "v1", title: form.title,
          payload, attribution: form.attribution || undefined, source: form.source || undefined,
        },
      });
      toast("Template published to your workspace", "success");
      setOpen(false);
      setForm({ module: "hooks", id: "", title: "", attribution: "", source: "", payload: '{\n  "template": "Your template with {topic}"\n}' });
      list.reload();
    } catch (e: any) {
      toast(e.message, "error", "Publish failed");
    } finally {
      setBusy(false);
    }
  }

  async function remove(m: string, id: string) {
    try {
      await wsApi.del(`/assets/templates/${m}/${id}`);
      toast("Template deleted", "warning");
      if (detail && detail.module === m && detail.id === id) setDetail(null);
      list.reload();
    } catch (e: any) {
      toast(e.message, "error", "Delete failed");
    }
  }

  async function show(m: string, id: string) {
    try {
      setDetail(await wsApi.get(`/assets/templates/${m}/${id}`));
    } catch (e: any) {
      toast(e.message, "error");
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader title="Templates" subtitle="Versioned prompt library — built-ins, your overrides, and workspace-authored customs with credit."
        actions={<button className="btn-primary !text-xs" onClick={() => setOpen(true)}>+ Author template</button>} />
      <Tabs tabs={[{ key: "", label: "All" }, ...MODULES.map((m) => ({ key: m, label: m }))]} active={module} onChange={setModule} />
      {!items.length ? (
        <Card><Empty title="No templates here" hint="Author one, or pick another module." /></Card>
      ) : (
        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-3">
          {items.map((t: any) => (
            <Card key={`${t.module}/${t.id}`} className="card-hover cursor-pointer" pad={true}>
              <div onClick={() => show(t.module, t.id)}>
                <div className="flex items-center gap-2 flex-wrap">
                  <b className="text-[13.5px]">{t.title}</b>
                  {t.custom && <Badge tone="info">custom</Badge>}
                  {t.overridden && <Badge tone="warning">overridden</Badge>}
                </div>
                <div className="font-mono text-[11.5px] mt-1" style={{ color: "var(--text-faint)" }}>{t.module}/{t.id} · {t.version}</div>
                <div className="text-[12px] mt-1.5" style={{ color: "var(--text-muted)" }}>
                  by {t.attribution || "unknown"}{t.source ? ` · ${t.source}` : ""}
                </div>
              </div>
              {t.custom && (
                <div className="mt-2.5"><ConfirmButton onConfirm={() => remove(t.module, t.id)} confirmText="Delete?">Delete</ConfirmButton></div>
              )}
            </Card>
          ))}
        </div>
      )}
      <Modal open={!!detail} onClose={() => setDetail(null)} title={detail ? `${detail.module}/${detail.id}` : ""} wide>
        {detail && (
          <>
            <div className="flex gap-2 flex-wrap mb-3">
              <Badge tone={detail.custom ? "info" : "muted"}>{detail.custom ? "custom" : "built-in"}</Badge>
              <span className="text-[12.5px]" style={{ color: "var(--text-muted)" }}>by {detail.attribution || "unknown"}</span>
              {detail.source && <span className="font-mono text-[12px] break-all" style={{ color: "var(--text-faint)" }}>{detail.source}</span>}
              <span className="font-mono text-[11.5px]" style={{ color: "var(--text-faint)" }}>versions: {(detail.versions ?? []).join(", ")}</span>
            </div>
            <pre className="text-[12px] p-3 rounded-xl overflow-x-auto" style={{ background: "var(--bg-inset)" }}>
              {JSON.stringify(detail.payload, null, 2)}
            </pre>
          </>
        )}
      </Modal>
      <Modal open={open} onClose={() => setOpen(false)} title="Author workspace template" wide>
        <div className="grid sm:grid-cols-2 gap-3">
          <Field label="Module">
            <select className="select" value={form.module} onChange={(e) => setForm({ ...form, module: e.target.value })}>
              {MODULES.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </Field>
          <Field label="ID" hint="Unique within the module. Built-in ids are reserved."><input className="input font-mono" value={form.id} onChange={(e) => setForm({ ...form, id: e.target.value })} placeholder="my-hook" /></Field>
        </div>
        <Field label="Title"><input className="input" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} placeholder="My killer hook" /></Field>
        <div className="grid sm:grid-cols-2 gap-3">
          <Field label="Attribution" hint="Credited author. Defaults to workspace custom."><input className="input" value={form.attribution} onChange={(e) => setForm({ ...form, attribution: e.target.value })} placeholder="Your name" /></Field>
          <Field label="Source link (optional)"><input className="input font-mono" value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value })} placeholder="https://…" /></Field>
        </div>
        <Field label="Payload JSON"><textarea className="textarea font-mono" rows={6} value={form.payload} onChange={(e) => setForm({ ...form, payload: e.target.value })} /></Field>
        <div className="flex justify-end gap-2 mt-4">
          <button className="btn-ghost !text-xs" onClick={() => setOpen(false)}>Cancel</button>
          <button className="btn-primary !text-xs" disabled={busy || !form.id.trim() || !form.title.trim()} onClick={author}>
            {busy ? "…" : "Publish template"}
          </button>
        </div>
      </Modal>
    </div>
  );
}
