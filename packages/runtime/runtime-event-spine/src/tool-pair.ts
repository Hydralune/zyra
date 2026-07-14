import type { DatabaseSync } from "node:sqlite";
import { EventEffect, MessageIntent, SenderKind, TrustLevel, type RuntimeEventDraft } from "./contracts.ts";
import { digestJson, requireRecord, requireString, stableId, utcNow, type JsonValue } from "./canonical.ts";
import { EventSpineErrorCode, ProjectorError } from "./errors.ts";

export const ToolPairPhase = {
  NEW: "new",
  INPUT_STREAMING: "input_streaming",
  INPUT_READY: "input_ready",
  CALLED: "called",
  RUNNING: "running",
  SUCCEEDED: "succeeded",
  FAILED: "failed",
  CANCELLED: "cancelled",
} as const;
export type ToolPairPhaseValue = (typeof ToolPairPhase)[keyof typeof ToolPairPhase];

export interface ToolPairIdentity {
  aggregateId: string;
  runId: string;
  sessionId?: string;
  taskId: string;
  workerId?: string;
  toolCallId: string;
  toolName: string;
  correlationId: string;
}

export interface ToolInputChunk {
  index: number;
  delta: string;
  createdAt: string;
}

export interface ToolProgressFrame {
  index: number;
  summary: string;
  stateDelta: Readonly<Record<string, JsonValue>>;
  artifactIds: readonly string[];
  createdAt: string;
}

export interface ToolPairSnapshot {
  identity: ToolPairIdentity;
  phase: ToolPairPhaseValue;
  startedEventId?: string;
  inputEndedEventId?: string;
  calledEventId?: string;
  settledEventId?: string;
  inputDigest?: string;
  resultDigest?: string;
  inputChunks: readonly ToolInputChunk[];
  progress: readonly ToolProgressFrame[];
  malformedResult: boolean;
  errorCode?: string;
  updatedAt: string;
}

export interface NormalizedToolResult {
  ok: boolean;
  structured: Readonly<Record<string, JsonValue>>;
  summary: string;
  errorCode?: string;
  malformed: boolean;
  originalDigest: string;
  warnings: readonly string[];
}

export function normalizeThirdPartyToolResult(value: unknown): NormalizedToolResult {
  const warnings: string[] = [];
  const originalDigest = digestJson(value === undefined ? null : value);
  if (value === null || value === undefined) {
    return {
      ok: false,
      structured: { value: null },
      summary: "Tool returned no result.",
      errorCode: "malformed_empty_result",
      malformed: true,
      originalDigest,
      warnings: ["empty_result_normalized"],
    };
  }
  if (typeof value === "string") {
    const text = value.trim();
    if (!text) {
      return {
        ok: false,
        structured: { text: "" },
        summary: "Tool returned an empty string.",
        errorCode: "malformed_empty_string",
        malformed: true,
        originalDigest,
        warnings: ["empty_string_normalized"],
      };
    }
    return {
      ok: true,
      structured: { text },
      summary: text.slice(0, 512),
      malformed: false,
      originalDigest,
      warnings,
    };
  }
  if (typeof value !== "object" || Array.isArray(value)) {
    warnings.push("scalar_result_wrapped");
    return {
      ok: true,
      structured: { value: value as JsonValue },
      summary: String(value).slice(0, 512),
      malformed: true,
      originalDigest,
      warnings,
    };
  }
  const record = requireRecord(value, "tool_result");
  const explicitError = record.error;
  const status = typeof record.status === "string" ? record.status.toLowerCase() : "";
  const ok = record.ok === false || explicitError !== undefined || ["error", "failed", "cancelled"].includes(status) ? false : true;
  let summary = "";
  for (const key of ["summary", "message", "text", "output"]) {
    const selected = record[key];
    if (typeof selected === "string" && selected.trim()) {
      summary = selected.trim().slice(0, 512);
      break;
    }
  }
  if (!summary) summary = ok ? "Tool completed with structured result." : "Tool failed with structured error.";
  const errorCode = !ok
    ? typeof record.error_code === "string"
      ? record.error_code
      : typeof explicitError === "object" && explicitError !== null && typeof (explicitError as Record<string, unknown>).code === "string"
        ? String((explicitError as Record<string, unknown>).code)
        : "third_party_tool_failure"
    : undefined;
  if (!("ok" in record) && !status) warnings.push("implicit_success_normalized");
  return {
    ok,
    structured: record,
    summary,
    errorCode,
    malformed: warnings.length > 0,
    originalDigest,
    warnings,
  };
}

export class ToolPairBuilder {
  readonly identity: ToolPairIdentity;
  private phase: ToolPairPhaseValue = ToolPairPhase.NEW;
  private startedEventId?: string;
  private inputEndedEventId?: string;
  private calledEventId?: string;
  private settledEventId?: string;
  private inputDigest?: string;
  private resultDigest?: string;
  private readonly chunks: ToolInputChunk[] = [];
  private readonly progressFrames: ToolProgressFrame[] = [];
  private malformedResult = false;
  private errorCode?: string;
  private updatedAt = utcNow();

  constructor(identity: ToolPairIdentity) {
    this.identity = identity;
  }

  startInput(eventId = stableId("evt", this.identity.toolCallId, "input-start"), createdAt = utcNow()): RuntimeEventDraft {
    this.expect([ToolPairPhase.NEW], "tool input can only start once");
    this.phase = ToolPairPhase.INPUT_STREAMING;
    this.startedEventId = eventId;
    this.updatedAt = createdAt;
    return this.draft("runtime.tool.input.started", eventId, undefined, {
      tool_name: this.identity.toolName,
      tool_call_id: this.identity.toolCallId,
      stream_id: `tool-input:${this.identity.toolCallId}`,
    }, EventEffect.EFFECTIVE, createdAt);
  }

  inputDelta(delta: string, createdAt = utcNow()): RuntimeEventDraft {
    this.expect([ToolPairPhase.INPUT_STREAMING], "tool input delta requires active input stream");
    const chunk: ToolInputChunk = { index: this.chunks.length, delta, createdAt };
    this.chunks.push(chunk);
    this.updatedAt = createdAt;
    return this.draft(
      "runtime.tool.input.delta",
      stableId("live", this.identity.toolCallId, "input-delta", chunk.index, digestJson(delta)),
      this.startedEventId,
      {
        tool_name: this.identity.toolName,
        tool_call_id: this.identity.toolCallId,
        stream_id: `tool-input:${this.identity.toolCallId}`,
        delta,
        chunk_index: chunk.index,
      },
      EventEffect.NON_EFFECTIVE,
      createdAt,
      "live_only",
    );
  }

  endInput(input: Readonly<Record<string, JsonValue>>, eventId = stableId("evt", this.identity.toolCallId, "input-end"), createdAt = utcNow()): RuntimeEventDraft {
    this.expect([ToolPairPhase.INPUT_STREAMING], "tool input end requires active input stream");
    this.phase = ToolPairPhase.INPUT_READY;
    this.inputEndedEventId = eventId;
    this.inputDigest = digestJson(input);
    this.updatedAt = createdAt;
    return this.draft("runtime.tool.input.ended", eventId, this.startedEventId, {
      tool_name: this.identity.toolName,
      tool_call_id: this.identity.toolCallId,
      stream_id: `tool-input:${this.identity.toolCallId}`,
      input_digest: this.inputDigest,
      chunk_count: this.chunks.length,
    }, EventEffect.EFFECTIVE, createdAt);
  }

  called(input: Readonly<Record<string, JsonValue>>, providerExecuted = false, eventId = stableId("evt", this.identity.toolCallId, "called"), createdAt = utcNow()): RuntimeEventDraft {
    this.expect([ToolPairPhase.NEW, ToolPairPhase.INPUT_READY], "tool call cannot be emitted from current phase");
    this.phase = ToolPairPhase.CALLED;
    this.calledEventId = eventId;
    this.inputDigest = this.inputDigest ?? digestJson(input);
    this.updatedAt = createdAt;
    return this.draft("runtime.tool.called", eventId, this.inputEndedEventId, {
      tool_name: this.identity.toolName,
      tool_call_id: this.identity.toolCallId,
      input_digest: this.inputDigest,
      provider_executed: providerExecuted,
      input,
    }, EventEffect.EFFECTIVE, createdAt);
  }

  progress(summary: string, stateDelta: Readonly<Record<string, JsonValue>> = {}, artifactIds: readonly string[] = [], createdAt = utcNow()): RuntimeEventDraft {
    this.expect([ToolPairPhase.CALLED, ToolPairPhase.RUNNING], "tool progress requires unsettled call");
    this.phase = ToolPairPhase.RUNNING;
    const frame: ToolProgressFrame = {
      index: this.progressFrames.length,
      summary: summary.slice(0, 512),
      stateDelta,
      artifactIds: [...artifactIds],
      createdAt,
    };
    this.progressFrames.push(frame);
    this.updatedAt = createdAt;
    return this.draft("runtime.tool.progress", stableId("evt", this.identity.toolCallId, "progress", frame.index, digestJson(frame)), this.calledEventId, {
      tool_name: this.identity.toolName,
      tool_call_id: this.identity.toolCallId,
      progress_index: frame.index,
      progress_summary: frame.summary,
      state_delta_digest: digestJson(stateDelta),
      artifact_ids: [...artifactIds],
    }, EventEffect.NON_EFFECTIVE, createdAt);
  }

  settle(value: unknown, eventId?: string, createdAt = utcNow()): RuntimeEventDraft {
    this.expect([ToolPairPhase.CALLED, ToolPairPhase.RUNNING], "tool result requires unsettled call");
    const normalized = normalizeThirdPartyToolResult(value);
    this.phase = normalized.ok ? ToolPairPhase.SUCCEEDED : ToolPairPhase.FAILED;
    this.resultDigest = normalized.originalDigest;
    this.malformedResult = normalized.malformed;
    this.errorCode = normalized.errorCode;
    this.settledEventId = eventId ?? stableId("evt", this.identity.toolCallId, this.phase, this.resultDigest);
    this.updatedAt = createdAt;
    return this.draft(normalized.ok ? "runtime.tool.succeeded" : "runtime.tool.failed", this.settledEventId, this.calledEventId, {
      tool_name: this.identity.toolName,
      tool_call_id: this.identity.toolCallId,
      result_digest: this.resultDigest,
      error_code: normalized.errorCode ?? "",
      malformed_result: normalized.malformed,
      normalization_warnings: [...normalized.warnings],
      structured: normalized.structured,
    }, EventEffect.EFFECTIVE, createdAt);
  }

  cancel(reason: string, eventId = stableId("evt", this.identity.toolCallId, "cancelled"), createdAt = utcNow()): RuntimeEventDraft {
    this.expect([ToolPairPhase.CALLED, ToolPairPhase.RUNNING], "tool cancel requires unsettled call");
    this.phase = ToolPairPhase.CANCELLED;
    this.settledEventId = eventId;
    this.errorCode = "cancelled";
    this.updatedAt = createdAt;
    return this.draft("runtime.tool.cancelled", eventId, this.calledEventId, {
      tool_name: this.identity.toolName,
      tool_call_id: this.identity.toolCallId,
      reason: reason.slice(0, 512),
      result_digest: digestJson({ cancelled: true, reason }),
    }, EventEffect.EFFECTIVE, createdAt);
  }

  snapshot(): ToolPairSnapshot {
    return {
      identity: this.identity,
      phase: this.phase,
      startedEventId: this.startedEventId,
      inputEndedEventId: this.inputEndedEventId,
      calledEventId: this.calledEventId,
      settledEventId: this.settledEventId,
      inputDigest: this.inputDigest,
      resultDigest: this.resultDigest,
      inputChunks: [...this.chunks],
      progress: [...this.progressFrames],
      malformedResult: this.malformedResult,
      errorCode: this.errorCode,
      updatedAt: this.updatedAt,
    };
  }

  private draft(
    eventType: string,
    eventId: string,
    causationId: string | undefined,
    inline: Readonly<Record<string, JsonValue>>,
    effect: "effective" | "non_effective",
    createdAt: string,
    durability: "durable" | "live_only" = "durable",
  ): RuntimeEventDraft {
    return {
      eventId,
      eventType,
      aggregateId: this.identity.aggregateId,
      idempotencyKey: `tool:${this.identity.toolCallId}:${eventType}:${eventId}`,
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
        kind: eventType.includes("succeeded") || eventType.includes("failed") || eventType.includes("progress") ? SenderKind.TOOL : SenderKind.WORKER,
        id: eventType.includes("succeeded") || eventType.includes("failed") || eventType.includes("progress") ? this.identity.toolName : this.identity.workerId ?? "code-worker",
        capabilityRefs: ["tool.lifecycle"],
      },
      intent: eventType.includes("succeeded") || eventType.includes("failed") ? MessageIntent.TOOL_RESULT : eventType.includes("progress") ? MessageIntent.STATUS : MessageIntent.TOOL_CALL,
      summary: `${eventType}: ${this.identity.toolName}`,
      stateDelta: {
        domain: "tool",
        operation: effect === EventEffect.EFFECTIVE ? "transition" : "none",
        path: [this.identity.toolCallId, "phase"],
        value: this.phase,
        effective: effect === EventEffect.EFFECTIVE,
      },
      provenance: {
        sourceRepository: "opencode+oh-my-pi",
        sourceModule: "session-event.ts + packages/agent/src/agent-loop.ts",
        migrationRole: "supplementary",
        producerVersion: "M1-S05C-01",
        trust: TrustLevel.INTERNAL,
        normalizedFrom: "tool-pair",
      },
      inline,
      metadata: { tool_pair_phase: this.phase },
    };
  }

  private expect(allowed: readonly ToolPairPhaseValue[], message: string): void {
    if (allowed.includes(this.phase)) return;
    throw new ProjectorError(EventSpineErrorCode.TOOL_PAIR_VIOLATION, message, {
      tool_call_id: this.identity.toolCallId,
      tool_name: this.identity.toolName,
      phase: this.phase,
      allowed,
    });
  }
}

export function verifyToolPairs(db: DatabaseSync, aggregateId: string): Readonly<Record<string, JsonValue>> {
  const rows = db.prepare(`
    SELECT tool_call_id, tool_name, status, called_event_id, settled_event_id,
           input_digest, result_digest, malformed_result, error_code
    FROM runtime_projected_tools
    WHERE aggregate_id = ?
    ORDER BY tool_call_id
  `).all(aggregateId) as unknown as Array<Record<string, unknown>>;
  const findings: Array<Record<string, JsonValue>> = [];
  for (const row of rows) {
    const status = String(row.status ?? "");
    if (["called", "running"].includes(status)) {
      findings.push({
        code: "unsettled_tool_call",
        tool_call_id: String(row.tool_call_id ?? ""),
        tool_name: String(row.tool_name ?? ""),
        status,
      });
    }
    if (["succeeded", "failed", "cancelled"].includes(status) && !row.settled_event_id) {
      findings.push({
        code: "terminal_tool_missing_settlement_event",
        tool_call_id: String(row.tool_call_id ?? ""),
        status,
      });
    }
  }
  return {
    schema: "zyra.tool-pair-audit/v1",
    aggregate_id: aggregateId,
    tool_call_count: rows.length,
    finding_count: findings.length,
    findings,
    passed: findings.length === 0,
  };
}
