import { randomUUID } from "node:crypto";

import type { JsonObject } from "../contracts.ts";
import type { WatchdogObservation, WatchdogRefs } from "./runtime.ts";

export type SupplementaryObservationEmitter = (
  observerId: string,
  observation: WatchdogObservation,
) => Promise<void>;

export type ToolExecutionPhase =
  | "armed"
  | "running"
  | "timed_out"
  | "settled"
  | "cancelled"
  | "failed";

export type EffectPhase = "prepared" | "running" | "committed" | "failed" | "cancelled";

export interface ToolExecutionBindingInput {
  refs: WatchdogRefs;
  generation: number;
  deadlineMs: number;
  batchId: string;
  batchIndex: number;
  batchSize: number;
  executionMode: string;
  interruptible: boolean;
  metadata?: JsonObject;
}

export interface ToolEffectInput {
  effectId: string;
  idempotencyKey: string;
  effectKind: string;
  targetDigest: string;
  metadata?: JsonObject;
}

export interface ToolEffectReceipt {
  effectId: string;
  idempotencyKey: string;
  phase: EffectPhase;
  committed: boolean;
  duplicate: boolean;
  resultDigest: string;
  revision: number;
  metadata: JsonObject;
}

export interface ToolSettlementReceipt {
  toolCallId: string;
  generation: number;
  resultId: string;
  phase: ToolExecutionPhase;
  accepted: boolean;
  lateAfterTimeout: boolean;
  duplicate: boolean;
  committedEffectIds: string[];
  skippedSiblingIds: string[];
  revision: number;
}

interface EffectRecord {
  effectId: string;
  idempotencyKey: string;
  effectKind: string;
  targetDigest: string;
  phase: EffectPhase;
  resultDigest: string;
  revision: number;
  metadata: JsonObject;
}

interface ToolExecutionRecord {
  refs: WatchdogRefs;
  generation: number;
  deadlineMs: number;
  startedAtMs: number;
  deadlineAtMs: number;
  batchId: string;
  batchIndex: number;
  batchSize: number;
  executionMode: string;
  interruptible: boolean;
  phase: ToolExecutionPhase;
  abortController: AbortController;
  timeout: NodeJS.Timeout | undefined;
  terminalResultId: string;
  terminalOk: boolean | null;
  timeoutObservationId: string;
  revision: number;
  effects: Map<string, EffectRecord>;
  effectKeys: Map<string, string>;
  lateResultIds: string[];
  duplicateResultIds: string[];
  skippedSiblingIds: string[];
  metadata: JsonObject;
}

function runtimeId(prefix: string): string {
  return prefix + "_" + randomUUID().replaceAll("-", "");
}

function nowIso(): string {
  return new Date().toISOString();
}

function requireIdentity(name: string, value: string): string {
  const selected = value.trim();
  if (selected.length === 0) throw new Error(name + " must not be empty");
  return selected;
}

function cloneJson(value: JsonObject | undefined): JsonObject {
  return structuredClone(value ?? {});
}

/**
 * OMP-derived tool deadline and side-effect fence.
 *
 * The source mechanism is the agent-loop split between interruptible and
 * non-interruptible tools plus terminal result synthesis after abort. Zyra's
 * adaptation adds explicit generation, idempotency and committed-effect
 * receipts so a retry cannot repeat a side effect that completed before a
 * provider/tool deadline became visible.
 */
export class ToolExecutionSupervisor {
  readonly sourceRevision = "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca";
  readonly migrationMode = "cropped_same_language";
  readonly observerId = "ts-tool-deadline";

  #records = new Map<string, ToolExecutionRecord>();
  #effectKeys = new Map<string, { toolCallId: string; effectId: string }>();
  #emit: SupplementaryObservationEmitter;
  #now: () => number;
  #armedCount = 0;
  #timeoutCount = 0;
  #acceptedResultCount = 0;
  #lateResultCount = 0;
  #duplicateResultCount = 0;
  #committedEffectCount = 0;
  #duplicateEffectCount = 0;

  constructor(emit: SupplementaryObservationEmitter, now: () => number = () => Date.now()) {
    this.#emit = emit;
    this.#now = now;
  }

  arm(input: ToolExecutionBindingInput): { signal: AbortSignal; leaseToken: string } {
    const toolCallId = requireIdentity("toolCallId", input.refs.toolCallId);
    requireIdentity("toolName", input.refs.toolName);
    requireIdentity("runId", input.refs.runId);
    requireIdentity("taskId", input.refs.taskId);
    if (!Number.isSafeInteger(input.generation) || input.generation < 0) {
      throw new Error("tool generation must be a non-negative integer");
    }
    if (!Number.isSafeInteger(input.deadlineMs) || input.deadlineMs < 1) {
      throw new Error("tool deadline must be a positive integer");
    }
    const current = this.#records.get(toolCallId);
    if (current !== undefined) {
      if (input.generation < current.generation) {
        throw new Error("stale tool generation cannot replace current execution");
      }
      if (input.generation === current.generation && !this.#terminal(current.phase)) {
        throw new Error("tool generation already has an active execution");
      }
      this.#clearTimer(current);
    }
    const startedAtMs = this.#now();
    const leaseToken = runtimeId("ts_tool_lease");
    const abortController = new AbortController();
    const record: ToolExecutionRecord = {
      refs: structuredClone(input.refs),
      generation: input.generation,
      deadlineMs: input.deadlineMs,
      startedAtMs,
      deadlineAtMs: startedAtMs + input.deadlineMs,
      batchId: requireIdentity("batchId", input.batchId),
      batchIndex: input.batchIndex,
      batchSize: input.batchSize,
      executionMode: requireIdentity("executionMode", input.executionMode),
      interruptible: input.interruptible,
      phase: "armed",
      abortController,
      timeout: undefined,
      terminalResultId: "",
      terminalOk: null,
      timeoutObservationId: "",
      revision: 1,
      effects: new Map(),
      effectKeys: new Map(),
      lateResultIds: [],
      duplicateResultIds: [],
      skippedSiblingIds: [],
      metadata: { ...cloneJson(input.metadata), lease_token: leaseToken },
    };
    record.timeout = setTimeout(() => {
      void this.expire(toolCallId, input.generation, "host_deadline_elapsed");
    }, input.deadlineMs);
    this.#records.set(toolCallId, record);
    this.#armedCount += 1;
    return { signal: abortController.signal, leaseToken };
  }

  start(toolCallId: string, generation: number): void {
    const record = this.#require(toolCallId, generation);
    if (record.phase !== "armed") {
      throw new Error("tool execution can start only from armed phase");
    }
    record.phase = "running";
    record.revision += 1;
  }

  prepareEffect(toolCallId: string, generation: number, input: ToolEffectInput): ToolEffectReceipt {
    const record = this.#requireActive(toolCallId, generation);
    requireIdentity("effectId", input.effectId);
    const idempotencyKey = requireIdentity("idempotencyKey", input.idempotencyKey);
    requireIdentity("effectKind", input.effectKind);
    requireIdentity("targetDigest", input.targetDigest);
    const global = this.#effectKeys.get(idempotencyKey);
    if (global !== undefined) {
      const owner = this.#records.get(global.toolCallId);
      const prior = owner?.effects.get(global.effectId);
      if (prior === undefined) throw new Error("effect idempotency index is corrupt");
      this.#duplicateEffectCount += 1;
      return this.#effectReceipt(prior, true);
    }
    const existing = record.effects.get(input.effectId);
    if (existing !== undefined) {
      if (existing.idempotencyKey !== idempotencyKey) {
        throw new Error("effectId cannot be rebound to another idempotency key");
      }
      this.#duplicateEffectCount += 1;
      return this.#effectReceipt(existing, true);
    }
    const effect: EffectRecord = {
      effectId: input.effectId,
      idempotencyKey,
      effectKind: input.effectKind,
      targetDigest: input.targetDigest,
      phase: "prepared",
      resultDigest: "",
      revision: 1,
      metadata: cloneJson(input.metadata),
    };
    record.effects.set(effect.effectId, effect);
    record.effectKeys.set(idempotencyKey, effect.effectId);
    this.#effectKeys.set(idempotencyKey, { toolCallId, effectId: effect.effectId });
    record.revision += 1;
    return this.#effectReceipt(effect, false);
  }

  beginEffect(toolCallId: string, generation: number, effectId: string): ToolEffectReceipt {
    const record = this.#requireActive(toolCallId, generation);
    const effect = this.#requireEffect(record, effectId);
    if (effect.phase === "committed") return this.#effectReceipt(effect, true);
    if (effect.phase !== "prepared") {
      throw new Error("effect can begin only from prepared phase");
    }
    effect.phase = "running";
    effect.revision += 1;
    record.revision += 1;
    return this.#effectReceipt(effect, false);
  }

  commitEffect(
    toolCallId: string,
    generation: number,
    effectId: string,
    resultDigest: string,
    metadata: JsonObject = {},
  ): ToolEffectReceipt {
    const record = this.#require(toolCallId, generation);
    const effect = this.#requireEffect(record, effectId);
    if (effect.phase === "committed") {
      if (resultDigest && effect.resultDigest !== resultDigest) {
        throw new Error("committed effect replay carries a different result digest");
      }
      this.#duplicateEffectCount += 1;
      return this.#effectReceipt(effect, true);
    }
    if (effect.phase !== "running" && effect.phase !== "prepared") {
      throw new Error("failed or cancelled effect cannot commit");
    }
    effect.phase = "committed";
    effect.resultDigest = requireIdentity("resultDigest", resultDigest);
    effect.metadata = { ...effect.metadata, ...cloneJson(metadata), committed_at: nowIso() };
    effect.revision += 1;
    record.revision += 1;
    this.#committedEffectCount += 1;
    return this.#effectReceipt(effect, false);
  }

  failEffect(
    toolCallId: string,
    generation: number,
    effectId: string,
    errorCode: string,
  ): ToolEffectReceipt {
    const record = this.#require(toolCallId, generation);
    const effect = this.#requireEffect(record, effectId);
    if (effect.phase === "committed") return this.#effectReceipt(effect, true);
    effect.phase = "failed";
    effect.metadata = { ...effect.metadata, error_code: requireIdentity("errorCode", errorCode) };
    effect.revision += 1;
    record.revision += 1;
    return this.#effectReceipt(effect, false);
  }

  async expire(toolCallId: string, generation: number, reasonCode: string): Promise<boolean> {
    const record = this.#records.get(toolCallId);
    if (record === undefined || record.generation !== generation || this.#terminal(record.phase)) return false;
    record.phase = "timed_out";
    record.revision += 1;
    record.timeoutObservationId = runtimeId("ts_tool_timeout_observation");
    this.#clearTimer(record);
    if (record.interruptible && !record.abortController.signal.aborted) {
      record.abortController.abort(new Error("tool deadline expired: " + reasonCode));
    }
    for (const effect of record.effects.values()) {
      if (effect.phase === "prepared" || effect.phase === "running") {
        effect.phase = "cancelled";
        effect.metadata = { ...effect.metadata, timeout_reason_code: reasonCode };
        effect.revision += 1;
      }
    }
    this.#timeoutCount += 1;
    const elapsedMs = Math.max(0, this.#now() - record.startedAtMs);
    await this.#emit(this.observerId, {
      observationId: record.timeoutObservationId,
      observerId: this.observerId,
      category: "tool",
      code: "tool_timeout",
      status: "timed_out",
      summary: "Tool execution crossed its generation-fenced host deadline.",
      errorType: "ToolTimeoutError",
      retryable: true,
      terminal: true,
      elapsedMs,
      deadlineMs: record.deadlineMs,
      statusCode: null,
      refs: {
        ...structuredClone(record.refs),
        observationId: record.timeoutObservationId,
        sourceStateRevision: record.revision,
      },
      details: {
        ...record.metadata,
        reason_code: reasonCode,
        generation,
        batch_id: record.batchId,
        batch_index: record.batchIndex,
        batch_size: record.batchSize,
        execution_mode: record.executionMode,
        interruptible: record.interruptible,
        abort_dispatched: record.abortController.signal.aborted,
        committed_effect_ids: this.#effectsByPhase(record, "committed"),
        cancelled_effect_ids: this.#effectsByPhase(record, "cancelled"),
        committed_effects_may_repeat: false,
      },
      observedAt: nowIso(),
    });
    return true;
  }

  settle(
    toolCallId: string,
    generation: number,
    resultId: string,
    ok: boolean,
  ): ToolSettlementReceipt {
    const record = this.#require(toolCallId, generation);
    requireIdentity("resultId", resultId);
    if (record.phase === "timed_out") {
      if (!record.lateResultIds.includes(resultId)) record.lateResultIds.push(resultId);
      this.#lateResultCount += 1;
      record.revision += 1;
      return this.#settlement(record, resultId, false, true, false);
    }
    if (record.terminalResultId) {
      if (!record.duplicateResultIds.includes(resultId)) record.duplicateResultIds.push(resultId);
      this.#duplicateResultCount += 1;
      record.revision += 1;
      return this.#settlement(record, resultId, false, false, true);
    }
    if (record.phase === "cancelled") {
      return this.#settlement(record, resultId, false, false, false);
    }
    record.terminalResultId = resultId;
    record.terminalOk = ok;
    record.phase = ok ? "settled" : "failed";
    record.revision += 1;
    this.#clearTimer(record);
    this.#acceptedResultCount += 1;
    return this.#settlement(record, resultId, true, false, false);
  }

  markSkippedSiblings(
    batchId: string,
    triggeringToolCallId: string,
    siblingToolCallIds: string[],
  ): string[] {
    const triggering = this.#records.get(triggeringToolCallId);
    if (triggering === undefined || triggering.batchId !== batchId) {
      throw new Error("triggering tool is outside batch scope");
    }
    const skipped: string[] = [];
    for (const siblingId of siblingToolCallIds) {
      if (siblingId === triggeringToolCallId) continue;
      const sibling = this.#records.get(siblingId);
      if (sibling === undefined || sibling.batchId !== batchId) continue;
      if (this.#terminal(sibling.phase)) continue;
      sibling.phase = "cancelled";
      sibling.revision += 1;
      this.#clearTimer(sibling);
      if (!triggering.skippedSiblingIds.includes(siblingId)) triggering.skippedSiblingIds.push(siblingId);
      skipped.push(siblingId);
    }
    triggering.revision += 1;
    return skipped;
  }

  replayEffect(idempotencyKey: string): ToolEffectReceipt | null {
    const owner = this.#effectKeys.get(idempotencyKey);
    if (owner === undefined) return null;
    const record = this.#records.get(owner.toolCallId);
    const effect = record?.effects.get(owner.effectId);
    return effect === undefined ? null : this.#effectReceipt(effect, true);
  }

  dispose(): void {
    for (const record of this.#records.values()) {
      this.#clearTimer(record);
      if (!this.#terminal(record.phase)) {
        record.phase = "cancelled";
        record.revision += 1;
      }
    }
  }

  snapshot(): JsonObject {
    const records: JsonObject = {};
    for (const [toolCallId, record] of [...this.#records.entries()].sort(([left], [right]) => left.localeCompare(right))) {
      records[toolCallId] = {
        refs: record.refs as unknown as JsonObject,
        generation: record.generation,
        phase: record.phase,
        deadline_ms: record.deadlineMs,
        started_at_ms: record.startedAtMs,
        deadline_at_ms: record.deadlineAtMs,
        batch_id: record.batchId,
        batch_index: record.batchIndex,
        batch_size: record.batchSize,
        interruptible: record.interruptible,
        terminal_result_id: record.terminalResultId,
        terminal_ok: record.terminalOk,
        timeout_observation_id: record.timeoutObservationId,
        committed_effect_ids: this.#effectsByPhase(record, "committed"),
        cancelled_effect_ids: this.#effectsByPhase(record, "cancelled"),
        late_result_ids: [...record.lateResultIds],
        duplicate_result_ids: [...record.duplicateResultIds],
        skipped_sibling_ids: [...record.skippedSiblingIds],
        revision: record.revision,
      };
    }
    return {
      schema: "zyra.typescript-tool-execution-supervisor/v1",
      source_repo: "oh-my-pi",
      source_revision: this.sourceRevision,
      migration_mode: this.migrationMode,
      records,
      counts: {
        armed: this.#armedCount,
        timed_out: this.#timeoutCount,
        accepted_results: this.#acceptedResultCount,
        late_results: this.#lateResultCount,
        duplicate_results: this.#duplicateResultCount,
        committed_effects: this.#committedEffectCount,
        duplicate_effects: this.#duplicateEffectCount,
      },
      committed_effects_may_repeat: false,
      recovery_plan_owner: "M1-S07C",
    };
  }

  #require(toolCallId: string, generation: number): ToolExecutionRecord {
    const record = this.#records.get(toolCallId);
    if (record === undefined) throw new Error("tool execution is not armed: " + toolCallId);
    if (record.generation !== generation) throw new Error("stale tool execution generation");
    return record;
  }

  #requireActive(toolCallId: string, generation: number): ToolExecutionRecord {
    const record = this.#require(toolCallId, generation);
    if (this.#terminal(record.phase)) throw new Error("tool execution is terminal: " + record.phase);
    return record;
  }

  #requireEffect(record: ToolExecutionRecord, effectId: string): EffectRecord {
    const effect = record.effects.get(effectId);
    if (effect === undefined) throw new Error("tool effect is not prepared: " + effectId);
    return effect;
  }

  #effectReceipt(effect: EffectRecord, duplicate: boolean): ToolEffectReceipt {
    return {
      effectId: effect.effectId,
      idempotencyKey: effect.idempotencyKey,
      phase: effect.phase,
      committed: effect.phase === "committed",
      duplicate,
      resultDigest: effect.resultDigest,
      revision: effect.revision,
      metadata: structuredClone(effect.metadata),
    };
  }

  #settlement(
    record: ToolExecutionRecord,
    resultId: string,
    accepted: boolean,
    lateAfterTimeout: boolean,
    duplicate: boolean,
  ): ToolSettlementReceipt {
    return {
      toolCallId: record.refs.toolCallId,
      generation: record.generation,
      resultId,
      phase: record.phase,
      accepted,
      lateAfterTimeout,
      duplicate,
      committedEffectIds: this.#effectsByPhase(record, "committed"),
      skippedSiblingIds: [...record.skippedSiblingIds],
      revision: record.revision,
    };
  }

  #effectsByPhase(record: ToolExecutionRecord, phase: EffectPhase): string[] {
    return [...record.effects.values()]
      .filter((effect) => effect.phase === phase)
      .map((effect) => effect.effectId)
      .sort();
  }

  #clearTimer(record: ToolExecutionRecord): void {
    if (record.timeout !== undefined) {
      clearTimeout(record.timeout);
      record.timeout = undefined;
    }
  }

  #terminal(phase: ToolExecutionPhase): boolean {
    return ["timed_out", "settled", "cancelled", "failed"].includes(phase);
  }
}

export function toolExecutionSupervisorContract(): JsonObject {
  return {
    schema: "zyra.typescript-tool-execution-supervisor-contract/v1",
    source_repo: "oh-my-pi",
    source_revision: "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
    migration_mode: "cropped_same_language",
    source_mechanisms: [
      "agent-loop interruptible abort controller",
      "terminal tool result synthesis",
      "completed post-tool effect preservation",
    ],
    generation_fenced: true,
    deadline_aborts_interruptible_work: true,
    committed_effect_receipt_precedes_retry: true,
    duplicate_committed_effect_blocked: true,
    late_terminal_result_cannot_overwrite_timeout: true,
    recovery_plan_owner: "M1-S07C",
  };
}
