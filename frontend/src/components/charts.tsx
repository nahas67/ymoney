/* Tiny dependency-free SVG charts for ops dashboards. */

export function Bars({ data, height = 120 }: { data: { label: string; value: number }[]; height?: number }) {
  const max = Math.max(1, ...data.map((d) => d.value));
  const w = 340;
  const bw = data.length ? w / data.length : w;
  return (
    <svg viewBox={`0 0 ${w} ${height}`} style={{ width: "100%", height }} role="img">
      {data.map((d, i) => {
        const h = Math.max(2, (d.value / max) * (height - 22));
        return (
          <g key={i}>
            <rect x={i * bw + 3} y={height - 18 - h} width={Math.max(2, bw - 6)} height={h} rx={2.5}
              fill={d.value > 0 ? "var(--accent)" : "var(--border-strong)"} opacity={0.85}>
              <title>{`${d.label}: ${d.value}`}</title>
            </rect>
            {bw > 34 && (
              <text x={i * bw + bw / 2} y={height - 5} fontSize={8.5} textAnchor="middle" fill="var(--text-faint)">{d.label}</text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

export function Spark({ data, height = 44, stroke = "var(--accent)" }: { data: number[]; height?: number; stroke?: string }) {
  const w = 220;
  const max = Math.max(1, ...data);
  const min = Math.min(0, ...data);
  const pts = data.map((v, i) => {
    const x = data.length > 1 ? (i / (data.length - 1)) * w : w / 2;
    const y = height - 4 - ((v - min) / Math.max(1e-9, max - min)) * (height - 10);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return (
    <svg viewBox={`0 0 ${w} ${height}`} style={{ width: "100%", height }} role="img">
      <polyline points={pts.join(" ")} fill="none" stroke={stroke} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

export function Donut({ parts, size = 110 }: { parts: { label: string; value: number; color: string }[]; size?: number }) {
  const total = Math.max(1e-9, parts.reduce((a, p) => a + p.value, 0));
  const r = 44;
  const c = 2 * Math.PI * r;
  let acc = 0;
  return (
    <div className="flex items-center gap-4">
      <svg width={size} height={size} viewBox="0 0 110 110" role="img">
        <circle cx={55} cy={55} r={r} fill="none" stroke="var(--bg-subtle)" strokeWidth={13} />
        {parts.map((p, i) => {
          const frac = p.value / total;
          const el = (
            <circle key={i} cx={55} cy={55} r={r} fill="none" stroke={p.color} strokeWidth={13}
              strokeDasharray={`${(frac * c).toFixed(1)} ${c.toFixed(1)}`}
              strokeDashoffset={(-acc * c).toFixed(1)} transform="rotate(-90 55 55)" strokeLinecap="butt">
              <title>{`${p.label}: ${p.value}`}</title>
            </circle>
          );
          acc += frac;
          return el;
        })}
      </svg>
      <div className="space-y-1 text-[12px]">
        {parts.map((p, i) => (
          <div key={i} className="flex items-center gap-2">
            <span className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: p.color }} />
            <span style={{ color: "var(--text-muted)" }}>{p.label}</span>
            <b className="font-mono">{p.value}</b>
          </div>
        ))}
      </div>
    </div>
  );
}
