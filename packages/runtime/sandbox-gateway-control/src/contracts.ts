export const COMMAND_SCHEMA = "zyra.gateway-command-envelope.v1";
export const APPROVAL_SCHEMA = "zyra.gateway-approval-binding.v1";
export const RECEIPT_SCHEMA = "zyra.gateway-control-receipt.v1";
export const CREDENTIAL_SCHEMA = "zyra.gateway-credential-envelope.v1";
export const RPC_SCHEMA = "zyra.gateway-control-rpc.v1";

export const Effects = {
  allow: "allow",
  ask: "ask",
  deny: "deny",
} as const;

export type Effect = (typeof Effects)[keyof typeof Effects];

export const Risks = {
  none: "none",
  low: "low",
  medium: "medium",
  high: "high",
  critical: "critical",
} as const;

export type Risk = (typeof Risks)[keyof typeof Risks];

export type JsonPrimitive = null | boolean | number | string;
export type JsonValue =
  | JsonPrimitive
  | readonly JsonValue[]
  | { readonly [key: string]: JsonValue };

export interface CommandBudget {
  readonly timeoutSeconds: number;
  readonly cancelGraceSeconds: number;
  readonly stdoutLimitBytes: number;
  readonly stderrLimitBytes: number;
  readonly combinedOutputLimitBytes: number;
  readonly maxProcesses: number;
}

export interface CommandEnvelope {
  readonly schema: typeof COMMAND_SCHEMA;
  readonly commandId: string;
  readonly sessionId: string;
  readonly runId: string;
  readonly taskId: string;
  readonly workerId: string;
  readonly toolUseId: string;
  readonly executable: string;
  readonly argv: readonly string[];
  readonly cwd: string;
  readonly environmentDigest: string;
  readonly budget: CommandBudget;
  readonly workspaceId: string;
  readonly ownerEpoch: number;
  readonly fenceDigest: string;
  readonly networkProfile: string;
  readonly provenanceRef: string;
  readonly idempotencyKey: string;
  readonly causationId: string;
  readonly correlationId: string;
  readonly metadata: Readonly<Record<string, JsonValue>>;
}

export interface CommandEnvelopeInput {
  readonly commandId?: string;
  readonly sessionId: string;
  readonly runId: string;
  readonly taskId: string;
  readonly workerId: string;
  readonly toolUseId: string;
  readonly executable: string;
  readonly argv?: readonly string[];
  readonly cwd?: string;
  readonly environmentDigest?: string;
  readonly budget?: Partial<CommandBudget>;
  readonly workspaceId?: string;
  readonly ownerEpoch?: number;
  readonly fenceDigest?: string;
  readonly networkProfile?: string;
  readonly provenanceRef?: string;
  readonly idempotencyKey?: string;
  readonly causationId?: string;
  readonly correlationId?: string;
  readonly metadata?: Readonly<Record<string, JsonValue>>;
}

export interface PolicyEvidence {
  readonly code: string;
  readonly effect: Effect;
  readonly risk: Risk;
  readonly reason: string;
  readonly source: string;
  readonly metadata: Readonly<Record<string, JsonValue>>;
}

export interface PolicyDecision {
  readonly effect: Effect;
  readonly reason: string;
  readonly commandDigest: string;
  readonly policyDigest: string;
  readonly evidence: readonly PolicyEvidence[];
  readonly requiresPermissionRuntime: true;
  readonly eligibleForSealedAutoAllow: boolean;
  readonly recovery: readonly string[];
  readonly metadata: Readonly<Record<string, JsonValue>>;
}

export interface ApprovalRequest {
  readonly requestId: string;
  readonly requestFingerprint: string;
  readonly envelope: CommandEnvelope;
  readonly policy: PolicyDecision;
  readonly interactive: boolean;
  readonly sealed: boolean;
  readonly createdAt: number;
}

export interface ApprovalGrant {
  readonly requestFingerprint: string;
  readonly grantDigest: string;
  readonly effect: Exclude<Effect, "deny">;
  readonly issuedAt: number;
  readonly expiresAt: number;
  readonly permissionOwner: "ToolPermissionRuntime";
  readonly metadata: Readonly<Record<string, JsonValue>>;
}

export interface ApprovalBinding {
  readonly schema: typeof APPROVAL_SCHEMA;
  readonly bindingId: string;
  readonly sessionId: string;
  readonly commandId: string;
  readonly toolUseId: string;
  readonly requestFingerprint: string;
  readonly commandDigest: string;
  readonly grantDigest: string;
  readonly effect: Exclude<Effect, "deny">;
  readonly issuedAt: number;
  readonly expiresAt: number;
  readonly consumedAt: number | null;
  readonly consumptionId: string;
  readonly revision: number;
  readonly metadata: Readonly<Record<string, JsonValue>>;
}

export interface ApprovalConsumption {
  readonly accepted: boolean;
  readonly binding: ApprovalBinding;
  readonly reason: string;
}

export interface ControlReceipt {
  readonly schema: typeof RECEIPT_SCHEMA;
  readonly receiptId: string;
  readonly commandId: string;
  readonly commandDigest: string;
  readonly policyDigest: string;
  readonly bindingId: string;
  readonly consumptionId: string;
  readonly effect: Effect;
  readonly createdAt: number;
  readonly metadata: Readonly<Record<string, JsonValue>>;
}

export interface HashlineLine {
  readonly lineNumber: number;
  readonly hash: string;
  readonly text: string;
}

export interface HashlineSnapshot {
  readonly snapshotId: string;
  readonly contentDigest: string;
  readonly lineCount: number;
  readonly newline: "\n" | "\r\n";
  readonly trailingNewline: boolean;
  readonly lines: readonly HashlineLine[];
}

export interface HashlineRange {
  readonly startLine: number;
  readonly endLine: number;
  readonly expectedStartHash: string;
  readonly expectedEndHash: string;
}

export interface HashlineEdit {
  readonly editId: string;
  readonly snapshotId: string;
  readonly range: HashlineRange;
  readonly replacement: string;
  readonly expectedContentDigest: string;
}

export interface HashlineApplyResult {
  readonly editId: string;
  readonly previousSnapshotId: string;
  readonly nextSnapshot: HashlineSnapshot;
  readonly content: string;
  readonly changed: boolean;
}

export interface CredentialRequest {
  readonly requestId: string;
  readonly sessionId: string;
  readonly commandId: string;
  readonly provider: string;
  readonly credentialName: string;
  readonly audience: string;
  readonly scope: readonly string[];
  readonly ttlMilliseconds: number;
}

export interface CredentialEnvelope {
  readonly schema: typeof CREDENTIAL_SCHEMA;
  readonly envelopeId: string;
  readonly requestId: string;
  readonly sessionId: string;
  readonly commandId: string;
  readonly audience: string;
  readonly scope: readonly string[];
  readonly secretDigest: string;
  readonly nonce: string;
  readonly issuedAt: number;
  readonly expiresAt: number;
  readonly consumedAt: number | null;
  readonly metadata: Readonly<Record<string, JsonValue>>;
}

export interface RpcRequest<T = JsonValue> {
  readonly schema: typeof RPC_SCHEMA;
  readonly requestId: string;
  readonly method: string;
  readonly params: T;
}

export interface RpcSuccess<T = JsonValue> {
  readonly schema: typeof RPC_SCHEMA;
  readonly requestId: string;
  readonly ok: true;
  readonly result: T;
}

export interface RpcFailure {
  readonly schema: typeof RPC_SCHEMA;
  readonly requestId: string;
  readonly ok: false;
  readonly error: {
    readonly code: string;
    readonly message: string;
    readonly retryable: boolean;
    readonly metadata: Readonly<Record<string, JsonValue>>;
  };
}

export type RpcResponse<T = JsonValue> = RpcSuccess<T> | RpcFailure;

export class GatewayProtocolError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly metadata: Readonly<Record<string, JsonValue>>;

  constructor(
    code: string,
    message: string,
    options: {
      readonly retryable?: boolean;
      readonly metadata?: Readonly<Record<string, JsonValue>>;
    } = {},
  ) {
    super(message);
    this.name = "GatewayProtocolError";
    this.code = code;
    this.retryable = options.retryable ?? false;
    this.metadata = options.metadata ?? {};
  }

  toJSON(): JsonValue {
    return {
      code: this.code,
      message: this.message,
      retryable: this.retryable,
      metadata: this.metadata,
    };
  }
}

export const DEFAULT_BUDGET: CommandBudget = Object.freeze({
  timeoutSeconds: 120,
  cancelGraceSeconds: 3,
  stdoutLimitBytes: 2 * 1024 * 1024,
  stderrLimitBytes: 2 * 1024 * 1024,
  combinedOutputLimitBytes: 3 * 1024 * 1024,
  maxProcesses: 32,
});

export function assertEffect(value: string): Effect {
  if (value === Effects.allow || value === Effects.ask || value === Effects.deny) {
    return value;
  }
  throw new GatewayProtocolError("invalid_effect", "Unknown policy effect: " + value);
}

export function assertRisk(value: string): Risk {
  if (
    value === Risks.none ||
    value === Risks.low ||
    value === Risks.medium ||
    value === Risks.high ||
    value === Risks.critical
  ) {
    return value;
  }
  throw new GatewayProtocolError("invalid_risk", "Unknown policy risk: " + value);
}

export function assertNonEmpty(value: string, field: string): string {
  const normalized = value.normalize("NFC").trim();
  if (!normalized) {
    throw new GatewayProtocolError("invalid_request", field + " is required");
  }
  if (/[\u0000-\u001f\u007f]/u.test(normalized)) {
    throw new GatewayProtocolError(
      "invalid_request",
      field + " contains control characters",
    );
  }
  return normalized;
}

export function normalizeBudget(
  input: Partial<CommandBudget> = {},
): CommandBudget {
  const value: CommandBudget = {
    timeoutSeconds: input.timeoutSeconds ?? DEFAULT_BUDGET.timeoutSeconds,
    cancelGraceSeconds:
      input.cancelGraceSeconds ?? DEFAULT_BUDGET.cancelGraceSeconds,
    stdoutLimitBytes:
      input.stdoutLimitBytes ?? DEFAULT_BUDGET.stdoutLimitBytes,
    stderrLimitBytes:
      input.stderrLimitBytes ?? DEFAULT_BUDGET.stderrLimitBytes,
    combinedOutputLimitBytes:
      input.combinedOutputLimitBytes ??
      DEFAULT_BUDGET.combinedOutputLimitBytes,
    maxProcesses: input.maxProcesses ?? DEFAULT_BUDGET.maxProcesses,
  };
  if (
    value.timeoutSeconds <= 0 ||
    value.cancelGraceSeconds < 0 ||
    value.stdoutLimitBytes <= 0 ||
    value.stderrLimitBytes <= 0 ||
    value.combinedOutputLimitBytes <= 0 ||
    value.maxProcesses <= 0
  ) {
    throw new GatewayProtocolError(
      "invalid_budget",
      "Command budget values are outside their valid ranges",
    );
  }
  if (
    value.combinedOutputLimitBytes >
    value.stdoutLimitBytes + value.stderrLimitBytes
  ) {
    throw new GatewayProtocolError(
      "invalid_budget",
      "Combined output budget exceeds stream budgets",
    );
  }
  return Object.freeze(value);
}

export function evidence(
  code: string,
  effect: Effect,
  risk: Risk,
  reason: string,
  source: string,
  metadata: Readonly<Record<string, JsonValue>> = {},
): PolicyEvidence {
  return Object.freeze({
    code,
    effect,
    risk,
    reason,
    source,
    metadata: Object.freeze({ ...metadata }),
  });
}

export function aggregateEffects(values: Iterable<Effect>): Effect {
  let ask = false;
  for (const value of values) {
    if (value === Effects.deny) {
      return Effects.deny;
    }
    if (value === Effects.ask) {
      ask = true;
    }
  }
  return ask ? Effects.ask : Effects.allow;
}

export function isJsonValue(value: unknown, depth = 0): value is JsonValue {
  if (depth > 32) {
    return false;
  }
  if (
    value === null ||
    typeof value === "boolean" ||
    typeof value === "string"
  ) {
    return true;
  }
  if (typeof value === "number") {
    return Number.isFinite(value);
  }
  if (Array.isArray(value)) {
    return value.every((item) => isJsonValue(item, depth + 1));
  }
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>).every(
      ([key, item]) =>
        typeof key === "string" && isJsonValue(item, depth + 1),
    );
  }
  return false;
}

export function assertJsonRecord(
  value: unknown,
  field: string,
): Readonly<Record<string, JsonValue>> {
  if (
    value === null ||
    Array.isArray(value) ||
    typeof value !== "object" ||
    !isJsonValue(value)
  ) {
    throw new GatewayProtocolError(
      "invalid_json_record",
      field + " must be a JSON object",
    );
  }
  return value as Readonly<Record<string, JsonValue>>;
}

export function assertCommandEnvelope(value: unknown): CommandEnvelope {
  if (value === null || Array.isArray(value) || typeof value !== "object") {
    throw new GatewayProtocolError(
      "invalid_command_envelope",
      "Command envelope must be an object",
    );
  }
  const record = value as Record<string, unknown>;
  if (record.schema !== COMMAND_SCHEMA) {
    throw new GatewayProtocolError(
      "invalid_command_schema",
      "Command envelope schema mismatch",
    );
  }
  const strings = [
    "commandId",
    "sessionId",
    "runId",
    "taskId",
    "workerId",
    "toolUseId",
    "executable",
    "cwd",
    "environmentDigest",
    "workspaceId",
    "fenceDigest",
    "networkProfile",
    "provenanceRef",
    "idempotencyKey",
    "causationId",
    "correlationId",
  ] as const;
  for (const field of strings) {
    if (typeof record[field] !== "string") {
      throw new GatewayProtocolError(
        "invalid_command_envelope",
        field + " must be a string",
      );
    }
  }
  if (
    !Array.isArray(record.argv) ||
    !record.argv.every((item) => typeof item === "string")
  ) {
    throw new GatewayProtocolError(
      "invalid_command_envelope",
      "argv must be a string array",
    );
  }
  if (
    !Number.isSafeInteger(record.ownerEpoch) ||
    Number(record.ownerEpoch) < 0
  ) {
    throw new GatewayProtocolError(
      "invalid_command_envelope",
      "ownerEpoch must be a non-negative integer",
    );
  }
  normalizeBudget(record.budget as Partial<CommandBudget>);
  assertJsonRecord(record.metadata, "metadata");
  return value as CommandEnvelope;
}

export function assertApprovalBinding(value: unknown): ApprovalBinding {
  if (value === null || Array.isArray(value) || typeof value !== "object") {
    throw new GatewayProtocolError(
      "invalid_approval_binding",
      "Approval binding must be an object",
    );
  }
  const binding = value as Record<string, unknown>;
  if (binding.schema !== APPROVAL_SCHEMA) {
    throw new GatewayProtocolError(
      "invalid_approval_schema",
      "Approval binding schema mismatch",
    );
  }
  for (const key of [
    "bindingId",
    "sessionId",
    "commandId",
    "toolUseId",
    "requestFingerprint",
    "commandDigest",
    "grantDigest",
    "consumptionId",
  ]) {
    if (typeof binding[key] !== "string") {
      throw new GatewayProtocolError(
        "invalid_approval_binding",
        key + " must be a string",
      );
    }
  }
  if (binding.effect !== Effects.allow && binding.effect !== Effects.ask) {
    throw new GatewayProtocolError(
      "invalid_approval_binding",
      "Approval binding cannot carry deny",
    );
  }
  return value as ApprovalBinding;
}
