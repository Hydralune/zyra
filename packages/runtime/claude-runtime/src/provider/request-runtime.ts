import {
  type Clock,
  type IdFactory,
  type JsonRecord,
  type JsonValue,
  RandomIdFactory,
  RuntimeInvariantError,
  SystemClock,
  assertFiniteNumber,
  asJsonValue,
  assertNonEmpty,
  assertNonNegativeInteger,
  compareNumbers,
  compareStrings,
  deepClone,
  digestJson,
} from "../core/runtime-primitives.js";

export type ProviderRequestStatus =
  | "created"
  | "prepared"
  | "dispatched"
  | "streaming"
  | "retry_wait"
  | "completed"
  | "failed"
  | "cancelled";

export type ProviderAttemptStatus =
  | "prepared"
  | "in_flight"
  | "streaming"
  | "succeeded"
  | "failed"
  | "abandoned";

export interface ProviderRequestBudget {
  maximumAttempts: number;
  maximumInputTokens: number;
  maximumOutputTokens: number;
  maximumCost: number | null;
  deadlineAt: number;
}

export interface ProviderRequestRecord {
  requestId: string;
  sessionId: string;
  runId: string;
  queryId: string;
  turnId: string;
  idempotencyKey: string;
  status: ProviderRequestStatus;
  modelPreference: string;
  payload: JsonRecord;
  payloadDigest: string;
  contextDigest: string;
  toolSetDigest: string;
  budget: ProviderRequestBudget;
  attemptIds: string[];
  activeAttemptId: string | null;
  createdAt: number;
  updatedAt: number;
  retryAt: number | null;
  output: JsonValue | null;
  outputDigest: string | null;
  usage: ProviderUsage | null;
  stopReason: string | null;
  errorCode: string | null;
  cancellationReason: string | null;
  restartEpoch: number;
  revision: number;
  metadata: JsonRecord;
}

export interface ProviderAttemptRecord {
  attemptId: string;
  requestId: string;
  ordinal: number;
  routeId: string;
  providerId: string;
  modelId: string;
  endpointId: string;
  credentialId: string;
  status: ProviderAttemptStatus;
  requestBodyDigest: string;
  requestHeadersDigest: string;
  startedAt: number | null;
  firstByteAt: number | null;
  finishedAt: number | null;
  expectedChunkSequence: number;
  chunkIds: string[];
  responseStatus: number | null;
  providerRequestId: string | null;
  errorClass: string | null;
  errorMessage: string | null;
  retryable: boolean | null;
  revision: number;
}

export interface ProviderResponseChunk {
  chunkId: string;
  attemptId: string;
  sequence: number;
  channel: "text" | "reasoning" | "tool" | "usage" | "control";
  payload: JsonValue;
  payloadDigest: string;
  receivedAt: number;
  final: boolean;
}

export interface ProviderUsage {
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  cost: number | null;
}

export interface ProviderRequestTransition {
  transitionId: string;
  requestId: string;
  sequence: number;
  previousStatus: ProviderRequestStatus | null;
  nextStatus: ProviderRequestStatus;
  kind: string;
  occurredAt: number;
  correlationId: string;
  payload: JsonRecord;
  stateDigest: string;
}

export interface ProviderRequestSnapshot {
  version: "zyra.provider-requests/v1";
  restartEpoch: number;
  requests: ProviderRequestRecord[];
  attempts: ProviderAttemptRecord[];
  chunks: ProviderResponseChunk[];
  transitions: ProviderRequestTransition[];
  checksum: string;
}

export interface ProviderRequestRuntimeOptions {
  clock?: Clock;
  ids?: IdFactory;
  maximumTransitionsPerRequest?: number;
  maximumChunksPerAttempt?: number;
}

export class ProviderRequestRuntime {
  private readonly clock: Clock;
  private readonly ids: IdFactory;
  private readonly maximumTransitionsPerRequest: number;
  private readonly maximumChunksPerAttempt: number;
  private readonly requests = new Map<string, ProviderRequestRecord>();
  private readonly attempts = new Map<string, ProviderAttemptRecord>();
  private readonly chunks = new Map<string, ProviderResponseChunk>();
  private readonly transitions = new Map<string, ProviderRequestTransition[]>();
  private readonly idempotencyIndex = new Map<string, string>();
  private restartEpoch = 0;

  constructor(options: ProviderRequestRuntimeOptions = {}) {
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.maximumTransitionsPerRequest =
      options.maximumTransitionsPerRequest ?? 1_000;
    this.maximumChunksPerAttempt = options.maximumChunksPerAttempt ?? 100_000;
    assertNonNegativeInteger(
      this.maximumTransitionsPerRequest,
      "maximumTransitionsPerRequest",
    );
    assertNonNegativeInteger(
      this.maximumChunksPerAttempt,
      "maximumChunksPerAttempt",
    );
  }

  create(input: {
    requestId?: string;
    sessionId: string;
    runId: string;
    queryId: string;
    turnId: string;
    idempotencyKey: string;
    modelPreference: string;
    payload: JsonRecord;
    contextDigest: string;
    toolSetDigest: string;
    budget: ProviderRequestBudget;
    correlationId: string;
    metadata?: JsonRecord;
  }): ProviderRequestRecord {
    validateCreate(input);
    const indexKey = `${input.sessionId}:${input.idempotencyKey}`;
    const existingId = this.idempotencyIndex.get(indexKey);
    const payloadDigest = digestJson(input.payload);
    if (existingId !== undefined) {
      const existing = this.requireRequest(existingId);
      if (
        existing.payloadDigest !== payloadDigest ||
        existing.modelPreference !== input.modelPreference
      ) {
        throw new RuntimeInvariantError("provider_request_idempotency_conflict", {
          idempotencyKey: input.idempotencyKey,
          requestId: existingId,
        });
      }
      return deepClone(existing);
    }
    const requestId = input.requestId ?? this.ids.next("provider-request");
    if (this.requests.has(requestId)) {
      throw new RuntimeInvariantError("provider_request_already_exists", {
        requestId,
      });
    }
    const now = this.clock.now();
    const record: ProviderRequestRecord = {
      requestId,
      sessionId: input.sessionId,
      runId: input.runId,
      queryId: input.queryId,
      turnId: input.turnId,
      idempotencyKey: input.idempotencyKey,
      status: "created",
      modelPreference: input.modelPreference,
      payload: deepClone(input.payload),
      payloadDigest,
      contextDigest: input.contextDigest,
      toolSetDigest: input.toolSetDigest,
      budget: deepClone(input.budget),
      attemptIds: [],
      activeAttemptId: null,
      createdAt: now,
      updatedAt: now,
      retryAt: null,
      output: null,
      outputDigest: null,
      usage: null,
      stopReason: null,
      errorCode: null,
      cancellationReason: null,
      restartEpoch: this.restartEpoch,
      revision: 0,
      metadata: deepClone(input.metadata ?? {}),
    };
    this.requests.set(requestId, record);
    this.idempotencyIndex.set(indexKey, requestId);
    this.transitions.set(requestId, []);
    this.transition(record, "request.created", "created", input.correlationId, {
      payloadDigest,
      modelPreference: input.modelPreference,
    }, null);
    return deepClone(record);
  }

  prepareAttempt(input: {
    requestId: string;
    routeId: string;
    providerId: string;
    modelId: string;
    endpointId: string;
    credentialId: string;
    requestBody: JsonValue;
    requestHeaders: Record<string, string>;
    correlationId: string;
  }): ProviderAttemptRecord {
    const request = this.requireRequest(input.requestId);
    this.assertStatus(request, ["created", "retry_wait"]);
    this.assertNotExpired(request);
    if (request.retryAt !== null && request.retryAt > this.clock.now()) {
      throw new RuntimeInvariantError("provider_retry_not_due", {
        requestId: request.requestId,
        retryAt: request.retryAt,
      });
    }
    if (request.attemptIds.length >= request.budget.maximumAttempts) {
      throw new RuntimeInvariantError("provider_attempt_budget_exceeded", {
        requestId: request.requestId,
        maximumAttempts: request.budget.maximumAttempts,
      });
    }
    const attemptId = this.ids.next("provider-attempt");
    const attempt: ProviderAttemptRecord = {
      attemptId,
      requestId: request.requestId,
      ordinal: request.attemptIds.length + 1,
      routeId: input.routeId,
      providerId: input.providerId,
      modelId: input.modelId,
      endpointId: input.endpointId,
      credentialId: input.credentialId,
      status: "prepared",
      requestBodyDigest: digestJson(input.requestBody),
      requestHeadersDigest: digestHeaders(input.requestHeaders),
      startedAt: null,
      firstByteAt: null,
      finishedAt: null,
      expectedChunkSequence: 1,
      chunkIds: [],
      responseStatus: null,
      providerRequestId: null,
      errorClass: null,
      errorMessage: null,
      retryable: null,
      revision: 1,
    };
    this.attempts.set(attemptId, attempt);
    request.attemptIds.push(attemptId);
    request.activeAttemptId = attemptId;
    request.retryAt = null;
    this.transition(request, "attempt.prepared", "prepared", input.correlationId, {
      attemptId,
      ordinal: attempt.ordinal,
      routeId: input.routeId,
      providerId: input.providerId,
      modelId: input.modelId,
      requestBodyDigest: attempt.requestBodyDigest,
      requestHeadersDigest: attempt.requestHeadersDigest,
    }, null);
    return deepClone(attempt);
  }

  dispatch(input: {
    requestId: string;
    attemptId: string;
    correlationId: string;
  }): ProviderAttemptRecord {
    const request = this.requireRequest(input.requestId);
    this.assertStatus(request, ["prepared"]);
    const attempt = this.requireActiveAttempt(request, input.attemptId);
    if (attempt.status !== "prepared") {
      throw new RuntimeInvariantError("provider_attempt_not_prepared", {
        attemptId: attempt.attemptId,
        status: attempt.status,
      });
    }
    attempt.status = "in_flight";
    attempt.startedAt = this.clock.now();
    attempt.revision += 1;
    this.transition(request, "attempt.dispatched", "dispatched", input.correlationId, {
      attemptId: attempt.attemptId,
      routeId: attempt.routeId,
      endpointId: attempt.endpointId,
    }, null);
    return deepClone(attempt);
  }

  responseStarted(input: {
    requestId: string;
    attemptId: string;
    responseStatus: number;
    providerRequestId?: string | null;
    correlationId: string;
  }): ProviderAttemptRecord {
    const request = this.requireRequest(input.requestId);
    this.assertStatus(request, ["dispatched", "streaming"]);
    const attempt = this.requireActiveAttempt(request, input.attemptId);
    assertNonNegativeInteger(input.responseStatus, "responseStatus");
    if (attempt.firstByteAt === null) {
      attempt.firstByteAt = this.clock.now();
    }
    attempt.responseStatus = input.responseStatus;
    attempt.providerRequestId = input.providerRequestId ?? null;
    attempt.status = "streaming";
    attempt.revision += 1;
    if (request.status !== "streaming") {
      this.transition(request, "response.started", "streaming", input.correlationId, {
        attemptId: attempt.attemptId,
        responseStatus: input.responseStatus,
        providerRequestId: attempt.providerRequestId,
      }, null);
    }
    return deepClone(attempt);
  }

  appendChunk(input: {
    requestId: string;
    attemptId: string;
    chunkId?: string;
    sequence: number;
    channel: ProviderResponseChunk["channel"];
    payload: JsonValue;
    final?: boolean;
  }): ProviderResponseChunk {
    const request = this.requireRequest(input.requestId);
    this.assertStatus(request, ["streaming"]);
    const attempt = this.requireActiveAttempt(request, input.attemptId);
    if (attempt.status !== "streaming") {
      throw new RuntimeInvariantError("provider_attempt_not_streaming", {
        attemptId: attempt.attemptId,
      });
    }
    const chunkId = input.chunkId ?? this.ids.next("provider-chunk");
    const payloadDigest = digestJson(input.payload);
    const existing = this.chunks.get(chunkId);
    if (existing !== undefined) {
      if (
        existing.attemptId !== input.attemptId ||
        existing.sequence !== input.sequence ||
        existing.payloadDigest !== payloadDigest
      ) {
        throw new RuntimeInvariantError("provider_chunk_conflict", { chunkId });
      }
      return deepClone(existing);
    }
    if (input.sequence !== attempt.expectedChunkSequence) {
      throw new RuntimeInvariantError("provider_chunk_sequence_gap", {
        attemptId: attempt.attemptId,
        expected: attempt.expectedChunkSequence,
        actual: input.sequence,
      });
    }
    if (attempt.chunkIds.length >= this.maximumChunksPerAttempt) {
      throw new RuntimeInvariantError("provider_chunk_limit_exceeded", {
        attemptId: attempt.attemptId,
        maximum: this.maximumChunksPerAttempt,
      });
    }
    const chunk: ProviderResponseChunk = {
      chunkId,
      attemptId: attempt.attemptId,
      sequence: input.sequence,
      channel: input.channel,
      payload: deepClone(input.payload),
      payloadDigest,
      receivedAt: this.clock.now(),
      final: input.final ?? false,
    };
    this.chunks.set(chunkId, chunk);
    attempt.chunkIds.push(chunkId);
    attempt.expectedChunkSequence += 1;
    attempt.revision += 1;
    return deepClone(chunk);
  }

  complete(input: {
    requestId: string;
    attemptId: string;
    output: JsonValue;
    usage: ProviderUsage;
    stopReason: string;
    correlationId: string;
  }): ProviderRequestRecord {
    const request = this.requireRequest(input.requestId);
    this.assertStatus(request, ["dispatched", "streaming"]);
    const attempt = this.requireActiveAttempt(request, input.attemptId);
    validateUsage(input.usage, request.budget);
    assertNonEmpty(input.stopReason, "stopReason");
    const outputDigest = digestJson(input.output);
    attempt.status = "succeeded";
    attempt.finishedAt = this.clock.now();
    attempt.retryable = false;
    attempt.revision += 1;
    request.output = deepClone(input.output);
    request.outputDigest = outputDigest;
    request.usage = deepClone(input.usage);
    request.stopReason = input.stopReason;
    request.errorCode = null;
    request.activeAttemptId = null;
    this.transition(request, "request.completed", "completed", input.correlationId, {
      attemptId: attempt.attemptId,
      outputDigest,
      usage: asJsonValue(input.usage),
      stopReason: input.stopReason,
    }, null);
    return deepClone(request);
  }

  failAttempt(input: {
    requestId: string;
    attemptId: string;
    errorClass: string;
    errorMessage: string;
    retryable: boolean;
    retryAfterMilliseconds?: number;
    correlationId: string;
  }): ProviderRequestRecord {
    const request = this.requireRequest(input.requestId);
    this.assertStatus(request, ["dispatched", "streaming"]);
    const attempt = this.requireActiveAttempt(request, input.attemptId);
    assertNonEmpty(input.errorClass, "errorClass");
    assertNonEmpty(input.errorMessage, "errorMessage");
    const now = this.clock.now();
    attempt.status = "failed";
    attempt.finishedAt = now;
    attempt.errorClass = input.errorClass;
    attempt.errorMessage = input.errorMessage;
    attempt.retryable = input.retryable;
    attempt.revision += 1;
    request.errorCode = input.errorClass;
    request.activeAttemptId = null;
    const attemptsRemain =
      request.attemptIds.length < request.budget.maximumAttempts;
    const canRetry =
      input.retryable && attemptsRemain && now < request.budget.deadlineAt;
    if (canRetry) {
      const delay = input.retryAfterMilliseconds ?? 0;
      assertNonNegativeInteger(delay, "retryAfterMilliseconds");
      request.retryAt = Math.min(now + delay, request.budget.deadlineAt);
      this.transition(request, "attempt.retry_scheduled", "retry_wait", input.correlationId, {
        attemptId: attempt.attemptId,
        errorClass: input.errorClass,
        retryAt: request.retryAt,
        remainingAttempts: request.budget.maximumAttempts - request.attemptIds.length,
      }, null);
    } else {
      request.stopReason = input.retryable
        ? attemptsRemain
          ? "deadline_exceeded"
          : "attempt_budget_exhausted"
        : "non_retryable_error";
      this.transition(request, "request.failed", "failed", input.correlationId, {
        attemptId: attempt.attemptId,
        errorClass: input.errorClass,
        retryable: input.retryable,
        stopReason: request.stopReason,
      }, null);
    }
    return deepClone(request);
  }

  cancel(requestId: string, reason: string, correlationId: string): ProviderRequestRecord {
    const request = this.requireRequest(requestId);
    if (isTerminal(request.status)) {
      return deepClone(request);
    }
    assertNonEmpty(reason, "reason");
    if (request.activeAttemptId !== null) {
      const attempt = this.requireAttempt(request.activeAttemptId);
      attempt.status = "abandoned";
      attempt.finishedAt = this.clock.now();
      attempt.revision += 1;
    }
    request.activeAttemptId = null;
    request.cancellationReason = reason;
    request.stopReason = "cancelled";
    this.transition(request, "request.cancelled", "cancelled", correlationId, {
      reason,
    }, null);
    return deepClone(request);
  }

  get(requestId: string): ProviderRequestRecord {
    return deepClone(this.requireRequest(requestId));
  }

  getAttempt(attemptId: string): ProviderAttemptRecord {
    return deepClone(this.requireAttempt(attemptId));
  }

  getChunks(attemptId: string, afterSequence = 0): ProviderResponseChunk[] {
    const attempt = this.requireAttempt(attemptId);
    assertNonNegativeInteger(afterSequence, "afterSequence");
    return attempt.chunkIds
      .map((chunkId) => this.requireChunk(chunkId))
      .filter((chunk) => chunk.sequence > afterSequence)
      .sort((left, right) => compareNumbers(left.sequence, right.sequence))
      .map((chunk) => deepClone(chunk));
  }

  pending(): ProviderRequestRecord[] {
    return [...this.requests.values()]
      .filter((request) => !isTerminal(request.status))
      .sort(
        (left, right) =>
          compareNumbers(left.createdAt, right.createdAt) ||
          compareStrings(left.requestId, right.requestId),
      )
      .map((request) => deepClone(request));
  }

  listTransitions(
    requestId: string,
    afterSequence = 0,
  ): ProviderRequestTransition[] {
    this.requireRequest(requestId);
    return (this.transitions.get(requestId) ?? [])
      .filter((transition) => transition.sequence > afterSequence)
      .map((transition) => deepClone(transition));
  }

  snapshot(): ProviderRequestSnapshot {
    const body = {
      version: "zyra.provider-requests/v1" as const,
      restartEpoch: this.restartEpoch,
      requests: [...this.requests.values()]
        .sort((left, right) => compareStrings(left.requestId, right.requestId))
        .map((request) => deepClone(request)),
      attempts: [...this.attempts.values()]
        .sort((left, right) => compareStrings(left.attemptId, right.attemptId))
        .map((attempt) => deepClone(attempt)),
      chunks: [...this.chunks.values()]
        .sort(
          (left, right) =>
            compareStrings(left.attemptId, right.attemptId) ||
            compareNumbers(left.sequence, right.sequence),
        )
        .map((chunk) => deepClone(chunk)),
      transitions: [...this.transitions.values()]
        .flat()
        .sort(
          (left, right) =>
            compareStrings(left.requestId, right.requestId) ||
            compareNumbers(left.sequence, right.sequence),
        )
        .map((transition) => deepClone(transition)),
    };
    return { ...body, checksum: digestJson(body) };
  }

  restore(snapshot: ProviderRequestSnapshot): void {
    const { checksum, ...body } = snapshot;
    if (snapshot.version !== "zyra.provider-requests/v1") {
      throw new RuntimeInvariantError("unsupported_provider_request_snapshot", {
        version: snapshot.version,
      });
    }
    if (digestJson(body) !== checksum) {
      throw new RuntimeInvariantError("provider_request_snapshot_checksum_mismatch");
    }
    this.requests.clear();
    this.attempts.clear();
    this.chunks.clear();
    this.transitions.clear();
    this.idempotencyIndex.clear();
    for (const request of snapshot.requests) {
      validateStoredRequest(request);
      this.requests.set(request.requestId, deepClone(request));
      this.transitions.set(request.requestId, []);
      const key = `${request.sessionId}:${request.idempotencyKey}`;
      if (this.idempotencyIndex.has(key)) {
        throw new RuntimeInvariantError("duplicate_provider_idempotency_snapshot", {
          key,
        });
      }
      this.idempotencyIndex.set(key, request.requestId);
    }
    for (const attempt of snapshot.attempts) {
      if (!this.requests.has(attempt.requestId)) {
        throw new RuntimeInvariantError("provider_attempt_without_request", {
          attemptId: attempt.attemptId,
        });
      }
      this.attempts.set(attempt.attemptId, deepClone(attempt));
    }
    for (const chunk of snapshot.chunks) {
      if (!this.attempts.has(chunk.attemptId)) {
        throw new RuntimeInvariantError("provider_chunk_without_attempt", {
          chunkId: chunk.chunkId,
        });
      }
      if (chunk.payloadDigest !== digestJson(chunk.payload)) {
        throw new RuntimeInvariantError("provider_chunk_digest_mismatch", {
          chunkId: chunk.chunkId,
        });
      }
      this.chunks.set(chunk.chunkId, deepClone(chunk));
    }
    for (const transition of snapshot.transitions) {
      const values = this.transitions.get(transition.requestId);
      if (values === undefined) {
        throw new RuntimeInvariantError("provider_transition_without_request", {
          transitionId: transition.transitionId,
        });
      }
      values.push(deepClone(transition));
    }
    this.restartEpoch = snapshot.restartEpoch + 1;
    for (const request of this.requests.values()) {
      request.restartEpoch = this.restartEpoch;
      if (request.status === "dispatched" || request.status === "streaming") {
        request.status = "retry_wait";
        request.retryAt = this.clock.now();
        request.activeAttemptId = null;
        request.revision += 1;
      }
    }
  }

  private transition(
    request: ProviderRequestRecord,
    kind: string,
    nextStatus: ProviderRequestStatus,
    correlationId: string,
    payload: JsonRecord,
    previousOverride: ProviderRequestStatus | null,
  ): void {
    assertNonEmpty(correlationId, "correlationId");
    const previousStatus = previousOverride ?? request.status;
    request.status = nextStatus;
    request.updatedAt = this.clock.now();
    request.revision += 1;
    const values = this.transitions.get(request.requestId);
    if (values === undefined) {
      throw new RuntimeInvariantError("provider_transition_store_missing", {
        requestId: request.requestId,
      });
    }
    if (values.length >= this.maximumTransitionsPerRequest) {
      throw new RuntimeInvariantError("provider_transition_limit_exceeded", {
        requestId: request.requestId,
        maximum: this.maximumTransitionsPerRequest,
      });
    }
    const transition: ProviderRequestTransition = {
      transitionId: this.ids.next("provider-request-transition"),
      requestId: request.requestId,
      sequence: values.length + 1,
      previousStatus,
      nextStatus,
      kind,
      occurredAt: this.clock.now(),
      correlationId,
      payload: deepClone(payload),
      stateDigest: requestStateDigest(request),
    };
    values.push(transition);
  }

  private assertStatus(
    request: ProviderRequestRecord,
    allowed: ProviderRequestStatus[],
  ): void {
    if (!allowed.includes(request.status)) {
      throw new RuntimeInvariantError("provider_request_status_conflict", {
        requestId: request.requestId,
        actual: request.status,
        allowed,
      });
    }
  }

  private assertNotExpired(request: ProviderRequestRecord): void {
    if (this.clock.now() >= request.budget.deadlineAt) {
      throw new RuntimeInvariantError("provider_request_deadline_exceeded", {
        requestId: request.requestId,
        deadlineAt: request.budget.deadlineAt,
      });
    }
  }

  private requireActiveAttempt(
    request: ProviderRequestRecord,
    attemptId: string,
  ): ProviderAttemptRecord {
    if (request.activeAttemptId !== attemptId) {
      throw new RuntimeInvariantError("provider_attempt_not_active", {
        requestId: request.requestId,
        expected: request.activeAttemptId,
        actual: attemptId,
      });
    }
    return this.requireAttempt(attemptId);
  }

  private requireRequest(requestId: string): ProviderRequestRecord {
    const request = this.requests.get(requestId);
    if (request === undefined) {
      throw new RuntimeInvariantError("unknown_provider_request", { requestId });
    }
    return request;
  }

  private requireAttempt(attemptId: string): ProviderAttemptRecord {
    const attempt = this.attempts.get(attemptId);
    if (attempt === undefined) {
      throw new RuntimeInvariantError("unknown_provider_attempt", { attemptId });
    }
    return attempt;
  }

  private requireChunk(chunkId: string): ProviderResponseChunk {
    const chunk = this.chunks.get(chunkId);
    if (chunk === undefined) {
      throw new RuntimeInvariantError("unknown_provider_chunk", { chunkId });
    }
    return chunk;
  }
}

function validateCreate(input: {
  sessionId: string;
  runId: string;
  queryId: string;
  turnId: string;
  idempotencyKey: string;
  modelPreference: string;
  contextDigest: string;
  toolSetDigest: string;
  budget: ProviderRequestBudget;
  correlationId: string;
}): void {
  assertNonEmpty(input.sessionId, "sessionId");
  assertNonEmpty(input.runId, "runId");
  assertNonEmpty(input.queryId, "queryId");
  assertNonEmpty(input.turnId, "turnId");
  assertNonEmpty(input.idempotencyKey, "idempotencyKey");
  assertNonEmpty(input.modelPreference, "modelPreference");
  assertNonEmpty(input.contextDigest, "contextDigest");
  assertNonEmpty(input.toolSetDigest, "toolSetDigest");
  assertNonEmpty(input.correlationId, "correlationId");
  validateBudget(input.budget);
}

function validateBudget(budget: ProviderRequestBudget): void {
  assertNonNegativeInteger(budget.maximumAttempts, "maximumAttempts");
  assertNonNegativeInteger(budget.maximumInputTokens, "maximumInputTokens");
  assertNonNegativeInteger(budget.maximumOutputTokens, "maximumOutputTokens");
  assertNonNegativeInteger(budget.deadlineAt, "deadlineAt");
  if (budget.maximumAttempts === 0) {
    throw new RuntimeInvariantError("provider_request_zero_attempts");
  }
  if (budget.maximumCost !== null && budget.maximumCost < 0) {
    throw new RuntimeInvariantError("provider_request_negative_cost_budget", {
      maximumCost: budget.maximumCost,
    });
  }
}

function validateUsage(
  usage: ProviderUsage,
  budget: ProviderRequestBudget,
): void {
  assertNonNegativeInteger(usage.inputTokens, "usage.inputTokens");
  assertNonNegativeInteger(usage.outputTokens, "usage.outputTokens");
  assertNonNegativeInteger(usage.cacheReadTokens, "usage.cacheReadTokens");
  assertNonNegativeInteger(usage.cacheWriteTokens, "usage.cacheWriteTokens");
  if (usage.cost !== null) {
    assertFiniteNumber(usage.cost, "usage.cost");
    if (usage.cost < 0) {
      throw new RuntimeInvariantError("provider_negative_usage_cost", {
        cost: usage.cost,
      });
    }
  }
  if (usage.inputTokens > budget.maximumInputTokens) {
    throw new RuntimeInvariantError("provider_input_token_budget_exceeded", {
      actual: usage.inputTokens,
      maximum: budget.maximumInputTokens,
    });
  }
  if (usage.outputTokens > budget.maximumOutputTokens) {
    throw new RuntimeInvariantError("provider_output_token_budget_exceeded", {
      actual: usage.outputTokens,
      maximum: budget.maximumOutputTokens,
    });
  }
  if (
    usage.cost !== null &&
    budget.maximumCost !== null &&
    usage.cost > budget.maximumCost
  ) {
    throw new RuntimeInvariantError("provider_cost_budget_exceeded", {
      actual: usage.cost,
      maximum: budget.maximumCost,
    });
  }
}

function validateStoredRequest(request: ProviderRequestRecord): void {
  assertNonEmpty(request.requestId, "requestId");
  assertNonEmpty(request.sessionId, "sessionId");
  if (request.payloadDigest !== digestJson(request.payload)) {
    throw new RuntimeInvariantError("provider_request_payload_digest_mismatch", {
      requestId: request.requestId,
    });
  }
  validateBudget(request.budget);
}

function digestHeaders(headers: Record<string, string>): string {
  const redacted: Record<string, string> = {};
  for (const [name, value] of Object.entries(headers).sort(([left], [right]) =>
    compareStrings(left.toLowerCase(), right.toLowerCase()),
  )) {
    redacted[name.toLowerCase()] = digestJson(value);
  }
  return digestJson(redacted);
}

function requestStateDigest(request: ProviderRequestRecord): string {
  return digestJson({
    requestId: request.requestId,
    status: request.status,
    attemptIds: request.attemptIds,
    activeAttemptId: request.activeAttemptId,
    retryAt: request.retryAt,
    outputDigest: request.outputDigest,
    usage: request.usage,
    stopReason: request.stopReason,
    errorCode: request.errorCode,
    cancellationReason: request.cancellationReason,
    restartEpoch: request.restartEpoch,
    revision: request.revision,
  });
}

function isTerminal(status: ProviderRequestStatus): boolean {
  return status === "completed" || status === "failed" || status === "cancelled";
}
