import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, PageHeader, Section } from "../components/ui";
import { fmtAgo } from "../lib/format";

/* Learned patterns + decision feed + cost intelligence. */

export default function Intelligence() {
  const pat = useFetch(() => wsApi.get("/analytics/patterns"), []);
  const decision = useFetch(() => wsApi.get("/decision"), []);
  const costIntel = useFetch(() => wsApi.get("/costs/intelligence"), []);
  const ci: any = costIntel.data;

  return (
    <div className="space-y-4">
      <PageHeader title="Intelligence" subtitle="What the system learned, what it decided, and what it costs." />

      <div className="grid lg:grid-cols-2 gap-4">
        <Card>
          <b className="text-[14px]">Current decision context</b>
          {(decision.data as any) ? (
            <pre className="text-[12px] font-mono whitespace-pre-wrap mt-2 max-h-[300px] overflow-y-auto">{JSON.stringify(decision.data, null, 2)}</pre>
          ) : decision.loading ? (
            <div className="text-[13px] mt-2" style={{ color: "var(--text-faint)" }}>Loading…</div>
          ) : (
            <div className="text-[13px] mt-2" style={{ color: "var(--text-faint)" }}>No decision context available.</div>
          )}
        </Card>
        <Card>
          <b className="text-[14px]">Cost intelligence</b>
          {ci ? (
            <pre className="text-[12px] font-mono whitespace-pre-wrap mt-2 max-h-[300px] overflow-y-auto">{JSON.stringify(ci, null, 2)}</pre>
          ) : costIntel.loading ? (
            <div className="text-[13px] mt-2" style={{ color: "var(--text-faint)" }}>Loading…</div>
          ) : (
            <div className="text-[13px] mt-2" style={{ color: "var(--text-faint)" }}>No cost data yet.</div>
          )}
        </Card>
      </div>

      <Card>
        <b className="text-[14px]">Learned patterns</b>
        <Section data={(pat.data as any)?.items} loading={pat.loading} error={pat.error} onRetry={pat.reload}
          empty="No patterns yet" emptyHint="Patterns form after 4+ measured posts, with confidence from sample size.">
          {(list) => (
            <div className="mt-2 space-y-2">
              {list.map((p: any) => (
                <div key={p.pattern_key} className="flex items-center gap-2 text-[13px] flex-wrap">
                  <code className="text-[12px]">{p.pattern_key}</code>
                  <span className="flex-1" style={{ color: "var(--text-muted)" }}>{p.description}</span>
                  <Badge tone={p.active ? "success" : "muted"}>{p.active ? "active" : "inactive"}</Badge>
                  <span className="font-mono text-[12px]">{p.improvement_pct}% · {p.confidence} · n={p.sample_size} · {fmtAgo(p.updated_at)}</span>
                </div>
              ))}
            </div>
          )}
        </Section>
      </Card>
    </div>
  );
}
