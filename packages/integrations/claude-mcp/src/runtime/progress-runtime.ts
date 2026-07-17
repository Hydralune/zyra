import type { JsonObject, JsonValue } from "../contracts.ts";
import { canonicalJson, cloneJson, deterministicMcpId, monotonicNow, sha256 } from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";

export interface McpProgressOwner {
  progressId: string;
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  requestId: string;
  progressToken: string | number;
  current: number;
  total: number | null;
  message: string | null;
  sequence: number;
  status: "active" | "completed" | "cancelled" | "failed";
  createdAt: string;
  updatedAt: string;
  completedAt: string | null;
  metadata: JsonObject;
}

export interface McpProgressEvent {
  eventId: string;
  progressId: string;
  sequence: number;
  current: number;
  total: number | null;
  message: string | null;
  delta: number;
  occurredAt: string;
  payloadDigest: string;
  metadata: JsonObject;
}

export interface McpProgressSnapshot {
  version: "zyra.mcp-progress-runtime/v1";
  revision: number;
  owners: McpProgressOwner[];
  events: McpProgressEvent[];
  digest: string;
  capturedAt: string;
}

export class McpProgressRuntime {
  private readonly owners = new Map<string, McpProgressOwner>();
  private readonly byToken = new Map<string, string>();
  private readonly events: McpProgressEvent[] = [];
  private readonly listeners = new Set<(event: McpProgressEvent) => void | Promise<void>>();
  private readonly now: () => Date;
  private readonly maximumEvents: number;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumEvents?: number; snapshot?: McpProgressSnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumEvents = options.maximumEvents ?? 50_000;
    if (options.snapshot) this.restore(options.snapshot);
  }

  begin(input: {
    serverId: string;
    connectionId: string;
    connectionEpoch: number;
    requestId: string;
    progressToken: string | number;
    total?: number | null;
    message?: string | null;
    metadata?: JsonObject;
  }): McpProgressOwner {
    validateProgressNumber(input.total ?? null, "total", true);
    const tokenKey = key(input.serverId, input.connectionEpoch, input.progressToken);
    const existingId = this.byToken.get(tokenKey);
    if (existingId) {
      const existing = this.owners.get(existingId)!;
      if (existing.requestId !== input.requestId) throw progressError(input.serverId, "progress_token_conflict", `progress token is already bound to ${existing.requestId}`);
      return cloneJson(existing);
    }
    const progressId = deterministicMcpId("mcp-progress", {
      server_id: input.serverId,
      connection_id: input.connectionId,
      connection_epoch: input.connectionEpoch,
      request_id: input.requestId,
      progress_token: input.progressToken,
    }, 40);
    const timestamp = this.timestamp();
    const owner: McpProgressOwner = {
      progressId,
      serverId: input.serverId,
      connectionId: input.connectionId,
      connectionEpoch: input.connectionEpoch,
      requestId: input.requestId,
      progressToken: input.progressToken,
      current: 0,
      total: input.total ?? null,
      message: input.message ?? null,
      sequence: 0,
      status: "active",
      createdAt: timestamp,
      updatedAt: timestamp,
      completedAt: null,
      metadata: cloneJson(input.metadata ?? {}),
    };
    this.owners.set(progressId, owner);
    this.byToken.set(tokenKey, progressId);
    this.revision += 1;
    return cloneJson(owner);
  }

  async update(input: {
    serverId: string;
    connectionEpoch: number;
    progressToken: string | number;
    progress: number;
    total?: number | null;
    message?: string | null;
    metadata?: JsonObject;
  }): Promise<McpProgressEvent> {
    validateProgressNumber(input.progress, "progress");
    validateProgressNumber(input.total ?? null, "total", true);
    const progressId = this.byToken.get(key(input.serverId, input.connectionEpoch, input.progressToken));
    if (!progressId) throw progressError(input.serverId, "progress_not_found", "progress token is not registered for this connection epoch");
    const owner = this.owners.get(progressId)!;
    if (owner.status !== "active") throw progressError(input.serverId, "progress_not_active", `progress ${progressId} is ${owner.status}`);
    if (input.progress < owner.current) throw progressError(input.serverId, "progress_regression", `progress cannot regress from ${owner.current} to ${input.progress}`);
    const total = input.total ?? owner.total;
    if (total !== null && input.progress > total) throw progressError(input.serverId, "progress_exceeds_total", `progress ${input.progress} exceeds total ${total}`);
    const occurredAt = this.timestamp();
    const base = {
      progressId,
      sequence: owner.sequence + 1,
      current: input.progress,
      total,
      message: input.message ?? owner.message,
      delta: input.progress - owner.current,
      occurredAt,
      metadata: cloneJson(input.metadata ?? {}),
    };
    const event: McpProgressEvent = {
      eventId: deterministicMcpId("mcp-progress-event", base, 40),
      ...base,
      payloadDigest: sha256(base),
    };
    owner.current = input.progress;
    owner.total = total;
    owner.message = base.message;
    owner.sequence = base.sequence;
    owner.updatedAt = occurredAt;
    if (total !== null && input.progress === total) {
      owner.status = "completed";
      owner.completedAt = occurredAt;
    }
    this.events.push(event);
    while (this.events.length > this.maximumEvents) this.events.shift();
    this.revision += 1;
    for (const listener of this.listeners) await listener(cloneJson(event));
    return cloneJson(event);
  }

  handleNotification(serverId: string, connectionEpoch: number, paramsValue: JsonObject): Promise<McpProgressEvent> {
    const token = paramsValue.progressToken;
    if (typeof token !== "string" && typeof token !== "number") throw progressError(serverId, "progress_token_missing", "progress notification lacks progressToken");
    const progress = paramsValue.progress;
    if (typeof progress !== "number") throw progressError(serverId, "progress_value_missing", "progress notification lacks numeric progress");
    return this.update({
      serverId,
      connectionEpoch,
      progressToken: token,
      progress,
      total: typeof paramsValue.total === "number" ? paramsValue.total : null,
      message: typeof paramsValue.message === "string" ? paramsValue.message : null,
      metadata: paramsValue._meta && typeof paramsValue._meta === "object" && !Array.isArray(paramsValue._meta)
        ? canonicalJson(paramsValue._meta) as JsonObject
        : {},
    });
  }

  finish(progressId: string, status: "completed" | "cancelled" | "failed", metadata: JsonObject = {}): McpProgressOwner {
    const owner = this.owners.get(progressId);
    if (!owner) throw progressError("", "progress_not_found", `progress ${progressId} was not found`);
    if (owner.status !== "active") return cloneJson(owner);
    owner.status = status;
    owner.completedAt = this.timestamp();
    owner.updatedAt = owner.completedAt;
    owner.metadata = { ...owner.metadata, ...cloneJson(metadata) };
    this.revision += 1;
    return cloneJson(owner);
  }

  onProgress(listener: (event: McpProgressEvent) => void | Promise<void>): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  get(progressId: string): McpProgressOwner | null {
    const owner = this.owners.get(progressId);
    return owner ? cloneJson(owner) : null;
  }

  list(serverId?: string): McpProgressOwner[] {
    return [...this.owners.values()].filter((owner) => !serverId || owner.serverId === serverId).sort((left, right) => left.createdAt.localeCompare(right.createdAt)).map(cloneJson);
  }

  snapshot(): McpProgressSnapshot {
    const withoutDigest = {
      version: "zyra.mcp-progress-runtime/v1" as const,
      revision: this.revision,
      owners: this.list(),
      events: this.events.map(cloneJson),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: sha256(withoutDigest) };
  }

  restore(snapshot: McpProgressSnapshot): void {
    if (snapshot.version !== "zyra.mcp-progress-runtime/v1") throw progressError("", "unsupported_progress_snapshot", "unsupported progress snapshot version");
    const { digest, ...withoutDigest } = snapshot;
    if (sha256(withoutDigest) !== digest) throw progressError("", "progress_snapshot_digest_mismatch", "progress snapshot digest mismatch");
    this.owners.clear();
    this.byToken.clear();
    this.events.splice(0);
    this.revision = snapshot.revision;
    for (const value of snapshot.owners) {
      const owner = cloneJson(value);
      if (owner.status === "active") {
        owner.status = "failed";
        owner.completedAt = snapshot.capturedAt;
        owner.updatedAt = snapshot.capturedAt;
        owner.metadata = { ...owner.metadata, restore_failure: "progress owner interrupted by restart" };
      }
      this.owners.set(owner.progressId, owner);
      this.byToken.set(key(owner.serverId, owner.connectionEpoch, owner.progressToken), owner.progressId);
    }
    for (const event of snapshot.events.slice(-this.maximumEvents)) this.events.push(cloneJson(event));
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function key(serverId: string, connectionEpoch: number, token: string | number): string {
  return `${serverId}\0${connectionEpoch}\0${typeof token}:${String(token)}`;
}

function validateProgressNumber(value: number | null, field: string, nullable = false): void {
  if (value === null && nullable) return;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) throw progressError("", `invalid_${field}`, `${field} must be a finite non-negative number`);
}

function progressError(serverId: string, code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-progress", { server_id: serverId, code, message }),
    category: code.includes("conflict") || code.includes("regression") ? "conflict" : "protocol",
    code,
    message,
    serverId,
    retryable: false,
    disposition: "terminal",
  });
}
