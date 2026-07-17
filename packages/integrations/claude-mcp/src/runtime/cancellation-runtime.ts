import type { JsonObject } from "../contracts.ts";
import type { McpTransportAdapter } from "../connection/contracts.ts";
import { cloneJson, deterministicMcpId, monotonicNow, sha256 } from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";
import { McpProtocolCodec } from "../core/protocol.ts";

export type McpCancellationStatus = "registered" | "cancelling" | "cancelled" | "completed" | "failed";

export interface McpCancellationOwner {
  cancellationId: string;
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  requestId: string;
  wireRequestId: string | number;
  operation: string;
  status: McpCancellationStatus;
  reason: string | null;
  registeredAt: string;
  completedAt: string | null;
  metadata: JsonObject;
}

export interface McpCancellationSnapshot {
  version: "zyra.mcp-cancellation-runtime/v1";
  revision: number;
  owners: McpCancellationOwner[];
  digest: string;
  capturedAt: string;
}

export class McpCancellationRuntime {
  private readonly codec = new McpProtocolCodec();
  private readonly owners = new Map<string, McpCancellationOwner>();
  private readonly byWireRequest = new Map<string, string>();
  private readonly controllers = new Map<string, AbortController>();
  private readonly now: () => Date;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; snapshot?: McpCancellationSnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restore(options.snapshot);
  }

  register(input: {
    serverId: string;
    connectionId: string;
    connectionEpoch: number;
    requestId: string;
    wireRequestId: string | number;
    operation: string;
    parentSignal?: AbortSignal;
    metadata?: JsonObject;
  }): { owner: McpCancellationOwner; signal: AbortSignal; release: (status?: "completed" | "failed") => McpCancellationOwner } {
    validate(input);
    const cancellationId = deterministicMcpId("mcp-cancellation", {
      server_id: input.serverId,
      connection_id: input.connectionId,
      connection_epoch: input.connectionEpoch,
      request_id: input.requestId,
      wire_request_id: input.wireRequestId,
      operation: input.operation,
    }, 40);
    const existing = this.owners.get(cancellationId);
    if (existing && existing.status === "registered") {
      const controller = this.controllers.get(cancellationId);
      if (!controller) throw cancellationError(input.serverId, "cancellation_controller_missing", `controller for ${cancellationId} is missing`);
      return this.binding(existing, controller);
    }
    const wireKey = key(input.serverId, input.connectionEpoch, input.wireRequestId);
    const conflict = this.byWireRequest.get(wireKey);
    if (conflict && conflict !== cancellationId) throw cancellationError(input.serverId, "wire_request_already_registered", `wire request ${String(input.wireRequestId)} already has a cancellation owner`);
    const owner: McpCancellationOwner = {
      cancellationId,
      serverId: input.serverId,
      connectionId: input.connectionId,
      connectionEpoch: input.connectionEpoch,
      requestId: input.requestId,
      wireRequestId: input.wireRequestId,
      operation: input.operation,
      status: "registered",
      reason: null,
      registeredAt: this.timestamp(),
      completedAt: null,
      metadata: cloneJson(input.metadata ?? {}),
    };
    const controller = new AbortController();
    if (input.parentSignal) {
      if (input.parentSignal.aborted) controller.abort(input.parentSignal.reason);
      else input.parentSignal.addEventListener("abort", () => this.cancel(cancellationId, String(input.parentSignal!.reason ?? "parent_aborted")), { once: true });
    }
    this.owners.set(cancellationId, owner);
    this.byWireRequest.set(wireKey, cancellationId);
    this.controllers.set(cancellationId, controller);
    this.revision += 1;
    return this.binding(owner, controller);
  }

  cancel(cancellationId: string, reason = "cancelled"): McpCancellationOwner {
    const owner = this.require(cancellationId);
    if (owner.status === "cancelled" || owner.status === "completed" || owner.status === "failed") return cloneJson(owner);
    owner.status = "cancelled";
    owner.reason = reason;
    owner.completedAt = this.timestamp();
    this.controllers.get(cancellationId)?.abort(new Error(reason));
    this.controllers.delete(cancellationId);
    this.revision += 1;
    return cloneJson(owner);
  }

  cancelByNotification(serverId: string, connectionEpoch: number, params: JsonObject): McpCancellationOwner | null {
    const requestId = params.requestId;
    if (typeof requestId !== "string" && typeof requestId !== "number") throw cancellationError(serverId, "cancel_request_id_missing", "cancel notification lacks requestId");
    const cancellationId = this.byWireRequest.get(key(serverId, connectionEpoch, requestId));
    if (!cancellationId) return null;
    return this.cancel(cancellationId, typeof params.reason === "string" ? params.reason : "cancelled_by_peer");
  }

  async notifyPeer(
    cancellationId: string,
    transport: McpTransportAdapter,
    reason = "cancelled_by_client",
    signal?: AbortSignal,
  ): Promise<McpCancellationOwner> {
    const owner = this.require(cancellationId);
    if (owner.status !== "registered") return cloneJson(owner);
    owner.status = "cancelling";
    owner.reason = reason;
    this.revision += 1;
    try {
      await transport.notify(this.codec.notification("notifications/cancelled", {
        requestId: owner.wireRequestId,
        reason,
      }), signal);
      return this.cancel(cancellationId, reason);
    } catch (error) {
      owner.status = "failed";
      owner.completedAt = this.timestamp();
      owner.metadata = { ...owner.metadata, notification_failure: error instanceof Error ? error.message : String(error) };
      this.controllers.get(cancellationId)?.abort(error);
      this.controllers.delete(cancellationId);
      this.revision += 1;
      throw error;
    }
  }

  get(cancellationId: string): McpCancellationOwner | null {
    const owner = this.owners.get(cancellationId);
    return owner ? cloneJson(owner) : null;
  }

  list(serverId?: string): McpCancellationOwner[] {
    return [...this.owners.values()].filter((owner) => !serverId || owner.serverId === serverId).sort((left, right) => left.registeredAt.localeCompare(right.registeredAt)).map(cloneJson);
  }

  snapshot(): McpCancellationSnapshot {
    const withoutDigest = {
      version: "zyra.mcp-cancellation-runtime/v1" as const,
      revision: this.revision,
      owners: this.list(),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: sha256(withoutDigest) };
  }

  restore(snapshot: McpCancellationSnapshot): void {
    if (snapshot.version !== "zyra.mcp-cancellation-runtime/v1") throw cancellationError("", "unsupported_cancellation_snapshot", "unsupported cancellation snapshot version");
    const { digest, ...withoutDigest } = snapshot;
    if (sha256(withoutDigest) !== digest) throw cancellationError("", "cancellation_snapshot_digest_mismatch", "cancellation snapshot digest mismatch");
    this.owners.clear();
    this.byWireRequest.clear();
    this.controllers.clear();
    this.revision = snapshot.revision;
    for (const value of snapshot.owners) {
      const owner = cloneJson(value);
      if (owner.status === "registered" || owner.status === "cancelling") {
        owner.status = "failed";
        owner.reason = "request interrupted by restart";
        owner.completedAt = snapshot.capturedAt;
        owner.metadata = { ...owner.metadata, restore_failure: "pending cancellation cannot retain process signal" };
      }
      this.owners.set(owner.cancellationId, owner);
      this.byWireRequest.set(key(owner.serverId, owner.connectionEpoch, owner.wireRequestId), owner.cancellationId);
    }
  }

  private binding(owner: McpCancellationOwner, controller: AbortController) {
    return {
      owner: cloneJson(owner),
      signal: controller.signal,
      release: (status: "completed" | "failed" = "completed") => this.finish(owner.cancellationId, status),
    };
  }

  private finish(cancellationId: string, status: "completed" | "failed"): McpCancellationOwner {
    const owner = this.require(cancellationId);
    if (owner.status === "cancelled") return cloneJson(owner);
    owner.status = status;
    owner.completedAt = this.timestamp();
    this.controllers.delete(cancellationId);
    this.revision += 1;
    return cloneJson(owner);
  }

  private require(cancellationId: string): McpCancellationOwner {
    const owner = this.owners.get(cancellationId);
    if (!owner) throw cancellationError("", "cancellation_not_found", `cancellation ${cancellationId} was not found`);
    return owner;
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function key(serverId: string, epoch: number, requestId: string | number): string {
  return `${serverId}\0${epoch}\0${typeof requestId}:${String(requestId)}`;
}

function validate(input: { serverId: string; connectionId: string; connectionEpoch: number; requestId: string; operation: string }): void {
  if (!input.serverId || !input.connectionId || !input.requestId || !input.operation) throw cancellationError(input.serverId, "cancellation_binding_incomplete", "cancellation binding is incomplete");
  if (!Number.isSafeInteger(input.connectionEpoch) || input.connectionEpoch < 0) throw cancellationError(input.serverId, "cancellation_epoch_invalid", "cancellation connection epoch is invalid");
}

function cancellationError(serverId: string, code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-cancellation", { server_id: serverId, code, message }),
    category: code.includes("conflict") || code.includes("registered") ? "conflict" : "protocol",
    code,
    message,
    serverId,
    retryable: false,
    disposition: "terminal",
  });
}
