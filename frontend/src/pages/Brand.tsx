import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, PageHeader, Section } from "../components/ui";

export default function Brand() {
  const ws = useFetch(() => wsApi.get("/settings"), []);
  const data: any = (ws.data as any)?.settings ?? ws.data;

  return (
    <div className="space-y-4 max-w-[720px]">
      <PageHeader title="Brand" subtitle="Voice, guardrails and disclosure posture — what every render must honor." />
      <Card>
        <div className="panel-label mb-1.5">Brand voice</div>
        <p className="text-[13.5px] whitespace-pre-wrap">{data?.brand_voice || "Not set — add one in Settings → Workspace. The Strategist falls back to a confident, direct default."}</p>
      </Card>
      <Card>
        <div className="panel-label mb-1.5">Disclosure posture (automatic)</div>
        <div className="space-y-1.5 text-[13px]">
          <div className="flex gap-2 items-center"><Badge tone="info">AI-generated</Badge><span style={{ color: "var(--text-muted)" }}>Every publish carries an AI-content disclosure in description/caption.</span></div>
          <div className="flex gap-2 items-center"><Badge tone="warning">Finance</Badge><span style={{ color: "var(--text-muted)" }}>Money topics get “Not financial advice” injected plus a burned-in footer on renders.</span></div>
          <div className="flex gap-2 items-center"><Badge tone="muted">BGM</Badge><span style={{ color: "var(--text-muted)" }}>Only license-allowlisted tracks; unknown beds render silent.</span></div>
        </div>
      </Card>
      <Card>
        <div className="panel-label mb-1.5">Workspace</div>
        <Section data={data ? [data] : []} loading={ws.loading} error={ws.error} onRetry={ws.reload} empty="No workspace data" >
          {() => (
            <div className="text-[13px] font-mono space-y-1" style={{ color: "var(--text-muted)" }}>
              <div>niche: {data?.niche ?? "—"}</div>
              <div>language: {data?.language ?? "—"} · timezone: {data?.timezone ?? "—"}</div>
            </div>
          )}
        </Section>
      </Card>
    </div>
  );
}
