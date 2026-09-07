import { useCallback, useEffect, useState } from "react";
import { wsApi } from "../lib/api";
import { AsyncSection, Badge, Card, PageHeader, Tabs } from "../components/ui";
import { AreaChart, BarChart } from "../components/charts";

export default function Analytics() {
  const [tab, setTab] = useState("overview");
  const [overview, setOverview] = useState<any>(null);
  const [posts, setPosts] = useState<any[]>([]);
  const [costs, setCosts] = useState<any>(null);
  const [breakdowns, setBreakdowns] = useState<any>(null);
  const [patterns, setPatterns] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [o, p, c, b, pat] = await Promise.all([
        wsApi.get("/analytics/overview"),
        wsApi.get("/publishing/posts?limit=100"),
        wsApi.get("/costs/intelligence"),
        wsApi.get("/analytics/breakdowns"),
        wsApi.get("/analytics/patterns"),
      ]);
      setOverview(o);
      setPosts(p.items ?? []);
      setCosts(c);
      setBreakdowns(b);
      setPatterns(pat);
    } catch (e: any) { setError(e.message); } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const ranked = [...posts]
    .filter((p) => (p.metrics?.views ?? 0) > 0)
    .sort((a, b) => b.metrics.views - a.metrics.views);

  return (
    <div className="space-y-5">
      <PageHeader
        title="Analytics"
        subtitle="Real performance data collected by the Analytics Agent."
        actions={overview?.mock_analytics ? <Badge tone="warning">MOCK ANALYTICS — simulated metrics</Badge> : undefined}
      />

      <Tabs
        tabs={[
          { key: "overview", label: "Overview" },
          { key: "breakdowns", label: "Breakdowns" },
          { key: "patterns", label: "Learned patterns" },
          { key: "platforms", label: "Platforms" },
          { key: "content", label: "Content performance" },
          { key: "costs", label: "Costs & efficiency" },
        ]}
        active={tab}
        onChange={setTab}
      />

      <AsyncSection data={overview} loading={loading} error={error} onRetry={load}
        empty="No analytics yet" emptyHint="Publish content and the Analytics Agent will collect results.">
        {() => (
          <>
            {tab === "overview" && (
              <div className="space-y-4 mb-4">
                <Card>
                  <h3 className="text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: "var(--text-muted)" }}>
                    Views by publish date
                  </h3>
                  <AreaChart
                    data={(posts ?? [])
                      .filter((pp: any) => pp.published_at)
                      .sort((a: any, b: any) => a.published_at.localeCompare(b.published_at))
                      .map((pp: any) => ({
                        x: new Date(pp.published_at).toLocaleDateString(undefined, { month: "short", day: "numeric" }),
                        y: pp.metrics?.views ?? 0,
                      }))}
                    label="views"
                  />
                </Card>
                <Card>
                  <h3 className="text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: "var(--text-muted)" }}>
                    Views by platform
                  </h3>
                  <BarChart items={Object.entries(overview.per_platform).map(([k, v]: any) => ({ label: k, value: v.views }))} format={fmt} />
                </Card>
              </div>
            )}
            {false && overview && (
  <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
                <Metric label="Views" value={fmt(overview.totals.views)} />
                <Metric label="Likes" value={fmt(overview.totals.likes)} />
                <Metric label="Comments" value={fmt(overview.totals.comments)} />
                <Metric label="Shares" value={fmt(overview.totals.shares)} />
                <Metric label="Followers +" value={fmt(overview.totals.followers_gained)} />
                <Metric label="Posts" value={overview.posts_published} />
                <div className="col-span-full">
                  {overview.best_post && (
                    <Card className="mt-2">
                      <p className="text-xs uppercase tracking-wider mb-1" style={{ color: "var(--text-muted)" }}>Best post</p>
                      <p className="text-sm font-medium">{overview.best_post.title || "(untitled)"}</p>
                      <p className="text-[12px]" style={{ color: "var(--text-muted)" }}>
                        {overview.best_post.platform} · {fmt(overview.best_post.views)} views
                      </p>
                    </Card>
                  )}
                </div>
              </div>
            )}

            {tab === "breakdowns" && breakdowns && (
              <div className="space-y-4">
                <BreakdownTable title="By topic" rows={breakdowns.by_topic} />
                <BreakdownTable title="By hook style" rows={breakdowns.by_hook_style} />
                <BreakdownTable title="By duration" rows={breakdowns.by_duration} />
                {breakdowns.posts_with_metrics === 0 && (
                  <p className="text-sm" style={{ color: "var(--text-muted)" }}>
                    No measured posts yet — breakdowns fill in after the MEASURE stage runs.
                  </p>
                )}
              </div>
            )}

            {tab === "patterns" && (
              <Card pad={false}>
                <table className="table">
                  <thead><tr><th>Pattern</th><th>Insight</th><th>Improvement</th><th>Confidence</th><th>Sample</th><th>Status</th></tr></thead>
                  <tbody>
                    {(patterns?.items ?? []).map((p: any) => (
                      <tr key={p.pattern_key}>
                        <td className="font-mono text-[12px]">{p.pattern_key}</td>
                        <td className="max-w-[320px]">{p.description}</td>
                        <td className={`font-mono ${p.improvement_pct >= 0 ? "text-green-600" : "text-red-500"}`}>
                          {p.improvement_pct >= 0 ? "+" : ""}{p.improvement_pct.toFixed(0)}%
                        </td>
                        <td><Badge tone={p.confidence === "high" ? "success" : p.confidence === "medium" ? "info" : "neutral"}>{p.confidence}</Badge></td>
                        <td>n={p.sample_size}</td>
                        <td>{p.active ? <Badge tone="success">active</Badge> : <Badge>inactive</Badge>}</td>
                      </tr>
                    ))}
                    {(patterns?.items ?? []).length === 0 && (
                      <tr><td colSpan={6} className="text-center py-8" style={{ color: "var(--text-muted)" }}>
                        No learned patterns yet — the Learning Agent needs at least 4 measured posts before it extracts patterns (it will not invent insights from thin air).
                      </td></tr>
                    )}
                  </tbody>
                </table>
              </Card>
            )}

            {tab === "platforms" && (
              <Card pad={false}>
                <table className="table">
                  <thead><tr><th>Platform</th><th>Posts</th><th>Views</th><th>Avg views / post</th></tr></thead>
                  <tbody>
                    {Object.entries(overview.per_platform).map(([name, d]: any) => (
                      <tr key={name}>
                        <td className="capitalize font-medium">{name}</td>
                        <td>{d.posts}</td>
                        <td>{fmt(d.views)}</td>
                        <td>{d.posts ? fmt(Math.round(d.views / d.posts)) : "—"}</td>
                      </tr>
                    ))}
                    {Object.keys(overview.per_platform).length === 0 && (
                      <tr><td colSpan={4} className="text-center py-8" style={{ color: "var(--text-muted)" }}>No platform data yet.</td></tr>
                    )}
                  </tbody>
                </table>
              </Card>
            )}

            {tab === "content" && (
              <Card pad={false}>
                <table className="table">
                  <thead><tr><th>#</th><th>Title</th><th>Platform</th><th>Views</th><th>Likes</th><th>Engagement</th></tr></thead>
                  <tbody>
                    {ranked.map((p, i) => {
                      const eng = p.metrics.views ? ((p.metrics.likes + p.metrics.comments + p.metrics.shares) / p.metrics.views) * 100 : null;
                      return (
                        <tr key={p.id}>
                          <td className="font-mono" style={{ color: "var(--text-muted)" }}>{i + 1}</td>
                          <td className="max-w-[280px] truncate">{p.title || "(untitled)"}</td>
                          <td className="capitalize">{p.platform}{p.is_mock && <Badge tone="warning">mock</Badge>}</td>
                          <td>{fmt(p.metrics.views)}</td>
                          <td>{fmt(p.metrics.likes)}</td>
                          <td>{eng != null ? `${eng.toFixed(1)}%` : "—"}</td>
                        </tr>
                      );
                    })}
                    {ranked.length === 0 && (
                      <tr><td colSpan={6} className="text-center py-8" style={{ color: "var(--text-muted)" }}>
                        No measured posts yet — performance appears after the first MEASURE stage.
                      </td></tr>
                    )}
                  </tbody>
                </table>
              </Card>
            )}

            {tab === "costs" && costs && (
              <div className="grid md:grid-cols-2 gap-5">
                <Card>
                  <h3 className="text-xs font-semibold uppercase tracking-wider mb-3" style={{ color: "var(--text-muted)" }}>Efficiency</h3>
                  <dl className="space-y-2 text-sm">
                    <Row k="Total cost" v={`$${costs.total_cost_usd}`} />
                    <Row k="Cost per cycle" v={costs.per_cycle_usd != null ? `$${costs.per_cycle_usd}` : "—"} />
                    <Row k="Cost per video" v={costs.per_video_usd != null ? `$${costs.per_video_usd}` : "—"} />
                    <Row k="Cost per publication" v={costs.per_publication_usd != null ? `$${costs.per_publication_usd}` : "—"} />
                    <Row k="Cost per 1k views" v={costs.cost_per_1000_views_usd != null ? `$${costs.cost_per_1000_views_usd}` : "no view data yet"} />
                    <Row k="Estimated revenue/return" v="Not configured — requires monetization APIs" muted />
                  </dl>
                </Card>
                <Card>
                  <h3 className="text-xs font-semibold uppercase tracking-wider mb-3" style={{ color: "var(--text-muted)" }}>By category</h3>
                  <dl className="space-y-2 text-sm">
                    {Object.entries(costs.by_category).map(([k, v]: any) => (
                      <Row key={k} k={k} v={`$${Number(v).toFixed(4)}`} />
                    ))}
                    {Object.keys(costs.by_category).length === 0 && (
                      <p style={{ color: "var(--text-muted)" }}>No costs recorded yet.</p>
                    )}
                  </dl>
                </Card>
              </div>
            )}
          </>
        )}
      </AsyncSection>
    </div>
  );
}

function BreakdownTable({ title, rows }: { title: string; rows: any[] }) {
  const maxViews = Math.max(1, ...(rows ?? []).map((r) => r.total_views));
  return (
    <Card pad={false}>
      <div className="px-4 pt-3 pb-1">
        <h3 className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>{title}</h3>
      </div>
      <table className="table">
        <thead><tr><th>Key</th><th>Posts</th><th>Total views</th><th>Avg views</th><th>Engagement</th><th style={{ width: "30%" }}></th></tr></thead>
        <tbody>
          {(rows ?? []).map((r) => (
            <tr key={r.key}>
              <td className="max-w-[220px] truncate font-medium">{r.key}</td>
              <td>{r.posts}{r.mock_posts > 0 && <span className="text-[10px] ml-1" style={{ color: "var(--text-muted)" }}>({r.mock_posts} mock)</span>}</td>
              <td>{fmt(r.total_views)}</td>
              <td>{fmt(r.avg_views)}</td>
              <td>{r.engagement_pct.toFixed(1)}%</td>
              <td>
                <div className="h-1.5 rounded-full" style={{ background: "var(--bg-subtle)" }}>
                  <div className="h-full rounded-full" style={{ width: `${(r.total_views / maxViews) * 100}%`, background: "var(--accent)" }} />
                </div>
              </td>
            </tr>
          ))}
          {(rows ?? []).length === 0 && (
            <tr><td colSpan={6} className="text-center py-6" style={{ color: "var(--text-muted)" }}>No data yet.</td></tr>
          )}
        </tbody>
      </table>
    </Card>
  );
}

function Metric({ label, value }: any) {
  return (
    <Card>
      <div className="text-xl font-semibold">{value}</div>
      <div className="text-[11px] mt-0.5" style={{ color: "var(--text-muted)" }}>{label}</div>
    </Card>
  );
}

function Row({ k, v, muted }: any) {
  return (
    <div className="flex justify-between gap-4">
      <dt style={{ color: "var(--text-muted)" }}>{k}</dt>
      <dd className={`font-mono text-right ${muted ? "text-[12px]" : ""}`}>{v}</dd>
    </div>
  );
}

function fmt(n: number | undefined): string {
  if (!n) return "0";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}
