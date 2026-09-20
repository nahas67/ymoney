import { useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, Field, PageHeader } from "../components/ui";

export function brandLogoSrc(): string {
  const ws = localStorage.getItem("ym_ws");
  const t = localStorage.getItem("ym_token");
  return `/api/v1/workspaces/${ws}/brand/logo/file${t ? `?token=${encodeURIComponent(t)}` : ""}`;
}

export default function Brand() {
  const brand = useFetch(() => wsApi.get("/brand"), []);
  const [appName, setAppName] = useState<string | null>(null);
  const [accent, setAccent] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");

  const cur: any = brand.data?.brand ?? {};

  async function save() {
    setBusy("save");
    setMsg("");
    try {
      const s = await wsApi.get("/settings");
      await wsApi.put("/settings", {
        settings: {
          templates: s.settings?.templates ?? undefined,
          brand: {
            app_name: (appName ?? cur.app_name ?? "").slice(0, 60),
            accent: accent ?? cur.accent ?? "#22c55e",
            logo_path: cur.logo_path ?? "",
          },
        },
      });
      setMsg("Saved — reload to apply chrome.");
      brand.reload();
      window.dispatchEvent(new Event("ym-brand"));
    } catch (e: any) {
      setMsg(e.message);
    } finally {
      setBusy("");
    }
  }

  async function upload() {
    if (!file) return;
    setBusy("upload");
    setMsg("");
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch(`/api/v1/workspaces/${localStorage.getItem("ym_ws")}/brand/logo`, {
        method: "POST",
        headers: { Authorization: `Bearer ${localStorage.getItem("ym_token")}` },
        body: form,
      });
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
      setFile(null);
      setMsg("Logo uploaded.");
      brand.reload();
      window.dispatchEvent(new Event("ym-brand"));
    } catch (e: any) {
      setMsg(e.message);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="space-y-4 max-w-[720px]">
      <PageHeader title="Brand" subtitle="White-label kit: studio name, accent color and logo applied across the whole UI." />
      <Card>
        <div className="grid md:grid-cols-2 gap-4">
          <div>
            <Field label="Studio display name">
              <input className="input" value={appName ?? cur.app_name ?? ""} onChange={(e) => setAppName(e.target.value)} placeholder="My Studio" />
            </Field>
            <Field label="Accent color" hint="Applied to buttons, badges, charts and active nav. Invalid values fall back safely.">
              <div className="flex gap-2 items-center">
                <input type="color" value={/^#[0-9a-fA-F]{6}$/.test(accent ?? cur.accent ?? "") ? (accent ?? cur.accent) : "#22c55e"}
                  onChange={(e) => setAccent(e.target.value)} aria-label="Accent color" className="w-10 h-9 cursor-pointer" />
                <input className="input font-mono" value={accent ?? cur.accent ?? ""} onChange={(e) => setAccent(e.target.value)} placeholder="#22c55e" />
              </div>
            </Field>
            <button className="btn-primary !text-xs" disabled={busy === "save"} onClick={save}>{busy === "save" ? "…" : "Save brand kit"}</button>
            {msg && <span className="ml-2 text-[12.5px]" style={{ color: "var(--text-muted)" }}>{msg}</span>}
          </div>
          <div>
            <div className="panel-label mb-1.5">Logo (PNG/JPG/WebP ≤2MB)</div>
            {cur.logo_path ? (
              <img src={brandLogoSrc()} alt="workspace logo" className="max-h-24 rounded-lg mb-2" style={{ border: "var(--seam)" }} />
            ) : (
              <div className="text-[12.5px] mb-2" style={{ color: "var(--text-faint)" }}>No logo — the ¥ mark shows.</div>
            )}
            <input type="file" accept="image/png,image/jpeg,image/webp" onChange={(e) => setFile(e.target.files?.[0] ?? null)} aria-label="Upload logo" />
            <button className="btn-outline !text-xs mt-2" disabled={!file || busy === "upload"} onClick={upload}>
              {busy === "upload" ? "…" : "Upload logo"}
            </button>
          </div>
        </div>
      </Card>
      <Card>
        <div className="panel-label mb-1.5">Disclosure posture (automatic)</div>
        <div className="space-y-1.5 text-[13px]">
          <div className="flex gap-2 items-center"><Badge tone="info">AI-generated</Badge><span style={{ color: "var(--text-muted)" }}>Every publish carries an AI-content disclosure in description/caption.</span></div>
          <div className="flex gap-2 items-center"><Badge tone="warning">Finance</Badge><span style={{ color: "var(--text-muted)" }}>Money topics get “Not financial advice” injected plus a burned-in footer on renders.</span></div>
          <div className="flex gap-2 items-center"><Badge tone="muted">BGM</Badge><span style={{ color: "var(--text-muted)" }}>Only license-allowlisted tracks; unknown beds render silent.</span></div>
        </div>
      </Card>
    </div>
  );
}
