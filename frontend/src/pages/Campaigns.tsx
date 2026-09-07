import { useCallback, useEffect, useState } from "react";
import { wsApi } from "../lib/api";
import {
  AsyncSection, Badge, Card, Field, Modal, PageHeader, useToast,
} from "../components/ui";

const PLATFORM_OPTIONS = ["youtube", "tiktok", "facebook", "instagram"];

export default function Campaigns() {
  const [campaigns, setCampaigns] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const { push } = useToast();

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const r = await wsApi.get("/campaigns");
      setCampaigns(r.items ?? []);
    } catch (e: any) { setError(e.message); } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <div className="space-y-5">
      <PageHeader
        title="Campaigns"
        subtitle="Time-boxed content operations with goals, budgets and cadence."
        actions={<button className="btn-primary" onClick={() => setShowCreate(true)}>+ New campaign</button>}
      />

      <AsyncSection data={campaigns} loading={loading} error={error} onRetry={load}
        empty="No campaigns yet"
        emptyHint="The autopilot runs workspace-wide; campaigns organize production toward a specific goal.">
        {(list) => (
          <div className="grid md:grid-cols-2 gap-4">
            {(list as any[]).map((c) => (
              <CampaignCard key={c.id} campaign={c} onChanged={load} />
            ))}
          </div>
        )}
      </AsyncSection>

      <CreateModal open={showCreate} onClose={() => { setShowCreate(false); load(); }} />
    </div>
  );
}

function CampaignCard({ campaign: c, onChanged }: any) {
  const [detail, setDetail] = useState<any>(null);
  useEffect(() => {
    wsApi.get(`/campaigns/${c.id}`).then(setDetail).catch(() => {});
  }, [c.id]);

  async function start() {
    await wsApi.post("/autopilot/start", {
      mode: "CONTINUOUS",
      cycles_target: c.target_videos || 0,
      config: { interval_seconds: 30 },
    });
    onChanged();
  }

  const progressPct = detail?.target_videos
    ? Math.min(100, Math.round((detail.progress.published / detail.target_videos) * 100))
    : null;

  return (
    <Card>
      <div className="flex items-start justify-between gap-3 mb-2">
        <h3 className="font-semibold">{c.name}</h3>
        <Badge tone={c.status === "RUNNING" ? "success" : "neutral"}>{(detail?.status ?? c.status).toLowerCase()}</Badge>
      </div>
      {c.goal && <p className="text-[13px] mb-2" style={{ color: "var(--text-muted)" }}>{c.goal}</p>}
      <dl className="text-[12px] space-y-1 mb-3" style={{ color: "var(--text-muted)" }}>
        <div>Platforms: {(c.platforms ?? []).join(", ") || "any"}</div>
        <div>Target: {c.target_videos || "∞"} videos · {c.videos_per_day || "—"}/day</div>
        {c.budget_daily_usd != null && <div>Budget: ${c.budget_daily_usd}/day</div>}
      </dl>

      {progressPct != null && (
        <div className="mb-3">
          <div className="flex justify-between text-[11px] mb-1" style={{ color: "var(--text-muted)" }}>
            <span>{detail.progress.published}/{detail.target_videos} published</span>
            <span>{progressPct}%</span>
          </div>
          <div className="h-1.5 rounded-full overflow-hidden" style={{ background: "var(--bg-subtle)" }}>
            <div className="h-full rounded-full transition-all" style={{ width: `${progressPct}%`, background: "var(--accent)" }} />
          </div>
        </div>
      )}

      <div className="flex gap-2">
        <button className="btn-primary !py-1 text-xs" onClick={start}>Run autopilot for target</button>
        <LinkDetail id={c.id} />
      </div>
    </Card>
  );
}

function LinkDetail({ id }: any) {
  // campaign analytics surface via Analytics page filters; keep honest link to studio filtered by campaign
  return (
    <a className="btn-outline !py-1 text-xs" href={`/studio?campaign=${id}`}>View content</a>
  );
}

function CreateModal({ open, onClose }: any) {
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [targetVideos, setTargetVideos] = useState("10");
  const [perDay, setPerDay] = useState("1");
  const [platforms, setPlatforms] = useState<string[]>(["youtube"]);
  const [budget, setBudget] = useState("");
  const { push } = useToast();

  async function create() {
    if (!name.trim()) { push("error", "Name is required"); return; }
    try {
      await wsApi.post("/campaigns", {
        name,
        goal,
        target_videos: parseInt(targetVideos) || 0,
        videos_per_day: parseFloat(perDay) || 0,
        platforms,
        automation_level: "FULL_AUTOPILOT",
        budget_daily_usd: budget ? parseFloat(budget) : null,
      });
      push("success", "Campaign created");
      onClose();
    } catch (e: any) { push("error", e.message); }
  }

  return (
    <Modal open={open} onClose={onClose}>
      <h3 className="font-semibold mb-4">New campaign</h3>
      <div className="space-y-4">
        <Field label="Name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="30-Day AI Shorts" /></Field>
        <Field label="Goal"><textarea className="textarea" rows={2} value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="Grow shorts channel with qualified engagement…" /></Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Target videos"><input className="input" type="number" value={targetVideos} onChange={(e) => setTargetVideos(e.target.value)} /></Field>
          <Field label="Videos / day"><input className="input" type="number" step="0.5" value={perDay} onChange={(e) => setPerDay(e.target.value)} /></Field>
        </div>
        <Field label="Platforms">
          <div className="flex gap-2 flex-wrap">
            {PLATFORM_OPTIONS.map((p) => (
              <button key={p} aria-pressed={platforms.includes(p)}
                onClick={() => setPlatforms((ps) => ps.includes(p) ? ps.filter((x) => x !== p) : [...ps, p])}
                className={`btn-outline capitalize !py-1 text-xs ${platforms.includes(p) ? "!border-emerald-500 !text-emerald-600 dark:!text-emerald-400" : ""}`}>
                {p}
              </button>
            ))}
          </div>
        </Field>
        <Field label="Daily budget ($, optional)">
          <input className="input" type="number" step="0.5" value={budget} onChange={(e) => setBudget(e.target.value)} />
        </Field>
        <div className="flex justify-end gap-2">
          <button className="btn-outline" onClick={onClose}>Cancel</button>
          <button className="btn-primary" onClick={create}>Create</button>
        </div>
      </div>
    </Modal>
  );
}
