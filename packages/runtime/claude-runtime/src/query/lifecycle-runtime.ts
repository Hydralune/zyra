import { createHash, randomUUID } from "node:crypto";

import { asBoolean, asObject, asString, type JsonObject, type JsonValue } from "../contracts.ts";

export const QUERY_LIFECYCLE_SNAPSHOT_VERSION = "zyra.query-lifecycle/v1";

export type QueryLifecycleStatus =
  | "created"
  | "admitted"
  | "queued"
  | "running"
  | "waiting_tool"
  | "revising"
  | "compacting"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";
export type QueryTurnStatus =
  | "planned"
  | "sampling"
  | "executing_tools"
  | "observing"
  | "completed"
  | "failed"
  | "cancelled";
export type QueryStopReason =
  | "end_turn"
  | "max_turns"
  | "budget_exhausted"
  | "aborted"
  | "model_error"
  | "tool_error"
  | "permission_denied"
  | "empty_turn"
  | "structured_output"
  | "user_cancelled"
  | "runtime_invariant"
  | "unknown";
export type QueryInputKind = "prompt" | "side_question" | "control" | "system_notice";
export type QueryPriority = "background" | "normal" | "interactive" | "critical";
export type QueryTransitionEffect = "state" | "tool" | "context" | "control" | "none";

export interface QueryIdentity {
  queryId: string;
  sessionId: string;
  runId: string;
  taskId: string;
  workerRequestId: string;
  parentQueryId: string | null;
  branchId: string;
}

export interface QueryInput {
  inputId: string;
  kind: QueryInputKind;
  content: string;
  priority: QueryPriority;
  idempotencyKey: string;
  correlationId: string;
  metadata: JsonObject;
  createdAt: string;
}

export interface QueryAdmission {
  admissionId: string;
  accepted: boolean;
  reason: string;
  queuePosition: number;
  effectivePriority: number;
  duplicateOf: string | null;
  admittedAt: string;
}

export interface QueryToolCall {
  toolCallId: string;
  turnId: string;
  name: string;
  arguments: JsonObject;
  argumentsDigest: string;
  readOnly: boolean;
  status: "planned" | "permission_pending" | "running" | "succeeded" | "failed" | "cancelled";
  permissionDecisionId: string | null;
  resultDigest: string | null;
  resultSummary: string | null;
  error: string | null;
  startedAt: string | null;
  completedAt: string | null;
  revision: number;
}

export interface QueryTurn {
  turnId: string;
  index: number;
  status: QueryTurnStatus;
  model: string;
  inputIds: string[];
  messageDigestBefore: string;
  messageDigestAfter: string | null;
  toolCallIds: string[];
  inputTokens: number;
  outputTokens: number;
  resultChars: number;
  samplingAttempt: number;
  stopReason: string | null;
  error: string | null;
  startedAt: string | null;
  completedAt: string | null;
  revision: number;
}

export interface QueryTransition {
  transitionId: string;
  sequence: number;
  queryId: string;
  turnId: string | null;
  from: QueryLifecycleStatus;
  to: QueryLifecycleStatus;
  cause: string;
  effect: QueryTransitionEffect;
  correlationId: string;
  idempotencyKey: string;
  payloadDigest: string;
  stateDigest: string;
  revision: number;
  createdAt: string;
}

export interface QueryBudget {
  maximumTurns: number | null;
  maximumInputTokens: number | null;
  maximumOutputTokens: number | null;
  maximumToolCalls: number | null;
  maximumToolResultChars: number | null;
  wallTimeMs: number | null;
  consumedTurns: number;
  consumedInputTokens: number;
  consumedOutputTokens: number;
  consumedToolCalls: number;
  consumedToolResultChars: number;
  startedAt: string;
}

export interface QueryControlState {
  paused: boolean;
  abortRequested: boolean;
  abortReason: string | null;
  pendingCompact: boolean;
  pendingModel: string | null;
  permissionMode: string;
  lastControlId: string | null;
  revision: number;
}

export interface QueryLifecycleSnapshot {
  version: typeof QUERY_LIFECYCLE_SNAPSHOT_VERSION;
  identity: QueryIdentity;
  status: QueryLifecycleStatus;
  revision: number;
  sequence: number;
  restartEpoch: number;
  inputs: QueryInput[];
  admissions: QueryAdmission[];
  queuedInputIds: string[];
  turns: QueryTurn[];
  toolCalls: QueryToolCall[];
  transitions: QueryTransition[];
  budget: QueryBudget;
  control: QueryControlState;
  activeTurnId: string | null;
  stopReason: QueryStopReason | null;
  terminalError: string | null;
  committedIdempotencyKeys: string[];
  resumeCorrelationIds: string[];
  checksum: string;
}

export interface QueryLifecycleOptions {
  identity: QueryIdentity;
  budget?: Partial<Omit<QueryBudget, "consumedTurns" | "consumedInputTokens" | "consumedOutputTokens" | "consumedToolCalls" | "consumedToolResultChars" | "startedAt">>;
  initialPermissionMode?: string;
}

export interface QueryTurnPlan {
  turnId?: string;
  model: string;
  messageDigest: string;
  inputIds: readonly string[];
}

export interface QueryAskInput {
  commands?: readonly string[];
  prompt: JsonValue;
  promptUuid?: string;
  cwd?: string;
  tools?: readonly string[];
  maxTurns?: number | null;
  maxBudgetUsd?: number | null;
  mutableMessages?: readonly JsonValue[];
  userSpecifiedModel?: string | null;
  abortRequested?: boolean;
  inputId: string;
  idempotencyKey: string;
  correlationId: string;
  createdAt?: string;
}

export interface QueryLoopContinuation {
  messagesForQuery: readonly JsonValue[];
  assistantMessages: readonly JsonValue[];
  toolResults: readonly JsonValue[];
  turnCount: number;
  maxTurns?: number | null;
}

export interface ToolPlanInput {
  toolCallId?: string;
  name: string;
  arguments: JsonObject;
  readOnly: boolean;
}

export interface ToolResultInput {
  toolCallId: string;
  ok: boolean;
  summary: string;
  result: JsonValue;
  error?: string | null;
  resultChars?: number;
  completedAt?: string;
}

export interface QueryStopDecision {
  stop: boolean;
  reason: QueryStopReason | null;
  compactFirst: boolean;
  abortTools: boolean;
  details: JsonObject;
}

const STATUS_TRANSITIONS: Readonly<Record<QueryLifecycleStatus, readonly QueryLifecycleStatus[]>> = {
  created: ["admitted", "cancelled", "failed"],
  admitted: ["queued", "running", "cancelled", "failed"],
  queued: ["running", "paused", "completed", "cancelled", "failed"],
  running: ["waiting_tool", "revising", "compacting", "paused", "completed", "failed", "cancelled"],
  waiting_tool: ["revising", "running", "paused", "failed", "cancelled"],
  revising: ["running", "waiting_tool", "compacting", "paused", "completed", "failed", "cancelled"],
  compacting: ["running", "paused", "failed", "cancelled"],
  paused: ["queued", "running", "cancelled", "failed"],
  completed: ["created"],
  failed: ["created"],
  cancelled: ["created"],
};

export class QueryLifecycleRuntime {
  private readonly identity: QueryIdentity;
  private readonly inputs = new Map<string, QueryInput>();
  private readonly admissions = new Map<string, QueryAdmission>();
  private readonly queue: string[] = [];
  private readonly turns = new Map<string, QueryTurn>();
  private readonly toolCalls = new Map<string, QueryToolCall>();
  private readonly transitions: QueryTransition[] = [];
  private readonly committedIdempotencyKeys = new Set<string>();
  private readonly resumeCorrelationIds = new Set<string>();
  private budget: QueryBudget;
  private control: QueryControlState;
  private status: QueryLifecycleStatus = "created";
  private revision = 0;
  private sequence = 0;
  private restartEpoch = 0;
  private activeTurnId: string | null = null;
  private stopReason: QueryStopReason | null = null;
  private terminalError: string | null = null;

  constructor(options: QueryLifecycleOptions) {
    this.identity = normalizeIdentity(options.identity);
    this.budget = normalizeBudget(options.budget);
    this.control = {
      paused: false,
      abortRequested: false,
      abortReason: null,
      pendingCompact: false,
      pendingModel: null,
      permissionMode: options.initialPermissionMode?.trim() || "default",
      lastControlId: null,
      revision: 0,
    };
  }

  QueryEngine_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "inspect");
    if (action === "admit") {
      const input = queryInputFromJson(asObject(value.input));
      const admission = this.admit(input);
      return admissionToJson(admission);
    }
    if (action === "control") return this.applyControl(asObject(value.command));
    if (action === "stop_decision") return stopDecisionToJson(this.shouldStop());
    if (action === "snapshot") return this.snapshot() as unknown as JsonObject;
    return this.project();
  }

  query_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "inspect");
    if (action === "start_turn") {
      const turn = this.startTurn({
        model: asString(value.model, "unknown"),
        messageDigest: asString(value.message_digest, digest(value.messages ?? [])),
        inputIds: Array.isArray(value.input_ids) ? value.input_ids.map(String) : [],
      });
      return turnToJson(turn);
    }
    if (action === "plan_tools") {
      const plans = Array.isArray(value.tools) ? value.tools.map((item) => toolPlanFromJson(asObject(item))) : [];
      return { tools: this.planTools(asString(value.turn_id), plans).map(toolCallToJson) };
    }
    if (action === "tool_result") return toolCallToJson(this.recordToolResult(toolResultFromJson(value)));
    if (action === "finish_turn") {
      return turnToJson(this.completeTurn(asString(value.turn_id), {
        messageDigest: asString(value.message_digest),
        inputTokens: integer(value.input_tokens, 0),
        outputTokens: integer(value.output_tokens, 0),
        stopReason: asString(value.stop_reason, "end_turn"),
      }));
    }
    return this.project();
  }

  /** Adapted from QueryEngine.ask: product/UI inputs are cropped at admission. */
  ask(input: QueryAskInput): QueryAdmission {
    if (process.env.ZYRA_DISABLE_E04_QUERY_SOURCE_RUNTIME === "1") {
      throw new Error("e04_query_source_runtime_disabled");
    }
    const prompt = typeof input.prompt === "string"
      ? input.prompt
      : canonicalJson(input.prompt);
    return this.admit({
      inputId: input.inputId,
      kind: "prompt",
      content: prompt,
      priority: "interactive",
      idempotencyKey: input.idempotencyKey,
      correlationId: input.correlationId,
      metadata: {
        source: "claude_query_engine_ask",
        prompt_uuid: input.promptUuid ?? null,
        cwd: input.cwd ?? null,
        command_count: input.commands?.length ?? 0,
        tool_count: input.tools?.length ?? 0,
        max_turns: input.maxTurns ?? null,
        max_budget_usd: input.maxBudgetUsd ?? null,
        mutable_message_count: input.mutableMessages?.length ?? 0,
        user_specified_model: input.userSpecifiedModel ?? null,
        abort_requested: input.abortRequested ?? false,
      },
      createdAt: input.createdAt ?? new Date().toISOString(),
    });
  }

  resumeForContinuation(correlationId: string): void {
    if (!this.isTerminal()) return;
    if (process.env.ZYRA_DISABLE_E04_QUERY_SOURCE_RUNTIME === "1") {
      throw new Error("e04_query_source_runtime_disabled");
    }
    this.restartEpoch += 1;
    this.stopReason = null;
    this.terminalError = null;
    this.activeTurnId = null;
    this.transition(
      "created",
      "query_resume_continuation",
      "state",
      correlationId,
      `query:${this.identity.queryId}:resume:${this.restartEpoch}`,
      { restart_epoch: this.restartEpoch },
    );
    this.bump();
  }

  /** Adapted from queryLoop's observation-to-next-turn tail. */
  advanceAfterObservation(input: QueryLoopContinuation): JsonObject {
    if (process.env.ZYRA_DISABLE_E04_QUERY_SOURCE_RUNTIME === "1") {
      throw new Error("e04_query_source_runtime_disabled");
    }
    const nextTurnCount = input.turnCount + 1;
    if (input.maxTurns && nextTurnCount > input.maxTurns) {
      this.recordSameState(
        "query_max_turns_reached",
        "state",
        this.identity.queryId,
        `query:${this.identity.queryId}:max-turns:${nextTurnCount}`,
        { max_turns: input.maxTurns, turn_count: nextTurnCount },
      );
      this.bump();
      return { reason: "max_turns", turn_count: nextTurnCount, messages: [] };
    }
    const messages = [
      ...input.messagesForQuery,
      ...input.assistantMessages,
      ...input.toolResults,
    ].map((message) => structuredClone(message));
    this.recordSameState(
      "query_recursive_call",
      "state",
      this.identity.queryId,
      `query:${this.identity.queryId}:next-turn:${nextTurnCount}:${digest(messages)}`,
      {
        reason: "next_turn",
        turn_count: nextTurnCount,
        observation_count: input.toolResults.length,
        message_digest: digest(messages),
      },
    );
    this.bump();
    return {
      reason: "next_turn",
      turn_count: nextTurnCount,
      messages,
      transition: { reason: "next_turn" },
    };
  }

  admit(input: QueryInput): QueryAdmission {
    const normalized = normalizeInput(input);
    const duplicate = this.findByIdempotencyKey(normalized.idempotencyKey);
    if (duplicate) {
      const existing = this.admissions.get(duplicate.inputId);
      return {
        admissionId: randomUUID(),
        accepted: false,
        reason: "duplicate_idempotency_key",
        queuePosition: existing?.queuePosition ?? -1,
        effectivePriority: priorityScore(duplicate.priority),
        duplicateOf: duplicate.inputId,
        admittedAt: new Date().toISOString(),
      };
    }
    if (this.isTerminal()) {
      return {
        admissionId: randomUUID(),
        accepted: false,
        reason: `query_is_${this.status}`,
        queuePosition: -1,
        effectivePriority: priorityScore(normalized.priority),
        duplicateOf: null,
        admittedAt: new Date().toISOString(),
      };
    }
    this.inputs.set(normalized.inputId, normalized);
    this.queue.push(normalized.inputId);
    this.sortQueue();
    const admission: QueryAdmission = {
      admissionId: randomUUID(),
      accepted: true,
      reason: "accepted",
      queuePosition: this.queue.indexOf(normalized.inputId),
      effectivePriority: priorityScore(normalized.priority),
      duplicateOf: null,
      admittedAt: new Date().toISOString(),
    };
    this.admissions.set(normalized.inputId, admission);
    if (this.status === "created") this.transition("admitted", "query_input_admitted", "state", normalized.correlationId, normalized.idempotencyKey, inputToJson(normalized));
    if (this.status === "admitted") this.transition("queued", "query_input_queued", "state", normalized.correlationId, `${normalized.idempotencyKey}:queue`, { input_id: normalized.inputId });
    this.committedIdempotencyKeys.add(normalized.idempotencyKey);
    this.bump();
    this.reindexAdmissions();
    return structuredClone(admission);
  }

  dequeue(maximum = 1): QueryInput[] {
    if (this.control.paused) return [];
    const count = Math.max(0, Math.floor(maximum));
    const selected: QueryInput[] = [];
    while (selected.length < count && this.queue.length > 0) {
      const id = this.queue.shift()!;
      const input = this.inputs.get(id);
      if (input) selected.push(structuredClone(input));
    }
    this.reindexAdmissions();
    if (selected.length > 0 && (this.status === "queued" || this.status === "admitted" || this.status === "paused")) {
      const correlation = selected[0].correlationId;
      this.transition("running", "query_inputs_dequeued", "state", correlation, `${correlation}:dequeue:${this.revision}`, {
        input_ids: selected.map((item) => item.inputId),
      });
    }
    return selected;
  }

  startTurn(plan: QueryTurnPlan): QueryTurn {
    this.requireRunnable();
    if (this.activeTurnId) throw new Error(`query already has active turn ${this.activeTurnId}`);
    const decision = this.shouldStop();
    if (decision.stop) throw new Error(`query cannot start turn: ${decision.reason}`);
    const inputIds = plan.inputIds.length > 0 ? [...new Set(plan.inputIds)] : this.dequeue(16).map((item) => item.inputId);
    for (const id of inputIds) if (!this.inputs.has(id)) throw new Error(`turn references unknown input: ${id}`);
    const turn: QueryTurn = {
      turnId: plan.turnId?.trim() || randomUUID(),
      index: this.turns.size,
      status: "sampling",
      model: required(plan.model, "model"),
      inputIds,
      messageDigestBefore: required(plan.messageDigest, "message digest"),
      messageDigestAfter: null,
      toolCallIds: [],
      inputTokens: 0,
      outputTokens: 0,
      resultChars: 0,
      samplingAttempt: 1,
      stopReason: null,
      error: null,
      startedAt: new Date().toISOString(),
      completedAt: null,
      revision: 1,
    };
    this.turns.set(turn.turnId, turn);
    this.activeTurnId = turn.turnId;
    if (this.status !== "running") {
      this.transition("running", "turn_started", "state", turn.turnId, `turn:${turn.turnId}:start`, turnToJson(turn), turn.turnId);
    } else {
      this.recordSameState("turn_started", "state", turn.turnId, `turn:${turn.turnId}:start`, turnToJson(turn), turn.turnId);
    }
    this.bump();
    return structuredClone(turn);
  }

  markSamplingRetry(turnId: string, error: string): QueryTurn {
    const turn = this.requireTurn(turnId);
    if (turn.status !== "sampling") throw new Error(`turn is not sampling: ${turn.status}`);
    turn.samplingAttempt += 1;
    turn.error = error.trim().slice(0, 4_096);
    turn.revision += 1;
    this.recordSameState("sampling_retry", "state", turnId, `turn:${turnId}:sample:${turn.samplingAttempt}`, {
      sampling_attempt: turn.samplingAttempt,
      error: turn.error,
    }, turnId);
    this.bump();
    return structuredClone(turn);
  }

  planTools(turnId: string, plans: readonly ToolPlanInput[]): QueryToolCall[] {
    const turn = this.requireTurn(turnId);
    if (turn.status !== "sampling" && turn.status !== "observing") {
      throw new Error(`cannot plan tools while turn is ${turn.status}`);
    }
    const result: QueryToolCall[] = [];
    const ids = new Set<string>();
    for (const plan of plans) {
      const toolCallId = plan.toolCallId?.trim() || randomUUID();
      if (ids.has(toolCallId) || this.toolCalls.has(toolCallId)) throw new Error(`duplicate tool call id: ${toolCallId}`);
      ids.add(toolCallId);
      const call: QueryToolCall = {
        toolCallId,
        turnId,
        name: required(plan.name, "tool name"),
        arguments: asObject(plan.arguments),
        argumentsDigest: digest(plan.arguments),
        readOnly: plan.readOnly,
        status: "planned",
        permissionDecisionId: null,
        resultDigest: null,
        resultSummary: null,
        error: null,
        startedAt: null,
        completedAt: null,
        revision: 1,
      };
      this.toolCalls.set(call.toolCallId, call);
      turn.toolCallIds.push(call.toolCallId);
      result.push(structuredClone(call));
    }
    if (result.length > 0) {
      turn.status = "executing_tools";
      turn.revision += 1;
      this.transition("waiting_tool", "tool_calls_planned", "tool", turnId, `turn:${turnId}:tools:${turn.revision}`, {
        tool_call_ids: result.map((item) => item.toolCallId),
        tool_names: result.map((item) => item.name),
      }, turnId);
    }
    this.bump();
    return result;
  }

  requestPermission(toolCallId: string): QueryToolCall {
    const call = this.requireToolCall(toolCallId);
    if (call.status !== "planned") throw new Error(`tool call cannot request permission from ${call.status}`);
    call.status = "permission_pending";
    call.revision += 1;
    this.recordSameState("tool_permission_requested", "control", call.turnId, `tool:${toolCallId}:permission`, {
      tool_call_id: toolCallId,
      tool_name: call.name,
      arguments_digest: call.argumentsDigest,
    }, call.turnId);
    this.bump();
    return structuredClone(call);
  }

  resolvePermission(toolCallId: string, decisionId: string, allowed: boolean): QueryToolCall {
    const call = this.requireToolCall(toolCallId);
    if (call.status !== "permission_pending" && call.status !== "planned") {
      throw new Error(`tool permission cannot resolve from ${call.status}`);
    }
    call.permissionDecisionId = required(decisionId, "permission decision id");
    if (allowed) {
      call.status = "planned";
      call.error = null;
    } else {
      call.status = "failed";
      call.error = "permission_denied";
      call.completedAt = new Date().toISOString();
    }
    call.revision += 1;
    this.recordSameState(
      allowed ? "tool_permission_allowed" : "tool_permission_denied",
      "control",
      decisionId,
      `tool:${toolCallId}:permission:${decisionId}`,
      { tool_call_id: toolCallId, decision_id: decisionId, allowed },
      call.turnId,
    );
    this.bump();
    return structuredClone(call);
  }

  startTool(toolCallId: string): QueryToolCall {
    const call = this.requireToolCall(toolCallId);
    if (call.status !== "planned") throw new Error(`tool call cannot start from ${call.status}`);
    const decision = this.shouldStop();
    if (decision.abortTools) throw new Error(`tool call blocked: ${decision.reason}`);
    call.status = "running";
    call.startedAt = new Date().toISOString();
    call.revision += 1;
    this.recordSameState("tool_started", "tool", toolCallId, `tool:${toolCallId}:start`, toolCallToJson(call), call.turnId);
    this.bump();
    return structuredClone(call);
  }

  recordToolResult(input: ToolResultInput): QueryToolCall {
    const call = this.requireToolCall(input.toolCallId);
    if (call.status === "succeeded" || call.status === "failed") {
      const incoming = digest(input.result);
      if (call.resultDigest !== incoming) throw new Error(`conflicting duplicate tool result: ${input.toolCallId}`);
      return structuredClone(call);
    }
    if (call.status !== "running" && call.status !== "planned") {
      throw new Error(`tool result cannot settle call from ${call.status}`);
    }
    call.status = input.ok ? "succeeded" : "failed";
    call.resultDigest = digest(input.result);
    call.resultSummary = input.summary.trim().slice(0, 4_096);
    call.error = input.ok ? null : (input.error ?? "tool_failed").trim().slice(0, 4_096);
    call.completedAt = normalizeTimestamp(input.completedAt);
    call.revision += 1;
    const turn = this.requireTurn(call.turnId);
    const resultChars = input.resultChars ?? canonicalJson(input.result).length;
    turn.resultChars += Math.max(0, Math.floor(resultChars));
    turn.revision += 1;
    this.budget.consumedToolCalls += 1;
    this.budget.consumedToolResultChars += Math.max(0, Math.floor(resultChars));
    this.recordSameState(
      input.ok ? "tool_succeeded" : "tool_failed",
      "tool",
      call.toolCallId,
      `tool:${call.toolCallId}:result:${call.resultDigest}`,
      {
        tool_call_id: call.toolCallId,
        result_digest: call.resultDigest,
        ok: input.ok,
        result_chars: resultChars,
        error: call.error,
      },
      call.turnId,
    );
    if (this.turnToolsSettled(turn)) {
      turn.status = "observing";
      turn.revision += 1;
      if (this.status === "waiting_tool") {
        this.transition("revising", "tool_batch_settled", "state", turn.turnId, `turn:${turn.turnId}:observe`, {
          turn_id: turn.turnId,
          tool_call_ids: turn.toolCallIds,
        }, turn.turnId);
      }
    }
    this.bump();
    return structuredClone(call);
  }

  completeTurn(turnId: string, input: {
    messageDigest: string;
    inputTokens: number;
    outputTokens: number;
    stopReason: string;
  }): QueryTurn {
    const turn = this.requireTurn(turnId);
    if (turn.status === "completed") return structuredClone(turn);
    if (turn.status === "failed" || turn.status === "cancelled") {
      throw new Error(`cannot complete terminal turn: ${turn.status}`);
    }
    if (!this.turnToolsSettled(turn)) throw new Error("cannot complete turn with unsettled tool calls");
    turn.status = "completed";
    turn.messageDigestAfter = required(input.messageDigest, "message digest");
    turn.inputTokens = Math.max(0, Math.floor(input.inputTokens));
    turn.outputTokens = Math.max(0, Math.floor(input.outputTokens));
    turn.stopReason = input.stopReason.trim() || "end_turn";
    turn.completedAt = new Date().toISOString();
    turn.revision += 1;
    this.activeTurnId = null;
    this.budget.consumedTurns += 1;
    this.budget.consumedInputTokens += turn.inputTokens;
    this.budget.consumedOutputTokens += turn.outputTokens;
    this.recordSameState("turn_completed", "state", turnId, `turn:${turnId}:complete`, turnToJson(turn), turnId);
    const decision = this.shouldStop();
    if (decision.stop) this.finish(decision.reason ?? "unknown");
    else if (turn.stopReason === "end_turn" && this.queue.length === 0) this.finish("end_turn");
    else if (!this.isTerminal() && this.status !== "running") {
      this.transition("running", "turn_revision_ready", "state", turnId, `turn:${turnId}:revise`, { turn_id: turnId }, turnId);
    }
    this.bump();
    return structuredClone(turn);
  }

  failTurn(turnId: string, error: string, terminal = true): QueryTurn {
    const turn = this.requireTurn(turnId);
    if (turn.status === "completed") throw new Error("completed turn cannot fail");
    turn.status = "failed";
    turn.error = error.trim().slice(0, 8_192);
    turn.completedAt = new Date().toISOString();
    turn.revision += 1;
    this.cancelTurnTools(turn, "turn_failed");
    this.activeTurnId = null;
    this.recordSameState("turn_failed", "state", turnId, `turn:${turnId}:failed`, {
      turn_id: turnId,
      error: turn.error,
      terminal,
    }, turnId);
    if (terminal) this.fail("model_error", turn.error);
    else if (!this.isTerminal() && this.status !== "running") {
      this.transition("running", "turn_retry_ready", "state", turnId, `turn:${turnId}:retry`, { turn_id: turnId }, turnId);
    }
    this.bump();
    return structuredClone(turn);
  }

  requestCompaction(correlationId: string): void {
    if (this.isTerminal()) return;
    this.control.pendingCompact = true;
    this.control.revision += 1;
    if (this.status === "running" || this.status === "revising") {
      this.transition("compacting", "context_compaction_requested", "context", correlationId, `compact:${correlationId}:${this.revision}`, {
        compact_generation_requested: this.control.revision,
      }, this.activeTurnId);
    }
    this.bump();
  }

  completeCompaction(correlationId: string, contextDigest: string): void {
    if (!this.control.pendingCompact) throw new Error("no context compaction is pending");
    this.control.pendingCompact = false;
    this.control.revision += 1;
    if (this.status === "compacting") {
      this.transition("running", "context_compaction_completed", "context", correlationId, `compact:${correlationId}:complete`, {
        context_digest: contextDigest,
      }, this.activeTurnId);
    } else {
      this.recordSameState("context_compaction_completed", "context", correlationId, `compact:${correlationId}:complete`, {
        context_digest: contextDigest,
      }, this.activeTurnId);
    }
    this.bump();
  }

  applyControl(command: JsonObject): JsonObject {
    const controlId = asString(command.control_id, randomUUID());
    const kind = asString(command.kind, asString(command.command));
    const expectedRevision = command.expected_revision;
    if (typeof expectedRevision === "number" && expectedRevision !== this.control.revision) {
      throw new Error(`control revision conflict: expected ${expectedRevision}, actual ${this.control.revision}`);
    }
    if (this.control.lastControlId === controlId) return this.controlToJson();
    if (kind === "pause") this.pause(controlId);
    else if (kind === "resume") this.resume(controlId, asString(command.resume_correlation_id, controlId));
    else if (kind === "cancel" || kind === "abort") this.cancel(controlId, asString(command.reason, "user_cancelled"));
    else if (kind === "compact") this.requestCompaction(controlId);
    else if (kind === "set_model") {
      this.control.pendingModel = required(asString(command.model), "model");
      this.control.lastControlId = controlId;
      this.control.revision += 1;
      this.recordSameState("model_change_requested", "control", controlId, `control:${controlId}`, {
        model: this.control.pendingModel,
      }, this.activeTurnId);
    } else if (kind === "set_permission_mode") {
      this.control.permissionMode = required(asString(command.permission_mode), "permission mode");
      this.control.lastControlId = controlId;
      this.control.revision += 1;
      this.recordSameState("permission_mode_changed", "control", controlId, `control:${controlId}`, {
        permission_mode: this.control.permissionMode,
      }, this.activeTurnId);
    } else throw new Error(`unsupported query control command: ${kind}`);
    this.bump();
    return this.controlToJson();
  }

  pause(controlId: string): void {
    if (this.isTerminal() || this.control.paused) return;
    this.control.paused = true;
    this.control.lastControlId = controlId;
    this.control.revision += 1;
    this.transition("paused", "query_paused", "control", controlId, `control:${controlId}:pause`, {}, this.activeTurnId);
    this.bump();
  }

  resume(controlId: string, resumeCorrelationId: string): void {
    if (this.isTerminal()) throw new Error(`cannot resume terminal query: ${this.status}`);
    const correlation = required(resumeCorrelationId, "resume correlation id");
    if (this.resumeCorrelationIds.has(correlation)) return;
    this.resumeCorrelationIds.add(correlation);
    this.control.paused = false;
    this.control.abortRequested = false;
    this.control.abortReason = null;
    this.control.lastControlId = controlId;
    this.control.revision += 1;
    const next: QueryLifecycleStatus = this.activeTurnId ? "running" : "queued";
    if (this.status === "paused") {
      this.transition(next, "query_resumed", "control", correlation, `resume:${correlation}`, {
        restart_epoch: this.restartEpoch,
      }, this.activeTurnId);
    } else {
      this.recordSameState("query_resume_correlated", "control", correlation, `resume:${correlation}`, {
        restart_epoch: this.restartEpoch,
      }, this.activeTurnId);
    }
    this.bump();
  }

  cancel(controlId: string, reason: string): void {
    if (this.isTerminal()) return;
    this.control.abortRequested = true;
    this.control.abortReason = reason.trim().slice(0, 4_096);
    this.control.lastControlId = controlId;
    this.control.revision += 1;
    if (this.activeTurnId) {
      const turn = this.requireTurn(this.activeTurnId);
      if (turn.status !== "completed" && turn.status !== "failed") {
        turn.status = "cancelled";
        turn.error = this.control.abortReason;
        turn.completedAt = new Date().toISOString();
        turn.revision += 1;
        this.cancelTurnTools(turn, "query_cancelled");
      }
      this.activeTurnId = null;
    }
    this.stopReason = reason === "user_cancelled" ? "user_cancelled" : "aborted";
    this.transition("cancelled", "query_cancelled", "control", controlId, `control:${controlId}:cancel`, {
      reason: this.control.abortReason,
    });
    this.bump();
  }

  shouldStop(): QueryStopDecision {
    if (this.isTerminal()) {
      return {
        stop: true,
        reason: this.stopReason ?? (this.status === "cancelled" ? "aborted" : this.status === "failed" ? "model_error" : "end_turn"),
        compactFirst: false,
        abortTools: true,
        details: { status: this.status },
      };
    }
    if (this.control.abortRequested) {
      return {
        stop: true,
        reason: "aborted",
        compactFirst: false,
        abortTools: true,
        details: { abort_reason: this.control.abortReason },
      };
    }
    if (this.budget.wallTimeMs !== null && Date.now() - Date.parse(this.budget.startedAt) >= this.budget.wallTimeMs) {
      return budgetStop("budget_exhausted", "wall_time_ms", this.budget.wallTimeMs);
    }
    if (this.budget.maximumTurns !== null && this.budget.consumedTurns >= this.budget.maximumTurns) {
      return budgetStop("max_turns", "turns", this.budget.maximumTurns);
    }
    if (this.budget.maximumInputTokens !== null && this.budget.consumedInputTokens >= this.budget.maximumInputTokens) {
      return budgetStop("budget_exhausted", "input_tokens", this.budget.maximumInputTokens, true);
    }
    if (this.budget.maximumOutputTokens !== null && this.budget.consumedOutputTokens >= this.budget.maximumOutputTokens) {
      return budgetStop("budget_exhausted", "output_tokens", this.budget.maximumOutputTokens);
    }
    if (this.budget.maximumToolCalls !== null && this.budget.consumedToolCalls >= this.budget.maximumToolCalls) {
      return budgetStop("budget_exhausted", "tool_calls", this.budget.maximumToolCalls);
    }
    if (this.budget.maximumToolResultChars !== null && this.budget.consumedToolResultChars >= this.budget.maximumToolResultChars) {
      return budgetStop("budget_exhausted", "tool_result_chars", this.budget.maximumToolResultChars, true);
    }
    return {
      stop: false,
      reason: null,
      compactFirst: this.control.pendingCompact,
      abortTools: false,
      details: {
        status: this.status,
        queue_depth: this.queue.length,
        active_turn_id: this.activeTurnId,
      },
    };
  }

  finish(reason: QueryStopReason): void {
    if (this.isTerminal()) return;
    if (this.activeTurnId) throw new Error("cannot finish query with an active turn");
    this.stopReason = reason;
    this.transition("completed", "query_completed", "state", this.identity.queryId, `query:${this.identity.queryId}:complete`, {
      stop_reason: reason,
      budget: budgetToJson(this.budget),
    });
    this.bump();
  }

  fail(reason: QueryStopReason, error: string): void {
    if (this.isTerminal()) return;
    this.stopReason = reason;
    this.terminalError = error.trim().slice(0, 8_192);
    this.transition("failed", "query_failed", "state", this.identity.queryId, `query:${this.identity.queryId}:failed:${this.revision}`, {
      stop_reason: reason,
      error: this.terminalError,
    }, this.activeTurnId);
    this.activeTurnId = null;
    this.bump();
  }

  project(): JsonObject {
    return {
      identity: identityToJson(this.identity),
      status: this.status,
      revision: this.revision,
      sequence: this.sequence,
      restart_epoch: this.restartEpoch,
      queue_depth: this.queue.length,
      queued_input_ids: [...this.queue],
      active_turn_id: this.activeTurnId,
      turn_count: this.turns.size,
      tool_call_count: this.toolCalls.size,
      stop_reason: this.stopReason,
      terminal_error: this.terminalError,
      budget: budgetToJson(this.budget),
      control: this.controlToJson(),
      state_digest: this.stateDigest(),
    };
  }

  snapshot(): QueryLifecycleSnapshot {
    const unsigned: Omit<QueryLifecycleSnapshot, "checksum"> = {
      version: QUERY_LIFECYCLE_SNAPSHOT_VERSION,
      identity: structuredClone(this.identity),
      status: this.status,
      revision: this.revision,
      sequence: this.sequence,
      restartEpoch: this.restartEpoch,
      inputs: structuredClone([...this.inputs.values()]),
      admissions: structuredClone([...this.admissions.values()]),
      queuedInputIds: [...this.queue],
      turns: structuredClone([...this.turns.values()]),
      toolCalls: structuredClone([...this.toolCalls.values()]),
      transitions: structuredClone(this.transitions),
      budget: structuredClone(this.budget),
      control: structuredClone(this.control),
      activeTurnId: this.activeTurnId,
      stopReason: this.stopReason,
      terminalError: this.terminalError,
      committedIdempotencyKeys: [...this.committedIdempotencyKeys].sort(),
      resumeCorrelationIds: [...this.resumeCorrelationIds].sort(),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshot: QueryLifecycleSnapshot): void {
    if (snapshot.version !== QUERY_LIFECYCLE_SNAPSHOT_VERSION) {
      throw new Error("unsupported query lifecycle snapshot version");
    }
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) throw new Error("query lifecycle snapshot checksum mismatch");
    if (snapshot.identity.sessionId !== this.identity.sessionId || snapshot.identity.queryId !== this.identity.queryId) {
      throw new Error("query lifecycle snapshot identity mismatch");
    }
    validateRestoredSnapshot(snapshot);
    this.inputs.clear();
    for (const input of snapshot.inputs) this.inputs.set(input.inputId, structuredClone(input));
    this.admissions.clear();
    for (const admission of snapshot.admissions) {
      const inputId = snapshot.inputs.find((item) => this.admissions.size === snapshot.admissions.indexOf(admission))?.inputId;
      if (inputId) this.admissions.set(inputId, structuredClone(admission));
    }
    this.queue.splice(0, this.queue.length, ...snapshot.queuedInputIds);
    this.turns.clear();
    for (const turn of snapshot.turns) this.turns.set(turn.turnId, structuredClone(turn));
    this.toolCalls.clear();
    for (const call of snapshot.toolCalls) this.toolCalls.set(call.toolCallId, structuredClone(call));
    this.transitions.splice(0, this.transitions.length, ...structuredClone(snapshot.transitions));
    this.budget = structuredClone(snapshot.budget);
    this.control = structuredClone(snapshot.control);
    this.status = snapshot.status;
    this.revision = snapshot.revision;
    this.sequence = snapshot.sequence;
    this.restartEpoch = snapshot.restartEpoch + 1;
    this.activeTurnId = snapshot.activeTurnId;
    this.stopReason = snapshot.stopReason;
    this.terminalError = snapshot.terminalError;
    this.committedIdempotencyKeys.clear();
    for (const key of snapshot.committedIdempotencyKeys) this.committedIdempotencyKeys.add(key);
    this.resumeCorrelationIds.clear();
    for (const id of snapshot.resumeCorrelationIds) this.resumeCorrelationIds.add(id);
    this.recordSameState(
      "query_restored",
      "state",
      this.identity.queryId,
      `restore:${this.restartEpoch}:${snapshot.checksum}`,
      { restart_epoch: this.restartEpoch, parent_checksum: snapshot.checksum },
      this.activeTurnId,
    );
    this.bump();
  }

  private transition(
    next: QueryLifecycleStatus,
    cause: string,
    effect: QueryTransitionEffect,
    correlationId: string,
    idempotencyKey: string,
    payload: JsonObject,
    turnId: string | null = null,
  ): void {
    const allowed = STATUS_TRANSITIONS[this.status];
    if (!allowed.includes(next)) throw new Error(`invalid query transition ${this.status} -> ${next}`);
    if (this.committedIdempotencyKeys.has(idempotencyKey)) return;
    const from = this.status;
    this.status = next;
    this.revision += 1;
    this.sequence += 1;
    this.committedIdempotencyKeys.add(idempotencyKey);
    this.transitions.push({
      transitionId: transitionId(this.identity.queryId, this.restartEpoch, this.revision, this.sequence),
      sequence: this.sequence,
      queryId: this.identity.queryId,
      turnId,
      from,
      to: next,
      cause,
      effect,
      correlationId,
      idempotencyKey,
      payloadDigest: digest(payload),
      stateDigest: this.stateDigest(),
      revision: this.revision,
      createdAt: new Date().toISOString(),
    });
  }

  private recordSameState(
    cause: string,
    effect: QueryTransitionEffect,
    correlationId: string,
    idempotencyKey: string,
    payload: JsonObject,
    turnId: string | null = null,
  ): void {
    if (this.committedIdempotencyKeys.has(idempotencyKey)) return;
    this.revision += 1;
    this.sequence += 1;
    this.committedIdempotencyKeys.add(idempotencyKey);
    this.transitions.push({
      transitionId: transitionId(this.identity.queryId, this.restartEpoch, this.revision, this.sequence),
      sequence: this.sequence,
      queryId: this.identity.queryId,
      turnId,
      from: this.status,
      to: this.status,
      cause,
      effect,
      correlationId,
      idempotencyKey,
      payloadDigest: digest(payload),
      stateDigest: this.stateDigest(),
      revision: this.revision,
      createdAt: new Date().toISOString(),
    });
  }

  private stateDigest(): string {
    return digest({
      identity: this.identity,
      status: this.status,
      revision: this.revision,
      sequence: this.sequence,
      restart_epoch: this.restartEpoch,
      queue: this.queue,
      active_turn_id: this.activeTurnId,
      turn_revisions: [...this.turns.values()].map((item) => [item.turnId, item.revision, item.status]),
      tool_revisions: [...this.toolCalls.values()].map((item) => [item.toolCallId, item.revision, item.status]),
      budget: this.budget,
      control: this.control,
      stop_reason: this.stopReason,
    });
  }

  private controlToJson(): JsonObject {
    return {
      paused: this.control.paused,
      abort_requested: this.control.abortRequested,
      abort_reason: this.control.abortReason,
      pending_compact: this.control.pendingCompact,
      pending_model: this.control.pendingModel,
      permission_mode: this.control.permissionMode,
      last_control_id: this.control.lastControlId,
      revision: this.control.revision,
    };
  }

  private findByIdempotencyKey(key: string): QueryInput | null {
    for (const input of this.inputs.values()) if (input.idempotencyKey === key) return input;
    return null;
  }

  private sortQueue(): void {
    this.queue.sort((leftId, rightId) => {
      const left = this.inputs.get(leftId)!;
      const right = this.inputs.get(rightId)!;
      const priority = priorityScore(right.priority) - priorityScore(left.priority);
      if (priority !== 0) return priority;
      const created = Date.parse(left.createdAt) - Date.parse(right.createdAt);
      if (created !== 0) return created;
      return left.inputId.localeCompare(right.inputId);
    });
  }

  private reindexAdmissions(): void {
    for (const [inputId, admission] of this.admissions) {
      admission.queuePosition = this.queue.indexOf(inputId);
    }
  }

  private requireTurn(turnId: string): QueryTurn {
    const turn = this.turns.get(turnId);
    if (!turn) throw new Error(`query turn not found: ${turnId}`);
    return turn;
  }

  private requireToolCall(toolCallId: string): QueryToolCall {
    const call = this.toolCalls.get(toolCallId);
    if (!call) throw new Error(`query tool call not found: ${toolCallId}`);
    return call;
  }

  private requireRunnable(): void {
    if (this.isTerminal()) throw new Error(`query is terminal: ${this.status}`);
    if (this.control.paused) throw new Error("query is paused");
    if (this.control.abortRequested) throw new Error("query abort was requested");
    if (this.status === "created") throw new Error("query has not been admitted");
  }

  private isTerminal(): boolean {
    return this.status === "completed" || this.status === "failed" || this.status === "cancelled";
  }

  private turnToolsSettled(turn: QueryTurn): boolean {
    return turn.toolCallIds.every((id) => {
      const state = this.requireToolCall(id).status;
      return state === "succeeded" || state === "failed" || state === "cancelled";
    });
  }

  private cancelTurnTools(turn: QueryTurn, reason: string): void {
    for (const id of turn.toolCallIds) {
      const call = this.requireToolCall(id);
      if (call.status === "succeeded" || call.status === "failed" || call.status === "cancelled") continue;
      call.status = "cancelled";
      call.error = reason;
      call.completedAt = new Date().toISOString();
      call.revision += 1;
    }
  }

  private bump(): void {
    this.revision += 1;
  }
}

function normalizeIdentity(value: QueryIdentity): QueryIdentity {
  return {
    queryId: required(value.queryId, "query id"),
    sessionId: required(value.sessionId, "session id"),
    runId: required(value.runId, "run id"),
    taskId: required(value.taskId, "task id"),
    workerRequestId: required(value.workerRequestId, "worker request id"),
    parentQueryId: value.parentQueryId?.trim() || null,
    branchId: required(value.branchId, "branch id"),
  };
}

function normalizeInput(value: QueryInput): QueryInput {
  return {
    inputId: value.inputId?.trim() || randomUUID(),
    kind: inputKind(value.kind),
    content: required(value.content, "query input content"),
    priority: priority(value.priority),
    idempotencyKey: required(value.idempotencyKey, "idempotency key"),
    correlationId: required(value.correlationId, "correlation id"),
    metadata: asObject(value.metadata),
    createdAt: normalizeTimestamp(value.createdAt),
  };
}

function normalizeBudget(
  value: QueryLifecycleOptions["budget"],
): QueryBudget {
  return {
    maximumTurns: nullablePositive(value?.maximumTurns),
    maximumInputTokens: nullablePositive(value?.maximumInputTokens),
    maximumOutputTokens: nullablePositive(value?.maximumOutputTokens),
    maximumToolCalls: nullablePositive(value?.maximumToolCalls),
    maximumToolResultChars: nullablePositive(value?.maximumToolResultChars),
    wallTimeMs: nullablePositive(value?.wallTimeMs),
    consumedTurns: 0,
    consumedInputTokens: 0,
    consumedOutputTokens: 0,
    consumedToolCalls: 0,
    consumedToolResultChars: 0,
    startedAt: new Date().toISOString(),
  };
}

function validateRestoredSnapshot(snapshot: QueryLifecycleSnapshot): void {
  const inputIds = new Set(snapshot.inputs.map((item) => item.inputId));
  for (const id of snapshot.queuedInputIds) if (!inputIds.has(id)) throw new Error(`snapshot queues unknown input: ${id}`);
  const turnIds = new Set(snapshot.turns.map((item) => item.turnId));
  if (snapshot.activeTurnId && !turnIds.has(snapshot.activeTurnId)) throw new Error("snapshot active turn is missing");
  const callIds = new Set(snapshot.toolCalls.map((item) => item.toolCallId));
  for (const turn of snapshot.turns) {
    for (const id of turn.toolCallIds) if (!callIds.has(id)) throw new Error(`snapshot turn references missing tool call: ${id}`);
  }
  let lastSequence = 0;
  const transitionIds = new Set<string>();
  for (const transition of snapshot.transitions) {
    if (transition.sequence <= lastSequence) throw new Error("snapshot transition sequence is not monotonic");
    if (transitionIds.has(transition.transitionId)) throw new Error("snapshot contains duplicate transition id");
    lastSequence = transition.sequence;
    transitionIds.add(transition.transitionId);
  }
}

function queryInputFromJson(value: JsonObject): QueryInput {
  return {
    inputId: asString(value.input_id, randomUUID()),
    kind: inputKind(asString(value.kind, "prompt")),
    content: asString(value.content),
    priority: priority(asString(value.priority, "normal")),
    idempotencyKey: asString(value.idempotency_key, randomUUID()),
    correlationId: asString(value.correlation_id, randomUUID()),
    metadata: asObject(value.metadata),
    createdAt: asString(value.created_at, new Date().toISOString()),
  };
}

function toolPlanFromJson(value: JsonObject): ToolPlanInput {
  return {
    toolCallId: asString(value.tool_call_id) || undefined,
    name: asString(value.name, asString(value.tool_name)),
    arguments: asObject(value.arguments),
    readOnly: asBoolean(value.read_only, false),
  };
}

function toolResultFromJson(value: JsonObject): ToolResultInput {
  return {
    toolCallId: asString(value.tool_call_id),
    ok: asBoolean(value.ok, false),
    summary: asString(value.summary),
    result: value.result ?? null,
    error: asString(value.error) || null,
    resultChars: integer(value.result_chars, canonicalJson(value.result ?? null).length),
    completedAt: asString(value.completed_at) || undefined,
  };
}

function inputToJson(value: QueryInput): JsonObject {
  return {
    input_id: value.inputId,
    kind: value.kind,
    content: value.content,
    priority: value.priority,
    idempotency_key: value.idempotencyKey,
    correlation_id: value.correlationId,
    metadata: value.metadata,
    created_at: value.createdAt,
  };
}

function admissionToJson(value: QueryAdmission): JsonObject {
  return {
    admission_id: value.admissionId,
    accepted: value.accepted,
    reason: value.reason,
    queue_position: value.queuePosition,
    effective_priority: value.effectivePriority,
    duplicate_of: value.duplicateOf,
    admitted_at: value.admittedAt,
  };
}

function turnToJson(value: QueryTurn): JsonObject {
  return {
    turn_id: value.turnId,
    index: value.index,
    status: value.status,
    model: value.model,
    input_ids: value.inputIds,
    message_digest_before: value.messageDigestBefore,
    message_digest_after: value.messageDigestAfter,
    tool_call_ids: value.toolCallIds,
    input_tokens: value.inputTokens,
    output_tokens: value.outputTokens,
    result_chars: value.resultChars,
    sampling_attempt: value.samplingAttempt,
    stop_reason: value.stopReason,
    error: value.error,
    started_at: value.startedAt,
    completed_at: value.completedAt,
    revision: value.revision,
  };
}

function toolCallToJson(value: QueryToolCall): JsonObject {
  return {
    tool_call_id: value.toolCallId,
    turn_id: value.turnId,
    name: value.name,
    arguments: value.arguments,
    arguments_digest: value.argumentsDigest,
    read_only: value.readOnly,
    status: value.status,
    permission_decision_id: value.permissionDecisionId,
    result_digest: value.resultDigest,
    result_summary: value.resultSummary,
    error: value.error,
    started_at: value.startedAt,
    completed_at: value.completedAt,
    revision: value.revision,
  };
}

function budgetToJson(value: QueryBudget): JsonObject {
  return {
    maximum_turns: value.maximumTurns,
    maximum_input_tokens: value.maximumInputTokens,
    maximum_output_tokens: value.maximumOutputTokens,
    maximum_tool_calls: value.maximumToolCalls,
    maximum_tool_result_chars: value.maximumToolResultChars,
    wall_time_ms: value.wallTimeMs,
    consumed_turns: value.consumedTurns,
    consumed_input_tokens: value.consumedInputTokens,
    consumed_output_tokens: value.consumedOutputTokens,
    consumed_tool_calls: value.consumedToolCalls,
    consumed_tool_result_chars: value.consumedToolResultChars,
    started_at: value.startedAt,
  };
}

function identityToJson(value: QueryIdentity): JsonObject {
  return {
    query_id: value.queryId,
    session_id: value.sessionId,
    run_id: value.runId,
    task_id: value.taskId,
    worker_request_id: value.workerRequestId,
    parent_query_id: value.parentQueryId,
    branch_id: value.branchId,
  };
}

function stopDecisionToJson(value: QueryStopDecision): JsonObject {
  return {
    stop: value.stop,
    reason: value.reason,
    compact_first: value.compactFirst,
    abort_tools: value.abortTools,
    details: value.details,
  };
}

function budgetStop(
  reason: QueryStopReason,
  dimension: string,
  limit: number,
  compactFirst = false,
): QueryStopDecision {
  return {
    stop: true,
    reason,
    compactFirst,
    abortTools: true,
    details: { dimension, limit },
  };
}

function inputKind(value: string): QueryInputKind {
  if (value === "side_question" || value === "control" || value === "system_notice") return value;
  return "prompt";
}

function priority(value: string): QueryPriority {
  if (value === "background" || value === "interactive" || value === "critical") return value;
  return "normal";
}

function priorityScore(value: QueryPriority): number {
  if (value === "critical") return 100;
  if (value === "interactive") return 50;
  if (value === "normal") return 10;
  return 0;
}

function nullablePositive(value: number | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  if (!Number.isFinite(value) || value <= 0) return null;
  return Math.floor(value);
}

function integer(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.floor(value)) : fallback;
}

function required(value: string, name: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${name} is required`);
  return normalized;
}

function normalizeTimestamp(value?: string): string {
  if (!value) return new Date().toISOString();
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) throw new Error(`invalid timestamp: ${value}`);
  return new Date(timestamp).toISOString();
}

function transitionId(
  queryId: string,
  restartEpoch: number,
  revision: number,
  sequence: number,
): string {
  return `query:${queryId}:epoch:${restartEpoch}:revision:${revision}:sequence:${sequence}:${randomUUID()}`;
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function digest(value: unknown): string {
  return `sha256:${createHash("sha256").update(canonicalJson(value)).digest("hex")}`;
}
