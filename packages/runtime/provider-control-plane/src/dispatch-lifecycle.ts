import type { JsonRecord } from "./canonical.ts";
import {
  type Clock,
  type IdFactory,
  RandomIdFactory,
  SystemClock,
  canonicalJson,
  canonicalize,
  deepClone,
  digestJson,
} from "./canonical.ts";
import type {
  ProviderDispatchRequest,
  ProviderDispatchResult,
  ProviderStreamFrame,
} from "./contracts.ts";
import { ProviderControlPlaneError, asProviderError } from "./errors.ts";
import { ProviderControlPlaneStore } from "./store.ts";

export type DispatchLifecycleState =
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "reconcile_required";

export interface DispatchLifecycleSnapshot {
  readonly dispatchId: string;
  readonly routeId: string;
  readonly idempotencyKey: string;
  readonly requestDigest: string;
  readonly state: DispatchLifecycleState;
  readonly epoch: number;
  readonly ownerToken: string;
  readonly leaseExpiresAt: number;
  readonly outputObserved: boolean;
  readonly frameCount: number;
  readonly lastSequence: number;
  readonly streamEvidenceDigest: string;
  readonly result: ProviderDispatchResult | null;
  readonly failure: JsonRecord | null;
  readonly createdAt: number;
  readonly updatedAt: number;
  readonly completedAt: number | null;
}

export interface DispatchClaim {
  readonly disposition: "execute" | "cached";
  readonly snapshot: DispatchLifecycleSnapshot;
  readonly result: ProviderDispatchResult | null;
}

interface JsonRow {
  readonly json: string;
}

interface LifecycleOptions {
  readonly clock?: Clock;
  readonly ids?: IdFactory;
  readonly leaseMilliseconds?: number;
}

export class ProviderDispatchLifecycle {
  private readonly store: ProviderControlPlaneStore;
  private readonly clock: Clock;
  private readonly ids: IdFactory;
  private readonly leaseMilliseconds: number;

  constructor(store: ProviderControlPlaneStore, options: LifecycleOptions = {}) {
    this.store = store;
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.leaseMilliseconds = options.leaseMilliseconds ?? 120_000;
    if (!Number.isSafeInteger(this.leaseMilliseconds) || this.leaseMilliseconds <= 0) {
      throw new TypeError("dispatch lifecycle leaseMilliseconds must be positive");
    }
    this.initialize();
  }

  claim(request: ProviderDispatchRequest): DispatchClaim {
    validateRequest(request);
    const requestDigest = digestRequest(request);
    const now = this.clock.now();
    return this.store.transaction(() => {
      const byKey = this.readByKey(request.routeId, request.idempotencyKey);
      const byDispatch = this.read(request.dispatchId);
      if (byKey !== null && byDispatch !== null && byKey.dispatchId !== byDispatch.dispatchId) {
        throw lifecycleConflict(request, "dispatch identity and idempotency key resolve to different records", {
          dispatchRecord: byDispatch.dispatchId,
          idempotencyRecord: byKey.dispatchId,
        });
      }
      const existing = byKey ?? byDispatch;
      if (existing === null) {
        const created: DispatchLifecycleSnapshot = {
          dispatchId: request.dispatchId,
          routeId: request.routeId,
          idempotencyKey: request.idempotencyKey,
          requestDigest,
          state: "running",
          epoch: 1,
          ownerToken: this.ids.next("provider_dispatch_owner"),
          leaseExpiresAt: now + this.leaseMilliseconds,
          outputObserved: false,
          frameCount: 0,
          lastSequence: 0,
          streamEvidenceDigest: "",
          result: null,
          failure: null,
          createdAt: now,
          updatedAt: now,
          completedAt: null,
        };
        this.write(created);
        return { disposition: "execute", snapshot: deepClone(created), result: null };
      }
      this.assertIdentity(existing, request, requestDigest);
      if (existing.state === "succeeded") {
        if (existing.result === null) throw lifecycleCorruption(existing, "succeeded dispatch has no cached result");
        return { disposition: "cached", snapshot: deepClone(existing), result: deepClone(existing.result) };
      }
      if (existing.state === "reconcile_required" || existing.outputObserved) {
        throw lifecycleConflict(request, "observable output requires reconciliation and forbids replay", {
          state: existing.state,
          epoch: existing.epoch,
          frameCount: existing.frameCount,
          streamEvidenceDigest: existing.streamEvidenceDigest,
        });
      }
      if (existing.state === "running" && existing.leaseExpiresAt > now) {
        throw lifecycleConflict(request, "dispatch idempotency claim is already running", {
          epoch: existing.epoch,
          leaseExpiresAt: existing.leaseExpiresAt,
        });
      }
      const resumed: DispatchLifecycleSnapshot = {
        ...existing,
        state: "running",
        epoch: existing.epoch + 1,
        ownerToken: this.ids.next("provider_dispatch_owner"),
        leaseExpiresAt: now + this.leaseMilliseconds,
        failure: null,
        updatedAt: now,
        completedAt: null,
      };
      this.write(resumed);
      return { disposition: "execute", snapshot: deepClone(resumed), result: null };
    });
  }

  heartbeat(dispatchId: string, ownerToken: string): DispatchLifecycleSnapshot {
    return this.mutateOwned(dispatchId, ownerToken, (current, now) => ({
      ...current,
      leaseExpiresAt: now + this.leaseMilliseconds,
      updatedAt: now,
    }));
  }

  observeFrames(
    dispatchId: string,
    ownerToken: string,
    frames: readonly ProviderStreamFrame[],
  ): DispatchLifecycleSnapshot {
    if (frames.length === 0) return this.require(dispatchId);
    return this.mutateOwned(dispatchId, ownerToken, (current, now) => {
      let outputObserved = current.outputObserved;
      let lastSequence = current.lastSequence;
      for (const frame of frames) {
        if (frame.dispatchId !== current.dispatchId) throw lifecycleCorruption(current, "frame dispatch identity mismatch");
        if (frame.sequence <= lastSequence) throw lifecycleCorruption(current, "frame sequence is not strictly increasing");
        lastSequence = frame.sequence;
        if (frame.kind === "text_delta" || frame.kind === "tool_call_delta") outputObserved = true;
      }
      const evidence = {
        previous: current.streamEvidenceDigest,
        frames: frames.map((frame) => ({
          frameId: frame.frameId,
          sequence: frame.sequence,
          kind: frame.kind,
          toolCallId: frame.toolCallId,
          textDigest: frame.text === null ? "" : digestJson(frame.text),
          jsonDigest: frame.jsonDelta === null ? "" : digestJson(frame.jsonDelta),
        })),
      };
      return {
        ...current,
        outputObserved,
        frameCount: current.frameCount + frames.length,
        lastSequence,
        streamEvidenceDigest: digestJson(evidence),
        leaseExpiresAt: now + this.leaseMilliseconds,
        updatedAt: now,
      };
    });
  }

  succeed(
    dispatchId: string,
    ownerToken: string,
    result: ProviderDispatchResult,
  ): DispatchLifecycleSnapshot {
    return this.mutateOwned(dispatchId, ownerToken, (current, now) => {
      if (result.dispatchId !== dispatchId) throw lifecycleCorruption(current, "result dispatch identity mismatch");
      return {
        ...current,
        state: "succeeded",
        outputObserved: current.outputObserved || result.frames.some(isObservableFrame),
        result: deepClone(result),
        failure: null,
        leaseExpiresAt: now,
        updatedAt: now,
        completedAt: now,
      };
    });
  }

  fail(
    dispatchId: string,
    ownerToken: string,
    error: unknown,
  ): DispatchLifecycleSnapshot {
    const providerError = asProviderError(error);
    return this.mutateOwned(dispatchId, ownerToken, (current, now) => {
      const outputObserved = current.outputObserved || providerError.outputObserved;
      return {
        ...current,
        state: outputObserved ? "reconcile_required" : providerError.kind === "request_aborted" ? "cancelled" : "failed",
        outputObserved,
        result: null,
        failure: canonicalize(providerError.safe()) as JsonRecord,
        leaseExpiresAt: now,
        updatedAt: now,
        completedAt: now,
      };
    });
  }

  require(dispatchId: string): DispatchLifecycleSnapshot {
    const value = this.read(dispatchId);
    if (value === null) {
      throw new ProviderControlPlaneError({
        layer: "transport",
        kind: "route_not_found",
        message: `dispatch lifecycle not found: ${dispatchId}`,
        detail: { dispatchId },
      });
    }
    return deepClone(value);
  }

  list(routeId?: string): DispatchLifecycleSnapshot[] {
    const rows = routeId === undefined
      ? this.store.db.prepare("SELECT json FROM provider_dispatch_lifecycle ORDER BY created_at, dispatch_id").all()
      : this.store.db.prepare("SELECT json FROM provider_dispatch_lifecycle WHERE route_id = ? ORDER BY created_at, dispatch_id").all(routeId);
    return (rows as unknown as JsonRow[]).map((row) => this.decode(row.json));
  }

  sweepExpired(limit = 100): DispatchLifecycleSnapshot[] {
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 10_000) throw new TypeError("sweep limit out of range");
    const now = this.clock.now();
    return this.store.transaction(() => {
      const rows = this.store.db.prepare(`
        SELECT json FROM provider_dispatch_lifecycle
        WHERE state = 'running' AND lease_expires_at <= ?
        ORDER BY lease_expires_at, dispatch_id LIMIT ?
      `).all(now, limit) as unknown as JsonRow[];
      const updated: DispatchLifecycleSnapshot[] = [];
      for (const row of rows) {
        const current = this.decode(row.json);
        const next: DispatchLifecycleSnapshot = {
          ...current,
          state: current.outputObserved ? "reconcile_required" : "failed",
          failure: {
            code: "dispatch_owner_lost",
            message: current.outputObserved
              ? "dispatch owner expired after observable output"
              : "dispatch owner expired before observable output",
          },
          leaseExpiresAt: now,
          updatedAt: now,
          completedAt: now,
        };
        this.write(next);
        updated.push(deepClone(next));
      }
      return updated;
    });
  }

  private mutateOwned(
    dispatchId: string,
    ownerToken: string,
    mutate: (current: DispatchLifecycleSnapshot, now: number) => DispatchLifecycleSnapshot,
  ): DispatchLifecycleSnapshot {
    return this.store.transaction(() => {
      const current = this.read(dispatchId);
      if (current === null) throw new TypeError(`dispatch lifecycle not found: ${dispatchId}`);
      if (current.state !== "running") throw lifecycleCorruption(current, `dispatch is not running: ${current.state}`);
      if (current.ownerToken !== ownerToken) {
        throw new ProviderControlPlaneError({
          layer: "transport",
          kind: "route_revision_conflict",
          message: "dispatch lifecycle owner token is fenced",
          routeId: current.routeId,
          detail: { dispatchId, epoch: current.epoch },
        });
      }
      const next = mutate(current, this.clock.now());
      this.write(next);
      return deepClone(next);
    });
  }

  private assertIdentity(
    existing: DispatchLifecycleSnapshot,
    request: ProviderDispatchRequest,
    requestDigest: string,
  ): void {
    if (existing.dispatchId !== request.dispatchId) {
      throw lifecycleConflict(request, "idempotency key was reused by another dispatch", {
        existingDispatchId: existing.dispatchId,
      });
    }
    if (existing.routeId !== request.routeId || existing.requestDigest !== requestDigest) {
      throw lifecycleConflict(request, "dispatch identity was reused with different request content", {
        existingRouteId: existing.routeId,
        existingRequestDigest: existing.requestDigest,
        requestDigest,
      });
    }
  }

  private initialize(): void {
    this.store.db.exec(`
      CREATE TABLE IF NOT EXISTS provider_dispatch_lifecycle (
        dispatch_id TEXT PRIMARY KEY,
        route_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        state TEXT NOT NULL,
        epoch INTEGER NOT NULL,
        owner_token TEXT NOT NULL,
        lease_expires_at INTEGER NOT NULL,
        output_observed INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        UNIQUE(route_id, idempotency_key)
      );
      CREATE INDEX IF NOT EXISTS idx_provider_dispatch_lifecycle_state
        ON provider_dispatch_lifecycle(state, lease_expires_at);
    `);
  }

  private read(dispatchId: string): DispatchLifecycleSnapshot | null {
    const row = this.store.db.prepare(
      "SELECT json FROM provider_dispatch_lifecycle WHERE dispatch_id = ?",
    ).get(dispatchId) as JsonRow | undefined;
    return row === undefined ? null : this.decode(row.json);
  }

  private readByKey(routeId: string, idempotencyKey: string): DispatchLifecycleSnapshot | null {
    const row = this.store.db.prepare(
      "SELECT json FROM provider_dispatch_lifecycle WHERE route_id = ? AND idempotency_key = ?",
    ).get(routeId, idempotencyKey) as JsonRow | undefined;
    return row === undefined ? null : this.decode(row.json);
  }

  private write(value: DispatchLifecycleSnapshot): void {
    const json = canonicalJson(value);
    this.store.db.prepare(`
      INSERT INTO provider_dispatch_lifecycle(
        dispatch_id, route_id, idempotency_key, request_digest, state, epoch,
        owner_token, lease_expires_at, output_observed, created_at, updated_at,
        json, checksum
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(dispatch_id) DO UPDATE SET
        state = excluded.state,
        epoch = excluded.epoch,
        owner_token = excluded.owner_token,
        lease_expires_at = excluded.lease_expires_at,
        output_observed = excluded.output_observed,
        updated_at = excluded.updated_at,
        json = excluded.json,
        checksum = excluded.checksum
    `).run(
      value.dispatchId,
      value.routeId,
      value.idempotencyKey,
      value.requestDigest,
      value.state,
      value.epoch,
      value.ownerToken,
      value.leaseExpiresAt,
      value.outputObserved ? 1 : 0,
      value.createdAt,
      value.updatedAt,
      json,
      digestJson(value),
    );
  }

  private decode(json: string): DispatchLifecycleSnapshot {
    const value = JSON.parse(json) as DispatchLifecycleSnapshot;
    return deepClone(value);
  }
}

function validateRequest(request: ProviderDispatchRequest): void {
  if (!request.dispatchId.trim()) throw new TypeError("dispatchId is required");
  if (!request.routeId.trim()) throw new TypeError("routeId is required");
  if (!request.idempotencyKey.trim()) throw new TypeError("idempotencyKey is required");
}

function digestRequest(request: ProviderDispatchRequest): string {
  return digestJson({
    routeId: request.routeId,
    runId: request.runId,
    taskId: request.taskId,
    nodeId: request.nodeId,
    sessionId: request.sessionId,
    turnId: request.turnId,
    messages: request.messages,
    tools: request.tools,
    maximumOutputTokens: request.maximumOutputTokens,
    temperature: request.temperature,
    stream: request.stream,
    timeoutMilliseconds: request.timeoutMilliseconds,
    chunkTimeoutMilliseconds: request.chunkTimeoutMilliseconds,
    extraBody: request.extraBody,
    metadata: request.metadata,
  });
}

function isObservableFrame(frame: ProviderStreamFrame): boolean {
  return frame.kind === "text_delta" || frame.kind === "tool_call_delta";
}

function lifecycleConflict(
  request: ProviderDispatchRequest,
  message: string,
  detail: JsonRecord,
): ProviderControlPlaneError {
  return new ProviderControlPlaneError({
    layer: "transport",
    kind: "route_revision_conflict",
    message,
    routeId: request.routeId,
    recoveryIntent: "surface_to_operator",
    detail: { dispatchId: request.dispatchId, idempotencyKey: request.idempotencyKey, ...detail },
  });
}

function lifecycleCorruption(
  snapshot: DispatchLifecycleSnapshot,
  message: string,
): ProviderControlPlaneError {
  return new ProviderControlPlaneError({
    layer: "transport",
    kind: "response_protocol_error",
    message,
    routeId: snapshot.routeId,
    recoveryIntent: snapshot.outputObserved ? "reconcile_partial_response" : "surface_to_operator",
    outputObserved: snapshot.outputObserved,
    detail: { dispatchId: snapshot.dispatchId, state: snapshot.state, epoch: snapshot.epoch },
  });
}
