import { createHash } from "node:crypto";

export type GatewaySurface =
  | "code_worker"
  | "browser_worker"
  | "mcp_tool"
  | "host_tool"
  | "remote_worker"
  | "control_command";

export type GatewayOutcome =
  | "allowed"
  | "denied"
  | "pending"
  | "committed"
  | "quarantined"
  | "cancelled"
  | "failed"
  | "recovery_required";

export interface WorkerGatewayIdentity {
  readonly runId: string;
  readonly taskId: string;
  readonly nodeId?: string;
  readonly workerId: string;
  readonly sessionId: string;
  readonly requestId?: string;
  readonly workspaceId?: string;
  readonly ownerEpoch: number;
  readonly backendId: string;
  readonly generation: number;
}

export interface GatewayInvocationEnvelope {
  readonly schema: "zyra.gateway-integration-envelope.v1";
  readonly invocationId: string;
  readonly surface: GatewaySurface;
  readonly action: string;
  readonly identity: WorkerGatewayIdentity;
  readonly toolCallId: string;
  readonly argumentsDigest: string;
  readonly policyDigest: string;
  readonly permissionRequestId?: string;
  readonly permissionGrantDigest?: string;
  readonly idempotencyKey?: string;
  readonly causationId?: string;
  readonly correlationId?: string;
  readonly createdAt: number;
  readonly metadata: Readonly<Record<string, unknown>>;
}

export interface GatewayExecutionReceipt {
  readonly schema: "zyra.gateway-integration-receipt.v1";
  readonly receiptId: string;
  readonly invocationDigest: string;
  readonly outcome: GatewayOutcome;
  readonly resultDigest: string;
  readonly permissionConsumptionId?: string;
  readonly commandReceiptId?: string;
  readonly patchReceiptId?: string;
  readonly artifactRefs: readonly string[];
  readonly eventRefs: readonly string[];
  readonly ownerEpochBefore: number;
  readonly ownerEpochAfter: number;
  readonly backendGeneration: number;
  readonly startedAt: number;
  readonly finishedAt: number;
  readonly failureCode?: string;
  readonly metadata: Readonly<Record<string, unknown>>;
}

const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
const DIGEST = /^sha256:[a-f0-9]{64}$/;

export function canonicalize(value: unknown): unknown {
  if (value === null || value === undefined) {
    return value ?? null;
  }
  if (
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  ) {
    return value;
  }
  if (typeof value === "bigint") {
    return value.toString(10);
  }
  if (value instanceof Uint8Array) {
    return Object.freeze({
      encoding: "sha256",
      bytes: value.byteLength,
      digest: digestBytes(value),
    });
  }
  if (Array.isArray(value)) {
    return Object.freeze(value.map((item) => canonicalize(item)));
  }
  if (value instanceof Set) {
    const items = [...value].map((item) => canonicalize(item));
    items.sort((left, right) =>
      JSON.stringify(left).localeCompare(JSON.stringify(right)),
    );
    return Object.freeze(items);
  }
  if (value instanceof Map) {
    const mapped: Record<string, unknown> = {};
    const entries = [...value.entries()].sort(([left], [right]) =>
      String(left).localeCompare(String(right)),
    );
    for (const [key, item] of entries) {
      mapped[String(key)] = canonicalize(item);
    }
    return Object.freeze(mapped);
  }
  if (typeof value === "object") {
    const mapped: Record<string, unknown> = {};
    const entries = Object.entries(value as Record<string, unknown>).sort(
      ([left], [right]) => left.localeCompare(right),
    );
    for (const [key, item] of entries) {
      mapped[key] = canonicalize(item);
    }
    return Object.freeze(mapped);
  }
  return String(value);
}

export function canonicalJson(value: unknown): string {
  return JSON.stringify(canonicalize(value));
}

export function digestValue(value: unknown): string {
  return `sha256:${createHash("sha256").update(canonicalJson(value)).digest("hex")}`;
}

export function digestBytes(value: Uint8Array): string {
  return `sha256:${createHash("sha256").update(value).digest("hex")}`;
}

export function stableId(namespace: string, ...parts: readonly unknown[]): string {
  requireIdentifier(namespace, "namespace");
  return `${namespace}:${createHash("sha256")
    .update(canonicalJson([namespace, ...parts]))
    .digest("hex")
    .slice(0, 32)}`;
}

export function requireIdentifier(value: string, field: string): string {
  const normalized = String(value ?? "").trim();
  if (!IDENTIFIER.test(normalized)) {
    throw new Error(`${field} is not a stable identifier`);
  }
  return normalized;
}

export function requireDigest(value: string, field: string): string {
  const normalized = String(value ?? "").trim().toLowerCase();
  if (!DIGEST.test(normalized)) {
    throw new Error(`${field} is not a sha256 digest`);
  }
  return normalized;
}

export function freezeIdentity(
  value: WorkerGatewayIdentity,
): Readonly<WorkerGatewayIdentity> {
  requireIdentifier(value.runId, "runId");
  requireIdentifier(value.taskId, "taskId");
  requireIdentifier(value.workerId, "workerId");
  requireIdentifier(value.sessionId, "sessionId");
  requireIdentifier(value.backendId, "backendId");
  if (value.nodeId) requireIdentifier(value.nodeId, "nodeId");
  if (value.requestId) requireIdentifier(value.requestId, "requestId");
  if (value.workspaceId) requireIdentifier(value.workspaceId, "workspaceId");
  if (!Number.isSafeInteger(value.ownerEpoch) || value.ownerEpoch < 0) {
    throw new Error("ownerEpoch must be a non-negative safe integer");
  }
  if (!Number.isSafeInteger(value.generation) || value.generation < 0) {
    throw new Error("generation must be a non-negative safe integer");
  }
  return Object.freeze({ ...value });
}

export function createInvocation(input: {
  surface: GatewaySurface;
  action: string;
  identity: WorkerGatewayIdentity;
  toolCallId: string;
  arguments: unknown;
  policyDigest: string;
  permissionRequestId?: string;
  permissionGrantDigest?: string;
  idempotencyKey?: string;
  causationId?: string;
  correlationId?: string;
  metadata?: Readonly<Record<string, unknown>>;
  now?: number;
}): Readonly<GatewayInvocationEnvelope> {
  const identity = freezeIdentity(input.identity);
  const toolCallId = requireIdentifier(input.toolCallId, "toolCallId");
  const argumentsDigest = digestValue(input.arguments);
  const policyDigest = requireDigest(input.policyDigest, "policyDigest");
  const invocationId = stableId(
    "gateway-invocation",
    identity,
    toolCallId,
    argumentsDigest,
    policyDigest,
  );
  if (input.permissionRequestId) {
    requireIdentifier(input.permissionRequestId, "permissionRequestId");
  }
  if (input.permissionGrantDigest) {
    requireDigest(input.permissionGrantDigest, "permissionGrantDigest");
  }
  return Object.freeze({
    schema: "zyra.gateway-integration-envelope.v1",
    invocationId,
    surface: input.surface,
    action: String(input.action),
    identity,
    toolCallId,
    argumentsDigest,
    policyDigest,
    permissionRequestId: input.permissionRequestId,
    permissionGrantDigest: input.permissionGrantDigest,
    idempotencyKey: input.idempotencyKey,
    causationId: input.causationId,
    correlationId: input.correlationId,
    createdAt: input.now ?? Date.now() / 1000,
    metadata: Object.freeze({ ...(input.metadata ?? {}) }),
  });
}

export function createExecutionReceipt(input: {
  invocation: GatewayInvocationEnvelope;
  outcome: GatewayOutcome;
  result: unknown;
  permissionConsumptionId?: string;
  commandReceiptId?: string;
  patchReceiptId?: string;
  artifactRefs?: readonly string[];
  eventRefs?: readonly string[];
  ownerEpochBefore?: number;
  ownerEpochAfter?: number;
  backendGeneration?: number;
  startedAt?: number;
  finishedAt?: number;
  failureCode?: string;
  metadata?: Readonly<Record<string, unknown>>;
}): Readonly<GatewayExecutionReceipt> {
  const invocationDigest = digestValue(input.invocation);
  const resultDigest = digestValue(input.result);
  const ownerEpochBefore = input.ownerEpochBefore ?? 0;
  const ownerEpochAfter = input.ownerEpochAfter ?? ownerEpochBefore;
  const backendGeneration = input.backendGeneration ?? 0;
  const startedAt = input.startedAt ?? Date.now() / 1000;
  const finishedAt = input.finishedAt ?? startedAt;
  if (ownerEpochBefore < 0 || ownerEpochAfter < ownerEpochBefore) {
    throw new Error("receipt owner epoch moved backwards");
  }
  if (backendGeneration < 0) {
    throw new Error("backend generation cannot be negative");
  }
  if (finishedAt < startedAt) {
    throw new Error("receipt finished before it started");
  }
  const receiptId = stableId(
    "gateway-execution",
    invocationDigest,
    input.outcome,
    resultDigest,
  );
  return Object.freeze({
    schema: "zyra.gateway-integration-receipt.v1",
    receiptId,
    invocationDigest,
    outcome: input.outcome,
    resultDigest,
    permissionConsumptionId: input.permissionConsumptionId,
    commandReceiptId: input.commandReceiptId,
    patchReceiptId: input.patchReceiptId,
    artifactRefs: Object.freeze([...(input.artifactRefs ?? [])]),
    eventRefs: Object.freeze([...(input.eventRefs ?? [])]),
    ownerEpochBefore,
    ownerEpochAfter,
    backendGeneration,
    startedAt,
    finishedAt,
    failureCode: input.failureCode,
    metadata: Object.freeze({ ...(input.metadata ?? {}) }),
  });
}

export function assertInvocationReplay(
  approved: GatewayInvocationEnvelope,
  replay: GatewayInvocationEnvelope,
): void {
  const fields: readonly (keyof GatewayInvocationEnvelope)[] = [
    "schema",
    "invocationId",
    "surface",
    "action",
    "toolCallId",
    "argumentsDigest",
    "policyDigest",
    "permissionRequestId",
    "permissionGrantDigest",
    "idempotencyKey",
    "causationId",
    "correlationId",
  ];
  for (const field of fields) {
    if (canonicalJson(approved[field]) !== canonicalJson(replay[field])) {
      throw new Error(`gateway invocation replay changed ${field}`);
    }
  }
  if (canonicalJson(approved.identity) !== canonicalJson(replay.identity)) {
    throw new Error("gateway invocation replay changed identity");
  }
}

export function receiptDigest(receipt: GatewayExecutionReceipt): string {
  return digestValue(receipt);
}
