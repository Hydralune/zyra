import {
  type Clock,
  SystemClock,
  assertIdentifier,
  assertNonEmpty,
  canonicalJson,
  deepClone,
  digestJson,
} from "./canonical.ts";
import { ProviderControlPlaneError } from "./errors.ts";
import { ProviderControlPlaneStore } from "./store.ts";

export type DispatchCancellationState = "requested" | "acknowledged" | "completed_before_cancel";

export interface DispatchCancellationReceipt {
  readonly dispatchId: string;
  readonly state: DispatchCancellationState;
  readonly reason: string;
  readonly requestedBy: string;
  readonly requestedAt: number;
  readonly acknowledgedAt: number | null;
  readonly generation: number;
}

export interface DispatchCancellationBinding {
  readonly dispatchId: string;
  readonly signal: AbortSignal;
  readonly cancellationRequested: () => boolean;
  release(outcome: "succeeded" | "failed" | "aborted"): DispatchCancellationReceipt | null;
}

interface JsonRow { readonly json: string }

interface ActiveDispatch {
  readonly controller: AbortController;
  released: boolean;
}

export class ProviderDispatchCancellationRuntime {
  private readonly store: ProviderControlPlaneStore;
  private readonly clock: Clock;
  private readonly active = new Map<string, ActiveDispatch>();

  constructor(store: ProviderControlPlaneStore, options: { readonly clock?: Clock } = {}) {
    this.store = store;
    this.clock = options.clock ?? new SystemClock();
    this.initialize();
  }

  bind(dispatchId: string, externalSignal?: AbortSignal): DispatchCancellationBinding {
    assertIdentifier(dispatchId, "dispatchId");
    if (this.active.has(dispatchId)) {
      throw new ProviderControlPlaneError({
        layer: "transport",
        kind: "invalid_request",
        message: `dispatch is already active in this process: ${dispatchId}`,
        retryable: true,
        recoveryIntent: "retry_same_route",
      });
    }
    const controller = new AbortController();
    const active: ActiveDispatch = { controller, released: false };
    this.active.set(dispatchId, active);
    const persisted = this.get(dispatchId);
    if (persisted?.state === "requested") {
      controller.abort(cancellationError(persisted));
    }
    let removeExternalListener: (() => void) | null = null;
    if (externalSignal !== undefined) {
      const abortFromExternal = () => {
        if (!controller.signal.aborted) {
          controller.abort(externalSignal.reason ?? new DOMException("dispatch aborted", "AbortError"));
        }
      };
      if (externalSignal.aborted) abortFromExternal();
      else {
        externalSignal.addEventListener("abort", abortFromExternal, { once: true });
        removeExternalListener = () => externalSignal.removeEventListener("abort", abortFromExternal);
      }
    }
    return {
      dispatchId,
      signal: controller.signal,
      cancellationRequested: () => this.get(dispatchId)?.state === "requested",
      release: (outcome) => {
        if (active.released) throw new Error(`dispatch cancellation binding already released: ${dispatchId}`);
        active.released = true;
        removeExternalListener?.();
        this.active.delete(dispatchId);
        return this.settle(dispatchId, outcome);
      },
    };
  }

  request(dispatchId: string, reason: string, requestedBy: string): DispatchCancellationReceipt {
    assertIdentifier(dispatchId, "dispatchId");
    assertNonEmpty(reason, "reason");
    assertNonEmpty(requestedBy, "requestedBy");
    const receipt = this.store.transaction(() => {
      const current = this.read(dispatchId);
      if (current?.state === "acknowledged") return current;
      const next: DispatchCancellationReceipt = {
        dispatchId,
        state: "requested",
        reason,
        requestedBy,
        requestedAt: current?.requestedAt ?? this.clock.now(),
        acknowledgedAt: null,
        generation: (current?.generation ?? 0) + 1,
      };
      this.write(next);
      return next;
    });
    const active = this.active.get(dispatchId);
    if (active !== undefined && !active.controller.signal.aborted) {
      active.controller.abort(cancellationError(receipt));
    }
    return deepClone(receipt);
  }

  get(dispatchId: string): DispatchCancellationReceipt | null {
    assertIdentifier(dispatchId, "dispatchId");
    return deepClone(this.read(dispatchId));
  }

  list(state?: DispatchCancellationState): DispatchCancellationReceipt[] {
    const rows = state === undefined
      ? this.store.db.prepare("SELECT json FROM provider_dispatch_cancellations ORDER BY requested_at, dispatch_id").all()
      : this.store.db.prepare(
          "SELECT json FROM provider_dispatch_cancellations WHERE state = ? ORDER BY requested_at, dispatch_id",
        ).all(state);
    return (rows as unknown as JsonRow[]).map((row) => JSON.parse(row.json) as DispatchCancellationReceipt);
  }

  purgeCompleted(before: number): number {
    if (!Number.isSafeInteger(before) || before < 0) throw new TypeError("before must be a non-negative integer");
    const result = this.store.db.prepare(`
      DELETE FROM provider_dispatch_cancellations
      WHERE state IN ('acknowledged', 'completed_before_cancel')
        AND acknowledged_at IS NOT NULL
        AND acknowledged_at < ?
    `).run(before) as unknown as { changes?: number };
    return Number(result.changes ?? 0);
  }

  close(): void {
    for (const [dispatchId, active] of this.active) {
      if (!active.controller.signal.aborted) {
        active.controller.abort(new DOMException(`provider control plane closed during ${dispatchId}`, "AbortError"));
      }
    }
    this.active.clear();
  }

  private settle(
    dispatchId: string,
    outcome: "succeeded" | "failed" | "aborted",
  ): DispatchCancellationReceipt | null {
    return this.store.transaction(() => {
      const current = this.read(dispatchId);
      if (current === null) return null;
      if (current.state !== "requested") return deepClone(current);
      const acknowledgedAt = this.clock.now();
      const next: DispatchCancellationReceipt = {
        ...current,
        state: outcome === "aborted" ? "acknowledged" : "completed_before_cancel",
        acknowledgedAt,
        generation: current.generation + 1,
      };
      this.write(next);
      return deepClone(next);
    });
  }

  private initialize(): void {
    this.store.db.exec(`
      CREATE TABLE IF NOT EXISTS provider_dispatch_cancellations (
        dispatch_id TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        requested_at INTEGER NOT NULL,
        acknowledged_at INTEGER,
        generation INTEGER NOT NULL,
        json TEXT NOT NULL,
        checksum TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_provider_dispatch_cancellation_state
        ON provider_dispatch_cancellations(state, requested_at);
    `);
  }

  private read(dispatchId: string): DispatchCancellationReceipt | null {
    const row = this.store.db.prepare(
      "SELECT json FROM provider_dispatch_cancellations WHERE dispatch_id = ?",
    ).get(dispatchId) as JsonRow | undefined;
    return row === undefined ? null : JSON.parse(row.json) as DispatchCancellationReceipt;
  }

  private write(receipt: DispatchCancellationReceipt): void {
    const json = canonicalJson(receipt);
    this.store.db.prepare(`
      INSERT INTO provider_dispatch_cancellations(
        dispatch_id, state, requested_at, acknowledged_at, generation, json, checksum
      ) VALUES (?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(dispatch_id) DO UPDATE SET
        state = excluded.state,
        acknowledged_at = excluded.acknowledged_at,
        generation = excluded.generation,
        json = excluded.json,
        checksum = excluded.checksum
    `).run(
      receipt.dispatchId,
      receipt.state,
      receipt.requestedAt,
      receipt.acknowledgedAt,
      receipt.generation,
      json,
      digestJson(receipt),
    );
  }
}

function cancellationError(receipt: DispatchCancellationReceipt): ProviderControlPlaneError {
  return new ProviderControlPlaneError({
    layer: "transport",
    kind: "request_aborted",
    message: `dispatch cancelled: ${receipt.reason}`,
    retryable: false,
    recoveryIntent: "none",
    detail: {
      dispatchId: receipt.dispatchId,
      requestedBy: receipt.requestedBy,
      requestedAt: receipt.requestedAt,
      generation: receipt.generation,
    },
  });
}
