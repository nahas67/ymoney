/* Work 13 wire contract for caption presets, motion templates, the effect /
   transition registries, CaptionMotionQC and the Work 12 evidence report.
   React-free by design (same contract as intelApi.ts) so the types can be
   imported by non-React code. Every field here mirrors a TYPED backend
   schema; there is no free-form filter string anywhere in the wire. */

import { wsApi, ApiError } from "../../lib/api";

export type ApiCall = <T>(path: string, body?: unknown) => Promise<T>;

export interface CaptionStyleWire {
  font: string;
  size: number;
  weight: number;
  italic: boolean;
  align: "left" | "center" | "right";
  position: "top" | "middle" | "bottom";
  primary_color: string;
  stroke_width: number;
  stroke_color: string;
  shadow: boolean;
  background: string;
  padding: number;
  corner_radius: number;
  opacity: number;
  case: "none" | "upper" | "lower" | "title";
  line_spacing: number;
  word_spacing: number;
  animation: CaptionAnimationWire;
}

export interface CaptionAnimationWire {
  entrance: string;
  exit: string;
  word_animation: string;
  highlight_animation: string;
  duration: number;
  easing: string;
}

export interface CaptionPresetWire {
  key: string;
  label: string;
  description: string;
  style: CaptionStyleWire;
  default_emphasis: string[];
  max_chars_per_line: number;
  max_lines: number;
  /** False when BrandDNA forbids this preset for the workspace. */
  brand_approved: boolean;
}

export interface PresetsResponse {
  items: CaptionPresetWire[];
  brand_approved: string[];
  brand_caption_style: Record<string, unknown>;
}

export interface MotionTemplateWire {
  key: string;
  type: string;
  label: string;
  description: string;
  body: string;
  sub_body: string;
  slots: string[];
  position: string;
  align: string;
  default_duration: number;
  style: CaptionStyleWire;
  safe_zone: Record<string, number>;
  requires_known_metadata: boolean;
  brand_forbidden: boolean;
}

export interface EffectParamWire {
  name: string;
  kind: "float" | "int" | "bool" | "enum" | "color";
  default: unknown;
  low: number;
  high: number;
  choices: string[];
  required: boolean;
}

export interface EffectWire {
  key: string;
  label: string;
  description: string;
  requires_tracking: boolean;
  params: EffectParamWire[];
  brand_allowed: boolean;
}

export interface TransitionWire {
  key: string;
  label: string;
  xfade: string;
  cross: boolean;
  description: string;
}

export interface EmphasisVocabulary {
  emphasis_kinds: string[];
  protected_kinds_never_offered: string[];
  entrances: string[];
  exits: string[];
  word_animations: string[];
  highlight_animations: string[];
  easings: string[];
}

export interface QcCheckWire {
  name: string;
  passed: boolean;
  detail: string;
  severity: "PASS" | "PASS_WITH_WARNINGS" | "REVIEW_REQUIRED" | "FAIL";
  target: string;
}

export interface QcReportWire {
  status: "PASS" | "PASS_WITH_WARNINGS" | "REVIEW_REQUIRED" | "FAIL";
  blocking: boolean;
  checks: QcCheckWire[];
  failed: number;
  total: number;
}

export interface EvidenceWire {
  asset_id: string;
  word_alignment: { available: boolean; reason: string; word_level: boolean; word_count: number };
  speaker_windows: { speaker_id: string; label: string; start_s: number; end_s: number }[];
  face_tracking: boolean;
  subject_mask: boolean;
  allows: {
    word_level_captions: boolean;
    speaker_lower_thirds: boolean;
    tracked_callouts: boolean;
    background_blur: boolean;
  };
  blocked_reasons: string[];
  min_confidence: number;
}

export function listPresets(call: ApiCall): Promise<PresetsResponse> {
  return call<PresetsResponse>("/captions/presets");
}
export function listTemplates(call: ApiCall): Promise<{ items: MotionTemplateWire[] }> {
  return call<{ items: MotionTemplateWire[] }>("/captions/templates");
}
export function listEffects(call: ApiCall): Promise<{ items: EffectWire[] }> {
  return call<{ items: EffectWire[] }>("/captions/effects");
}
export function listTransitions(call: ApiCall): Promise<{ items: TransitionWire[] }> {
  return call<{ items: TransitionWire[] }>("/captions/transitions");
}
export function emphasisVocabulary(call: ApiCall): Promise<EmphasisVocabulary> {
  return call<EmphasisVocabulary>("/captions/emphasis");
}
export function runCaptionMotionQc(
  call: ApiCall,
  timelineId: string
): Promise<QcReportWire> {
  return call<QcReportWire>("/captions/qc", { timeline_id: timelineId });
}
export function motionEvidence(call: ApiCall, assetId: string): Promise<EvidenceWire> {
  return call<EvidenceWire>(`/captions/evidence?asset_id=${encodeURIComponent(assetId)}`);
}

export { wsApi, ApiError };