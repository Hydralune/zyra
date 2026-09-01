import { EventDurability, EventEffect, MessageIntent, SenderKind, TrustLevel, type RuntimeEventDraft } from "./contracts.ts";
import { boundedText, digestJson, stableId, utcNow, type JsonValue } from "./canonical.ts";
import { EventSpineErrorCode, ProjectorError } from "./errors.ts";

export const StreamKind = {
  TEXT: "text",
  REASONING: "reasoning",
  TOOL_INPUT: "tool_input",
} as const;
export type StreamKindValue = (typeof StreamKind)[keyof typeof StreamKind];

export const StreamPhase = {
  NEW: "new",
  STARTED: "started",
  ENDED: "ended",
  ABORTED: "aborted",
} as const;
export type StreamPhaseValue = (typeof StreamPhase)[keyof typeof StreamPhase];

const PRODUCT_TEXT_CHUNK_LIMIT_BYTES = 1_024;

function presentationText(kind: StreamKindValue, value: string): string | undefined {
  return kind === StreamKind.TEXT && Buffer.byteLength(value, "utf8") <= PRODUCT_TEXT_CHUNK_LIMIT_BYTES
    ? value
    : undefined;
}

export interface StreamIdentity {
  aggregateId: string;
  runId: string;
  sessionId?: string;
  taskId: string;
  workerId?: string;
  assistantMessageId: string;
  streamId: string;
  kind: StreamKindValue;
  correlationId: string;
  toolCallId?: string;
}

export interface StreamChunk {
  index: number;
  delta: string;
  byteLength: number;
  digest: string;
  createdAt: string;
}

export interface StreamFoldSnapshot {
  identity: StreamIdentity;
  phase: StreamPhaseValue;
  startEventId?: string;
  endEventId?: string;
  chunks: number;
  totalBytes: number;
  finalDigest?: string;
  finalText?: string;
  interrupted: boolean;
  updatedAt: string;
}

function eventPrefix(kind: StreamKindValue): string {
  if (kind === StreamKind.TOOL_INPUT) return "runtime.tool.input";
  return `runtime.${kind}`;
}

export class StreamFold {
  readonly identity: StreamIdentity;
  readonly retainLiveText: boolean;
  private phase: StreamPhaseValue = StreamPhase.NEW;
  private startEventId?: string;
  private endEventId?: string;
  private chunks: StreamChunk[] = [];
  private totalBytes = 0;
  private finalDigest?: string;
  private finalText?: string;
  private interrupted = false;
  private updatedAt = utcNow();

  constructor(identity: StreamIdentity, retainLiveText = true) {
    this.identity = identity;
    this.retainLiveText = retainLiveText;
  }

  start(createdAt = utcNow()): RuntimeEventDraft {
    this.expect(StreamPhase.NEW, "stream already started");
    this.phase = StreamPhase.STARTED;
    this.startEventId = stableId("evt", this.identity.aggregateId, this.identity.streamId, "start");
    this.updatedAt = createdAt;
    return this.draft(`${eventPrefix(this.identity.kind)}.started`, this.startEventId, undefined, {
      stream_id: this.identity.streamId,
      assistant_message_id: this.identity.assistantMessageId,
      tool_name: this.identity.kind === StreamKind.TOOL_INPUT ? "unknown" : undefined,
    }, EventDurability.DURABLE, EventEffect.NON_EFFECTIVE, createdAt);
  }

  delta(value: string, createdAt = utcNow()): RuntimeEventDraft {
    this.expect(StreamPhase.STARTED, "stream delta requires active stream");
    const chunk: StreamChunk = {
      index: this.chunks.length,
      delta: this.retainLiveText ? value : "",
      byteLength: Buffer.byteLength(value, "utf8"),
      digest: digestJson(value),
      createdAt,
    };
    this.chunks.push(chunk);
    this.totalBytes += chunk.byteLength;
    this.updatedAt = createdAt;
    return this.draft(`${eventPrefix(this.identity.kind)}.delta`, stableId("live", this.identity.streamId, chunk.index, chunk.digest), this.startEventId, {
      stream_id: this.identity.streamId,
      assistant_message_id: this.identity.assistantMessageId,
      chunk_index: chunk.index,
      delta: value,
      presentation_text: presentationText(this.identity.kind, value),
      delta_digest: chunk.digest,
      delta_bytes: chunk.byteLength,
      tool_name: this.identity.kind === StreamKind.TOOL_INPUT ? "unknown" : undefined,
    }, EventDurability.LIVE_ONLY, EventEffect.NON_EFFECTIVE, createdAt);
  }

  end(finalValue?: string, createdAt = utcNow()): RuntimeEventDraft {
    this.expect(StreamPhase.STARTED, "stream end requires active stream");
    const selected = finalValue ?? (this.retainLiveText ? this.chunks.map((chunk) => chunk.delta).join("") : "");
    this.phase = StreamPhase.ENDED;
    this.finalText = selected;
    this.finalDigest = digestJson(selected);
    this.endEventId = stableId("evt", this.identity.aggregateId, this.identity.streamId, "end", this.finalDigest);
    this.updatedAt = createdAt;
    return this.draft(`${eventPrefix(this.identity.kind)}.ended`, this.endEventId, this.startEventId, {
      stream_id: this.identity.streamId,
      assistant_message_id: this.identity.assistantMessageId,
      final_text: selected,
      presentation_text: presentationText(this.identity.kind, selected),
      final_digest: this.finalDigest,
      chunk_count: this.chunks.length,
      total_bytes: this.totalBytes,
      interrupted: false,
      tool_name: this.identity.kind === StreamKind.TOOL_INPUT ? "unknown" : undefined,
    }, EventDurability.DURABLE, this.identity.kind === StreamKind.REASONING ? EventEffect.NON_EFFECTIVE : EventEffect.EFFECTIVE, createdAt);
  }

  abort(reason: string, completedPrefix = "", createdAt = utcNow()): RuntimeEventDraft {
    this.expect(StreamPhase.STARTED, "stream abort requires active stream");
    this.interrupted = true;
    this.phase = StreamPhase.ABORTED;
    this.finalText = completedPrefix;
    this.finalDigest = digestJson({ completedPrefix, interrupted: true, reason });
    this.endEventId = stableId("evt", this.identity.aggregateId, this.identity.streamId, "aborted", this.finalDigest);
    this.updatedAt = createdAt;
    return this.draft(`${eventPrefix(this.identity.kind)}.ended`, this.endEventId, this.startEventId, {
      stream_id: this.identity.streamId,
      assistant_message_id: this.identity.assistantMessageId,
      final_text: completedPrefix,
      presentation_text: presentationText(this.identity.kind, completedPrefix),
      final_digest: this.finalDigest,
      chunk_count: this.chunks.length,
      total_bytes: this.totalBytes,
      interrupted: true,
      interruption_reason: boundedText(reason, 512),
      tool_name: this.identity.kind === StreamKind.TOOL_INPUT ? "unknown" : undefined,
    }, EventDurability.DURABLE, EventEffect.NON_EFFECTIVE, createdAt);
  }

  snapshot(): StreamFoldSnapshot {
    return {
      identity: this.identity,
      phase: this.phase,
      startEventId: this.startEventId,
      endEventId: this.endEventId,
      chunks: this.chunks.length,
      totalBytes: this.totalBytes,
      finalDigest: this.finalDigest,
      finalText: this.finalText,
      interrupted: this.interrupted,
      updatedAt: this.updatedAt,
    };
  }

  private draft(
    eventType: string,
    eventId: string,
    causationId: string | undefined,
    inline: Readonly<Record<string, JsonValue | undefined>>,
    durability: "durable" | "live_only",
    effect: "effective" | "non_effective",
    createdAt: string,
  ): RuntimeEventDraft {
    const cleanInline: Record<string, JsonValue> = {};
    for (const [key, value] of Object.entries(inline)) if (value !== undefined) cleanInline[key] = value;
    return {
      eventId,
      eventType,
      aggregateId: this.identity.aggregateId,
      idempotencyKey: `stream:${this.identity.streamId}:${eventType}:${eventId}`,
      correlationId: this.identity.correlationId,
      causationId,
      createdAt,
      durability,
      effect,
      identity: {
        runId: this.identity.runId,
        sessionId: this.identity.sessionId,
        taskId: this.identity.taskId,
        workerId: this.identity.workerId,
        toolCallId: this.identity.toolCallId,
      },
      sender: {
        kind: SenderKind.WORKER,
        id: this.identity.workerId ?? "code-worker",
        capabilityRefs: ["stream.emit"],
      },
      intent: MessageIntent.OBSERVATION,
      summary: `${eventType}: ${this.identity.streamId}`,
      stateDelta: {
        domain: "stream",
        operation: effect === EventEffect.EFFECTIVE ? "transition" : "none",
        path: [this.identity.streamId, "phase"],
        value: this.phase,
        effective: effect === EventEffect.EFFECTIVE,
      },
      provenance: {
        sourceRepository: "opencode",
        sourceModule: "packages/schema/src/session-event.ts",
        migrationRole: "primary",
        producerVersion: "M1-S05C-01",
        trust: TrustLevel.INTERNAL,
        normalizedFrom: "partial-final-stream",
      },
      inline: cleanInline,
      metadata: {
        stream_kind: this.identity.kind,
        live_only_delta: durability === EventDurability.LIVE_ONLY,
        final_replay_boundary: eventType.endsWith(".ended"),
      },
    };
  }

  private expect(expected: StreamPhaseValue, message: string): void {
    if (this.phase === expected) return;
    throw new ProjectorError(EventSpineErrorCode.STREAM_LIFECYCLE_VIOLATION, message, {
      stream_id: this.identity.streamId,
      stream_kind: this.identity.kind,
      expected,
      actual: this.phase,
    });
  }
}
