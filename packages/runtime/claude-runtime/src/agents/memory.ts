import { createHash } from "node:crypto";

import type { JsonObject, JsonValue } from "../contracts.ts";
import type { AgentContextSnapshot } from "./contracts.ts";

export function canonicalDigest(value: JsonValue | unknown): string {
  return "sha256:" + createHash("sha256").update(canonicalJson(value)).digest("hex");
}

export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return "[" + value.map((item) => canonicalJson(item)).join(",") + "]";
  }
  const record = value as Record<string, unknown>;
  return "{" + Object.keys(record).sort().map(
    (key) => JSON.stringify(key) + ":" + canonicalJson(record[key]),
  ).join(",") + "}";
}

export function createContextSnapshot(input: {
  snapshotId: string;
  parentSessionId: string;
  parentTaskId: string;
  parentWorkerRequestId: string;
  mode: "isolated" | "fork" | "resume";
  messageRefs?: string[];
  artifactRefs?: string[];
  evidenceRefs?: string[];
  ancestry: string[];
  depth: number;
  metadata?: JsonObject;
}): AgentContextSnapshot {
  const payload = {
    snapshot_id: input.snapshotId,
    parent_session_id: input.parentSessionId,
    parent_task_id: input.parentTaskId,
    parent_worker_request_id: input.parentWorkerRequestId,
    mode: input.mode,
    message_refs: [...(input.messageRefs ?? [])],
    artifact_refs: [...(input.artifactRefs ?? [])],
    evidence_refs: [...(input.evidenceRefs ?? [])],
    ancestry: [...input.ancestry],
    depth: input.depth,
    metadata: input.metadata ?? {},
  };
  return {
    snapshotId: input.snapshotId,
    parentSessionId: input.parentSessionId,
    parentTaskId: input.parentTaskId,
    parentWorkerRequestId: input.parentWorkerRequestId,
    mode: input.mode,
    messageRefs: payload.message_refs,
    artifactRefs: payload.artifact_refs,
    evidenceRefs: payload.evidence_refs,
    ancestry: payload.ancestry,
    depth: input.depth,
    digest: canonicalDigest(payload),
    metadata: payload.metadata,
  };
}
