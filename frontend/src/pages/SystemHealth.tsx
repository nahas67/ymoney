import { useState } from "react";
import { wsApi, api } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, PageHeader, Section, Tabs, statusTone } from "../components/ui";
import { fmtUSD } from "../lib/format";

export default function SystemHealth() {
  const [tab, setTab] = useState<"status" | "engine" | "jobs" | "logs" | "costs">("status");
  const health = useFetch(() => api("GET", "/system/health"), [tab]);
  const readiness = useFetch(() => api("GET", "/system/readiness"), [tab]);
  const engine = useFetch(() => wsApi.get("/connections/video-engine"), [tab]);
  const tts = useFetch(() => wsApi.get("/connections/tts"), [tab]);
  const images = useFetch(() => wsApi.get("/connections/images"), [tab]);
  const jobs = useFetch(() => wsApi.get("/jobs?limit=40"), [tab]);
  const logs = useFetch(() => wsApi.get("/logs?limit=60"), [tab]);
  const costs = useFetch(() => wsApi.get("/costs"), [tab]);
  const [ttsText, setTtsText] = useState("YMONEY narration test.");
  const [ttsBusy, setTtsBusy] = useState(false);
  const [imgPrompt, setImgPrompt] = useState("");
  const [imgBusy, setImgBusy] = useState(false);
  const [imgResult, setImgResult] = useState("");

  const h: any = health.data;

  async function playTts() {
    setTtsBusy(true);
    try {
      const r = await fetch(`/api/v1/workspaces/${localStorage.getItem("ym_ws")}/connections/tts/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${localStorage.getItem("ym_token")}` },
        body: JSON.stringify({ text: ttsText }),
      });
      if (!r.ok) throw new Error(await r.text());
      const blob = await r.blob();
      new Audio(URL.createObjectURL(blob)).play();
    } catch (e: any) {
      alert(e.message);
    } finally {
      setTtsBusy(false);
    }
  }

  async function testImage() {
    setImgBusy(true);
    try {
      const r = await fetch(`/api/v1/workspaces/${localStorage.getItem("ym_ws")}/connections/images/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${localStorage.getItem("ym_token")}` },
        body: JSON.stringify({ prompt: imgPrompt }),
      });
      if (!r.ok) throw new Error(await r.text());
      const blob = await r.blob();
      setImgResult(URL.createObjectURL(blob));
    } catch (e: any) {
      alert(e.message);
    } finally {
      setImgBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader title="System Health" subtitle="Providers, engine, queue, logs and spend — the Birads-eye view."
        actions={<Badge tone={h?.status === "healthy" ? "success" : "warning"}>{h?.status ?? "…"}</Badge>} />
      <Tabs tabs={[
        { key: "status", label: "Status" }, { key: "engine", label: "Engine & voices" },
        { key: "jobs", label: "Jobs" }, { key: "logs", label: "Logs" }, { key: "costs", label: "Costs" },
      ]} active={tab} onChange={setTab} />

      {tab === "status" && (
        <div className="grid md:grid-cols-2 gap-4">
          <Card>
            <b className="text-[14px]">Services</b>
            <div className="mt-2 space-y-1.5 text-[13px]">
              <Row k="Database" v={h?.database ? "● ok" : "● down"} ok={h?.database} />
              <Row k={`Video engine (${h?.video_engine_name ?? "?"})`} v={h?.video_engine ? `● ok${h?.video_engine_version ? ` · ${h.video_engine_version}` : ""}` : "● down"} ok={h?.video_engine} />
              <Row k="LLM" v={h?.llm_provider ? "● ok" : "● down"} ok={h?.llm_provider} />
              <Row k={`TTS (${(h?.tts as any)?.provider ?? "?"})`} v={(h?.tts as any)?.healthy ? "● ok" : "● down"} ok={(h?.tts as any)?.healthy} />
            </div>
            <div className="mt-3">
              <div className="panel-label mb-1.5">Publishers</div>
              {Object.entries(h?.publishers ?? {}).map(([p, v]: any) => (
                <div key={p} className="flex justify-between text-[12.5px] py-1" style={{ borderBottom: "var(--seam)" }}>
                  <span className="font-mono">{p}</span>
                  <span><Badge tone={v.ready ? "success" : "muted"}>{v.mode}</Badge> <span style={{ color: "var(--text-faint)" }}>{v.detail}</span></span>
                </div>
              ))}
            </div>
          </Card>
          <Card>
            <b className="text-[14px]">Readiness ({(readiness.data as any)?.status})</b>
            <div className="mt-2 space-y-1.5 text-[13px]">
              {((readiness.data as any)?.checks ?? []).map((c: any) => (
                <div key={c.id} className="flex justify-between gap-2">
                  <span className="capitalize" style={{ color: "var(--text-muted)" }}>{c.id.replace(/_/g, " ")}</span>
                  <span className="truncate" style={{ color: c.status === "passed" ? "var(--accent)" : "var(--danger)" }}>{c.detail}</span>
                </div>
              ))}
            </div>
          </Card>
        </div>
      )}

      {tab === "engine" && (
        <div className="grid md:grid-cols-2 gap-4">
          <Card>
            <b className="text-[14px]">Video engine</b>
            <div className="text-[13px] mt-2 space-y-1 font-mono" style={{ color: "var(--text-muted)" }}>
              <div>engine: {(engine.data as any)?.engine}</div>
              <div>base: {(engine.data as any)?.base_url}</div>
              <div>healthy: {String((engine.data as any)?.healthy)}</div>
              <div>caps: {((engine.data as any)?.capabilities ?? []).join(", ")}</div>
            </div>
            <div className="mt-3">
              <b className="text-[13px]">TTS test ({(tts.data as any)?.provider})</b>
              <div className="flex gap-2 mt-2">
                <input className="input" value={ttsText} onChange={(e) => setTtsText(e.target.value)} />
                <button className="btn-outline !text-xs whitespace-nowrap" disabled={ttsBusy} onClick={playTts}>{ttsBusy ? "…" : "▶ Play"}</button>
              </div>
              <div className="text-[12px] mt-1.5 font-mono" style={{ color: "var(--text-faint)" }}>
                {(tts.data as any)?.voices?.length ?? 0} voices {(tts.data as any)?.error ? `· ${(tts.data as any).error}` : ""}
              </div>
            </div>
          </Card>
          <Card>
            <b className="text-[14px]">Image provider ({(images.data as any)?.provider})</b>
            <div className="flex gap-2 mt-2">
              <input className="input" placeholder="Test prompt…" value={imgPrompt} onChange={(e) => setImgPrompt(e.target.value)} />
              <button className="btn-outline !text-xs whitespace-nowrap" disabled={imgBusy} onClick={testImage}>{imgBusy ? "…" : "Generate"}</button>
            </div>
            {imgResult && <img src={imgResult} alt="test render" className="mt-3 rounded-lg w-full" />}
          </Card>
        </div>
      )}

      {tab === "jobs" && (
        <Section data={(jobs.data as any)?.items} loading={jobs.loading} error={jobs.error} onRetry={jobs.reload} empty="Queue empty" emptyHint="All durable work flows through here.">
          {(list) => (
            <Card pad={false} className="overflow-x-auto">
              <table className="table">
                <thead><tr><th>Type</th><th>Status</th><th>Retries</th><th>Error</th></tr></thead>
                <tbody>
                  {list.map((j: any) => (
                    <tr key={j.id}>
                      <td className="font-mono text-[12px]">{j.type}</td>
                      <td><Badge tone={statusTone(j.status)}>{j.status}</Badge></td>
                      <td className="font-mono">{j.retry_count}/{j.max_retries}</td>
                      <td className="max-w-[360px] truncate text-[12px]" style={{ color: "var(--text-muted)" }}>{j.last_error || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}
        </Section>
      )}

      {tab === "logs" && (
        <Section data={(logs.data as any)?.items} loading={logs.loading} error={logs.error} onRetry={logs.reload} empty="No logs" emptyHint="System, publishing and security events land here.">
          {(list) => (
            <Card pad={false}>
              <div className="max-h-[480px] overflow-y-auto p-3 font-mono text-[12px] space-y-1">
                {list.map((l: any) => (
                  <div key={l.id} style={{ color: l.level === "error" ? "var(--danger)" : undefined }}>
                    <span style={{ color: "var(--text-faint)" }}>[{l.category}/{l.level}]</span> {l.message}
                  </div>
                ))}
              </div>
            </Card>
          )}
        </Section>
      )}

      {tab === "costs" && (
        <Card>
          <b className="text-[14px]">Spend (24h)</b>
          <div className="mt-2 space-y-1.5">
            {Object.entries((costs.data as any)?.last_24h_by_category ?? {}).map(([k, v]: any) => (
              <div key={k} className="flex justify-between text-[13px] font-mono">
                <span style={{ color: "var(--text-muted)" }}>{k}</span><span>{fmtUSD(v)}</span>
              </div>
            ))}
            <div className="flex justify-between text-[13.5px] font-mono pt-2" style={{ borderTop: "var(--seam)" }}>
              <b>Total</b><b>{fmtUSD((costs.data as any)?.spent_last_24h_usd)}</b>
            </div>
            <div className="text-[12.5px]" style={{ color: "var(--text-muted)" }}>
              Budget ${(costs.data as any)?.daily_budget_usd} · remaining ${(costs.data as any)?.remaining_usd} · per-video ${(costs.data as any)?.per_video_budget_usd}
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}

function Row({ k, v, ok }: { k: string; v: string; ok?: boolean }) {
  return (
    <div className="flex justify-between">
      <span style={{ color: "var(--text-muted)" }}>{k}</span>
      <span className="font-mono" style={{ color: ok ? "var(--accent)" : "var(--danger)" }}>{v}</span>
    </div>
  );
}
