import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Card, Field, PageHeader } from "../components/ui";

/* Manual schedule composer: pick any render-ready content, platform and time. */

export default function Composer() {
  const nav = useNavigate();
  const lib = useFetch(() => wsApi.get("/content?limit=100"), []);
  const [contentId, setContentId] = useState("");
  const [platform, setPlatform] = useState("youtube");
  const [runAt, setRunAt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const items: any[] = ((lib.data as any)?.items ?? []).filter((c: any) => c.video?.status === "READY");

  async function submit() {
    setBusy(true);
    setError("");
    try {
      // datetime-local is naive — convert to explicit UTC ISO for the API guard.
      const iso = new Date(runAt).toISOString();
      await wsApi.post("/calendar", { content_item_id: contentId, platform, run_at: iso });
      nav("/calendar");
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="max-w-[620px] mx-auto">
      <PageHeader title="Composer" subtitle="Manually queue any render-ready video for a platform and time." />
      <Card>
        <Field label="Video (render-ready only)">
          <select className="select" value={contentId} onChange={(e) => setContentId(e.target.value)}>
            <option value="">— pick a video —</option>
            {items.map((c: any) => <option key={c.id} value={c.id}>{c.topic.slice(0, 80)} ({c.video?.engine})</option>)}
          </select>
        </Field>
        <Field label="Platform">
          <select className="select" value={platform} onChange={(e) => setPlatform(e.target.value)}>
            {["youtube", "tiktok", "facebook", "instagram"].map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </Field>
        <Field label="Publish at (local time)" hint="Converted to UTC ISO — the sweep dispatches when due.">
          <input className="input" type="datetime-local" value={runAt} onChange={(e) => setRunAt(e.target.value)} />
        </Field>
        {error && <div className="text-[13px] mb-3" style={{ color: "var(--danger)" }}>{error}</div>}
        <button className="btn-primary" disabled={busy || !contentId || !runAt} onClick={submit}>
          {busy ? "…" : "Schedule publish"}
        </button>
      </Card>
    </div>
  );
}
