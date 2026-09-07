import { useEffect, useState } from "react";
import { wsApi, api } from "../lib/api";
import { Badge, Card, Field, PageHeader, useToast } from "../components/ui";

const PLATFORMS = ["youtube", "tiktok", "facebook", "instagram"];

/**
 * Universal post composer: schedules a real calendar entry per platform with
 * platform-specific copy. AI-generated metadata arrives from the SEO agent
 * during the pipeline; manual scheduling composes what you type here.
 */
export default function Composer() {
  const [platforms, setPlatforms] = useState<string[]>(["youtube"]);
  const [runAt, setRunAt] = useState(() => new Date(Date.now() + 3600_000).toISOString().slice(0, 16));
  const [contentId, setContentId] = useState("");
  const [contentItems, setContentItems] = useState<any[]>([]);
  const [perPlatform, setPerPlatform] = useState<Record<string, { title: string; caption: string; hashtags: string }>>({});
  const [busy, setBusy] = useState(false);
  const [estimate, setEstimate] = useState<any>(null);
  const { push } = useToast();

  useEffect(() => {
    wsApi.get("/content?limit=50").then((r) => setContentItems(r.items ?? [])).catch(() => {});
    // Cost estimate is engine-aware and static per configuration
    wsApi.post("/content/estimate-cost", { video_count: 1 }).then(setEstimate).catch(() => {});
  }, []);

  function toggle(p: string) {
    setPlatforms((ps) => ps.includes(p) ? ps.filter((x) => x !== p) : [...ps, p]);
    setPerPlatform((m) => (m[p] ? m : { ...m, [p]: { title: "", caption: "", hashtags: "" } }));
  }

  async function schedule() {
    if (!contentId) { push("error", "Select a content item to schedule"); return; }
    if (platforms.length === 0) { push("error", "Pick at least one platform"); return; }
    setBusy(true);
    try {
      for (const p of platforms) {
        await wsApi.post("/calendar", {
          platform: p,
          run_at: new Date(runAt).toISOString(),
          content_item_id: contentId,
        });
      }
      push("success", `Scheduled on ${platforms.length} platform(s)`);
    } catch (e: any) {
      push("error", e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-5 max-w-3xl">
      <PageHeader
        title="Composer"
        subtitle="Schedule platform-specific posts for existing content. Each platform gets its own copy."
      />

      <Card>
        <div className="space-y-5">
          <Field label="Content item">
            <select className="select" value={contentId} onChange={(e) => setContentId(e.target.value)}>
              <option value="">— select content —</option>
              {contentItems.map((c) => (
                <option key={c.id} value={c.id}>
                  [{c.status}] {c.topic.slice(0, 80)}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Platforms" hint="Each selected platform is scheduled separately with its own copy below.">
            <div className="flex gap-2 flex-wrap">
              {PLATFORMS.map((p) => (
                <button
                  key={p}
                  onClick={() => toggle(p)}
                  aria-pressed={platforms.includes(p)}
                  className={`btn-outline capitalize ${platforms.includes(p) ? "!border-emerald-500 !text-emerald-600 dark:!text-emerald-400" : ""}`}
                >
                  {p}
                </button>
              ))}
            </div>
          </Field>

          <Field label="Publish at">
            <input className="input !w-64" type="datetime-local" value={runAt} onChange={(e) => setRunAt(e.target.value)} />
          </Field>

          {platforms.map((p) => (
            <div key={p} className="rounded-lg border p-4 space-y-3" style={{ borderColor: "var(--border)" }}>
              <div className="flex items-center justify-between">
                <h3 className="font-medium text-sm uppercase tracking-wide">{p}</h3>
                <Badge tone="neutral">platform-specific</Badge>
              </div>
              {p === "youtube" && (
                <Field label="Title">
                  <input className="input" maxLength={100}
                    value={perPlatform[p]?.title ?? ""}
                    onChange={(e) => setPerPlatform((m) => ({ ...m, [p]: { ...m[p], title: e.target.value } }))} />
                </Field>
              )}
              <Field label={p === "youtube" ? "Description" : "Caption"}>
                <textarea className="textarea" rows={3} maxLength={2200}
                  value={perPlatform[p]?.caption ?? ""}
                  onChange={(e) => setPerPlatform((m) => ({ ...m, [p]: { ...m[p], caption: e.target.value } }))} />
              </Field>
              <Field label="Hashtags" hint="Space-separated; appended where the platform supports them.">
                <input className="input" placeholder="#money #finance"
                  value={perPlatform[p]?.hashtags ?? ""}
                  onChange={(e) => setPerPlatform((m) => ({ ...m, [p]: { ...m[p], hashtags: e.target.value } }))} />
              </Field>
            </div>
          ))}

          <div className="flex items-center justify-between gap-3 pt-1 flex-wrap">
            {estimate && (
              <span className="badge" style={{ background: "var(--bg-subtle)", color: "var(--text-muted)" }}
                    title={`Engine: ${estimate.engine} — flat per-render estimate`}>
                ≈ ${estimate.per_video_usd?.toFixed?.(2) ?? estimate.per_video_usd} / render · {estimate.engine}
              </span>
            )}
            <button className="btn-primary" onClick={schedule} disabled={busy}>
              {busy ? "Scheduling…" : `Schedule on ${platforms.length || 0} platform(s)`}
            </button>
          </div>
        </div>
      </Card>

      <p className="text-xs" style={{ color: "var(--text-muted)" }}>
        Note: AI-optimized titles/captions/hashtags are generated by the SEO agent during
        the autopilot pipeline and stored on each content item's variants. Manual entries
        here control your own scheduled copies.
      </p>
    </div>
  );
}
