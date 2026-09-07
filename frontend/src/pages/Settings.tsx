import { useCallback, useEffect, useState } from "react";
import { api, wsApi } from "../lib/api";
import { Badge, Card, Field, PageHeader, Tabs, Toggle, useToast } from "../components/ui";


const SECTIONS = [
  ["general", "General"], ["connections", "Connections & Keys"], ["video", "Video Engine"],
  ["voice", "Voice (TTS)"], ["images", "Images"],
  ["workspace", "Workspace"], ["ai", "AI & Models"], ["agents", "Agents"], ["trends", "Trends"],
  ["content", "Content rules"], ["automation", "Automation"], ["safety", "Safety & budget"],
  ["publishing", "Publishing"], ["appearance", "Appearance"],
] as const;

export default function Settings() {
  const [section, setSection] = useState<string>("general");
  return (
    <div className="space-y-5">
      <PageHeader title="Settings" subtitle="Everything configurable, with safe defaults." />
      <Tabs tabs={SECTIONS.map(([k, l]) => ({ key: k, label: l }))} active={section} onChange={setSection} />
      {section === "general" && <General />}
      {section === "connections" && <Connections />}
      {section === "video" && <VideoEngine />}
      {section === "voice" && <VoiceSection />}
      {section === "images" && <ImagesSection />}
      {section === "workspace" && <WorkspaceSection />}
      {section === "ai" && <AiSection />}
      {section === "agents" && <AgentsSection />}
      {section === "trends" && <TrendsSection />}
      {section === "content" && <ContentRules />}
      {section === "automation" && <Automation />}
      {section === "safety" && <Safety />}
      {section === "publishing" && <PublishingSection />}
      {section === "appearance" && <Appearance />}
    </div>
  );
}

/* ------------------------------- GENERAL --------------------------------- */

function General() {
  const [theme, setTheme] = useState(localStorage.getItem("ym_theme") ?? "dark");
  function set(t: string) {
    localStorage.setItem("ym_theme", t);
    if (t === "light") document.documentElement.classList.remove("dark");
    else document.documentElement.classList.add("dark");
    setTheme(t);
  }
  const [me, setMe] = useState<any>(null);
  useEffect(() => { api("GET", "/auth/me").then(setMe).catch(() => {}); }, []);
  return (
    <Card className="max-w-xl">
      <h3 className="text-sm font-semibold mb-4">Profile</h3>
      <dl className="text-sm space-y-1.5 mb-6">
        <div className="flex justify-between"><dt style={{ color: "var(--text-muted)" }}>Email</dt><dd>{me?.email ?? "…"}</dd></div>
        <div className="flex justify-between"><dt style={{ color: "var(--text-muted)" }}>Display name</dt><dd>{me?.display_name ?? "…"}</dd></div>
        <div className="flex justify-between"><dt style={{ color: "var(--text-muted)" }}>Workspaces</dt><dd>{me?.workspaces?.length ?? "…"}</dd></div>
      </dl>
      <h3 className="text-sm font-semibold mb-2">Theme</h3>
      <div className="flex gap-2">
        {["dark", "light"].map((t) => (
          <button key={t} className={`btn-outline capitalize ${theme === t ? "!border-emerald-500 !text-emerald-600 dark:!text-emerald-400" : ""}`} onClick={() => set(t)}>
            {t}
          </button>
        ))}
      </div>
    </Card>
  );
}

/* ----------------------------- VIDEO ENGINE ------------------------------- */

function VideoEngine() {
  const [info, setInfo] = useState<any>(null);
  const [baseUrl, setBaseUrl] = useState("");
  const [timeout, setTimeoutVal] = useState("");
  const { push } = useToast();

  const load = useCallback(async () => {
    const r = await wsApi.get("/connections/video-engine");
    setInfo(r);
    setBaseUrl(r.base_url ?? "");
    setTimeoutVal(String(r.timeout_seconds ?? ""));
  }, []);
  useEffect(() => { load(); }, [load]);

  async function save() {
    try {
      const body: any = {};
      if (baseUrl && baseUrl !== info.base_url) body.base_url = baseUrl;
      if (timeout && String(info.timeout_seconds) !== timeout) body.timeout_seconds = parseInt(timeout, 10);
      if (Object.keys(body).length) {
        const updated = await wsApi.put("/connections/video-engine", body);
        setInfo(updated);
        push("success", "Video engine settings applied");
      }
    } catch (e: any) { push("error", e.message); }
  }

  if (!info) return null;
  const isMock = info.engine === "mock";

  return (
    <div className="space-y-5 max-w-2xl">
      <Card>
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-semibold">
            Active engine: <span className="font-mono">{isMock ? "MockVideoEngine" : "MoneyPrinterTurbo"}</span>
          </h3>
          <Badge tone={info.healthy ? "success" : "error"}>
            {info.healthy ? "HEALTHY" : "UNAVAILABLE"}
          </Badge>
        </div>

        {!isMock && (
          <div className="space-y-4">
            <Field label="Engine base URL"
                   hint="Where MoneyPrinterTurbo's API is reachable. Saved encrypted; applied immediately.">
              <input className="input font-mono" value={baseUrl}
                     onChange={(e) => setBaseUrl(e.target.value)}
                     placeholder="http://127.0.0.1:8081" />
            </Field>
            <Field label="Render timeout (seconds)">
              <input className="input !w-44" type="number" min={60} max={7200}
                     value={timeout} onChange={(e) => setTimeoutVal(e.target.value)} />
            </Field>
            <div className="flex items-center gap-3">
              <button className="btn-primary" onClick={save}>Apply</button>
              <button className="btn-outline" onClick={load}>Re-check health</button>
            </div>
          </div>
        )}

        {isMock && (
          <p className="text-[13px]" style={{ color: "var(--text-muted)" }}>
            Mock engine produces simulated artifacts for development/CI — clearly labeled
            everywhere. Set <code>VIDEO_ENGINE=moneyprinterturbo</code> and the engine base
            URL above to render real videos.
          </p>
        )}
      </Card>

      <Card>
        <h3 className="text-sm font-semibold mb-2">Capabilities</h3>
        <div className="flex flex-wrap gap-1.5 mb-4">
          {(info.capabilities ?? []).map((c: string) => (
            <Badge key={c} tone={info.healthy ? "success" : "neutral"}>{c.toLowerCase().replace(/_/g, " ")}</Badge>
          ))}
          {(info.capabilities ?? []).length === 0 && (
            <span className="text-[13px]" style={{ color: "var(--text-muted)" }}>unavailable while the engine is down</span>
          )}
        </div>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
          <div><dt style={{ color: "var(--text-muted)" }}>Version</dt><dd className="font-mono">{info.version ?? "—"}</dd></div>
          <div><dt style={{ color: "var(--text-muted)" }}>Default voice</dt><dd className="font-mono">{info.defaults.voice}</dd></div>
          <div><dt style={{ color: "var(--text-muted)" }}>Subtitles</dt><dd>{info.defaults.subtitles ? "on" : "off"}</dd></div>
          <div><dt style={{ color: "var(--text-muted)" }}>Default aspect</dt><dd className="font-mono">{info.defaults.aspect_ratio}</dd></div>
          <div><dt style={{ color: "var(--text-muted)" }}>Config source</dt><dd>{JSON.stringify(info.sources)}</dd></div>
          <div><dt style={{ color: "var(--text-muted)" }}>Max concurrent renders</dt><dd>see Safety & budget</dd></div>
        </dl>
      </Card>

      <p className="text-[12px]" style={{ color: "var(--text-muted)" }}>
        The engine renders; YMONEY orchestrates everything else. Swapping engines later
        requires no changes to autopilot, agents, QC, publishing or this UI.
      </p>
    </div>
  );
}

/* -------------------------------- VOICE ---------------------------------- */

function VoiceSection() {
  const [info, setInfo] = useState<any>(null);
  const [voice, setVoice] = useState("");
  const [text, setText] = useState("");
  const [testing, setTesting] = useState(false);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [audioMeta, setAudioMeta] = useState<{ provider: string; mock: boolean } | null>(null);
  const [error, setError] = useState("");
  const { push } = useToast();

  const load = useCallback(async () => {
    try {
      const r = await wsApi.get("/connections/tts");
      setInfo(r);
    } catch (e: any) {
      setError(e.message);
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  async function runTest() {
    setTesting(true); setError("");
    try {
      const blob = await wsApi.postBlob("/connections/tts/test", {
        text, voice,
      });
      if (audioUrl) URL.revokeObjectURL(audioUrl);
      const url = URL.createObjectURL(blob.blob);
      setAudioUrl(url);
      setAudioMeta({ provider: blob.headers.get("X-TTS-Provider") ?? "?", mock: blob.headers.get("X-TTS-Mock") === "1" });
    } catch (e: any) {
      setError(e.message);
    } finally {
      setTesting(false);
    }
  }

  if (error && !info) return <Card><p className="text-sm text-red-500">{error}</p></Card>;
  if (!info) return null;
  const isMock = info.provider === "mock";

  return (
    <div className="space-y-5 max-w-2xl">
      <Card>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold">
            Narration provider: <span className="font-mono">{info.provider}</span>
          </h3>
          <div className="flex gap-2">
            {isMock && <Badge tone="warning">MOCK</Badge>}
            <Badge tone={info.healthy ? "success" : "error"}>{info.healthy ? "HEALTHY" : "UNAVAILABLE"}</Badge>
          </div>
        </div>
        <p className="text-[12px] mb-3" style={{ color: "var(--text-muted)" }}>
          {info.provider === "edge" && "Microsoft Edge neural voices — free, no key, no local model."}
          {info.provider === "kokoro" && `Local Kokoro-82M server at ${info.base_url ?? "(unset)"} — fully offline narration.`}
          {info.provider === "mock" && "Simulation silence. Set TTS_PROVIDER=edge or kokoro for real narration."}
          {info.provider !== "edge" && info.provider !== "kokoro" && info.provider !== "mock" && "Custom provider."}
        </p>
        {info.error && <p className="text-[13px] text-red-500 mb-2">{info.error}</p>}
        <Field label="Voice" hint={`${(info.voices ?? []).length} voices available`}>
          <select className="input" value={voice} onChange={(e) => setVoice(e.target.value)}>
            <option value="">Provider default</option>
            {(info.voices ?? []).map((v: any) => (
              <option key={v.id} value={v.id}>
                {v.id}{v.locale ? ` (${v.locale})` : ""}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Sample text" hint="Optional — a default line is used when empty.">
          <input className="input" value={text} onChange={(e) => setText(e.target.value)}
                 placeholder="YMONEY narration test. This is how your videos will sound." />
        </Field>
        <div className="flex items-center gap-3 mt-2">
          <button className="btn-primary" onClick={runTest} disabled={testing}>
            {testing ? "Synthesizing…" : "Test narration"}
          </button>
          {audioUrl && <audio controls src={audioUrl} className="h-9" />}
        </div>
        {audioMeta && (
          <p className="text-[11px] mt-2" style={{ color: "var(--text-muted)" }}>
            generated by {audioMeta.provider}{audioMeta.mock ? " — [MOCK] simulation audio" : ""}
          </p>
        )}
        {error && <p className="text-[13px] text-red-500 mt-2">{error}</p>}
      </Card>
      <p className="text-[12px]" style={{ color: "var(--text-muted)" }}>
        For fully local narration run a Kokoro-82M server (e.g. kokoro-fastapi) and set
        <code> KOKORO_BASE_URL</code>. The video engine accepts <code>kokoro:&lt;voice&gt;</code> voice
        names for end-to-end local rendering.
      </p>
    </div>
  );
}

/* ------------------------------- IMAGES ---------------------------------- */

function ImagesSection() {
  const [info, setInfo] = useState<any>(null);
  const [prompt, setPrompt] = useState("");
  const [testing, setTesting] = useState(false);
  const [imgUrl, setImgUrl] = useState<string | null>(null);
  const [imgMeta, setImgMeta] = useState<{ provider: string; mock: boolean } | null>(null);
  const [error, setError] = useState("");
  const { push } = useToast();

  const load = useCallback(async () => {
    try {
      const r = await wsApi.get("/connections/images");
      setInfo(r);
    } catch (e: any) {
      setError(e.message);
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  async function runTest() {
    setTesting(true); setError("");
    try {
      const blob = await wsApi.postBlob("/connections/images/test", { prompt });
      if (imgUrl) URL.revokeObjectURL(imgUrl);
      setImgUrl(URL.createObjectURL(blob.blob));
      setImgMeta({
        provider: blob.headers.get("X-Image-Provider") ?? "?",
        mock: blob.headers.get("X-Image-Mock") === "1",
      });
    } catch (e: any) {
      setError(e.message);
      push("error", "Image generation failed");
    } finally {
      setTesting(false);
    }
  }

  if (error && !info) return <Card><p className="text-sm text-red-500">{error}</p></Card>;
  if (!info) return null;
  const isMock = info.provider === "mock";

  return (
    <div className="space-y-5 max-w-2xl">
      <Card>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold">
            Image provider: <span className="font-mono">{info.provider}</span>
          </h3>
          <div className="flex gap-2">
            {isMock && <Badge tone="warning">MOCK</Badge>}
            <Badge tone={info.healthy ? "success" : "error"}>{info.healthy ? "HEALTHY" : "UNAVAILABLE"}</Badge>
          </div>
        </div>
        <p className="text-[12px] mb-3" style={{ color: "var(--text-muted)" }}>
          {info.provider === "pollinations" && "Pollinations.ai — free, keyless scene-image generation. Intermittent failures retry automatically."}
          {info.provider === "openai_compat" && "Any OpenAI-compatible /images/generations endpoint (LocalAI, ComfyUI bridge, or vendor). Configure under Connections & Keys."}
          {info.provider === "mock" && "Deterministic placeholder gradients — labeled, never presented as real generation."}
          {info.provider !== "pollinations" && info.provider !== "openai_compat" && info.provider !== "mock" && "Custom provider."}
        </p>
        {info.error && <p className="text-[13px] text-red-500 mb-2">{info.error}</p>}
        <Field label="Test prompt" hint="Optional — a default prompt is used when empty.">
          <input className="input" value={prompt} onChange={(e) => setPrompt(e.target.value)}
                 placeholder="a clean minimal flat illustration of a rising chart, soft colors" />
        </Field>
        <div className="flex items-center gap-3 mt-2">
          <button className="btn-primary" onClick={runTest} disabled={testing}>
            {testing ? "Generating…" : "Generate test image"}
          </button>
          {imgUrl && <img src={imgUrl} alt="generated test" className="h-24 rounded-md border" style={{ borderColor: "var(--border)" }} />}
        </div>
        {imgMeta && (
          <p className="text-[11px] mt-2" style={{ color: "var(--text-muted)" }}>
            generated by {imgMeta.provider}{imgMeta.mock ? " — [MOCK] placeholder image" : ""}
          </p>
        )}
        {error && <p className="text-[13px] text-red-500 mt-2">{error}</p>}
      </Card>
      <p className="text-[12px]" style={{ color: "var(--text-muted)" }}>
        Scene images feed the FFmpeg avatar engine and asset library. For fully offline
        generation set <code>IMAGE_PROVIDER=openai_compat</code> and point it at a local
        image server.
      </p>
    </div>
  );
}

function Appearance() {
  return (
    <Card>
      <p className="text-[13px]" style={{ color: "var(--text-muted)" }}>
        Theme options live under General. YMONEY ships with a restrained professional
        theme; no extra appearance toggles are currently functional.
      </p>
    </Card>
  );
}

/* ----------------------------- CONNECTIONS -------------------------------- */

const CONNECTION_FIELDS: { key: string; hint?: string; placeholder?: string }[] = [
  { key: "llm.api_key", placeholder: "sk-…", hint: "OpenAI-compatible key. Stored encrypted; used by research/script/QC agents." },
  { key: "llm.base_url", placeholder: "https://api.openai.com/v1" },
  { key: "llm.model", placeholder: "gpt-4o-mini" },
  { key: "google.client_id", placeholder: "….apps.googleusercontent.com", hint: "Required for the Connect YouTube OAuth flow. Redirect URI shown below must be registered in Google Cloud console." },
  { key: "google.client_secret", placeholder: "GOCSPX-…" },
  { key: "upload_post.api_key", placeholder: "up-…", hint: "Optional relay for TikTok/Instagram/Facebook publishing (free tier: 10 uploads/mo)." },
  { key: "upload_post.username", placeholder: "your upload-post profile name" },
  { key: "newsdata.api_key", placeholder: "newsdata key", hint: "NewsData.io structured news trend source (free: 200 credits/day). Auto-added to discovery when set — get one at newsdata.io/register." },
];

function Connections() {
  const [items, setItems] = useState<any[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [test, setTest] = useState<any>(null);
  const [testing, setTesting] = useState(false);
  const { push } = useToast();

  const load = useCallback(async () => {
    const r = await wsApi.get("/connections");
    setItems(r.items ?? []);
  }, []);
  useEffect(() => { load(); }, [load]);

  async function save(key: string) {
    try {
      await wsApi.put("/connections", { key, value: drafts[key] ?? "" });
      push("success", `${key} saved`);
      setDrafts((d) => ({ ...d, [key]: "" }));
      load();
    } catch (e: any) { push("error", e.message); }
  }

  async function clear(key: string) {
    await wsApi.put("/connections", { key, value: null });
    push("info", `${key} cleared — falling back to environment`);
    load();
  }

  async function runTest() {
    setTesting(true);
    try { setTest(await wsApi.post("/connections/test-llm")); }
    catch (e: any) { push("error", e.message); }
    finally { setTesting(false); }
  }

  const wsId = localStorage.getItem("ym_ws");
  const origin = window.location.origin;
  const [pubTest, setPubTest] = useState<any>(null);
  const [pubTesting, setPubTesting] = useState(false);

  async function runPubTest() {
    setPubTesting(true);
    try { setPubTest(await wsApi.post("/connections/test-publishing", {})); }
    catch (e: any) { push("error", e.message); }
    finally { setPubTesting(false); }
  }

  return (
    <div className="space-y-5 max-w-2xl">
      <Card>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold">LLM connection test</h3>
          <button className="btn-outline !py-1 text-xs" onClick={runTest} disabled={testing}>
            {testing ? "Testing…" : "Test now"}
          </button>
        </div>
        {test ? (
          <div className="flex items-center gap-2 text-sm">
            <Badge tone={test.ok ? "success" : "error"}>{test.ok ? "OK" : "FAILED"}</Badge>
            <span>{test.detail}</span>
            {test.model_available === false && (
              <Badge tone="warning">model not in provider list</Badge>
            )}
          </div>
        ) : (
          <p className="text-[13px]" style={{ color: "var(--text-muted)" }}>Run a live check of the effective LLM configuration.</p>
        )}
      </Card>

      <Card>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold">Publishing path test</h3>
          <button className="btn-outline !py-1 text-xs" onClick={runPubTest} disabled={pubTesting}>
            {pubTesting ? "Testing…" : "Test now"}
          </button>
        </div>
        {pubTest ? (
          <div className="space-y-2 text-sm">
            <div className="flex items-center gap-2">
              <Badge tone={pubTest.ok ? "success" : "warning"}>{pubTest.ok ? "REAL PUBLISHING READY" : "SIMULATED"}</Badge>
              <span className="text-[13px]">{pubTest.detail}</span>
            </div>
            {pubTest.relay_valid === true && (
              <div className="text-[13px]">Upload-Post relay valid — plan: <b>{pubTest.relay_plan || "free"}</b>{pubTest.relay_email ? ` · ${pubTest.relay_email}` : ""}</div>
            )}
            {pubTest.relay_valid === false && (
              <div className="text-[13px] text-red-500">Relay check failed: {pubTest.detail}</div>
            )}
            {Object.keys(pubTest.connected_accounts || {}).length > 0 && (
              <div className="text-[13px]">
                Connected accounts:{" "}
                {Object.entries(pubTest.connected_accounts).map(([p, c]) => `${p} ×${c}`).join(", ")}
              </div>
            )}
          </div>
        ) : (
          <p className="text-[13px]" style={{ color: "var(--text-muted)" }}>
            Validates the Upload-Post relay key live and lists connected platform accounts. Publishing is real when either path is ready.
          </p>
        )}
      </Card>

      <Card>
        <h3 className="text-sm font-semibold mb-1">Provider credentials</h3>
        <p className="text-[12px] mb-4" style={{ color: "var(--text-muted)" }}>
          Stored AES-256-GCM encrypted server-side, never returned in plaintext and never exposed to logs.
        </p>
        <div className="space-y-4">
          {CONNECTION_FIELDS.map(({ key, hint, placeholder }) => {
            const info = items.find((i) => i.key === key);
            return (
              <Field
                key={key}
                label={`${info?.label ?? key}  ${info?.configured ? `(${info.source})` : "(not set)"}`}
                hint={hint}
              >
                <div className="flex gap-2">
                  <input
                    className="input"
                    type={info?.secret ? "password" : "text"}
                    placeholder={info?.configured ? info.masked ?? "configured" : placeholder}
                    value={drafts[key] ?? ""}
                    onChange={(e) => setDrafts((d) => ({ ...d, [key]: e.target.value }))}
                    autoComplete="off"
                  />
                  <button className="btn-primary shrink-0" onClick={() => save(key)} disabled={!drafts[key]}>
                    Save
                  </button>
                  {info?.source === "db" && (
                    <button className="btn-outline shrink-0" onClick={() => clear(key)}>Clear</button>
                  )}
                </div>
              </Field>
            );
          })}
        </div>
      </Card>

      <Card>
        <h3 className="text-sm font-semibold mb-1">Google OAuth redirect URI</h3>
        <p className="text-[12px] mb-2" style={{ color: "var(--text-muted)" }}>
          Register this exact URL in Google Cloud Console → Credentials → your OAuth client:
        </p>
        <code className="block rounded-lg px-3 py-2 text-[12px] overflow-x-auto select-all"
              style={{ background: "var(--bg-subtle)" }}>
          {origin}/api/v1/workspaces/{wsId}/publishing/oauth/youtube/callback
        </code>
      </Card>
    </div>
  );
}

/* ------------------------------ WORKSPACE -------------------------------- */

function WorkspaceSection() {
  const [ws, setWs] = useState<any>(null);
  const [saved, setSaved] = useState(false);
  const load = useCallback(async () => {
    const list = await api("GET", "/workspaces");
    setWs(list.items[0]);
  }, []);
  useEffect(() => { load(); }, [load]);
  if (!ws) return null;
  return (
    <Card className="max-w-xl space-y-4">
      <Field label="Name">
        <input className="input" value={ws.name} onChange={(e) => setWs({ ...ws, name: e.target.value })} />
      </Field>
      <Field label="Default niche" hint="Discovery + audience-fit scoring key on this.">
        <input className="input" value={ws.niche ?? ""} onChange={(e) => setWs({ ...ws, niche: e.target.value })} />
      </Field>
      <Field label="Language / timezone / currency">
        <div className="grid grid-cols-3 gap-2">
          <input className="input" value={ws.language ?? ""} onChange={(e) => setWs({ ...ws, language: e.target.value })} />
          <input className="input" value={ws.timezone ?? ""} onChange={(e) => setWs({ ...ws, timezone: e.target.value })} />
          <input className="input" value={ws.currency ?? ""} onChange={(e) => setWs({ ...ws, currency: e.target.value })} />
        </div>
      </Field>
      <button className="btn-primary" onClick={async () => {
        await api("PATCH", `/workspaces/${ws.id}`, { name: ws.name, niche: ws.niche, brand_voice: ws.brand_voice });
        await wsApi.put("/settings", { settings: { language: ws.language, timezone: ws.timezone, currency: ws.currency } });
        setSaved(true); setTimeout(() => setSaved(false), 1500);
      }}>{saved ? "Saved ✓" : "Save workspace"}</button>
    </Card>
  );
}

/* ---------------------------------- AI ----------------------------------- */

function AiSection() {
  const [health, setHealth] = useState<any>(null);
  const [engine, setEngine] = useState<any>(null);
  useEffect(() => {
    fetch("/api/v1/system/health").then((r) => r.json()).then(setHealth).catch(() => {});
    wsApi.get("/connections/video-engine").then(setEngine).catch(() => {});
  }, []);
  if (!health) return null;
  return (
    <div className="space-y-4 max-w-xl">
      <Card>
        <h3 className="text-sm font-semibold mb-3">Video engine</h3>
        {engine ? (
          <>
            <dl className="text-sm space-y-1.5">
              <div className="flex justify-between">
                <dt style={{ color: "var(--text-muted)" }}>Active engine</dt>
                <dd className="font-mono">{engine.engine}</dd>
              </div>
              <div className="flex justify-between">
                <dt style={{ color: "var(--text-muted)" }}>Status</dt>
                <dd><Badge tone={engine.healthy ? "success" : "error"}>{engine.healthy ? "CONNECTED" : "UNAVAILABLE"}</Badge></dd>
              </div>
              <div className="flex justify-between">
                <dt style={{ color: "var(--text-muted)" }}>Base URL</dt>
                <dd className="font-mono text-[12px]">{engine.base_url}</dd>
              </div>
              {engine.version && (
                <div className="flex justify-between">
                  <dt style={{ color: "var(--text-muted)" }}>Version</dt>
                  <dd className="font-mono">{engine.version}</dd>
                </div>
              )}
              <div className="flex justify-between">
                <dt style={{ color: "var(--text-muted)" }}>Render timeout</dt>
                <dd>{Math.round(engine.timeout_seconds / 60)} min</dd>
              </div>
            </dl>
            {engine.capabilities?.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {engine.capabilities.map((c: string) => <Badge key={c} tone="neutral">{c.toLowerCase().replace(/_/g, " ")}</Badge>)}
              </div>
            )}
          </>
        ) : <p className="text-[13px]" style={{ color: "var(--text-muted)" }}>Loading engine status…</p>}
        <p className="text-[12px] mt-3" style={{ color: "var(--text-muted)" }}>
          Configure via environment variables (<code>MPT_BASE_URL</code> or{" "}
          <code>MONEYPRINTERTURBO_BASE_URL</code>, <code>VIDEO_ENGINE=mock|moneyprinterturbo</code>,
          <code> MPT_TIMEOUT_SECONDS</code>). The engine runs as an independent service — YMONEY
          talks to it over HTTP only.
        </p>
      </Card>

      <Card>
        <h3 className="text-sm font-semibold mb-2">LLM provider status</h3>
        <dl className="text-sm space-y-1.5">
          <div className="flex justify-between">
            <dt style={{ color: "var(--text-muted)" }}>Mode</dt>
            <dd><Badge tone={health.mocks.llm ? "warning" : "success"}>{health.mocks.llm ? "MOCK (offline)" : "REAL"}</Badge></dd>
          </div>
          <div className="flex justify-between">
            <dt style={{ color: "var(--text-muted)" }}>Endpoint reachable</dt>
            <dd>{health.mocks.llm ? "n/a in mock mode" : health.llm_provider ? "yes" : "NO — check OPENAI_BASE_URL/key"}</dd>
          </div>
        </dl>
        <p className="text-[12px] mt-4 leading-relaxed" style={{ color: "var(--text-muted)" }}>
          Keys are managed under <strong>Connections &amp; Keys</strong> (encrypted at rest) or
          via environment variables and restart — never accepted from the browser for storage in plaintext.
        </p>
      </Card>
    </div>
  );
}

/* -------------------------------- AGENTS --------------------------------- */

function AgentsSection() {
  const [configs, setConfigs] = useState<any[]>([]);
  useEffect(() => { wsApi.get("/agents/config").then((r) => setConfigs(r.items ?? [])).catch(() => {}); }, []);
  async function toggle(key: string, enabled: boolean) {
    await wsApi.put(`/agents/config/${key}`, { enabled });
    setConfigs((cs) => cs.map((c) => (c.key === key ? { ...c, enabled } : c)));
  }
  return (
    <Card pad={false} className="max-w-xl">
      <table className="table">
        <thead><tr><th>Agent</th><th>Model override</th><th>Enabled</th></tr></thead>
        <tbody>
          {configs.map((c) => (
            <tr key={c.key}>
              <td>{c.title}</td>
              <td style={{ color: "var(--text-muted)" }}>{c.model || "default"}</td>
              <td><Toggle checked={c.enabled} onChange={(v) => toggle(c.key, v)} label={`Toggle ${c.title}`} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

/* -------------------------------- TRENDS --------------------------------- */

function TrendsSection() {
  const [sources, setSources] = useState<any[]>([]);
  const [kinds, setKinds] = useState<{ kind: string; label: string }[]>([]);
  const { push } = useToast();
  const load = useCallback(async () => {
    const r = await wsApi.get("/trend-sources");
    setSources(r.items ?? []);
  }, []);
  useEffect(() => { load(); }, [load]);

  async function add(kind: string) {
    try {
      await wsApi.post("/trend-sources", { kind, priority: 50 });
      push("success", `${kind} source added`);
      load();
    } catch (e: any) { push("error", e.message); }
  }
  async function remove(id: string) {
    await wsApi.del(`/trend-sources/${id}`);
    load();
  }
  async function toggle(t: any) {
    await wsApi.post("/trend-sources", { kind: t.kind, name: t.name, priority: t.priority, config: t.config });
    await wsApi.del(`/trend-sources/${t.id}`);
    load();
  }

  // available kinds come from the backend registry via a create attempt; known set:
  const KNOWN = [
    ["google_trends", "Google Trends RSS (no key)"],
    ["reddit", "Reddit public JSON"],
    ["mock", "Mock trends (dev only)"],
  ];

  return (
    <div className="space-y-4 max-w-xl">
      <Card pad={false}>
        <table className="table">
          <thead><tr><th>Source</th><th>Kind</th><th>State</th><th></th></tr></thead>
          <tbody>
            {sources.map((s) => (
              <tr key={s.id}>
                <td>{s.name}</td>
                <td className="font-mono text-[12px]">{s.kind}</td>
                <td><Badge tone={s.enabled ? "success" : "neutral"}>{s.enabled ? "enabled" : "disabled"}</Badge></td>
                <td className="text-right whitespace-nowrap">
                  <button className="btn-ghost !py-1 !px-2 text-xs" onClick={() => toggle(s)}>Flip</button>
                  <button className="btn-outline !py-1 !px-2 text-xs ml-1.5" onClick={() => remove(s.id)}>Remove</button>
                </td>
              </tr>
            ))}
            {sources.length === 0 && (
              <tr><td colSpan={4} className="text-center py-6" style={{ color: "var(--text-muted)" }}>
                Using system default discovery (Google Trends when enabled).
              </td></tr>
            )}
          </tbody>
        </table>
      </Card>
      <div className="flex gap-2 flex-wrap">
        {KNOWN.map(([k, label]) => (
          <button key={k} className="btn-outline text-xs" onClick={() => add(k)} disabled={sources.some((s) => s.kind === k)}>
            + {label}
          </button>
        ))}
      </div>
      <p className="text-[12px]" style={{ color: "var(--text-muted)" }}>
        Scoring weights are tunable per workspace via the API ({`PUT /workspaces/{id}/settings`} →
        <code> scoring_weights</code>) — component keys mirror the WHY panel.
      </p>
    </div>
  );
}

/* ----------------------------- CONTENT RULES ------------------------------ */

function ContentRules() {
  const [settings, setSettings] = useState<any>(null);
  const load = useCallback(async () => {
    const r = await wsApi.get("/settings");
    setSettings(r.settings ?? {});
  }, []);
  useEffect(() => { load(); }, [load]);
  if (!settings) return null;
  const content = settings.content_rules ?? {};
  async function save(patch: any) {
    const merged = { ...content, ...patch };
    setSettings({ ...settings, content_rules: merged });
    await wsApi.put("/settings", { settings: { content_rules: merged } });
  }
  return (
    <Card className="max-w-xl space-y-4">
      <Field label="Default duration seconds (20–90)">
        <input className="input !w-40" type="number" min={20} max={90} defaultValue={content.duration_seconds ?? 32}
          onBlur={(e) => save({ duration_seconds: Number(e.target.value) })} />
      </Field>
      <Field label="Blocked topics (comma-separated)" hint="Adds risk weight; risky candidates escalate to HUMAN_REVIEW.">
        <input className="input" defaultValue={content.blocked_topics ?? ""}
          onBlur={(e) => save({ blocked_topics: e.target.value.split(",").map((s: string) => s.trim()).filter(Boolean) })} />
      </Field>
      <Field label="Minimum QC score" hint="Also editable under Safety & budget — same underlying setting.">
        <input className="input !w-24" type="number" defaultValue={settings.safety?.min_qc_score ?? 75}
          onBlur={(e) => wsApi.put("/safety", { min_qc_score: Number(e.target.value) })} />
      </Field>
    </Card>
  );
}

/* ------------------------------ AUTOMATION -------------------------------- */

function Automation() {
  const [status, setStatus] = useState<any>(null);
  useEffect(() => { wsApi.get("/autopilot/status").then(setStatus).catch(() => {}); }, []);
  return (
    <div className="space-y-4 max-w-xl">
      <Card>
        <h3 className="text-sm font-semibold mb-3">Autopilot</h3>
        <dl className="text-sm space-y-1.5">
          <div className="flex justify-between"><dt style={{ color: "var(--text-muted)" }}>State</dt><dd>{status?.state ?? "—"}</dd></div>
          <div className="flex justify-between"><dt style={{ color: "var(--text-muted)" }}>Cycles completed</dt><dd>{status?.cycles_completed ?? 0}</dd></div>
          <div className="flex justify-between"><dt style={{ color: "var(--text-muted)" }}>Queued jobs</dt><dd>{status?.queued_jobs ?? 0}</dd></div>
        </dl>
      </Card>
      <Card>
        <h3 className="text-sm font-semibold mb-2">Stop conditions (always enforced)</h3>
        <ul className="list-disc pl-5 text-[13px] space-y-1" style={{ color: "var(--text-muted)" }}>
          <li>Budget exhausted (daily / monthly / per-video)</li>
          <li>Repeated cycle failures → circuit breaker after 3</li>
          <li>Safety triggers (upload rate, repeated failure) auto-pause with explanation</li>
          <li>User STOP — always wins over automation</li>
        </ul>
      </Card>
    </div>
  );
}

/* -------------------------------- SAFETY ---------------------------------- */

function Safety() {
  const [safety, setSafety] = useState<any>(null);
  const load = useCallback(async () => {
    const r = await wsApi.get("/safety");
    setSafety(r.safety);
  }, []);
  useEffect(() => { load(); }, [load]);
  const { push } = useToast();
  if (!safety) return null;

  const FIELDS: [string, string][] = [
    ["daily_budget_usd", "Daily budget ($)"],
    ["monthly_budget_usd", "Monthly budget ($)"],
    ["per_video_budget_usd", "Per-video budget ($)"],
    ["max_videos_per_day", "Max videos / day"],
    ["max_uploads_per_hour", "Max uploads / hour"],
    ["min_qc_score", "Min QC score"],
    ["max_render_attempts", "Max render attempts"],
    ["max_consecutive_failures", "Max consecutive failures"],
    ["similarity_threshold", "Similarity skip threshold (0-1)"],
    ["require_human_review_risk_above", "Human-review risk above"],
  ];

  return (
    <Card className="max-w-xl">
      <h3 className="text-sm font-semibold mb-1">Limits & triggers</h3>
      <p className="text-[12px] mb-4" style={{ color: "var(--text-muted)" }}>
        Exceeding a limit pauses autopilot automatically with an explanation on the activity feed.
      </p>
      <div className="grid sm:grid-cols-2 gap-x-4 gap-y-3">
        {FIELDS.map(([k, label]) => (
          <Field key={k} label={label}>
            <input
              className="input"
              type="number"
              step={k === "similarity_threshold" ? "0.05" : "1"}
              defaultValue={safety[k]}
              aria-label={label}
              onBlur={async (e) => {
                if (e.target.value === "" || String(safety[k]) === e.target.value) return;
                try {
                  await wsApi.put("/safety", { [k]: Number(e.target.value) });
                  push("success", `${label} updated`);
                } catch (err: any) { push("error", err.message); }
              }}
            />
          </Field>
        ))}
      </div>
    </Card>
  );
}

/* ------------------------------ PUBLISHING -------------------------------- */

function PublishingSection() {
  const [accounts, setAccounts] = useState<any[]>([]);
  useEffect(() => { wsApi.get("/publishing/accounts").then((r) => setAccounts(r.items ?? [])).catch(() => {}); }, []);
  return (
    <Card className="max-w-xl">
      <h3 className="text-sm font-semibold mb-3">Connected accounts</h3>
      {accounts.length > 0 ? (
        <ul className="space-y-2 text-sm">
          {accounts.map((a) => (
            <li key={a.id} className="flex justify-between">
              <span className="capitalize">{a.platform} — {a.display_name}</span>
              <Badge tone={a.status === "connected" ? "success" : "warning"}>{a.status}</Badge>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-[13px]" style={{ color: "var(--text-muted)" }}>
          No accounts connected — publishing is clearly-labeled mock. Manage connections on the Publishing page.
        </p>
      )}
    </Card>
  );
}
