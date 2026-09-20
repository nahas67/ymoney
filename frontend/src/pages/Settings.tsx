import { useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, Field, PageHeader, Tabs } from "../components/ui";

const CONN_GROUPS: { title: string; keys: string[] }[] = [
  { title: "LLM (scripts, research, QC)", keys: ["llm.api_key", "llm.base_url", "llm.model", "llm.model_cheap", "llm.model_reasoning", "llm.model_verification"] },
  { title: "Publishing — OAuth apps", keys: ["google.client_id", "google.client_secret", "tiktok.client_key", "tiktok.client_secret", "meta.app_id", "meta.app_secret"] },
  { title: "Publishing — relay", keys: ["upload_post.api_key", "upload_post.username"] },
  { title: "Trend sources", keys: ["youtube.api_key", "newsdata.api_key", "coingecko.api_key", "pexels.api_key"] },
  { title: "Voices & images", keys: ["tts.provider", "tts.kokoro_base_url", "tts.kokoro_api_key", "tts.chatterbox_base_url", "tts.qwen_base_url", "tts.qwen_instruct", "tts.qwen_api_key", "image.openai_base_url", "image.openai_api_key", "image.openai_model"] },
  { title: "Telegram", keys: ["telegram.bot_token"] },
  { title: "Avatar", keys: ["avatar.backend", "avatar.base_url", "avatar.sadtalker_dir", "avatar.wavlip_dir"] },
  { title: "B-roll & AI video", keys: ["broll.ai_backend", "broll.ai_base_url"] },
];

const SAFETY_FIELDS: { key: string; label: string; type: "num" | "bool"; hint?: string }[] = [
  { key: "daily_budget_usd", label: "Daily budget (USD)", type: "num" },
  { key: "monthly_budget_usd", label: "Monthly budget (USD)", type: "num" },
  { key: "per_video_budget_usd", label: "Per-video budget (USD)", type: "num" },
  { key: "max_videos_per_day", label: "Max videos / day", type: "num" },
  { key: "max_uploads_per_hour", label: "Max uploads / hour", type: "num" },
  { key: "min_qc_score", label: "Min QC score", type: "num", hint: "Below this, renders regenerate (max attempts) then fail." },
  { key: "max_render_attempts", label: "Max render attempts", type: "num" },
  { key: "max_consecutive_failures", label: "Max consecutive failures", type: "num", hint: "Circuit breaker + auto-pause trigger." },
  { key: "max_concurrent_renders", label: "Max concurrent renders", type: "num" },
  { key: "similarity_threshold", label: "Similarity threshold", type: "num", hint: "Above this, topics hard-SKIP as repeats." },
  { key: "require_human_review_risk_above", label: "Human review above risk", type: "num" },
  { key: "produce_score_threshold", label: "Produce score threshold", type: "num" },
  { key: "require_approval_before_publish", label: "Approval hold before publish", type: "bool", hint: "QC-passed videos wait in APPROVED for a human instead of auto-publishing." },
];

export default function Settings() {
  const [tab, setTab] = useState<"workspace" | "connections" | "safety" | "engine" | "trends">("workspace");
  return (
    <div className="space-y-4">
      <PageHeader title="Settings" subtitle="Workspace, provider keys, Safety Center budgets, engine and trend sources." />
      <Tabs tabs={[
        { key: "workspace", label: "Workspace" }, { key: "connections", label: "Connections & keys" },
        { key: "safety", label: "Safety Center" }, { key: "engine", label: "Video engine" }, { key: "trends", label: "Trend sources" },
      ]} active={tab} onChange={setTab} />
      {tab === "workspace" && <WorkspaceTab />}
      {tab === "connections" && <ConnectionsTab />}
      {tab === "safety" && <SafetyTab />}
      {tab === "engine" && <EngineTab />}
      {tab === "trends" && <TrendsTab />}
    </div>
  );
}

function WorkspaceTab() {
  const ws = useFetch(() => wsApi.get(""), []);
  const [form, setForm] = useState<any>(null);
  const [saved, setSaved] = useState("");
  const data: any = ws.data;
  const cur = form ?? { name: data?.name ?? "", niche: data?.niche ?? "", brand_voice: data?.brand_voice ?? "" };

  async function save() {
    try {
      await wsApi.patch("", cur);
      setSaved("Saved");
      setTimeout(() => setSaved(""), 2000);
      ws.reload();
    } catch (e: any) {
      alert(e.message);
    }
  }

  if (ws.loading) return <Card>Loading…</Card>;
  if (ws.error) return <Card>Error: {ws.error}</Card>;
  return (
    <Card className="max-w-[620px]">
      <Field label="Studio name"><input className="input" value={cur.name} onChange={(e) => setForm({ ...cur, name: e.target.value })} /></Field>
      <Field label="Niche"><input className="input" value={cur.niche} onChange={(e) => setForm({ ...cur, niche: e.target.value })} /></Field>
      <Field label="Brand voice" hint="Tone guidance honored by the Strategist and Scriptwriter."><textarea className="textarea" rows={3} value={cur.brand_voice} onChange={(e) => setForm({ ...cur, brand_voice: e.target.value })} /></Field>
      <div className="text-[12px] font-mono mb-3" style={{ color: "var(--text-faint)" }}>
        language: {data?.language ?? "—"} · timezone: {data?.timezone ?? "—"} · slug: {data?.slug ?? "—"}
      </div>
      <button className="btn-primary !text-xs" onClick={save}>Save workspace</button>
      {saved && <span className="ml-2 text-[12.5px]" style={{ color: "var(--accent)" }}>{saved}</span>}
    </Card>
  );
}

function ConnectionsTab() {
  const conns = useFetch(() => wsApi.get("/connections"), []);
  const [vals, setVals] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState("");
  const items: any[] = (conns.data as any)?.items ?? [];
  const byKey: Record<string, any> = Object.fromEntries(items.map((i: any) => [i.key, i]));

  async function save(key: string) {
    try {
      await wsApi.put("/connections", { key, value: vals[key] || null });
      setVals({ ...vals, [key]: "" });
      setSaved(key);
      setTimeout(() => setSaved(""), 2000);
      conns.reload();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function test(kind: string) {
    try {
      const r = await wsApi.post(`/connections/test-${kind}`, {});
      alert(JSON.stringify(r, null, 2).slice(0, 600));
    } catch (e: any) {
      alert(e.message);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex gap-2">
        <button className="btn-outline !text-xs" onClick={() => test("llm")}>Test LLM</button>
        <button className="btn-outline !text-xs" onClick={() => test("publishing")}>Test publishing paths</button>
      </div>
      {CONN_GROUPS.map((g) => (
        <Card key={g.title}>
          <b className="text-[13.5px]">{g.title}</b>
          <div className="mt-2 space-y-2.5">
            {g.keys.map((k) => {
              const meta = byKey[k];
              if (!meta) return null;
              return (
                <div key={k} className="grid md:grid-cols-[220px_1fr_auto] gap-2 items-center">
                  <div>
                    <div className="text-[12.5px] font-medium">{meta.label}</div>
                    {meta.hint && <div className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>{meta.hint}</div>}
                    <div className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                      {meta.configured ? `● set (${meta.source}) ${meta.masked ?? ""}` : "○ not set"}
                    </div>
                  </div>
                  <input className="input font-mono !text-xs" type={meta.secret ? "password" : "text"}
                    placeholder={meta.configured ? "•••• (enter to replace, blank clears)" : "paste value…"}
                    value={vals[k] ?? ""} onChange={(e) => setVals({ ...vals, [k]: e.target.value })} />
                  <button className="btn-outline !text-xs whitespace-nowrap" onClick={() => save(k)}>
                    {saved === k ? "Saved ✓" : "Save"}
                  </button>
                </div>
              );
            })}
          </div>
        </Card>
      ))}
    </div>
  );
}

function SafetyTab() {
  const s = useFetch(() => wsApi.get("/safety"), []);
  const [form, setForm] = useState<any>(null);
  const cur: any = form ?? (s.data as any)?.safety ?? s.data ?? {};
  const [saved, setSaved] = useState(false);

  async function save() {
    try {
      await wsApi.put("/safety", cur);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
      s.reload();
    } catch (e: any) {
      alert(e.message);
    }
  }

  if (s.loading) return <Card>Loading…</Card>;
  return (
    <Card className="max-w-[680px]">
      <div className="flex items-center gap-2 mb-3">
        <b className="text-[14px]">Safety Center</b>
        <Badge tone="info">auto-pause on triggers</Badge>
      </div>
      <div className="grid md:grid-cols-2 gap-x-5">
        {SAFETY_FIELDS.map((f) => (
          <Field key={f.key} label={f.label} hint={f.hint}>
            {f.type === "bool" ? (
              <select className="select" value={String(!!cur[f.key])} onChange={(e) => setForm({ ...cur, [f.key]: e.target.value === "true" })}>
                <option value="true">On (hold for approval)</option>
                <option value="false">Off (auto-publish)</option>
              </select>
            ) : (
              <input className="input font-mono" type="number" step="any" value={cur[f.key] ?? ""} onChange={(e) => setForm({ ...cur, [f.key]: e.target.value === "" ? "" : Number(e.target.value) })} />
            )}
          </Field>
        ))}
      </div>
      <button className="btn-primary !text-xs mt-1" onClick={save}>Save safety limits</button>
      {saved && <span className="ml-2 text-[12.5px]" style={{ color: "var(--accent)" }}>Saved ✓</span>}
    </Card>
  );
}

function EngineTab() {
  const e = useFetch(() => wsApi.get("/connections/video-engine"), []);
  const [url, setUrl] = useState<string | null>(null);
  const [timeout, setTimeout] = useState<string | null>(null);
  const data: any = e.data;

  async function save() {
    try {
      await wsApi.put("/connections/video-engine", {
        ...(url != null ? { base_url: url } : {}),
        ...(timeout != null ? { timeout_seconds: Number(timeout) } : {}),
      });
      setUrl(null);
      setTimeout(null);
      e.reload();
    } catch (err: any) {
      alert(err.message);
    }
  }

  return (
    <Card className="max-w-[620px]">
      <div className="flex gap-2 items-center"><b className="text-[14px]">Video engine</b>
        <Badge tone={data?.healthy ? "success" : "error"}>{data?.engine ?? "…"} · {data?.healthy ? "healthy" : "down"}</Badge>
      </div>
      <div className="text-[12.5px] font-mono mt-2 space-y-1" style={{ color: "var(--text-muted)" }}>
        <div>version: {data?.version ?? "—"}</div>
        <div>capabilities: {(data?.capabilities ?? []).join(", ") || "—"}</div>
      </div>
      <div className="grid md:grid-cols-2 gap-3 mt-3">
        <Field label="Base URL (MPT mode)"><input className="input font-mono !text-xs" value={url ?? data?.base_url ?? ""} onChange={(ev) => setUrl(ev.target.value)} /></Field>
        <Field label="Timeout seconds"><input className="input font-mono" type="number" value={timeout ?? data?.timeout_seconds ?? ""} onChange={(ev) => setTimeout(ev.target.value)} /></Field>
      </div>
      <button className="btn-primary !text-xs" onClick={save}>Save engine config</button>
    </Card>
  );
}

function TrendsTab() {
  const t = useFetch(() => wsApi.get("/trend-sources"), []);
  const [kind, setKind] = useState("google_trends");
  const [name, setName] = useState("");
  const items: any[] = (t.data as any)?.items ?? (t.data as any)?.sources ?? [];

  async function add() {
    try {
      await wsApi.post("/trend-sources", { kind, name: name || kind, enabled: true, config: {} });
      setName("");
      t.reload();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function remove(id: string) {
    if (!confirm("Remove this trend source?")) return;
    try {
      await wsApi.del(`/trend-sources/${id}`);
      t.reload();
    } catch (e: any) {
      alert(e.message);
    }
  }

  return (
    <Card className="max-w-[680px]">
      <b className="text-[14px]">Trend sources</b>
      <div className="mt-2 space-y-2">
        {items.map((s: any) => (
          <div key={s.id} className="flex items-center gap-2 text-[13px]">
            <Badge tone={s.enabled ? "success" : "muted"}>{s.kind}</Badge>
            <span>{s.name}</span>
            <button className="btn-ghost !text-[11px] !py-0.5 ml-auto" onClick={() => remove(s.id)}>Remove</button>
          </div>
        ))}
        {!items.length && <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>None configured — discovery falls back to Google Trends + Hacker News + keyless catalog.</div>}
      </div>
      <div className="flex gap-2 mt-4 flex-wrap">
        <select className="select !w-48" value={kind} onChange={(e) => setKind(e.target.value)}>
          {["google_trends", "reddit", "hacker_news", "newsdata", "coingecko", "devto", "youtube_trending", "youtube_channel"].map((k) => <option key={k} value={k}>{k}</option>)}
        </select>
        <input className="input !w-52" placeholder="Display name (optional)" value={name} onChange={(e) => setName(e.target.value)} />
        <button className="btn-primary !text-xs" onClick={add}>Add source</button>
      </div>
    </Card>
  );
}
