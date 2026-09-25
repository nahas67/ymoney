import { useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, PageHeader, Progress, Section, Tabs } from "../components/ui";
import { Bars } from "../components/charts";
import { fmtCompact, fmtUSD } from "../lib/format";

export default function Analytics() {
  const [tab, setTab] = useState<"overview" | "breakdowns" | "patterns">("overview");
  const over = useFetch(() => wsApi.get("/analytics/overview"), [tab]);
  const down = useFetch(() => wsApi.get("/analytics/breakdowns"), [tab]);
  const pat = useFetch(() => wsApi.get("/analytics/patterns"), [tab]);

  const o: any = over.data;
  const d: any = down.data;
  const isMock = o?.mock_analytics ?? d?.mock_analytics;

  return (
    <div className="space-y-4">
      <PageHeader title="Analytics" subtitle="Real platform metrics — never simulated without a label."
        actions={isMock ? <Badge tone="warning">MOCK ANALYTICS</Badge> : <Badge tone="success">REAL DATA</Badge>} />
      <Tabs tabs={[{ key: "overview", label: "Overview" }, { key: "breakdowns", label: "Breakdowns" }, { key: "patterns", label: "Patterns" }]}
        active={tab} onChange={setTab} />

      {tab === "overview" && (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            {(["views", "likes", "comments", "shares"] as const).map((k) => (
              <Card key={k}>
                <div className="panel-label mb-1">Total {k}</div>
                <div className="text-[22px] font-semibold">{fmtCompact(o?.totals?.[k])}</div>
              </Card>
            ))}
          </div>
          <div className="grid lg:grid-cols-2 gap-4">
            <Card>
              <b className="text-[14px]">Views per platform</b>
              <div className="mt-2">
                <Bars data={Object.entries(o?.per_platform ?? {}).map(([label, v]: any) => ({ label, value: v.views ?? 0 }))} />
              </div>
              <div className="text-[12px] mt-2 font-mono" style={{ color: "var(--text-muted)" }}>
                {o?.posts_published ?? 0} posts · {o?.content_items ?? 0} items · {fmtUSD(o?.cost_total_usd)} total cost
              </div>
            </Card>
            <Card>
              <b className="text-[14px]">Best post</b>
              {o?.best_post ? (
                <div className="mt-2">
                  <Badge>{o.best_post.platform}</Badge>
                  <div className="text-[14px] font-medium mt-1.5">{o.best_post.title}</div>
                  <div className="text-[20px] font-semibold mt-1">{fmtCompact(o.best_post.views)} <span className="text-[12px] font-normal" style={{ color: "var(--text-muted)" }}>views</span></div>
                </div>
              ) : <div className="text-[13px] mt-2" style={{ color: "var(--text-faint)" }}>No measured posts yet.</div>}
            </Card>
          </div>
        </>
      )}

      {tab === "breakdowns" && (
        <div className="grid lg:grid-cols-3 gap-4">
          {[["by_topic", "By topic"], ["by_hook_style", "By hook style"], ["by_duration", "By duration"]].map(([key, label]) => (
            <Card key={key}>
              <b className="text-[14px]">{label}</b>
              <div className="mt-2 space-y-2">
                {((d?.[key] ?? []) as any[]).slice(0, 8).map((r: any) => (
                  <div key={r.key} className="text-[12.5px]">
                    <div className="flex justify-between gap-2">
                      <span className="truncate">{r.key}</span>
                      <span className="font-mono whitespace-nowrap">{fmtCompact(r.avg_views)} avg · {r.engagement_pct}%</span>
                    </div>
                    <div className="mt-1"><Progress value={r.engagement_pct} /></div>
                    {r.mock_posts > 0 && <span className="font-mono text-[10.5px]" style={{ color: "var(--warn)" }}>{r.mock_posts} mock</span>}
                  </div>
                ))}
                {!((d?.[key] ?? []).length) && <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>No data.</div>}
              </div>
            </Card>
          ))}
        </div>
      )}

      {tab === "patterns" && (
        <Section data={(pat.data as any)?.items} loading={pat.loading} error={pat.error} onRetry={pat.reload}
          empty="No patterns yet" emptyHint="The Learning Agent needs 4+ measured posts before it trusts a pattern.">
          {(list) => (
            <div className="grid md:grid-cols-2 gap-3">
              {list.map((p: any) => (
                <Card key={p.pattern_key}>
                  <div className="flex justify-between items-center gap-2">
                    <code className="text-[12px]">{p.pattern_key}</code>
                    <Badge tone={p.confidence === "high" ? "success" : p.confidence === "medium" ? "info" : "muted"}>
                      {p.confidence} · n={p.sample_size}
                    </Badge>
                  </div>
                  <div className="text-[13px] mt-1.5">{p.description}</div>
                  <div className="font-mono text-[13px] mt-1" style={{ color: Number(p.improvement_pct) >= 0 ? "var(--accent)" : "var(--danger)" }}>
                    {Number(p.improvement_pct) >= 0 ? "+" : ""}{Number(p.improvement_pct).toFixed(1)}% vs channel median
                  </div>
                </Card>
              ))}
            </div>
          )}
        </Section>
      )}
    </div>
  );
}
