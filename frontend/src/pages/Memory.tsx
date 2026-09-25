import { useEffect, useRef, useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, ConfirmButton, PageHeader, SearchInput, Section, Tabs, toast } from "../components/ui";

const TYPES = ["all", "short_term", "episodic", "semantic", "strategic", "preference"];

export default function Memory() {
  const [type, setType] = useState("all");
  const [q, setQ] = useState("");
  const [semantic, setSemantic] = useState(true);
  const [content, setContent] = useState("");
  const [scope, setScope] = useState("");
  const mem = useFetch(() => {
    const p = new URLSearchParams();
    if (type !== "all") p.set("type", type);
    if (q) p.set("q", q);
    if (semantic && q) p.set("semantic", "true");
    return wsApi.get(`/memory?${p}`);
  }, [type]);

  async function search() {
    mem.reload();
  }

  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return; // skip mount (useFetch already loads)
    }
    const t = setTimeout(() => search(), 450);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, semantic, type]);

  async function store() {
    if (!content.trim()) return;
    try {
      await wsApi.post("/memory/store", { content, type: "semantic", scope });
      setContent("");
      setScope("");
      mem.reload();
      toast("Memory stored", "success");
    } catch (e: any) {
      toast(e.message, "error", "Store failed");
    }
  }

  async function remove(id: string) {
    try {
      await wsApi.del(`/memory/${id}`);
      toast("Memory deleted", "warning");
      mem.reload();
    } catch (e: any) {
      toast(e.message, "error", "Delete failed");
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader title="Memory" subtitle="Persistent workspace knowledge — targeted retrieval only, never a dump. Learning patterns land here as semantic memories."
        actions={
          <button className={`chip${semantic ? " on" : ""}`} onClick={() => { setSemantic(!semantic); }} title="Rank by token-overlap relevance instead of keyword order">
            {semantic ? "✦ Semantic rank" : "○ Keyword order"}
          </button>
        } />
      <div className="flex flex-wrap gap-3 items-center">
        <div className="flex-1 min-w-[220px]"><SearchInput value={q} onChange={setQ} placeholder="Search memories… (Enter)" /></div>
      </div>
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

      <Section data={(mem.data as any)?.items} loading={mem.loading} error={mem.error} onRetry={search}
        empty="No memories" emptyHint="Store style guidance above, or let the Learning Agent build semantic memories from performance.">
        {(list) => (
          <div className="space-y-2">
            {list.map((m: any) => (
              <Card key={m.id} className="card-hover" style={{ padding: 13 }}>
                <div className="flex gap-2 items-center flex-wrap mb-1">
                  <Badge tone="info">{m.type}</Badge>
                  {m.scope && <Badge tone="muted">{m.scope}</Badge>}
                  {m.semantic_score != null && m.semantic_score > 0 && (
                    <Badge tone="success">≈{(m.semantic_score * 100).toFixed(0)}% match</Badge>
                  )}
                  <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>conf {m.confidence} · imp {m.importance} · {m.source}</span>
                  <span className="ml-auto"><ConfirmButton onConfirm={() => remove(m.id)} confirmText="Delete?" className="btn-ghost !text-[11px] !py-0.5">Delete</ConfirmButton></span>
                </div>
                <div className="text-[13.5px]">{m.content}</div>
                {(m.matched_terms ?? []).length > 0 && (
                  <div className="flex gap-1.5 flex-wrap mt-1.5">
                    {(m.matched_terms ?? []).map((t: string) => (
                      <span key={t} className="font-mono text-[10.5px] px-1.5 py-0.5 rounded-md" style={{ background: "var(--accent-dim)", color: "var(--accent-bright)" }}>{t}</span>
                    ))}
                  </div>
                )}
              </Card>
            ))}
          </div>
        )}
      </Section>
    </div>
  );
}
