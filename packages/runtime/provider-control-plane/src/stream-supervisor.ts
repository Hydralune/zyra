import type { JsonRecord, JsonValue } from "./canonical.ts";
import { canonicalize, deepClone, digestJson } from "./canonical.ts";
import type { ProviderDispatchRequest, ProviderRouteLease, ProviderStreamFrame } from "./contracts.ts";
import { ProviderControlPlaneError } from "./errors.ts";

export type ProviderStreamPhase =
  | "created"
  | "response_started"
  | "output_streaming"
  | "terminal"
  | "failed";

export type ProviderStreamRecoveryAction =
  | "accept"
  | "retry_same_route"
  | "change_provider_route"
  | "reconcile_partial_response"
  | "surface_to_operator";

export interface ProviderStreamBudget {
  readonly firstByteMilliseconds: number;
  readonly chunkMilliseconds: number;
  readonly totalMilliseconds: number;
  readonly maximumFrames: number;
  readonly maximumTextCharacters: number;
  readonly maximumThinkingCharacters: number;
  readonly maximumToolCalls: number;
  readonly maximumToolArgumentCharacters: number;
  readonly maximumProviderNotices: number;
}

export interface ProviderToolCallSnapshot {
  readonly key: string;
  readonly toolCallId: string | null;
  readonly toolName: string | null;
  readonly argumentsText: string;
  readonly argumentsDigest: string;
  readonly argumentCharacters: number;
  readonly jsonComplete: boolean;
  readonly parsedArguments: JsonValue | null;
  readonly firstSequence: number;
  readonly lastSequence: number;
  readonly deltaCount: number;
  readonly transactionState: "receiving_arguments" | "arguments_complete";
}

export interface ProviderStreamSnapshot {
  readonly dispatchId: string;
  readonly routeId: string;
  readonly phase: ProviderStreamPhase;
  readonly startedAt: number;
  readonly responseStartedAt: number | null;
  readonly firstOutputAt: number | null;
  readonly lastFrameAt: number | null;
  readonly completedAt: number | null;
  readonly frameCount: number;
  readonly nextSequence: number;
  readonly responseStartCount: number;
  readonly responseEndCount: number;
  readonly textCharacters: number;
  readonly thinkingCharacters: number;
  readonly providerNoticeCount: number;
  readonly outputObserved: boolean;
  readonly sideEffectCandidateObserved: boolean;
  readonly usage: JsonRecord;
  readonly toolCalls: readonly ProviderToolCallSnapshot[];
  readonly terminalProviderEvent: string | null;
  readonly evidenceDigest: string;
}

export interface ProviderStreamCompletion {
  readonly snapshot: ProviderStreamSnapshot;
  readonly recoveryAction: ProviderStreamRecoveryAction;
  readonly replaySafe: boolean;
  readonly accepted: boolean;
  readonly reason: string;
}

export interface ProviderStreamFailureContext {
  readonly bytesSent: number;
  readonly bytesReceived: number;
  readonly providerId: string;
  readonly modelId: string;
  readonly credentialId: string;
}

export interface ProviderStreamSupervisorOptions {
  readonly budget?: Partial<ProviderStreamBudget>;
  readonly now?: () => number;
  readonly recoveryAttempt?: number;
  readonly maximumRecoveryAttempts?: number;
}

const DEFAULT_BUDGET: ProviderStreamBudget = {
  firstByteMilliseconds: 30_000,
  chunkMilliseconds: 30_000,
  totalMilliseconds: 120_000,
  maximumFrames: 100_000,
  maximumTextCharacters: 4_000_000,
  maximumThinkingCharacters: 4_000_000,
  maximumToolCalls: 1_024,
  maximumToolArgumentCharacters: 2_000_000,
  maximumProviderNotices: 10_000,
};

// A provider may emit one normalized frame per output token and additional
// reasoning/usage/terminal frames.  Keep the anti-flood ceiling, but scale it
// with the output budget advertised in the same request.  A fixed 100k limit
// is smaller than the 131k output window used by long benchmark turns and can
// therefore reject a valid provider response before its terminal frame.
const FRAMES_PER_OUTPUT_TOKEN = 4;
const FRAME_BUDGET_OVERHEAD = 1_024;
const MAXIMUM_DYNAMIC_FRAMES = 1_000_000;

interface MutableToolCall {
  readonly key: string;
  toolCallId: string | null;
  toolName: string | null;
  readonly assembler: IncrementalJsonAssembler;
  readonly firstSequence: number;
  lastSequence: number;
  deltaCount: number;
}

interface UsageLeaf {
  readonly path: string;
  readonly value: number;
}

/**
 * Supervises normalized provider frames after dialect decoding. It owns no
 * catalog or route state; its output is evidence consumed by route fallback.
 */
export class ProviderStreamSupervisor {
  private readonly request: ProviderDispatchRequest;
  private readonly lease: ProviderRouteLease;
  private readonly budget: ProviderStreamBudget;
  private readonly now: () => number;
  private readonly startedAt: number;
  private readonly recoveryAttempt: number;
  private readonly maximumRecoveryAttempts: number;
  private phaseValue: ProviderStreamPhase = "created";
  private responseStartedAtValue: number | null = null;
  private firstOutputAtValue: number | null = null;
  private lastFrameAtValue: number | null = null;
  private completedAtValue: number | null = null;
  private frameCountValue = 0;
  private nextSequenceValue = 1;
  private responseStartCountValue = 0;
  private responseEndCountValue = 0;
  private textCharactersValue = 0;
  private thinkingCharactersValue = 0;
  private providerNoticeCountValue = 0;
  private outputObservedValue = false;
  private sideEffectCandidateObservedValue = false;
  private terminalProviderEventValue: string | null = null;
  private usageValue: JsonRecord = {};
  private readonly usageLeaves = new Map<string, number>();
  private readonly toolCalls = new Map<string, MutableToolCall>();
  private readonly toolIndexKeys = new Map<string, string>();
  private currentAnonymousToolKey: string | null = null;
  private failureValue: ProviderControlPlaneError | null = null;
  private readonly observedFrameDigests = new Map<string, string>();

  constructor(
    request: ProviderDispatchRequest,
    lease: ProviderRouteLease,
    options: ProviderStreamSupervisorOptions = {},
  ) {
    this.request = request;
    this.lease = lease;
    this.now = options.now ?? Date.now;
    this.startedAt = this.now();
    this.recoveryAttempt = Math.max(0, Math.trunc(options.recoveryAttempt ?? 0));
    this.maximumRecoveryAttempts = Math.max(1, Math.trunc(options.maximumRecoveryAttempts ?? 1));
    this.budget = normalizeBudget(request, options.budget ?? {});
    this.assertIdentity();
  }

  get phase(): ProviderStreamPhase {
    return this.phaseValue;
  }

  get outputObserved(): boolean {
    return this.outputObservedValue;
  }

  get sideEffectCandidateObserved(): boolean {
    return this.sideEffectCandidateObservedValue;
  }

  get replaySafe(): boolean {
    return !this.sideEffectCandidateObservedValue;
  }

  get failed(): boolean {
    return this.failureValue !== null;
  }

  observe(frames: readonly ProviderStreamFrame[]): void {
    this.assertActive();
    for (const frame of frames) this.observeFrame(frame);
  }

  checkWatchdog(now = this.now()): void {
    this.assertActive();
    const totalElapsed = now - this.startedAt;
    if (this.lastFrameAtValue === null) {
      if (totalElapsed > this.budget.firstByteMilliseconds) {
        throw this.fail("stream_timeout", "provider stream did not produce a first frame", {
          watchdog: "first_byte",
          elapsedMilliseconds: totalElapsed,
          limitMilliseconds: this.budget.firstByteMilliseconds,
        });
      }
      return;
    }
    const chunkElapsed = now - this.lastFrameAtValue;
    if (chunkElapsed > this.budget.chunkMilliseconds) {
      throw this.fail("stream_timeout", "provider stream stalled between frames", {
        watchdog: "chunk",
        elapsedMilliseconds: chunkElapsed,
        limitMilliseconds: this.budget.chunkMilliseconds,
      });
    }
  }

  complete(options: { readonly requireTerminalFrame: boolean }): ProviderStreamCompletion {
    if (this.failureValue !== null) throw this.failureValue;
    if (this.phaseValue !== "terminal") this.checkWatchdog();
    if (this.frameCountValue === 0) {
      throw this.fail("response_protocol_error", "provider response contained no normalized frames", {});
    }
    if (options.requireTerminalFrame && this.responseEndCountValue !== 1) {
      throw this.fail("response_protocol_error", "provider stream ended without one terminal frame", {
        responseEndCount: this.responseEndCountValue,
      });
    }
    for (const call of this.toolCalls.values()) {
      if (!call.toolName) {
        throw this.fail("response_protocol_error", "provider tool call completed without a tool name", {
          toolKey: call.key,
          firstSequence: call.firstSequence,
        });
      }
      const finalized = call.assembler.finalize();
      if (!finalized.complete) {
        throw this.fail("tool_arguments_incomplete", "provider tool arguments ended with incomplete JSON", {
          toolKey: call.key,
          toolCallId: call.toolCallId ?? "",
          toolName: call.toolName,
          argumentCharacters: call.assembler.length,
          parserState: finalized.reason,
          argumentFragments: call.assembler.text,
          recoveryAttempt: this.recoveryAttempt,
          maximumRecoveryAttempts: this.maximumRecoveryAttempts,
          recoveryExhausted: this.recoveryAttempt >= this.maximumRecoveryAttempts,
        });
      }
      if (!isJsonObject(finalized.value)) {
        throw this.fail("response_protocol_error", "provider tool arguments must decode to a JSON object", {
          toolKey: call.key,
          toolCallId: call.toolCallId ?? "",
          toolName: call.toolName,
          decodedType: jsonType(finalized.value),
        });
      }
    }
    this.phaseValue = "terminal";
    this.completedAtValue = this.now();
    const snapshot = this.snapshot();
    return {
      snapshot,
      recoveryAction: "accept",
      replaySafe: this.replaySafe,
      accepted: true,
      reason: "normalized provider stream passed sequence, budget, usage, and tool-call checks",
    };
  }

  failure(
    kind: "stream_timeout" | "response_protocol_error" | "tool_arguments_incomplete" | "partial_response_observed",
    message: string,
    context: ProviderStreamFailureContext,
    detail: JsonRecord = {},
  ): ProviderControlPlaneError {
    return this.fail(kind, message, detail, context);
  }

  snapshot(): ProviderStreamSnapshot {
    const toolCalls = [...this.toolCalls.values()]
      .sort((left, right) => left.firstSequence - right.firstSequence || left.key.localeCompare(right.key))
      .map((call) => toolSnapshot(call));
    const evidence = {
      dispatchId: this.request.dispatchId,
      routeId: this.lease.routeId,
      phase: this.phaseValue,
      startedAt: this.startedAt,
      responseStartedAt: this.responseStartedAtValue,
      firstOutputAt: this.firstOutputAtValue,
      lastFrameAt: this.lastFrameAtValue,
      completedAt: this.completedAtValue,
      frameCount: this.frameCountValue,
      nextSequence: this.nextSequenceValue,
      responseStartCount: this.responseStartCountValue,
      responseEndCount: this.responseEndCountValue,
      textCharacters: this.textCharactersValue,
      thinkingCharacters: this.thinkingCharactersValue,
      providerNoticeCount: this.providerNoticeCountValue,
      outputObserved: this.outputObservedValue,
      sideEffectCandidateObserved: this.sideEffectCandidateObservedValue,
      usage: canonicalize(this.usageValue) as JsonRecord,
      toolCalls,
      terminalProviderEvent: this.terminalProviderEventValue,
    };
    return {
      ...deepClone(evidence),
      evidenceDigest: digestJson(evidence),
    };
  }

  recoveryFor(error: ProviderControlPlaneError): ProviderStreamRecoveryAction {
    if (error.kind === "tool_arguments_incomplete") {
      return error.retryable ? "retry_same_route" : "surface_to_operator";
    }
    if (error.outputObserved || this.outputObservedValue || this.sideEffectCandidateObservedValue) {
      return "reconcile_partial_response";
    }
    if (["provider_unavailable", "provider_timeout", "stream_timeout", "rate_limited"].includes(error.kind)) {
      return "change_provider_route";
    }
    if (error.retryable) return "retry_same_route";
    return "surface_to_operator";
  }

  private observeFrame(frame: ProviderStreamFrame): void {
    this.assertFrameIdentity(frame);
    const frameDigest = digestJson(frame as unknown as JsonValue);
    const observedDigest = this.observedFrameDigests.get(frame.frameId);
    if (observedDigest !== undefined) {
      if (observedDigest === frameDigest) return;
      throw this.fail("response_protocol_error", "provider reused a frame identity with different content", {
        frameId: frame.frameId,
        sequence: frame.sequence,
      });
    }
    this.checkWatchdog(frame.createdAt);
    if (this.phaseValue === "terminal") {
      throw this.fail("response_protocol_error", "provider emitted a frame after terminal response", {
        sequence: frame.sequence,
        kind: frame.kind,
      });
    }
    if (frame.sequence !== this.nextSequenceValue) {
      throw this.fail("response_protocol_error", "provider frame sequence is not contiguous", {
        expectedSequence: this.nextSequenceValue,
        actualSequence: frame.sequence,
        kind: frame.kind,
      });
    }
    if (this.lastFrameAtValue !== null && frame.createdAt < this.lastFrameAtValue) {
      throw this.fail("response_protocol_error", "provider frame timestamp moved backwards", {
        previousCreatedAt: this.lastFrameAtValue,
        actualCreatedAt: frame.createdAt,
        sequence: frame.sequence,
      });
    }
    this.nextSequenceValue += 1;
    this.observedFrameDigests.set(frame.frameId, frameDigest);
    this.frameCountValue += 1;
    this.lastFrameAtValue = frame.createdAt;
    if (this.frameCountValue > this.budget.maximumFrames) {
      throw this.fail("response_protocol_error", "provider stream exceeded normalized frame budget", {
        frameCount: this.frameCountValue,
        maximumFrames: this.budget.maximumFrames,
      });
    }
    switch (frame.kind) {
      case "response_start":
        this.observeResponseStart(frame);
        break;
      case "text_delta":
        this.observeText(frame, false);
        break;
      case "thinking_delta":
        this.observeText(frame, true);
        break;
      case "tool_call_delta":
        this.observeToolCall(frame);
        break;
      case "usage":
        this.observeUsage(frame);
        break;
      case "response_end":
        this.observeResponseEnd(frame);
        break;
      case "provider_notice":
        this.observeProviderNotice(frame);
        break;
    }
  }

  private observeResponseStart(frame: ProviderStreamFrame): void {
    this.responseStartCountValue += 1;
    if (this.responseStartCountValue > 1) {
      throw this.fail("response_protocol_error", "provider emitted duplicate response_start frames", {
        sequence: frame.sequence,
        responseStartCount: this.responseStartCountValue,
      });
    }
    if (this.phaseValue !== "created") {
      throw this.fail("response_protocol_error", "provider emitted response_start after output", {
        sequence: frame.sequence,
        phase: this.phaseValue,
      });
    }
    this.phaseValue = "response_started";
    this.responseStartedAtValue = frame.createdAt;
  }

  private observeText(frame: ProviderStreamFrame, thinking: boolean): void {
    if (frame.text === null) {
      throw this.fail("response_protocol_error", "provider text frame has no text", {
        sequence: frame.sequence,
        kind: frame.kind,
      });
    }
    if (frame.toolCallId !== null || frame.toolName !== null || frame.jsonDelta !== null) {
      throw this.fail("response_protocol_error", "provider text frame contains tool-call fields", {
        sequence: frame.sequence,
        kind: frame.kind,
      });
    }
    if (thinking) {
      this.thinkingCharactersValue += frame.text.length;
      if (this.thinkingCharactersValue > this.budget.maximumThinkingCharacters) {
        throw this.fail("response_protocol_error", "provider thinking output exceeded character budget", {
          thinkingCharacters: this.thinkingCharactersValue,
          maximumThinkingCharacters: this.budget.maximumThinkingCharacters,
        });
      }
    } else {
      this.textCharactersValue += frame.text.length;
      if (this.textCharactersValue > this.budget.maximumTextCharacters) {
        throw this.fail("response_protocol_error", "provider text output exceeded character budget", {
          textCharacters: this.textCharactersValue,
          maximumTextCharacters: this.budget.maximumTextCharacters,
        });
      }
    }
    if (frame.text.length > 0) this.markOutput(frame.createdAt, false);
  }

  private observeToolCall(frame: ProviderStreamFrame): void {
    if (frame.text !== null) {
      throw this.fail("response_protocol_error", "provider tool-call frame contains text payload", {
        sequence: frame.sequence,
      });
    }
    const key = this.resolveToolCallKey(frame);
    let call = this.toolCalls.get(key);
    if (call === undefined) {
      if (this.toolCalls.size >= this.budget.maximumToolCalls) {
        throw this.fail("response_protocol_error", "provider stream exceeded tool-call count budget", {
          maximumToolCalls: this.budget.maximumToolCalls,
          sequence: frame.sequence,
        });
      }
      call = {
        key,
        toolCallId: frame.toolCallId,
        toolName: frame.toolName,
        assembler: new IncrementalJsonAssembler(this.budget.maximumToolArgumentCharacters),
        firstSequence: frame.sequence,
        lastSequence: frame.sequence,
        deltaCount: 0,
      };
      this.toolCalls.set(key, call);
    }
    if (frame.toolCallId !== null) {
      if (call.toolCallId !== null && call.toolCallId !== frame.toolCallId) {
        throw this.fail("response_protocol_error", "provider changed tool-call identity mid-stream", {
          key,
          previousToolCallId: call.toolCallId,
          actualToolCallId: frame.toolCallId,
        });
      }
      call.toolCallId = frame.toolCallId;
    }
    if (frame.toolName !== null) {
      if (call.toolName !== null && call.toolName !== frame.toolName) {
        throw this.fail("response_protocol_error", "provider changed tool name mid-stream", {
          key,
          previousToolName: call.toolName,
          actualToolName: frame.toolName,
        });
      }
      call.toolName = frame.toolName;
    }
    if (frame.jsonDelta !== null) call.assembler.append(frame.jsonDelta);
    call.lastSequence = frame.sequence;
    call.deltaCount += 1;
    // Model argument bytes are not a physical tool dispatch.
    this.markOutput(frame.createdAt, false);
  }

  private observeUsage(frame: ProviderStreamFrame): void {
    const canonical = canonicalize(frame.usage);
    if (!isJsonObject(canonical)) {
      throw this.fail("response_protocol_error", "provider usage frame must be a JSON object", {
        sequence: frame.sequence,
      });
    }
    const leaves = numericLeaves(canonical);
    for (const leaf of leaves) {
      if (!Number.isFinite(leaf.value) || leaf.value < 0) {
        throw this.fail("response_protocol_error", "provider usage contains an invalid numeric counter", {
          path: leaf.path,
          value: leaf.value,
          sequence: frame.sequence,
        });
      }
      const previous = this.usageLeaves.get(leaf.path);
      if (previous !== undefined && leaf.value < previous) {
        throw this.fail("response_protocol_error", "provider usage counter moved backwards", {
          path: leaf.path,
          previous,
          actual: leaf.value,
          sequence: frame.sequence,
        });
      }
      this.usageLeaves.set(leaf.path, leaf.value);
    }
    this.usageValue = canonical;
  }

  private observeResponseEnd(frame: ProviderStreamFrame): void {
    this.responseEndCountValue += 1;
    if (this.responseEndCountValue > 1) {
      throw this.fail("response_protocol_error", "provider emitted duplicate terminal frames", {
        sequence: frame.sequence,
        responseEndCount: this.responseEndCountValue,
      });
    }
    this.phaseValue = "terminal";
    this.completedAtValue = frame.createdAt;
    this.terminalProviderEventValue = frame.providerEvent;
  }

  private observeProviderNotice(frame: ProviderStreamFrame): void {
    this.providerNoticeCountValue += 1;
    if (this.providerNoticeCountValue > this.budget.maximumProviderNotices) {
      throw this.fail("response_protocol_error", "provider emitted too many unrecognized notices", {
        providerNoticeCount: this.providerNoticeCountValue,
        maximumProviderNotices: this.budget.maximumProviderNotices,
        sequence: frame.sequence,
      });
    }
  }

  private resolveToolCallKey(frame: ProviderStreamFrame): string {
    const providerIndex = frame.metadata.providerIndex;
    const indexKey = typeof providerIndex === "string" || typeof providerIndex === "number"
      ? String(providerIndex)
      : null;
    if (frame.toolCallId !== null) {
      const key = `id:${frame.toolCallId}`;
      if (indexKey !== null) this.toolIndexKeys.set(indexKey, key);
      if (this.toolCalls.has(key)) {
        this.currentAnonymousToolKey = key;
        return key;
      }
      const anonymous = this.currentAnonymousToolKey === null ? undefined : this.toolCalls.get(this.currentAnonymousToolKey);
      if (anonymous !== undefined && anonymous.toolCallId === null) {
        this.toolCalls.delete(anonymous.key);
        const promoted: MutableToolCall = { ...anonymous, key, toolCallId: frame.toolCallId };
        this.toolCalls.set(key, promoted);
        this.currentAnonymousToolKey = key;
      }
      return key;
    }
    if (indexKey !== null) {
      const mapped = this.toolIndexKeys.get(indexKey);
      if (mapped !== undefined) {
        this.currentAnonymousToolKey = mapped;
        return mapped;
      }
      const key = `index:${indexKey}`;
      this.toolIndexKeys.set(indexKey, key);
      this.currentAnonymousToolKey = key;
      return key;
    }
    if (frame.toolName !== null) {
      const existing = [...this.toolCalls.values()].find((call) => call.toolCallId === null && call.toolName === frame.toolName);
      if (existing !== undefined) {
        this.currentAnonymousToolKey = existing.key;
        return existing.key;
      }
    }
    if (this.currentAnonymousToolKey !== null) return this.currentAnonymousToolKey;
    const key = `anonymous:${frame.sequence}`;
    this.currentAnonymousToolKey = key;
    return key;
  }

  private markOutput(createdAt: number, sideEffectCandidate: boolean): void {
    this.outputObservedValue = true;
    this.sideEffectCandidateObservedValue ||= sideEffectCandidate;
    this.firstOutputAtValue ??= createdAt;
    if (this.phaseValue === "created" || this.phaseValue === "response_started") {
      this.phaseValue = "output_streaming";
    }
  }

  private assertIdentity(): void {
    for (const [name, requestValue, leaseValue] of [
      ["routeId", this.request.routeId, this.lease.routeId],
      ["runId", this.request.runId, this.lease.runId],
      ["taskId", this.request.taskId, this.lease.taskId],
      ["sessionId", this.request.sessionId, this.lease.sessionId],
      ["turnId", this.request.turnId, this.lease.turnId],
    ] as const) {
      if (requestValue !== leaseValue) {
        throw new ProviderControlPlaneError({
          layer: "route",
          kind: "route_policy_rejected",
          message: `stream ${name} does not match route custody`,
          routeId: this.lease.routeId,
          providerId: this.lease.providerId,
          modelId: this.lease.modelId,
          credentialId: this.lease.credentialId,
          detail: { field: name },
        });
      }
    }
  }

  private assertFrameIdentity(frame: ProviderStreamFrame): void {
    if (frame.dispatchId !== this.request.dispatchId || frame.routeId !== this.lease.routeId) {
      throw this.fail("response_protocol_error", "provider frame escaped dispatch route custody", {
        expectedDispatchId: this.request.dispatchId,
        actualDispatchId: frame.dispatchId,
        expectedRouteId: this.lease.routeId,
        actualRouteId: frame.routeId,
        sequence: frame.sequence,
      });
    }
    if (!frame.frameId.trim()) {
      throw this.fail("response_protocol_error", "provider frame has no stable identity", {
        sequence: frame.sequence,
      });
    }
  }

  private assertActive(): void {
    if (this.failureValue !== null) throw this.failureValue;
    if (this.phaseValue === "terminal") {
      throw this.fail("response_protocol_error", "provider stream is already terminal", {
        completedAt: this.completedAtValue ?? 0,
      });
    }
  }

  private fail(
    kind: "stream_timeout" | "response_protocol_error" | "tool_arguments_incomplete" | "partial_response_observed",
    message: string,
    detail: JsonRecord,
    context?: ProviderStreamFailureContext,
  ): ProviderControlPlaneError {
    this.phaseValue = "failed";
    this.completedAtValue = this.now();
    const toolArgumentsIncomplete = kind === "tool_arguments_incomplete";
    const outputObserved = this.outputObservedValue || this.sideEffectCandidateObservedValue;
    const effectiveKind = outputObserved && !toolArgumentsIncomplete && kind !== "partial_response_observed"
      ? "partial_response_observed"
      : kind;
    const recoveryExhausted = toolArgumentsIncomplete
      && this.recoveryAttempt >= this.maximumRecoveryAttempts;
    const error = new ProviderControlPlaneError({
      layer: "protocol",
      kind: effectiveKind,
      message,
      retryable: toolArgumentsIncomplete
        ? !recoveryExhausted
        : !outputObserved && kind === "stream_timeout",
      recoveryIntent: toolArgumentsIncomplete
        ? recoveryExhausted ? "surface_to_operator" : "retry_same_route"
        : outputObserved ? "reconcile_partial_response" : kind === "stream_timeout" ? "change_provider_route" : "surface_to_operator",
      providerId: context?.providerId ?? this.lease.providerId,
      modelId: context?.modelId ?? this.lease.modelId,
      routeId: this.lease.routeId,
      credentialId: context?.credentialId ?? this.lease.credentialId,
      bytesSent: context?.bytesSent ?? 0,
      bytesReceived: context?.bytesReceived ?? 0,
      outputObserved,
      detail: {
        ...detail,
        streamPhase: this.phaseValue,
        streamFrameCount: this.frameCountValue,
        streamEvidenceDigest: digestJson(this.snapshotEvidence()),
        recoveryAttempt: this.recoveryAttempt,
        maximumRecoveryAttempts: this.maximumRecoveryAttempts,
        recoveryExhausted,
        toolCalls: canonicalize([...this.toolCalls.values()].map((call) => toolSnapshot(call))),
      },
    });
    this.failureValue = error;
    return error;
  }

  private snapshotEvidence(): JsonRecord {
    return canonicalize({
      dispatchId: this.request.dispatchId,
      routeId: this.lease.routeId,
      phase: this.phaseValue,
      frameCount: this.frameCountValue,
      nextSequence: this.nextSequenceValue,
      responseStartCount: this.responseStartCountValue,
      responseEndCount: this.responseEndCountValue,
      textCharacters: this.textCharactersValue,
      thinkingCharacters: this.thinkingCharactersValue,
      toolCallCount: this.toolCalls.size,
      outputObserved: this.outputObservedValue,
      sideEffectCandidateObserved: this.sideEffectCandidateObservedValue,
    }) as JsonRecord;
  }
}

export class IncrementalJsonAssembler {
  private readonly maximumCharacters: number;
  private textValue = "";
  private depth = 0;
  private inString = false;
  private escaped = false;
  private started = false;
  private completeValue = false;
  private invalidReasonValue: string | null = null;

  constructor(maximumCharacters: number) {
    if (!Number.isInteger(maximumCharacters) || maximumCharacters < 2) {
      throw new TypeError("maximum JSON argument characters must be an integer greater than one");
    }
    this.maximumCharacters = maximumCharacters;
  }

  get text(): string {
    return this.textValue;
  }

  get length(): number {
    return this.textValue.length;
  }

  get invalidReason(): string | null {
    return this.invalidReasonValue;
  }

  append(delta: string): void {
    if (this.invalidReasonValue !== null) throw new TypeError(this.invalidReasonValue);
    if (this.completeValue && delta.trim()) {
      this.invalidate("tool argument stream continued after complete JSON value");
    }
    if (this.textValue.length + delta.length > this.maximumCharacters) {
      this.invalidate("tool argument stream exceeded character budget");
    }
    this.textValue += delta;
    for (const character of delta) this.consume(character);
  }

  finalize(): { readonly complete: boolean; readonly value: JsonValue | null; readonly reason: string } {
    if (this.invalidReasonValue !== null) {
      return { complete: false, value: null, reason: this.invalidReasonValue };
    }
    if (!this.started) return { complete: true, value: {}, reason: "empty arguments normalized to an object" };
    if (this.inString) return { complete: false, value: null, reason: "JSON string is incomplete" };
    if (this.depth !== 0) return { complete: false, value: null, reason: "JSON container is incomplete" };
    try {
      const value = canonicalize(JSON.parse(this.textValue));
      return { complete: true, value, reason: "complete" };
    } catch (error) {
      return {
        complete: false,
        value: null,
        reason: error instanceof Error ? error.message : "tool arguments are not valid JSON",
      };
    }
  }

  private consume(character: string): void {
    if (this.completeValue) {
      if (!isWhitespace(character)) this.invalidate("non-whitespace follows complete tool arguments");
      return;
    }
    if (this.inString) {
      if (this.escaped) {
        this.escaped = false;
        return;
      }
      if (character === "\\") {
        this.escaped = true;
        return;
      }
      if (character === '"') this.inString = false;
      return;
    }
    if (isWhitespace(character) && !this.started) return;
    if (!this.started) {
      this.started = true;
      if (character !== "{" && character !== "[") {
        this.invalidate("tool arguments must start with a JSON object or array");
      }
    }
    if (character === '"') {
      this.inString = true;
      return;
    }
    if (character === "{" || character === "[") {
      this.depth += 1;
      return;
    }
    if (character === "}" || character === "]") {
      this.depth -= 1;
      if (this.depth < 0) this.invalidate("tool arguments contain an unmatched closing delimiter");
      if (this.depth === 0) this.completeValue = true;
    }
  }

  private invalidate(reason: string): never {
    this.invalidReasonValue = reason;
    throw new TypeError(reason);
  }
}

function normalizeBudget(
  request: ProviderDispatchRequest,
  override: Partial<ProviderStreamBudget>,
): ProviderStreamBudget {
  const totalMilliseconds = Math.min(
    positiveInteger(override.totalMilliseconds ?? request.timeoutMilliseconds, "totalMilliseconds"),
    request.timeoutMilliseconds,
  );
  const firstByteMilliseconds = Math.min(
    positiveInteger(override.firstByteMilliseconds ?? request.chunkTimeoutMilliseconds, "firstByteMilliseconds"),
    totalMilliseconds,
  );
  const chunkMilliseconds = Math.min(
    positiveInteger(override.chunkMilliseconds ?? request.chunkTimeoutMilliseconds, "chunkMilliseconds"),
    totalMilliseconds,
  );
  const maximumFrames = override.maximumFrames === undefined
    ? defaultMaximumFrames(request.maximumOutputTokens)
    : positiveInteger(override.maximumFrames, "maximumFrames");
  return {
    firstByteMilliseconds,
    chunkMilliseconds,
    totalMilliseconds,
    maximumFrames,
    maximumTextCharacters: positiveInteger(override.maximumTextCharacters ?? DEFAULT_BUDGET.maximumTextCharacters, "maximumTextCharacters"),
    maximumThinkingCharacters: positiveInteger(override.maximumThinkingCharacters ?? DEFAULT_BUDGET.maximumThinkingCharacters, "maximumThinkingCharacters"),
    maximumToolCalls: positiveInteger(override.maximumToolCalls ?? DEFAULT_BUDGET.maximumToolCalls, "maximumToolCalls"),
    maximumToolArgumentCharacters: positiveInteger(
      override.maximumToolArgumentCharacters ?? DEFAULT_BUDGET.maximumToolArgumentCharacters,
      "maximumToolArgumentCharacters",
    ),
    maximumProviderNotices: positiveInteger(override.maximumProviderNotices ?? DEFAULT_BUDGET.maximumProviderNotices, "maximumProviderNotices"),
  };
}

function defaultMaximumFrames(maximumOutputTokens: number): number {
  const outputTokens = positiveInteger(maximumOutputTokens, "maximumOutputTokens");
  const maximumScalableTokens = Math.floor(
    (MAXIMUM_DYNAMIC_FRAMES - FRAME_BUDGET_OVERHEAD) / FRAMES_PER_OUTPUT_TOKEN,
  );
  const scaled = outputTokens >= maximumScalableTokens
    ? MAXIMUM_DYNAMIC_FRAMES
    : outputTokens * FRAMES_PER_OUTPUT_TOKEN + FRAME_BUDGET_OVERHEAD;
  return Math.max(DEFAULT_BUDGET.maximumFrames, scaled);
}

function positiveInteger(value: number, name: string): number {
  if (!Number.isInteger(value) || value < 1) throw new TypeError(`${name} must be a positive integer`);
  return value;
}

function toolSnapshot(call: MutableToolCall): ProviderToolCallSnapshot {
  const finalized = call.assembler.finalize();
  return {
    key: call.key,
    toolCallId: call.toolCallId,
    toolName: call.toolName,
    argumentsText: call.assembler.text,
    argumentsDigest: digestJson({ arguments: call.assembler.text }),
    argumentCharacters: call.assembler.length,
    jsonComplete: finalized.complete,
    parsedArguments: finalized.value,
    firstSequence: call.firstSequence,
    lastSequence: call.lastSequence,
    deltaCount: call.deltaCount,
    transactionState: finalized.complete ? "arguments_complete" : "receiving_arguments",
  };
}

function numericLeaves(value: JsonValue, prefix = "$"): UsageLeaf[] {
  if (typeof value === "number") return [{ path: prefix, value }];
  if (Array.isArray(value)) return value.flatMap((item, index) => numericLeaves(item, `${prefix}[${index}]`));
  if (isJsonObject(value)) {
    return Object.entries(value).flatMap(([key, item]) => numericLeaves(item, `${prefix}.${key}`));
  }
  return [];
}

function isJsonObject(value: JsonValue | null): value is JsonRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function jsonType(value: JsonValue | null): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  return typeof value;
}

function isWhitespace(value: string): boolean {
  return value === " " || value === "\n" || value === "\r" || value === "\t";
}
