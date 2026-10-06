/* Typed client for the ROOT-MOUNTED internal operations probes (Work 16.5.3 §13).
 *
 * WHY THIS EXISTS AND IS SEPARATE FROM `lib/api.ts`
 * --------------------------------------------------
 * `backend/app/main.py` mounts `internal_ops_router` on the APP ROOT, not under
 * `/api/v1`, and says why in a comment: these are process-level, carry no
 * workspace scope, and "an orchestrator cannot authenticate as a workspace".
 *
 * Two consequences follow, and both are deliberate:
 *
 *   1. `lib/api.ts` has a hardcoded `BASE` of `/api/v1` and cannot address these
 *      routes. It is NOT modified to accommodate them -- doing so would put a
 *      second base URL into the one transport every other screen shares.
 *   2. These calls carry NO bearer token, because there is no principal to
 *      present. That is correct for these four routes and would be a security
 *      hole anywhere else.
 *
 * WHY THE PATH SET IS CLOSED
 * --------------------------
 * This was previously `processProbe<T>(path: string)`, which accepted any string.
 * That is exactly the "arbitrary untyped fetch" this work order removes: a typo
 * compiles, an unauthenticated request goes out, and nothing fails.
 *
 * `INTERNAL_OPS_PATH` is a frozen const object and `fetchInternalOps` accepts
 * only its values. Inventing a path is now a TYPE ERROR rather than a runtime
 * surprise. Each entry corresponds to a route the backend actually mounts; the
 * companion test asserts that mapping against `main.py` so the two cannot drift.
 *
 * NOT IN OPENAPI, ON PURPOSE
 * --------------------------
 * These routes are `include_in_schema=False`. That is correct: publishing them
 * would advertise unauthenticated operational endpoints to anyone who can load
 * the spec. The cost is that the normal contract suite cannot see them, so this
 * module plus its test IS their contract.
 */

/** The four root-mounted probes, and only these. */
export const INTERNAL_OPS_PATH = {
  /** Process liveness. Dependency-free so a DB outage cannot cause a restart loop. */
  liveness: "/livez",
  /** Every alert RULE with its live verdict. */
  alerts: "/internal/alerts",
  /** SLO TARGETS. Explicitly not achieved values. */
  slo: "/internal/slo",
  /** Which metric collectors are currently up. */
  collectors: "/internal/collectors",
} as const;

export type InternalOpsKey = keyof typeof INTERNAL_OPS_PATH;
export type InternalOpsPath = (typeof INTERNAL_OPS_PATH)[InternalOpsKey];

/**
 * Fields this client must never surface.
 *
 * Listed so a future edit that starts spreading an operational payload into a
 * user-facing surface has somewhere obvious to be caught. None of these appear
 * in any declared response type.
 */
export const INTERNAL_OPS_FORBIDDEN_FIELDS = [
  "api_key",
  "secret",
  "password",
  "token",
  "authorization",
  "connection_string",
  "env",
] as const;

/**
 * Fetch a root-mounted probe.
 *
 * Generic in the RESPONSE type only, never in the path: the caller states what
 * shape it expects, and the path is constrained by `InternalOpsPath`.
 */
export function fetchInternalOps<T>(path: InternalOpsPath): Promise<T> {
  return fetch(path, { headers: { Accept: "application/json" } }).then((res) => {
    if (!res.ok) {
      throw new InternalOpsError(
        res.status,
        `${path} answered ${res.status} — this probe is unauthenticated and ` +
          `carries no workspace scope.`,
      );
    }
    return res.json() as Promise<T>;
  });
}

/**
 * A probe failed.
 *
 * Its own class so a screen can tell "the internal probe is down" apart from a
 * workspace API failure. They look identical if both are a generic Error, and an
 * operator reading a red panel cannot tell which subsystem is broken.
 */
export class InternalOpsError extends Error {
  readonly status: number;
  readonly path: string;

  constructor(status: number, message: string) {
    super(message);
    this.name = "InternalOpsError";
    this.status = status;
    // Recover the path from the message's prefix so the caller can branch on it.
    this.path = message.split(" ")[0];
  }
}

/** True when a thrown value came from this client rather than the API layer. */
export function isInternalOpsError(err: unknown): err is InternalOpsError {
  return err instanceof InternalOpsError;
}