import { createHash } from "node:crypto";

import type { JsonObject, JsonValue } from "../contracts.ts";


export interface SessionAppendEntry {
  eventId: string;
  sequence: number;
  kind: string;
  payloadDigest: string;
}

export interface SessionAppendRequest {
  runId: string;
  taskId: string;
  sessionId: string;
  parentSessionId: string;
  checkpointId: string;
  expectedRevision: number;
  entries: SessionAppendEntry[];
  idempotencyKey: string;
  metadata?: JsonObject;
}

export interface SessionContinuityReceipt {
  schema: "zyra.omp-session-continuity-receipt/v1";
  receipt_id: string;
  run_id: string;
  task_id: string;
  session_id: string;
  parent_session_id: string;
  checkpoint_id: string;
  previous_revision: number;
  revision: number;
  appended_event_ids: string[];
  lineage: string[];
  content_digest: string;
  replayed: boolean;
  created_at: string;
}

export type TaskTerminalState = "succeeded" | "failed" | "cancelled";

export interface TaskTerminalRequest {
  runId: string;
  taskId: string;
  jobId: string;
  attemptId: string;
  generation: number;
  state: TaskTerminalState;
  resultRef: string;
  errorCode: string;
  idempotencyKey: string;
  metadata?: JsonObject;
}

export interface TaskTerminalReceipt {
  schema: "zyra.omp-task-terminal-receipt/v1";
  receipt_id: string;
  run_id: string;
  task_id: string;
  job_id: string;
  attempt_id: string;
  generation: number;
  state: TaskTerminalState;
  result_ref: string;
  error_code: string;
  replayed: boolean;
  created_at: string;
}

export interface WorktreeMergeRequest {
  runId: string;
  taskId: string;
  worktreeId: string;
  baseRevision: string;
  headRevision: string;
  targetRevision: string;
  changedPaths: string[];
  conflictPaths: string[];
  idempotencyKey: string;
  metadata?: JsonObject;
}

export interface WorktreeMergeReceipt {
  schema: "zyra.omp-worktree-merge-receipt/v1";
  receipt_id: string;
  run_id: string;
  task_id: string;
  worktree_id: string;
  base_revision: string;
  head_revision: string;
  target_revision: string;
  changed_paths: string[];
  conflict_paths: string[];
  state: "committed" | "conflicted";
  side_effect_key: string;
  replayed: boolean;
  created_at: string;
}

interface SessionState {
  runId: string;
  taskId: string;
  sessionId: string;
  parentSessionId: string;
  checkpointId: string;
  revision: number;
  entries: SessionAppendEntry[];
  lineage: string[];
  receipts: SessionContinuityReceipt[];
}

function stableJson(value: unknown): string {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return "[" + value.map((item) => stableJson(item)).join(",") + "]";
  }
  const record = value as Record<string, unknown>;
  return "{" + Object.keys(record).sort().map((key) => JSON.stringify(key) + ":" + stableJson(record[key])).join(",") + "}";
}

function digest(value: unknown): string {
  return "sha256:" + createHash("sha256").update(stableJson(value)).digest("hex");
}

function identity(name: string, value: string): string {
  const candidate = String(value ?? "").trim();
  if (!candidate || !/^[A-Za-z0-9][A-Za-z0-9._:@/\-]{0,511}$/.test(candidate)) {
    throw new Error(name + " is missing or invalid");
  }
  return candidate;
}

function revision(name: string, value: number): number {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error(name + " must be a non-negative safe integer");
  }
  return value;
}

function uniqueIdentities(name: string, values: string[]): string[] {
  const result: string[] = [];
  const seen = new Set<string>();
  for (const raw of values) {
    const value = identity(name, raw);
    if (seen.has(value)) {
      continue;
    }
    seen.add(value);
    result.push(value);
  }
  return result;
}

function logicalPath(name: string, value: string): string {
  const candidate = identity(name, value).replaceAll("\\", "/");
  if (candidate.startsWith("/") || /^[A-Za-z]:\//.test(candidate)) {
    throw new Error(name + " cannot be absolute");
  }
  const parts = candidate.split("/");
  if (parts.some((part) => !part || part === "." || part === "..")) {
    throw new Error(name + " contains an unsafe segment");
  }
  return candidate;
}

export class AppendOnlySessionContinuityRuntime {
  private readonly states = new Map<string, SessionState>();
  private readonly idempotency = new Map<string, { requestDigest: string; receipt: SessionContinuityReceipt }>();

  commit(request: SessionAppendRequest): SessionContinuityReceipt {
    const normalized = this.normalize(request);
    const requestDigest = digest(normalized);
    const replayKey = this.replayKey(normalized);
    const replay = this.idempotency.get(replayKey);
    if (replay) {
      if (replay.requestDigest !== requestDigest) {
        throw new Error("session append idempotency key was reused with different content");
      }
      return { ...structuredClone(replay.receipt), replayed: true };
    }

    const current = this.states.get(normalized.sessionId);
    if (current) {
      this.assertScope(current, normalized);
      if (current.revision !== normalized.expectedRevision) {
        throw new Error("session append compare-and-swap revision changed");
      }
    } else if (normalized.expectedRevision !== 0) {
      throw new Error("initial session append must expect revision zero");
    }

    const existingEntries = current?.entries ?? [];
    const knownEventIds = new Set(existingEntries.map((item) => item.eventId));
    const appended: SessionAppendEntry[] = [];
    let expectedSequence = existingEntries.length
      ? existingEntries[existingEntries.length - 1].sequence + 1
      : 1;
    for (const entry of normalized.entries) {
      if (knownEventIds.has(entry.eventId)) {
        const existing = existingEntries.find((item) => item.eventId === entry.eventId);
        if (!existing || digest(existing) !== digest(entry)) {
          throw new Error("session event identity was reused with different content");
        }
        continue;
      }
      if (entry.sequence !== expectedSequence) {
        throw new Error("session append sequence is not contiguous");
      }
      knownEventIds.add(entry.eventId);
      appended.push(structuredClone(entry));
      expectedSequence += 1;
    }
    if (!appended.length) {
      throw new Error("session append contains no new events");
    }

    const priorRevision = current?.revision ?? 0;
    const nextEntries = [...existingEntries, ...appended];
    const parentLineage = current?.lineage ?? (normalized.parentSessionId ? [normalized.parentSessionId] : []);
    const lineage = [...parentLineage, normalized.sessionId].filter((value, index, values) => values.indexOf(value) === index);
    const contentDigest = digest({
      runId: normalized.runId,
      taskId: normalized.taskId,
      sessionId: normalized.sessionId,
      parentSessionId: normalized.parentSessionId,
      checkpointId: normalized.checkpointId,
      entries: nextEntries,
      lineage,
    });
    const receipt: SessionContinuityReceipt = {
      schema: "zyra.omp-session-continuity-receipt/v1",
      receipt_id: "ompsession_" + digest({ replayKey, contentDigest, revision: priorRevision + 1 }).slice(7, 47),
      run_id: normalized.runId,
      task_id: normalized.taskId,
      session_id: normalized.sessionId,
      parent_session_id: normalized.parentSessionId,
      checkpoint_id: normalized.checkpointId,
      previous_revision: priorRevision,
      revision: priorRevision + 1,
      appended_event_ids: appended.map((item) => item.eventId),
      lineage,
      content_digest: contentDigest,
      replayed: false,
      created_at: new Date().toISOString(),
    };
    const state: SessionState = {
      runId: normalized.runId,
      taskId: normalized.taskId,
      sessionId: normalized.sessionId,
      parentSessionId: normalized.parentSessionId,
      checkpointId: normalized.checkpointId,
      revision: receipt.revision,
      entries: nextEntries,
      lineage,
      receipts: [...(current?.receipts ?? []), receipt],
    };
    this.states.set(normalized.sessionId, state);
    this.idempotency.set(replayKey, { requestDigest, receipt });
    return structuredClone(receipt);
  }

  resume(sessionId: string, processedEventIds: string[] = []): JsonObject {
    const state = this.require(sessionId);
    const processed = new Set(uniqueIdentities("processed event id", processedEventIds));
    const pending = state.entries.filter((item) => !processed.has(item.eventId));
    return {
      schema: "zyra.omp-session-resume-workset/v1",
      run_id: state.runId,
      task_id: state.taskId,
      session_id: state.sessionId,
      checkpoint_id: state.checkpointId,
      revision: state.revision,
      lineage: [...state.lineage],
      pending_entries: structuredClone(pending) as unknown as JsonValue,
      bypassed_event_ids: state.entries.filter((item) => processed.has(item.eventId)).map((item) => item.eventId),
      append_only: true,
      processed_response_replay: false,
    };
  }

  fork(parentSessionId: string, childRequest: SessionAppendRequest): SessionContinuityReceipt {
    const parent = this.require(parentSessionId);
    if (childRequest.parentSessionId !== parent.sessionId) {
      throw new Error("fork parent session identity mismatch");
    }
    if (childRequest.runId !== parent.runId || childRequest.taskId !== parent.taskId) {
      throw new Error("fork cannot cross run or task custody");
    }
    if (this.states.has(childRequest.sessionId)) {
      throw new Error("fork target session already exists");
    }
    const receipt = this.commit(childRequest);
    const child = this.require(childRequest.sessionId);
    child.lineage = [...parent.lineage, child.sessionId].filter((value, index, values) => values.indexOf(value) === index);
    const corrected = { ...receipt, lineage: [...child.lineage] };
    child.receipts[child.receipts.length - 1] = corrected;
    this.states.set(child.sessionId, child);
    const replayKey = this.replayKey(this.normalize(childRequest));
    const replay = this.idempotency.get(replayKey);
    if (replay) {
      this.idempotency.set(replayKey, { ...replay, receipt: corrected });
    }
    return structuredClone(corrected);
  }

  snapshot(): JsonObject {
    return {
      schema: "zyra.omp-session-continuity-runtime/v1",
      sessions: Object.fromEntries([...this.states].map(([key, value]) => [key, {
        run_id: value.runId,
        task_id: value.taskId,
        parent_session_id: value.parentSessionId,
        checkpoint_id: value.checkpointId,
        revision: value.revision,
        event_count: value.entries.length,
        lineage: [...value.lineage],
        latest_receipt_id: value.receipts.at(-1)?.receipt_id ?? "",
      }])) as JsonObject,
      canonical_session_owner: "python.SessionLifecycleRuntime",
      process_local_receipts_only: true,
    };
  }

  private require(sessionId: string): SessionState {
    const selected = this.states.get(identity("session id", sessionId));
    if (!selected) {
      throw new Error("session continuity state does not exist");
    }
    return structuredClone(selected);
  }

  private normalize(request: SessionAppendRequest): SessionAppendRequest {
    const entries = request.entries.map((entry) => ({
      eventId: identity("event id", entry.eventId),
      sequence: revision("event sequence", entry.sequence),
      kind: identity("event kind", entry.kind),
      payloadDigest: identity("payload digest", entry.payloadDigest),
    }));
    const seen = new Set<string>();
    for (const entry of entries) {
      if (seen.has(entry.eventId)) {
        throw new Error("session append contains duplicate event ids");
      }
      seen.add(entry.eventId);
    }
    return {
      runId: identity("run id", request.runId),
      taskId: identity("task id", request.taskId),
      sessionId: identity("session id", request.sessionId),
      parentSessionId: request.parentSessionId ? identity("parent session id", request.parentSessionId) : "",
      checkpointId: identity("checkpoint id", request.checkpointId),
      expectedRevision: revision("expected revision", request.expectedRevision),
      entries,
      idempotencyKey: identity("idempotency key", request.idempotencyKey),
      metadata: structuredClone(request.metadata ?? {}),
    };
  }

  private replayKey(request: SessionAppendRequest): string {
    return digest({
      runId: request.runId,
      taskId: request.taskId,
      sessionId: request.sessionId,
      idempotencyKey: request.idempotencyKey,
    });
  }

  private assertScope(current: SessionState, request: SessionAppendRequest): void {
    if (current.runId !== request.runId || current.taskId !== request.taskId) {
      throw new Error("session append run or task identity changed");
    }
    if (current.parentSessionId !== request.parentSessionId) {
      throw new Error("session append parent lineage changed");
    }
  }
}

export class TaskTerminalReceiptRuntime {
  private readonly terminals = new Map<string, TaskTerminalReceipt>();
  private readonly requestDigests = new Map<string, string>();

  settle(request: TaskTerminalRequest): TaskTerminalReceipt {
    const key = digest({ runId: request.runId, taskId: request.taskId, jobId: request.jobId, idempotencyKey: request.idempotencyKey });
    const requestDigest = digest(request);
    const replay = this.terminals.get(key);
    if (replay) {
      if (this.requestDigests.get(key) !== requestDigest) {
        throw new Error("task terminal idempotency key was reused with different content");
      }
      return { ...structuredClone(replay), replayed: true };
    }
    const generation = revision("task generation", request.generation);
    if (!["succeeded", "failed", "cancelled"].includes(request.state)) {
      throw new Error("task terminal state is invalid");
    }
    const receipt: TaskTerminalReceipt = {
      schema: "zyra.omp-task-terminal-receipt/v1",
      receipt_id: "omptask_" + digest({ key, requestDigest }).slice(7, 47),
      run_id: identity("run id", request.runId),
      task_id: identity("task id", request.taskId),
      job_id: identity("job id", request.jobId),
      attempt_id: identity("attempt id", request.attemptId),
      generation,
      state: request.state,
      result_ref: request.resultRef ? identity("result ref", request.resultRef) : "",
      error_code: request.errorCode ? identity("error code", request.errorCode) : "",
      replayed: false,
      created_at: new Date().toISOString(),
    };
    if (receipt.state === "succeeded" && !receipt.result_ref) {
      throw new Error("successful task terminal receipt requires a result ref");
    }
    if (receipt.state === "failed" && !receipt.error_code) {
      throw new Error("failed task terminal receipt requires an error code");
    }
    this.terminals.set(key, receipt);
    this.requestDigests.set(key, requestDigest);
    return structuredClone(receipt);
  }

  snapshot(): JsonObject {
    return {
      schema: "zyra.omp-task-terminal-runtime/v1",
      receipt_count: this.terminals.size,
      receipts: [...this.terminals.values()] as unknown as JsonValue,
      canonical_task_owner: "python.TaskState+typescript.AgentTaskRuntime",
      process_local_receipts_only: true,
    };
  }
}

export class WorktreeMergeReceiptRuntime {
  private readonly receipts = new Map<string, WorktreeMergeReceipt>();
  private readonly requestDigests = new Map<string, string>();

  settle(request: WorktreeMergeRequest): WorktreeMergeReceipt {
    const key = digest({ runId: request.runId, taskId: request.taskId, worktreeId: request.worktreeId, idempotencyKey: request.idempotencyKey });
    const normalizedPaths = uniqueIdentities("worktree path", request.changedPaths).map((item) => logicalPath("worktree path", item)).sort();
    const conflictPaths = uniqueIdentities("worktree conflict path", request.conflictPaths).map((item) => logicalPath("worktree conflict path", item)).sort();
    const requestDigest = digest({ ...request, changedPaths: normalizedPaths, conflictPaths });
    const replay = this.receipts.get(key);
    if (replay) {
      if (this.requestDigests.get(key) !== requestDigest) {
        throw new Error("worktree merge idempotency key was reused with different content");
      }
      return { ...structuredClone(replay), replayed: true };
    }
    const receipt: WorktreeMergeReceipt = {
      schema: "zyra.omp-worktree-merge-receipt/v1",
      receipt_id: "ompmerge_" + digest({ key, requestDigest }).slice(7, 47),
      run_id: identity("run id", request.runId),
      task_id: identity("task id", request.taskId),
      worktree_id: identity("worktree id", request.worktreeId),
      base_revision: identity("base revision", request.baseRevision),
      head_revision: identity("head revision", request.headRevision),
      target_revision: identity("target revision", request.targetRevision),
      changed_paths: normalizedPaths,
      conflict_paths: conflictPaths,
      state: conflictPaths.length ? "conflicted" : "committed",
      side_effect_key: digest({ taskId: request.taskId, worktreeId: request.worktreeId, targetRevision: request.targetRevision }),
      replayed: false,
      created_at: new Date().toISOString(),
    };
    if (!normalizedPaths.length && !conflictPaths.length) {
      throw new Error("worktree merge receipt requires a changed or conflict path");
    }
    this.receipts.set(key, receipt);
    this.requestDigests.set(key, requestDigest);
    return structuredClone(receipt);
  }

  snapshot(): JsonObject {
    return {
      schema: "zyra.omp-worktree-merge-runtime/v1",
      receipt_count: this.receipts.size,
      receipts: [...this.receipts.values()] as unknown as JsonValue,
      canonical_workspace_owner: "python.WorkspaceManagerRuntime",
      committed_side_effect_replay: false,
    };
  }
}

export class OmpContinuityReceiptRuntime {
  readonly sessions = new AppendOnlySessionContinuityRuntime();
  readonly tasks = new TaskTerminalReceiptRuntime();
  readonly worktrees = new WorktreeMergeReceiptRuntime();

  snapshot(): JsonObject {
    return {
      schema: "zyra.omp-continuity-receipt-runtime/v1",
      sessions: this.sessions.snapshot(),
      tasks: this.tasks.snapshot(),
      worktrees: this.worktrees.snapshot(),
      canonical_checkpoint_owner: "python.RecoveryPlanStore",
      applied_recovery_owner: "python.RecoveryDecisionRuntime",
      supplementary_only: true,
    };
  }
}
