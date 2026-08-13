import { createHash, randomUUID } from "node:crypto";

import { asObject, asString, type JsonObject, type JsonValue, type ToolStep } from "../contracts.ts";
import {
  ToolObservationBudgetRuntime,
  type ToolObservationBudgetSnapshot,
} from "./tool-observation-budget-runtime.ts";
import { projectToolOutputForRuntime } from "../tools/model-result-projection.ts";

export const MODEL_ITERATION_SNAPSHOT_VERSION = "zyra.model-iteration/v1";

export type ModelIterationPhase =
  | "idle"
  | "ready"
  | "provider_running"
  | "tools_running"
  | "observation_ready"
  | "completed"
  | "failed"
  | "cancelled";

export type ModelRoundState =
  | "provider_running"
  | "tools_running"
  | "observation_ready"
  | "completed"
  | "failed";

export type IterationToolState = "planned" | "running" | "succeeded" | "failed" | "denied" | "cancelled";

export interface ModelIterationIdentity {
  sessionId: string;
  runId: string;
  taskId: string;
  workerRequestId: string;
}

export interface IterationToolRecord {
  callId: string;
  roundId: string;
  turnId: string | null;
  toolName: string;
  arguments: JsonObject;
  argumentsDigest: string;
  state: IterationToolState;
  resultDigest: string | null;
  summary: string;
  output: JsonObject;
  error: string | null;
  permissionEffect: "allow" | "deny" | "ask" | "unknown";
  plannedAt: string;
  startedAt: string | null;
  completedAt: string | null;
  revision: number;
}

export interface ModelRoundRecord {
  roundId: string;
  roundIndex: number;
  requestKey: string;
  state: ModelRoundState;
  inputDigest: string;
  outputDigest: string | null;
  providerRequestId: string | null;
  providerModel: string;
  stopReason: string | null;
  finalText: string;
  toolCallIds: string[];
  messageCountBefore: number;
  messageCountAfter: number;
  startedAt: string;
  completedAt: string | null;
  error: string | null;
  revision: number;
}

export interface IterationTransition {
  transitionId: string;
  epoch: number;
  sequence: number;
  revisionBefore: number;
  revisionAfter: number;
  operation: string;
  subjectId: string;
  payloadDigest: string;
  stateDigest: string;
  occurredAt: string;
}

export interface ModelIterationAudit {
  ok: boolean;
  phase: ModelIterationPhase;
  revision: number;
  restartEpoch: number;
  roundCount: number;
  toolCount: number;
  unsettledRoundIds: string[];
  unsettledToolIds: string[];
  orphanToolIds: string[];
  duplicateRequestKeys: string[];
  replayedTransitionIds: string[];
  invariantFailures: string[];
  stateDigest: string;
}

export interface ModelIterationSnapshot {
  version: typeof MODEL_ITERATION_SNAPSHOT_VERSION;
  identity: ModelIterationIdentity;
  phase: ModelIterationPhase;
  revision: number;
  restartEpoch: number;
  transitionSequence: number;
  maximumRounds: number | null;
  maximumToolCalls: number | null;
  rounds: ModelRoundRecord[];
  tools: IterationToolRecord[];
  transcript: JsonObject[];
  transitions: IterationTransition[];
  toolObservationBudget: ToolObservationBudgetSnapshot;
  activeRoundId: string | null;
  finalText: string;
  terminalError: string | null;
  checksum: string;
}

export interface BeginModelRoundInput {
  messages: readonly JsonObject[];
  model: string;
  requestKey: string;
}

export interface AcceptProviderResultInput {
  roundId: string;
  providerRequestId: string | null;
  model: string;
  stopReason: string;
  finalText: string;
  steps: readonly ToolStep[];
}

export interface ToolObservationInput {
  callId: string;
  turnId: string | null;
  ok: boolean;
  summary: string;
  output: JsonObject;
  error: string | null;
  permissionEffect?: "allow" | "deny" | "ask" | "unknown";
}

export class ModelIterationRuntime {
  private identity: ModelIterationIdentity;
  private phase: ModelIterationPhase = "idle";
  private revision = 0;
  private restartEpoch = 0;
  private transitionSequence = 0;
  private maximumRounds: number | null = null;
  private maximumToolCalls: number | null = null;
  private readonly rounds = new Map<string, ModelRoundRecord>();
  private readonly tools = new Map<string, IterationToolRecord>();
  private readonly transcript: JsonObject[] = [];
  private readonly transitions: IterationTransition[] = [];
  private readonly toolObservationBudget = new ToolObservationBudgetRuntime();
  private activeRoundId: string | null = null;
  private finalText = "";
  private terminalError: string | null = null;

  constructor(identity: ModelIterationIdentity) {
    this.identity = normalizeIdentity(identity);
  }

  start(
    messages: readonly JsonObject[],
    limits: { maximumRounds?: number | null; maximumToolCalls?: number | null } = {},
  ): void {
    if (this.phase !== "idle") {
      if (this.phase === "ready" && this.transcript.length === normalizeTranscript(messages).length) return;
      throw new Error(`model iteration cannot start from ${this.phase}`);
    }
    this.maximumRounds = limits.maximumRounds === null || limits.maximumRounds === undefined
      ? null
      : boundedInteger(limits.maximumRounds, 1, 100_000, "maximum rounds");
    this.maximumToolCalls = limits.maximumToolCalls === null || limits.maximumToolCalls === undefined
      ? null
      : boundedInteger(limits.maximumToolCalls, 1, 1_000_000, "maximum tool calls");
    this.transcript.push(...normalizeTranscript(messages));
    if (this.transcript.length === 0) {
      this.transcript.push({ role: "user", content: "Execute the requested coding task." });
    }
    this.phase = "ready";
    this.commit("iteration.started", this.identity.sessionId, {
      message_count: this.transcript.length,
      maximum_rounds: this.maximumRounds,
      maximum_tool_calls: this.maximumToolCalls,
    });
  }

  beginProviderRound(input: BeginModelRoundInput): ModelRoundRecord {
    this.assertOperational("begin provider round");
    if (this.phase !== "ready" && this.phase !== "observation_ready") {
      throw new Error(`provider round cannot start from ${this.phase}`);
    }
    if (this.activeRoundId !== null) throw new Error(`provider round is already active: ${this.activeRoundId}`);
    if (this.maximumRounds !== null && this.rounds.size >= this.maximumRounds) {
      this.fail("model_round_limit_exceeded");
      throw new Error("model round limit exceeded");
    }
    const requestKey = required(input.requestKey, "provider request key");
    if ([...this.rounds.values()].some((round) => round.requestKey === requestKey)) {
      throw new Error(`provider request key was already used: ${requestKey}`);
    }
    const normalizedMessages = normalizeTranscript(input.messages);
    if (normalizedMessages.length === 0) throw new Error("provider round requires at least one message");
    this.replaceTranscript(normalizedMessages);
    const roundIndex = this.rounds.size;
    const roundId = `${this.identity.sessionId}:round:${roundIndex}:epoch:${this.restartEpoch}`;
    const now = timestamp();
    const round: ModelRoundRecord = {
      roundId,
      roundIndex,
      requestKey,
      state: "provider_running",
      inputDigest: digest({ messages: this.transcript, model: input.model, request_key: requestKey }),
      outputDigest: null,
      providerRequestId: null,
      providerModel: required(input.model, "provider model"),
      stopReason: null,
      finalText: "",
      toolCallIds: [],
      messageCountBefore: this.transcript.length,
      messageCountAfter: this.transcript.length,
      startedAt: now,
      completedAt: null,
      error: null,
      revision: 1,
    };
    this.rounds.set(roundId, round);
    this.activeRoundId = roundId;
    this.phase = "provider_running";
    this.commit("provider.round.started", roundId, {
      round_index: roundIndex,
      request_key: requestKey,
      model: round.providerModel,
      input_digest: round.inputDigest,
      message_count: round.messageCountBefore,
    });
    return clone(round);
  }

  acceptProviderResult(input: AcceptProviderResultInput): ModelRoundRecord {
    const round = this.requireActiveRound(input.roundId, "provider_running");
    const steps = normalizeSteps(input.steps);
    const ids = new Set<string>();
    for (const step of steps) {
      const callId = required(step.step_id ?? "", "provider tool call id");
      if (ids.has(callId) || this.tools.has(callId)) throw new Error(`provider repeated tool call id: ${callId}`);
      ids.add(callId);
    }
    round.providerRequestId = nullable(input.providerRequestId);
    round.providerModel = required(input.model || round.providerModel, "provider model");
    round.stopReason = required(input.stopReason || (steps.length > 0 ? "tool_use" : "end_turn"), "stop reason");
    round.finalText = input.finalText.trim();
    round.outputDigest = digest({
      provider_request_id: round.providerRequestId,
      model: round.providerModel,
      stop_reason: round.stopReason,
      final_text: round.finalText,
      steps,
    });
    round.revision += 1;
    if (steps.length === 0 && isProviderOutputTruncated(round.stopReason)) {
      round.state = "completed";
      round.completedAt = timestamp();
      round.messageCountAfter = this.appendTruncatedAssistant(round.finalText, round);
      this.activeRoundId = null;
      this.phase = "ready";
      this.commit("provider.round.truncated", round.roundId, {
        provider_request_id: round.providerRequestId,
        stop_reason: round.stopReason,
        output_digest: round.outputDigest,
        partial_text_digest: digest(round.finalText),
      });
      return clone(round);
    }
    if (steps.length === 0) {
      round.state = "completed";
      round.completedAt = timestamp();
      round.messageCountAfter = this.appendFinalAssistant(round.finalText, round);
      this.finalText = round.finalText;
      this.activeRoundId = null;
      this.phase = "completed";
      this.commit("provider.round.final", round.roundId, {
        provider_request_id: round.providerRequestId,
        stop_reason: round.stopReason,
        output_digest: round.outputDigest,
        final_text_digest: digest(round.finalText),
      });
      return clone(round);
    }
    if (
      this.maximumToolCalls !== null
      && this.tools.size + steps.length > this.maximumToolCalls
    ) {
      round.state = "failed";
      round.error = "model_tool_call_limit_exceeded";
      round.completedAt = timestamp();
      this.activeRoundId = null;
      this.fail(round.error);
      throw new Error("model tool call limit exceeded");
    }
    const assistantContent: JsonValue[] = [];
    if (round.finalText) assistantContent.push({ type: "text", text: round.finalText });
    for (const step of steps) {
      const callId = required(step.step_id ?? "", "provider tool call id");
      const tool: IterationToolRecord = {
        callId,
        roundId: round.roundId,
        turnId: null,
        toolName: required(step.tool_name, "provider tool name"),
        arguments: cloneObject(step.arguments),
        argumentsDigest: digest(step.arguments),
        state: "planned",
        resultDigest: null,
        summary: "",
        output: {},
        error: null,
        permissionEffect: "unknown",
        plannedAt: timestamp(),
        startedAt: null,
        completedAt: null,
        revision: 1,
      };
      this.tools.set(callId, tool);
      round.toolCallIds.push(callId);
      assistantContent.push({ type: "tool_use", id: callId, name: tool.toolName, input: tool.arguments });
    }
    this.transcript.push({
      role: "assistant",
      content: assistantContent,
      metadata: {
        model_round_id: round.roundId,
        provider_request_id: round.providerRequestId,
        canonical_owner: "model_iteration_runtime",
      },
    });
    round.state = "tools_running";
    round.messageCountAfter = this.transcript.length;
    this.phase = "tools_running";
    this.commit("provider.round.tools_planned", round.roundId, {
      provider_request_id: round.providerRequestId,
      tool_call_ids: round.toolCallIds,
      tool_count: round.toolCallIds.length,
      output_digest: round.outputDigest,
    });
    return clone(round);
  }

  markToolRunning(callId: string, turnId: string | null): IterationToolRecord {
    this.assertOperational("mark tool running");
    const tool = this.requireTool(callId);
    if (tool.state !== "planned") {
      if (tool.state === "running" && tool.turnId === nullable(turnId)) return clone(tool);
      throw new Error(`tool ${callId} cannot start from ${tool.state}`);
    }
    const round = this.requireRound(tool.roundId);
    if (round.state !== "tools_running") throw new Error(`tool round ${round.roundId} is ${round.state}`);
    tool.turnId = nullable(turnId);
    tool.state = "running";
    tool.startedAt = timestamp();
    tool.revision += 1;
    this.commit("tool.iteration.started", callId, {
      round_id: tool.roundId,
      turn_id: tool.turnId,
      tool_name: tool.toolName,
      arguments_digest: tool.argumentsDigest,
    });
    return clone(tool);
  }

  recordToolObservation(input: ToolObservationInput): IterationToolRecord {
    this.assertOperational("record tool observation");
    const tool = this.requireTool(input.callId);
    if (isToolTerminal(tool.state)) {
      const candidateDigest = digest({ ok: input.ok, summary: input.summary, output: input.output, error: input.error });
      if (candidateDigest === tool.resultDigest) return clone(tool);
      throw new Error(`tool observation conflicts with terminal result: ${input.callId}`);
    }
    if (tool.state === "planned") this.markToolRunning(input.callId, input.turnId);
    if (tool.state !== "running") throw new Error(`tool ${input.callId} cannot settle from ${tool.state}`);
    const permissionEffect = input.permissionEffect ?? inferPermissionEffect(input.error);
    tool.turnId = nullable(input.turnId) ?? tool.turnId;
    tool.summary = input.summary.trim() || (input.ok ? `${tool.toolName} completed` : `${tool.toolName} failed`);
    tool.output = projectToolOutputForRuntime(input.output);
    tool.error = input.ok ? null : required(input.error ?? "tool_error", "tool error");
    tool.permissionEffect = permissionEffect;
    tool.state = input.ok ? "succeeded" : permissionEffect === "deny" || permissionEffect === "ask" ? "denied" : "failed";
    tool.resultDigest = digest({ ok: input.ok, summary: input.summary, output: input.output, error: input.error });
    tool.completedAt = timestamp();
    tool.revision += 1;
    this.commit("tool.iteration.observed", input.callId, {
      round_id: tool.roundId,
      turn_id: tool.turnId,
      tool_name: tool.toolName,
      state: tool.state,
      result_digest: tool.resultDigest,
      permission_effect: permissionEffect,
    });
    const round = this.requireRound(tool.roundId);
    const settled = round.toolCallIds.every((id) => isToolTerminal(this.requireTool(id).state));
    if (settled) {
      round.state = "observation_ready";
      round.revision += 1;
      this.phase = "observation_ready";
      this.commit("provider.round.observation_ready", round.roundId, {
        tool_call_ids: round.toolCallIds,
        failed_tool_ids: round.toolCallIds.filter((id) => this.requireTool(id).state !== "succeeded"),
      });
    }
    return clone(tool);
  }

  buildRevisionMessages(roundId: string): JsonObject[] {
    const round = this.requireActiveRound(roundId, "observation_ready");
    if (round.toolCallIds.length === 0) throw new Error("revision requires at least one tool observation");
    for (const callId of round.toolCallIds) {
      const tool = this.requireTool(callId);
      if (!isToolTerminal(tool.state) || !tool.resultDigest) throw new Error(`tool result is unsettled: ${callId}`);
      this.toolObservationBudget.register({
        callId,
        roundId: round.roundId,
        toolName: tool.toolName,
        content: toolResultContent(tool),
        isError: tool.state !== "succeeded",
      });
    }
    const budgetPlan = this.toolObservationBudget.enforceRound(round.roundId);
    const resultContent: JsonValue[] = [];
    for (const callId of round.toolCallIds) {
      const tool = this.requireTool(callId);
      if (!isToolTerminal(tool.state) || !tool.resultDigest) throw new Error(`tool result is unsettled: ${callId}`);
      resultContent.push({
        type: "tool_result",
        tool_use_id: callId,
        content: this.toolObservationBudget.contentFor(callId) as JsonValue,
        is_error: tool.state !== "succeeded",
      });
    }
    this.transcript.push({
      role: "user",
      content: resultContent,
      metadata: {
        model_round_id: round.roundId,
        observation_count: resultContent.length,
        observation_budget_plan_digest: budgetPlan.digest,
        canonical_owner: "model_iteration_runtime",
      },
    });
    round.state = "completed";
    round.completedAt = timestamp();
    round.messageCountAfter = this.transcript.length;
    round.revision += 1;
    this.activeRoundId = null;
    this.phase = "ready";
    this.commit("provider.round.observed", round.roundId, {
      message_count: this.transcript.length,
      observation_count: resultContent.length,
      transcript_digest: digest(this.transcript),
    });
    return clone(this.transcript);
  }

  abandonPlannedToolRoundForFinalization(roundId: string, reason: string): ModelRoundRecord {
    this.assertOperational("abandon planned tool round for finalization");
    const round = this.requireActiveRound(roundId, "tools_running");
    const error = required(reason, "tool round finalization reason");
    for (const callId of round.toolCallIds) {
      const tool = this.requireTool(callId);
      if (tool.state !== "planned") {
        throw new Error(`tool ${callId} already entered execution from ${tool.state}`);
      }
    }
    for (const callId of round.toolCallIds) {
      const tool = this.requireTool(callId);
      tool.state = "cancelled";
      tool.error = error;
      tool.completedAt = timestamp();
      tool.resultDigest = digest({ error, call_id: callId, round_id: roundId });
      tool.revision += 1;
    }
    const abandonedMessage = this.transcript.at(-1);
    if (
      !abandonedMessage
      || asString(asObject(abandonedMessage.metadata).model_round_id) !== roundId
    ) {
      throw new Error("planned tool round is not the latest transcript message");
    }
    const abandonedMessageDigest = digest(abandonedMessage);
    this.transcript.pop();
    round.state = "failed";
    round.error = error;
    round.completedAt = timestamp();
    round.messageCountAfter = this.transcript.length;
    round.revision += 1;
    this.activeRoundId = null;
    this.phase = "ready";
    this.commit("provider.round.tools_abandoned_for_finalization", roundId, {
      reason: error,
      tool_call_ids: round.toolCallIds,
      abandoned_message_digest: abandonedMessageDigest,
    });
    return clone(round);
  }

  rejectProviderRoundForRetry(roundId: string, reason: string): ModelRoundRecord {
    this.assertOperational("reject provider round for retry");
    const round = this.requireActiveRound(roundId, "provider_running");
    const error = required(reason, "provider retry reason");
    round.state = "failed";
    round.error = error;
    round.completedAt = timestamp();
    round.revision += 1;
    this.activeRoundId = null;
    this.phase = "ready";
    this.commit("provider.round.rejected_for_retry", roundId, { reason: error });
    return clone(round);
  }

  failProviderRound(roundId: string, error: string): void {
    const round = this.requireRound(roundId);
    if (round.state === "completed" || round.state === "failed") return;
    round.state = "failed";
    round.error = required(error, "provider error");
    round.completedAt = timestamp();
    round.revision += 1;
    for (const callId of round.toolCallIds) {
      const tool = this.requireTool(callId);
      if (!isToolTerminal(tool.state)) {
        tool.state = "cancelled";
        tool.error = "provider_round_failed";
        tool.completedAt = timestamp();
        tool.resultDigest = digest({ error: tool.error, round_id: roundId });
        tool.revision += 1;
      }
    }
    this.activeRoundId = null;
    this.phase = "failed";
    this.terminalError = round.error;
    this.commit("provider.round.failed", roundId, { error: round.error });
  }

  cancel(reason: string): void {
    if (isIterationTerminal(this.phase)) return;
    const error = required(reason, "cancel reason");
    if (this.activeRoundId) {
      const round = this.requireRound(this.activeRoundId);
      if (round.state !== "completed" && round.state !== "failed") {
        round.state = "failed";
        round.error = error;
        round.completedAt = timestamp();
        round.revision += 1;
      }
    }
    for (const tool of this.tools.values()) {
      if (!isToolTerminal(tool.state)) {
        tool.state = "cancelled";
        tool.error = error;
        tool.resultDigest = digest({ error, call_id: tool.callId });
        tool.completedAt = timestamp();
        tool.revision += 1;
      }
    }
    this.activeRoundId = null;
    this.phase = "cancelled";
    this.terminalError = error;
    this.commit("iteration.cancelled", this.identity.sessionId, { reason: error });
  }

  fail(error: string): void {
    if (isIterationTerminal(this.phase)) return;
    const normalized = required(error, "iteration error");
    this.activeRoundId = null;
    this.phase = "failed";
    this.terminalError = normalized;
    this.commit("iteration.failed", this.identity.sessionId, { error: normalized });
  }

  finish(finalText = this.finalText): void {
    if (this.phase === "completed") return;
    if (this.phase !== "ready") throw new Error(`model iteration cannot finish from ${this.phase}`);
    if ([...this.tools.values()].some((tool) => !isToolTerminal(tool.state))) {
      throw new Error("model iteration cannot finish with unsettled tools");
    }
    this.finalText = finalText.trim();
    if (this.finalText) this.appendFinalAssistant(this.finalText, null);
    this.phase = "completed";
    this.commit("iteration.completed", this.identity.sessionId, {
      final_text_digest: digest(this.finalText),
      round_count: this.rounds.size,
      tool_count: this.tools.size,
    });
  }

  currentMessages(): JsonObject[] {
    return clone(this.transcript);
  }

  compactTranscript(
    messages: readonly JsonObject[],
    boundaryId: string,
  ): JsonObject[] {
    this.assertOperational("compact model transcript");
    if (this.phase !== "ready" || this.activeRoundId !== null) {
      throw new Error(`model transcript cannot compact from ${this.phase}`);
    }
    const normalized = normalizeTranscript(messages);
    if (normalized.length === 0) throw new Error("compacted model transcript cannot be empty");
    validateTranscriptToolPairs(normalized);
    const previousCount = this.transcript.length;
    const previousChars = JSON.stringify(this.transcript).length;
    const nextChars = JSON.stringify(normalized).length;
    if (nextChars >= previousChars) {
      throw new Error("model transcript compaction did not reduce provider context");
    }
    const previousDigest = digest(this.transcript);
    this.transcript.splice(0, this.transcript.length, ...normalized);
    this.commit("iteration.transcript.compacted", required(boundaryId, "compact boundary id"), {
      boundary_id: boundaryId,
      previous_message_count: previousCount,
      message_count: this.transcript.length,
      previous_chars: previousChars,
      context_chars: nextChars,
      previous_digest: previousDigest,
      transcript_digest: digest(this.transcript),
    });
    return clone(this.transcript);
  }

  currentRound(): ModelRoundRecord | null {
    return this.activeRoundId ? clone(this.requireRound(this.activeRoundId)) : null;
  }

  project(): JsonObject {
    const audit = this.audit();
    return {
      phase: this.phase,
      revision: this.revision,
      restart_epoch: this.restartEpoch,
      active_round_id: this.activeRoundId,
      round_count: this.rounds.size,
      tool_count: this.tools.size,
      message_count: this.transcript.length,
      final_text: this.finalText,
      terminal_error: this.terminalError,
      audit_ok: audit.ok,
      state_digest: audit.stateDigest,
    };
  }

  audit(): ModelIterationAudit {
    const failures: string[] = [];
    try {
      this.toolObservationBudget.audit();
    } catch (error) {
      failures.push(`tool observation budget: ${error instanceof Error ? error.message : String(error)}`);
    }
    const unsettledRoundIds: string[] = [];
    const unsettledToolIds: string[] = [];
    const orphanToolIds: string[] = [];
    const requestKeys = new Map<string, number>();
    for (const round of this.rounds.values()) {
      requestKeys.set(round.requestKey, (requestKeys.get(round.requestKey) ?? 0) + 1);
      if (round.state === "provider_running" || round.state === "tools_running" || round.state === "observation_ready") {
        unsettledRoundIds.push(round.roundId);
      }
      if (round.toolCallIds.length !== new Set(round.toolCallIds).size) failures.push(`round ${round.roundId} repeats a tool call id`);
      for (const callId of round.toolCallIds) {
        const tool = this.tools.get(callId);
        if (!tool) failures.push(`round ${round.roundId} references missing tool ${callId}`);
        else if (tool.roundId !== round.roundId) failures.push(`tool ${callId} belongs to ${tool.roundId}, not ${round.roundId}`);
      }
      if (round.state === "completed" && round.toolCallIds.length > 0) {
        for (const callId of round.toolCallIds) {
          if (!isToolTerminal(this.requireTool(callId).state)) failures.push(`completed round ${round.roundId} has unsettled tool ${callId}`);
        }
      }
    }
    for (const tool of this.tools.values()) {
      if (!this.rounds.has(tool.roundId)) orphanToolIds.push(tool.callId);
      if (!isToolTerminal(tool.state)) unsettledToolIds.push(tool.callId);
      if (isToolTerminal(tool.state) && (!tool.completedAt || !tool.resultDigest)) {
        failures.push(`terminal tool ${tool.callId} lacks completion evidence`);
      }
      if (tool.state === "succeeded" && tool.error !== null) failures.push(`successful tool ${tool.callId} has an error`);
      if (tool.state === "denied" && tool.permissionEffect === "allow") failures.push(`denied tool ${tool.callId} claims allow`);
    }
    const duplicateRequestKeys = [...requestKeys.entries()].filter(([, count]) => count > 1).map(([key]) => key);
    if (duplicateRequestKeys.length > 0) failures.push(`duplicate provider request keys: ${duplicateRequestKeys.join(",")}`);
    const transitionIds = this.transitions.map((item) => item.transitionId);
    const seen = new Set<string>();
    const replayedTransitionIds = transitionIds.filter((id) => seen.has(id) || !seen.add(id));
    if (replayedTransitionIds.length > 0) failures.push(`replayed iteration transitions: ${replayedTransitionIds.join(",")}`);
    if (this.activeRoundId && !this.rounds.has(this.activeRoundId)) failures.push("active round does not exist");
    if (isIterationTerminal(this.phase) && (unsettledRoundIds.length > 0 || unsettledToolIds.length > 0)) {
      failures.push("terminal iteration retains unsettled work");
    }
    const stateDigest = digest(this.unsignedState(false));
    return {
      ok: failures.length === 0,
      phase: this.phase,
      revision: this.revision,
      restartEpoch: this.restartEpoch,
      roundCount: this.rounds.size,
      toolCount: this.tools.size,
      unsettledRoundIds: unique(unsettledRoundIds),
      unsettledToolIds: unique(unsettledToolIds),
      orphanToolIds: unique(orphanToolIds),
      duplicateRequestKeys: unique(duplicateRequestKeys),
      replayedTransitionIds: unique(replayedTransitionIds),
      invariantFailures: failures,
      stateDigest,
    };
  }

  snapshot(): ModelIterationSnapshot {
    const audit = this.audit();
    if (!audit.ok) throw new Error(`model iteration invariant failure: ${audit.invariantFailures.join("; ")}`);
    const unsigned = this.unsignedState(true);
    assertSecretFree(unsigned, "iteration");
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshot: ModelIterationSnapshot, allowRunRebind = false): void {
    if (this.phase !== "idle" || this.revision !== 0) throw new Error("model iteration restore requires a fresh runtime");
    if (snapshot.version !== MODEL_ITERATION_SNAPSHOT_VERSION) throw new Error("unsupported model iteration snapshot version");
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) throw new Error("model iteration snapshot checksum mismatch");
    if (snapshot.identity.sessionId !== this.identity.sessionId || snapshot.identity.taskId !== this.identity.taskId) {
      throw new Error("model iteration snapshot identity mismatch");
    }
    if (!allowRunRebind && snapshot.identity.runId !== this.identity.runId) throw new Error("model iteration run identity mismatch");
    this.identity = {
      ...normalizeIdentity(snapshot.identity),
      runId: allowRunRebind ? this.identity.runId : snapshot.identity.runId,
    };
    this.phase = snapshot.phase;
    this.revision = nonnegative(snapshot.revision, "iteration revision");
    this.restartEpoch = nonnegative(snapshot.restartEpoch, "iteration restart epoch") + 1;
    this.transitionSequence = nonnegative(snapshot.transitionSequence, "iteration transition sequence");
    this.maximumRounds = snapshot.maximumRounds === null
      ? null
      : boundedInteger(snapshot.maximumRounds, 1, 100_000, "maximum rounds");
    this.maximumToolCalls = snapshot.maximumToolCalls === null
      ? null
      : boundedInteger(snapshot.maximumToolCalls, 1, 1_000_000, "maximum tool calls");
    this.rounds.clear();
    for (const round of snapshot.rounds) {
      if (this.rounds.has(round.roundId)) throw new Error(`duplicate restored model round: ${round.roundId}`);
      this.rounds.set(round.roundId, clone(round));
    }
    this.tools.clear();
    for (const tool of snapshot.tools) {
      if (this.tools.has(tool.callId)) throw new Error(`duplicate restored iteration tool: ${tool.callId}`);
      this.tools.set(tool.callId, clone(tool));
    }
    this.transcript.splice(0, this.transcript.length, ...normalizeTranscript(snapshot.transcript));
    this.transitions.splice(0, this.transitions.length, ...snapshot.transitions.map(clone));
    this.toolObservationBudget.restore(snapshot.toolObservationBudget);
    this.activeRoundId = snapshot.activeRoundId;
    this.finalText = snapshot.finalText;
    this.terminalError = snapshot.terminalError;
    if (this.phase === "provider_running") {
      const round = this.activeRoundId ? this.requireRound(this.activeRoundId) : null;
      if (round) {
        round.state = "failed";
        round.error = "provider_interrupted_by_restart";
        round.completedAt = timestamp();
        round.revision += 1;
      }
      this.activeRoundId = null;
      this.phase = "ready";
    }
    if (this.phase === "tools_running") {
      const round = this.activeRoundId ? this.requireRound(this.activeRoundId) : null;
      if (round) {
        for (const callId of round.toolCallIds) {
          const tool = this.requireTool(callId);
          if (tool.state === "running") {
            tool.state = "planned";
            tool.startedAt = null;
            tool.revision += 1;
          }
        }
      }
    }
    this.commit("iteration.restored", this.identity.sessionId, {
      prior_run_id: snapshot.identity.runId,
      current_run_id: this.identity.runId,
      restart_epoch: this.restartEpoch,
      phase: this.phase,
    });
    const audit = this.audit();
    if (!audit.ok) throw new Error(`restored model iteration invariant failure: ${audit.invariantFailures.join("; ")}`);
  }

  private replaceTranscript(messages: readonly JsonObject[]): void {
    const normalized = normalizeTranscript(messages);
    if (normalized.length === 0) throw new Error("model transcript cannot be empty");
    const currentDigest = digest(this.transcript);
    const nextDigest = digest(normalized);
    if (currentDigest === nextDigest) return;
    if (normalized.length < this.transcript.length) throw new Error("model transcript cannot discard committed messages");
    for (let index = 0; index < this.transcript.length; index += 1) {
      if (digest(this.transcript[index]) !== digest(normalized[index])) {
        throw new Error(`model transcript prefix changed at message ${index}`);
      }
    }
    this.transcript.splice(0, this.transcript.length, ...normalized);
  }

  private appendFinalAssistant(text: string, round: ModelRoundRecord | null): number {
    if (!text.trim()) return this.transcript.length;
    const last = this.transcript.at(-1);
    const content = [{ type: "text", text: text.trim() }];
    if (last?.role === "assistant" && digest(last.content) === digest(content)) return this.transcript.length;
    this.transcript.push({
      role: "assistant",
      content,
      metadata: {
        model_round_id: round?.roundId ?? null,
        provider_request_id: round?.providerRequestId ?? null,
        final_answer: true,
        canonical_owner: "model_iteration_runtime",
      },
    });
    return this.transcript.length;
  }

  private appendTruncatedAssistant(text: string, round: ModelRoundRecord): number {
    if (!text.trim()) return this.transcript.length;
    this.transcript.push({
      role: "assistant",
      content: [{ type: "text", text: text.trim() }],
      metadata: {
        model_round_id: round.roundId,
        provider_request_id: round.providerRequestId,
        final_answer: false,
        truncated: true,
        canonical_owner: "model_iteration_runtime",
      },
    });
    return this.transcript.length;
  }

  private requireActiveRound(roundId: string, expected: ModelRoundState): ModelRoundRecord {
    const round = this.requireRound(roundId);
    if (this.activeRoundId !== roundId) throw new Error(`model round is not active: ${roundId}`);
    if (round.state !== expected) throw new Error(`model round ${roundId} is ${round.state}; expected ${expected}`);
    return round;
  }

  private requireRound(roundId: string): ModelRoundRecord {
    const round = this.rounds.get(required(roundId, "round id"));
    if (!round) throw new Error(`model round not found: ${roundId}`);
    return round;
  }

  private requireTool(callId: string): IterationToolRecord {
    const tool = this.tools.get(required(callId, "tool call id"));
    if (!tool) throw new Error(`iteration tool not found: ${callId}`);
    return tool;
  }

  private assertOperational(operation: string): void {
    if (this.phase === "idle") throw new Error(`${operation} requires a started model iteration`);
    if (isIterationTerminal(this.phase)) throw new Error(`${operation} cannot run after ${this.phase}`);
  }

  private commit(operation: string, subjectId: string, payload: JsonObject): IterationTransition {
    const revisionBefore = this.revision;
    this.revision += 1;
    this.transitionSequence += 1;
    const transition: IterationTransition = {
      transitionId: digest({
        session_id: this.identity.sessionId,
        run_id: this.identity.runId,
        epoch: this.restartEpoch,
        sequence: this.transitionSequence,
        revision: this.revision,
        operation,
        subject_id: subjectId,
        nonce: randomUUID(),
      }),
      epoch: this.restartEpoch,
      sequence: this.transitionSequence,
      revisionBefore,
      revisionAfter: this.revision,
      operation,
      subjectId,
      payloadDigest: digest(payload),
      stateDigest: digest(this.unsignedState(false)),
      occurredAt: timestamp(),
    };
    if (this.transitions.some((item) => item.transitionId === transition.transitionId)) {
      throw new Error(`model iteration transition replayed: ${transition.transitionId}`);
    }
    this.transitions.push(transition);
    return clone(transition);
  }

  private unsignedState(includeTransitions: boolean): Omit<ModelIterationSnapshot, "checksum"> {
    return {
      version: MODEL_ITERATION_SNAPSHOT_VERSION,
      identity: clone(this.identity),
      phase: this.phase,
      revision: this.revision,
      restartEpoch: this.restartEpoch,
      transitionSequence: this.transitionSequence,
      maximumRounds: this.maximumRounds,
      maximumToolCalls: this.maximumToolCalls,
      rounds: [...this.rounds.values()].map(clone).sort((left, right) => left.roundIndex - right.roundIndex),
      tools: [...this.tools.values()].map(clone).sort((left, right) => left.callId.localeCompare(right.callId)),
      transcript: clone(this.transcript),
      transitions: includeTransitions ? this.transitions.map(clone) : [],
      toolObservationBudget: this.toolObservationBudget.snapshot(),
      activeRoundId: this.activeRoundId,
      finalText: this.finalText,
      terminalError: this.terminalError,
    };
  }
}

function normalizeIdentity(value: ModelIterationIdentity): ModelIterationIdentity {
  return {
    sessionId: required(value.sessionId, "session id"),
    runId: required(value.runId, "run id"),
    taskId: required(value.taskId, "task id"),
    workerRequestId: required(value.workerRequestId, "worker request id"),
  };
}

function normalizeTranscript(values: readonly JsonObject[]): JsonObject[] {
  const output: JsonObject[] = [];
  for (const raw of values) {
    const value = cloneObject(raw);
    const role = asString(value.role, "user").toLowerCase();
    if (!role) continue;
    const normalizedRole = role === "assistant" ? "assistant" : role === "system" ? "system" : "user";
    const content = normalizeMessageContent(value.content);
    if ((typeof content === "string" || Array.isArray(content)) && content.length === 0) continue;
    const message: JsonObject = { role: normalizedRole, content };
    const metadata = asObject(value.metadata);
    if (Object.keys(metadata).length > 0) message.metadata = cloneObject(metadata);
    output.push(message);
  }
  return output;
}

function validateTranscriptToolPairs(messages: readonly JsonObject[]): void {
  const toolUses = new Set<string>();
  const toolResults = new Set<string>();
  for (const message of messages) {
    const content = Array.isArray(message.content) ? message.content : [];
    for (const candidate of content) {
      const block = asObject(candidate);
      if (asString(block.type) === "tool_use") {
        const id = required(asString(block.id), "compacted tool use id");
        if (toolUses.has(id)) throw new Error(`compacted transcript repeats tool use ${id}`);
        toolUses.add(id);
      }
      if (asString(block.type) === "tool_result") {
        const id = required(
          asString(block.tool_use_id || block.toolUseId || block.toolCallId),
          "compacted tool result id",
        );
        if (!toolUses.has(id)) throw new Error(`compacted transcript has orphan tool result ${id}`);
        if (toolResults.has(id)) throw new Error(`compacted transcript repeats tool result ${id}`);
        toolResults.add(id);
      }
    }
  }
}

function normalizeMessageContent(value: JsonValue | undefined): JsonValue {
  if (typeof value === "string") return value.trim() ? value : " ";
  if (!Array.isArray(value)) {
    if (value === null || value === undefined) return "";
    return JSON.stringify(value);
  }
  const blocks: JsonValue[] = [];
  for (const raw of value) {
    if (typeof raw === "string") {
      if (raw.trim()) blocks.push({ type: "text", text: raw });
      continue;
    }
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) continue;
    const block = cloneObject(raw as JsonObject);
    const type = asString(block.type, "text");
    if (type === "text") {
      blocks.push({ type, text: asString(block.text, asString(block.content, " ")) });
    } else if (type === "tool_use") {
      blocks.push({
        type,
        id: required(asString(block.id), "tool use id"),
        name: required(asString(block.name), "tool use name"),
        input: cloneObject(asObject(block.input)),
      });
    } else if (type === "tool_result") {
      blocks.push({
        type,
        tool_use_id: required(asString(block.tool_use_id ?? block.toolUseId), "tool result id"),
        content: block.content ?? "",
        is_error: block.is_error === true || block.isError === true,
      });
    } else {
      blocks.push(block);
    }
  }
  return blocks;
}

function normalizeSteps(values: readonly ToolStep[]): ToolStep[] {
  return values.map((value, index) => ({
    ...value,
    step_id: required(value.step_id ?? `provider-tool-${index}`, "provider tool call id"),
    tool_name: required(value.tool_name, "provider tool name"),
    arguments: cloneObject(value.arguments),
    metadata: cloneObject(asObject(value.metadata)),
  }));
}

function toolResultContent(tool: IterationToolRecord): JsonValue {
  const payload: JsonObject = {
    summary: tool.summary,
    output: projectToolOutputForRuntime(tool.output),
    error: tool.error,
    state: tool.state,
    result_digest: tool.resultDigest,
  };
  const encoded = JSON.stringify(payload);
  return encoded.length > 64_000
    ? JSON.stringify({ summary: tool.summary, error: tool.error, state: tool.state, result_digest: tool.resultDigest, output_omitted: true })
    : encoded;
}

function inferPermissionEffect(error: string | null): "allow" | "deny" | "ask" | "unknown" {
  if (error === "permission_denied") return "deny";
  if (error === "permission_approval_required") return "ask";
  return "unknown";
}

function isToolTerminal(value: IterationToolState): boolean {
  return value === "succeeded" || value === "failed" || value === "denied" || value === "cancelled";
}

function isIterationTerminal(value: ModelIterationPhase): boolean {
  return value === "completed" || value === "failed" || value === "cancelled";
}

function isProviderOutputTruncated(value: string | null): boolean {
  const normalized = String(value ?? "").trim().toLowerCase().replaceAll("-", "_");
  return normalized === "length"
    || normalized === "max_tokens"
    || normalized === "maximum_tokens";
}

function assertSecretFree(value: unknown, path: string): void {
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertSecretFree(item, `${path}[${index}]`));
    return;
  }
  if (!value || typeof value !== "object") return;
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    if (
      key === "secret_redaction_state"
      && typeof item === "string"
      && ["clean", "redacted", "unknown"].includes(item)
    ) {
      continue;
    }
    if (/api.?key|authorization|access.?token|secret|password|cookie/i.test(key)) {
      if (typeof item === "string" && item && !/digest|fingerprint/i.test(key)) {
        throw new Error(`model iteration snapshot contains secret-like field: ${path}.${key}`);
      }
    }
    assertSecretFree(item, `${path}.${key}`);
  }
}

function nullable(value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  const normalized = value.trim();
  return normalized || null;
}

function required(value: string, name: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${name} is required`);
  return normalized;
}

function boundedInteger(value: number, minimum: number, maximum: number, name: string): number {
  if (!Number.isFinite(value)) throw new Error(`${name} must be finite`);
  const normalized = Math.floor(value);
  if (normalized < minimum || normalized > maximum) throw new Error(`${name} must be between ${minimum} and ${maximum}`);
  return normalized;
}

function nonnegative(value: number, name: string): number {
  if (!Number.isSafeInteger(value) || value < 0) throw new Error(`${name} must be a non-negative integer`);
  return value;
}

function cloneObject(value: JsonObject): JsonObject {
  return structuredClone(value);
}

function clone<T>(value: T): T {
  return structuredClone(value);
}

function unique(values: readonly string[]): string[] {
  return [...new Set(values)].sort((left, right) => left.localeCompare(right));
}

function timestamp(): string {
  return new Date().toISOString();
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonical(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function digest(value: unknown): string {
  return `sha256:${createHash("sha256").update(canonical(value)).digest("hex")}`;
}
