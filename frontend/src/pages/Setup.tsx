import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, setWorkspace } from "../lib/api";
import { Card, Field, PageHeader } from "../components/ui";
import { useFetch } from "../hooks/hooks";

/* First-run wizard: workspace → niche → providers → readiness → START. */

export default function Setup() {
  const nav = useNavigate();
  const [step, setStep] = useState(0);
  const [name, setName] = useState("My Studio");
  const [niche, setNiche] = useState("personal finance");
  const [wsId, setWsId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const ready = useFetch(() => api("GET", "/system/readiness"), []);

  async function create() {
    setError("");
    try {
      const ws = await api<any>("POST", "/workspaces", { name, niche });
      const id = ws.id ?? ws.workspace?.id;
      setWorkspace(id);
      setWsId(id);
      setStep(1);
    } catch (e: any) {
      setError(e.message);
    }
  }

  const checks: any[] = (ready.data as any)?.checks ?? [];
  const blocked: string[] = (ready.data as any)?.blocking_failures ?? [];

  return (
    <div className="max-w-[640px] mx-auto">
      <PageHeader title="Studio setup" subtitle={`Step ${step + 1} of 3 — two minutes to your first autonomous cycle.`} />
      {step === 0 && (
        <Card>
          <Field label="Studio name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} /></Field>
          <Field label="Niche — what every video is about" hint="Drives trend discovery, scoring fit and SEO. Be specific: 'budgeting for students' beats 'finance'.">
            <input className="input" value={niche} onChange={(e) => setNiche(e.target.value)} />
          </Field>
          {error && <div className="text-[13px] mb-3" style={{ color: "var(--danger)" }}>{error}</div>}
          <button className="btn-primary" onClick={create}>Create studio →</button>
        </Card>
      )}
      {step === 1 && (
        <Card>
          <h3 className="font-semibold mb-1">Connect providers (optional for now)</h3>
          <p className="text-[13px] mb-4" style={{ color: "var(--text-muted)" }}>
            Everything runs offline until you add keys. Add an LLM key for real scripts, a Pexels key for stock footage, and the Upload-Post relay (or OAuth accounts) for real publishing. You can do all of this later in Settings → Connections.
          </p>
          <div className="flex gap-2">
            <button className="btn-outline" onClick={() => nav("/settings")}>Open Settings</button>
            <button className="btn-primary" onClick={() => setStep(2)}>Check readiness →</button>
          </div>
        </Card>
      )}
      {step === 2 && (
        <Card>
          <h3 className="font-semibold mb-3">Production readiness</h3>
          {ready.loading && <div className="text-[13px]" style={{ color: "var(--text-muted)" }}>Probing providers…</div>}
          {checks.map((c: any) => (
            <div key={c.id} className="flex justify-between py-2 text-[13px]" style={{ borderBottom: "var(--seam)" }}>
              <span className="capitalize">{c.id.replace(/_/g, " ")}</span>
              <span style={{ color: c.status === "passed" ? "var(--accent)" : c.blocking ? "var(--danger)" : "var(--warn)" }}>
                {c.status === "passed" ? "● ready" : `● ${c.detail}`} 
              </span>
            </div>
          ))}
          <div className="flex gap-2 mt-4">
            <button className="btn-primary" onClick={() => nav("/")}>
              {blocked.length ? "Open Command Center anyway" : "Open Command Center →"}
            </button>
          </div>
          {wsId && <div className="hidden">{wsId}</div>}
        </Card>
      )}
    </div>
  );
}
