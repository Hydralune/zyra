import type { JsonObject, JsonRpcMessage } from "../contracts.ts";
import { cloneJson, deterministicMcpId, monotonicNow, sha256 } from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";
import { McpProtocolCodec, type McpTaskMetadata, type McpTaskResult } from "../core/protocol.ts";
import type { McpTransportAdapter } from "../connection/contracts.ts";

export interface McpTaskOwner {
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  requestId: string;
  sessionId: string;
  taskId: string;
  remoteTaskId: string;
  status: McpTaskMetadata["status"];
  statusMessage: string | null;
  result: McpTaskResult | null;
  resultDigest: string | null;
  pollCount: number;
  nextPollAt: string | null;
  createdAt: string;
  updatedAt: string;
  completedAt: string | null;
  cancelledAt: string | null;
  metadata: JsonObject;
}

export interface McpTaskSnapshot {
  version: "zyra.mcp-task-runtime/v1";
  revision: number;
  tasks: McpTaskOwner[];
  digest: string;
  capturedAt: string;
}

export class McpTaskRuntime {
  private readonly tasks = new Map<string, McpTaskOwner>();
  private readonly codec = new McpProtocolCodec();
  private readonly now: () => Date;
  private readonly maximumPolls: number;
  private readonly minimumPollMs: number;
  private readonly maximumPollMs: number;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumPolls?: number; minimumPollMs?: number; maximumPollMs?: number; snapshot?: McpTaskSnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumPolls = options.maximumPolls ?? 10_000;
    this.minimumPollMs = options.minimumPollMs ?? 100;
    this.maximumPollMs = options.maximumPollMs ?? 60_000;
    if (options.snapshot) this.restore(options.snapshot);
  }

  register(input: {
    serverId: string;
    connectionId: string;
    connectionEpoch: number;
    requestId: string;
    sessionId: string;
    taskId: string;
    task: McpTaskMetadata;
    metadata?: JsonObject;
  }): McpTaskOwner {
    const ownerId = ownerKey(input.serverId, input.task.taskId);
    const existing = this.tasks.get(ownerId);
    if (existing) {
      if (existing.connectionId !== input.connectionId || existing.connectionEpoch !== input.connectionEpoch) throw taskError(input.serverId, "task_connection_conflict", `task ${input.task.taskId} belongs to another connection`);
      return cloneJson(existing);
    }
    const timestamp = this.timestamp();
    const owner: McpTaskOwner = {
      serverId: input.serverId,
      connectionId: input.connectionId,
      connectionEpoch: input.connectionEpoch,
      requestId: input.requestId,
      sessionId: input.sessionId,
      taskId: input.taskId,
      remoteTaskId: input.task.taskId,
      status: input.task.status,
      statusMessage: input.task.statusMessage,
      result: null,
      resultDigest: null,
      pollCount: 0,
      nextPollAt: nextPoll(input.task, timestamp, this.minimumPollMs, this.maximumPollMs),
      createdAt: input.task.createdAt ?? timestamp,
      updatedAt: input.task.lastUpdatedAt ?? timestamp,
      completedAt: terminal(input.task.status) ? timestamp : null,
      cancelledAt: input.task.status === "cancelled" ? timestamp : null,
      metadata: cloneJson(input.metadata ?? {}),
    };
    this.tasks.set(ownerId, owner);
    this.revision += 1;
    return cloneJson(owner);
  }

  async poll(
    serverId: string,
    remoteTaskId: string,
    transport: McpTransportAdapter,
    signal?: AbortSignal,
  ): Promise<McpTaskOwner> {
    const owner = this.require(serverId, remoteTaskId);
    if (terminal(owner.status)) return cloneJson(owner);
    if (owner.pollCount >= this.maximumPolls) throw taskError(serverId, "task_poll_limit_exceeded", `task ${remoteTaskId} exceeded ${this.maximumPolls} polls`);
    if (owner.nextPollAt && Date.parse(owner.nextPollAt) > this.now().getTime()) throw taskError(serverId, "task_poll_too_early", `task ${remoteTaskId} may be polled after ${owner.nextPollAt}`);
    const requestId = deterministicMcpId("mcp-task-poll", {
      server_id: serverId,
      remote_task_id: remoteTaskId,
      connection_epoch: owner.connectionEpoch,
      poll: owner.pollCount + 1,
    });
    const message = this.codec.request(requestId, "tasks/get", { taskId: remoteTaskId });
    const response = await transport.request({
      requestId,
      method: "tasks/get",
      message,
      timeoutMs: 30_000,
      idempotent: true,
      idempotencyKey: requestId,
      authorization: null,
      headers: {},
      signal,
      metadata: { task_id: owner.taskId, session_id: owner.sessionId },
    });
    const result = responseResult(response.message, serverId, "tasks/get");
    const task = this.codec.parseTask(result);
    if (task.taskId !== remoteTaskId) throw taskError(serverId, "task_response_id_mismatch", `task response ${task.taskId} does not match ${remoteTaskId}`);
    owner.status = task.status;
    owner.statusMessage = task.statusMessage;
    owner.pollCount += 1;
    owner.updatedAt = task.lastUpdatedAt ?? this.timestamp();
    owner.nextPollAt = terminal(task.status) ? null : nextPoll(task, owner.updatedAt, this.minimumPollMs, this.maximumPollMs);
    owner.completedAt = terminal(task.status) ? owner.updatedAt : null;
    if (task.status === "cancelled") owner.cancelledAt = owner.updatedAt;
    this.revision += 1;
    return cloneJson(owner);
  }

  async result(
    serverId: string,
    remoteTaskId: string,
    transport: McpTransportAdapter,
    signal?: AbortSignal,
  ): Promise<McpTaskResult> {
    const owner = this.require(serverId, remoteTaskId);
    if (owner.result) return cloneJson(owner.result);
    if (!terminal(owner.status)) throw taskError(serverId, "task_not_terminal", `task ${remoteTaskId} is ${owner.status}`);
    const requestId = deterministicMcpId("mcp-task-result", {
      server_id: serverId,
      remote_task_id: remoteTaskId,
      connection_epoch: owner.connectionEpoch,
    });
    const response = await transport.request({
      requestId,
      method: "tasks/result",
      message: this.codec.request(requestId, "tasks/result", { taskId: remoteTaskId }),
      timeoutMs: 60_000,
      idempotent: true,
      idempotencyKey: requestId,
      authorization: null,
      headers: {},
      signal,
      metadata: { task_id: owner.taskId, session_id: owner.sessionId },
    });
    const value = responseResult(response.message, serverId, "tasks/result");
    const object = value !== null && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : { result: value };
    const task = this.codec.parseTask(object.task ?? {
      taskId: remoteTaskId,
      status: owner.status,
      statusMessage: owner.statusMessage,
      createdAt: owner.createdAt,
      lastUpdatedAt: owner.updatedAt,
    });
    const result: McpTaskResult = {
      task,
      result: object.result ?? null,
      meta: object._meta && typeof object._meta === "object" && !Array.isArray(object._meta) ? cloneJson(object._meta as JsonObject) : {},
    };
    owner.result = result;
    owner.resultDigest = sha256(result);
    owner.updatedAt = this.timestamp();
    owner.completedAt ??= owner.updatedAt;
    this.revision += 1;
    return cloneJson(result);
  }

  async cancel(
    serverId: string,
    remoteTaskId: string,
    transport: McpTransportAdapter,
    reason = "cancelled_by_client",
    signal?: AbortSignal,
  ): Promise<McpTaskOwner> {
    const owner = this.require(serverId, remoteTaskId);
    if (terminal(owner.status)) return cloneJson(owner);
    const requestId = deterministicMcpId("mcp-task-cancel", {
      server_id: serverId,
      remote_task_id: remoteTaskId,
      connection_epoch: owner.connectionEpoch,
      reason,
    });
    const response = await transport.request({
      requestId,
      method: "tasks/cancel",
      message: this.codec.request(requestId, "tasks/cancel", { taskId: remoteTaskId, reason }),
      timeoutMs: 30_000,
      idempotent: true,
      idempotencyKey: requestId,
      authorization: null,
      headers: {},
      signal,
      metadata: { task_id: owner.taskId, session_id: owner.sessionId },
    });
    const value = responseResult(response.message, serverId, "tasks/cancel");
    const task = this.codec.parseTask(value);
    if (task.taskId !== remoteTaskId) throw taskError(serverId, "task_response_id_mismatch", `task response ${task.taskId} does not match ${remoteTaskId}`);
    owner.status = task.status;
    owner.statusMessage = task.statusMessage;
    owner.updatedAt = task.lastUpdatedAt ?? this.timestamp();
    owner.cancelledAt = task.status === "cancelled" ? owner.updatedAt : null;
    owner.completedAt = terminal(task.status) ? owner.updatedAt : null;
    owner.nextPollAt = terminal(task.status) ? null : nextPoll(task, owner.updatedAt, this.minimumPollMs, this.maximumPollMs);
    this.revision += 1;
    return cloneJson(owner);
  }

  get(serverId: string, remoteTaskId: string): McpTaskOwner | null {
    const value = this.tasks.get(ownerKey(serverId, remoteTaskId));
    return value ? cloneJson(value) : null;
  }

  list(options: { serverId?: string; sessionId?: string; status?: McpTaskMetadata["status"] } = {}): McpTaskOwner[] {
    return [...this.tasks.values()]
      .filter((task) => !options.serverId || task.serverId === options.serverId)
      .filter((task) => !options.sessionId || task.sessionId === options.sessionId)
      .filter((task) => !options.status || task.status === options.status)
      .sort((left, right) => left.createdAt.localeCompare(right.createdAt))
      .map(cloneJson);
  }

  snapshot(): McpTaskSnapshot {
    const withoutDigest = {
      version: "zyra.mcp-task-runtime/v1" as const,
      revision: this.revision,
      tasks: this.list(),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: sha256(withoutDigest) };
  }

  restore(snapshot: McpTaskSnapshot): void {
    if (snapshot.version !== "zyra.mcp-task-runtime/v1") throw taskError("", "unsupported_task_snapshot", "unsupported task snapshot version");
    const { digest, ...withoutDigest } = snapshot;
    if (sha256(withoutDigest) !== digest) throw taskError("", "task_snapshot_digest_mismatch", "task snapshot digest mismatch");
    this.tasks.clear();
    this.revision = snapshot.revision;
    for (const task of snapshot.tasks) {
      const restored = cloneJson(task);
      if (!terminal(restored.status)) restored.metadata = { ...restored.metadata, restore_requires_poll: true };
      this.tasks.set(ownerKey(restored.serverId, restored.remoteTaskId), restored);
    }
  }

  private require(serverId: string, remoteTaskId: string): McpTaskOwner {
    const owner = this.tasks.get(ownerKey(serverId, remoteTaskId));
    if (!owner) throw taskError(serverId, "task_not_found", `MCP task ${remoteTaskId} was not found`);
    return owner;
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function ownerKey(serverId: string, remoteTaskId: string): string {
  return `${serverId}\0${remoteTaskId}`;
}

function terminal(status: McpTaskMetadata["status"]): boolean {
  return status === "completed" || status === "failed" || status === "cancelled";
}

function nextPoll(task: McpTaskMetadata, from: string, minimum: number, maximum: number): string {
  const interval = Math.max(minimum, Math.min(task.pollInterval ?? 1_000, maximum));
  return new Date(Date.parse(from) + interval).toISOString();
}

function responseResult(message: JsonRpcMessage, serverId: string, operation: string) {
  if ("result" in message) return message.result ?? null;
  const error = "error" in message ? message.error : null;
  throw taskError(serverId, "task_request_failed", error?.message ?? `${operation} returned no result`);
}

function taskError(serverId: string, code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-task", { server_id: serverId, code, message }),
    category: code.includes("conflict") || code.includes("early") ? "conflict" : "capability",
    code,
    message,
    serverId,
    retryable: code.includes("early"),
    disposition: code.includes("early") ? "retry_same_connection" : "terminal",
  });
}
