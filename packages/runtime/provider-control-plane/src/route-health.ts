import type { JsonRecord } from "./canonical.ts";
import {
  type Clock,
  type IdFactory,
  RandomIdFactory,
  SystemClock,
  canonicalJson,
  deepClone,
  digestJson,
} from "./canonical.ts";
import type { ProviderRouteLease } from "./contracts.ts";
import { ProviderControlPlaneError } from "./errors.ts";
import { ProviderControlPlaneStore } from "./store.ts";

export type RouteCircuitState = "closed" | "open" | "half_open";

export interface RouteHealthSnapshot {
  readonly providerId: string;
  readonly modelId: string;
  readonly generation: number;
  readonly circuit: RouteCircuitState;
  readonly failureStreak: number;
  readonly successStreak: number;
  readonly successCount: number;
  readonly failureCount: number;
  readonly ewmaLatencyMilliseconds: number;
  readonly ewmaFailureRate: number;
  readonly lastStatus: number | null;
  readonly lastFailureKind: string;
  readonly retryAfterUntil: number;
  readonly cooldownUntil: number;
  readonly halfOpenProbeOwner: string;
  readonly updatedAt: number;
}

export interface ProviderAdmissionPermit {
  readonly permitId: string;
  readonly routeId: string;
  readonly providerId: string;
  readonly modelId: string;
  readonly ownerToken: string;
  readonly acquiredAt: number;
  readonly expiresAt: number;
  readonly healthGeneration: number;
}

export interface RouteHealthOptions {
  readonly clock?: Clock;
  readonly ids?: IdFactory;
  readonly maximumInFlightPerProvider?: number;
  readonly permitMilliseconds?: number;
  readonly failureThreshold?: number;
  readonly cooldownMilliseconds?: number;
  readonly ewmaAlpha?: number;
}

interface JsonRow { readonly json: string }
interface CountRow { readonly count: number }

export class ProviderRouteHealthRuntime {
  private readonly store: ProviderControlPlaneStore;
  private readonly clock: Clock;
  private readonly ids: IdFactory;
  private readonly maximumInFlight: number;
  private readonly permitMilliseconds: number;
  private readonly failureThreshold: number;
  private readonly cooldownMilliseconds: number;
  private readonly alpha: number;

  constructor(store: ProviderControlPlaneStore, options: RouteHealthOptions = {}) {
    this.store = store;
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.maximumInFlight = options.maximumInFlightPerProvider ?? 8;
    this.permitMilliseconds = options.permitMilliseconds ?? 180_000;
    this.failureThreshold = options.failureThreshold ?? 3;
    this.cooldownMilliseconds = options.cooldownMilliseconds ?? 30_000;
    this.alpha = options.ewmaAlpha ?? 0.2;
    for (const [name, value] of Object.entries({
      maximumInFlight: this.maximumInFlight,
      permitMilliseconds: this.permitMilliseconds,
      failureThreshold: this.failureThreshold,
      cooldownMilliseconds: this.cooldownMilliseconds,
    })) {
      if (!Number.isSafeInteger(value) || value <= 0) throw new TypeError(`${name} must be a positive integer`);
    }
    if (!Number.isFinite(this.alpha) || this.alpha <= 0 || this.alpha > 1) throw new TypeError("ewmaAlpha must be in (0, 1]");
    this.initialize();
  }

  snapshot(providerId: string, modelId: string): RouteHealthSnapshot {
    const existing = this.read(providerId, modelId);
    if (existing === null) return initialHealth(providerId, modelId, this.clock.now());
    if (existing.circuit === "open" && existing.cooldownUntil <= this.clock.now()) {
      return this.store.transaction(() => {
        const current = this.read(providerId, modelId) ?? existing;
        return this.advanceCooldown(current, this.clock.now());
      });
    }
    return deepClone(existing);
  }

  list(): RouteHealthSnapshot[] {
    const rows = this.store.db.prepare(
      "SELECT json FROM provider_route_health ORDER BY provider_id, model_id",
    ).all() as unknown as JsonRow[];
    return rows.map((row) => this.decode(row.json));
  }

  isAdmissible(providerId: string, modelId: string): boolean {
    const value = this.snapshot(providerId, modelId);
    const now = this.clock.now();
    if (value.retryAfterUntil > now) return false;
    return value.circuit !== "open";
  }

  scoreAdjustment(providerId: string, modelId: string): number {
    const value = this.snapshot(providerId, modelId);
    if (value.circuit === "open") return -1_000_000;
    let score = value.circuit === "half_open" ? -100 : 0;
    score -= value.ewmaFailureRate * 200;
    score -= Math.min(100, value.ewmaLatencyMilliseconds / 1000);
    score += Math.min(25, value.successStreak);
    return score;
  }

  acquire(lease: ProviderRouteLease, signal?: AbortSignal): ProviderAdmissionPermit {
    if (signal?.aborted) throw abortError(lease, signal.reason);
    const now = this.clock.now();
    return this.store.transaction(() => {
      this.store.db.prepare("DELETE FROM provider_admission_permits WHERE expires_at <= ?").run(now);
      const current = this.read(lease.providerId, lease.modelId)
        ?? initialHealth(lease.providerId, lease.modelId, now);
      const health = this.advanceCooldown(current, now);
      if (health.retryAfterUntil > now) {
        throw unavailable(lease, "provider route is inside Retry-After window", {
          retryAfterUntil: health.retryAfterUntil,
          healthGeneration: health.generation,
        });
      }
      if (health.circuit === "open") {
        throw unavailable(lease, "provider route circuit is open", {
          cooldownUntil: health.cooldownUntil,
          healthGeneration: health.generation,
        });
      }
      const countRow = this.store.db.prepare(
        "SELECT COUNT(*) AS count FROM provider_admission_permits WHERE provider_id = ?",
      ).get(lease.providerId) as CountRow | undefined;
      if (Number(countRow?.count ?? 0) >= this.maximumInFlight) {
        throw unavailable(lease, "provider admission capacity is exhausted", {
          maximumInFlight: this.maximumInFlight,
          healthGeneration: health.generation,
        }, "retry_same_route");
      }
      const permit: ProviderAdmissionPermit = {
        permitId: this.ids.next("provider_permit"),
        routeId: lease.routeId,
        providerId: lease.providerId,
        modelId: lease.modelId,
        ownerToken: this.ids.next("provider_permit_owner"),
        acquiredAt: now,
        expiresAt: now + this.permitMilliseconds,
        healthGeneration: health.generation,
      };
      if (health.circuit === "half_open") {
        if (health.halfOpenProbeOwner) throw unavailable(
          lease,
          "provider half-open probe is already leased",
          { healthGeneration: health.generation },
          "retry_same_route",
        );
        this.write({ ...health, halfOpenProbeOwner: permit.ownerToken, updatedAt: now });
      }
      this.store.db.prepare(`
        INSERT INTO provider_admission_permits(
          permit_id, route_id, provider_id, model_id, owner_token,
          acquired_at, expires_at, json, checksum
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      `).run(
        permit.permitId,
        permit.routeId,
        permit.providerId,
        permit.modelId,
        permit.ownerToken,
        permit.acquiredAt,
        permit.expiresAt,
        canonicalJson(permit),
        digestJson(permit),
      );
      return deepClone(permit);
    });
  }

  heartbeat(permit: ProviderAdmissionPermit): ProviderAdmissionPermit {
    const now = this.clock.now();
    const next = { ...permit, expiresAt: now + this.permitMilliseconds };
    const result = this.store.db.prepare(`
      UPDATE provider_admission_permits SET expires_at = ?, json = ?, checksum = ?
      WHERE permit_id = ? AND owner_token = ? AND expires_at > ?
    `).run(next.expiresAt, canonicalJson(next), digestJson(next), permit.permitId, permit.ownerToken, now);
    if (Number((result as { changes?: number }).changes ?? 0) !== 1) throw permitLost(permit);
    return deepClone(next);
  }

  release(permit: ProviderAdmissionPermit): boolean {
    return this.store.transaction(() => {
      const result = this.store.db.prepare(
        "DELETE FROM provider_admission_permits WHERE permit_id = ? AND owner_token = ?",
      ).run(permit.permitId, permit.ownerToken);
      const health = this.read(permit.providerId, permit.modelId);
      if (health?.halfOpenProbeOwner === permit.ownerToken) {
        this.write({ ...health, halfOpenProbeOwner: "", updatedAt: this.clock.now() });
      }
      return Number((result as { changes?: number }).changes ?? 0) === 1;
    });
  }

  recordSuccess(
    permit: ProviderAdmissionPermit,
    latencyMilliseconds: number,
    httpStatus: number | null,
  ): RouteHealthSnapshot {
    return this.record(permit, {
      succeeded: true,
      latencyMilliseconds,
      httpStatus,
      failureKind: "",
      retryAfterMilliseconds: null,
    });
  }

  recordFailure(
    permit: ProviderAdmissionPermit,
    input: {
      readonly latencyMilliseconds: number;
      readonly httpStatus: number | null;
      readonly failureKind: string;
      readonly retryAfterMilliseconds: number | null;
    },
  ): RouteHealthSnapshot {
    return this.record(permit, { succeeded: false, ...input });
  }

  private record(
    permit: ProviderAdmissionPermit,
    observation: {
      readonly succeeded: boolean;
      readonly latencyMilliseconds: number;
      readonly httpStatus: number | null;
      readonly failureKind: string;
      readonly retryAfterMilliseconds: number | null;
    },
  ): RouteHealthSnapshot {
    const now = this.clock.now();
    return this.store.transaction(() => {
      const current = this.read(permit.providerId, permit.modelId) ?? initialHealth(permit.providerId, permit.modelId, now);
      const latency = Math.max(0, Number.isFinite(observation.latencyMilliseconds) ? observation.latencyMilliseconds : 0);
      const failureSample = observation.succeeded ? 0 : 1;
      const failureStreak = observation.succeeded ? 0 : current.failureStreak + 1;
      const shouldOpen = !observation.succeeded && (
        current.circuit === "half_open" || failureStreak >= this.failureThreshold
      );
      const next: RouteHealthSnapshot = {
        ...current,
        generation: current.generation + 1,
        circuit: observation.succeeded ? "closed" : shouldOpen ? "open" : current.circuit,
        failureStreak,
        successStreak: observation.succeeded ? current.successStreak + 1 : 0,
        successCount: current.successCount + (observation.succeeded ? 1 : 0),
        failureCount: current.failureCount + (observation.succeeded ? 0 : 1),
        ewmaLatencyMilliseconds: ewma(current.ewmaLatencyMilliseconds, latency, this.alpha),
        ewmaFailureRate: ewma(current.ewmaFailureRate, failureSample, this.alpha),
        lastStatus: observation.httpStatus,
        lastFailureKind: observation.succeeded ? "" : observation.failureKind,
        retryAfterUntil: observation.retryAfterMilliseconds === null
          ? observation.succeeded ? 0 : current.retryAfterUntil
          : now + Math.max(0, observation.retryAfterMilliseconds),
        cooldownUntil: shouldOpen ? now + this.cooldownMilliseconds : observation.succeeded ? 0 : current.cooldownUntil,
        halfOpenProbeOwner: "",
        updatedAt: now,
      };
      this.write(next);
      return deepClone(next);
    });
  }

  private initialize(): void {
    this.store.db.exec(`
      CREATE TABLE IF NOT EXISTS provider_route_health (
        provider_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        circuit TEXT NOT NULL,
        retry_after_until INTEGER NOT NULL,
        cooldown_until INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        PRIMARY KEY(provider_id, model_id)
      );
      CREATE TABLE IF NOT EXISTS provider_admission_permits (
        permit_id TEXT PRIMARY KEY,
        route_id TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        model_id TEXT NOT NULL,
        owner_token TEXT NOT NULL,
        acquired_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        json TEXT NOT NULL,
        checksum TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_provider_admission_capacity
        ON provider_admission_permits(provider_id, expires_at);
    `);
  }

  private advanceCooldown(current: RouteHealthSnapshot, now: number): RouteHealthSnapshot {
    if (current.circuit !== "open" || current.cooldownUntil > now) return deepClone(current);
    const next: RouteHealthSnapshot = {
      ...current,
      generation: current.generation + 1,
      circuit: "half_open",
      halfOpenProbeOwner: "",
      updatedAt: now,
    };
    this.write(next);
    return deepClone(next);
  }

  private read(providerId: string, modelId: string): RouteHealthSnapshot | null {
    const row = this.store.db.prepare(
      "SELECT json FROM provider_route_health WHERE provider_id = ? AND model_id = ?",
    ).get(providerId, modelId) as JsonRow | undefined;
    return row === undefined ? null : this.decode(row.json);
  }

  private write(value: RouteHealthSnapshot): void {
    const json = canonicalJson(value);
    this.store.db.prepare(`
      INSERT INTO provider_route_health(
        provider_id, model_id, generation, circuit, retry_after_until,
        cooldown_until, updated_at, json, checksum
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(provider_id, model_id) DO UPDATE SET
        generation = excluded.generation,
        circuit = excluded.circuit,
        retry_after_until = excluded.retry_after_until,
        cooldown_until = excluded.cooldown_until,
        updated_at = excluded.updated_at,
        json = excluded.json,
        checksum = excluded.checksum
    `).run(
      value.providerId,
      value.modelId,
      value.generation,
      value.circuit,
      value.retryAfterUntil,
      value.cooldownUntil,
      value.updatedAt,
      json,
      digestJson(value),
    );
  }

  private decode(json: string): RouteHealthSnapshot {
    return deepClone(JSON.parse(json) as RouteHealthSnapshot);
  }
}

function initialHealth(providerId: string, modelId: string, now: number): RouteHealthSnapshot {
  return {
    providerId,
    modelId,
    generation: 0,
    circuit: "closed",
    failureStreak: 0,
    successStreak: 0,
    successCount: 0,
    failureCount: 0,
    ewmaLatencyMilliseconds: 0,
    ewmaFailureRate: 0,
    lastStatus: null,
    lastFailureKind: "",
    retryAfterUntil: 0,
    cooldownUntil: 0,
    halfOpenProbeOwner: "",
    updatedAt: now,
  };
}

function ewma(previous: number, sample: number, alpha: number): number {
  if (previous === 0) return sample;
  return alpha * sample + (1 - alpha) * previous;
}

function unavailable(
  lease: ProviderRouteLease,
  message: string,
  detail: JsonRecord,
  recoveryIntent: "change_provider_route" | "retry_same_route" = "change_provider_route",
): ProviderControlPlaneError {
  return new ProviderControlPlaneError({
    layer: "route",
    kind: "provider_unavailable",
    message,
    retryable: true,
    recoveryIntent,
    providerId: lease.providerId,
    modelId: lease.modelId,
    routeId: lease.routeId,
    credentialId: lease.credentialId,
    bytesSent: 0,
    detail,
  });
}

function abortError(lease: ProviderRouteLease, reason: unknown): ProviderControlPlaneError {
  return new ProviderControlPlaneError({
    layer: "transport",
    kind: "request_aborted",
    message: reason instanceof Error ? reason.message : "provider admission was cancelled",
    routeId: lease.routeId,
    providerId: lease.providerId,
    modelId: lease.modelId,
    credentialId: lease.credentialId,
    bytesSent: 0,
  });
}

function permitLost(permit: ProviderAdmissionPermit): ProviderControlPlaneError {
  return new ProviderControlPlaneError({
    layer: "route",
    kind: "route_revision_conflict",
    message: "provider admission permit was lost or fenced",
    routeId: permit.routeId,
    providerId: permit.providerId,
    modelId: permit.modelId,
    detail: { permitId: permit.permitId, healthGeneration: permit.healthGeneration },
  });
}
