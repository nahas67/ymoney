import { Card, ConfirmButton } from "../ui";

/** Parsed body of lane D's save-side 409:
 *  `{error, expected_version, actual_version}` (api/v1/timelines.py). */
export type ConflictDetail = {
  error?: string;
  expected_version?: number;
  actual_version?: number;
} | null;

type Props = {
  conflict: ConflictDetail;
  localVersion: number;
  onReload: () => void;
};

/**
 * Optimistic-concurrency conflict notice (contracts §8/§12: "conflict notice
 * on 409 … FE-B ensures the reload path is usable").
 *
 * The user's local (unsaved) edit state stays on screen — the server is never
 * allowed to silently overwrite it — and reloading the server tip is an
 * explicit, armed action, so unsaved work is never discarded without the user
 * saying so.
 */
export default function ConflictNotice({ conflict, localVersion, onReload }: Props) {
  return (
    <Card>
      <b>Edit conflict:</b> the timeline changed elsewhere
      {conflict?.expected_version != null ? (
        <> — server is v{conflict.expected_version}, your view is v{localVersion}.</>
      ) : (
        <> (your view is v{localVersion}).</>
      )}{" "}
      Your unsaved edits stay on screen until you reload — nothing was overwritten on the server.
      <ConfirmButton
        className="btn-primary !text-xs ml-3"
        confirmText="Discard my unsaved edits?"
        onConfirm={onReload}
      >
        Reload latest
      </ConfirmButton>
    </Card>
  );
}
