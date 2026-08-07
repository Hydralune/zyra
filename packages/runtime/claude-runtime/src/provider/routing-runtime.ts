import {
  type Clock,
  type JsonRecord,
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
  uniqueSorted,
} from "../core/runtime-primitives.js";

export type ProviderRouteStatus = "available" | "degraded" | "open" | "disabled";
export type ProviderFailureClass =
  | "authentication"
  | "rate_limit"
  | "timeout"
  | "overloaded"
  | "invalid_request"
  | "content_policy"
  | "transport"
  | "unknown";

export interface ProviderRouteDefinition {
  routeId: string;
  providerId: string;
  modelId: string;
  endpointId: string;
  region: string;
  capabilities: string[];
  privacyClasses: string[];
  maximumContextTokens: number;
  maximumOutputTokens: number;
  inputCostPerMillion: number;
  outputCostPerMillion: number;
  baseLatencyMilliseconds: number;
  concurrencyLimit: number;
  priority: number;
  enabled: boolean;
  metadata: JsonRecord;
}

export interface ProviderRouteState {
  routeId: string;
  status: ProviderRouteStatus;
  inFlight: number;
  consecutiveFailures: number;
  successes: number;
  failures: number;
  latencyEwmaMilliseconds: number;
  availabilityEwma: number;
  openUntil: number | null;
  lastSelectedAt: number | null;
  lastSuccessAt: number | null;
  lastFailureAt: number | null;
  lastFailureClass: ProviderFailureClass | null;
  revision: number;
}

export interface ProviderRouteRequest {
  requestId: string;
  sessionId: string;
  requiredCapabilities: string[];
  privacyClass: string;
  estimatedInputTokens: number;
  requestedOutputTokens: number;
  maximumCost: number | null;
  maximumLatencyMilliseconds: number | null;
  preferredProviderIds: string[];
  excludedRouteIds: string[];
  stickyRouteId: string | null;
  allowDegraded: boolean;
  metadata: JsonRecord;
}

export interface ProviderRouteScore {
  routeId: string;
  eligible: boolean;
  score: number;
  reasons: string[];
  estimatedCost: number;
  estimatedLatencyMilliseconds: number;
  utilization: number;
  availability: number;
}

export interface ProviderRouteDecision {
  requestId: string;
  routeId: string;
  providerId: string;
  modelId: string;
  endpointId: string;
  selectedAt: number;
  score: number;
  estimatedCost: number;
  estimatedLatencyMilliseconds: number;
  fallbackRouteIds: string[];
  decisionDigest: string;
}

export interface ProviderRouteLease {
  requestId: string;
  routeId: string;
  acquiredAt: number;
  expiresAt: number;
  releasedAt: number | null;
}

export interface ProviderRouteWeights {
  priority: number;
  availability: number;
  latency: number;
  cost: number;
  utilization: number;
  preference: number;
  sticky: number;
}

export interface ProviderRoutingSnapshot {
  version: "zyra.provider-routing/v1";
  revision: number;
  definitions: ProviderRouteDefinition[];
  states: ProviderRouteState[];
  leases: ProviderRouteLease[];
  sessionAffinity: Array<{ sessionId: string; routeId: string }>;
  checksum: string;
}

export interface ProviderRoutingOptions {
  clock?: Clock;
  weights?: Partial<ProviderRouteWeights>;
  leaseMilliseconds?: number;
  circuitFailureThreshold?: number;
  circuitBaseOpenMilliseconds?: number;
  latencyEwmaAlpha?: number;
  availabilityEwmaAlpha?: number;
}

const DEFAULT_WEIGHTS: ProviderRouteWeights = {
  priority: 0.15,
  availability: 0.3,
  latency: 0.18,
  cost: 0.15,
  utilization: 0.12,
  preference: 0.05,
  sticky: 0.05,
};

export class ProviderRoutingRuntime {
  private readonly clock: Clock;
  private readonly weights: ProviderRouteWeights;
  private readonly leaseMilliseconds: number;
  private readonly circuitFailureThreshold: number;
  private readonly circuitBaseOpenMilliseconds: number;
  private readonly latencyEwmaAlpha: number;
  private readonly availabilityEwmaAlpha: number;
  private readonly definitions = new Map<string, ProviderRouteDefinition>();
  private readonly states = new Map<string, ProviderRouteState>();
  private readonly leases = new Map<string, ProviderRouteLease>();
  private readonly sessionAffinity = new Map<string, string>();
  private revision = 0;

  constructor(options: ProviderRoutingOptions = {}) {
    this.clock = options.clock ?? new SystemClock();
    this.weights = { ...DEFAULT_WEIGHTS, ...options.weights };
    // A route lease is held for one provider request and released by
    // recordSuccess/recordFailure.  A streaming completion near the output
    // token ceiling can run for several minutes, so a 120s lease expired
    // mid-stream and surfaced as unknown_provider_route_lease.  This matches
    // the sibling provider-control-plane routing default.
    this.leaseMilliseconds = options.leaseMilliseconds ?? 15 * 60_000;
    this.circuitFailureThreshold = options.circuitFailureThreshold ?? 4;
    this.circuitBaseOpenMilliseconds =
      options.circuitBaseOpenMilliseconds ?? 5_000;
    this.latencyEwmaAlpha = options.latencyEwmaAlpha ?? 0.2;
    this.availabilityEwmaAlpha = options.availabilityEwmaAlpha ?? 0.1;
    assertNonNegativeInteger(this.leaseMilliseconds, "leaseMilliseconds");
    assertNonNegativeInteger(
      this.circuitFailureThreshold,
      "circuitFailureThreshold",
    );
    assertNonNegativeInteger(
      this.circuitBaseOpenMilliseconds,
      "circuitBaseOpenMilliseconds",
    );
    assertUnitInterval(this.latencyEwmaAlpha, "latencyEwmaAlpha");
    assertUnitInterval(this.availabilityEwmaAlpha, "availabilityEwmaAlpha");
    const weightTotal = Object.values(this.weights).reduce(
      (sum, value) => sum + value,
      0,
    );
    if (weightTotal <= 0) {
      throw new RuntimeInvariantError("provider_route_weights_empty");
    }
  }

  register(definition: ProviderRouteDefinition): ProviderRouteDefinition {
    const normalized = normalizeDefinition(definition);
    if (this.definitions.has(normalized.routeId)) {
      throw new RuntimeInvariantError("provider_route_already_exists", {
        routeId: normalized.routeId,
      });
    }
    this.definitions.set(normalized.routeId, normalized);
    this.states.set(normalized.routeId, createInitialState(normalized));
    this.revision += 1;
    return deepClone(normalized);
  }

  update(
    routeId: string,
    update: Partial<Omit<ProviderRouteDefinition, "routeId">>,
  ): ProviderRouteDefinition {
    const previous = this.requireDefinition(routeId);
    const normalized = normalizeDefinition({
      ...previous,
      ...deepClone(update),
      routeId,
    });
    this.definitions.set(routeId, normalized);
    const state = this.requireState(routeId);
    if (!normalized.enabled) {
      this.states.set(routeId, {
        ...state,
        status: "disabled",
        revision: state.revision + 1,
      });
    } else if (state.status === "disabled") {
      this.states.set(routeId, {
        ...state,
        status: "available",
        revision: state.revision + 1,
      });
    }
    this.revision += 1;
    return deepClone(normalized);
  }

  unregister(routeId: string): void {
    const state = this.requireState(routeId);
    if (state.inFlight > 0) {
      throw new RuntimeInvariantError("provider_route_in_flight", {
        routeId,
        inFlight: state.inFlight,
      });
    }
    this.definitions.delete(routeId);
    this.states.delete(routeId);
    for (const [sessionId, affinity] of this.sessionAffinity) {
      if (affinity === routeId) {
        this.sessionAffinity.delete(sessionId);
      }
    }
    this.revision += 1;
  }

  score(request: ProviderRouteRequest): ProviderRouteScore[] {
    validateRouteRequest(request);
    this.expireCircuitsAndLeases();
    const preferred = new Set(request.preferredProviderIds);
    const excluded = new Set(request.excludedRouteIds);
    const sticky =
      request.stickyRouteId ?? this.sessionAffinity.get(request.sessionId) ?? null;
    const scores = [...this.definitions.values()].map((definition) => {
      const state = this.requireState(definition.routeId);
      const reasons: string[] = [];
      let eligible = true;
      if (!definition.enabled || state.status === "disabled") {
        eligible = false;
        reasons.push("disabled");
      }
      if (state.status === "open") {
        eligible = false;
        reasons.push("circuit_open");
      }
      if (state.status === "degraded" && !request.allowDegraded) {
        eligible = false;
        reasons.push("degraded_disallowed");
      }
      if (excluded.has(definition.routeId)) {
        eligible = false;
        reasons.push("explicitly_excluded");
      }
      const missingCapabilities = request.requiredCapabilities.filter(
        (capability) => !definition.capabilities.includes(capability),
      );
      if (missingCapabilities.length > 0) {
        eligible = false;
        reasons.push(`missing_capability:${missingCapabilities.join(",")}`);
      }
      if (!definition.privacyClasses.includes(request.privacyClass)) {
        eligible = false;
        reasons.push("privacy_class_mismatch");
      }
      if (request.estimatedInputTokens > definition.maximumContextTokens) {
        eligible = false;
        reasons.push("context_limit_exceeded");
      }
      if (request.requestedOutputTokens > definition.maximumOutputTokens) {
        eligible = false;
        reasons.push("output_limit_exceeded");
      }
      const estimatedCost = estimateCost(definition, request);
      if (
        request.maximumCost !== null &&
        estimatedCost > request.maximumCost
      ) {
        eligible = false;
        reasons.push("cost_limit_exceeded");
      }
      const utilization =
        definition.concurrencyLimit === 0
          ? 1
          : state.inFlight / definition.concurrencyLimit;
      if (state.inFlight >= definition.concurrencyLimit) {
        eligible = false;
        reasons.push("concurrency_saturated");
      }
      const estimatedLatency =
        Math.max(definition.baseLatencyMilliseconds, state.latencyEwmaMilliseconds) *
        (1 + utilization);
      if (
        request.maximumLatencyMilliseconds !== null &&
        estimatedLatency > request.maximumLatencyMilliseconds
      ) {
        eligible = false;
        reasons.push("latency_limit_exceeded");
      }
      const priorityScore = normalizePriority(definition.priority);
      const availabilityScore = clamp(state.availabilityEwma, 0, 1);
      const latencyScore = 1 / (1 + estimatedLatency / 1_000);
      const costScore = 1 / (1 + estimatedCost);
      const utilizationScore = 1 - clamp(utilization, 0, 1);
      const preferenceScore = preferred.has(definition.providerId) ? 1 : 0;
      const stickyScore = sticky === definition.routeId ? 1 : 0;
      const score =
        this.weights.priority * priorityScore +
        this.weights.availability * availabilityScore +
        this.weights.latency * latencyScore +
        this.weights.cost * costScore +
        this.weights.utilization * utilizationScore +
        this.weights.preference * preferenceScore +
        this.weights.sticky * stickyScore;
      return {
        routeId: definition.routeId,
        eligible,
        score: eligible ? score : Number.NEGATIVE_INFINITY,
        reasons,
        estimatedCost,
        estimatedLatencyMilliseconds: estimatedLatency,
        utilization,
        availability: state.availabilityEwma,
      };
    });
    return scores.sort(routeScoreComparator);
  }

  decide(request: ProviderRouteRequest): ProviderRouteDecision {
    const scores = this.score(request);
    const selected = scores.find((score) => score.eligible);
    if (selected === undefined) {
      throw new RuntimeInvariantError("no_eligible_provider_route", {
        requestId: request.requestId,
        rejected: scores.map((score) => ({
          routeId: score.routeId,
          reasons: score.reasons,
        })),
      });
    }
    const definition = this.requireDefinition(selected.routeId);
    const selectedAt = this.clock.now();
    const fallbackRouteIds = scores
      .filter((score) => score.eligible && score.routeId !== selected.routeId)
      .map((score) => score.routeId);
    const body = {
      requestId: request.requestId,
      routeId: definition.routeId,
      providerId: definition.providerId,
      modelId: definition.modelId,
      endpointId: definition.endpointId,
      selectedAt,
      score: selected.score,
      estimatedCost: selected.estimatedCost,
      estimatedLatencyMilliseconds: selected.estimatedLatencyMilliseconds,
      fallbackRouteIds,
    };
    return { ...body, decisionDigest: digestJson(body) };
  }

  acquire(decision: ProviderRouteDecision): ProviderRouteLease {
    if (this.leases.has(decision.requestId)) {
      const existing = this.leases.get(decision.requestId);
      if (existing?.routeId !== decision.routeId) {
        throw new RuntimeInvariantError("provider_request_route_conflict", {
          requestId: decision.requestId,
          existingRouteId: existing?.routeId ?? null,
          requestedRouteId: decision.routeId,
        });
      }
      return deepClone(existing);
    }
    const definition = this.requireDefinition(decision.routeId);
    const state = this.requireState(decision.routeId);
    if (state.status === "open" || state.status === "disabled") {
      throw new RuntimeInvariantError("provider_route_not_acquirable", {
        routeId: decision.routeId,
        status: state.status,
      });
    }
    if (state.inFlight >= definition.concurrencyLimit) {
      throw new RuntimeInvariantError("provider_route_capacity_exhausted", {
        routeId: decision.routeId,
      });
    }
    const acquiredAt = this.clock.now();
    const lease: ProviderRouteLease = {
      requestId: decision.requestId,
      routeId: decision.routeId,
      acquiredAt,
      expiresAt: acquiredAt + this.leaseMilliseconds,
      releasedAt: null,
    };
    this.leases.set(decision.requestId, lease);
    this.states.set(decision.routeId, {
      ...state,
      inFlight: state.inFlight + 1,
      lastSelectedAt: acquiredAt,
      revision: state.revision + 1,
    });
    this.revision += 1;
    return deepClone(lease);
  }

  recordSuccess(
    requestId: string,
    latencyMilliseconds: number,
    sessionId?: string,
  ): ProviderRouteState {
    assertNonNegativeInteger(latencyMilliseconds, "latencyMilliseconds");
    const lease = this.requireActiveLease(requestId);
    const state = this.requireState(lease.routeId);
    const now = this.clock.now();
    const next = {
      ...state,
      status: "available" as const,
      inFlight: Math.max(0, state.inFlight - 1),
      consecutiveFailures: 0,
      successes: state.successes + 1,
      latencyEwmaMilliseconds: ewma(
        state.latencyEwmaMilliseconds,
        latencyMilliseconds,
        this.latencyEwmaAlpha,
      ),
      availabilityEwma: ewma(
        state.availabilityEwma,
        1,
        this.availabilityEwmaAlpha,
      ),
      openUntil: null,
      lastSuccessAt: now,
      lastFailureClass: null,
      revision: state.revision + 1,
    };
    this.states.set(lease.routeId, next);
    this.releaseLease(lease, now);
    if (sessionId !== undefined) {
      this.sessionAffinity.set(sessionId, lease.routeId);
    }
    this.revision += 1;
    return deepClone(next);
  }

  recordFailure(
    requestId: string,
    failureClass: ProviderFailureClass,
  ): ProviderRouteState {
    const lease = this.requireActiveLease(requestId);
    const state = this.requireState(lease.routeId);
    const failures = state.consecutiveFailures + 1;
    const opensCircuit =
      failures >= this.circuitFailureThreshold ||
      failureClass === "authentication";
    const now = this.clock.now();
    const openDuration =
      this.circuitBaseOpenMilliseconds *
      Math.min(64, 2 ** Math.max(0, failures - this.circuitFailureThreshold));
    const next: ProviderRouteState = {
      ...state,
      status: opensCircuit ? "open" : "degraded",
      inFlight: Math.max(0, state.inFlight - 1),
      consecutiveFailures: failures,
      failures: state.failures + 1,
      availabilityEwma: ewma(
        state.availabilityEwma,
        0,
        this.availabilityEwmaAlpha,
      ),
      openUntil: opensCircuit ? now + openDuration : null,
      lastFailureAt: now,
      lastFailureClass: failureClass,
      revision: state.revision + 1,
    };
    this.states.set(lease.routeId, next);
    this.releaseLease(lease, now);
    this.revision += 1;
    return deepClone(next);
  }

  abandon(requestId: string): void {
    const lease = this.leases.get(requestId);
    if (lease === undefined || lease.releasedAt !== null) {
      return;
    }
    const state = this.requireState(lease.routeId);
    this.states.set(lease.routeId, {
      ...state,
      inFlight: Math.max(0, state.inFlight - 1),
      revision: state.revision + 1,
    });
    this.releaseLease(lease, this.clock.now());
    this.revision += 1;
  }

  forceStatus(
    routeId: string,
    status: ProviderRouteStatus,
    openUntil: number | null = null,
  ): ProviderRouteState {
    const state = this.requireState(routeId);
    if (status === "open" && openUntil === null) {
      throw new RuntimeInvariantError("open_route_requires_deadline", {
        routeId,
      });
    }
    const next = {
      ...state,
      status,
      openUntil: status === "open" ? openUntil : null,
      revision: state.revision + 1,
    };
    this.states.set(routeId, next);
    this.revision += 1;
    return deepClone(next);
  }

  clearAffinity(sessionId: string): void {
    this.sessionAffinity.delete(sessionId);
  }

  getDefinition(routeId: string): ProviderRouteDefinition {
    return deepClone(this.requireDefinition(routeId));
  }

  getState(routeId: string): ProviderRouteState {
    this.expireCircuitsAndLeases();
    return deepClone(this.requireState(routeId));
  }

  listStates(): ProviderRouteState[] {
    this.expireCircuitsAndLeases();
    return [...this.states.values()]
      .sort((left, right) => compareStrings(left.routeId, right.routeId))
      .map((state) => deepClone(state));
  }

  snapshot(): ProviderRoutingSnapshot {
    this.expireCircuitsAndLeases();
    const body = {
      version: "zyra.provider-routing/v1" as const,
      revision: this.revision,
      definitions: [...this.definitions.values()]
        .sort((left, right) => compareStrings(left.routeId, right.routeId))
        .map((definition) => deepClone(definition)),
      states: this.listStates(),
      leases: [...this.leases.values()]
        .sort((left, right) => compareStrings(left.requestId, right.requestId))
        .map((lease) => deepClone(lease)),
      sessionAffinity: [...this.sessionAffinity.entries()]
        .sort(([left], [right]) => compareStrings(left, right))
        .map(([sessionId, routeId]) => ({ sessionId, routeId })),
    };
    return { ...body, checksum: digestJson(body) };
  }

  restore(snapshot: ProviderRoutingSnapshot): void {
    const { checksum, ...body } = snapshot;
    if (snapshot.version !== "zyra.provider-routing/v1") {
      throw new RuntimeInvariantError("unsupported_provider_routing_snapshot", {
        version: snapshot.version,
      });
    }
    if (digestJson(body) !== checksum) {
      throw new RuntimeInvariantError("provider_routing_snapshot_checksum_mismatch");
    }
    this.definitions.clear();
    this.states.clear();
    this.leases.clear();
    this.sessionAffinity.clear();
    for (const source of snapshot.definitions) {
      const definition = normalizeDefinition(source);
      this.definitions.set(definition.routeId, definition);
    }
    for (const source of snapshot.states) {
      if (!this.definitions.has(source.routeId)) {
        throw new RuntimeInvariantError("route_state_without_definition", {
          routeId: source.routeId,
        });
      }
      this.states.set(source.routeId, deepClone(source));
    }
    for (const lease of snapshot.leases) {
      if (!this.states.has(lease.routeId)) {
        throw new RuntimeInvariantError("route_lease_without_state", {
          routeId: lease.routeId,
        });
      }
      this.leases.set(lease.requestId, deepClone(lease));
    }
    for (const affinity of snapshot.sessionAffinity) {
      if (this.definitions.has(affinity.routeId)) {
        this.sessionAffinity.set(affinity.sessionId, affinity.routeId);
      }
    }
    this.revision = snapshot.revision;
    this.expireCircuitsAndLeases();
  }

  private requireDefinition(routeId: string): ProviderRouteDefinition {
    const definition = this.definitions.get(routeId);
    if (definition === undefined) {
      throw new RuntimeInvariantError("unknown_provider_route", { routeId });
    }
    return definition;
  }

  private requireState(routeId: string): ProviderRouteState {
    const state = this.states.get(routeId);
    if (state === undefined) {
      throw new RuntimeInvariantError("unknown_provider_route_state", {
        routeId,
      });
    }
    return state;
  }

  private requireActiveLease(requestId: string): ProviderRouteLease {
    this.expireCircuitsAndLeases();
    const lease = this.leases.get(requestId);
    if (lease === undefined || lease.releasedAt !== null) {
      throw new RuntimeInvariantError("unknown_provider_route_lease", {
        requestId,
      });
    }
    return lease;
  }

  private releaseLease(lease: ProviderRouteLease, releasedAt: number): void {
    this.leases.set(lease.requestId, {
      ...lease,
      releasedAt,
    });
  }

  private expireCircuitsAndLeases(): void {
    const now = this.clock.now();
    for (const [routeId, state] of this.states) {
      if (state.status === "open" && (state.openUntil ?? 0) <= now) {
        this.states.set(routeId, {
          ...state,
          status: "degraded",
          openUntil: null,
          revision: state.revision + 1,
        });
        this.revision += 1;
      }
    }
    for (const lease of this.leases.values()) {
      if (lease.releasedAt === null && lease.expiresAt <= now) {
        const state = this.states.get(lease.routeId);
        if (state !== undefined) {
          this.states.set(lease.routeId, {
            ...state,
            inFlight: Math.max(0, state.inFlight - 1),
            revision: state.revision + 1,
          });
        }
        this.leases.set(lease.requestId, { ...lease, releasedAt: now });
        this.revision += 1;
      }
    }
  }
}

function normalizeDefinition(
  definition: ProviderRouteDefinition,
): ProviderRouteDefinition {
  assertNonEmpty(definition.routeId, "routeId");
  assertNonEmpty(definition.providerId, "providerId");
  assertNonEmpty(definition.modelId, "modelId");
  assertNonEmpty(definition.endpointId, "endpointId");
  assertNonEmpty(definition.region, "region");
  assertNonNegativeInteger(
    definition.maximumContextTokens,
    "maximumContextTokens",
  );
  assertNonNegativeInteger(
    definition.maximumOutputTokens,
    "maximumOutputTokens",
  );
  assertFiniteNumber(definition.inputCostPerMillion, "inputCostPerMillion");
  assertFiniteNumber(definition.outputCostPerMillion, "outputCostPerMillion");
  assertNonNegativeInteger(
    definition.baseLatencyMilliseconds,
    "baseLatencyMilliseconds",
  );
  assertNonNegativeInteger(definition.concurrencyLimit, "concurrencyLimit");
  if (definition.concurrencyLimit === 0) {
    throw new RuntimeInvariantError("zero_provider_route_concurrency", {
      routeId: definition.routeId,
    });
  }
  if (!Number.isSafeInteger(definition.priority)) {
    throw new RuntimeInvariantError("invalid_provider_route_priority", {
      priority: definition.priority,
    });
  }
  return {
    ...deepClone(definition),
    capabilities: uniqueSorted(definition.capabilities),
    privacyClasses: uniqueSorted(definition.privacyClasses),
  };
}

function createInitialState(
  definition: ProviderRouteDefinition,
): ProviderRouteState {
  return {
    routeId: definition.routeId,
    status: definition.enabled ? "available" : "disabled",
    inFlight: 0,
    consecutiveFailures: 0,
    successes: 0,
    failures: 0,
    latencyEwmaMilliseconds: definition.baseLatencyMilliseconds,
    availabilityEwma: 1,
    openUntil: null,
    lastSelectedAt: null,
    lastSuccessAt: null,
    lastFailureAt: null,
    lastFailureClass: null,
    revision: 1,
  };
}

function validateRouteRequest(request: ProviderRouteRequest): void {
  assertNonEmpty(request.requestId, "requestId");
  assertNonEmpty(request.sessionId, "sessionId");
  assertNonEmpty(request.privacyClass, "privacyClass");
  assertNonNegativeInteger(request.estimatedInputTokens, "estimatedInputTokens");
  assertNonNegativeInteger(request.requestedOutputTokens, "requestedOutputTokens");
  if (request.maximumCost !== null) {
    assertFiniteNumber(request.maximumCost, "maximumCost");
  }
  if (request.maximumLatencyMilliseconds !== null) {
    assertNonNegativeInteger(
      request.maximumLatencyMilliseconds,
      "maximumLatencyMilliseconds",
    );
  }
}

function estimateCost(
  definition: ProviderRouteDefinition,
  request: ProviderRouteRequest,
): number {
  return (
    (request.estimatedInputTokens / 1_000_000) *
      definition.inputCostPerMillion +
    (request.requestedOutputTokens / 1_000_000) *
      definition.outputCostPerMillion
  );
}

function normalizePriority(priority: number): number {
  return 1 / (1 + Math.exp(-priority / 10));
}

function ewma(previous: number, sample: number, alpha: number): number {
  return previous * (1 - alpha) + sample * alpha;
}

function assertUnitInterval(value: number, name: string): void {
  assertFiniteNumber(value, name);
  if (value < 0 || value > 1) {
    throw new RuntimeInvariantError("value_outside_unit_interval", {
      name,
      value,
    });
  }
}

function routeScoreComparator(
  left: ProviderRouteScore,
  right: ProviderRouteScore,
): number {
  if (left.eligible !== right.eligible) {
    return left.eligible ? -1 : 1;
  }
  return (
    compareNumbers(right.score, left.score) ||
    compareNumbers(left.estimatedCost, right.estimatedCost) ||
    compareNumbers(
      left.estimatedLatencyMilliseconds,
      right.estimatedLatencyMilliseconds,
    ) ||
    compareStrings(left.routeId, right.routeId)
  );
}
