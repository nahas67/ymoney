import { useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, ConfirmButton, CopyButton, PageHeader, statusTone, toast } from "../components/ui";
import { fmtAgo } from "../lib/format";

export default function Integrations() {
  const st = useFetch(() => wsApi.get("/telegram/status"), []);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState("");
  const data: any = st.data;
  const links: any[] = data?.links ?? [];

  async function pair() {
    setBusy("pair");
    try {
      const r = await wsApi.post("/telegram/pairing-code", {});
      setCode(r.code ?? JSON.stringify(r));
    } catch (e: any) {
      toast(e.message, "error", "Pairing failed");
    } finally {
      setBusy("");
    }
  }

  async function toggle(id: string) {
    try {
      await wsApi.post(`/telegram/links/${id}/toggle`, {});
      st.reload();
    } catch (e: any) {
      toast(e.message, "error", "Toggle failed");
    }
  }

  async function unlink(id: string) {
    try {
      await wsApi.del(`/telegram/links/${id}`);
      toast("Chat unlinked", "warning");
      st.reload();
    } catch (e: any) {
      toast(e.message, "error", "Unlink failed");
    }
  }

  async function test() {
    setBusy("test");
    try {
      await wsApi.post("/telegram/test", {});
      toast("Test message sent to linked chats", "success");
    } catch (e: any) {
      toast(e.message, "error", "Test failed");
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="space-y-4 max-w-[680px]">
      <PageHeader title="Telegram remote" subtitle="Run the studio from your phone: /status /run /stop /pause /cycle /cost, plus push alerts."
        actions={<Badge tone={data?.bot_configured ? "success" : "warning"}>{data?.bot_configured ? `bot configured (${data?.token_source ?? "?"})` : "needs bot token"}</Badge>} />
      <Card>
        <b className="text-[14px]">1 · Pair a chat</b>
        <p className="text-[13px] mt-1" style={{ color: "var(--text-muted)" }}>
          Create a bot with @BotFather, paste the token in Settings → Connections, then generate a code and send <code>/start &lt;code&gt;</code> to your bot.
        </p>
        <div className="flex gap-2 mt-3 items-center flex-wrap">
          <button className="btn-primary !text-xs" disabled={busy === "pair"} onClick={pair}>Generate pairing code</button>
          {code && (
            <>
              <code className="text-[15px] font-mono px-3 py-1.5 rounded-lg" style={{ background: "var(--bg-inset)", border: "var(--seam)" }}>{code}</code>
              <CopyButton text={code} />
            </>
          )}
        </div>
      </Card>
      <Card>
        <div className="flex items-center gap-2 mb-2">
          <b className="text-[14px]">2 · Linked chats ({links.length})</b>
          <button className="btn-outline !text-xs ml-auto" disabled={busy === "test"} onClick={test}>Send test message</button>
        </div>
        {links.map((l: any) => (
          <div key={l.id} className="flex items-center gap-2 py-2 text-[13px]" style={{ borderBottom: "var(--seam)" }}>
            <span className="font-medium">{l.chat_title || l.chat_id}</span>
            <Badge tone={statusTone(l.active ? "connected" : "paused")}>{l.active ? "active" : "paused"}</Badge>
            <span className="font-mono text-[11.5px]" style={{ color: "var(--text-faint)" }}>linked {fmtAgo(l.linked_at)}</span>
            <span className="ml-auto flex gap-1.5">
              <button className="btn-ghost !text-[11px] !py-0.5" onClick={() => toggle(l.id)}>{l.active ? "Pause" : "Enable"}</button>
              <ConfirmButton onConfirm={() => unlink(l.id)} confirmText="Unlink?" className="btn-ghost !text-[11px] !py-0.5">Unlink</ConfirmButton>
            </span>
          </div>
        ))}
        {!links.length && <div className="text-[13px]" style={{ color: "var(--text-faint)" }}>No chats linked yet.</div>}
      </Card>
    </div>
  );
}
