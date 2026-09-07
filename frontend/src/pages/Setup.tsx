import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, wsApi } from "../lib/api";
import { Badge, Card, PageHeader, StatusDot } from "../components/ui";

type Step = {
  id: string;
  title: string;
  why: string;
  done: boolean;
  minutes: string;
  link?: { label: string; url: string; external?: boolean };
  action?: { label: string; to: string };
};

export default function Setup() {
  const [steps, setSteps] = useState<Step[]>([]);
  const [loading, setLoading] = useState(true);
  const [readiness, setReadiness] = useState<any>(null);
  const nav = useNavigate();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [wsl, conn, accounts, rd] = await Promise.all([
        api("GET", "/workspaces"),
        wsApi.get("/connections").catch(() => ({ items: [] })),
        wsApi.get("/publishing/accounts").catch(() => ({ items: [] })),
        fetch("/api/v1/system/readiness").then((r) => r.json()).catch(() => null),
      ]);
      setReadiness(rd);

      const me = (wsl.items ?? [])[0];
      const llmKey = (conn.items ?? []).find((c: any) => c.key === "llm.api_key");
      const googleCid = (conn.items ?? []).find((c: any) => c.key === "google.client_id");
      const relayKey = (conn.items ?? []).find((c: any) => c.key === "upload_post.api_key");

      setSteps([
        {
          id: "workspace",
          title: "Workspace & niche",
          why: "Trend discovery and audience-fit scoring key on this.",
          done: Boolean(me?.niche),
          minutes: "1 min",
          action: { label: "Set your niche", to: "/settings" },
        },
        {
          id: "llm",
          title: "AI provider key",
          why: "Powers research, script writing, quality control, and metadata.",
          done: Boolean(llmKey?.configured),
          minutes: "3 min",
          link: {
            label: "Get an OpenAI key",
            url: "https://platform.openai.com/api-keys",
            external: true,
          },
          action: { label: "Paste your key", to: "/settings" },
        },
        {
          id: "video-engine",
          title: "Video engine (MoneyPrinterTurbo)",
          why: "Renders the actual video files.",
          done: rd?.checks?.find((c: any) => c.id === "video_engine")?.status === "passed",
          minutes: "5 min",
          action: { label: "Check engine", to: "/health" },
        },
        {
          id: "publishing",
          title: "Connect social accounts",
          why: "Without this, videos render but can't be published.",
          done: (accounts.items ?? []).length > 0,
          minutes: "5–15 min",
        },
      ]);
    } catch {} finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const doneCount = steps.filter((s) => s.done).length;
  const allDone = doneCount === steps.length;

  if (loading) return <div className="py-10 text-center" style={{ color: "var(--text-muted)" }}>Loading…</div>;

  return (
    <div className="space-y-6 max-w-2xl">
      <PageHeader
        title="Setup"
        subtitle={allDone ? "Everything is configured. Press START." : `${doneCount}/${steps.length} complete`}
      />

      {/* Readiness summary */}
      {readiness && (
        <Card>
          <div className="flex items-center gap-2 mb-3">
            <StatusDot tone={readiness.status === "ready" ? "success" : "warning"} />
            <span className="font-semibold text-sm">
              {readiness.status === "ready" ? "READY TO RUN" : `BLOCKED by ${readiness.blocking_failures.join(", ")}`}
            </span>
          </div>
          <div className="space-y-1">
            {(readiness.checks ?? []).map((c: any) => (
              <div key={c.id} className="flex items-center justify-between text-[13px]">
                <span className="flex items-center gap-1.5">
                  <StatusDot tone={c.status === "passed" ? "success" : c.blocking ? "error" : "neutral"} />
                  {c.id.replace(/_/g, " ")}
                </span>
                <span style={{ color: "var(--text-muted)" }}>{c.detail}</span>
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* Steps */}
      <div className="space-y-3">
        {steps.map((s, i) => (
          <Card key={s.id} className={s.done ? "opacity-60" : ""}>
            <div className="flex items-start gap-4">
              <div className={`grid place-items-center w-8 h-8 rounded-full text-sm font-bold shrink-0 mt-0.5 ${
                s.done ? "bg-emerald-500 text-white" : ""
              }`} style={!s.done ? { background: "var(--bg-subtle)", color: "var(--text-muted)" } : undefined}>
                {s.done ? "✓" : i + 1}
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-3">
                  <h3 className={`font-medium text-sm ${s.done ? "line-through opacity-60" : ""}`}>{s.title}</h3>
                  <Badge tone={s.done ? "success" : "neutral"}>{s.minutes}</Badge>
                </div>
                <p className="text-[13px] mt-0.5" style={{ color: "var(--text-muted)" }}>{s.why}</p>
                <div className="mt-2 flex gap-2 flex-wrap">
                  {s.link && (
                    <a href={s.link.url} target="_blank" rel="noreferrer" className="btn-outline !py-1 !px-2.5 text-xs">
                      {s.link.label} ↗
                    </a>
                  )}
                  {s.action && !s.done && (
                    <button className="btn-primary !py-1 !px-2.5 text-xs" onClick={() => nav(s.action!.to)}>
                      {s.action.label}
                    </button>
                  )}
                  {!s.action && !s.link && s.id === "publishing" && (
                    <PublishingGuide onDone={load} />
                  )}
                </div>
              </div>
            </div>
          </Card>
        ))}
      </div>

      {/* Publishing deep-dive */}
      <Card>
        <h3 className="font-semibold text-sm mb-3">Publishing setup paths</h3>
        <div className="grid sm:grid-cols-2 gap-4">
          <PathCard
            title="YouTube (direct OAuth)"
            difficulty="Intermediate"
            time="~10 min"
            cost="Free"
            steps={[
              "Go to console.cloud.google.com",
              "Create a project, enable YouTube Data API v3",
              "Configure OAuth consent screen (External)",
              "Create OAuth Client ID (Web application)",
              `Add redirect URI: ${window.location.origin}/api/v1/workspaces/${localStorage.getItem("ym_ws")}/publishing/oauth/youtube/callback`,
              "Copy client_id + client_secret into YMONEY Connections",
              "Click Connect YouTube on the Publishing page",
            ]}
            links={[{ label: "Google Cloud Console", url: "https://console.cloud.google.com/apis/credentials" }]}
          />
          <PathCard
            title="Upload-Post relay (easier)"
            difficulty="Beginner"
            time="~3 min"
            cost="Free tier: 10 uploads/mo"
            steps={[
              "Sign up at upload-post.com",
              "Connect TikTok / Instagram / YouTube through their dashboard",
              "Generate an API key from their settings",
              "Paste API key + username into YMONEY Connections",
            ]}
            links={[{ label: "Upload-Post Dashboard", url: "https://app.upload-post.com" }]}
          />
        </div>
      </Card>

      {allDone && (
        <Link to="/" className="btn-primary w-full !py-3 text-base inline-flex justify-center">
          GO TO COMMAND CENTER → START
        </Link>
      )}
    </div>
  );
}

function PathCard({ title, difficulty, time, cost, steps: items, links }: any) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-lg border p-4" style={{ borderColor: "var(--border)" }}>
      <div className="flex items-center justify-between mb-1">
        <h4 className="font-medium text-sm">{title}</h4>
        <Badge tone={difficulty === "Beginner" ? "success" : "info"}>{difficulty}</Badge>
      </div>
      <p className="text-[12px]" style={{ color: "var(--text-muted)" }}>{time} · {cost}</p>
      <button className="btn-outline !py-1 !px-2 text-xs mt-2" onClick={() => setOpen(!open)}>
        {open ? "Hide steps" : "Show steps"}
      </button>
      {open && (
        <ol className="list-decimal pl-4 mt-2 space-y-1 text-[12px]" style={{ color: "var(--text-muted)" }}>
          {items.map((s: string, i: number) => <li key={i}>{s}</li>)}
        </ol>
      )}
      {links && (
        <div className="mt-2">
          {links.map((l: any) => (
            <a key={l.url} href={l.url} target="_blank" rel="noreferrer"
               className="text-xs underline" style={{ color: "var(--accent)" }}>
              {l.label} ↗
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

function PublishingGuide({ onDone }: any) {
  return (
    <div className="mt-2 space-y-2">
      <Link to="/publishing" className="btn-primary !py-1 !px-2.5 text-xs inline-flex">
        Go to Publishing → Connect
      </Link>
      <p className="text-[11px]" style={{ color: "var(--text-muted)" }}>
        Or see detailed setup paths below.
      </p>
    </div>
  );
}
