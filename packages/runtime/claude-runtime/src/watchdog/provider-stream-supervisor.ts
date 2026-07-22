import { createHash, randomUUID } from "node:crypto";

import type { JsonObject } from "../contracts.ts";
import type { SupplementaryObservationEmitter } from "./execution-supervisor.ts";
import type { WatchdogObservation, WatchdogRefs } from "./runtime.ts";

export type ProviderStreamPhase =
  | "idle"
  | "streaming"
  | "interrupted"
  | "retryable"
  | "completed"
  | "failed"
  | "stopped";

export interface ProviderStreamInput {
  refs: WatchdogRefs;
  streamId: string;
  generation: number;
  attemptNumber: number;
  maxAttempts: number;
  metadata?: JsonObject;
}

export interface ProviderChunkInput {
  chunkId: string;
  sequence: number;
  contentDigest: string;
  textBytes: number;
  toolCallIds?: string[];
  metadata?: JsonObject;
}

export interface ProviderEffectCommitInput {
  effectId: string;
  idempotencyKey: string;
  toolCallId: string;
  resultDigest: string;
  chunkSequence: number;
  metadata?: JsonObject;
}

export interface ProviderFailureInput {
  errorCode: string;
  errorType: string;
  statusCode?: number | null;
  retryable?: boolean | null;
  terminal?: boolean | null;
  retryAfterMs?: number | null;
  metadata?: JsonObject;
}

export interface ProviderResumeCursor {
  streamId: string;
  generation: number;
  nextAttemptNumber: number;
  lastCommittedChunkSequence: number;
  partialContentDigest: string;
  committedEffectKeys: string[];
  retryAfterMs: number;
  cursorToken: string;
}

interface ProviderChunkRecord {
  chunkId: string;
  sequence: number;
  contentDigest: string;
  textBytes: number;
  toolCallIds: string[];
  metadata: JsonObject;
}

interface ProviderEffectRecord {
  effectId: string;
  idempotencyKey: string;
  toolCallId: string;
  resultDigest: string;
  chunkSequence: number;
  revision: number;
  metadata: JsonObject;
}

interface ProviderStreamRecord {
  refs: WatchdogRefs;
  streamId: string;
  generation: number;
  attemptNumber: number;
  maxAttempts: number;
  phase: ProviderStreamPhase;
  startedAtMs: number;
  updatedAtMs: number;
  revision: number;
  chunks: Map<number, ProviderChunkRecord>;
  chunkIds: Set<string>;
  effects: Map<string, ProviderEffectRecord>;
  effectKeys: Map<string, string>;
  lastSequence: number;
  partialContentDigest: string;
  totalTextBytes: number;
  lastErrorCode: string;
  lastStatusCode: number | null;
  retryAfterMs: number;
  observationId: string;
  cursorToken: string;
  metadata: JsonObject;
}

function runtimeId(prefix: string): string {
  return prefix + "_" + randomUUID().replaceAll("-", "");
}

function nowIso(): string {
  return new Date().toISOString();
}

function digestParts(parts: string[]): string {
  const hash = createHash("sha256");
  for (const part of parts) hash.update(part, "utf8").update("\0", "utf8");
  return "sha256:" + hash.digest("hex");
}

function requireIdentity(name: string, value: string): string {
  const selected = value.trim();
  if (!selected) throw new Error(name + " must not be empty");
  return selected;
}

/**
 * OMP-derived provider partial-stream terminalization and retry fence.
 *
 * A stream attempt may fail after assistant content or tool-call arguments
 * have arrived. The supervisor preserves the partial cursor and committed
 * effect keys, then emits one structured provider observation. A retry is
 * accepted only with the exact cursor token and a newer generation, which
 * makes already committed tool effects non-executable.
 */
export class ProviderStreamSupervisor {
  readonly observerId = "ts-provider-terminal";
  readonly sourceRevision = "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca";
  readonly migrationMode = "cropped_same_language";

  #records = new Map<string, ProviderStreamRecord>();
  #providerIndex = new Map<string, string>();
  #globalEffectKeys = new Map<string, { streamId: string; effectId: string }>();
  #emit: SupplementaryObservationEmitter;
  #now: () => number;
  #streamCount = 0;
  #chunkCount = 0;
  #duplicateChunkCount = 0;
  #interruptionCount = 0;
  #resumeCount = 0;
  #effectCommitCount = 0;
  #blockedDuplicateEffectCount = 0;

  constructor(emit: SupplementaryObservationEmitter, now: () => number = () => Date.now()) {
    this.#emit = emit;
    this.#now = now;
  }

  begin(input: ProviderStreamInput): void {
    const streamId = requireIdentity("streamId", input.streamId);
    const providerId = requireIdentity("providerId", input.refs.providerId);
    requireIdentity("runId", input.refs.runId);
    requireIdentity("taskId", input.refs.taskId);
    if (!Number.isSafeInteger(input.generation) || input.generation < 0) {
      throw new Error("provider stream generation must be non-negative");
    }
    if (!Number.isSafeInteger(input.attemptNumber) || input.attemptNumber < 1) {
      throw new Error("provider attemptNumber must be positive");
    }
    if (!Number.isSafeInteger(input.maxAttempts) || input.maxAttempts < input.attemptNumber) {
      throw new Error("provider maxAttempts must cover current attempt");
    }
    const currentId = this.#providerIndex.get(providerId);
    const current = currentId ? this.#records.get(currentId) : undefined;
    if (
      current !== undefined
      && !this.#terminal(current.phase)
      && current.phase !== "interrupted"
      && current.phase !== "retryable"
    ) {
      throw new Error("provider already owns an active stream");
    }
    const now = this.#now();
    const record: ProviderStreamRecord = {
      refs: structuredClone(input.refs),
      streamId,
      generation: input.generation,
      attemptNumber: input.attemptNumber,
      maxAttempts: input.maxAttempts,
      phase: "streaming",
      startedAtMs: now,
      updatedAtMs: now,
      revision: 1,
      chunks: new Map(),
      chunkIds: new Set(),
      effects: new Map(),
      effectKeys: new Map(),
      lastSequence: 0,
      partialContentDigest: digestParts([]),
      totalTextBytes: 0,
      lastErrorCode: "",
      lastStatusCode: null,
      retryAfterMs: 0,
      observationId: "",
      cursorToken: runtimeId("provider_cursor"),
      metadata: structuredClone(input.metadata ?? {}),
    };
    this.#records.set(streamId, record);
    this.#providerIndex.set(providerId, streamId);
    this.#streamCount += 1;
  }

  acceptChunk(streamId: string, generation: number, input: ProviderChunkInput): boolean {
    const record = this.#requireStreaming(streamId, generation);
    requireIdentity("chunkId", input.chunkId);
    requireIdentity("contentDigest", input.contentDigest);
    if (!Number.isSafeInteger(input.sequence) || input.sequence < 1) {
      throw new Error("provider chunk sequence must be positive");
    }
    if (!Number.isSafeInteger(input.textBytes) || input.textBytes < 0) {
      throw new Error("provider chunk textBytes must be non-negative");
    }
    if (record.chunkIds.has(input.chunkId)) {
      this.#duplicateChunkCount += 1;
      return false;
    }
    if (input.sequence <= record.lastSequence) {
      throw new Error("provider chunk sequence is stale or reordered");
    }
    const chunk: ProviderChunkRecord = {
      chunkId: input.chunkId,
      sequence: input.sequence,
      contentDigest: input.contentDigest,
      textBytes: input.textBytes,
      toolCallIds: [...new Set(input.toolCallIds ?? [])].sort(),
      metadata: structuredClone(input.metadata ?? {}),
    };
    record.chunks.set(input.sequence, chunk);
    record.chunkIds.add(input.chunkId);
    record.lastSequence = input.sequence;
    record.totalTextBytes += input.textBytes;
    record.partialContentDigest = digestParts(
      [...record.chunks.values()]
        .sort((left, right) => left.sequence - right.sequence)
        .map((value) => value.contentDigest),
    );
    record.updatedAtMs = this.#now();
    record.revision += 1;
    this.#chunkCount += 1;
    return true;
  }

  commitEffect(streamId: string, generation: number, input: ProviderEffectCommitInput): boolean {
    const record = this.#requireStreaming(streamId, generation);
    const idempotencyKey = requireIdentity("idempotencyKey", input.idempotencyKey);
    requireIdentity("effectId", input.effectId);
    requireIdentity("toolCallId", input.toolCallId);
    requireIdentity("resultDigest", input.resultDigest);
    if (!record.chunks.has(input.chunkSequence)) {
      throw new Error("provider effect references an unknown stream chunk");
    }
    const global = this.#globalEffectKeys.get(idempotencyKey);
    if (global !== undefined) {
      const owner = this.#records.get(global.streamId)?.effects.get(global.effectId);
      if (owner === undefined) throw new Error("provider effect key index is corrupt");
      if (owner.resultDigest !== input.resultDigest) {
        throw new Error("duplicate provider effect carries a different result digest");
      }
      this.#blockedDuplicateEffectCount += 1;
      return false;
    }
    const effect: ProviderEffectRecord = {
      effectId: input.effectId,
      idempotencyKey,
      toolCallId: input.toolCallId,
      resultDigest: input.resultDigest,
      chunkSequence: input.chunkSequence,
      revision: 1,
      metadata: structuredClone(input.metadata ?? {}),
    };
    record.effects.set(input.effectId, effect);
    record.effectKeys.set(idempotencyKey, input.effectId);
    this.#globalEffectKeys.set(idempotencyKey, { streamId, effectId: input.effectId });
    record.updatedAtMs = this.#now();
    record.revision += 1;
    this.#effectCommitCount += 1;
    return true;
  }

  shouldExecuteEffect(idempotencyKey: string): boolean {
    return !this.#globalEffectKeys.has(idempotencyKey);
  }

  async interrupt(
    streamId: string,
    generation: number,
    input: ProviderFailureInput,
  ): Promise<ProviderResumeCursor | null> {
    const record = this.#requireStreaming(streamId, generation);
    const errorCode = requireIdentity("errorCode", input.errorCode).toLowerCase();
    const retryable = this.#retryable(input, record);
    const exhausted = record.attemptNumber >= record.maxAttempts;
    const terminal = input.terminal ?? (exhausted || !retryable);
    record.phase = retryable && !exhausted && !terminal ? "retryable" : "failed";
    record.lastErrorCode = errorCode;
    record.lastStatusCode = input.statusCode ?? null;
    record.retryAfterMs = Math.max(0, Math.trunc(input.retryAfterMs ?? 0));
    record.observationId = runtimeId("ts_provider_stream_observation");
    record.updatedAtMs = this.#now();
    record.revision += 1;
    this.#interruptionCount += 1;
    const hasPartialContent = record.chunks.size > 0;
    await this.#emit(this.observerId, this.#observation(record, input, retryable, terminal, hasPartialContent));
    if (!retryable || exhausted || terminal) return null;
    record.phase = "interrupted";
    record.revision += 1;
    return this.#cursor(record);
  }

  resume(
    priorStreamId: string,
    cursor: ProviderResumeCursor,
    input: ProviderStreamInput,
  ): void {
    const prior = this.#records.get(priorStreamId);
    if (prior === undefined) throw new Error("prior provider stream does not exist");
    if (prior.phase !== "interrupted" && prior.phase !== "retryable") {
      throw new Error("provider stream is not resumable");
    }
    if (cursor.cursorToken !== prior.cursorToken) throw new Error("provider resume cursor token mismatch");
    if (input.refs.providerId !== prior.refs.providerId) throw new Error("provider resume cannot change provider identity");
    if (input.refs.runId !== prior.refs.runId || input.refs.taskId !== prior.refs.taskId) {
      throw new Error("provider resume cannot change run/task scope");
    }
    if (input.generation <= prior.generation) throw new Error("provider resume requires a newer generation");
    if (input.attemptNumber !== prior.attemptNumber + 1) throw new Error("provider resume attempt is not contiguous");
    this.begin(input);
    const next = this.#records.get(input.streamId);
    if (next === undefined) throw new Error("provider resume failed to create stream");
    for (const chunk of prior.chunks.values()) {
      next.chunks.set(chunk.sequence, structuredClone(chunk));
      next.chunkIds.add(chunk.chunkId);
    }
    for (const effect of prior.effects.values()) {
      next.effects.set(effect.effectId, structuredClone(effect));
      next.effectKeys.set(effect.idempotencyKey, effect.effectId);
      this.#globalEffectKeys.set(effect.idempotencyKey, {
        streamId: next.streamId,
        effectId: effect.effectId,
      });
    }
    next.lastSequence = prior.lastSequence;
    next.partialContentDigest = prior.partialContentDigest;
    next.totalTextBytes = prior.totalTextBytes;
    next.cursorToken = runtimeId("provider_cursor");
    next.revision += 1;
    prior.phase = "stopped";
    prior.revision += 1;
    this.#providerIndex.set(next.refs.providerId, next.streamId);
    this.#resumeCount += 1;
  }

  complete(streamId: string, generation: number, terminalDigest: string): void {
    const record = this.#requireStreaming(streamId, generation);
    requireIdentity("terminalDigest", terminalDigest);
    record.phase = "completed";
    record.metadata = {
      ...record.metadata,
      terminal_digest: terminalDigest,
      completed_at: nowIso(),
    };
    record.updatedAtMs = this.#now();
    record.revision += 1;
  }

  snapshot(): JsonObject {
    const streams: JsonObject = {};
    for (const [streamId, record] of [...this.#records.entries()].sort(([left], [right]) => left.localeCompare(right))) {
      streams[streamId] = {
        provider_id: record.refs.providerId,
        run_id: record.refs.runId,
        task_id: record.refs.taskId,
        generation: record.generation,
        attempt_number: record.attemptNumber,
        max_attempts: record.maxAttempts,
        phase: record.phase,
        chunk_count: record.chunks.size,
        last_sequence: record.lastSequence,
        partial_content_digest: record.partialContentDigest,
        total_text_bytes: record.totalTextBytes,
        committed_effect_keys: [...record.effectKeys.keys()].sort(),
        last_error_code: record.lastErrorCode,
        last_status_code: record.lastStatusCode,
        retry_after_ms: record.retryAfterMs,
        observation_id: record.observationId,
        revision: record.revision,
      };
    }
    return {
      schema: "zyra.typescript-provider-stream-supervisor/v1",
      source_repo: "oh-my-pi",
      source_revision: this.sourceRevision,
      migration_mode: this.migrationMode,
      streams,
      counts: {
        streams: this.#streamCount,
        chunks: this.#chunkCount,
        duplicate_chunks: this.#duplicateChunkCount,
        interruptions: this.#interruptionCount,
        resumes: this.#resumeCount,
        committed_effects: this.#effectCommitCount,
        blocked_duplicate_effects: this.#blockedDuplicateEffectCount,
      },
      partial_stream_retry_repeats_committed_effect: false,
      canonical_signal_owner: "python.FaultStateStore",
      recovery_plan_owner: "M1-S07C",
    };
  }

  #observation(
    record: ProviderStreamRecord,
    input: ProviderFailureInput,
    retryable: boolean,
    terminal: boolean,
    hasPartialContent: boolean,
  ): WatchdogObservation {
    return {
      observationId: record.observationId,
      observerId: this.observerId,
      category: "provider",
      code: record.lastErrorCode,
      status: "failed",
      summary: "Provider stream ended after a generation-fenced partial response.",
      errorType: input.errorType || "ProviderStreamInterrupted",
      retryable,
      terminal,
      elapsedMs: Math.max(0, record.updatedAtMs - record.startedAtMs),
      deadlineMs: null,
      statusCode: record.lastStatusCode,
      refs: {
        ...structuredClone(record.refs),
        observationId: record.observationId,
        sourceStateRevision: record.revision,
      },
      details: {
        ...structuredClone(record.metadata),
        ...structuredClone(input.metadata ?? {}),
        stream_id: record.streamId,
        generation: record.generation,
        attempt_number: record.attemptNumber,
        max_attempts: record.maxAttempts,
        partial_content_present: hasPartialContent,
        partial_content_digest: record.partialContentDigest,
        last_committed_chunk_sequence: record.lastSequence,
        committed_effect_keys: [...record.effectKeys.keys()].sort(),
        committed_effects_may_repeat: false,
        retry_after_ms: record.retryAfterMs,
      },
      observedAt: nowIso(),
    };
  }

  #cursor(record: ProviderStreamRecord): ProviderResumeCursor {
    return {
      streamId: record.streamId,
      generation: record.generation,
      nextAttemptNumber: record.attemptNumber + 1,
      lastCommittedChunkSequence: record.lastSequence,
      partialContentDigest: record.partialContentDigest,
      committedEffectKeys: [...record.effectKeys.keys()].sort(),
      retryAfterMs: record.retryAfterMs,
      cursorToken: record.cursorToken,
    };
  }

  #retryable(input: ProviderFailureInput, record: ProviderStreamRecord): boolean {
    if (input.retryable !== null && input.retryable !== undefined) return input.retryable;
    if (record.attemptNumber >= record.maxAttempts) return false;
    if ([408, 429, 500, 502, 503, 504, 529].includes(input.statusCode ?? -1)) return true;
    return ["provider_error", "rate_limited", "timeout", "connection_error", "stream_interrupted"].includes(
      input.errorCode.toLowerCase(),
    );
  }

  #requireStreaming(streamId: string, generation: number): ProviderStreamRecord {
    const record = this.#records.get(streamId);
    if (record === undefined) throw new Error("provider stream does not exist: " + streamId);
    if (record.generation !== generation) throw new Error("stale provider stream generation");
    if (record.phase !== "streaming") throw new Error("provider stream is not active: " + record.phase);
    return record;
  }

  #terminal(phase: ProviderStreamPhase): boolean {
    return ["completed", "failed", "stopped"].includes(phase);
  }
}

export function providerStreamSupervisorContract(): JsonObject {
  return {
    schema: "zyra.typescript-provider-stream-supervisor-contract/v1",
    source_repo: "oh-my-pi",
    source_revision: "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
    migration_mode: "cropped_same_language",
    source_mechanisms: [
      "stream interruption after content",
      "abort terminal message preservation",
      "tool result safety after provider failure",
    ],
    partial_cursor_preserved: true,
    retry_generation_fenced: true,
    committed_effect_idempotency_fenced: true,
    duplicate_effect_after_retry: false,
    canonical_signal_owner: "python.FaultStateStore",
    recovery_plan_owner: "M1-S07C",
  };
}
