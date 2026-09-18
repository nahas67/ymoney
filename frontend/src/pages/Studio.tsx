import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, PageHeader, Section, Tabs, statusTone } from "../components/ui";

export default function Studio() {
  const [tab, setTab] = useState("all");
  const [search, setSearch] = useState("");
  const lib = useFetch(() => {
    const p = new URLSearchParams({ limit: "100" });
    if (tab !== "all") p.set("status", tab);
    if (search) p.set("search", search);
    return wsApi.get(`/content?${p}`);
  }, [tab]);
  const nav = useNavigate();
  const [q, setQ] = useState("");

  function doSearch() {
    setSearch(q);
    lib.reload();
  }

  return (
    <div className="space-y-4">
      <PageHeader title="Content Studio" subtitle={`${(lib.data as any)?.total ?? 0} items — every artifact the system produced, with QC and variants.`}
        actions={<input className="input !w-56" placeholder="Search topics… (Enter)" value={q}
          onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && doSearch()} aria-label="Search content" />} />
      <Tabs tabs={[
        { key: "all", label: "All" }, { key: "QC", label: "In QC" }, { key: "APPROVED", label: "Approval hold" },
        { key: "PUBLISHED", label: "Published" }, { key: "LEARNED", label: "Learned" }, { key: "FAILED", label: "Failed" },
      ]} active={tab} onChange={setTab} />
      <Section data={(lib.data as any)?.items} loading={lib.loading} error={lib.error} onRetry={lib.reload}
        empty="No content matches" emptyHint="Adjust filters or run the autopilot to produce content.">
        {(list) => (
          <Card pad={false} className="overflow-x-auto">
            <table className="table">
              <thead><tr><th>Topic</th><th>Status</th><th>QC</th><th>Engine</th><th>Variants</th><th></th></tr></thead>
              <tbody>
                {list.map((c: any) => (
                  <tr key={c.id} className="cursor-pointer" onClick={() => nav(`/studio/${c.id}`)}>
                    <td className="max-w-[340px]"><span className="line-clamp-1 font-medium">{c.topic}</span></td>
                    <td><Badge tone={statusTone(c.status)}>{c.status}</Badge></td>
                    <td>{c.video?.quality != null
                      ? <Badge tone={c.video.quality_passed ? "success" : "error"}>{c.video.quality.toFixed(0)}</Badge>
                      : <span style={{ color: "var(--text-faint)" }}>—</span>}</td>
                    <td className="text-[12px] font-mono" style={{ color: "var(--text-muted)" }}>{c.video?.engine ?? "—"}</td>
                    <td className="font-mono">{c.variants_count}</td>
                    <td className="text-right"><span style={{ color: "var(--text-faint)" }}>→</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        )}
      </Section>
    </div>
  );
}
