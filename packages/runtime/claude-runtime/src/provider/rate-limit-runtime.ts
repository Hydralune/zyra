import {
  type Clock,
  type IdFactory,
  type JsonRecord,
  RandomIdFactory,
  RuntimeInvariantError,
  SystemClock,
  assertFiniteNumber,
  assertNonEmpty,
  assertNonNegativeInteger,
  clamp,
  compareNumbers,
  compareStrings,
  deepClone,
  digestJson,
} from "../core/runtime-primitives.js";

export type RateLimitDimension = "requests" | "input_tokens" | "output_tokens" | "cost";
export type RateLimitScopeKind = "global" | "provider" | "credential" | "model" | "session";

export interface RateLimitDefinition {
  limitId: string;
  scopeKind: RateLimitScopeKind;
  scopeId: string;
  dimension: RateLimitDimension;
  capacity: number;
  refillAmount: number;
  refillIntervalMilliseconds: number;
  burstCapacity: number;
  enabled: boolean;
  priority: number;
  metadata: JsonRecord;
}

export interface RateLimitBucket {
  limitId: string;
  available: number;
  reserved: number;
  consumed: number;
  lastRefillAt: number;
  blockedUntil: number | null;
  providerRemaining: number | null;
  providerResetAt: number | null;
  revision: number;
}

export interface RateLimitDemand {
  requestId: string;
  sessionId: string;
  providerId: string;
  credentialId: string;
  modelId: string;
  requests: number;
  inputTokens: number;
  outputTokens: number;
  estimatedCost: number;
  priority: number;
  deadlineAt: number;
}

export interface RateLimitAllocation {
  limitId: string;
  dimension: RateLimitDimension;
  amount: number;
  availableBefore: number;
  availableAfter: number;
}

export interface RateLimitReservation {
  reservationId: string;
  requestId: string;
  sessionId: string;
  status: "held" | "committed" | "released" | "expired";
  allocations: RateLimitAllocation[];
  acquiredAt: number;
  expiresAt: number;
  finishedAt: number | null;
  actual: Partial<Record<RateLimitDimension, number>>;
  revision: number;
}

export interface RateLimitDecision {
  allowed: boolean;
  requestId: string;
  limitingIds: string[];
  retryAt: number | null;
  allocations: RateLimitAllocation[];
  decisionDigest: string;
}

export interface ProviderRateHeaders {
  remainingRequests?: number | null;
  remainingInputTokens?: number | null;
  remainingOutputTokens?: number | null;
  resetRequestsAt?: number | null;
  resetTokensAt?: number | null;
  retryAfterMilliseconds?: number | null;
}

export interface RateLimitSnapshot {
  version: "zyra.provider-rate-limit/v1";
  definitions: RateLimitDefinition[];
  buckets: RateLimitBucket[];
  reservations: RateLimitReservation[];
  checksum: string;
}

export interface ProviderRateLimitOptions {
  clock?: Clock;
  ids?: IdFactory;
  reservationTtlMilliseconds?: number;
  maximumReservations?: number;
}

export class ProviderRateLimitRuntime {
  private readonly clock: Clock;
  private readonly ids: IdFactory;
  private readonly reservationTtlMilliseconds: number;
  private readonly maximumReservations: number;
  private readonly definitions = new Map<string, RateLimitDefinition>();
  private readonly buckets = new Map<string, RateLimitBucket>();
  private readonly reservations = new Map<string, RateLimitReservation>();
  private readonly requestIndex = new Map<string, string>();

  constructor(options: ProviderRateLimitOptions = {}) {
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.reservationTtlMilliseconds =
      options.reservationTtlMilliseconds ?? 120_000;
    this.maximumReservations = options.maximumReservations ?? 100_000;
    assertNonNegativeInteger(
      this.reservationTtlMilliseconds,
      "reservationTtlMilliseconds",
    );
    assertNonNegativeInteger(this.maximumReservations, "maximumReservations");
  }

  register(definition: RateLimitDefinition): RateLimitDefinition {
    const normalized = normalizeDefinition(definition);
    if (this.definitions.has(normalized.limitId)) {
      throw new RuntimeInvariantError("rate_limit_already_exists", {
        limitId: normalized.limitId,
      });
    }
    this.definitions.set(normalized.limitId, normalized);
    this.buckets.set(normalized.limitId, {
      limitId: normalized.limitId,
      available: normalized.capacity,
      reserved: 0,
      consumed: 0,
      lastRefillAt: this.clock.now(),
      blockedUntil: null,
      providerRemaining: null,
      providerResetAt: null,
      revision: 1,
    });
    return deepClone(normalized);
  }

  update(
    limitId: string,
    update: Partial<Omit<RateLimitDefinition, "limitId" | "scopeKind" | "scopeId" | "dimension">>,
  ): RateLimitDefinition {
    const previous = this.requireDefinition(limitId);
    const next = normalizeDefinition({
      ...previous,
      ...deepClone(update),
      limitId,
    });
    const bucket = this.requireBucket(limitId);
    this.refillOne(next, bucket);
    bucket.available = Math.min(next.burstCapacity, bucket.available);
    bucket.revision += 1;
    this.definitions.set(limitId, next);
    return deepClone(next);
  }

  remove(limitId: string): void {
    const held = [...this.reservations.values()].some(
      (reservation) =>
        reservation.status === "held" &&
        reservation.allocations.some((allocation) => allocation.limitId === limitId),
    );
    if (held) {
      throw new RuntimeInvariantError("rate_limit_has_held_reservation", {
        limitId,
      });
    }
    this.requireDefinition(limitId);
    this.definitions.delete(limitId);
    this.buckets.delete(limitId);
  }

  inspect(demand: RateLimitDemand): RateLimitDecision {
    validateDemand(demand);
    this.expireReservations();
    this.refillAll();
    const allocations = this.computeAllocations(demand);
    const limitingIds: string[] = [];
    let retryAt: number | null = null;
    for (const allocation of allocations) {
      const definition = this.requireDefinition(allocation.limitId);
      const bucket = this.requireBucket(allocation.limitId);
      const blockUntil = effectiveBlockUntil(bucket, this.clock.now());
      if (blockUntil !== null) {
        limitingIds.push(definition.limitId);
        retryAt = retryAt === null ? blockUntil : Math.max(retryAt, blockUntil);
        continue;
      }
      if (allocation.amount > bucket.available) {
        limitingIds.push(definition.limitId);
        const refillAt = estimateRefillAt(
          definition,
          bucket,
          allocation.amount,
          this.clock.now(),
        );
        retryAt = retryAt === null ? refillAt : Math.max(retryAt, refillAt);
      }
    }
    const body = {
      allowed: limitingIds.length === 0,
      requestId: demand.requestId,
      limitingIds: limitingIds.sort(compareStrings),
      retryAt,
      allocations,
    };
    return { ...body, decisionDigest: digestJson(body) };
  }

  reserve(
    demand: RateLimitDemand,
    minimumValidityMilliseconds = 0,
  ): RateLimitReservation {
    assertNonNegativeInteger(
      minimumValidityMilliseconds,
      "minimumValidityMilliseconds",
    );
    const existingId = this.requestIndex.get(demand.requestId);
    if (existingId !== undefined) {
      const existing = this.requireReservation(existingId);
      if (existing.status === "held" || existing.status === "committed") {
        return deepClone(existing);
      }
      throw new RuntimeInvariantError("rate_limit_request_already_finished", {
        requestId: demand.requestId,
        status: existing.status,
      });
    }
    const decision = this.inspect(demand);
    if (!decision.allowed) {
      throw new RuntimeInvariantError("rate_limit_capacity_denied", {
        requestId: demand.requestId,
        limitingIds: decision.limitingIds,
        retryAt: decision.retryAt,
      });
    }
    if (this.reservations.size >= this.maximumReservations) {
      this.trimFinishedReservations();
    }
    if (this.reservations.size >= this.maximumReservations) {
      throw new RuntimeInvariantError("rate_limit_reservation_limit", {
        maximum: this.maximumReservations,
      });
    }
    const applied: RateLimitAllocation[] = [];
    try {
      for (const allocation of decision.allocations) {
        const bucket = this.requireBucket(allocation.limitId);
        if (allocation.amount > bucket.available) {
          throw new RuntimeInvariantError("rate_limit_concurrent_reservation_conflict", {
            limitId: allocation.limitId,
            amount: allocation.amount,
            available: bucket.available,
          });
        }
        const before = bucket.available;
        bucket.available -= allocation.amount;
        bucket.reserved += allocation.amount;
        bucket.revision += 1;
        applied.push({
          ...allocation,
          availableBefore: before,
          availableAfter: bucket.available,
        });
      }
    } catch (error) {
      for (const allocation of applied) {
        const bucket = this.requireBucket(allocation.limitId);
        bucket.available += allocation.amount;
        bucket.reserved -= allocation.amount;
        bucket.revision += 1;
      }
      throw error;
    }
    const acquiredAt = this.clock.now();
    const validityMilliseconds = Math.max(
      this.reservationTtlMilliseconds,
      minimumValidityMilliseconds,
    );
    const reservation: RateLimitReservation = {
      reservationId: this.ids.next("rate-limit-reservation"),
      requestId: demand.requestId,
      sessionId: demand.sessionId,
      status: "held",
      allocations: applied,
      acquiredAt,
      expiresAt: Math.min(
        acquiredAt + validityMilliseconds,
        demand.deadlineAt,
      ),
      finishedAt: null,
      actual: {},
      revision: 1,
    };
    this.reservations.set(reservation.reservationId, reservation);
    this.requestIndex.set(demand.requestId, reservation.reservationId);
    return deepClone(reservation);
  }

  commit(
    reservationId: string,
    actual: Partial<Record<RateLimitDimension, number>>,
  ): RateLimitReservation {
    this.expireReservations();
    const reservation = this.requireReservation(reservationId);
    if (reservation.status === "committed") {
      if (digestJson(reservation.actual) !== digestJson(actual)) {
        throw new RuntimeInvariantError("rate_limit_commit_conflict", {
          reservationId,
        });
      }
      return deepClone(reservation);
    }
    if (reservation.status !== "held" && reservation.status !== "expired") {
      throw new RuntimeInvariantError("rate_limit_reservation_not_settleable", {
        reservationId: reservation.reservationId,
        status: reservation.status,
      });
    }
    validateActual(actual);
    const expiredBeforeCommit = reservation.status === "expired";
    // Expiry returned the estimate to the bucket. A late successful response
    // therefore consumes its actual usage from currently available capacity,
    // while the preflight keeps multi-scope settlement atomic and fail-closed.
    const settlements = reservation.allocations.map((allocation) => {
      const bucket = this.requireBucket(allocation.limitId);
      const consumed = actual[allocation.dimension] ?? allocation.amount;
      if (expiredBeforeCommit) {
        if (consumed > bucket.available) {
          throw new RuntimeInvariantError(
            "rate_limit_late_commit_capacity_conflict",
            {
              reservationId,
              limitId: allocation.limitId,
              actual: consumed,
              available: bucket.available,
            },
          );
        }
      } else if (consumed > allocation.amount) {
        const extra = consumed - allocation.amount;
        if (extra > bucket.available) {
          throw new RuntimeInvariantError("rate_limit_actual_exceeds_capacity", {
            reservationId,
            limitId: allocation.limitId,
            reserved: allocation.amount,
            actual: consumed,
            available: bucket.available,
          });
        }
      }
      return { allocation, bucket, consumed };
    });
    for (const { allocation, bucket, consumed } of settlements) {
      bucket.available += expiredBeforeCommit
        ? -consumed
        : allocation.amount - consumed;
      if (!expiredBeforeCommit) {
        bucket.reserved = Math.max(0, bucket.reserved - allocation.amount);
      }
      bucket.consumed += consumed;
      bucket.revision += 1;
    }
    reservation.status = "committed";
    reservation.finishedAt = this.clock.now();
    reservation.actual = deepClone(actual);
    reservation.revision += 1;
    return deepClone(reservation);
  }

  release(reservationId: string): RateLimitReservation {
    const reservation = this.requireReservation(reservationId);
    if (reservation.status === "released" || reservation.status === "expired") {
      return deepClone(reservation);
    }
    this.assertHeld(reservation);
    this.releaseAllocations(reservation);
    reservation.status = "released";
    reservation.finishedAt = this.clock.now();
    reservation.revision += 1;
    return deepClone(reservation);
  }

  applyProviderHeaders(input: {
    providerId: string;
    credentialId: string;
    modelId: string;
    headers: ProviderRateHeaders;
  }): string[] {
    const now = this.clock.now();
    const updated: string[] = [];
    for (const definition of this.definitions.values()) {
      if (!matchesProviderScope(definition, input)) {
        continue;
      }
      const remaining = providerRemainingForDimension(
        definition.dimension,
        input.headers,
      );
      const resetAt = providerResetForDimension(
        definition.dimension,
        input.headers,
      );
      const bucket = this.requireBucket(definition.limitId);
      if (remaining !== null) {
        assertFiniteNumber(remaining, "providerRemaining");
        bucket.providerRemaining = Math.max(0, remaining);
        bucket.available = Math.min(bucket.available, bucket.providerRemaining);
      }
      if (resetAt !== null) {
        bucket.providerResetAt = resetAt;
      }
      const retryAfter = input.headers.retryAfterMilliseconds ?? null;
      if (retryAfter !== null) {
        assertNonNegativeInteger(retryAfter, "retryAfterMilliseconds");
        bucket.blockedUntil = now + retryAfter;
      }
      bucket.revision += 1;
      updated.push(definition.limitId);
    }
    return updated.sort(compareStrings);
  }

  block(limitId: string, until: number, reason: string): RateLimitBucket {
    const bucket = this.requireBucket(limitId);
    assertNonNegativeInteger(until, "until");
    assertNonEmpty(reason, "reason");
    bucket.blockedUntil = Math.max(bucket.blockedUntil ?? 0, until);
    bucket.revision += 1;
    const definition = this.requireDefinition(limitId);
    definition.metadata = { ...definition.metadata, lastBlockReason: reason };
    return deepClone(bucket);
  }

  getBucket(limitId: string): RateLimitBucket {
    const definition = this.requireDefinition(limitId);
    const bucket = this.requireBucket(limitId);
    this.refillOne(definition, bucket);
    return deepClone(bucket);
  }

  snapshot(): RateLimitSnapshot {
    this.expireReservations();
    this.refillAll();
    const body = {
      version: "zyra.provider-rate-limit/v1" as const,
      definitions: [...this.definitions.values()]
        .sort((left, right) => compareStrings(left.limitId, right.limitId))
        .map((definition) => deepClone(definition)),
      buckets: [...this.buckets.values()]
        .sort((left, right) => compareStrings(left.limitId, right.limitId))
        .map((bucket) => deepClone(bucket)),
      reservations: [...this.reservations.values()]
        .sort((left, right) =>
          compareStrings(left.reservationId, right.reservationId),
        )
        .map((reservation) => deepClone(reservation)),
    };
    return { ...body, checksum: digestJson(body) };
  }

  restore(snapshot: RateLimitSnapshot): void {
    const { checksum, ...body } = snapshot;
    if (snapshot.version !== "zyra.provider-rate-limit/v1") {
      throw new RuntimeInvariantError("unsupported_rate_limit_snapshot", {
        version: snapshot.version,
      });
    }
    if (digestJson(body) !== checksum) {
      throw new RuntimeInvariantError("rate_limit_snapshot_checksum_mismatch");
    }
    this.definitions.clear();
    this.buckets.clear();
    this.reservations.clear();
    this.requestIndex.clear();
    for (const source of snapshot.definitions) {
      const definition = normalizeDefinition(source);
      this.definitions.set(definition.limitId, definition);
    }
    for (const bucket of snapshot.buckets) {
      if (!this.definitions.has(bucket.limitId)) {
        throw new RuntimeInvariantError("rate_bucket_without_definition", {
          limitId: bucket.limitId,
        });
      }
      this.buckets.set(bucket.limitId, deepClone(bucket));
    }
    for (const reservation of snapshot.reservations) {
      for (const allocation of reservation.allocations) {
        if (!this.buckets.has(allocation.limitId)) {
          throw new RuntimeInvariantError("rate_reservation_without_bucket", {
            reservationId: reservation.reservationId,
            limitId: allocation.limitId,
          });
        }
      }
      this.reservations.set(reservation.reservationId, deepClone(reservation));
      if (this.requestIndex.has(reservation.requestId)) {
        throw new RuntimeInvariantError("duplicate_rate_request_snapshot", {
          requestId: reservation.requestId,
        });
      }
      this.requestIndex.set(reservation.requestId, reservation.reservationId);
    }
    this.expireReservations();
    this.refillAll();
  }

  private computeAllocations(demand: RateLimitDemand): RateLimitAllocation[] {
    const allocations: RateLimitAllocation[] = [];
    for (const definition of this.definitions.values()) {
      if (!definition.enabled || !matchesDemandScope(definition, demand)) {
        continue;
      }
      const amount = demandForDimension(definition.dimension, demand);
      const bucket = this.requireBucket(definition.limitId);
      allocations.push({
        limitId: definition.limitId,
        dimension: definition.dimension,
        amount,
        availableBefore: bucket.available,
        availableAfter: bucket.available - amount,
      });
    }
    return allocations.sort(
      (left, right) =>
        compareStrings(left.limitId, right.limitId) ||
        compareStrings(left.dimension, right.dimension),
    );
  }

  private refillAll(): void {
    for (const definition of this.definitions.values()) {
      this.refillOne(definition, this.requireBucket(definition.limitId));
    }
  }

  private refillOne(
    definition: RateLimitDefinition,
    bucket: RateLimitBucket,
  ): void {
    const now = this.clock.now();
    if (definition.refillIntervalMilliseconds === 0) {
      return;
    }
    const intervals = Math.floor(
      (now - bucket.lastRefillAt) / definition.refillIntervalMilliseconds,
    );
    if (intervals <= 0) {
      return;
    }
    const refill = intervals * definition.refillAmount;
    const providerCeiling =
      bucket.providerRemaining === null
        ? definition.burstCapacity
        : Math.min(definition.burstCapacity, bucket.providerRemaining);
    bucket.available = clamp(bucket.available + refill, 0, providerCeiling);
    bucket.lastRefillAt += intervals * definition.refillIntervalMilliseconds;
    if (bucket.providerResetAt !== null && bucket.providerResetAt <= now) {
      bucket.providerRemaining = null;
      bucket.providerResetAt = null;
      bucket.available = Math.min(definition.burstCapacity, bucket.available + refill);
    }
    if (bucket.blockedUntil !== null && bucket.blockedUntil <= now) {
      bucket.blockedUntil = null;
    }
    bucket.revision += 1;
  }

  private expireReservations(): void {
    const now = this.clock.now();
    for (const reservation of this.reservations.values()) {
      if (reservation.status === "held" && reservation.expiresAt <= now) {
        this.releaseAllocations(reservation);
        reservation.status = "expired";
        reservation.finishedAt = now;
        reservation.revision += 1;
      }
    }
  }

  private releaseAllocations(reservation: RateLimitReservation): void {
    for (const allocation of reservation.allocations) {
      const bucket = this.requireBucket(allocation.limitId);
      bucket.available += allocation.amount;
      bucket.reserved = Math.max(0, bucket.reserved - allocation.amount);
      bucket.revision += 1;
    }
  }

  private trimFinishedReservations(): void {
    const finished = [...this.reservations.values()]
      .filter((reservation) => reservation.status !== "held")
      .sort(
        (left, right) =>
          compareNumbers(left.finishedAt ?? 0, right.finishedAt ?? 0) ||
          compareStrings(left.reservationId, right.reservationId),
      );
    const target = Math.max(1, Math.ceil(this.maximumReservations * 0.1));
    for (const reservation of finished.slice(0, target)) {
      this.reservations.delete(reservation.reservationId);
      this.requestIndex.delete(reservation.requestId);
    }
  }

  private requireDefinition(limitId: string): RateLimitDefinition {
    const definition = this.definitions.get(limitId);
    if (definition === undefined) {
      throw new RuntimeInvariantError("unknown_rate_limit", { limitId });
    }
    return definition;
  }

  private requireBucket(limitId: string): RateLimitBucket {
    const bucket = this.buckets.get(limitId);
    if (bucket === undefined) {
      throw new RuntimeInvariantError("unknown_rate_limit_bucket", { limitId });
    }
    return bucket;
  }

  private requireReservation(reservationId: string): RateLimitReservation {
    const reservation = this.reservations.get(reservationId);
    if (reservation === undefined) {
      throw new RuntimeInvariantError("unknown_rate_limit_reservation", {
        reservationId,
      });
    }
    return reservation;
  }

  private assertHeld(reservation: RateLimitReservation): void {
    if (reservation.status !== "held") {
      throw new RuntimeInvariantError("rate_limit_reservation_not_held", {
        reservationId: reservation.reservationId,
        status: reservation.status,
      });
    }
  }
}

function normalizeDefinition(definition: RateLimitDefinition): RateLimitDefinition {
  assertNonEmpty(definition.limitId, "limitId");
  assertNonEmpty(definition.scopeId, "scopeId");
  assertFiniteNumber(definition.capacity, "capacity");
  assertFiniteNumber(definition.refillAmount, "refillAmount");
  assertNonNegativeInteger(
    definition.refillIntervalMilliseconds,
    "refillIntervalMilliseconds",
  );
  assertFiniteNumber(definition.burstCapacity, "burstCapacity");
  if (
    definition.capacity < 0 ||
    definition.refillAmount < 0 ||
    definition.burstCapacity < definition.capacity
  ) {
    throw new RuntimeInvariantError("invalid_rate_limit_capacity", {
      limitId: definition.limitId,
      capacity: definition.capacity,
      refillAmount: definition.refillAmount,
      burstCapacity: definition.burstCapacity,
    });
  }
  return deepClone(definition);
}

function validateDemand(demand: RateLimitDemand): void {
  assertNonEmpty(demand.requestId, "requestId");
  assertNonEmpty(demand.sessionId, "sessionId");
  assertNonEmpty(demand.providerId, "providerId");
  assertNonEmpty(demand.credentialId, "credentialId");
  assertNonEmpty(demand.modelId, "modelId");
  assertFiniteNumber(demand.requests, "requests");
  assertFiniteNumber(demand.inputTokens, "inputTokens");
  assertFiniteNumber(demand.outputTokens, "outputTokens");
  assertFiniteNumber(demand.estimatedCost, "estimatedCost");
  assertNonNegativeInteger(demand.deadlineAt, "deadlineAt");
  if (
    demand.requests < 0 ||
    demand.inputTokens < 0 ||
    demand.outputTokens < 0 ||
    demand.estimatedCost < 0
  ) {
    throw new RuntimeInvariantError("negative_rate_limit_demand", {
      requestId: demand.requestId,
    });
  }
}

function validateActual(
  actual: Partial<Record<RateLimitDimension, number>>,
): void {
  for (const [dimension, amount] of Object.entries(actual)) {
    if (amount === undefined) {
      continue;
    }
    assertFiniteNumber(amount, `actual.${dimension}`);
    if (amount < 0) {
      throw new RuntimeInvariantError("negative_rate_limit_actual", {
        dimension,
        amount,
      });
    }
  }
}

function matchesDemandScope(
  definition: RateLimitDefinition,
  demand: RateLimitDemand,
): boolean {
  if (definition.scopeKind === "global") {
    return true;
  }
  if (definition.scopeKind === "provider") {
    return definition.scopeId === demand.providerId;
  }
  if (definition.scopeKind === "credential") {
    return definition.scopeId === demand.credentialId;
  }
  if (definition.scopeKind === "model") {
    return definition.scopeId === demand.modelId;
  }
  return definition.scopeId === demand.sessionId;
}

function matchesProviderScope(
  definition: RateLimitDefinition,
  input: { providerId: string; credentialId: string; modelId: string },
): boolean {
  return (
    definition.scopeKind === "global" ||
    (definition.scopeKind === "provider" && definition.scopeId === input.providerId) ||
    (definition.scopeKind === "credential" &&
      definition.scopeId === input.credentialId) ||
    (definition.scopeKind === "model" && definition.scopeId === input.modelId)
  );
}

function demandForDimension(
  dimension: RateLimitDimension,
  demand: RateLimitDemand,
): number {
  if (dimension === "requests") {
    return demand.requests;
  }
  if (dimension === "input_tokens") {
    return demand.inputTokens;
  }
  if (dimension === "output_tokens") {
    return demand.outputTokens;
  }
  return demand.estimatedCost;
}

function effectiveBlockUntil(
  bucket: RateLimitBucket,
  now: number,
): number | null {
  const candidates = [bucket.blockedUntil, bucket.providerResetAt].filter(
    (value): value is number => value !== null && value > now,
  );
  return candidates.length === 0 ? null : Math.max(...candidates);
}

function estimateRefillAt(
  definition: RateLimitDefinition,
  bucket: RateLimitBucket,
  required: number,
  now: number,
): number {
  if (definition.refillAmount === 0 || definition.refillIntervalMilliseconds === 0) {
    return Number.MAX_SAFE_INTEGER;
  }
  const deficit = Math.max(0, required - bucket.available);
  const intervals = Math.ceil(deficit / definition.refillAmount);
  return Math.max(now, bucket.lastRefillAt) +
    intervals * definition.refillIntervalMilliseconds;
}

function providerRemainingForDimension(
  dimension: RateLimitDimension,
  headers: ProviderRateHeaders,
): number | null {
  if (dimension === "requests") {
    return headers.remainingRequests ?? null;
  }
  if (dimension === "input_tokens") {
    return headers.remainingInputTokens ?? null;
  }
  if (dimension === "output_tokens") {
    return headers.remainingOutputTokens ?? null;
  }
  return null;
}

function providerResetForDimension(
  dimension: RateLimitDimension,
  headers: ProviderRateHeaders,
): number | null {
  if (dimension === "requests") {
    return headers.resetRequestsAt ?? null;
  }
  if (dimension === "input_tokens" || dimension === "output_tokens") {
    return headers.resetTokensAt ?? null;
  }
  return null;
}
