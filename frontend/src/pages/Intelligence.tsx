import { useCallback, useEffect, useState } from "react";
import { wsApi } from "../lib/api";
import { AsyncSection, Badge, Card, PageHeader } from "../components/ui";

/**
 * Intelligence Center — the "why" layer.
 * Explains what YMONEY learned, why it decides things, and recommends strategy.
 */
export default function Intelligence() {
  const [patterns, setPatterns] = useState<any[]>([]);
  const [decision, setDecision] = useState<any>(null);
  const [cycles, setCycles] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [p, d, c] = await Promise.all([
        wsApi.get("/analytics/patterns"),
        wsApi.get("/decision"),
        wsApi.get("/cycles?limit=15"),
      ]);
      setPatterns(p.items ?? []);
      setDecision(d);
      setCycles(c.items ?? []);
    } catch (e: any) { setError(e.message); } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  // Build recommendations only from real learned data
  const recommendations = patterns
    .filter((p) => p.confidence !== "low" && p.improvement_pct > 5)
    .map((p) => ({
      text: `Lean into: ${p.description} — observed +${p.improvement_pct.toFixed(0)}% (n=${p.sample_size}, ${p.confidence} confidence).`,
    }));

  const decisions = cycles
    .map((c) => ((c.summary_json ?? {}).select ?? {}))
    .filter((s) => s.decision)
    .slice(0, 8);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Intelligence"
        subtitle="Why content succeeds, why YMONEY decides what it decides, and what it recommends next."
      />

      <section>
        <h2 className="text-xs font-semibold uppercase tracking-wider mb-2.5" style={{ color: "var(--text-muted)" }}>
          Learned patterns — these change future decisions
        </h2>
        <AsyncSection data={patterns} loading={loading} error={error} onRetry={load}
          empty="No patterns learned yet" emptyHint="The Learning Agent needs ≥4 measured posts before extracting patterns. Correlation is never presented as causation.">
          {(list) => (
            <div className="grid md:grid-cols-2 gap-4">
              {(list as any[]).map((p) => (
                <Card key={p.pattern_key}>
                  <div className="flex items-start justify-between gap-3 mb-2">
                    <h3 className="font-medium text-sm">{p.description}</h3>
                    <Badge tone={p.confidence === "high" ? "success" : p.confidence === "medium" ? "info" : "neutral"}>
                      {p.confidence.toUpperCase()}
                    </Badge>
                  </div>
                  <p className={`text-lg font-bold ${p.improvement_pct >= 0 ? "text-emerald-500" : "text-red-500"}`}>
                    {p.improvement_pct >= 0 ? "+" : ""}{p.improvement_pct.toFixed(1)}% observed
                  </p>
                  <dl className="mt-2 space-y-0.5 text-[12px]" style={{ color: "var(--text-muted)" }}>
                    <div>sample size: {p.sample_size}</div>
                    <div>baseline: channel median views</div>
                    <div>updated: {new Date(p.updated_at).toLocaleDateString()}</div>
                  </dl>
                </Card>
              ))}
            </div>
          )}
        </AsyncSection>
      </section>

      <section className="grid lg:grid-cols-2 gap-5">
        <Card>
          <h2 className="text-xs font-semibold uppercase tracking-wider mb-2.5" style={{ color: "var(--text-muted)" }}>
            Current recommendation (next best action)
          </h2>
          {decision && decision.action !== "WAIT" ? (
            <div className="space-y-2">
              <Badge tone={decision.action === "PRODUCE" ? "success" : decision.action === "HUMAN_REVIEW" ? "warning" : "info"}>
                {String(decision.action).replace(/_/g, " ")}
              </Badge>
              {decision.topic && <p className="text-sm font-medium">{decision.topic}</p>}
              <ul className="list-disc pl-4 space-y-0.5">
                {(decision.evidence ?? []).map((e: string, i: number) => (
                  <li key={i} className="text-[12px]" style={{ color: "var(--text-muted)" }}>{e}</li>
                ))}
              </ul>
            </div>
          ) : (
            <p className="text-[13px]" style={{ color: "var(--text-muted)" }}>Waiting for candidates.</p>
          )}
          {recommendations.length > 0 && (
            <>
              <h3 className="text-xs font-semibold uppercase tracking-wider mt-4 mb-1.5" style={{ color: "var(--text-muted)" }}>
                Strategy suggestions
              </h3>
              <ul className="space-y-1.5">
                {recommendations.map((r, i) => (
                  <li key={i} className="text-[13px] flex gap-2"><span className="text-emerald-500">→</span>{r.text}</li>
                ))}
              </ul>
            </>
          )}
        </Card>

        <Card>
          <h2 className="text-xs font-semibold uppercase tracking-wider mb-2.5" style={{ color: "var(--text-muted)" }}>
            Recent autonomous decisions
          </h2>
          <div className="space-y-2.5">
            {decisions.map((d: any, i: number) => (
              <div key={i} className="flex items-start justify-between gap-3 text-[13px]">
                <div className="min-w-0">
                  <span className="line-clamp-1">{d.why?.topic ?? "(cycle-level decision)"}</span>
                  {d.why?.reasons?.[0] && (
                    <span className="block text-[11px]" style={{ color: "var(--text-muted)" }}>{d.why.reasons[0]}</span>
                  )}
                </div>
                <Badge tone={d.decision === "PRODUCE" ? "success" : d.decision === "SKIP" ? "neutral" : "info"}>
                  {d.decision}
                </Badge>
              </div>
            ))}
            {decisions.length === 0 && (
              <p className="text-[13px]" style={{ color: "var(--text-muted)" }}>No cycles yet.</p>
            )}
          </div>
        </Card>
      </section>

      <p className="text-xs" style={{ color: "var(--text-muted)" }}>
        Confidence labels follow strict sample-size rules: LOW &lt;10, MEDIUM &lt;30,
        HIGH ≥30 supporting posts. Patterns update via exponential moving average so old
        evidence fades as new results arrive.
      </p>
    </div>
  );
}
