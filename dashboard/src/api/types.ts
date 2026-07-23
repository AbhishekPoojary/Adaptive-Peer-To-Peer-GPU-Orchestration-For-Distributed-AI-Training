import type { components } from "./schema.gen";

export type NodeSummary = components["schemas"]["NodeSummary"];
export type NodeDetail = components["schemas"]["NodeDetailResponse"];
export type TelemetrySample = components["schemas"]["TelemetrySampleOut"];
export type GpuTelemetry = components["schemas"]["GpuTelemetry"];
export type HardwareInventory = components["schemas"]["HardwareInventory"];
export type GpuInfo = components["schemas"]["GpuInfo"];

export type JobSummary = components["schemas"]["JobSummary"];
export type JobDetail = components["schemas"]["JobDetailResponse"];
export type JobEvent = components["schemas"]["JobEventOut"];
export type Lease = components["schemas"]["LeaseOut"];
export type JobSpec = components["schemas"]["JobSpec"];
export type JobSubmitRequest = components["schemas"]["JobSubmitRequest"];

export type SchedulingDecision = components["schemas"]["SchedulingDecisionOut"];
export type SchedulingCandidate = components["schemas"]["SchedulingCandidateOut"];

/**
 * `Job(Summary|Detail).spec` and `JobEventOut.detail` are stored as free-form
 * JSONB on the backend (`dict[str, Any]`), so openapi-typescript can only
 * type them as `Record<string, never>`. The real shapes are known (JobSpec,
 * and a `detail` object that always carries a human-readable `message` per
 * orchestrator/models/job.py) — these helpers narrow defensively rather than
 * asserting, since nothing here is invented if a field is ever absent.
 */
export function asJobSpec(spec: unknown): Partial<JobSpec> {
  return (spec ?? {}) as Partial<JobSpec>;
}

export interface JobEventDetail {
  message?: string;
  [key: string]: unknown;
}

export function asJobEventDetail(detail: unknown): JobEventDetail {
  return (detail ?? {}) as JobEventDetail;
}

// --- Enum-shaped string unions, mirroring the backend models exactly -------
// (orchestrator/models/node.py NodeStatus, orchestrator/models/job.py
// JobState, orchestrator/models/lease.py LeaseState). Kept as plain string
// unions + const arrays rather than TS `enum` (tsconfig erasableSyntaxOnly).

export const NODE_STATUSES = ["ONLINE", "OFFLINE", "DRAINING"] as const;
export type NodeStatus = (typeof NODE_STATUSES)[number];

export const JOB_STATES = [
  "QUEUED",
  "SCHEDULED",
  "LEASED",
  "RUNNING",
  "COMPLETED",
  "FAILED",
  "REASSIGNED",
  "CANCELLED",
] as const;
export type JobState = (typeof JOB_STATES)[number];

export const LEASE_STATES = [
  "PENDING",
  "ACTIVE",
  "EXPIRED",
  "RELEASED",
  "COMPLETED",
  "FAILED",
] as const;
export type LeaseStateValue = (typeof LEASE_STATES)[number];

/** Jobs still ahead of RUNNING — used for Overview's "queue depth". */
export const QUEUE_DEPTH_STATES: readonly JobState[] = ["QUEUED", "SCHEDULED"];

/** Mirrors orchestrator/models/job.py TERMINAL_STATES exactly — a job here
 * never transitions again, so Job detail hides the Cancel action. */
export const JOB_TERMINAL_STATES: readonly JobState[] = [
  "COMPLETED",
  "FAILED",
  "CANCELLED",
];

export function isTerminalJobState(state: string): boolean {
  return (JOB_TERMINAL_STATES as string[]).includes(state);
}
