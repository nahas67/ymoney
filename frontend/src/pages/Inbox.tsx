import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Card, PageHeader, Section } from "../components/ui";
import { fmtAgo } from "../lib/format";

/* Inbox lights up only when read-API providers connect — honest by design. */

export default function Inbox() {
  const data = useFetch(() => wsApi.get("/inbox").catch((e: any) => {
    if (e?.status === 404) return { items: [], capability: "Comments/mentions APIs are not connected yet." };
    throw e;
  }), []);
  const items: any[] = (data.data as any)?.items ?? [];

  return (
    <div className="space-y-4 max-w-[720px]">
      <PageHeader title="Inbox" subtitle="Comments, mentions and review requests — live when read providers connect." />
      <Section data={items} loading={data.loading} error={data.error} onRetry={data.reload}
        empty="Inbox is quiet"
        emptyHint={(data.data as any)?.capability ?? "Connect a platform with comment-read access to see mentions here. Approval-hold items appear in Studio → Approval hold."}>
        {(list) => (
          <div className="space-y-2">
            {list.map((m: any, i: number) => (
              <Card key={m.id ?? i} style={{ padding: 13 }}>
                <div className="text-[13.5px]">{m.message ?? m.text}</div>
                <div className="text-[11.5px] font-mono mt-1" style={{ color: "var(--text-faint)" }}>{m.platform ?? ""} · {fmtAgo(m.created_at)}</div>
              </Card>
            ))}
          </div>
        )}
      </Section>
    </div>
  );
}
