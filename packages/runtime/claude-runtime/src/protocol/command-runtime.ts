import {
  type Clock,
  type IdFactory,
  type JsonRecord,
  type JsonValue,
  RandomIdFactory,
  RuntimeInvariantError,
  SystemClock,
  assertNonEmpty,
  assertNonNegativeInteger,
  compareNumbers,
  compareStrings,
  deepClone,
  digestJson,
  uniqueSorted,
  withTimeout,
} from "../core/runtime-primitives.js";

export type RuntimeCommandRisk = "read" | "mutate" | "interrupt" | "destructive";
export type RuntimeCommandStatus =
  | "accepted"
  | "authorized"
  | "running"
  | "committed"
  | "rejected"
  | "failed"
  | "expired";

export interface RuntimeCommandField {
  name: string;
  type: "string" | "number" | "boolean" | "object" | "array";
  required: boolean;
  enumValues: JsonValue[];
  minimum: number | null;
  maximum: number | null;
  maximumLength: number | null;
}

export interface RuntimeCommandDescriptor {
  commandName: string;
  owner: string;
  description: string;
  risk: RuntimeCommandRisk;
  fields: RuntimeCommandField[];
  requiredCapabilities: string[];
  allowedSessionStates: string[];
  timeoutMilliseconds: number;
  idempotent: boolean;
  enabled: boolean;
  revision: number;
  metadata: JsonRecord;
}

export interface RuntimeCommandRequest {
  commandId?: string;
  commandName: string;
  sessionId: string;
  runId: string;
  actorId: string;
  actorCapabilities: string[];
  sessionState: string;
  expectedSessionRevision: number | null;
  idempotencyKey: string;
  correlationId: string;
  causationId: string | null;
  arguments: JsonRecord;
  requestedAt?: number;
  expiresAt?: number | null;
  metadata?: JsonRecord;
}

export interface RuntimeCommandAuthorization {
  decisionId: string;
  allowed: boolean;
  reason: string;
  decidedAt: number;
  policyRevision: number;
  obligations: JsonRecord;
}

export interface RuntimeCommandExecution {
  commandId: string;
  commandName: string;
  sessionId: string;
  runId: string;
  actorId: string;
  status: RuntimeCommandStatus;
  arguments: JsonRecord;
  argumentDigest: string;
  idempotencyKey: string;
  correlationId: string;
  causationId: string | null;
  expectedSessionRevision: number | null;
  authorization: RuntimeCommandAuthorization | null;
  acceptedAt: number;
  startedAt: number | null;
  finishedAt: number | null;
  result: JsonValue | null;
  resultDigest: string | null;
  effectIds: string[];
  errorCode: string | null;
  errorMessage: string | null;
  revision: number;
  metadata: JsonRecord;
}

export interface RuntimeCommandContext {
  commandId: string;
  sessionId: string;
  runId: string;
  actorId: string;
  correlationId: string;
  arguments: JsonRecord;
  authorization: RuntimeCommandAuthorization;
  expectedSessionRevision: number | null;
}

export interface RuntimeCommandHandlerResult {
  result: JsonValue;
  effectIds?: string[];
  sessionRevision?: number | null;
  metadata?: JsonRecord;
}

export type RuntimeCommandHandler = (
  context: Readonly<RuntimeCommandContext>,
) => Promise<RuntimeCommandHandlerResult> | RuntimeCommandHandlerResult;

export type RuntimeCommandAuthorizer = (
  descriptor: Readonly<RuntimeCommandDescriptor>,
  request: Readonly<RuntimeCommandRequest>,
) => Promise<RuntimeCommandAuthorization> | RuntimeCommandAuthorization;

export interface RuntimeCommandSnapshot {
  version: "zyra.protocol-commands/v1";
  descriptors: RuntimeCommandDescriptor[];
  executions: RuntimeCommandExecution[];
  checksum: string;
}

export interface RuntimeCommandOptions {
  clock?: Clock;
  ids?: IdFactory;
  maximumExecutions?: number;
}

interface CommandBinding {
  descriptor: RuntimeCommandDescriptor;
  handler: RuntimeCommandHandler;
}

export class RuntimeCommandRuntime {
  private readonly clock: Clock;
  private readonly ids: IdFactory;
  private readonly maximumExecutions: number;
  private readonly descriptors = new Map<string, RuntimeCommandDescriptor>();
  private readonly bindings = new Map<string, CommandBinding>();
  private readonly executions = new Map<string, RuntimeCommandExecution>();
  private readonly idempotencyIndex = new Map<string, string>();

  constructor(options: RuntimeCommandOptions = {}) {
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.maximumExecutions = options.maximumExecutions ?? 20_000;
    assertNonNegativeInteger(this.maximumExecutions, "maximumExecutions");
  }

  register(
    descriptor: RuntimeCommandDescriptor,
    handler: RuntimeCommandHandler,
  ): RuntimeCommandDescriptor {
    const normalized = normalizeDescriptor(descriptor);
    if (this.descriptors.has(normalized.commandName)) {
      throw new RuntimeInvariantError("runtime_command_already_registered", {
        commandName: normalized.commandName,
      });
    }
    this.descriptors.set(normalized.commandName, normalized);
    this.bindings.set(normalized.commandName, {
      descriptor: normalized,
      handler,
    });
    return deepClone(normalized);
  }

  bind(commandName: string, handler: RuntimeCommandHandler): void {
    const descriptor = this.requireDescriptor(commandName);
    this.bindings.set(commandName, { descriptor, handler });
  }

  configure(
    commandName: string,
    update: Partial<
      Pick<
        RuntimeCommandDescriptor,
        | "description"
        | "requiredCapabilities"
        | "allowedSessionStates"
        | "timeoutMilliseconds"
        | "enabled"
        | "metadata"
      >
    >,
  ): RuntimeCommandDescriptor {
    const previous = this.requireDescriptor(commandName);
    const next = normalizeDescriptor({
      ...previous,
      ...deepClone(update),
      commandName,
      revision: previous.revision + 1,
    });
    this.descriptors.set(commandName, next);
    const binding = this.bindings.get(commandName);
    if (binding !== undefined) {
      this.bindings.set(commandName, { descriptor: next, handler: binding.handler });
    }
    return deepClone(next);
  }

  accept(request: RuntimeCommandRequest): RuntimeCommandExecution {
    const descriptor = this.requireDescriptor(request.commandName);
    validateRequest(request, descriptor, this.clock.now());
    const indexKey = idempotencyIndexKey(request.sessionId, request.idempotencyKey);
    const existingId = this.idempotencyIndex.get(indexKey);
    if (existingId !== undefined) {
      const existing = this.requireExecution(existingId);
      if (
        existing.commandName !== request.commandName ||
        existing.argumentDigest !== digestJson(request.arguments)
      ) {
        throw new RuntimeInvariantError("runtime_command_idempotency_conflict", {
          sessionId: request.sessionId,
          idempotencyKey: request.idempotencyKey,
          commandId: existingId,
        });
      }
      return deepClone(existing);
    }
    if (this.executions.size >= this.maximumExecutions) {
      this.evictTerminalExecutions();
    }
    if (this.executions.size >= this.maximumExecutions) {
      throw new RuntimeInvariantError("runtime_command_execution_limit", {
        maximum: this.maximumExecutions,
      });
    }
    const commandId = request.commandId ?? this.ids.next("runtime-command");
    if (this.executions.has(commandId)) {
      throw new RuntimeInvariantError("runtime_command_id_conflict", { commandId });
    }
    const execution: RuntimeCommandExecution = {
      commandId,
      commandName: request.commandName,
      sessionId: request.sessionId,
      runId: request.runId,
      actorId: request.actorId,
      status: "accepted",
      arguments: deepClone(request.arguments),
      argumentDigest: digestJson(request.arguments),
      idempotencyKey: request.idempotencyKey,
      correlationId: request.correlationId,
      causationId: request.causationId,
      expectedSessionRevision: request.expectedSessionRevision,
      authorization: null,
      acceptedAt: request.requestedAt ?? this.clock.now(),
      startedAt: null,
      finishedAt: null,
      result: null,
      resultDigest: null,
      effectIds: [],
      errorCode: null,
      errorMessage: null,
      revision: 1,
      metadata: {
        ...deepClone(request.metadata ?? {}),
        expiresAt: request.expiresAt ?? null,
      },
    };
    this.executions.set(commandId, execution);
    this.idempotencyIndex.set(indexKey, commandId);
    return deepClone(execution);
  }

  async authorize(
    commandId: string,
    request: RuntimeCommandRequest,
    authorizer: RuntimeCommandAuthorizer,
  ): Promise<RuntimeCommandExecution> {
    const execution = this.requireExecution(commandId);
    this.assertStatus(execution, ["accepted"]);
    this.assertRequestMatches(execution, request);
    const descriptor = this.requireDescriptor(execution.commandName);
    const authorization = await authorizer(
      deepClone(descriptor),
      deepClone(request),
    );
    validateAuthorization(authorization);
    execution.authorization = deepClone(authorization);
    execution.status = authorization.allowed ? "authorized" : "rejected";
    execution.finishedAt = authorization.allowed ? null : this.clock.now();
    execution.errorCode = authorization.allowed ? null : "authorization_denied";
    execution.errorMessage = authorization.allowed ? null : authorization.reason;
    execution.revision += 1;
    return deepClone(execution);
  }

  async execute(commandId: string): Promise<RuntimeCommandExecution> {
    const execution = this.requireExecution(commandId);
    if (execution.status === "committed") {
      return deepClone(execution);
    }
    this.assertStatus(execution, ["authorized"]);
    const binding = this.bindings.get(execution.commandName);
    if (binding === undefined) {
      throw new RuntimeInvariantError("runtime_command_handler_not_bound", {
        commandName: execution.commandName,
      });
    }
    const authorization = execution.authorization;
    if (authorization === null || !authorization.allowed) {
      throw new RuntimeInvariantError("runtime_command_not_authorized", {
        commandId,
      });
    }
    execution.status = "running";
    execution.startedAt = this.clock.now();
    execution.revision += 1;
    try {
      const result = await withTimeout(
        Promise.resolve(
          binding.handler({
            commandId,
            sessionId: execution.sessionId,
            runId: execution.runId,
            actorId: execution.actorId,
            correlationId: execution.correlationId,
            arguments: deepClone(execution.arguments),
            authorization: deepClone(authorization),
            expectedSessionRevision: execution.expectedSessionRevision,
          }),
        ),
        binding.descriptor.timeoutMilliseconds,
        `runtime-command:${execution.commandName}`,
      );
      validateHandlerResult(result);
      execution.status = "committed";
      execution.finishedAt = this.clock.now();
      execution.result = deepClone(result.result);
      execution.resultDigest = digestJson(result.result);
      execution.effectIds = uniqueSorted(result.effectIds ?? []);
      execution.metadata = {
        ...execution.metadata,
        ...deepClone(result.metadata ?? {}),
        resultingSessionRevision: result.sessionRevision ?? null,
      };
      execution.revision += 1;
      return deepClone(execution);
    } catch (error) {
      execution.status = "failed";
      execution.finishedAt = this.clock.now();
      execution.errorCode =
        error instanceof RuntimeInvariantError ? error.code : "command_failed";
      execution.errorMessage =
        error instanceof Error ? error.message : String(error);
      execution.revision += 1;
      return deepClone(execution);
    }
  }

  expire(now = this.clock.now()): string[] {
    const expired: string[] = [];
    for (const execution of this.executions.values()) {
      if (execution.status !== "accepted" && execution.status !== "authorized") {
        continue;
      }
      const expiresAt = execution.metadata.expiresAt;
      if (typeof expiresAt === "number" && expiresAt <= now) {
        execution.status = "expired";
        execution.finishedAt = now;
        execution.errorCode = "command_expired";
        execution.errorMessage = "command expired before execution";
        execution.revision += 1;
        expired.push(execution.commandId);
      }
    }
    return expired.sort(compareStrings);
  }

  retry(commandId: string): RuntimeCommandExecution {
    const execution = this.requireExecution(commandId);
    this.assertStatus(execution, ["failed"]);
    const descriptor = this.requireDescriptor(execution.commandName);
    if (!descriptor.idempotent || execution.effectIds.length > 0) {
      throw new RuntimeInvariantError("runtime_command_not_retryable", {
        commandId,
        idempotent: descriptor.idempotent,
        effectIds: execution.effectIds,
      });
    }
    execution.status = "authorized";
    execution.startedAt = null;
    execution.finishedAt = null;
    execution.errorCode = null;
    execution.errorMessage = null;
    execution.revision += 1;
    return deepClone(execution);
  }

  get(commandId: string): RuntimeCommandExecution {
    return deepClone(this.requireExecution(commandId));
  }

  list(filter: {
    sessionId?: string;
    commandName?: string;
    status?: RuntimeCommandStatus;
  } = {}): RuntimeCommandExecution[] {
    return [...this.executions.values()]
      .filter(
        (execution) =>
          (filter.sessionId === undefined ||
            execution.sessionId === filter.sessionId) &&
          (filter.commandName === undefined ||
            execution.commandName === filter.commandName) &&
          (filter.status === undefined || execution.status === filter.status),
      )
      .sort(
        (left, right) =>
          compareNumbers(left.acceptedAt, right.acceptedAt) ||
          compareStrings(left.commandId, right.commandId),
      )
      .map((execution) => deepClone(execution));
  }

  snapshot(): RuntimeCommandSnapshot {
    const body = {
      version: "zyra.protocol-commands/v1" as const,
      descriptors: [...this.descriptors.values()]
        .sort((left, right) =>
          compareStrings(left.commandName, right.commandName),
        )
        .map((descriptor) => deepClone(descriptor)),
      executions: this.list(),
    };
    return { ...body, checksum: digestJson(body) };
  }

  restore(snapshot: RuntimeCommandSnapshot): void {
    const { checksum, ...body } = snapshot;
    if (snapshot.version !== "zyra.protocol-commands/v1") {
      throw new RuntimeInvariantError("unsupported_runtime_command_snapshot", {
        version: snapshot.version,
      });
    }
    if (digestJson(body) !== checksum) {
      throw new RuntimeInvariantError("runtime_command_snapshot_checksum_mismatch");
    }
    const existingHandlers = new Map(
      [...this.bindings.entries()].map(([name, binding]) => [name, binding.handler]),
    );
    this.descriptors.clear();
    this.bindings.clear();
    this.executions.clear();
    this.idempotencyIndex.clear();
    for (const source of snapshot.descriptors) {
      const descriptor = normalizeDescriptor(source);
      this.descriptors.set(descriptor.commandName, descriptor);
      const handler = existingHandlers.get(descriptor.commandName);
      if (handler !== undefined) {
        this.bindings.set(descriptor.commandName, { descriptor, handler });
      }
    }
    for (const execution of snapshot.executions) {
      validateExecution(execution);
      this.executions.set(execution.commandId, deepClone(execution));
      const key = idempotencyIndexKey(
        execution.sessionId,
        execution.idempotencyKey,
      );
      if (this.idempotencyIndex.has(key)) {
        throw new RuntimeInvariantError("duplicate_command_idempotency_snapshot", {
          key,
        });
      }
      this.idempotencyIndex.set(key, execution.commandId);
    }
    this.expire();
  }

  private requireDescriptor(commandName: string): RuntimeCommandDescriptor {
    const descriptor = this.descriptors.get(commandName);
    if (descriptor === undefined) {
      throw new RuntimeInvariantError("unknown_runtime_command", { commandName });
    }
    return descriptor;
  }

  private requireExecution(commandId: string): RuntimeCommandExecution {
    const execution = this.executions.get(commandId);
    if (execution === undefined) {
      throw new RuntimeInvariantError("unknown_runtime_command_execution", {
        commandId,
      });
    }
    return execution;
  }

  private assertStatus(
    execution: RuntimeCommandExecution,
    allowed: RuntimeCommandStatus[],
  ): void {
    if (!allowed.includes(execution.status)) {
      throw new RuntimeInvariantError("runtime_command_status_conflict", {
        commandId: execution.commandId,
        actual: execution.status,
        allowed,
      });
    }
  }

  private assertRequestMatches(
    execution: RuntimeCommandExecution,
    request: RuntimeCommandRequest,
  ): void {
    if (
      execution.commandName !== request.commandName ||
      execution.sessionId !== request.sessionId ||
      execution.runId !== request.runId ||
      execution.actorId !== request.actorId ||
      execution.argumentDigest !== digestJson(request.arguments)
    ) {
      throw new RuntimeInvariantError("runtime_command_request_mismatch", {
        commandId: execution.commandId,
      });
    }
  }

  private evictTerminalExecutions(): void {
    const terminal = [...this.executions.values()]
      .filter((execution) =>
        ["committed", "rejected", "failed", "expired"].includes(
          execution.status,
        ),
      )
      .sort(
        (left, right) =>
          compareNumbers(left.finishedAt ?? 0, right.finishedAt ?? 0) ||
          compareStrings(left.commandId, right.commandId),
      );
    const target = Math.max(1, Math.ceil(this.maximumExecutions * 0.1));
    for (const execution of terminal.slice(0, target)) {
      this.executions.delete(execution.commandId);
      this.idempotencyIndex.delete(
        idempotencyIndexKey(execution.sessionId, execution.idempotencyKey),
      );
    }
  }
}

function normalizeDescriptor(
  descriptor: RuntimeCommandDescriptor,
): RuntimeCommandDescriptor {
  assertNonEmpty(descriptor.commandName, "commandName");
  assertNonEmpty(descriptor.owner, "owner");
  assertNonEmpty(descriptor.description, "description");
  assertNonNegativeInteger(
    descriptor.timeoutMilliseconds,
    "timeoutMilliseconds",
  );
  if (descriptor.timeoutMilliseconds === 0) {
    throw new RuntimeInvariantError("runtime_command_zero_timeout", {
      commandName: descriptor.commandName,
    });
  }
  assertNonNegativeInteger(descriptor.revision, "revision");
  const fieldNames = new Set<string>();
  const fields = descriptor.fields.map((field) => {
    assertNonEmpty(field.name, "field.name");
    if (fieldNames.has(field.name)) {
      throw new RuntimeInvariantError("duplicate_runtime_command_field", {
        commandName: descriptor.commandName,
        fieldName: field.name,
      });
    }
    fieldNames.add(field.name);
    return deepClone(field);
  });
  return {
    ...deepClone(descriptor),
    fields,
    requiredCapabilities: uniqueSorted(descriptor.requiredCapabilities),
    allowedSessionStates: uniqueSorted(descriptor.allowedSessionStates),
  };
}

function validateRequest(
  request: RuntimeCommandRequest,
  descriptor: RuntimeCommandDescriptor,
  now: number,
): void {
  assertNonEmpty(request.commandName, "commandName");
  assertNonEmpty(request.sessionId, "sessionId");
  assertNonEmpty(request.runId, "runId");
  assertNonEmpty(request.actorId, "actorId");
  assertNonEmpty(request.sessionState, "sessionState");
  assertNonEmpty(request.idempotencyKey, "idempotencyKey");
  assertNonEmpty(request.correlationId, "correlationId");
  if (!descriptor.enabled) {
    throw new RuntimeInvariantError("runtime_command_disabled", {
      commandName: descriptor.commandName,
    });
  }
  if (
    descriptor.allowedSessionStates.length > 0 &&
    !descriptor.allowedSessionStates.includes(request.sessionState)
  ) {
    throw new RuntimeInvariantError("runtime_command_session_state_denied", {
      commandName: descriptor.commandName,
      sessionState: request.sessionState,
    });
  }
  const capabilities = new Set(request.actorCapabilities);
  const missing = descriptor.requiredCapabilities.filter(
    (capability) => !capabilities.has(capability),
  );
  if (missing.length > 0) {
    throw new RuntimeInvariantError("runtime_command_capability_missing", {
      commandName: descriptor.commandName,
      missing,
    });
  }
  if (request.expiresAt !== undefined && request.expiresAt !== null) {
    assertNonNegativeInteger(request.expiresAt, "expiresAt");
    if (request.expiresAt <= now) {
      throw new RuntimeInvariantError("runtime_command_already_expired", {
        expiresAt: request.expiresAt,
      });
    }
  }
  validateArguments(descriptor.fields, request.arguments);
}

function validateArguments(
  fields: RuntimeCommandField[],
  argumentsValue: JsonRecord,
): void {
  const known = new Set(fields.map((field) => field.name));
  const unknown = Object.keys(argumentsValue).filter((name) => !known.has(name));
  if (unknown.length > 0) {
    throw new RuntimeInvariantError("runtime_command_unknown_arguments", {
      unknown: unknown.sort(compareStrings),
    });
  }
  for (const field of fields) {
    const value = argumentsValue[field.name];
    if (value === undefined) {
      if (field.required) {
        throw new RuntimeInvariantError("runtime_command_argument_required", {
          fieldName: field.name,
        });
      }
      continue;
    }
    const actualType = Array.isArray(value)
      ? "array"
      : value !== null && typeof value === "object"
        ? "object"
        : typeof value;
    if (actualType !== field.type) {
      throw new RuntimeInvariantError("runtime_command_argument_type", {
        fieldName: field.name,
        expected: field.type,
        actual: actualType,
      });
    }
    if (
      field.enumValues.length > 0 &&
      !field.enumValues.some((candidate) => digestJson(candidate) === digestJson(value))
    ) {
      throw new RuntimeInvariantError("runtime_command_argument_enum", {
        fieldName: field.name,
      });
    }
    if (typeof value === "number") {
      if (field.minimum !== null && value < field.minimum) {
        throw new RuntimeInvariantError("runtime_command_argument_minimum", {
          fieldName: field.name,
          minimum: field.minimum,
        });
      }
      if (field.maximum !== null && value > field.maximum) {
        throw new RuntimeInvariantError("runtime_command_argument_maximum", {
          fieldName: field.name,
          maximum: field.maximum,
        });
      }
    }
    if (
      typeof value === "string" &&
      field.maximumLength !== null &&
      value.length > field.maximumLength
    ) {
      throw new RuntimeInvariantError("runtime_command_argument_too_long", {
        fieldName: field.name,
        maximumLength: field.maximumLength,
      });
    }
  }
}

function validateAuthorization(
  authorization: RuntimeCommandAuthorization,
): void {
  assertNonEmpty(authorization.decisionId, "decisionId");
  assertNonEmpty(authorization.reason, "reason");
  assertNonNegativeInteger(authorization.decidedAt, "decidedAt");
  assertNonNegativeInteger(authorization.policyRevision, "policyRevision");
}

function validateHandlerResult(result: RuntimeCommandHandlerResult): void {
  for (const effectId of result.effectIds ?? []) {
    assertNonEmpty(effectId, "effectId");
  }
  if (result.sessionRevision !== undefined && result.sessionRevision !== null) {
    assertNonNegativeInteger(result.sessionRevision, "sessionRevision");
  }
}

function validateExecution(execution: RuntimeCommandExecution): void {
  assertNonEmpty(execution.commandId, "commandId");
  assertNonEmpty(execution.commandName, "commandName");
  assertNonEmpty(execution.idempotencyKey, "idempotencyKey");
  if (execution.argumentDigest !== digestJson(execution.arguments)) {
    throw new RuntimeInvariantError("runtime_command_argument_digest_mismatch", {
      commandId: execution.commandId,
    });
  }
  if (
    execution.result !== null &&
    execution.resultDigest !== digestJson(execution.result)
  ) {
    throw new RuntimeInvariantError("runtime_command_result_digest_mismatch", {
      commandId: execution.commandId,
    });
  }
}

function idempotencyIndexKey(sessionId: string, key: string): string {
  return `${sessionId}:${key}`;
}
