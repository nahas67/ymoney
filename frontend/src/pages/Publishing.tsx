import { useCallback, useEffect, useState } from "react";
import { wsApi } from "../lib/api";
import { AsyncSection, Badge, Card, PageHeader, Tabs, fmtDate } from "../components/ui";

const PLATFORMS = ["youtube", "tiktok", "facebook", "instagram"];

export default function Publishing() {
  const [tab, setTab] = useState("accounts");
  const [accounts, setAccounts] = useState<any[]>([]);
  const [jobs, setJobs] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [health, setHealth] = useState<any>(null);
  const [publishingStatus, setPublishingStatus] = useState<any>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [a, j, h, pub] = await Promise.all([
        wsApi.get("/publishing/accounts"),
        wsApi.get("/publishing/jobs?limit=50"),
        apiHealth(),
        wsApi.post("/connections/test-publishing", {}).catch(() => null),
      ]);
      setAccounts(a.items ?? []);
      setJobs(j.items ?? []);
      setHealth(h);
      setPublishingStatus(pub);
    } catch (e: any) { setError(e.message); } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const [connectPlatform, setConnectPlatform] = useState<string | null>(null);
  const [connectName, setConnectName] = useState("");

  async function openConnect(platform: string) {
    if (platform === "youtube") {
      // Real OAuth flow via backend
      try {
        const r = await wsApi.get("/publishing/oauth/youtube/start");
        window.open(r.authorize_url, "ymoney-oauth", "width=520,height=680");
        // poll until the account appears (callback stores it)
        const deadline = Date.now() + 120_000;
        const before = new Set((await wsApi.get("/publishing/accounts")).items.map((a: any) => a.platform));
        while (Date.now() < deadline) {
          await new Promise((r2) => setTimeout(r2, 2500));
          const after = await wsApi.get("/publishing/accounts");
          if ((after.items ?? []).some((a: any) => a.platform === "youtube" && !before.has("youtube"))) break;
          if (!(window as any).closed) continue;
        }
        load();
      } catch (e: any) {
        alert(e.message);
      }
      return;
    }
  }

  async function confirmConnect() {
    if (!connectPlatform) return;
    await wsApi.post("/publishing/accounts", {
      platform: connectPlatform,
      display_name: connectName || connectPlatform,
      access_token: "",
      refresh_token: "",
    });
    setConnectPlatform(null); setConnectName("");
    load();
  }

  async function disconnect(id: string) {
    await wsApi.del(`/publishing/accounts/${id}`);
    load();
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Publishing"
        subtitle="Connected accounts and delivery history. Tokens are encrypted at rest and never displayed."
        actions={<Badge tone={health?.publishers?.youtube?.mode === "mock" ? "warning" : "success"}>
          {health?.publishers?.youtube?.mode === "mock" ? "MOCK MODE" : "REAL MODE"}
        </Badge>}
      />

      <Tabs
        tabs={[
          { key: "accounts", label: "Accounts", count: accounts.length },
          { key: "history", label: "Delivery history", count: jobs.length },
        ]}
        active={tab}
        onChange={setTab}
      />

      {tab === "accounts" && (
        <div className="space-y-4">
          {publishingStatus && !publishingStatus.ok && (
            <Card className="border border-amber-500/40">
              <h3 className="font-semibold text-sm mb-1.5">⚠️ Publishing is simulated</h3>
              <p className="text-[13px] mb-3" style={{ color: "var(--text-muted)" }}>
                {publishingStatus.detail || "No real publishing path configured."} Videos are
                produced and QC'd, but delivery is a labeled local simulation. Pick one path to
                go live:
              </p>
              <div className="grid sm:grid-cols-2 gap-3">
                <div className="rounded-lg p-3.5 border" style={{ borderColor: "var(--border)" }}>
                  <div className="font-medium text-sm mb-1">Path 1 · Upload-Post relay (easiest)</div>
                  <p className="text-[12px] mb-2.5" style={{ color: "var(--text-muted)" }}>
                    One API key → TikTok, Instagram, YouTube, Facebook, LinkedIn. Free tier:
                    10 uploads/month, no app reviews needed.
                  </p>
                  <div className="flex gap-2">
                    <a className="btn-primary !py-1.5 !px-3 text-xs" href="https://www.upload-post.com" target="_blank" rel="noreferrer">Get free API key ↗</a>
                    <a className="btn-outline !py-1.5 !px-3 text-xs" href="/settings?tab=connections">Paste key in Settings</a>
                  </div>
                </div>
                <div className="rounded-lg p-3.5 border" style={{ borderColor: "var(--border)" }}>
                  <div className="font-medium text-sm mb-1">Path 2 · YouTube direct OAuth</div>
                  <p className="text-[12px] mb-2.5" style={{ color: "var(--text-muted)" }}>
                    Free Google Cloud OAuth client (youtube.upload scope). Unlimited uploads
                    to your channel; setup takes ~5 minutes.
                  </p>
                  <div className="flex gap-2">
                    <button className="btn-primary !py-1.5 !px-3 text-xs" onClick={() => openConnect("youtube")}>Connect YouTube</button>
                    <a className="btn-outline !py-1.5 !px-3 text-xs" href="/settings?tab=connections">Add Google OAuth keys</a>
                  </div>
                </div>
              </div>
            </Card>
          )}
          {publishingStatus?.relay_valid === true && (
            <Card className="border border-emerald-500/40">
              <span className="text-[13px]">✅ Upload-Post relay active — plan <b>{publishingStatus.relay_plan || "free"}</b>{publishingStatus.relay_email ? ` (${publishingStatus.relay_email})` : ""}. Cycles now publish for real.</span>
            </Card>
          )}
          <AsyncSection data={accounts} loading={loading} error={error} onRetry={load}
            empty="No accounts connected" emptyHint="Publishing runs in clearly-labeled mock mode until you connect real accounts.">
            {(list) => (
              <Card pad={false} className="overflow-x-auto">
                <table className="table">
                  <thead><tr><th>Platform</th><th>Account</th><th>Status</th><th>Token health</th><th>Expires</th><th></th></tr></thead>
                  <tbody>
                    {(list as any[]).map((a) => (
                      <tr key={a.id}>
                        <td className="capitalize font-medium">{a.platform}</td>
                        <td>{a.display_name}</td>
                        <td><AccountStatusBadge status={a.status} /></td>
                        <td>
                          <Badge tone={a.status === "connected" ? "success" : a.token_expires_at ? "warning" : "neutral"}>
                            {a.status === "connected" ? "authorized" : "requires review"}
                          </Badge>
                        </td>
                        <td style={{ color: "var(--text-muted)" }}>{fmtDate(a.token_expires_at)}</td>
                        <td className="text-right">
                          <button className="btn-outline !py-1 !px-2 text-xs" onClick={() => disconnect(a.id)}>Disconnect</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Card>
            )}
          </AsyncSection>

          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: "var(--text-muted)" }}>Connect a platform</h3>
            <div className="flex gap-2 flex-wrap">
              {PLATFORMS.filter((p) => !accounts.some((a) => a.platform === p)).map((p) => (
                <button key={p} className="btn-outline capitalize" onClick={() => openConnect(p)}>
                  + Connect {p}
                </button>
              ))}
            </div>
          </div>

          {health?.publishers && (
            <Card>
              <h3 className="text-xs font-semibold uppercase tracking-wider mb-2.5" style={{ color: "var(--text-muted)" }}>
                Provider readiness
              </h3>
              <table className="table">
                <tbody>
                  {Object.entries(health.publishers).map(([name, info]: any) => (
                    <tr key={name}>
                      <td className="capitalize w-28">{name}</td>
                      <td><Badge tone={info.mode === "mock" ? "warning" : "success"}>{info.mode}</Badge></td>
                      <td style={{ color: "var(--text-muted)" }}>{info.detail}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}
        </div>
      )}

      {tab === "history" && (
        <AsyncSection data={jobs} loading={loading} error={error} onRetry={load}
          empty="No publishing attempts yet">
          {(list) => (
            <Card pad={false} className="overflow-x-auto">
              <table className="table">
                <thead><tr><th>Platform</th><th>Status</th><th>Link</th><th>Error</th><th>When</th></tr></thead>
                <tbody>
                  {(list as any[]).map((j) => (
                    <tr key={j.id}>
                      <td className="capitalize">{j.platform}</td>
                      <td><Badge tone={j.status === "PUBLISHED" ? "success" : j.status === "FAILED" ? "error" : j.status === "QUEUED" || j.status === "RETRYING" ? "info" : "neutral"}>{j.status.toLowerCase()}</Badge>
                        {j.attempt > 1 && <span className="text-[10px] ml-1.5" style={{ color: "var(--text-muted)" }}>try #{j.attempt}</span>}
                      </td>
                      <td>{j.remote_url?.startsWith("http") ? <a className="underline text-xs" href={j.remote_url} target="_blank" rel="noreferrer">open</a> : "—"}</td>
                      <td className="max-w-[240px] truncate text-[12px] text-red-500">{j.error}</td>
                      <td className="text-[12px]" style={{ color: "var(--text-muted)" }}>{fmtDate(j.published_at ?? j.scheduled_at ?? j.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}
        </AsyncSection>
      )}
      {connectPlatform && (
        <div className="fixed inset-0 bg-black/50 grid place-items-center p-4 z-50" onClick={() => setConnectPlatform(null)}>
          <div className="card w-full max-w-md p-6 space-y-4" onClick={(e) => e.stopPropagation()}>
            <h3 className="font-semibold capitalize">Connect {connectPlatform}</h3>
            <label className="block">
              <span className="block text-xs mb-1.5" style={{ color: "var(--text-muted)" }}>Display name</span>
              <input className="input" value={connectName}
                     onChange={(e) => setConnectName(e.target.value)}
                     placeholder={connectPlatform} autoFocus />
            </label>
            <p className="text-[12px]" style={{ color: "var(--text-muted)" }}>
              Tokens can be added now or later — publishing stays clearly-labeled mock until
              real credentials are stored (encrypted server-side).
            </p>
            <div className="flex justify-end gap-2">
              <button className="btn-outline" onClick={() => setConnectPlatform(null)}>Cancel</button>
              <button className="btn-primary" onClick={confirmConnect}>Save account</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function AccountStatusBadge({ status }: { status: string }) {
  const map: Record<string, any> = {
    connected: ["success", "CONNECTED"],
    expired: ["warning", "EXPIRED"],
    error: ["error", "ERROR"],
  };
  const [tone, label] = map[status] ?? ["neutral", status.toUpperCase()];
  return <Badge tone={tone}>{label}</Badge>;
}

async function apiHealth() {
  const res = await fetch("/api/v1/system/health");
  if (!res.ok) throw new Error(`health ${res.status}`);
  return res.json();
}

