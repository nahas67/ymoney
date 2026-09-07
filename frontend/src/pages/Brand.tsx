import { useCallback, useEffect, useState } from "react";
import { api, getWorkspace, wsApi } from "../lib/api";
import { Badge, Card, Field, PageHeader, useToast } from "../components/ui";

/**
 * Brand Center — backed by workspace fields + structured `settings.brand`.
 * Agents consume niche/brand_voice today; content rules feed the risk/safety layer.
 */
export default function Brand() {
  const [ws, setWs] = useState<any>(null);
  const [brand, setBrand] = useState<any>({});
  const { push } = useToast();

  const load = useCallback(async () => {
    const list = await api("GET", "/workspaces");
    const me = list.items[0];
    setWs(me);
    setBrand((me.settings_json?.brand ?? {}));
  }, []);

  useEffect(() => { load(); }, [load]);

  async function save() {
    try {
      await api("PATCH", `/workspaces/${getWorkspace()}`, {
        name: ws.name, niche: ws.niche, brand_voice: ws.brand_voice,
      });
      await wsApi.put("/settings", { settings: { brand } });
      push("success", "Brand saved — agents use this on the next cycle");
    } catch (e: any) { push("error", e.message); }
  }

  function setField(k: string, v: any) { setBrand((b: any) => ({ ...b, [k]: v })); }

  if (!ws) return null;

  return (
    <div className="space-y-5 max-w-3xl">
      <PageHeader
        title="Brand"
        subtitle="Identity and voice the agents write with; content rules guard what gets produced."
      />

      <Card>
        <h3 className="text-sm font-semibold mb-4">Identity</h3>
        <div className="space-y-4">
          <Field label="Workspace / brand name">
            <input className="input" value={ws.name} onChange={(e) => setWs({ ...ws, name: e.target.value })} />
          </Field>
          <div className="grid sm:grid-cols-2 gap-4">
            <Field label="Niche" hint="Drives discovery & audience-fit scoring">
              <input className="input" value={ws.niche} onChange={(e) => setWs({ ...ws, niche: e.target.value })} placeholder="personal finance…" />
            </Field>
            <Field label="Primary language">
              <input className="input" value={ws.language ?? "en"} onChange={(e) => setWs({ ...ws, language: e.target.value })} />
            </Field>
          </div>
          <Field label="Voice & tone" hint="Used verbatim by the Script Agent when composing.">
            <textarea className="textarea" rows={3} value={ws.brand_voice}
              onChange={(e) => setWs({ ...ws, brand_voice: e.target.value })}
              placeholder="Confident, direct, friendly. Short sentences. No hype." />
          </Field>
        </div>
      </Card>

      <Card>
        <h3 className="text-sm font-semibold mb-1">Audience & style</h3>
        <p className="text-[12px] mb-4" style={{ color: "var(--text-muted)" }}>Stored as structured brand data and referenced by strategy prompts.</p>
        <div className="grid sm:grid-cols-2 gap-4">
          <Field label="Target audience">
            <input className="input" value={brand.audience ?? ""} onChange={(e) => setField("audience", e.target.value)} placeholder="young professionals exploring side income" />
          </Field>
          <Field label="Personality">
            <input className="input" value={brand.personality ?? ""} onChange={(e) => setField("personality", e.target.value)} placeholder="pragmatic optimist" />
          </Field>
          <Field label="CTA style">
            <input className="input" value={brand.cta_style ?? ""} onChange={(e) => setField("cta_style", e.target.value)} placeholder="follow for weekly breakdowns" />
          </Field>
          <Field label="Visual style">
            <input className="input" value={brand.visual_style ?? ""} onChange={(e) => setField("visual_style", e.target.value)} placeholder="clean b-roll, bold captions" />
          </Field>
        </div>
      </Card>

      <Card>
        <h3 className="text-sm font-semibold mb-1">Content rules</h3>
        <p className="text-[12px] mb-4" style={{ color: "var(--text-muted)" }}>
          Blocked topics add to the risk score; risky candidates escalate to human review automatically.
        </p>
        <div className="space-y-4">
          <Field label="Allowed topics (comma-separated)">
            <input className="input" value={brand.allowed_topics ?? ""} onChange={(e) => setField("allowed_topics", e.target.value)} placeholder="saving, budgeting, ai tools" />
          </Field>
          <Field label="Blocked topics">
            <input className="input" value={brand.blocked_topics ?? ""} onChange={(e) => setField("blocked_topics", e.target.value)} placeholder="politics, medical advice" />
          </Field>
          <Field label="Words to avoid">
            <input className="input" value={brand.words_to_avoid ?? ""} onChange={(e) => setField("words_to_avoid", e.target.value)} placeholder="guaranteed, get rich quick" />
          </Field>
          <Field label="Disclosure requirements">
            <input className="input" value={brand.disclosure ?? ""} onChange={(e) => setField("disclosure", e.target.value)} placeholder="#ad where applicable" />
          </Field>
        </div>
      </Card>

      <div className="flex items-center gap-3">
        <button className="btn-primary" onClick={save}>Save brand</button>
        <Badge tone="info">applies from the next production cycle</Badge>
      </div>
    </div>
  );
}
