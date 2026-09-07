import { useCallback, useEffect, useState } from "react";
import { wsApi } from "../lib/api";
import { Badge, Card, PageHeader, StatusDot, useToast } from "../components/ui";

type Link = {
  id: string;
  chat_id: string;
  chat_title: string;
  active: boolean;
  linked_at: string | null;
};

type TgStatus = {
  bot_configured: boolean;
  token_source: string;
  links: Link[];
  linked: boolean;
};

const COMMANDS = [
  ["/status", "Autopilot state, current stage, cycle count, today's spend"],
  ["/run", "Start the autopilot"],
  ["/stop", "Stop safely (running steps finish)"],
  ["/pause", "Pause before the next stage"],
  ["/resume", "Resume a paused run"],
  ["/cycle", "Run exactly one FIND→…→LEARN cycle"],
  ["/cost", "Today's spend vs daily budget"],
  ["/help", "List all commands"],
] as const;

export default function Integrations() {
  const [st, setSt] = useState<TgStatus | null>(null);
  const [code, setCode] = useState<{ code: string; expires_in_seconds: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const { push } = useToast();

  const load = useCallback(() => {
    wsApi.get("/telegram/status").then(setSt).catch(() => {});
  }, []);
  useEffect(() => { load(); }, [load]);

  async function genCode() {
    setBusy(true);
    try {
      const out = await wsApi.post("/telegram/pairing-code");
      setCode(out);
    } catch (e: any) {
      push("error", "Could not generate code: " + e.message);
    } finally { setBusy(false); }
  }

  async function toggle(id: string) {
    try { await wsApi.post(`/telegram/links/${id}/toggle`); load(); }
    catch (e: any) { push("error", "Toggle failed: " + e.message); }
  }

  async function unlink(id: string) {
    try { await wsApi.del(`/telegram/links/${id}`); load(); push("success", "Chat unlinked"); }
    catch (e: any) { push("error", "Unlink failed: " + e.message); }
  }

  async function testSend() {
    setBusy(true);
    try {
      const r = await wsApi.post("/telegram/test", {});
      push("success", `Test sent to ${r.sent} chat${r.sent === 1 ? "" : "s"}`);
    } catch (e: any) {
      push("error", "Test failed: " + e.message);
    } finally { setBusy(false); }
  }

  const activeLinks = (st?.links ?? []).filter((l) => l.active);

  return (
    <div className="space-y-5 max-w-3xl">
      <PageHeader
        title="Integrations"
        subtitle="Connect YMONEY to the outside world — starting with Telegram remote control."
      />

      {/* ------------------------------ Telegram ------------------------------ */}
      <Card>
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="flex items-start gap-3">
            <div className="grid place-items-center w-10 h-10 rounded-lg text-lg shrink-0"
                 style={{ background: "var(--accent-dim)", color: "var(--accent)" }} aria-hidden>✈</div>
            <div>
              <h2 className="text-base font-semibold flex items-center gap-2">
                Telegram Remote Control
                {st?.linked ? (
                  <Badge tone="success"><StatusDot tone="success" /> Connected</Badge>
                ) : st?.bot_configured ? (
                  <Badge tone="warning">Bot ready — pair a chat</Badge>
                ) : (
                  <Badge tone="neutral">Not configured</Badge>
                )}
              </h2>
              <p className="text-[13px] mt-1" style={{ color: "var(--text-muted)" }}>
                Control the whole pipeline from your phone: start/stop the autopilot, check status and
                costs, and receive alerts when cycles complete, quality reviews need attention, or the
                budget is at risk.
              </p>
            </div>
          </div>
          {st?.linked && (
            <button className="btn-outline text-xs" onClick={testSend} disabled={busy}>
              Send test message
            </button>
          )}
        </div>

        {/* Step 1: bot token */}
        <div className="mt-5 pt-4 border-t space-y-3" style={{ borderColor: "var(--border)" }}>
          <h3 className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>
            Step 1 · Bot token
          </h3>
          <ol className="text-[13px] space-y-1.5 list-decimal pl-5" style={{ color: "var(--text-muted)" }}>
            <li>Open <b>@BotFather</b> in Telegram and send <code className="font-mono">/newbot</code></li>
            <li>Choose a name and username — BotFather replies with a token like <code className="font-mono">123456:ABC-DEF…</code></li>
            <li>Paste it in <a href="/settings" className="underline" style={{ color: "var(--accent)" }}>Settings → Connections</a> under <i>Telegram bot token</i></li>
          </ol>
          <p className="text-[12px] font-mono" style={{ color: st?.bot_configured ? "var(--accent)" : "var(--text-faint)" }}>
            {st?.bot_configured
              ? `● token configured (source: ${st.token_source})`
              : "○ no token yet — steps 2-3 will work after it's set"}
          </p>
        </div>

        {/* Step 2: pairing */}
        <div className="mt-5 pt-4 border-t space-y-3" style={{ borderColor: "var(--border)" }}>
          <h3 className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>
            Step 2 · Pair a chat
          </h3>
          {!code ? (
            <div>
              <button className="btn-primary" onClick={genCode} disabled={busy}>Generate pairing code</button>
              <p className="text-[12px] mt-2" style={{ color: "var(--text-muted)" }}>
                One-time code, valid for 15 minutes. Any chat that sends it becomes a remote control for this workspace.
              </p>
            </div>
          ) : (
            <div className="rounded-lg border p-4 space-y-2" style={{ borderColor: "var(--accent)", background: "var(--accent-dim)" }}>
              <p className="text-[12px] font-mono uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>
                Send this command to your bot in Telegram:
              </p>
              <div className="flex items-center gap-3 flex-wrap">
                <code className="font-mono text-lg font-bold tracking-widest select-all" style={{ color: "var(--accent)" }}>
                  /start {code.code}
                </code>
                <button className="btn-outline !py-1 !px-2 text-xs" onClick={() => navigator.clipboard?.writeText(`/start ${code.code}`)}>
                  Copy
                </button>
              </div>
              <p className="text-[11px]" style={{ color: "var(--text-faint)" }}>
                Expires in {Math.max(0, Math.round(code.expires_in_seconds / 60))} min · one-time use
              </p>
            </div>
          )}
        </div>

        {/* Step 3: linked chats */}
        <div className="mt-5 pt-4 border-t" style={{ borderColor: "var(--border)" }}>
          <h3 className="text-xs font-semibold uppercase tracking-wider mb-3" style={{ color: "var(--text-muted)" }}>
            Step 3 · Linked chats ({activeLinks.length} active)
          </h3>
          {(st?.links ?? []).length === 0 ? (
            <p className="text-[13px]" style={{ color: "var(--text-muted)" }}>
              No chats paired yet. Once paired, this workspace's alerts flow to Telegram and commands are accepted.
            </p>
          ) : (
            <ul className="space-y-2">
              {st!.links.map((l) => (
                <li key={l.id} className="flex items-center justify-between gap-3 rounded-lg border px-3 py-2.5"
                    style={{ borderColor: "var(--border)" }}>
                  <div className="min-w-0">
                    <p className="text-sm font-medium truncate">
                      {l.chat_title || `Chat ${l.chat_id}`}
                      {!l.active && <span className="ml-2" style={{ color: "var(--warn)" }}>· disabled</span>}
                    </p>
                    <p className="text-[11px] font-mono" style={{ color: "var(--text-faint)" }}>
                      id {l.chat_id} {l.linked_at ? `· linked ${new Date(l.linked_at).toLocaleDateString()}` : ""}
                    </p>
                  </div>
                  <div className="flex gap-2 shrink-0">
                    <button className="btn-outline !py-1 !px-2 text-xs" onClick={() => toggle(l.id)}>
                      {l.active ? "Disable" : "Enable"}
                    </button>
                    <button className="btn-outline !py-1 !px-2 text-xs" style={{ color: "var(--danger)" }} onClick={() => unlink(l.id)}>
                      Unlink
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </Card>

      {/* --------------------------- Command reference --------------------------- */}
      <Card>
        <h2 className="text-xs font-semibold uppercase tracking-wider mb-3" style={{ color: "var(--text-muted)" }}>
          Phone commands
        </h2>
        <div className="grid sm:grid-cols-2 gap-2.5">
          {COMMANDS.map(([cmd, desc]) => (
            <div key={cmd} className="rounded-lg border px-3 py-2" style={{ borderColor: "var(--border)", background: "var(--bg-inset)" }}>
              <code className="font-mono text-[13px] font-bold" style={{ color: "var(--accent)" }}>{cmd}</code>
              <p className="text-[12px] mt-0.5" style={{ color: "var(--text-muted)" }}>{desc}</p>
            </div>
          ))}
        </div>
        <p className="text-[12px] mt-4" style={{ color: "var(--text-faint)" }}>
          Alerts are pushed automatically for: cycle completions/failures, published posts, quality reviews
          needing attention, safety auto-pauses, and budget warnings.
        </p>
      </Card>
    </div>
  );
}
