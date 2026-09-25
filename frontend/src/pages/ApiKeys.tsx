import { useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, ConfirmButton, CopyButton, Empty, Field, Modal, PageHeader, toast } from "../components/ui";
import { fmtAgo } from "../lib/format";

export default function ApiKeys() {
  const list = useFetch(() => wsApi.get("/api-keys"), []);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [role, setRole] = useState("member");
  const [minted, setMinted] = useState<any>(null);
  const [busy, setBusy] = useState(false);

  async function mint() {
    setBusy(true);
    try {
      const r = await wsApi.post("/api-keys", { name, role });
      setMinted(r);
      setOpen(false);
      setName("");
      list.reload();
      toast("Key minted — copy it now, it never shows again", "success");
    } catch (e: any) {
      toast(e.message, "error", "Mint failed");
    } finally {
      setBusy(false);
    }
  }

  async function revoke(id: string) {
    try {
      await wsApi.post(`/api-keys/${id}/revoke`, {});
      toast("Key revoked", "warning");
      list.reload();
    } catch (e: any) {
      toast(e.message, "error", "Revoke failed");
    }
  }

  const items: any[] = (list.data as any)?.items ?? [];

  return (
    <div className="space-y-4">
      <PageHeader title="API keys" subtitle="Third-party access, separate from your login — hashes only, plaintext shown once."
        actions={<button className="btn-primary !text-xs" onClick={() => setOpen(true)}>+ New key</button>} />
      {minted && (
        <Card style={{ borderColor: "var(--warn)" }}>
          <b className="text-[13.5px]">Copy this key now — it will never be shown again</b>
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <code className="text-[13px] px-2.5 py-1.5 rounded-lg break-all" style={{ background: "var(--bg-inset)" }}>{minted.api_key}</code>
            <CopyButton text={minted.api_key} label="Copy key" />
            <button className="btn-ghost !text-xs" onClick={() => setMinted(null)}>Done</button>
          </div>
        </Card>
      )}
      {!items.length ? (
        <Card><Empty title="No API keys" hint="Mint one for Zapier, n8n, or your own scripts. Use it as Bearer ym_… or X-API-Key." /></Card>
      ) : (
        <Card pad={false} className="overflow-x-auto">
          <table className="table">
            <thead><tr><th>Name</th><th>Prefix</th><th>Role</th><th>Last used</th><th>Status</th><th></th></tr></thead>
            <tbody>
              {items.map((k: any) => (
                <tr key={k.id}>
                  <td className="font-medium">{k.name || <span style={{ color: "var(--text-faint)" }}>unnamed</span>}</td>
                  <td className="font-mono text-[12px]">{k.prefix}…</td>
                  <td><Badge tone={k.role === "admin" ? "warning" : "muted"}>{k.role}</Badge></td>
                  <td className="font-mono text-[12px]" style={{ color: "var(--text-muted)" }}>{k.last_used_at ? fmtAgo(k.last_used_at) : "never"}</td>
                  <td><Badge tone={k.revoked ? "error" : "success"}>{k.revoked ? "revoked" : "live"}</Badge></td>
                  <td className="text-right">
                    {!k.revoked && <ConfirmButton onConfirm={() => revoke(k.id)} confirmText="Revoke?">Revoke</ConfirmButton>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
      <Modal open={open} onClose={() => setOpen(false)} title="Mint API key">
        <Field label="Name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="zapier-prod" /></Field>
        <Field label="Role" hint="What this key may do. Prefer the least privilege that works.">
          <select className="select" value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="viewer">viewer — read only</option>
            <option value="member">member — produce + publish</option>
            <option value="admin">admin — everything incl. settings</option>
          </select>
        </Field>
        <div className="flex justify-end gap-2 mt-4">
          <button className="btn-ghost !text-xs" onClick={() => setOpen(false)}>Cancel</button>
          <button className="btn-primary !text-xs" disabled={busy} onClick={mint}>{busy ? "…" : "Mint key"}</button>
        </div>
      </Modal>
    </div>
  );
}
