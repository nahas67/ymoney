import { useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, Field, PageHeader, toast } from "../components/ui";

/**
 * Provider status, paid-job incidents, voice preview and music policy.
 *
 * The point of this panel is to be honest about what actually works. A
 * provider that merely imports is not "ready", and this UI must never imply
 * otherwise — so every state word from the API is rendered with plain language
 * and an unavailable or commercially-restricted provider is visibly not
 * selectable.
 *
 * Secrets: the API returns credential STATES only (never a value, a length or
 * a digest), and this component never renders a value even if one appeared.
 *
 * Paid-job incidents (Work 15.7 §13) follow the same rule about money that the
 * rest of this file follows about providers: say what is true, plainly. Two
 * specific refusals —
 *
 *  - `SUBMISSION_UNKNOWN` is NEVER rendered as "Failed". A failed submit was
 *    rejected and cost nothing; an unknown one may already be an invoice.
 *    Collapsing them is how an operator cancels a job that already billed.
 *  - there is NO retry button unless the API proved the retry cannot double
 *    charge (`retry_safe`). The button is not rendered disabled — it is not
 *    rendered, because a disabled button invites a second click.
 */

/** How each maturity state reads to a human, and whether it may be picked. */
const STATE_UI: Record<string, { label: string; tone: string; hint: string }> = {
  LIVE_VERIFIED: {
    label: "Live verified",
    tone: "ok",
    hint: "Implemented, contract-tested and exercised against the real API.",
  },
  CONTRACT_TESTED: {
    label: "Contract tested",
    tone: "ok",
    hint: "Implemented and covered by tests against a stubbed transport. Not yet exercised against the live API.",
  },
  IMPLEMENTED: {
    label: "Implemented",
    tone: "warn",
    hint: "Code exists but has no contract test yet. Treat as unproven.",
  },
  UNVERIFIED: {
    label: "Unverified",
    tone: "warn",
    hint: "No evidence yet that this provider works. Not selectable.",
  },
  CONFIG_GATED: {
    label: "Needs credentials",
    tone: "warn",
    hint: "Implemented, but this workspace has not configured the required key.",
  },
  UNAVAILABLE: {
    label: "Unavailable",
    tone: "bad",
    hint: "Not implemented or deliberately disabled. Not selectable.",
  },
  BLOCKED_COMMERCIAL_TERMS: {
    label: "Restricted for commercial use",
    tone: "bad",
    hint: "Licence or vendor terms forbid production/commercial use here.",
  },
  EXTERNAL_LIMITATION: {
    label: "External limitation",
    tone: "warn",
    hint: "The vendor imposes a limit (no SLA, rate cap, region lock). Not a YMONEY defect.",
  },
  BLOCKED_LICENSE: {
    label: "Blocked by licence",
    tone: "bad",
    hint: "Licence, weight or attribution terms prevent use.",
  },
};

/** States in which a provider may actually be chosen. */
const SELECTABLE = new Set(["LIVE_VERIFIED", "CONTRACT_TESTED", "CONFIG_GATED"]);

const CAPABILITIES = ["llm", "tts", "music", "video", "image"];

/**
 * How a paid-submission state reads to an operator, and what to do about it.
 *
 * `tone` drives the badge colour. The hint is the whole reason this table
 * exists: "SUBMISSION_UNKNOWN" is a state word from a state machine, and an
 * operator who has to look it up cannot act on it during an incident.
 */
const INCIDENT_UI: Record<
  string,
  { label: string; tone: string; hint: string; action: string }
> = {
  SUBMISSION_UNKNOWN: {
    label: "Submission unconfirmed — may already be billed",
    tone: "bad",
    hint: "The request was sent but no confirmation came back. The provider may have accepted and billed it. Do not resubmit: reconcile with the provider's job list first.",
    action: "Reconcile with the provider",
  },
  SUBMISSION_ATTEMPTED: {
    label: "Submit in flight",
    tone: "warn",
    hint: "A billable submit was recorded but nothing confirmed acceptance yet. Give it time before concluding anything.",
    action: "Wait, then reconcile",
  },
};

/** What the operator should do next, in the API's vocabulary. */
const ACTION_UI: Record<string, string> = {
  RECONCILE: "Reconcile with the provider",
  RETRY_IF_CONFIRMED_SAFE: "Retry is permitted",
  MARK_FAILED: "Mark as failed",
  MANUAL_OVERRIDE: "Operator override recorded",
};

export default function ProviderStatus() {
  const [cap, setCap] = useState("tts");

  const maturity = useFetch<any>(
    () => wsApi.get(`/provider-maturity?capability=${encodeURIComponent(cap)}`),
    [cap]
  );
  const incidents = useFetch<any>(
    () => wsApi.get("/provider-maturity/incidents"),
    []
  );
  const preview = useFetch<any>(() => wsApi.get("/voice-preview/providers"), []);
  const music = useFetch<any>(() => wsApi.get("/music/policy"), []);
  const musicProviders = useFetch<any>(() => wsApi.get("/music/providers"), []);

  const [voiceProvider, setVoiceProvider] = useState("");
  const [voices, setVoices] = useState<any[]>([]);
  const [previewNote, setPreviewNote] = useState("");
  const [musicNote, setMusicNote] = useState("");

  const offerable: any[] = Array.isArray(preview.data?.items) ? preview.data.items : [];

  async function loadVoices(id: string) {
    setVoiceProvider(id);
    setVoices([]);
    setPreviewNote("");
    if (!id) return;
    try {
      const res = await wsApi.get(
        `/voice-preview/providers/${encodeURIComponent(id)}/voices`
      );
      setVoices(Array.isArray(res?.items) ? res.items : []);
    } catch (err: any) {
      setPreviewNote(err?.message || "Could not load voices.");
    }
  }

  async function doPreview(voiceId: string) {
    setPreviewNote("");
    try {
      const res = await wsApi.post("/voice-preview", {
        provider: voiceProvider,
        voice: voiceId,
        text: "This is a short sample of the selected voice.",
      });
      if (res?.clamped) {
        setPreviewNote("Sample text was shortened to fit the preview budget.");
      }
      toast("Preview generated");
    } catch (err: any) {
      // A 429 here is the cost guard doing its job, not a bug.
      setPreviewNote(err?.message || "Preview was refused or unavailable.");
    }
  }

  async function toggleMusic() {
    setMusicNote("");
    const next = !music.data?.generate;
    try {
      await wsApi.put("/music/policy", { generate: next });
      music.reload();
      toast(next ? "AI music enabled" : "AI music disabled");
    } catch (err: any) {
      setMusicNote(err?.message || "Could not change the music policy.");
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Providers"
        subtitle="What is actually implemented, what is merely present, and what is restricted."
      />

      <Card>
        <div className="flex items-center justify-between mb-3">
          <div className="font-medium">Maturity by capability</div>
          <select
            className="border rounded px-2 py-1 text-sm"
            value={cap}
            onChange={(e) => setCap(e.target.value)}
          >
            {CAPABILITIES.map((c) => (
              <option key={c} value={c}>
                {c.toUpperCase()}
              </option>
            ))}
          </select>
        </div>
        {maturity.loading && <div className="text-sm opacity-70">Loading…</div>}
        {maturity.error && <div className="text-sm text-red-600">{maturity.error}</div>}
        {maturity.data && (
          <MaturityTable items={maturity.data.items || []} />
        )}
      </Card>

      <Card>
        <div className="font-medium mb-1">Paid-job incidents</div>
        <div className="text-xs opacity-70 mb-3">
          Calls that may already have been billed and still need a decision.
          An unconfirmed submission is not a failure: it is a request the
          provider may have accepted, so it is never shown as failed and never
          offered a retry unless retrying is proven not to double-charge.
        </div>
        {incidents.loading && <div className="text-sm opacity-70">Loading…</div>}
        {incidents.error && (
          <div className="text-sm text-red-600">{incidents.error}</div>
        )}
        {incidents.data && <IncidentList data={incidents.data} />}
      </Card>

      <Card>
        <div className="font-medium mb-1">Routed execution</div>
        <div className="text-xs opacity-70 mb-3">
          How a routed request was actually executed: the tier and provider
          chosen, whether it ran locally or against a paid gateway, how many
          attempts it cost, and — for an ambiguous outcome — that a charge may
          have occurred and automatic fallback was blocked.
        </div>
        <RoutedExecution />
      </Card>

      <Card>
        <div className="font-medium mb-1">Voice</div>
        <div className="text-xs opacity-70 mb-3">
          Preview uses the same voice identities production narration uses, so a
          voice you approve here is a voice the render can actually speak.
        </div>
        {preview.loading && <div className="text-sm opacity-70">Loading…</div>}
        <div className="space-y-2">
          {offerable.map((p: any) => {
            const ok = p.available === true;
            const why = STATE_UI[p.implementation_status];
            return (
              <div key={p.key} className="border rounded p-2">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-sm">{p.key}</span>
                  <Badge tone={ok ? "ok" : "bad"}>
                    {ok ? "Selectable" : p.reason || "Unavailable"}
                  </Badge>
                </div>
                {!ok && (
                  <div className="text-xs opacity-70 mt-1">
                    {why?.hint || p.detail || "Not selectable."}
                    {Array.isArray(p.missing_credentials) &&
                      p.missing_credentials.length > 0 &&
                      ` Missing: ${p.missing_credentials.join(", ")}`}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        <div className="flex gap-2 items-end mt-3 flex-wrap">
          <Field label="Provider">
            <select
              className="border rounded px-2 py-1 text-sm"
              value={voiceProvider}
              onChange={(e) => loadVoices(e.target.value)}
            >
              <option value="">Select a provider…</option>
              {offerable
                .filter((p: any) => p.available === true)
                .map((p: any) => (
                  <option key={p.key} value={p.key}>
                    {p.key}
                  </option>
                ))}
            </select>
          </Field>
          <Field label="Voice">
            <select
              className="border rounded px-2 py-1 text-sm"
              value=""
              onChange={(e) => e.target.value && doPreview(e.target.value)}
              disabled={!voices.length}
            >
              <option value="">
                {voices.length ? "Preview a voice…" : "No voices"}
              </option>
              {voices.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.id}
                  {v.locale ? ` (${v.locale})` : ""}
                </option>
              ))}
            </select>
          </Field>
        </div>
        {previewNote && <div className="text-xs mt-2 opacity-80">{previewNote}</div>}
      </Card>

      <Card>
        <div className="font-medium mb-1">Music</div>
        <div className="text-xs opacity-70 mb-3">
          Generated music is opt-in and never replaces an original asset. Brand
          rules always win over a suggestion.
        </div>
        {music.loading && <div className="text-sm opacity-70">Loading…</div>}
        {music.data && (
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <span>
                Generate music{" "}
                <span className="text-xs opacity-70">
                  ({music.data.generate ? "on" : "off"}
                  {music.data.reason ? ` — ${music.data.reason}` : ""})
                </span>
              </span>
              <button
                className="border rounded px-3 py-1 text-sm"
                onClick={toggleMusic}
              >
                {music.data.generate ? "Disable" : "Enable"}
              </button>
            </div>
            <div className="text-xs opacity-70">
              Brand policy: {JSON.stringify(music.data.brand ?? {})}
            </div>
          </div>
        )}
        {musicNote && <div className="text-xs mt-2 opacity-80">{musicNote}</div>}

        {musicProviders.data && (
          <div className="mt-3 space-y-1">
            <div className="text-xs font-medium opacity-80">Music providers</div>
            {(musicProviders.data.items || []).map((m: any) => (
              <div key={m.key} className="flex items-center justify-between text-sm">
                <span className="font-mono">{m.key}</span>
                <Badge tone={m.available ? "ok" : "bad"}>
                  {m.available ? "Available" : m.reason || "Unavailable"}
                </Badge>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

function RoutedExecution() {
  const chains = useFetch<any>(() => wsApi.get("/intelligence/routing/chains"), []);

  if (chains.loading) {
    return <div className="text-sm opacity-70">Loading…</div>;
  }
  if (chains.error) {
    return <div className="text-sm text-red-600">{chains.error}</div>;
  }
  const rows: any[] = Array.isArray(chains.data?.chains) ? chains.data.chains : [];
  if (!rows.length) {
    return (
      <div className="text-sm opacity-70">
        No routed executions recorded in this session.
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {rows.map((chain: any, i: number) => {
        // ChainRecord: workspace_id, task_type, policy, legs, stop_reason,
        // paid_legs, estimated_exposure_usd, at, succeeded_tier.
        const unknown = String(chain.stop_reason || "").includes("UNKNOWN");
        return (
          <div
            key={`${chain.at}-${i}`}
            className="border rounded p-2 space-y-1"
          >
            <div className="flex items-center justify-between flex-wrap gap-2">
              <span className="font-mono text-xs">
                {chain.task_type || "routed request"}
                {chain.succeeded_tier ? ` → ${chain.succeeded_tier}` : ""}
              </span>
              <Badge tone={unknown ? "bad" : "ok"}>
                {unknown
                  ? "SUBMISSION UNKNOWN"
                  : chain.succeeded_tier
                    ? "completed"
                    : chain.stop_reason || "stopped"}
              </Badge>
            </div>
            <div className="text-xs opacity-80 flex flex-wrap gap-x-4 gap-y-1">
              <span>paid attempts: {chain.paid_legs ?? 0}</span>
              <span>legs walked: {(chain.legs || []).length}</span>
              <span>
                estimated exposure: $
                {Number(chain.estimated_exposure_usd ?? 0).toFixed(6)}
              </span>
              <span>at: {chain.at || "-"}</span>
            </div>
            {chain.stop_reason && (
              <div className="text-xs opacity-70">
                stopped because: {chain.stop_reason}
              </div>
            )}
            {unknown && (
              <div className="text-xs text-amber-600">
                A charge may have occurred. Automatic fallback is blocked; a
                human decision is required.
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function IncidentList({ data }: { data: any }) {
  const items: any[] = Array.isArray(data?.items) ? data.items : [];
  if (!items.length) {
    return (
      <div className="text-sm opacity-70">
        No paid submissions are waiting on a decision.
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {typeof data?.unknown_exposure_count === "number" &&
        data.unknown_exposure_count > 0 && (
          <div className="text-xs text-red-600">
            {data.unknown_exposure_count} of these may already have been billed
            at an amount nobody can price. They are not recorded as $0.
          </div>
        )}
      {items.map((row: any) => (
        <IncidentRow key={row.incident_id} row={row} />
      ))}
    </div>
  );
}

function IncidentRow({ row }: { row: any }) {
  const ui = INCIDENT_UI[row.state];
  // The state word is shown verbatim next to the prose, so a reader can match
  // it against the ledger. An unknown state renders as itself rather than
  // falling back to a reassuring default.
  const label = ui?.label || row.state;
  const tone = ui?.tone || "warn";
  const action = ACTION_UI[row.recommended_action] || row.recommended_action;
  const exposure =
    row.exposure_unknown || row.exposure === "UNKNOWN_EXPOSURE"
      ? "unknown — may have been billed"
      : row.estimated_exposure_usd == null
        ? "not recorded"
        : `~$${Number(row.estimated_exposure_usd).toFixed(4)}`;

  return (
    <div className="border rounded p-2">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <span className="font-mono text-sm">
          {row.provider}.{row.operation}
        </span>
        <Badge tone={tone}>{label}</Badge>
      </div>
      <dl className="grid sm:grid-cols-2 gap-x-4 gap-y-0.5 text-xs mt-2">
        <div className="flex gap-2">
          <dt className="opacity-60">State</dt>
          <dd className="font-mono">{row.display_state || row.state}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="opacity-60">Attempted</dt>
          <dd>{row.attempted_at || "unknown"}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="opacity-60">Remote id</dt>
          <dd className="font-mono">{row.remote_id || "none returned"}</dd>
        </div>
        <div className="flex gap-2">
          <dt className="opacity-60">Exposure</dt>
          <dd>{exposure}</dd>
        </div>
      </dl>
      <div className="text-xs mt-2">
        <span className="opacity-60">Recommended: </span>
        {action}
      </div>
      {ui?.hint && <div className="text-xs opacity-70 mt-0.5">{ui.hint}</div>}
      {row.detail && (
        <div className="text-xs opacity-70 mt-0.5 break-words">{row.detail}</div>
      )}
      {/* A retry control appears ONLY when the contract proved the submit never
          reached the provider. Everything else is a reconciliation task, and
          offering a button for it is offering a second charge. */}
      {row.retry_safe === true ? (
        <button className="border rounded px-2 py-1 text-xs mt-2">
          Retry (proven not to double-charge)
        </button>
      ) : (
        <div className="text-xs opacity-60 mt-2">
          No retry offered: resubmitting this could be billed twice.
        </div>
      )}
    </div>
  );
}

function MaturityTable({ items }: { items: any[] }) {
  if (!items.length) {
    return <div className="text-sm opacity-70">No providers reported.</div>;
  }
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-left opacity-70">
          <th className="py-1">Provider</th>
          <th>Capability</th>
          <th>Status</th>
          <th>Credential</th>
        </tr>
      </thead>
      <tbody>
        {items.map((row: any, i: number) => {
          const ui = STATE_UI[row.implementation_status];
          const selectable = SELECTABLE.has(row.implementation_status);
          return (
            <tr key={`${row.provider}-${i}`} className="border-t align-top">
              <td className="py-1 font-mono">{row.provider}</td>
              <td className="opacity-70">{row.capability}</td>
              <td>
                <div className="flex items-center gap-2 flex-wrap">
                  <Badge tone={selectable ? "ok" : "warn"}>
                    {ui?.label || row.implementation_status}
                  </Badge>
                  {row.commercial_status &&
                    row.commercial_status !== "UNVERIFIED" && (
                      <Badge tone={STATE_UI[row.commercial_status]?.tone || "warn"}>
                        {STATE_UI[row.commercial_status]?.label ||
                          row.commercial_status}
                      </Badge>
                    )}
                </div>
                {ui?.hint && (
                  <div className="text-xs opacity-70 mt-0.5">{ui.hint}</div>
                )}
              </td>
              <td className="opacity-70">{row.credential_status}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}