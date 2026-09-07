import { useCallback, useEffect, useState } from "react";
import { wsApi } from "../lib/api";
import { AsyncSection, Badge, Card, PageHeader, fmtDate } from "../components/ui";

export default function Assets() {
  const [assets, setAssets] = useState<any[]>([]);
  const [capabilities, setCapabilities] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const r = await wsApi.get("/assets");
      setAssets(r.items ?? []);
      setCapabilities(r.capabilities ?? null);
    } catch (e: any) { setError(e.message); } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <div className="space-y-5">
      <PageHeader
        title="Asset Library"
        subtitle="Every artifact the system has produced."
      />

      {capabilities && !capabilities.upload && (
        <Card><p className="text-[12px]" style={{ color: "var(--text-muted)" }}>
          ⓘ {capabilities.note}
        </p></Card>
      )}

      <AsyncSection data={assets} loading={loading} error={error} onRetry={load}
        empty="No assets yet" emptyHint="Rendered videos appear here automatically after each BUILD stage.">
        {(list) => (
          <Card pad={false}>
            <table className="table">
              <thead>
                <tr><th>Artifact</th><th>Type</th><th>Format</th><th>Status</th><th>Size</th><th>Created</th><th></th></tr>
              </thead>
              <tbody>
                {(list as any[]).map((a) => (
                  <tr key={a.id}>
                    <td className="max-w-[300px] truncate font-medium">{a.title}</td>
                    <td className="capitalize">{a.type}{a.is_mock && <Badge tone="warning">mock</Badge>}</td>
                    <td>{a.aspect_ratio} · {a.resolution}</td>
                    <td><Badge tone={a.status === "READY" ? "success" : "neutral"}>{a.status.toLowerCase()}</Badge></td>
                    <td>{a.size_bytes ? `${(a.size_bytes / 1024 / 1024).toFixed(1)} MB` : "—"}</td>
                    <td className="text-[12px]" style={{ color: "var(--text-muted)" }}>{fmtDate(a.created_at)}</td>
                    <td className="text-right">
                      <a
                        className="btn-outline !py-1 !px-2 text-xs"
                        href={`/api/v1/workspaces/${localStorage.getItem("ym_ws")}/videos/${a.video_id}/file`}
                        target="_blank" rel="noreferrer"
                      >
                        Open
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        )}
      </AsyncSection>
    </div>
  );
}
