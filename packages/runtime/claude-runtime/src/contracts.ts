import { randomUUID } from "node:crypto";

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];
export type JsonObject = { [key: string]: JsonValue };

export interface ToolSpecContract {
  name: string;
  purpose: string;
  source: string;
  input_schema: JsonObject;
  output_schema: JsonObject;
  metadata: Record<string, string>;
  execution_provenance?: JsonObject | null;
}

export interface ToolStep {
  tool_name: string;
  arguments: JsonObject;
  step_id?: string;
  prompt?: string;
  metadata?: JsonObject;
}

export interface ToolBatch {
  batchId: string;
  turnIndex: number;
  executionMode: "concurrent_read_only" | "serial_non_read_only";
  steps: ToolStep[];
}

export interface ToolExecutionRequest {
  toolCallId: string;
  toolName: string;
  arguments: JsonObject;
  turnIndex: number;
  stepIndex: number;
  batchId: string;
  batchIndex: number;
  batchSize: number;
  executionMode: ToolBatch["executionMode"];
  metadata: JsonObject;
  permissionDecision?: JsonObject;
  executionOwner?: string;
  permissionOnly?: boolean;
}

export interface ArtifactReceipt {
  artifact_id: string;
  kind: string;
  uri: string;
  title: string;
  producer_node_id?: string | null;
  created_at?: string;
  metadata?: JsonObject;
}

export interface ToolExecutionResponse {
  tool_call_id: string;
  ok: boolean;
  summary: string;
  output: JsonObject;
  artifacts: ArtifactReceipt[];
  error?: string | null;
  completed_at?: string;
  metadata: Record<string, string>;
}

export interface CapabilitySettlement {
  toolCallId: string;
  toolName: string;
  ok: boolean;
  error: string;
  metadata: JsonObject;
}

export interface ArtifactRequest {
  requestId: string;
  title: string;
  kind: string;
  extension: string;
  content: string;
  metadata: JsonObject;
}

export interface RuntimeEvent {
  phase: string;
  sequence: number;
  canonical_owner: "typescript";
  runtime_id: "zyra-typescript-claude-runtime";
  [key: string]: JsonValue;
}

export interface RuntimeHost {
  emitEvent(event: RuntimeEvent): Promise<void>;
  executeBatch(batch: ToolBatch, requests: ToolExecutionRequest[]): Promise<ToolExecutionResponse[]>;
  externalize(request: ArtifactRequest): Promise<ArtifactReceipt>;
  settleCapability?(settlement: CapabilitySettlement): Promise<void>;
  isAborted(): boolean;
}

export interface RuntimeConfig {
  maxTurns: number | null;
  maxToolResultChars: number;
  maxTurnToolResultChars: number | null;
  maxQueryContextChars: number;
  continueOnError: boolean;
  maxReadOnlyConcurrency: number;
  emitToolUseSummaries: boolean;
  allowEmptyTurns: boolean;
  modelName: string;
  runtimeConstraints: JsonObject;
  controlCommands: JsonValue[];
  permissionPolicy: JsonObject;
}

export interface RuntimeRunInput {
  runId: string;
  taskId: string;
  nodeId?: string | null;
  workerRequestId: string;
  sessionId: string;
  messages: JsonObject[];
  turns: JsonValue[];
  tools: ToolSpecContract[];
  config: Partial<RuntimeConfig>;
  sessionSeed?: JsonObject | null;
  contextSnapshot?: JsonObject | null;
  restoredState?: JsonObject | null;
  metadata?: JsonObject;
}

export interface RuntimeRunResult {
  ok: boolean;
  stoppedReason: string | null;
  turnCount: number;
  toolCallCount: number;
  contextCompactionCount: number;
  stepSummaries: string[];
  artifacts: ArtifactReceipt[];
  sessionSnapshot: JsonObject;
  metadata: Record<string, string>;
}

export function asObject(value: unknown): JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return {};
  }
  return value as JsonObject;
}

export function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export function asBoolean(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

export function positiveInteger(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : fallback;
}

export function cloneJson<T extends JsonValue>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

export function runtimeId(prefix: string): string {
  return prefix + "_" + randomUUID().replaceAll("-", "");
}

export function jsonChars(value: unknown): number {
  return JSON.stringify(value).length;
}

export function uniqueArtifacts(values: ArtifactReceipt[]): ArtifactReceipt[] {
  const seen = new Set<string>();
  const result: ArtifactReceipt[] = [];
  for (const value of values) {
    if (!value.artifact_id || seen.has(value.artifact_id)) {
      continue;
    }
    seen.add(value.artifact_id);
    result.push(value);
  }
  return result;
}
