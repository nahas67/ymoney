import { Card, EmptyState, PageHeader } from "../components/ui";

/**
 * Social Inbox.
 *
 * HONEST PRODUCT NOTE: YMONEY's current provider integrations (YouTube Data API,
 * TikTok Content Posting API, Facebook Graph via upload relay) support PUBLISHING
 * and public metrics — none of the configured providers expose comment/mention
 * read APIs in this build. Rather than fake an inbox, this surface explains
 * exactly what is and isn't available.
 */
export default function Inbox() {
  return (
    <div className="space-y-5 max-w-2xl">
      <PageHeader
        title="Inbox"
        subtitle="Comments, mentions and messages across platforms."
      />
      <Card>
        <EmptyState
          icon="✉"
          title="No inbox provider connected"
          hint="Reading comments and messages requires platform read APIs that are not part of the current publishing integrations (YouTube Data API comments, TikTok display API, Meta Graph). Connect official read scopes to enable this surface."
        />
        <div className="mt-4 pt-4 border-t text-[13px] space-y-2" style={{ borderColor: "var(--border)", color: "var(--text-muted)" }}>
          <p className="font-medium" style={{ color: "var(--text)" }}>What each platform would need:</p>
          <ul className="list-disc pl-5 space-y-1">
            <li><strong>YouTube:</strong> commentThreads.list scope on the connected OAuth account</li>
            <li><strong>TikTok:</strong> comments are not exposed by the public Content Posting API</li>
            <li><strong>Facebook/Instagram:</strong> Page conversations + comment webhooks</li>
          </ul>
          <p>This page will light up automatically once a provider with read access is connected.</p>
        </div>
      </Card>
    </div>
  );
}
