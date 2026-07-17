import type { ChildProcessWithoutNullStreams } from "node:child_process";

import type { JsonObject, JsonRpcMessage, JsonValue } from "../contracts.ts";
import type { McpFailureRecord } from "../core/failure.ts";

export type McpConnectionPhase =
  | "idle"
  | "connecting"
  | "authenticating"
  | "initializing"
  | "ready"
  | "degraded"
  | "reconnecting"
  | "closing"
  | "closed"
  | "failed";

export type McpTransportEventKind =
  | "starting"
  | "started"
  | "message_sent"
  | "message_received"
  | "stderr"
  | "http_response"
  | "sse_opened"
  | "sse_event"
  | "sse_closed"
  | "retry_scheduled"
  | "process_exit"
  | "transport_error"
  | "closing"
  | "closed";

export interface McpTransportEvent {
  eventId: string;
  serverId: string;
  transportId: string;
  connectionEpoch: number;
  sequence: number;
  kind: McpTransportEventKind;
  requestId: string | null;
  messageDigest: string | null;
  details: JsonObject;
  occurredAt: string;
}

export interface McpTransportRequest {
  requestId: string;
  method: string;
  message: JsonRpcMessage;
  timeoutMs: number;
  idempotent: boolean;
  idempotencyKey: string | null;
  authorization: string | null;
  headers: Record<string, string>;
  signal?: AbortSignal;
  metadata: JsonObject;
}

export interface McpTransportResponse {
  requestId: string;
  message: JsonRpcMessage;
  transportId: string;
  connectionEpoch: number;
  elapsedMs: number;
  replayed: boolean;
  headers: Record<string, string>;
  metadata: JsonObject;
}

export interface McpTransportHealth {
  transportId: string;
  serverId: string;
  phase: McpConnectionPhase;
  connectionEpoch: number;
  connected: boolean;
  startedAt: string | null;
  lastActivityAt: string | null;
  lastMessageAt: string | null;
  pendingRequests: number;
  completedRequests: number;
  failedRequests: number;
  reconnectCount: number;
  bytesSent: number;
  bytesReceived: number;
  processId: number | null;
  endpoint: string | null;
  lastFailure: McpFailureRecord | null;
}

export interface McpTransportSnapshot {
  version: "zyra.mcp-transport/v1";
  transportId: string;
  serverId: string;
  phase: McpConnectionPhase;
  connectionEpoch: number;
  sequence: number;
  health: McpTransportHealth;
  events: McpTransportEvent[];
  pending: McpPendingTransportRequest[];
  completed: McpCompletedTransportRequest[];
  idempotencyReceipts: McpIdempotencyReceipt[];
  metadata: JsonObject;
}

export interface McpPendingTransportRequest {
  requestId: string;
  method: string;
  messageDigest: string;
  idempotent: boolean;
  idempotencyKey: string | null;
  connectionEpoch: number;
  preparedAt: string;
  sentAt: string | null;
  deadlineAt: string;
  attempt: number;
  metadata: JsonObject;
}

export interface McpCompletedTransportRequest {
  requestId: string;
  method: string;
  messageDigest: string;
  responseDigest: string;
  idempotencyKey: string | null;
  connectionEpoch: number;
  attempt: number;
  status: "committed" | "failed" | "cancelled";
  preparedAt: string;
  sentAt: string | null;
  completedAt: string;
  failure: McpFailureRecord | null;
  response: JsonRpcMessage | null;
  metadata: JsonObject;
}

export interface McpIdempotencyReceipt {
  idempotencyKey: string;
  requestId: string;
  requestDigest: string;
  responseDigest: string;
  response: JsonRpcMessage;
  connectionEpoch: number;
  committedAt: string;
}

export interface McpTransportAdapter {
  readonly serverId: string;
  readonly transportId: string;
  start(signal?: AbortSignal): Promise<McpTransportHealth>;
  request(request: McpTransportRequest): Promise<McpTransportResponse>;
  notify(message: JsonRpcMessage, signal?: AbortSignal): Promise<void>;
  close(reason?: string): Promise<void>;
  health(): McpTransportHealth;
  snapshot(): McpTransportSnapshot;
  onMessage(listener: (message: JsonRpcMessage) => void | Promise<void>): () => void;
  onEvent(listener: (event: McpTransportEvent) => void | Promise<void>): () => void;
}

export interface McpProcessFactoryInput {
  command: string;
  arguments: string[];
  cwd: string | null;
  environment: NodeJS.ProcessEnv;
  signal?: AbortSignal;
}

export type McpProcessFactory = (input: McpProcessFactoryInput) => ChildProcessWithoutNullStreams;

export interface McpFetchInput {
  url: string;
  init: RequestInit;
}

export type McpFetch = (input: McpFetchInput) => Promise<Response>;

export interface McpSseEvent {
  eventId: string | null;
  event: string;
  data: string;
  retryMs: number | null;
  comments: string[];
  receivedAt: string;
  rawBytes: number;
}

export interface McpSseParserSnapshot {
  version: "zyra.mcp-sse-parser/v1";
  buffer: string;
  eventName: string;
  dataLines: string[];
  eventId: string | null;
  retryMs: number | null;
  comments: string[];
  totalBytes: number;
  emittedEvents: number;
  lastEventId: string | null;
}

export interface McpConnectionRecord {
  serverId: string;
  connectionId: string;
  phase: McpConnectionPhase;
  epoch: number;
  configDigest: string;
  policyDigest: string;
  protocolVersion: string | null;
  initialized: boolean;
  connectedAt: string | null;
  readyAt: string | null;
  lastActivityAt: string | null;
  lastFailure: McpFailureRecord | null;
  reconnectAttempt: number;
  reconnectNotBefore: string | null;
  catalogRevision: number;
  authRevision: number;
  metadata: JsonObject;
}

export interface McpConnectionTransition {
  transitionId: string;
  serverId: string;
  connectionId: string;
  sequence: number;
  fromPhase: McpConnectionPhase;
  toPhase: McpConnectionPhase;
  reason: string;
  epoch: number;
  requestId: string | null;
  failure: McpFailureRecord | null;
  metadata: JsonObject;
  occurredAt: string;
  previousHash: string;
  transitionHash: string;
}

export interface McpConnectionSnapshot {
  version: "zyra.mcp-connection-runtime/v1";
  revision: number;
  sequence: number;
  digest: string;
  connections: McpConnectionRecord[];
  transitions: McpConnectionTransition[];
  transports: Record<string, McpTransportSnapshot>;
  metadata: JsonObject;
}

export interface McpConnectionResult {
  serverId: string;
  connectionId: string;
  phase: McpConnectionPhase;
  epoch: number;
  initialized: boolean;
  reconnected: boolean;
  catalogRevision: number;
  transitionId: string;
  metadata: JsonObject;
}

export function jsonValueRecord(value: JsonValue): JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value : { value };
}
