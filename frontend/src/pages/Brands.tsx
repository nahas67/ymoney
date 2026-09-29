/* Brands — Work 08 Lane D: Brand DNA, assets, presets, effective policy,
 * consistency verifier (backend/app/api/v1/brands.py).
 *
 * NOTE: this is the CREATIVE identity surface. The legacy white-label kit
 * (studio name / accent / logo chrome) stays on /brand (pages/Brand.tsx) and
 * is linked from this page's header — both remain reachable.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import {
  Badge, Card, ConfirmButton, ErrorBox, Field, Loading, PageHeader, Tabs, toast,
} from "../components/ui";

type TabKey = "dna" | "assets" | "presets" | "effective" | "verify";

const TABS: { key: TabKey; label: string }[] = [
  { key: "dna", label: "DNA editor" },
  { key: "assets", label: "Asset library" },
  { key: "presets", label: "Presets" },
  { key: "effective", label: "Effective policy" },
  { key: "verify", label: "Verify" },
];

const ASSET_ROLES = ["logo", "watermark", "intro", "outro", "lower_third", "thumbnail_frame"];
const PLATFORMS = ["youtube", "tiktok", "instagram", "facebook"];
const HEX_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;

const DNA_DEFAULTS: Record<string, any> = {
  logos: [], colors: {}, fonts: {}, typography: {}, caption_style: {}, cta_style: {},
  watermark: {}, intro: "", outro: "", motion_language: {}, music_prefs: {},
  voice_identity: {}, avatar_identity: {}, visual_style: {}, thumbnail_style: {},
  writing_tone: "", vocabulary: { preferred: [], avoid: [] }, pronunciation_rules: [],
  forbidden_phrases: [], required_disclaimers: [], claims_policy: {}, platform_overrides: {},
};

const PROV_TONE: Record<string, string> = {
  workspace: "muted", brand: "info", campaign: "success", content: "warning", platform: "error",
};

const REPORT_TONE: Record<string, string> = {
  PASS: "success", PASS_WITH_WARNINGS: "warning", REVIEW_REQUIRED: "info", FAIL: "error",
};

/* ------------------------------------------------------------------ */
/* small editors (draft-on-typing, commit-on-blur)                      */
/* ------------------------------------------------------------------ */

function toRows(obj: Record<string, any>): { k: string; v: string }[] {
  return Object.entries(obj ?? {}).map(([k, v]) => [
    k,
    typeof v === "object" && v !== null ? JSON.stringify(v) : String(v ?? ""),
  ] as [string, string]).map(([k, v]) => ({ k, v }));
}

function parseValue(raw: string): any {
  const t = raw.trim();
  if (t.startsWith("{") || t.startsWith("[")) {
    try {
      return JSON.parse(t);
    } catch {
      return raw; // leave as string; save-time validation reports it
    }
  }
  if (t === "true") return true;
  if (t === "false") return false;
  return raw;
}

function KVEditor({ value, onChange, keyPlaceholder = "key", valPlaceholder = "value" }: {
  value: Record<string, any>;
  onChange: (v: Record<string, any>) => void;
  keyPlaceholder?: string;
  valPlaceholder?: string;
}) {
  const [rows, setRows] = useState(() => toRows(value));
  const [newK, setNewK] = useState("");
  const [newV, setNewV] = useState("");
  const src = JSON.stringify(value ?? {});
  useEffect(() => setRows(toRows(value)), [src]); // eslint-disable-line react-hooks/exhaustive-deps

  function commit(next: { k: string; v: string }[]) {
    setRows(next);
    const out: Record<string, any> = {};
    for (const r of next) {
      const key = r.k.trim();
      if (!key) continue;
      out[key] = parseValue(r.v);
    }
    onChange(out);
  }

  return (
    <div className="space-y-1.5">
      {rows.map((r, i) => (
        <div key={i} className="flex gap-1.5">
          <input className="input flex-1 font-mono !text-[12px]" value={r.k} placeholder={keyPlaceholder}
            onChange={(e) => commit(rows.map((x, j) => (j === i ? { ...x, k: e.target.value } : x)))} />
          <input className="input flex-1 font-mono !text-[12px]" value={r.v} placeholder={valPlaceholder}
            onChange={(e) => commit(rows.map((x, j) => (j === i ? { ...x, v: e.target.value } : x)))} />
          <button className="btn-ghost !px-2 !text-xs" aria-label="Remove row"
            onClick={() => commit(rows.filter((_, j) => j !== i))}>✕</button>
        </div>
      ))}
      {!rows.length && <div className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>No entries yet.</div>}
      <div className="flex gap-1.5">
        <input className="input flex-1 font-mono !text-[12px]" placeholder={keyPlaceholder} value={newK}
          onChange={(e) => setNewK(e.target.value)} />
        <input className="input flex-1 font-mono !text-[12px]" placeholder={valPlaceholder} value={newV}
          onChange={(e) => setNewV(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && newK.trim()) { commit([...rows, { k: newK, v: newV }]); setNewK(""); setNewV(""); } }} />
        <button className="btn-outline !text-xs" disabled={!newK.trim()}
          onClick={() => { commit([...rows, { k: newK, v: newV }]); setNewK(""); setNewV(""); }}>+ Add</button>
      </div>
      <div className="text-[10.5px]" style={{ color: "var(--text-faint)" }}>
        Values starting with {'{'} or {'['} are stored as JSON.
      </div>
    </div>
  );
}

function TagList({ items, onChange, placeholder }: {
  items: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState("");
  function add() {
    const t = draft.trim();
    if (!t) return;
    if (items.includes(t)) { setDraft(""); return; }
    onChange([...items, t]);
    setDraft("");
  }
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap gap-1.5">
        {items.map((it, i) => (
          <span key={`${it}-${i}`} className="chip !text-[11.5px] !py-1">
            {it}
            <button className="ml-1.5 opacity-70 hover:opacity-100" aria-label={`Remove ${it}`}
              onClick={() => onChange(items.filter((_, j) => j !== i))}>✕</button>
          </span>
        ))}
        {!items.length && <span className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>None.</span>}
      </div>
      <div className="flex gap-1.5">
        <input className="input flex-1 !text-[12px]" placeholder={placeholder ?? "Add + Enter"} value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), add())} />
        <button className="btn-outline !text-xs" disabled={!draft.trim()} onClick={add}>+ Add</button>
      </div>
    </div>
  );
}

function ColorRows({ value, onChange }: { value: Record<string, string>; onChange: (v: Record<string, string>) => void }) {
  const [rows, setRows] = useState(() => Object.entries(value ?? {}));
  const src = JSON.stringify(value ?? {});
  useEffect(() => setRows(Object.entries(value ?? {})), [src]); // eslint-disable-line react-hooks/exhaustive-deps

  function commit(next: [string, string][]) {
    setRows(next);
    const out: Record<string, string> = {};
    for (const [role, hex] of next) {
      const r = role.trim();
      if (!r) continue;
      out[r] = hex.trim();
    }
    onChange(out);
  }

  return (
    <div className="space-y-1.5">
      {rows.map(([role, hex], i) => (
        <div key={i} className="flex items-center gap-1.5">
          <span className="w-7 h-7 rounded-md shrink-0" title={hex}
            style={{ background: HEX_RE.test(hex) ? hex : "transparent", border: "var(--seam)" }} />
          <input className="input flex-1 !text-[12px] font-mono" value={role} placeholder="role (e.g. primary)"
            onChange={(e) => commit(rows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} />
          <input type="color" className="w-8 h-8 cursor-pointer rounded" aria-label={`Color for ${role}`}
            value={HEX_RE.test(hex) ? hex : "#000000"}
            onChange={(e) => commit(rows.map((r, j) => (j === i ? [r[0], e.target.value] : r)))} />
          <input className="input w-[104px] !text-[12px] font-mono" value={hex} placeholder="#aabbcc"
            onChange={(e) => commit(rows.map((r, j) => (j === i ? [r[0], e.target.value] : r)))} />
          <button className="btn-ghost !px-2 !text-xs" aria-label="Remove color"
            onClick={() => commit(rows.filter((_, j) => j !== i))}>✕</button>
        </div>
      ))}
      {!rows.length && <div className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>No colors configured.</div>}
      <button className="btn-outline !text-xs" onClick={() => commit([...rows, ["primary", "#22c55e"]])}>+ Add color</button>
    </div>
  );
}

function PronunciationRows({ value, onChange }: { value: any[]; onChange: (v: any[]) => void }) {
  const rows = (value ?? []).map((r: any) =>
    typeof r === "string" ? { term: r, pronunciation: "" } : { term: r?.term ?? "", pronunciation: r?.pronunciation ?? "" });
  function commit(next: { term: string; pronunciation: string }[]) {
    onChange(next.filter((r) => r.term.trim()).map((r) => ({ term: r.term.trim(), pronunciation: r.pronunciation.trim() })));
  }
  return (
    <div className="space-y-1.5">
      {rows.map((r, i) => (
        <div key={i} className="flex gap-1.5">
          <input className="input flex-1 !text-[12px]" placeholder="term / brand name" value={r.term}
            onChange={(e) => commit(rows.map((x, j) => (j === i ? { ...x, term: e.target.value } : x)))} />
          <input className="input flex-1 !text-[12px]" placeholder="pronounced like…" value={r.pronunciation}
            onChange={(e) => commit(rows.map((x, j) => (j === i ? { ...x, pronunciation: e.target.value } : x)))} />
          <button className="btn-ghost !px-2 !text-xs" aria-label="Remove rule"
            onClick={() => commit(rows.filter((_, j) => j !== i))}>✕</button>
        </div>
      ))}
      {!rows.length && <div className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>No pronunciation rules.</div>}
      <button className="btn-outline !text-xs" onClick={() => commit([...rows, { term: "", pronunciation: "" }])}>
        + Add rule
      </button>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* page                                                                */
/* ------------------------------------------------------------------ */

export default function Brands() {
  const brandsRes = useFetch(() => wsApi.get("/brands"), []);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tab, setTab] = useState<TabKey>("dna");
  const [dna, setDna] = useState<Record<string, any> | null>(null);
  const [brandName, setBrandName] = useState("");
  const [busy, setBusy] = useState("");
  const [newBrand, setNewBrand] = useState("");

  // effective policy + presets + media assets (lazily keyed by tab)
  const [platform, setPlatform] = useState("");
  const effective = useFetch(
    () => (tab === "effective" && selectedId
      ? wsApi.get(`/brands/effective?brand_id=${encodeURIComponent(selectedId)}${platform ? `&platform=${encodeURIComponent(platform)}` : ""}`)
      : Promise.resolve(null)),
    [tab, selectedId, platform]
  );
  const presets = useFetch(
    () => (tab === "presets" ? wsApi.get("/brands/presets") : Promise.resolve(null)),
    [tab]
  );
  const media = useFetch(() => wsApi.get("/assets/media?limit=200"), []);

  // verify panel
  const [sampleText, setSampleText] = useState(
    "Welcome back! This is not financial advice — do your own research before investing."
  );
  const [artifactKind, setArtifactKind] = useState("clip");
  const [verifyPlatform, setVerifyPlatform] = useState("");
  const [report, setReport] = useState<any>(null);
  const [linkAssetId, setLinkAssetId] = useState("");
  const [linkRole, setLinkRole] = useState("logo");
  const [linkLabel, setLinkLabel] = useState("");
  const [overridePlatform, setOverridePlatform] = useState("");

  const brands: any[] = brandsRes.data?.brands ?? [];
  const selected = brands.find((b) => b.id === selectedId) ?? null;

  useEffect(() => {
    if (!brands.length) return;
    if (!selectedId || !brands.some((b) => b.id === selectedId)) setSelectedId(brands[0].id);
  }, [brands, selectedId]);

  useEffect(() => {
    if (!selected) return;
    setDna({ ...DNA_DEFAULTS, ...(selected.dna ?? {}) });
    setBrandName(selected.name ?? "");
    setReport(null);
  }, [selected?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const assets: any[] = selected?.assets ?? [];

  function validate(): string {
    if (!dna) return "no brand selected";
    for (const [role, hex] of Object.entries(dna.colors ?? {})) {
      if (!HEX_RE.test(String(hex))) return `colors.${role}: “${hex}” is not a hex color (#abc / #aabbcc)`;
    }
    for (const [key, frag] of Object.entries(dna.platform_overrides ?? {})) {
      const colors = (frag as any)?.colors ?? {};
      for (const [role, hex] of Object.entries(colors)) {
        if (!HEX_RE.test(String(hex))) return `platform_overrides.${key}.colors.${role}: “${hex}” is not a hex color`;
      }
      for (const listKey of ["forbidden_phrases", "required_disclaimers"]) {
        const list = (frag as any)?.[listKey];
        if (list != null && (!Array.isArray(list) || list.some((x: any) => !String(x ?? "").trim()))) {
          return `platform_overrides.${key}.${listKey} must be a list of non-empty strings`;
        }
      }
    }
    for (const listKey of ["forbidden_phrases", "required_disclaimers", "logos"]) {
      const list = dna[listKey];
      if (list != null && (!Array.isArray(list) || list.some((x: any) => !String(x ?? "").trim()))) {
        return `${listKey} must be a list of non-empty strings`;
      }
    }
    return "";
  }

  async function save() {
    if (!selected) return;
    const problem = validate();
    if (problem) {
      toast(problem, "error", "Invalid BrandDNA");
      return;
    }
    setBusy("save");
    try {
      await wsApi.put(`/brands/${selected.id}`, {
        name: brandName.trim().slice(0, 160) || selected.name,
        dna,
      });
      toast("Brand DNA saved", "success");
      brandsRes.reload();
    } catch (e: any) {
      toast(String(e?.message ?? e), "error", "Save failed");
    } finally {
      setBusy("");
    }
  }

  async function createBrand() {
    const name = newBrand.trim();
    if (!name) return;
    setBusy("create");
    try {
      const r: any = await wsApi.post("/brands", { name });
      setNewBrand("");
      setSelectedId(r?.brand?.id ?? null);
      toast(`Brand “${name}” created`, "success");
      brandsRes.reload();
    } catch (e: any) {
      toast(String(e?.message ?? e), "error", "Create failed");
    } finally {
      setBusy("");
    }
  }

  async function patchBrand(patch: Record<string, unknown>, label: string) {
    if (!selected) return;
    setBusy(label);
    try {
      await wsApi.put(`/brands/${selected.id}`, patch);
      toast(label, "success");
      brandsRes.reload();
    } catch (e: any) {
      toast(String(e?.message ?? e), "error", label + " failed");
    } finally {
      setBusy("");
    }
  }

  async function linkAsset() {
    if (!selected || !linkAssetId) return;
    setBusy("link");
    try {
      await wsApi.post(`/brands/${selected.id}/assets`, {
        media_asset_id: linkAssetId,
        asset_role: linkRole,
        label: linkLabel.trim(),
      });
      toast("Asset linked (reference only — bytes stay in storage)", "success");
      setLinkAssetId("");
      setLinkLabel("");
      brandsRes.reload();
    } catch (e: any) {
      toast(String(e?.message ?? e), "error", "Link failed");
    } finally {
      setBusy("");
    }
  }

  async function unlinkAsset(mediaAssetId: string, role: string) {
    if (!selected) return;
    setBusy("unlink");
    try {
      await wsApi.del(`/brands/${selected.id}/assets?media_asset_id=${encodeURIComponent(mediaAssetId)}&asset_role=${encodeURIComponent(role)}`);
      toast("Asset unlinked", "success");
      brandsRes.reload();
    } catch (e: any) {
      toast(String(e?.message ?? e), "error", "Unlink failed");
    } finally {
      setBusy("");
    }
  }

  async function runVerify() {
    if (!selected) return;
    setBusy("verify");
    setReport(null);
    try {
      const body: Record<string, unknown> = {
        artifact: { text: sampleText },
        artifact_kind: artifactKind,
      };
      if (verifyPlatform) body.platform = verifyPlatform;
      const r: any = await wsApi.post(`/brands/${selected.id}/verify`, body);
      setReport(r?.report ?? null);
    } catch (e: any) {
      toast(String(e?.message ?? e), "error", "Verify failed");
    } finally {
      setBusy("");
    }
  }

  function setDnaKey(key: string, value: unknown) {
    setDna((d) => ({ ...(d ?? {}), [key]: value }));
  }

  const prov: Record<string, string> = effective.data?.provenance ?? {};
  const policy: Record<string, any> = effective.data?.policy ?? {};
  const hard: Record<string, any> = effective.data?.hard_constraints ?? {};

  return (
    <div className="space-y-4">
      <PageHeader
        title="Brands"
        subtitle="Brand DNA identities, asset refs, presets, resolved effective policy and the consistency verifier."
        actions={
          <>
            <Link className="btn-outline !text-xs" to="/brand">◐ White-label kit</Link>
            <button className="btn-primary !text-xs" disabled={!selected || busy === "save"} onClick={save}>
              {busy === "save" ? "Saving…" : "Save DNA"}
            </button>
          </>
        }
      />

      <div className="grid lg:grid-cols-[260px_1fr] gap-4">
        {/* -------- brand list -------- */}
        <Card>
          <div className="panel-label mb-1.5">Workspace brands ({brands.length})</div>
          <div className="flex gap-1.5">
            <input className="input flex-1 !text-[12px]" placeholder="New brand name" value={newBrand}
              onChange={(e) => setNewBrand(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && createBrand()} />
            <button className="btn-primary !text-xs" disabled={busy === "create" || !newBrand.trim()} onClick={createBrand}>
              {busy === "create" ? "…" : "+ Create"}
            </button>
          </div>
          <div className="mt-3 space-y-1">
            {brandsRes.loading && !brands.length && <Loading rows={3} />}
            {brandsRes.error && !brands.length && <ErrorBox error={brandsRes.error} onRetry={brandsRes.reload} />}
            {!brandsRes.loading && !brands.length && !brandsRes.error && (
              <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>
                No brands yet — create one above, then shape its DNA.
              </div>
            )}
            {brands.map((b) => (
              <button key={b.id} onClick={() => setSelectedId(b.id)}
                className="w-full text-left px-2.5 py-2 rounded-xl text-[13px] flex items-center gap-2"
                style={b.id === selectedId
                  ? { background: "var(--accent-dim)", color: "var(--accent-bright)", boxShadow: "inset 0 0 0 1px var(--accent)" }
                  : { color: "var(--text)" }}>
                <span className="truncate flex-1">{b.name || "Untitled"}</span>
                {b.is_default && <Badge tone="info">default</Badge>}
              </button>
            ))}
          </div>
          {selected && (
            <div className="mt-3 pt-3 space-y-2" style={{ borderTop: "var(--seam)" }}>
              <Field label="Brand name">
                <input className="input !text-[12.5px]" value={brandName} onChange={(e) => setBrandName(e.target.value)} />
              </Field>
              <div className="flex flex-wrap gap-1.5">
                {!selected.is_default && (
                  <button className="btn-outline !text-xs" disabled={busy === "default"}
                    onClick={() => patchBrand({ is_default: true }, "Set as default brand")}>
                    Make default
                  </button>
                )}
                <ConfirmButton className="btn-danger !text-xs" confirmText="Archive it?"
                  onConfirm={() => patchBrand({ status: "archived" }, "Brand archived")}>
                  Archive
                </ConfirmButton>
              </div>
              <div className="font-mono text-[10.5px] truncate" style={{ color: "var(--text-faint)" }}>{selected.id}</div>
            </div>
          )}
        </Card>

        {/* -------- tabs -------- */}
        <div>
          <Tabs tabs={TABS} active={tab} onChange={setTab} />

          {!selected && <Card><div className="text-[13px]" style={{ color: "var(--text-faint)" }}>Select or create a brand.</div></Card>}

          {/* ===== DNA editor ===== */}
          {selected && tab === "dna" && dna && (
            <div className="space-y-4">
              <Card>
                <div className="flex items-center gap-2 flex-wrap mb-3">
                  <b className="text-[13.5px]">Visual kit</b>
                  <Badge tone="muted">refs only — never bytes</Badge>
                  <span className="ml-auto">
                    <button className="btn-primary !text-xs" disabled={busy === "save"} onClick={save}>
                      {busy === "save" ? "Saving…" : "Save DNA"}
                    </button>
                  </span>
                </div>
                <div className="grid md:grid-cols-2 gap-5">
                  <div>
                    <div className="panel-label mb-1.5">Brand colors (role → hex)</div>
                    <ColorRows value={dna.colors ?? {}} onChange={(v) => setDnaKey("colors", v)} />
                    <div className="panel-label mt-4 mb-1.5">Fonts (family → stack)</div>
                    <KVEditor value={dna.fonts ?? {}} onChange={(v) => setDnaKey("fonts", v)}
                      keyPlaceholder="heading" valPlaceholder="Inter, sans-serif" />
                  </div>
                  <div>
                    <div className="panel-label mb-1.5">Typography scale</div>
                    <KVEditor value={dna.typography ?? {}} onChange={(v) => setDnaKey("typography", v)}
                      keyPlaceholder="h1_size" valPlaceholder="64" />
                    <div className="panel-label mt-4 mb-1.5">Logo refs (MediaAsset ids)</div>
                    <TagList items={dna.logos ?? []} onChange={(v) => setDnaKey("logos", v)} placeholder="asset id + Enter" />
                    <div className="panel-label mt-4 mb-1.5">Watermark</div>
                    <KVEditor value={dna.watermark ?? {}} onChange={(v) => setDnaKey("watermark", v)}
                      keyPlaceholder="asset_id" valPlaceholder="ref" />
                  </div>
                </div>
              </Card>

              <Card>
                <b className="text-[13.5px]">Motion & captions</b>
                <div className="grid md:grid-cols-2 gap-5 mt-3">
                  <div>
                    <div className="panel-label mb-1.5">Caption style</div>
                    <KVEditor value={dna.caption_style ?? {}} onChange={(v) => setDnaKey("caption_style", v)}
                      keyPlaceholder="preset" valPlaceholder="pop" />
                  </div>
                  <div>
                    <div className="panel-label mb-1.5">CTA style</div>
                    <KVEditor value={dna.cta_style ?? {}} onChange={(v) => setDnaKey("cta_style", v)}
                      keyPlaceholder="text_style" valPlaceholder="bold" />
                  </div>
                </div>
              </Card>

              <Card>
                <b className="text-[13.5px]">Verbal identity</b>
                <div className="grid md:grid-cols-2 gap-5 mt-3">
                  <div className="space-y-4">
                    <Field label="Tone / writing voice" hint="Free text — consumed as the generation tone constraint.">
                      <input className="input" value={String(dna.writing_tone ?? "")}
                        onChange={(e) => setDnaKey("writing_tone", e.target.value)} placeholder="direct, numbers-first, zero hype" />
                    </Field>
                    <div>
                      <div className="panel-label mb-1.5">Vocabulary — preferred</div>
                      <TagList items={dna.vocabulary?.preferred ?? []} placeholder="term + Enter"
                        onChange={(v) => setDnaKey("vocabulary", { ...(dna.vocabulary ?? {}), preferred: v })} />
                    </div>
                    <div>
                      <div className="panel-label mb-1.5">Vocabulary — avoid</div>
                      <TagList items={dna.vocabulary?.avoid ?? []} placeholder="term + Enter"
                        onChange={(v) => setDnaKey("vocabulary", { ...(dna.vocabulary ?? {}), avoid: v })} />
                    </div>
                    <div>
                      <div className="panel-label mb-1.5">Pronunciation rules</div>
                      <PronunciationRows value={dna.pronunciation_rules ?? []}
                        onChange={(v) => setDnaKey("pronunciation_rules", v)} />
                    </div>
                  </div>
                  <div className="space-y-4">
                    <div>
                      <div className="panel-label mb-1.5">Forbidden phrases</div>
                      <TagList items={dna.forbidden_phrases ?? []} placeholder="phrase + Enter"
                        onChange={(v) => setDnaKey("forbidden_phrases", v)} />
                      <div className="text-[11px] mt-1" style={{ color: "var(--text-faint)" }}>
                        Hard constraint — the verifier fails any artifact containing these.
                      </div>
                    </div>
                    <div>
                      <div className="panel-label mb-1.5">Required disclaimers</div>
                      <TagList items={dna.required_disclaimers ?? []} placeholder="disclaimer + Enter"
                        onChange={(v) => setDnaKey("required_disclaimers", v)} />
                    </div>
                    <div>
                      <div className="panel-label mb-1.5">Claims policy</div>
                      <KVEditor value={dna.claims_policy ?? {}} onChange={(v) => setDnaKey("claims_policy", v)}
                        keyPlaceholder="financial_claims" valPlaceholder="disclosure_required" />
                    </div>
                  </div>
                </div>
              </Card>

              <Card>
                <b className="text-[13.5px]">Approved voice &amp; avatar</b>
                <div className="grid md:grid-cols-2 gap-5 mt-3">
                  <div className="space-y-3">
                    <Field label="Default voice id">
                      <input className="input font-mono" value={String(dna.voice_identity?.default ?? "")}
                        onChange={(e) => setDnaKey("voice_identity", { ...(dna.voice_identity ?? {}), default: e.target.value })}
                        placeholder="ava" />
                    </Field>
                    <div>
                      <div className="panel-label mb-1.5">Approved voices</div>
                      <TagList items={dna.voice_identity?.approved ?? []} placeholder="voice id + Enter"
                        onChange={(v) => setDnaKey("voice_identity", { ...(dna.voice_identity ?? {}), approved: v })} />
                    </div>
                  </div>
                  <div className="space-y-3">
                    <Field label="Default avatar id">
                      <input className="input font-mono" value={String(dna.avatar_identity?.default ?? "")}
                        onChange={(e) => setDnaKey("avatar_identity", { ...(dna.avatar_identity ?? {}), default: e.target.value })}
                        placeholder="avatar_01" />
                    </Field>
                    <div>
                      <div className="panel-label mb-1.5">Approved avatars</div>
                      <TagList items={dna.avatar_identity?.approved ?? []} placeholder="avatar id + Enter"
                        onChange={(v) => setDnaKey("avatar_identity", { ...(dna.avatar_identity ?? {}), approved: v })} />
                    </div>
                  </div>
                </div>
              </Card>

              <Card>
                <div className="flex items-center gap-2 flex-wrap mb-3">
                  <b className="text-[13.5px]">Platform overrides</b>
                  <Badge tone="muted">applied last by the inheritance resolver</Badge>
                </div>
                <div className="flex flex-wrap gap-1.5 mb-3">
                  {PLATFORMS.map((p) => {
                    const has = Boolean((dna.platform_overrides ?? {})[p]);
                    return (
                      <button key={p} className={`chip !py-1 ${overridePlatform === p ? "on" : ""}`}
                        onClick={() => setOverridePlatform(overridePlatform === p ? "" : p)}>
                        {p}{has ? " •" : ""}
                      </button>
                    );
                  })}
                  {Object.keys(dna.platform_overrides ?? {})
                    .filter((p) => !PLATFORMS.includes(p))
                    .map((p) => (
                      <button key={p} className={`chip !py-1 ${overridePlatform === p ? "on" : ""}`}
                        onClick={() => setOverridePlatform(overridePlatform === p ? "" : p)}>{p} •</button>
                    ))}
                </div>
                {overridePlatform && (
                  <PlatformFragment
                    fragment={(dna.platform_overrides ?? {})[overridePlatform] ?? {}}
                    onChange={(frag) => setDnaKey("platform_overrides", {
                      ...(dna.platform_overrides ?? {}),
                      [overridePlatform]: frag,
                    })}
                    onRemove={() => {
                      const next = { ...(dna.platform_overrides ?? {}) };
                      delete next[overridePlatform];
                      setDnaKey("platform_overrides", next);
                      setOverridePlatform("");
                    }}
                  />
                )}
                {!overridePlatform && (
                  <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>
                    Pick a platform above to edit its fragment (colors, phrases, disclaimers, caption style).
                  </div>
                )}
              </Card>
            </div>
          )}

          {/* ===== asset library (link existing MediaAssets only) ===== */}
          {selected && tab === "assets" && (
            <div className="space-y-4">
              <Card>
                <div className="flex items-center gap-2 flex-wrap mb-3">
                  <b className="text-[13.5px]">Linked brand assets</b>
                  <Badge tone="muted">MediaAsset references — no uploads here</Badge>
                </div>
                {!assets.length && (
                  <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>
                    Nothing linked yet — link an existing workspace asset to a role below.
                  </div>
                )}
                <div className="space-y-1">
                  {assets.map((a) => (
                    <div key={a.id} className="flex items-center gap-2 py-1.5 text-[12.5px]" style={{ borderBottom: "var(--seam)" }}>
                      <Badge tone="info">{a.asset_role}</Badge>
                      <span className="font-mono text-[11.5px] truncate">{a.media_asset_id}</span>
                      <span className="truncate flex-1" style={{ color: "var(--text-muted)" }}>{a.label}</span>
                      <button className="btn-ghost !text-xs" disabled={busy === "unlink"}
                        onClick={() => unlinkAsset(a.media_asset_id, a.asset_role)}>Unlink</button>
                    </div>
                  ))}
                </div>
                <div className="mt-4 grid md:grid-cols-[1fr_150px_1fr_auto] gap-2 items-end">
                  <Field label="Workspace media asset">
                    <select className="select" value={linkAssetId} onChange={(e) => setLinkAssetId(e.target.value)}>
                      <option value="">Choose an asset…</option>
                      {(media.data?.items ?? []).map((m: any) => (
                        <option key={m.id} value={m.id}>
                          {m.title || (m.storage_key ?? "").split("/").pop() || m.id} · {m.type}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Role">
                    <select className="select" value={linkRole} onChange={(e) => setLinkRole(e.target.value)}>
                      {ASSET_ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                    </select>
                  </Field>
                  <Field label="Label (optional)">
                    <input className="input" value={linkLabel} onChange={(e) => setLinkLabel(e.target.value)} placeholder="primary logo" />
                  </Field>
                  <button className="btn-primary !text-xs" disabled={!linkAssetId || busy === "link"} onClick={linkAsset}>
                    {busy === "link" ? "Linking…" : "Link asset"}
                  </button>
                </div>
                {media.error && <div className="text-[12px] mt-2" style={{ color: "var(--danger)" }}>Assets: {media.error}</div>}
              </Card>
            </div>
          )}

          {/* ===== presets ===== */}
          {tab === "presets" && (
            <Card>
              <b className="text-[13.5px]">Creative template presets (GET /brands/presets)</b>
              {presets.loading && <Loading rows={3} />}
              {presets.error && <ErrorBox error={presets.error} onRetry={presets.reload} />}
              {presets.data && !(presets.data.presets ?? []).length && (
                <div className="text-[12.5px] mt-2" style={{ color: "var(--text-faint)" }}>No presets configured for this workspace.</div>
              )}
              <div className="mt-3 space-y-2">
                {(presets.data?.presets ?? []).map((p: any) => (
                  <div key={p.id} className="rounded-lg p-3" style={{ border: "var(--seam)", background: "var(--bg-inset)" }}>
                    <div className="flex items-center gap-2">
                      <b className="text-[13px]">{p.name}</b>
                      {p.builtin && <Badge tone="info">builtin</Badge>}
                    </div>
                    <pre className="mt-1.5 text-[11.5px] overflow-x-auto font-mono" style={{ color: "var(--text-muted)" }}>
                      {JSON.stringify(p.preset, null, 2)}
                    </pre>
                  </div>
                ))}
              </div>
            </Card>
          )}

          {/* ===== effective policy ===== */}
          {tab === "effective" && (
            <div className="space-y-4">
              <Card>
                <div className="flex items-center gap-2 flex-wrap">
                  <b className="text-[13.5px]">Resolved effective policy</b>
                  <Badge tone="muted">workspace → brand → campaign → content → platform</Badge>
                  <select className="select ml-auto !w-auto !text-[12px]" value={platform}
                    onChange={(e) => setPlatform(e.target.value)} aria-label="Platform override">
                    <option value="">base (no platform)</option>
                    {PLATFORMS.map((p) => <option key={p} value={p}>{p}</option>)}
                  </select>
                  <button className="btn-ghost !text-xs" onClick={effective.reload}>Refresh</button>
                </div>
                {effective.loading && !effective.data && <Loading rows={4} />}
                {effective.error && <ErrorBox error={effective.error} onRetry={effective.reload} />}
                {effective.data && (
                  <>
                    <div className="flex flex-wrap gap-1.5 mt-3">
                      <Badge tone="muted">dna {effective.data.dna_version || "—"}</Badge>
                      <Badge tone="muted">config {String(effective.data.effective_config_id || "—").slice(0, 14)}</Badge>
                      {Object.entries(effective.data.subject ?? {}).map(([k, v]) =>
                        v ? <Badge key={k} tone="info">{k}: {String(v).slice(0, 12)}</Badge> : null)}
                    </div>

                    <div className="panel-label mt-4 mb-1.5">Hard constraints</div>
                    <div className="space-y-0">
                      {[
                        ["forbidden_phrases", hard.forbidden_phrases],
                        ["required_disclaimers", hard.required_disclaimers],
                        ["approved_voices", hard.approved_voices],
                        ["approved_avatars", hard.approved_avatars],
                      ].map(([name, value]: any) => (
                        <div key={name} className="flex flex-wrap items-center gap-2 py-1.5" style={{ borderBottom: "var(--seam)" }}>
                          <span className="font-mono text-[11.5px] w-[170px]" style={{ color: "var(--text-faint)" }}>{name}</span>
                          <span className="flex flex-wrap gap-1.5">
                            {(value ?? []).length
                              ? value.map((v: string, i: number) => <span key={i} className="chip !text-[11px] !py-0.5">{v}</span>)
                              : <span className="text-[12px]" style={{ color: "var(--text-faint)" }}>—</span>}
                          </span>
                          <Badge tone={PROV_TONE[prov[name] ?? "workspace"] ?? "muted"}>{prov[name] ?? "workspace"}</Badge>
                        </div>
                      ))}
                      <div className="flex flex-wrap items-center gap-2 py-1.5" style={{ borderBottom: "var(--seam)" }}>
                        <span className="font-mono text-[11.5px] w-[170px]" style={{ color: "var(--text-faint)" }}>logo_safe_zone</span>
                        <span className="font-mono text-[12px]">{JSON.stringify(hard.logo_safe_zone ?? {})}</span>
                        <Badge tone={PROV_TONE[prov.logo_safe_zone ?? "workspace"] ?? "muted"}>{prov.logo_safe_zone ?? "workspace"}</Badge>
                      </div>
                    </div>

                    <div className="panel-label mt-4 mb-1.5">Full policy + provenance</div>
                    <div className="space-y-0">
                      {[
                        "tone", "brand_colors", "caption_style", "cta_style", "vocabulary",
                        "pronunciation_rules", "fonts", "claims_policy", "watermark",
                        "approved_logos", "thumbnail_style",
                      ].map((name) => {
                        const value = policy[name];
                        const empty = value == null || value === "" ||
                          (Array.isArray(value) && !value.length) ||
                          (typeof value === "object" && !Array.isArray(value) && !Object.keys(value).length);
                        return (
                          <div key={name} className="flex flex-wrap items-center gap-2 py-1.5" style={{ borderBottom: "var(--seam)" }}>
                            <span className="font-mono text-[11.5px] w-[170px]" style={{ color: "var(--text-faint)" }}>{name}</span>
                            <span className="text-[12.5px] min-w-0 break-words flex-1">
                              {name === "brand_colors" && Array.isArray(value) && value.length ? (
                                <span className="flex gap-1.5 flex-wrap">
                                  {value.map((hex: string, i: number) => (
                                    <span key={i} className="flex items-center gap-1 chip !text-[11px] !py-0.5">
                                      <span className="w-3 h-3 rounded-sm inline-block" style={{ background: hex, border: "1px solid var(--border)" }} />
                                      {hex}
                                    </span>
                                  ))}
                                </span>
                              ) : empty ? (
                                <span style={{ color: "var(--text-faint)" }}>—</span>
                              ) : typeof value === "string" ? (
                                value
                              ) : (
                                <span className="font-mono text-[11.5px] break-all">{JSON.stringify(value)}</span>
                              )}
                            </span>
                            <Badge tone={PROV_TONE[prov[name] ?? "workspace"] ?? "muted"}>{prov[name] ?? "workspace"}</Badge>
                          </div>
                        );
                      })}
                    </div>
                  </>
                )}
              </Card>
            </div>
          )}

          {/* ===== verify ===== */}
          {selected && tab === "verify" && (
            <div className="space-y-4">
              <Card>
                <div className="flex items-center gap-2 flex-wrap mb-2">
                  <b className="text-[13.5px]">Brand consistency check</b>
                  <Badge tone="muted">POST /brands/&#123;id&#125;/verify</Badge>
                </div>
                <Field label="Sample artifact text" hint="Every string in the artifact is scanned for forbidden phrases, required disclaimers and terminology.">
                  <textarea className="textarea w-full" rows={4} value={sampleText}
                    onChange={(e) => setSampleText(e.target.value)} />
                </Field>
                <div className="flex flex-wrap gap-2 items-end">
                  <Field label="Artifact kind">
                    <select className="select" value={artifactKind} onChange={(e) => setArtifactKind(e.target.value)}>
                      {["clip", "script", "caption", "post", "thumbnail"].map((k) => <option key={k} value={k}>{k}</option>)}
                    </select>
                  </Field>
                  <Field label="Platform">
                    <select className="select" value={verifyPlatform} onChange={(e) => setVerifyPlatform(e.target.value)}>
                      <option value="">—</option>
                      {PLATFORMS.map((p) => <option key={p} value={p}>{p}</option>)}
                    </select>
                  </Field>
                  <button className="btn-primary !text-xs" disabled={busy === "verify"} onClick={runVerify}>
                    {busy === "verify" ? "Verifying…" : "Run verify"}
                  </button>
                </div>
              </Card>

              {report && (
                <Card>
                  <div className="flex items-center gap-2 flex-wrap">
                    <b className="text-[13.5px]">BrandConsistencyReport</b>
                    <Badge tone={REPORT_TONE[report.status] ?? "muted"}>{report.status}</Badge>
                    <Badge tone="muted">kind: {report.artifact_kind || "—"}</Badge>
                    <Badge tone="muted">{report.authoritative ? "authoritative" : "advisory"}</Badge>
                  </div>
                  <div className="mt-3 space-y-0">
                    {Object.entries(report.checks ?? {}).map(([name, check]: any) => (
                      <div key={name} className="flex items-start gap-2.5 py-2" style={{ borderBottom: "var(--seam)" }}>
                        <span className="w-[160px] shrink-0 font-mono text-[11.5px]" style={{ color: "var(--text-faint)" }}>{name}</span>
                        <Badge tone={check?.status === "pass" ? "success" : check?.status === "warn" ? "warning" : check?.status === "fail" ? "error" : "muted"}>
                          {String(check?.status ?? "").toUpperCase() || "—"}
                        </Badge>
                        <span className="text-[12.5px] flex-1 break-words">{check?.detail}</span>
                      </div>
                    ))}
                  </div>
                  {report.semantic && (
                    <div className="mt-3 text-[12px]" style={{ color: "var(--text-muted)" }}>
                      <Badge tone="muted">SHADOW advisory</Badge>{" "}
                      {report.semantic.detail ?? ""} <span style={{ color: "var(--text-faint)" }}>(never authoritative)</span>
                    </div>
                  )}
                  {report.provenance && (
                    <div className="mt-3 flex flex-wrap gap-1.5">
                      {Object.entries(report.provenance).map(([field, level]) => (
                        <Badge key={field} tone={PROV_TONE[level as string] ?? "muted"}>{field}: {String(level)}</Badge>
                      ))}
                    </div>
                  )}
                </Card>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/* per-platform DNA fragment sub-editor (validated by _check_fragment server-side) */
function PlatformFragment({ fragment, onChange, onRemove }: {
  fragment: Record<string, any>;
  onChange: (f: Record<string, any>) => void;
  onRemove: () => void;
}) {
  return (
    <div className="space-y-4 rounded-xl p-4" style={{ border: "var(--seam)", background: "var(--bg-inset)" }}>
      <div className="flex items-center gap-2">
        <b className="text-[13px]">Override fragment</b>
        <ConfirmButton className="btn-danger !text-xs" confirmText="Delete override?" onConfirm={onRemove}>
          Remove override
        </ConfirmButton>
      </div>
      <div className="grid md:grid-cols-2 gap-5">
        <div>
          <div className="panel-label mb-1.5">Colors</div>
          <ColorRows value={fragment.colors ?? {}} onChange={(v) => onChange({ ...fragment, colors: v })} />
        </div>
        <div>
          <div className="panel-label mb-1.5">Caption style</div>
          <KVEditor value={fragment.caption_style ?? {}} onChange={(v) => onChange({ ...fragment, caption_style: v })}
            keyPlaceholder="preset" valPlaceholder="pop" />
        </div>
        <div>
          <div className="panel-label mb-1.5">Forbidden phrases</div>
          <TagList items={fragment.forbidden_phrases ?? []} placeholder="phrase + Enter"
            onChange={(v) => onChange({ ...fragment, forbidden_phrases: v })} />
        </div>
        <div>
          <div className="panel-label mb-1.5">Required disclaimers</div>
          <TagList items={fragment.required_disclaimers ?? []} placeholder="disclaimer + Enter"
            onChange={(v) => onChange({ ...fragment, required_disclaimers: v })} />
        </div>
      </div>
    </div>
  );
}
