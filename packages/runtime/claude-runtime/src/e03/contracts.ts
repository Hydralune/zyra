import { createHash, randomUUID } from "node:crypto";

import type { ArtifactReceipt, JsonObject, JsonValue } from "../contracts.ts";

export type AgentTaskPhase =
  | "created"
  | "queued"
  | "running"
  | "waiting"
  | "completed"
  | "failed"
  | "cancelled"
  | "killed";

export type CommitPhase = "prepare" | "effect" | "receipt" | "commit" | "ack";
export type DeliveryKind =
  | "progress"
  | "partial"
  | "final"
  | "error"
  | "artifact";
export type IsolationMode =
  | "none"
  | "workspace"
  | "worktree"
  | "sandbox"
  | "remote";
export type ControlCommand =
  | "agent.create"
  | "agent.steer"
  | "agent.cancel"
  | "agent.kill"
  | "agent.wait"
  | "agent.result"
  | "agent.status"
  | "agent.resume"
  | "agent.list"
  | "team.send"
  | "team.fanout"
  | "team.collect"
  | "worktree.prepare"
  | "worktree.merge"
  | "worktree.cleanup"
  | "context.compact"
  | "context.clear"
  | "session.model"
  | "permission.inspect"
  | "mcp.inspect"
  | "skills.inspect";

export interface E03Identity {
  runId: string;
  sessionId: string;
  taskId: string;
  parentTaskId: string;
  parentSessionId: string;
  attemptId: string;
  attempt: number;
  leaseId: string;
  lineage: string[];
}

export interface E03Budget {
  maxTurns: number;
  maxToolCalls: number;
  maxInputTokens: number;
  maxOutputTokens: number;
  maxResultChars: number;
  maxWallTimeMs: number;
  maxChildren: number;
  maxDepth: number;
  maxConcurrency: number;
  consumedTurns: number;
  consumedToolCalls: number;
  consumedInputTokens: number;
  consumedOutputTokens: number;
  consumedResultChars: number;
  startedAt: string;
  deadlineAt: string;
}

export interface E03CapabilityScope {
  tools: string[];
  deniedTools: string[];
  skills: string[];
  mcpServers: string[];
  permissionMode: string;
  permissionCeilingDigest: string;
  workspaceRoots: string[];
  isolationModes: IsolationMode[];
  allowBackground: boolean;
  allowTeamMessaging: boolean;
  allowFanout: boolean;
  allowKill: boolean;
  maxDepth: number;
  maxChildren: number;
  digest: string;
}

export interface E03AgentDefinition {
  name: string;
  description: string;
  version: string;
  source: "builtin" | "user" | "project" | "plugin";
  priority: number;
  model: string;
  effort: string;
  systemPrompt: string;
  tools: string[];
  deniedTools: string[];
  skills: string[];
  mcpServers: string[];
  permissionMode: string;
  isolation: IsolationMode;
  background: boolean;
  memoryScope: string;
  budget: E03Budget;
  metadata: JsonObject;
  digest: string;
}

export interface E03ContextSnapshot {
  snapshotId: string;
  parentSnapshotId: string | null;
  sessionId: string;
  taskId: string;
  branchId: string;
  sequence: number;
  messageRefs: string[];
  artifactRefs: string[];
  evidenceRefs: string[];
  memoryRefs: string[];
  compactBoundaryIds: string[];
  permissionDigest: string;
  toolCatalogDigest: string;
  topologyRevision: number;
  createdAt: string;
  checksum: string;
}

export interface E03Message {
  messageId: string;
  taskId: string;
  senderTaskId: string;
  recipientTaskId: string;
  sequence: number;
  kind: "prompt" | "steer" | "cancel" | "kill" | "clarification" | "response";
  body: string;
  idempotencyKey: string;
  createdAt: string;
  deliveredAt: string | null;
  acknowledgedAt: string | null;
  digest: string;
}

export interface E03Delivery {
  deliveryId: string;
  taskId: string;
  sequence: number;
  kind: DeliveryKind;
  summary: string;
  payload: JsonObject;
  artifactIds: string[];
  idempotencyKey: string;
  createdAt: string;
  acknowledgedAt: string | null;
  digest: string;
}

export interface E03IsolationRequest {
  requestId: string;
  taskId: string;
  leaseId: string;
  mode: IsolationMode;
  workspaceRoot: string;
  baseRevision: string;
  branchName: string;
  expectedArtifacts: string[];
  allowDirtyBaseline: boolean;
  allowNestedRepository: boolean;
  idempotencyKey: string;
  preparedAt: string;
  digest: string;
}

export interface E03IsolationReceipt {
  receiptId: string;
  requestId: string;
  taskId: string;
  leaseId: string;
  accepted: boolean;
  workspacePath: string;
  observedBaseRevision: string;
  resultingRevision: string;
  dirtyBaseline: boolean;
  nestedRepository: boolean;
  mergeConflict: boolean;
  cleanupFailed: boolean;
  artifacts: ArtifactReceipt[];
  error: string;
  completedAt: string;
  digest: string;
}

export interface E03EffectRequest {
  effectId: string;
  requestId: string;
  taskId: string;
  leaseId: string;
  expectedRevision: number;
  effectKind: "persist" | "workspace" | "process" | "artifact" | "event";
  operation: string;
  payload: JsonObject;
  idempotencyKey: string;
  preparedAt: string;
  digest: string;
}

export interface E03EffectReceipt {
  receiptId: string;
  effectId: string;
  requestId: string;
  taskId: string;
  leaseId: string;
  expectedRevision: number;
  accepted: boolean;
  replayed: boolean;
  result: JsonObject;
  artifacts: ArtifactReceipt[];
  error: string;
  completedAt: string;
  digest: string;
}

export interface E03Transition {
  transitionId: string;
  taskId: string;
  requestId: string;
  idempotencyKey: string;
  phase: CommitPhase;
  fromRevision: number;
  toRevision: number;
  fromStatus: AgentTaskPhase;
  toStatus: AgentTaskPhase;
  eventType: string;
  effectId: string | null;
  receiptId: string | null;
  writerId: string;
  leaseId: string;
  preparedAt: string;
  committedAt: string | null;
  acknowledgedAt: string | null;
  digest: string;
}

export interface E03TaskState {
  identity: E03Identity;
  definition: E03AgentDefinition;
  scope: E03CapabilityScope;
  context: E03ContextSnapshot;
  status: AgentTaskPhase;
  revision: number;
  sequence: number;
  prompt: string;
  promptDigest: string;
  executionMode: "foreground" | "background";
  isolation: E03IsolationRequest | null;
  isolationReceipt: E03IsolationReceipt | null;
  messages: E03Message[];
  deliveries: E03Delivery[];
  transitions: E03Transition[];
  childTaskIds: string[];
  result: JsonObject | null;
  artifacts: ArtifactReceipt[];
  usage: JsonObject;
  error: string;
  createdAt: string;
  updatedAt: string;
  terminalAt: string | null;
  checksum: string;
}

export interface E03PreparedMutation {
  transition: E03Transition;
  before: E03TaskState | null;
  proposed: E03TaskState;
  effect: E03EffectRequest | null;
  preparedDigest: string;
}

export interface E03CommittedMutation {
  transition: E03Transition;
  state: E03TaskState;
  receipt: E03EffectReceipt | null;
  committedDigest: string;
}

export interface E03ControlEnvelope {
  schema_version: "3.0";
  request_id: string;
  idempotency_key: string;
  run_id: string;
  session_id: string;
  parent_task_id: string;
  expected_revision: number;
  command: ControlCommand;
  body: JsonObject;
  simulate_lost_ack?: boolean;
}

export interface E03ControlResponse {
  ok: boolean;
  request_id: string;
  command: ControlCommand;
  phase: CommitPhase | "rejected";
  revision: number;
  replayed: boolean;
  restored: boolean;
  dispatch_count: number;
  runtime_origin: "typescript.E03AgentControlCoordinator";
  python_logical_owner: false;
  python_fallback_attempted: false;
  commit_protocol: readonly ["prepare", "effect", "receipt", "commit", "ack"];
  state: JsonObject | null;
  result: JsonObject | null;
  error: string;
}

export interface E03RegistrySnapshot {
  schemaVersion: "3.0";
  revision: number;
  tasks: Record<string, E03TaskState>;
  requests: Record<string, E03ControlResponse>;
  effects: Record<string, E03EffectReceipt>;
  definitions: Record<string, E03AgentDefinition[]>;
  writerLeases: Record<string, string>;
  createdAt: string;
  updatedAt: string;
  checksum: string;
}

export interface E03PhysicalPort {
  restore(
    runId: string,
    sessionId: string,
  ): Promise<E03RegistrySnapshot | null>;
  effect(request: E03EffectRequest): Promise<E03EffectReceipt>;
  compareAndSwap(
    expectedRevision: number,
    snapshot: E03RegistrySnapshot,
  ): Promise<{
    accepted: boolean;
    revision: number;
    replayed: boolean;
    error: string;
  }>;
}

export interface E03Clock {
  now(): string;
}

export class SystemE03Clock implements E03Clock {
  now(): string {
    return new Date().toISOString();
  }
}

export class E03RuntimeError extends Error {
  readonly code: string;
  readonly details: JsonObject;

  constructor(code: string, message: string, details: JsonObject = {}) {
    super(message);
    this.name = "E03RuntimeError";
    this.code = code;
    this.details = details;
  }
}

export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const row = value as Record<string, unknown>;
    return `{${Object.keys(row)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(row[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

export function digest(value: unknown): string {
  return createHash("sha256").update(canonicalJson(value)).digest("hex");
}

export function unique(values: readonly string[]): string[] {
  return [
    ...new Set(values.map((value) => value.trim()).filter(Boolean)),
  ].sort();
}

export function requireString(
  value: unknown,
  field: string,
  minimum = 1,
  maximum = 256_000,
): string {
  if (typeof value !== "string")
    throw new E03RuntimeError("invalid_field", `${field} must be a string`, {
      field,
    });
  const normalized = value.trim();
  if (normalized.length < minimum || normalized.length > maximum) {
    throw new E03RuntimeError(
      "invalid_field",
      `${field} length must be between ${minimum} and ${maximum}`,
      { field, minimum, maximum, actual: normalized.length },
    );
  }
  return normalized;
}

export function optionalString(
  value: unknown,
  field: string,
  maximum = 256_000,
): string {
  if (value === undefined || value === null) return "";
  return requireString(value, field, 0, maximum);
}

export function requireInteger(
  value: unknown,
  field: string,
  minimum = 0,
  maximum = Number.MAX_SAFE_INTEGER,
): number {
  if (
    !Number.isSafeInteger(value) ||
    (value as number) < minimum ||
    (value as number) > maximum
  ) {
    throw new E03RuntimeError(
      "invalid_field",
      `${field} must be an integer between ${minimum} and ${maximum}`,
      { field, minimum, maximum },
    );
  }
  return value as number;
}

export function requireBoolean(value: unknown, field: string): boolean {
  if (typeof value !== "boolean")
    throw new E03RuntimeError("invalid_field", `${field} must be a boolean`, {
      field,
    });
  return value;
}

export function requireObject(value: unknown, field: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new E03RuntimeError("invalid_field", `${field} must be an object`, {
      field,
    });
  return value as JsonObject;
}

export function stringArray(
  value: unknown,
  field: string,
  maximum = 10_000,
): string[] {
  if (!Array.isArray(value))
    throw new E03RuntimeError("invalid_field", `${field} must be an array`, {
      field,
    });
  if (value.length > maximum)
    throw new E03RuntimeError(
      "invalid_field",
      `${field} exceeds ${maximum} entries`,
      { field, maximum },
    );
  return unique(
    value.map((item, index) =>
      requireString(item, `${field}[${index}]`, 1, 8_192),
    ),
  );
}

export function assertDigest(
  value: unknown,
  actual: string,
  field: string,
): void {
  const expected = digest(value);
  if (expected !== actual)
    throw new E03RuntimeError(
      "checksum_mismatch",
      `${field} checksum mismatch`,
      { field, expected, actual },
    );
}

export function withoutChecksum<T extends { checksum: string }>(
  value: T,
): Omit<T, "checksum"> {
  const { checksum: _checksum, ...rest } = value;
  return rest;
}

export function withChecksum<T extends JsonObject>(
  value: T,
): T & { checksum: string } {
  return { ...value, checksum: digest(value) };
}

export function cloneJson<T>(value: T): T {
  return structuredClone(value);
}

export function createId(prefix: string): string {
  return `${prefix}-${randomUUID()}`;
}

export function isTerminal(status: AgentTaskPhase): boolean {
  return (
    status === "completed" ||
    status === "failed" ||
    status === "cancelled" ||
    status === "killed"
  );
}

export function assertTaskChecksum(task: E03TaskState): void {
  assertDigest(
    withoutChecksum(task),
    task.checksum,
    `task:${task.identity.taskId}`,
  );
}

export function sealTask(
  task: Omit<E03TaskState, "checksum"> | E03TaskState,
): E03TaskState {
  const { checksum: _checksum, ...value } = task as E03TaskState;
  return { ...value, checksum: digest(value) };
}

export function assertSnapshotChecksum(snapshot: E03RegistrySnapshot): void {
  assertDigest(withoutChecksum(snapshot), snapshot.checksum, "registry");
  for (const task of Object.values(snapshot.tasks)) assertTaskChecksum(task);
}

export function sealSnapshot(
  snapshot: Omit<E03RegistrySnapshot, "checksum"> | E03RegistrySnapshot,
): E03RegistrySnapshot {
  const { checksum: _checksum, ...value } = snapshot as E03RegistrySnapshot;
  return { ...value, checksum: digest(value) };
}

export function emptySnapshot(
  clock: E03Clock = new SystemE03Clock(),
): E03RegistrySnapshot {
  const now = clock.now();
  return sealSnapshot({
    schemaVersion: "3.0",
    revision: 0,
    tasks: {},
    requests: {},
    effects: {},
    definitions: {},
    writerLeases: {},
    createdAt: now,
    updatedAt: now,
  });
}

export function response(
  input: Partial<E03ControlResponse> &
    Pick<E03ControlResponse, "request_id" | "command">,
): E03ControlResponse {
  return {
    ok: input.ok ?? false,
    request_id: input.request_id,
    command: input.command,
    phase: input.phase ?? "rejected",
    revision: input.revision ?? 0,
    replayed: input.replayed ?? false,
    restored: input.restored ?? false,
    dispatch_count: input.dispatch_count ?? 0,
    runtime_origin: "typescript.E03AgentControlCoordinator",
    python_logical_owner: false,
    python_fallback_attempted: false,
    commit_protocol: ["prepare", "effect", "receipt", "commit", "ack"],
    state: input.state ?? null,
    result: input.result ?? null,
    error: input.error ?? "",
  };
}

export function jsonObject(value: unknown): JsonObject {
  return requireObject(value, "value");
}

export function jsonValue(value: unknown): JsonValue {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  )
    return value;
  if (Array.isArray(value)) return value.map(jsonValue);
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, jsonValue(item)]),
    );
  throw new E03RuntimeError("invalid_json", "value is not JSON serializable");
}

export const DEFAULT_E03_BUDGET: Readonly<E03Budget> = Object.freeze({
  maxTurns: 12,
  maxToolCalls: 48,
  maxInputTokens: 64_000,
  maxOutputTokens: 16_000,
  maxResultChars: 120_000,
  maxWallTimeMs: 900_000,
  maxChildren: 4,
  maxDepth: 3,
  maxConcurrency: 4,
  consumedTurns: 0,
  consumedToolCalls: 0,
  consumedInputTokens: 0,
  consumedOutputTokens: 0,
  consumedResultChars: 0,
  startedAt: "",
  deadlineAt: "",
});

export const COMMIT_PROTOCOL: readonly [
  "prepare",
  "effect",
  "receipt",
  "commit",
  "ack",
] = Object.freeze(["prepare", "effect", "receipt", "commit", "ack"]);
