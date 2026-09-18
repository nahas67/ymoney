import { useState } from "react";
import { wsApi, api } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, PageHeader, Section, Tabs, statusTone } from "../components/ui";
import { fmtAgo, fmtCompact, platformLabel } from "../lib/format";

const OAUTH_PLATFORMS = ["youtube", "tiktok", "facebook", "instagram"];

export default function Publishing() {
  const [tab, setTab] = useState<"accounts" | "jobs" | "posts">("accounts");
  const accounts = useFetch(() => wsApi.get("/publishing/accounts"), []);
  const jobs = useFetch(() => wsApi.get("/publishing/jobs?limit=60"), []);
  const posts = useFetch(() => wsApi.get("/publishing/posts?limit=60"), []);
  const diag = useFetch(() => wsApi.post("/connections/test-publishing", {}), []);
  const health = useFetch(() => api("GET", "/system/health"), []);
  const [busy, setBusy] = useState("");

  async function connect(platform: string) {
    setBusy(platform);
    try {
      const r = await wsApi.get(`/publishing/oauth/${platform}/start`);
      window.open(r.authorize_url, "ymoney-oauth", "width=560,height=700");
      const before = new Set(((await wsApi.get("/publishing/accounts")).items ?? []).map((a: any) => a.id));
      const deadline = Date.now() + 120_000;
      while (Date.now() < deadline) {
        await new Promise((r2) => setTimeout(r2, 2500));
        const after = await wsApi.get("/publishing/accounts");
        if ((after.items ?? []).some((a: any) => !before.has(a.id))) break;
      }
      accounts.reload();
      diag.reload();
    } catch (e: any) {
      alert(e.message);
    } finally {
      setBusy("");
    }
  }

  async function disconnect(id: string) {
    if (!confirm("Disconnect this account?")) return;
    await wsApi.del(`/publishing/accounts/${id}`);
    accounts.reload();
  }

  const pubs: any = (health.data as any)?.publishers ?? {};
  const allReal = OAUTH_PLATFORMS.every((p) => pubs[p]?.mode !== "mock");

  return (
    <div className="space-y-4">
      <PageHeader title="Publishing" subtitle="Accounts, OAuth connects, relay setup and every delivery with attempts and remote IDs."
        actions={<Badge tone={allReal ? "success" : "warning"}>{allReal ? "REAL MODE" : "MOCK / PARTIAL"}</Badge>} />

      {(diag.data as any) && !(diag.data as any).ok && (
        <Card style={{ borderColor: "var(--warn)" }}>
          <b className="text-[13.5px]">⚠️ {(diag.data as any).detail || "No real publishing path configured."}</b>
          <p className="text-[12.5px] mt-1" style={{ color: "var(--text-muted)" }}>
            Two ways live: connect OAuth accounts below, or paste an Upload-Post relay key in Settings → Connections (relay covers TikTok, Instagram, YouTube, Facebook with auto-transcoding).
          </p>
        </Card>
      )}

      <Tabs tabs={[
        { key: "accounts", label: "Accounts", count: (accounts.data as any)?.items?.length },
        { key: "jobs", label: "Delivery jobs", count: (jobs.data as any)?.items?.length },
        { key: "posts", label: "Published posts", count: (posts.data as any)?.items?.length },
      ]} active={tab} onChange={setTab} />

      {tab === "accounts" && (
        <div className="grid md:grid-cols-2 gap-3">
          {OAUTH_PLATFORMS.map((p) => {
            const acc = ((accounts.data as any)?.items ?? []).filter((a: any) => a.platform === p);
            const info = pubs[p];
            return (
              <Card key={p}>
                <div className="flex items-center gap-2 mb-2">
                  <b className="text-[14px]">{platformLabel(p)}</b>
                  <Badge tone={acc.length ? "success" : info?.via_relay ? "info" : "muted"}>
                    {acc.length ? `${acc.length} connected` : info?.via_relay ? "via relay" : "not connected"}
                  </Badge>
                </div>
                {acc.map((a: any) => (
                  <div key={a.id} className="flex items-center gap-2 text-[12.5px] py-1.5" style={{ borderBottom: "var(--seam)" }}>
                    <span>{a.display_name || a.external_id || p}</span>
                    <Badge tone={statusTone(a.status)}>{a.status}</Badge>
                    <button className="btn-ghost !text-[11px] !py-0.5 ml-auto" onClick={() => disconnect(a.id)}>Disconnect</button>
                  </div>
                ))}
                <button className="btn-outline !text-xs mt-2.5" disabled={busy === p} onClick={() => connect(p)}>
                  {busy === p ? "Waiting for OAuth…" : `Connect ${platformLabel(p)}`}
                </button>
                {info?.detail && <div className="text-[11.5px] mt-1.5 font-mono" style={{ color: "var(--text-faint)" }}>{info.detail}</div>}
              </Card>
            );
          })}
        </div>
      )}

      {tab === "jobs" && (
        <Section data={(jobs.data as any)?.items} loading={jobs.loading} error={jobs.error} onRetry={jobs.reload}
          empty="No deliveries yet" emptyHint="Approved videos publish here with per-platform attempts.">
          {(list) => (
            <Card pad={false} className="overflow-x-auto">
              <table className="table">
                <thead><tr><th>Platform</th><th>Status</th><th>Attempts</th><th>Remote</th><th>Error</th><th>When</th></tr></thead>
                <tbody>
                  {list.map((j: any) => (
                    <tr key={j.id}>
                      <td>{platformLabel(j.platform)}</td>
                      <td><Badge tone={statusTone(j.status)}>{j.status}</Badge></td>
                      <td className="font-mono">{j.attempt}</td>
                      <td className="max-w-[220px] truncate text-[12px]">
                        {j.remote_url ? <a href={j.remote_url} target="_blank" rel="noreferrer" style={{ color: "var(--info)" }}>{j.remote_post_id || "link"}</a> : <span style={{ color: "var(--text-faint)" }}>—</span>}
                      </td>
                      <td className="max-w-[280px] truncate text-[12px]" style={{ color: "var(--text-muted)" }}>{j.error || "—"}</td>
                      <td className="text-[12px]" style={{ color: "var(--text-muted)" }}>{fmtAgo(j.published_at ?? j.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}
        </Section>
      )}

      {tab === "posts" && (
        <Section data={(posts.data as any)?.items} loading={posts.loading} error={posts.error} onRetry={posts.reload}
          empty="Nothing published" emptyHint="Successful deliveries land here with live metrics.">
          {(list) => (
            <div className="grid md:grid-cols-2 gap-3">
              {list.map((p: any) => (
                <Card key={p.id} style={{ padding: 14 }}>
                  <div className="flex gap-2 items-center flex-wrap mb-1">
                    <Badge tone="muted">{platformLabel(p.platform)}</Badge>
                    {p.is_mock && <Badge tone="warning">MOCK</Badge>}
                  </div>
                  <div className="text-[13.5px] font-medium">{p.title || "(untitled)"}</div>
                  <div className="flex gap-3 mt-1.5 text-[12px] font-mono" style={{ color: "var(--text-muted)" }}>
                    <span>👁 {fmtCompact(p.metrics?.views)}</span>
                    <span>♥ {fmtCompact(p.metrics?.likes)}</span>
                    <span>💬 {fmtCompact(p.metrics?.comments)}</span>
                    {p.metrics?.completion_rate != null && <span>✓ {(p.metrics.completion_rate * 100).toFixed(0)}%</span>}
                  </div>
                  {p.remote_url && <a className="text-[12px]" style={{ color: "var(--info)" }} href={p.remote_url} target="_blank" rel="noreferrer">Open post ↗</a>}
                </Card>
              ))}
            </div>
          )}
        </Section>
      )}
    </div>
  );
}
