import type { JsonObject, JsonRpcMessage } from "../contracts.ts";
import type { McpTransportAdapter } from "../connection/contracts.ts";
import { cloneJson, deterministicMcpId, monotonicNow, sha256 } from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";
import { McpProtocolCodec } from "../core/protocol.ts";

export type McpSubscriptionStatus = "subscribing" | "active" | "unsubscribing" | "closed" | "failed";

export interface McpResourceSubscription {
  subscriptionId: string;
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  sessionId: string;
  taskId: string;
  uri: string;
  status: McpSubscriptionStatus;
  revision: number;
  contentRevision: number;
  lastContentDigest: string | null;
  lastNotificationDigest: string | null;
  subscribedAt: string | null;
  updatedAt: string;
  closedAt: string | null;
  failure: JsonObject | null;
  metadata: JsonObject;
}

export interface McpResourceUpdate {
  updateId: string;
  subscriptionId: string;
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  uri: string;
  contentRevision: number;
  notificationDigest: string;
  contentDigest: string | null;
  content: JsonObject | null;
  occurredAt: string;
  metadata: JsonObject;
}

export interface McpSubscriptionSnapshot {
  version: "zyra.mcp-subscription-runtime/v1";
  revision: number;
  subscriptions: McpResourceSubscription[];
  updates: McpResourceUpdate[];
  digest: string;
  capturedAt: string;
}

export class McpSubscriptionRuntime {
  private readonly codec = new McpProtocolCodec();
  private readonly subscriptions = new Map<string, McpResourceSubscription>();
  private readonly byResource = new Map<string, string>();
  private readonly updates: McpResourceUpdate[] = [];
  private readonly listeners = new Set<(update: McpResourceUpdate) => void | Promise<void>>();
  private readonly now: () => Date;
  private readonly maximumUpdates: number;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumUpdates?: number; snapshot?: McpSubscriptionSnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumUpdates = options.maximumUpdates ?? 20_000;
    if (options.snapshot) this.restore(options.snapshot);
  }

  async subscribe(input: {
    serverId: string;
    connectionId: string;
    connectionEpoch: number;
    sessionId: string;
    taskId: string;
    uri: string;
    transport: McpTransportAdapter;
    signal?: AbortSignal;
    metadata?: JsonObject;
  }): Promise<McpResourceSubscription> {
    validateBinding(input);
    const key = resourceKey(input.serverId, input.sessionId, input.uri);
    const existingId = this.byResource.get(key);
    if (existingId) {
      const existing = this.subscriptions.get(existingId)!;
      if (existing.connectionId === input.connectionId && existing.connectionEpoch === input.connectionEpoch && existing.status === "active") {
        return cloneJson(existing);
      }
      if (existing.status === "subscribing" || existing.status === "unsubscribing") {
        throw subscriptionError(input.serverId, "subscription_transition_inflight", `resource ${input.uri} has an in-flight subscription transition`);
      }
    }
    const subscriptionId = deterministicMcpId("mcp-resource-subscription", {
      server_id: input.serverId,
      connection_id: input.connectionId,
      connection_epoch: input.connectionEpoch,
      session_id: input.sessionId,
      uri: input.uri,
    }, 40);
    const timestamp = this.timestamp();
    const record: McpResourceSubscription = {
      subscriptionId,
      serverId: input.serverId,
      connectionId: input.connectionId,
      connectionEpoch: input.connectionEpoch,
      sessionId: input.sessionId,
      taskId: input.taskId,
      uri: input.uri,
      status: "subscribing",
      revision: 1,
      contentRevision: 0,
      lastContentDigest: null,
      lastNotificationDigest: null,
      subscribedAt: null,
      updatedAt: timestamp,
      closedAt: null,
      failure: null,
      metadata: cloneJson(input.metadata ?? {}),
    };
    this.subscriptions.set(subscriptionId, record);
    this.byResource.set(key, subscriptionId);
    this.revision += 1;
    try {
      const requestId = deterministicMcpId("mcp-resource-subscribe-request", { subscription_id: subscriptionId });
      const response = await input.transport.request({
        requestId,
        method: "resources/subscribe",
        message: this.codec.request(requestId, "resources/subscribe", { uri: input.uri }),
        timeoutMs: 30_000,
        idempotent: true,
        idempotencyKey: requestId,
        authorization: null,
        headers: {},
        signal: input.signal,
        metadata: { subscription_id: subscriptionId },
      });
      requireSuccess(response.message, input.serverId, "resources/subscribe");
      record.status = "active";
      record.subscribedAt = this.timestamp();
      record.updatedAt = record.subscribedAt;
      record.revision += 1;
      record.failure = null;
      this.revision += 1;
      return cloneJson(record);
    } catch (error) {
      record.status = "failed";
      record.updatedAt = this.timestamp();
      record.revision += 1;
      record.failure = failureObject(error, "resource_subscribe_failed");
      this.revision += 1;
      throw error;
    }
  }

  async unsubscribe(
    subscriptionId: string,
    transport: McpTransportAdapter,
    reason = "requested",
    signal?: AbortSignal,
  ): Promise<McpResourceSubscription> {
    const record = this.require(subscriptionId);
    if (record.status === "closed") return cloneJson(record);
    if (record.status !== "active" && record.status !== "failed") {
      throw subscriptionError(record.serverId, "subscription_not_active", `subscription ${subscriptionId} is ${record.status}`);
    }
    record.status = "unsubscribing";
    record.updatedAt = this.timestamp();
    record.revision += 1;
    this.revision += 1;
    try {
      const requestId = deterministicMcpId("mcp-resource-unsubscribe-request", {
        subscription_id: subscriptionId,
        content_revision: record.contentRevision,
      });
      const response = await transport.request({
        requestId,
        method: "resources/unsubscribe",
        message: this.codec.request(requestId, "resources/unsubscribe", { uri: record.uri }),
        timeoutMs: 30_000,
        idempotent: true,
        idempotencyKey: requestId,
        authorization: null,
        headers: {},
        signal,
        metadata: { subscription_id: subscriptionId, reason },
      });
      requireSuccess(response.message, record.serverId, "resources/unsubscribe");
      record.status = "closed";
      record.closedAt = this.timestamp();
      record.updatedAt = record.closedAt;
      record.revision += 1;
      record.failure = null;
      this.byResource.delete(resourceKey(record.serverId, record.sessionId, record.uri));
      this.revision += 1;
      return cloneJson(record);
    } catch (error) {
      record.status = "failed";
      record.updatedAt = this.timestamp();
      record.revision += 1;
      record.failure = failureObject(error, "resource_unsubscribe_failed");
      this.revision += 1;
      throw error;
    }
  }

  async applyNotification(input: {
    serverId: string;
    connectionId: string;
    connectionEpoch: number;
    params: JsonObject;
    read?: (uri: string) => Promise<JsonObject>;
    metadata?: JsonObject;
  }): Promise<McpResourceUpdate[]> {
    const uri = typeof input.params.uri === "string" ? input.params.uri : "";
    if (!uri) throw subscriptionError(input.serverId, "resource_update_uri_missing", "resource update notification lacks uri");
    const notificationDigest = sha256(input.params);
    const matching = [...this.subscriptions.values()].filter((record) =>
      record.serverId === input.serverId
      && record.uri === uri
      && record.status === "active"
    );
    const output: McpResourceUpdate[] = [];
    for (const record of matching) {
      if (record.connectionId !== input.connectionId || record.connectionEpoch !== input.connectionEpoch) continue;
      if (record.lastNotificationDigest === notificationDigest) continue;
      let content: JsonObject | null = null;
      let contentDigest: string | null = null;
      if (input.read) {
        content = cloneJson(await input.read(uri));
        contentDigest = sha256(content);
      }
      record.contentRevision += 1;
      record.revision += 1;
      record.lastNotificationDigest = notificationDigest;
      record.lastContentDigest = contentDigest;
      record.updatedAt = this.timestamp();
      const base = {
        subscriptionId: record.subscriptionId,
        serverId: record.serverId,
        connectionId: record.connectionId,
        connectionEpoch: record.connectionEpoch,
        uri,
        contentRevision: record.contentRevision,
        notificationDigest,
        contentDigest,
        content,
        occurredAt: record.updatedAt,
        metadata: cloneJson(input.metadata ?? {}),
      };
      const update: McpResourceUpdate = {
        updateId: deterministicMcpId("mcp-resource-update", base, 40),
        ...base,
      };
      this.updates.push(update);
      output.push(cloneJson(update));
      this.revision += 1;
      for (const listener of this.listeners) await listener(cloneJson(update));
    }
    while (this.updates.length > this.maximumUpdates) this.updates.shift();
    return output;
  }

  connectionLost(serverId: string, connectionId: string, connectionEpoch: number, reason: string): McpResourceSubscription[] {
    const changed: McpResourceSubscription[] = [];
    for (const record of this.subscriptions.values()) {
      if (record.serverId !== serverId || record.connectionId !== connectionId || record.connectionEpoch !== connectionEpoch) continue;
      if (record.status !== "active" && record.status !== "subscribing") continue;
      record.status = "failed";
      record.revision += 1;
      record.updatedAt = this.timestamp();
      record.failure = { code: "subscription_connection_lost", reason, reconnect_safe: true };
      changed.push(cloneJson(record));
      this.revision += 1;
    }
    return changed;
  }

  onUpdate(listener: (update: McpResourceUpdate) => void | Promise<void>): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  get(subscriptionId: string): McpResourceSubscription | null {
    const value = this.subscriptions.get(subscriptionId);
    return value ? cloneJson(value) : null;
  }

  list(options: { serverId?: string; sessionId?: string; status?: McpSubscriptionStatus } = {}): McpResourceSubscription[] {
    return [...this.subscriptions.values()]
      .filter((record) => !options.serverId || record.serverId === options.serverId)
      .filter((record) => !options.sessionId || record.sessionId === options.sessionId)
      .filter((record) => !options.status || record.status === options.status)
      .sort((left, right) => left.subscriptionId.localeCompare(right.subscriptionId))
      .map(cloneJson);
  }

  history(serverId?: string): McpResourceUpdate[] {
    return this.updates.filter((update) => !serverId || update.serverId === serverId).map(cloneJson);
  }

  snapshot(): McpSubscriptionSnapshot {
    const withoutDigest = {
      version: "zyra.mcp-subscription-runtime/v1" as const,
      revision: this.revision,
      subscriptions: this.list(),
      updates: this.history(),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: sha256(withoutDigest) };
  }

  restore(snapshot: McpSubscriptionSnapshot): void {
    if (snapshot.version !== "zyra.mcp-subscription-runtime/v1") throw subscriptionError("", "unsupported_subscription_snapshot", "unsupported subscription snapshot version");
    const { digest, ...withoutDigest } = snapshot;
    if (sha256(withoutDigest) !== digest) throw subscriptionError("", "subscription_snapshot_digest_mismatch", "subscription snapshot digest mismatch");
    this.subscriptions.clear();
    this.byResource.clear();
    this.updates.splice(0);
    this.revision = snapshot.revision;
    for (const value of snapshot.subscriptions) {
      const record = cloneJson(value);
      if (record.status === "subscribing" || record.status === "unsubscribing") {
        record.status = "failed";
        record.revision += 1;
        record.failure = { code: "subscription_restart_interrupted", prior_status: value.status };
      } else if (record.status === "active") {
        record.status = "failed";
        record.revision += 1;
        record.failure = { code: "subscription_restore_requires_resubscribe", prior_epoch: record.connectionEpoch };
      }
      this.subscriptions.set(record.subscriptionId, record);
      if (record.status !== "closed") this.byResource.set(resourceKey(record.serverId, record.sessionId, record.uri), record.subscriptionId);
    }
    for (const update of snapshot.updates.slice(-this.maximumUpdates)) this.updates.push(cloneJson(update));
  }

  private require(subscriptionId: string): McpResourceSubscription {
    const value = this.subscriptions.get(subscriptionId);
    if (!value) throw subscriptionError("", "subscription_not_found", `subscription ${subscriptionId} was not found`);
    return value;
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function validateBinding(input: { serverId: string; connectionId: string; connectionEpoch: number; sessionId: string; taskId: string; uri: string }): void {
  if (!input.serverId || !input.connectionId || !input.sessionId || !input.taskId || !input.uri) throw subscriptionError(input.serverId, "subscription_binding_incomplete", "resource subscription binding is incomplete");
  if (!Number.isSafeInteger(input.connectionEpoch) || input.connectionEpoch < 0) throw subscriptionError(input.serverId, "subscription_epoch_invalid", "resource subscription connection epoch is invalid");
}

function resourceKey(serverId: string, sessionId: string, uri: string): string {
  return `${serverId}\0${sessionId}\0${uri}`;
}

function requireSuccess(message: JsonRpcMessage, serverId: string, operation: string): void {
  if ("result" in message) return;
  const error = "error" in message ? message.error : null;
  throw subscriptionError(serverId, "subscription_remote_rejected", error?.message ?? `${operation} returned no result`);
}

function failureObject(error: unknown, code: string): JsonObject {
  return {
    code,
    name: error instanceof Error ? error.name : "Error",
    message: error instanceof Error ? error.message : String(error),
  };
}

function subscriptionError(serverId: string, code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-subscription", { server_id: serverId, code, message }),
    category: code.includes("transition") || code.includes("epoch") ? "conflict" : "capability",
    code,
    message,
    serverId,
    retryable: code.includes("connection") || code.includes("transition"),
    disposition: code.includes("connection") ? "reconnect_then_retry" : "terminal",
  });
}
