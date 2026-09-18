import { useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, PageHeader, Section, Tabs } from "../components/ui";

const TYPES = ["all", "short_term", "episodic", "semantic", "strategic", "preference"];

export default function Memory() {
  const [type, setType] = useState("all");
  const [q, setQ] = useState("");
  const [content, setContent] = useState("");
  const [scope, setScope] = useState("");
  const mem = useFetch(() => {
    const p = new URLSearchParams();
    if (type !== "all") p.set("type", type);
    if (q) p.set("q", q);
    return wsApi.get(`/memory?${p}`);
  }, [type]);

  async function search() {
    mem.reload();
  }

  async function store() {
    if (!content.trim()) return;
    await wsApi.post("/memory/store", { content, type: "semantic", scope });
    setContent("");
    setScope("");
    mem.reload();
  }

  async function remove(id: string) {
    if (!confirm("Delete this memory?")) return;
    await wsApi.del(`/memory/${id}`);
    mem.reload();
  }

  return (
    <div className="space-y-4">
      <PageHeader title="Memory" subtitle="Persistent workspace knowledge — targeted retrieval only, never a dump. Learning patterns land here as semantic memories."
        actions={<input className="input !w-52" placeholder="Keyword search…" value={q}
          onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search()} />} />
      <Tabs tabs={TYPES.map((t) => ({ key: t, label: t.replace(/_/g, " ") }))} active={type} onChange={setType} />

      <Card>
        <b className="text-[13.5px]">Store a memory</b>
        <div className="grid md:grid-cols-[1fr_220px_auto] gap-2 mt-2">
          <input className="input" placeholder="e.g. Faceless documentary style only — no talking heads" value={content} onChange={(e) => setContent(e.target.value)} />
          <input className="input" placeholder="scope (optional)" value={scope} onChange={(e) => setScope(e.target.value)} />
          <button className="btn-primary !text-xs" onClick={store}>Store</button>
        </div>
        <p className="text-[11.5px] mt-1.5" style={{ color: "var(--text-faint)" }}>Preference + strategic memories steer the Strategist and Scriptwriter; semantic memories ground Research.</p>
      </Card>

      <Section data={(mem.data as any)?.items} loading={mem.loading} error={mem.error} onRetry={mem.reload}
        empty="No memories" emptyHint="Store style guidance above, or let the Learning Agent build semantic memories from performance.">
        {(list) => (
          <div className="space-y-2">
            {list.map((m: any) => (
              <Card key={m.id} style={{ padding: 13 }}>
                <div className="flex gap-2 items-center flex-wrap mb-1">
                  <Badge tone="info">{m.type}</Badge>
                  {m.scope && <Badge tone="muted">{m.scope}</Badge>}
                  <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>conf {m.confidence} · imp {m.importance} · {m.source}</span>
                  <button className="btn-ghost !text-[11px] !py-0.5 ml-auto" onClick={() => remove(m.id)}>Delete</button>
                </div>
                <div className="text-[13.5px]">{m.content}</div>
              </Card>
            ))}
          </div>
        )}
      </Section>
    </div>
  );
}
