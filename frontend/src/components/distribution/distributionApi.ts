/* Work 14 §12 — distribution API client (read-only).
 *
 * One typed wrapper per endpoint, mirroring backend/app/api/v1/distribution.py.
 * Nothing here can publish: the Work 14 surface is deliberately read-only plus
 * the optimizer, which is a pure calculation.
 */

import { wsApi } from "../../lib/api";

import type {
  CapabilityRow,
  OptimizationResult,
  PlatformRow,
} from "./DistributionPanel";

export interface LimitField {
  value: unknown;
  source: string;
  note: string;
  verified: boolean;
}

export interface PlatformDetail {
  platform: string;
  capabilities: string[];
  publish_mode: string;
  required_permissions: string[];
  verified_notes: string[];
  [field: string]: unknown;
}

async function get<T>(path: string): Promise<T> {
  return (wsApi.get as unknown as (p: string) => Promise<T>)(path);
}

async function post<T>(path: string, body: unknown): Promise<T> {
  return (wsApi.post as unknown as (p: string, b: unknown) => Promise<T>)(
    path,
    body
  );
}

/** Every verified platform profile, with what is and is not documented. */
export function listPlatforms(): Promise<{ items: PlatformRow[] }> {
  return get<{ items: PlatformRow[] }>("/distribution/platforms");
}

/** One profile, with the source document behind every verified limit. */
export function getPlatform(platform: string): Promise<PlatformDetail> {
  return get<PlatformDetail>(`/distribution/platforms/${platform}`);
}

/** Capability badges, publish mode, inbox/analytics readiness. */
export function listCapabilities(): Promise<{ items: CapabilityRow[] }> {
  return get<{ items: CapabilityRow[] }>("/distribution/capabilities");
}

export interface OptimizeRequest {
  platform: string;
  master: Record<string, unknown>;
  brand?: Record<string, unknown>;
  overrides?: Record<string, unknown>;
  learned?: Record<string, unknown>;
}

/** Run the platform-variant optimizer and return the diff + provenance. */
export function optimize(
  request: OptimizeRequest
): Promise<OptimizationResult> {
  return post<OptimizationResult>("/distribution/optimize", request);
}
