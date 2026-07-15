import type { JsonObject, JsonValue } from "../contracts.ts";
import { digest } from "./kernel.ts";

export const EXECUTION_CUSTODY_SNAPSHOT_VERSION = "zyra.e01-execution-custody/v1";

export type ProviderCustodyState =
  | "prepared"
  | "executing"
  | "succeeded"
  | "failed"
  | "cancelled";

export type ToolCustodyState =
  | "planned"
  | "leased"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export type TurnCustodyState = "running" | "completed" | "failed";

export interface ProviderCustodyRecord {
  externalRequestId: string;
  providerModelRequestId: string;
  sessionId: string;
  runId: string;
  providerId: string;
  modelId: string;
  credentialId: string | null;
  credentialFingerprint: string;
  promptId: string;
  promptFingerprint: string;
  routeId: string;
  routeDecisionDigest: string;
  reservationId: string;
  durableRequestId: string;
  attemptId: string;
  responseId: string;
  bodyDigest: string;
  headersDigest: string;
  state: ProviderCustodyState;
  executionAttempt: number;
  transportStatus: number | null;
  providerRequestId: string | null;
  responseDigest: string | null;
  errorCode: string | null;
  preparedAt: string;
  executionStartedAt: string | null;
  settledAt: string | null;
  revision: number;
}

export interface ToolCustodyRecord {
  callId: string;
  sessionId: string;
  runId: string;
  taskId: string;
  turnId: string;
  batchId: string;
  toolName: string;
  argumentsDigest: string;
  readOnly: boolean;
  permissionMode: "implicit_read" | "delegated_host";
  permissionDecisionId: string | null;
  permissionGranted: boolean | null;
  invocationRevision: number;
  leaseId: string | null;
  leaseOwner: string | null;
  effectId: string | null;
  resultId: string | null;
  deliveryId: string | null;
  deliveryDigest: string | null;
  resultDigest: string | null;
  state: ToolCustodyState;
  attempt: number;
  errorCode: string | null;
  plannedAt: string;
  startedAt: string | null;
  settledAt: string | null;
  deliveredAt: string | null;
  revision: number;
}

export interface SessionMessageCustodyRecord {
  messageId: string;
  role: "system" | "user" | "assistant" | "tool";
  contentDigest: string;
  turnId: string | null;
  toolCallId: string | null;
  source: "initial_projection" | "turn" | "tool_result" | "turn_status";
  attachedAt: string;
  revision: number;
}

export interface TurnCustodyRecord {
  turnId: string;
  sessionId: string;
  runId: string;
  turnIndex: number;
  promptDigest: string;
  state: TurnCustodyState;
  toolCallIds: string[];
  userMessageId: string | null;
  assistantMessageId: string | null;
  errorCode: string | null;
  startedAt: string;
  completedAt: string | null;
  revision: number;
}

export interface ExecutionCustodyAudit {
  ok: boolean;
  providerCount: number;
  toolCount: number;
  turnCount: number;
  messageCount: number;
  activeProviders: string[];
  activeTools: string[];
  activeTurns: string[];
  orphanToolIds: string[];
  orphanMessageIds: string[];
  invariantFailures: string[];
  stateDigest: string;
}

export interface ExecutionCustodySnapshot {
  version: typeof EXECUTION_CUSTODY_SNAPSHOT_VERSION;
  sessionId: string;
  runId: string;
  revision: number;
  restartEpoch: number;
  providers: ProviderCustodyRecord[];
  tools: ToolCustodyRecord[];
  turns: TurnCustodyRecord[];
  messages: SessionMessageCustodyRecord[];
  checksum: string;
}

export interface BindProviderInput {
  externalRequestId: string;
  providerModelRequestId: string;
  providerId: string;
  modelId: string;
  credentialId: string | null;
  credentialFingerprint: string;
  promptId: string;
  promptFingerprint: string;
  routeId: string;
  routeDecisionDigest: string;
  reservationId: string;
  durableRequestId: string;
  attemptId: string;
  responseId: string;
  bodyDigest: string;
  redactedHeaders: Readonly<Record<string, string>>;
}

export interface SettleProviderInput {
  ok: boolean;
  transportStatus: number;
  providerRequestId: string | null;
  response: JsonValue;
  errorCode: string | null;
}

export interface PlanToolInput {
  callId: string;
  taskId: string;
  turnId: string;
  batchId: string;
  toolName: string;
  arguments: JsonObject;
  readOnly: boolean;
  permissionDecisionId: string | null;
  invocationRevision: number;
}

export interface BindToolRuntimeInput {
  callId: string;
  leaseId: string;
  leaseOwner: string;
  effectId: string;
  resultId: string;
}

export interface SettleToolInput {
  callId: string;
  ok: boolean;
  result: JsonValue;
  errorCode: string | null;
}

export class E01ExecutionCustodyRuntime {
  private readonly providers = new Map<string, ProviderCustodyRecord>();
  private readonly providerModelRequestIndex = new Map<string, string>();
  private readonly tools = new Map<string, ToolCustodyRecord>();
  private readonly turns = new Map<string, TurnCustodyRecord>();
  private readonly messages = new Map<string, SessionMessageCustodyRecord>();
  private revision = 0;
  private restartEpoch = 0;

  constructor(
    readonly sessionId: string,
    readonly runId: string,
  ) {
    requireText(sessionId, "sessionId");
    requireText(runId, "runId");
  }

  bindProvider(input: BindProviderInput): ProviderCustodyRecord {
    validateProviderBinding(input);
    const externalRequestId = input.externalRequestId.trim();
    const existing = this.providers.get(externalRequestId);
    const incomingFingerprint = providerBindingFingerprint(input);
    if (existing) {
      if (providerRecordFingerprint(existing) !== incomingFingerprint) {
        throw new Error(`provider custody binding conflict: ${externalRequestId}`);
      }
      return clone(existing);
    }
    const indexedExternal = this.providerModelRequestIndex.get(
      input.providerModelRequestId,
    );
    if (indexedExternal && indexedExternal !== externalRequestId) {
      throw new Error(
        `provider model request already bound to ${indexedExternal}: ${input.providerModelRequestId}`,
      );
    }
    const record: ProviderCustodyRecord = {
      externalRequestId,
      providerModelRequestId: input.providerModelRequestId.trim(),
      sessionId: this.sessionId,
      runId: this.runId,
      providerId: input.providerId.trim(),
      modelId: input.modelId.trim(),
      credentialId: nullableText(input.credentialId),
      credentialFingerprint: input.credentialFingerprint.trim(),
      promptId: input.promptId.trim(),
      promptFingerprint: input.promptFingerprint.trim(),
      routeId: input.routeId.trim(),
      routeDecisionDigest: input.routeDecisionDigest.trim(),
      reservationId: input.reservationId.trim(),
      durableRequestId: input.durableRequestId.trim(),
      attemptId: input.attemptId.trim(),
      responseId: input.responseId.trim(),
      bodyDigest: input.bodyDigest.trim(),
      headersDigest: digest(sortedStringRecord(input.redactedHeaders)),
      state: "prepared",
      executionAttempt: 0,
      transportStatus: null,
      providerRequestId: null,
      responseDigest: null,
      errorCode: null,
      preparedAt: now(),
      executionStartedAt: null,
      settledAt: null,
      revision: 1,
    };
    assertProviderRecord(record);
    this.providers.set(externalRequestId, record);
    this.providerModelRequestIndex.set(record.providerModelRequestId, externalRequestId);
    this.bump();
    return clone(record);
  }

  beginProviderExecution(externalRequestId: string): ProviderCustodyRecord {
    const record = this.requireProvider(externalRequestId);
    if (record.state === "executing") return clone(record);
    if (isProviderTerminal(record.state)) {
      throw new Error(
        `provider execution cannot begin from ${record.state}: ${record.externalRequestId}`,
      );
    }
    if (record.state !== "prepared") {
      throw new Error(`provider custody state is not executable: ${record.state}`);
    }
    record.state = "executing";
    record.executionAttempt += 1;
    record.executionStartedAt = now();
    record.errorCode = null;
    record.revision += 1;
    this.bump();
    return clone(record);
  }

  settleProvider(
    externalRequestId: string,
    input: SettleProviderInput,
  ): ProviderCustodyRecord {
    const record = this.requireProvider(externalRequestId);
    const nextState: ProviderCustodyState = input.ok ? "succeeded" : "failed";
    const responseDigest = digest(input.response);
    if (isProviderTerminal(record.state)) {
      if (record.state !== nextState) {
        throw new Error(
          `provider custody terminal state conflict: ${record.state} versus ${nextState}`,
        );
      }
      if (input.ok && record.responseDigest !== responseDigest) {
        throw new Error(`provider custody terminal response conflict: ${externalRequestId}`);
      }
      if (!input.ok && record.errorCode !== normalizedError(input.errorCode)) {
        throw new Error(`provider custody terminal error conflict: ${externalRequestId}`);
      }
      return clone(record);
    }
    if (record.state !== "prepared" && record.state !== "executing") {
      throw new Error(`provider custody cannot settle from ${record.state}`);
    }
    if (input.ok && (input.transportStatus < 200 || input.transportStatus >= 300)) {
      throw new Error(`successful provider custody has invalid status ${input.transportStatus}`);
    }
    if (!input.ok && input.errorCode === null) {
      throw new Error("failed provider custody requires an error code");
    }
    record.state = nextState;
    record.transportStatus = integer(input.transportStatus, "transportStatus");
    record.providerRequestId = nullableText(input.providerRequestId);
    record.responseDigest = input.ok ? responseDigest : null;
    record.errorCode = input.ok ? null : normalizedError(input.errorCode);
    record.settledAt = now();
    record.revision += 1;
    assertProviderRecord(record);
    this.bump();
    return clone(record);
  }

  cancelProvider(externalRequestId: string, reason: string): ProviderCustodyRecord {
    const record = this.requireProvider(externalRequestId);
    if (record.state === "cancelled") return clone(record);
    if (record.state === "succeeded" || record.state === "failed") {
      throw new Error(`settled provider request cannot be cancelled: ${externalRequestId}`);
    }
    record.state = "cancelled";
    record.errorCode = requireText(reason, "provider cancellation reason");
    record.settledAt = now();
    record.revision += 1;
    this.bump();
    return clone(record);
  }

  provider(externalRequestId: string): ProviderCustodyRecord {
    return clone(this.requireProvider(externalRequestId));
  }

  providerForModelRequest(providerModelRequestId: string): ProviderCustodyRecord {
    const externalRequestId = this.providerModelRequestIndex.get(
      requireText(providerModelRequestId, "providerModelRequestId"),
    );
    if (!externalRequestId) {
      throw new Error(`unknown provider model request: ${providerModelRequestId}`);
    }
    return this.provider(externalRequestId);
  }

  planTool(input: PlanToolInput): ToolCustodyRecord {
    validateToolPlan(input);
    const callId = input.callId.trim();
    const argumentsDigest = digest(input.arguments);
    const existing = this.tools.get(callId);
    if (existing) {
      if (
        existing.toolName !== input.toolName.trim()
        || existing.turnId !== input.turnId.trim()
        || existing.argumentsDigest !== argumentsDigest
      ) {
        throw new Error(`tool custody plan conflict: ${callId}`);
      }
      return clone(existing);
    }
    const turn = this.requireTurn(input.turnId);
    if (turn.state !== "running") {
      throw new Error(`tool cannot be planned for ${turn.state} turn: ${turn.turnId}`);
    }
    const record: ToolCustodyRecord = {
      callId,
      sessionId: this.sessionId,
      runId: this.runId,
      taskId: input.taskId.trim(),
      turnId: turn.turnId,
      batchId: input.batchId.trim(),
      toolName: input.toolName.trim(),
      argumentsDigest,
      readOnly: input.readOnly,
      permissionMode: input.readOnly ? "implicit_read" : "delegated_host",
      permissionDecisionId: nullableText(input.permissionDecisionId),
      permissionGranted: input.readOnly ? true : null,
      invocationRevision: positiveInteger(
        input.invocationRevision,
        "invocationRevision",
      ),
      leaseId: null,
      leaseOwner: null,
      effectId: null,
      resultId: null,
      deliveryId: null,
      deliveryDigest: null,
      resultDigest: null,
      state: "planned",
      attempt: 0,
      errorCode: null,
      plannedAt: now(),
      startedAt: null,
      settledAt: null,
      deliveredAt: null,
      revision: 1,
    };
    assertToolRecord(record);
    this.tools.set(callId, record);
    turn.toolCallIds.push(callId);
    turn.revision += 1;
    this.bump();
    return clone(record);
  }

  bindToolRuntime(input: BindToolRuntimeInput): ToolCustodyRecord {
    const record = this.requireTool(input.callId);
    if (record.state === "leased" || record.state === "running") {
      if (
        record.leaseId !== input.leaseId
        || record.effectId !== input.effectId
        || record.resultId !== input.resultId
      ) {
        throw new Error(`tool custody runtime binding conflict: ${record.callId}`);
      }
      return clone(record);
    }
    if (record.state !== "planned") {
      throw new Error(`tool runtime cannot bind from ${record.state}: ${record.callId}`);
    }
    record.leaseId = requireText(input.leaseId, "leaseId");
    record.leaseOwner = requireText(input.leaseOwner, "leaseOwner");
    record.effectId = requireText(input.effectId, "effectId");
    record.resultId = requireText(input.resultId, "resultId");
    record.state = "leased";
    record.revision += 1;
    assertToolRecord(record);
    this.bump();
    return clone(record);
  }

  startTool(callId: string): ToolCustodyRecord {
    const record = this.requireTool(callId);
    if (record.state === "running") return clone(record);
    if (record.state !== "leased") {
      throw new Error(`tool custody cannot start from ${record.state}: ${record.callId}`);
    }
    record.state = "running";
    record.attempt += 1;
    record.startedAt = now();
    record.revision += 1;
    assertToolRecord(record);
    this.bump();
    return clone(record);
  }

  resolveDelegatedPermission(
    callId: string,
    granted: boolean,
    decisionId: string,
  ): ToolCustodyRecord {
    const record = this.requireTool(callId);
    if (record.permissionMode !== "delegated_host") {
      if (!granted) throw new Error(`implicit read permission cannot be denied: ${callId}`);
      return clone(record);
    }
    const normalizedDecisionId = requireText(decisionId, "permissionDecisionId");
    if (
      record.permissionGranted !== null
      && (
        record.permissionGranted !== granted
        || record.permissionDecisionId !== normalizedDecisionId
      )
    ) {
      throw new Error(`delegated permission outcome conflict: ${callId}`);
    }
    record.permissionGranted = granted;
    record.permissionDecisionId = normalizedDecisionId;
    record.revision += 1;
    this.bump();
    return clone(record);
  }

  settleTool(input: SettleToolInput): ToolCustodyRecord {
    const record = this.requireTool(input.callId);
    const nextState: ToolCustodyState = input.ok ? "succeeded" : "failed";
    const resultDigest = digest(input.result);
    if (isToolTerminal(record.state)) {
      if (record.state !== nextState || record.resultDigest !== resultDigest) {
        throw new Error(`tool custody terminal result conflict: ${record.callId}`);
      }
      return clone(record);
    }
    if (record.state !== "running") {
      throw new Error(`tool custody cannot settle from ${record.state}: ${record.callId}`);
    }
    if (!input.ok && input.errorCode === null) {
      throw new Error(`failed tool custody requires an error code: ${record.callId}`);
    }
    record.state = nextState;
    record.resultDigest = resultDigest;
    record.errorCode = input.ok ? null : normalizedError(input.errorCode);
    record.settledAt = now();
    record.revision += 1;
    assertToolRecord(record);
    this.bump();
    return clone(record);
  }

  deliverTool(
    callId: string,
    deliveryId: string,
    deliveryDigest: string,
  ): ToolCustodyRecord {
    const record = this.requireTool(callId);
    if (record.state !== "succeeded" && record.state !== "failed") {
      throw new Error(`tool custody cannot deliver from ${record.state}: ${record.callId}`);
    }
    const normalizedId = requireText(deliveryId, "deliveryId");
    const normalizedDigest = requireText(deliveryDigest, "deliveryDigest");
    if (record.deliveryId !== null) {
      if (
        record.deliveryId !== normalizedId
        || record.deliveryDigest !== normalizedDigest
      ) {
        throw new Error(`tool custody delivery conflict: ${record.callId}`);
      }
      return clone(record);
    }
    record.deliveryId = normalizedId;
    record.deliveryDigest = normalizedDigest;
    record.deliveredAt = now();
    record.revision += 1;
    this.bump();
    return clone(record);
  }

  cancelTool(callId: string, reason: string): ToolCustodyRecord {
    const record = this.requireTool(callId);
    if (record.state === "cancelled") return clone(record);
    if (record.state === "succeeded" || record.state === "failed") {
      throw new Error(`settled tool custody cannot be cancelled: ${record.callId}`);
    }
    record.state = "cancelled";
    record.errorCode = requireText(reason, "tool cancellation reason");
    record.settledAt = now();
    record.revision += 1;
    this.bump();
    return clone(record);
  }

  tool(callId: string): ToolCustodyRecord {
    return clone(this.requireTool(callId));
  }

  attachMessage(input: {
    messageId: string;
    role: SessionMessageCustodyRecord["role"];
    content: JsonValue;
    turnId: string | null;
    toolCallId: string | null;
    source: SessionMessageCustodyRecord["source"];
  }): SessionMessageCustodyRecord {
    const messageId = requireText(input.messageId, "messageId");
    const contentDigest = digest(input.content);
    const existing = this.messages.get(messageId);
    if (existing) {
      if (
        existing.role !== input.role
        || existing.contentDigest !== contentDigest
        || existing.turnId !== nullableText(input.turnId)
        || existing.toolCallId !== nullableText(input.toolCallId)
      ) {
        throw new Error(`session message custody conflict: ${messageId}`);
      }
      return clone(existing);
    }
    if (input.toolCallId && !this.tools.has(input.toolCallId)) {
      throw new Error(`tool result message references unknown tool call: ${input.toolCallId}`);
    }
    if (input.turnId && !this.turns.has(input.turnId)) {
      throw new Error(`session message references unknown turn: ${input.turnId}`);
    }
    const record: SessionMessageCustodyRecord = {
      messageId,
      role: messageRole(input.role),
      contentDigest,
      turnId: nullableText(input.turnId),
      toolCallId: nullableText(input.toolCallId),
      source: messageSource(input.source),
      attachedAt: now(),
      revision: 1,
    };
    this.messages.set(messageId, record);
    this.bump();
    return clone(record);
  }

  beginTurn(input: {
    turnId: string;
    turnIndex: number;
    prompt: string;
    userMessageId: string | null;
  }): TurnCustodyRecord {
    const turnId = requireText(input.turnId, "turnId");
    const promptDigest = digest(input.prompt);
    const existing = this.turns.get(turnId);
    if (existing) {
      if (
        existing.turnIndex !== input.turnIndex
        || existing.promptDigest !== promptDigest
      ) {
        throw new Error(`turn custody identity conflict: ${turnId}`);
      }
      return clone(existing);
    }
    const record: TurnCustodyRecord = {
      turnId,
      sessionId: this.sessionId,
      runId: this.runId,
      turnIndex: nonNegativeInteger(input.turnIndex, "turnIndex"),
      promptDigest,
      state: "running",
      toolCallIds: [],
      userMessageId: nullableText(input.userMessageId),
      assistantMessageId: null,
      errorCode: null,
      startedAt: now(),
      completedAt: null,
      revision: 1,
    };
    this.turns.set(turnId, record);
    this.bump();
    return clone(record);
  }

  finishTurn(input: {
    turnId: string;
    ok: boolean;
    errorCode: string | null;
    assistantMessageId: string;
  }): TurnCustodyRecord {
    const record = this.requireTurn(input.turnId);
    const nextState: TurnCustodyState = input.ok ? "completed" : "failed";
    const assistantMessageId = requireText(
      input.assistantMessageId,
      "assistantMessageId",
    );
    if (record.state !== "running") {
      if (
        record.state !== nextState
        || record.assistantMessageId !== assistantMessageId
        || record.errorCode !== (input.ok ? null : normalizedError(input.errorCode))
      ) {
        throw new Error(`turn custody terminal conflict: ${record.turnId}`);
      }
      return clone(record);
    }
    const unsettledTools = record.toolCallIds
      .map((callId) => this.requireTool(callId))
      .filter((tool) => !isToolTerminal(tool.state));
    if (unsettledTools.length > 0) {
      throw new Error(
        `turn custody has unsettled tools: ${unsettledTools.map((tool) => tool.callId).join(",")}`,
      );
    }
    if (!input.ok && input.errorCode === null) {
      throw new Error(`failed turn custody requires an error code: ${record.turnId}`);
    }
    record.state = nextState;
    record.assistantMessageId = assistantMessageId;
    record.errorCode = input.ok ? null : normalizedError(input.errorCode);
    record.completedAt = now();
    record.revision += 1;
    this.bump();
    return clone(record);
  }

  turn(turnId: string): TurnCustodyRecord {
    return clone(this.requireTurn(turnId));
  }

  audit(): ExecutionCustodyAudit {
    const failures: string[] = [];
    const orphanToolIds: string[] = [];
    const orphanMessageIds: string[] = [];
    for (const provider of this.providers.values()) {
      try {
        assertProviderRecord(provider);
      } catch (error) {
        failures.push(errorMessage(error));
      }
      if (
        this.providerModelRequestIndex.get(provider.providerModelRequestId)
        !== provider.externalRequestId
      ) {
        failures.push(`provider model index mismatch: ${provider.externalRequestId}`);
      }
    }
    for (const tool of this.tools.values()) {
      try {
        assertToolRecord(tool);
      } catch (error) {
        failures.push(errorMessage(error));
      }
      const turn = this.turns.get(tool.turnId);
      if (!turn || !turn.toolCallIds.includes(tool.callId)) {
        orphanToolIds.push(tool.callId);
      }
    }
    for (const message of this.messages.values()) {
      if (message.turnId && !this.turns.has(message.turnId)) {
        orphanMessageIds.push(message.messageId);
      }
      if (message.toolCallId && !this.tools.has(message.toolCallId)) {
        orphanMessageIds.push(message.messageId);
      }
    }
    const activeProviders = [...this.providers.values()]
      .filter((record) => !isProviderTerminal(record.state))
      .map((record) => record.externalRequestId)
      .sort();
    const activeTools = [...this.tools.values()]
      .filter((record) => !isToolTerminal(record.state))
      .map((record) => record.callId)
      .sort();
    const activeTurns = [...this.turns.values()]
      .filter((record) => record.state === "running")
      .map((record) => record.turnId)
      .sort();
    const invariantFailures = uniqueSorted(failures);
    const projection = this.project();
    return {
      ok: invariantFailures.length === 0
        && orphanToolIds.length === 0
        && orphanMessageIds.length === 0,
      providerCount: this.providers.size,
      toolCount: this.tools.size,
      turnCount: this.turns.size,
      messageCount: this.messages.size,
      activeProviders,
      activeTools,
      activeTurns,
      orphanToolIds: uniqueSorted(orphanToolIds),
      orphanMessageIds: uniqueSorted(orphanMessageIds),
      invariantFailures,
      stateDigest: digest(projection),
    };
  }

  project(): JsonObject {
    return {
      session_id: this.sessionId,
      run_id: this.runId,
      revision: this.revision,
      restart_epoch: this.restartEpoch,
      providers: sortedProviders(this.providers.values()) as unknown as JsonValue,
      tools: sortedTools(this.tools.values()) as unknown as JsonValue,
      turns: sortedTurns(this.turns.values()) as unknown as JsonValue,
      messages: sortedMessages(this.messages.values()) as unknown as JsonValue,
    };
  }

  snapshot(): ExecutionCustodySnapshot {
    const unsigned: Omit<ExecutionCustodySnapshot, "checksum"> = {
      version: EXECUTION_CUSTODY_SNAPSHOT_VERSION,
      sessionId: this.sessionId,
      runId: this.runId,
      revision: this.revision,
      restartEpoch: this.restartEpoch,
      providers: sortedProviders(this.providers.values()),
      tools: sortedTools(this.tools.values()),
      turns: sortedTurns(this.turns.values()),
      messages: sortedMessages(this.messages.values()),
    };
    assertSecretFree(unsigned as unknown as JsonValue);
    return {
      ...unsigned,
      checksum: digest(unsigned),
    };
  }

  restore(snapshot: ExecutionCustodySnapshot, allowRunRebind = false): void {
    if (snapshot.version !== EXECUTION_CUSTODY_SNAPSHOT_VERSION) {
      throw new Error(`unsupported execution custody snapshot: ${snapshot.version}`);
    }
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) {
      throw new Error("execution custody snapshot checksum mismatch");
    }
    if (snapshot.sessionId !== this.sessionId) {
      throw new Error("execution custody snapshot session mismatch");
    }
    if (!allowRunRebind && snapshot.runId !== this.runId) {
      throw new Error("execution custody snapshot run mismatch");
    }
    assertSecretFree(unsigned as unknown as JsonValue);
    const providers = new Map<string, ProviderCustodyRecord>();
    const providerModelRequestIndex = new Map<string, string>();
    for (const raw of snapshot.providers) {
      const record = clone(raw);
      assertProviderRecord(record);
      if (providers.has(record.externalRequestId)) {
        throw new Error(`duplicate provider custody record: ${record.externalRequestId}`);
      }
      if (providerModelRequestIndex.has(record.providerModelRequestId)) {
        throw new Error(
          `duplicate provider model custody record: ${record.providerModelRequestId}`,
        );
      }
      record.runId = this.runId;
      providers.set(record.externalRequestId, record);
      providerModelRequestIndex.set(
        record.providerModelRequestId,
        record.externalRequestId,
      );
    }
    const tools = new Map<string, ToolCustodyRecord>();
    for (const raw of snapshot.tools) {
      const record = clone(raw);
      assertToolRecord(record);
      if (tools.has(record.callId)) {
        throw new Error(`duplicate tool custody record: ${record.callId}`);
      }
      record.runId = this.runId;
      tools.set(record.callId, record);
    }
    const turns = new Map<string, TurnCustodyRecord>();
    for (const raw of snapshot.turns) {
      const record = clone(raw);
      assertTurnRecord(record);
      if (turns.has(record.turnId)) {
        throw new Error(`duplicate turn custody record: ${record.turnId}`);
      }
      record.runId = this.runId;
      turns.set(record.turnId, record);
    }
    const messages = new Map<string, SessionMessageCustodyRecord>();
    for (const raw of snapshot.messages) {
      const record = clone(raw);
      assertMessageRecord(record);
      if (messages.has(record.messageId)) {
        throw new Error(`duplicate message custody record: ${record.messageId}`);
      }
      messages.set(record.messageId, record);
    }
    this.providers.clear();
    this.providerModelRequestIndex.clear();
    this.tools.clear();
    this.turns.clear();
    this.messages.clear();
    for (const [key, value] of providers) this.providers.set(key, value);
    for (const [key, value] of providerModelRequestIndex) {
      this.providerModelRequestIndex.set(key, value);
    }
    for (const [key, value] of tools) this.tools.set(key, value);
    for (const [key, value] of turns) this.turns.set(key, value);
    for (const [key, value] of messages) this.messages.set(key, value);
    this.revision = nonNegativeInteger(snapshot.revision, "revision");
    this.restartEpoch = nonNegativeInteger(snapshot.restartEpoch, "restartEpoch") + 1;
    const audit = this.audit();
    if (!audit.ok) {
      throw new Error(
        `execution custody snapshot violates invariants: ${[
          ...audit.invariantFailures,
          ...audit.orphanToolIds,
          ...audit.orphanMessageIds,
        ].join(";")}`,
      );
    }
    this.bump();
  }

  private requireProvider(externalRequestId: string): ProviderCustodyRecord {
    const id = requireText(externalRequestId, "externalRequestId");
    const record = this.providers.get(id);
    if (!record) throw new Error(`unknown provider custody request: ${id}`);
    return record;
  }

  private requireTool(callId: string): ToolCustodyRecord {
    const id = requireText(callId, "callId");
    const record = this.tools.get(id);
    if (!record) throw new Error(`unknown tool custody call: ${id}`);
    return record;
  }

  private requireTurn(turnId: string): TurnCustodyRecord {
    const id = requireText(turnId, "turnId");
    const record = this.turns.get(id);
    if (!record) throw new Error(`unknown turn custody record: ${id}`);
    return record;
  }

  private bump(): void {
    this.revision += 1;
  }
}

function validateProviderBinding(input: BindProviderInput): void {
  requireText(input.externalRequestId, "externalRequestId");
  requireText(input.providerModelRequestId, "providerModelRequestId");
  requireText(input.providerId, "providerId");
  requireText(input.modelId, "modelId");
  requireText(input.credentialFingerprint, "credentialFingerprint");
  requireText(input.promptId, "promptId");
  requireText(input.promptFingerprint, "promptFingerprint");
  requireText(input.routeId, "routeId");
  requireText(input.routeDecisionDigest, "routeDecisionDigest");
  requireText(input.reservationId, "reservationId");
  requireText(input.durableRequestId, "durableRequestId");
  requireText(input.attemptId, "attemptId");
  requireText(input.responseId, "responseId");
  requireText(input.bodyDigest, "bodyDigest");
  assertRedactedHeaders(input.redactedHeaders);
}

function validateToolPlan(input: PlanToolInput): void {
  requireText(input.callId, "callId");
  requireText(input.taskId, "taskId");
  requireText(input.turnId, "turnId");
  requireText(input.batchId, "batchId");
  requireText(input.toolName, "toolName");
  positiveInteger(input.invocationRevision, "invocationRevision");
  if (!input.readOnly && !input.permissionDecisionId) {
    throw new Error("mutating tool custody requires delegated permission correlation");
  }
}

function providerBindingFingerprint(input: BindProviderInput): string {
  return digest({
    externalRequestId: input.externalRequestId.trim(),
    providerModelRequestId: input.providerModelRequestId.trim(),
    providerId: input.providerId.trim(),
    modelId: input.modelId.trim(),
    credentialId: nullableText(input.credentialId),
    credentialFingerprint: input.credentialFingerprint.trim(),
    promptId: input.promptId.trim(),
    promptFingerprint: input.promptFingerprint.trim(),
    routeId: input.routeId.trim(),
    routeDecisionDigest: input.routeDecisionDigest.trim(),
    reservationId: input.reservationId.trim(),
    durableRequestId: input.durableRequestId.trim(),
    attemptId: input.attemptId.trim(),
    responseId: input.responseId.trim(),
    bodyDigest: input.bodyDigest.trim(),
    headersDigest: digest(sortedStringRecord(input.redactedHeaders)),
  });
}

function providerRecordFingerprint(record: ProviderCustodyRecord): string {
  return digest({
    externalRequestId: record.externalRequestId,
    providerModelRequestId: record.providerModelRequestId,
    providerId: record.providerId,
    modelId: record.modelId,
    credentialId: record.credentialId,
    credentialFingerprint: record.credentialFingerprint,
    promptId: record.promptId,
    promptFingerprint: record.promptFingerprint,
    routeId: record.routeId,
    routeDecisionDigest: record.routeDecisionDigest,
    reservationId: record.reservationId,
    durableRequestId: record.durableRequestId,
    attemptId: record.attemptId,
    responseId: record.responseId,
    bodyDigest: record.bodyDigest,
    headersDigest: record.headersDigest,
  });
}

function assertProviderRecord(record: ProviderCustodyRecord): void {
  requireText(record.externalRequestId, "provider.externalRequestId");
  requireText(record.providerModelRequestId, "provider.providerModelRequestId");
  requireText(record.sessionId, "provider.sessionId");
  requireText(record.runId, "provider.runId");
  requireText(record.providerId, "provider.providerId");
  requireText(record.modelId, "provider.modelId");
  requireText(record.credentialFingerprint, "provider.credentialFingerprint");
  requireText(record.bodyDigest, "provider.bodyDigest");
  positiveInteger(record.revision, "provider.revision");
  nonNegativeInteger(record.executionAttempt, "provider.executionAttempt");
  if (record.state === "executing" && !record.executionStartedAt) {
    throw new Error(`executing provider custody lacks start time: ${record.externalRequestId}`);
  }
  if (isProviderTerminal(record.state) && !record.settledAt) {
    throw new Error(`terminal provider custody lacks settlement time: ${record.externalRequestId}`);
  }
  if (record.state === "succeeded" && !record.responseDigest) {
    throw new Error(`successful provider custody lacks response digest: ${record.externalRequestId}`);
  }
  if ((record.state === "failed" || record.state === "cancelled") && !record.errorCode) {
    throw new Error(`failed provider custody lacks error code: ${record.externalRequestId}`);
  }
}

function assertToolRecord(record: ToolCustodyRecord): void {
  requireText(record.callId, "tool.callId");
  requireText(record.sessionId, "tool.sessionId");
  requireText(record.runId, "tool.runId");
  requireText(record.taskId, "tool.taskId");
  requireText(record.turnId, "tool.turnId");
  requireText(record.batchId, "tool.batchId");
  requireText(record.toolName, "tool.toolName");
  requireText(record.argumentsDigest, "tool.argumentsDigest");
  positiveInteger(record.invocationRevision, "tool.invocationRevision");
  positiveInteger(record.revision, "tool.revision");
  nonNegativeInteger(record.attempt, "tool.attempt");
  if (record.permissionMode === "implicit_read" && record.permissionGranted !== true) {
    throw new Error(`implicit read tool custody lacks grant: ${record.callId}`);
  }
  if (record.permissionMode === "delegated_host" && !record.permissionDecisionId) {
    throw new Error(`delegated tool custody lacks decision correlation: ${record.callId}`);
  }
  if (
    record.state !== "planned"
    && (!record.leaseId || !record.leaseOwner || !record.effectId || !record.resultId)
  ) {
    throw new Error(`bound tool custody lacks runtime references: ${record.callId}`);
  }
  if (
    (record.state === "running" || isToolTerminal(record.state))
    && !record.startedAt
  ) {
    throw new Error(`started tool custody lacks start time: ${record.callId}`);
  }
  if (isToolTerminal(record.state) && !record.settledAt) {
    throw new Error(`terminal tool custody lacks settlement time: ${record.callId}`);
  }
  if (
    (record.state === "succeeded" || record.state === "failed")
    && !record.resultDigest
  ) {
    throw new Error(`settled tool custody lacks result digest: ${record.callId}`);
  }
  if ((record.state === "failed" || record.state === "cancelled") && !record.errorCode) {
    throw new Error(`failed tool custody lacks error code: ${record.callId}`);
  }
}

function assertTurnRecord(record: TurnCustodyRecord): void {
  requireText(record.turnId, "turn.turnId");
  requireText(record.sessionId, "turn.sessionId");
  requireText(record.runId, "turn.runId");
  requireText(record.promptDigest, "turn.promptDigest");
  nonNegativeInteger(record.turnIndex, "turn.turnIndex");
  positiveInteger(record.revision, "turn.revision");
  if (record.state !== "running" && !record.completedAt) {
    throw new Error(`terminal turn custody lacks completion time: ${record.turnId}`);
  }
  if (record.state === "failed" && !record.errorCode) {
    throw new Error(`failed turn custody lacks error code: ${record.turnId}`);
  }
}

function assertMessageRecord(record: SessionMessageCustodyRecord): void {
  requireText(record.messageId, "message.messageId");
  requireText(record.contentDigest, "message.contentDigest");
  positiveInteger(record.revision, "message.revision");
  messageRole(record.role);
  messageSource(record.source);
}

function assertRedactedHeaders(headers: Readonly<Record<string, string>>): void {
  for (const [name, value] of Object.entries(headers)) {
    requireText(name, "header name");
    if (/authorization|api[-_]?key|token|secret|cookie/iu.test(name)) {
      if (!/^\[redacted:[0-9a-f]{8}\]$/u.test(value)) {
        throw new Error(`provider custody received an unredacted sensitive header: ${name}`);
      }
    }
  }
}

function assertSecretFree(value: JsonValue, path = "$" ): void {
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertSecretFree(item, `${path}[${index}]`));
    return;
  }
  if (!value || typeof value !== "object") return;
  for (const [key, item] of Object.entries(value)) {
    if (/api[-_]?key|authorization|access[-_]?token|refresh[-_]?token|secret/iu.test(key)) {
      if (typeof item === "string" && item && !/fingerprint|digest/iu.test(key)) {
        throw new Error(`execution custody snapshot contains secret-like field: ${path}.${key}`);
      }
    }
    assertSecretFree(item, `${path}.${key}`);
  }
}

function sortedProviders(
  values: Iterable<ProviderCustodyRecord>,
): ProviderCustodyRecord[] {
  return [...values]
    .map(clone)
    .sort((left, right) => left.externalRequestId.localeCompare(right.externalRequestId));
}

function sortedTools(values: Iterable<ToolCustodyRecord>): ToolCustodyRecord[] {
  return [...values]
    .map(clone)
    .sort((left, right) => left.callId.localeCompare(right.callId));
}

function sortedTurns(values: Iterable<TurnCustodyRecord>): TurnCustodyRecord[] {
  return [...values]
    .map(clone)
    .sort((left, right) => left.turnIndex - right.turnIndex || left.turnId.localeCompare(right.turnId));
}

function sortedMessages(
  values: Iterable<SessionMessageCustodyRecord>,
): SessionMessageCustodyRecord[] {
  return [...values]
    .map(clone)
    .sort((left, right) => left.attachedAt.localeCompare(right.attachedAt) || left.messageId.localeCompare(right.messageId));
}

function sortedStringRecord(
  value: Readonly<Record<string, string>>,
): Record<string, string> {
  return Object.fromEntries(
    Object.entries(value)
      .map(([key, item]) => [key.toLowerCase(), item] as const)
      .sort(([left], [right]) => left.localeCompare(right)),
  );
}

function isProviderTerminal(state: ProviderCustodyState): boolean {
  return state === "succeeded" || state === "failed" || state === "cancelled";
}

function isToolTerminal(state: ToolCustodyState): boolean {
  return state === "succeeded" || state === "failed" || state === "cancelled";
}

function messageRole(
  value: SessionMessageCustodyRecord["role"],
): SessionMessageCustodyRecord["role"] {
  if (value === "system" || value === "user" || value === "assistant" || value === "tool") {
    return value;
  }
  throw new Error(`invalid custody message role: ${String(value)}`);
}

function messageSource(
  value: SessionMessageCustodyRecord["source"],
): SessionMessageCustodyRecord["source"] {
  if (
    value === "initial_projection"
    || value === "turn"
    || value === "tool_result"
    || value === "turn_status"
  ) {
    return value;
  }
  throw new Error(`invalid custody message source: ${String(value)}`);
}

function normalizedError(value: string | null): string {
  return requireText(value ?? "runtime_error", "error code").slice(0, 512);
}

function nullableText(value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  const normalized = value.trim();
  return normalized || null;
}

function requireText(value: string, name: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${name} must not be empty`);
  return normalized;
}

function integer(value: number, name: string): number {
  if (!Number.isSafeInteger(value)) throw new Error(`${name} must be a safe integer`);
  return value;
}

function nonNegativeInteger(value: number, name: string): number {
  const normalized = integer(value, name);
  if (normalized < 0) throw new Error(`${name} must not be negative`);
  return normalized;
}

function positiveInteger(value: number, name: string): number {
  const normalized = integer(value, name);
  if (normalized <= 0) throw new Error(`${name} must be positive`);
  return normalized;
}

function uniqueSorted(values: readonly string[]): string[] {
  return [...new Set(values)].sort((left, right) => left.localeCompare(right));
}

function now(): string {
  return new Date().toISOString();
}

function clone<T>(value: T): T {
  return structuredClone(value);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
