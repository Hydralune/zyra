import { describe, expect, test } from "bun:test";

import {
  DeterministicIdFactory,
  ManualClock,
} from "../../src/core/runtime-primitives.ts";
import {
  InMemorySecretVault,
  ProviderCredentialRuntime,
} from "../../src/provider/credential-runtime.ts";
import { ProviderRateLimitRuntime } from "../../src/provider/rate-limit-runtime.ts";
import { ProviderRoutingRuntime } from "../../src/provider/routing-runtime.ts";

function ids(seed: string, sequence = 0): DeterministicIdFactory {
  return new DeterministicIdFactory(seed, sequence);
}

function credentialInput(overrides: Partial<{
  credentialId: string;
  providerId: string;
  accountId: string;
  kind: "api_key" | "bearer_token" | "oauth2" | "aws_sigv4" | "custom_header";
  secret: { value: string; refreshToken?: string };
  scopes: string[];
  allowedModels: string[];
  headerName: string;
  priority: number;
  expiresAt: number | null;
  refreshAfter: number | null;
}> = {}) {
  return {
    credentialId: overrides.credentialId ?? "credential-1",
    providerId: overrides.providerId ?? "anthropic",
    accountId: overrides.accountId ?? "account-1",
    kind: overrides.kind ?? "api_key",
    secret: overrides.secret ?? { value: "secret-value-1" },
    scopes: overrides.scopes ?? ["messages:create"],
    allowedModels: overrides.allowedModels ?? ["claude-test"],
    headerName: overrides.headerName,
    priority: overrides.priority ?? 10,
    expiresAt: overrides.expiresAt ?? null,
    refreshAfter: overrides.refreshAfter ?? null,
    metadata: { fixture: "credential" },
  };
}

function routeDefinition(overrides: Partial<{
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
}> = {}) {
  return {
    routeId: overrides.routeId ?? "route-primary",
    providerId: overrides.providerId ?? "anthropic",
    modelId: overrides.modelId ?? "claude-test",
    endpointId: overrides.endpointId ?? "endpoint-primary",
    region: overrides.region ?? "us-east",
    capabilities: overrides.capabilities ?? ["text", "tools", "streaming"],
    privacyClasses: overrides.privacyClasses ?? ["public", "internal"],
    maximumContextTokens: overrides.maximumContextTokens ?? 200_000,
    maximumOutputTokens: overrides.maximumOutputTokens ?? 16_000,
    inputCostPerMillion: overrides.inputCostPerMillion ?? 3,
    outputCostPerMillion: overrides.outputCostPerMillion ?? 15,
    baseLatencyMilliseconds: overrides.baseLatencyMilliseconds ?? 200,
    concurrencyLimit: overrides.concurrencyLimit ?? 4,
    priority: overrides.priority ?? 10,
    enabled: overrides.enabled ?? true,
    metadata: { fixture: "route" },
  };
}

function routeRequest(overrides: Partial<{
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
}> = {}) {
  return {
    requestId: overrides.requestId ?? "request-1",
    sessionId: overrides.sessionId ?? "session-1",
    requiredCapabilities: overrides.requiredCapabilities ?? ["text"],
    privacyClass: overrides.privacyClass ?? "internal",
    estimatedInputTokens: overrides.estimatedInputTokens ?? 1_000,
    requestedOutputTokens: overrides.requestedOutputTokens ?? 500,
    maximumCost: overrides.maximumCost ?? null,
    maximumLatencyMilliseconds: overrides.maximumLatencyMilliseconds ?? null,
    preferredProviderIds: overrides.preferredProviderIds ?? [],
    excludedRouteIds: overrides.excludedRouteIds ?? [],
    stickyRouteId: overrides.stickyRouteId ?? null,
    allowDegraded: overrides.allowDegraded ?? false,
    metadata: { fixture: "request" },
  };
}

function rateDefinition(overrides: Partial<{
  limitId: string;
  scopeKind: "global" | "provider" | "credential" | "model" | "session";
  scopeId: string;
  dimension: "requests" | "input_tokens" | "output_tokens" | "cost";
  capacity: number;
  refillAmount: number;
  refillIntervalMilliseconds: number;
  burstCapacity: number;
}> = {}) {
  return {
    limitId: overrides.limitId ?? "limit-requests",
    scopeKind: overrides.scopeKind ?? "provider",
    scopeId: overrides.scopeId ?? "anthropic",
    dimension: overrides.dimension ?? "requests",
    capacity: overrides.capacity ?? 10,
    refillAmount: overrides.refillAmount ?? 10,
    refillIntervalMilliseconds: overrides.refillIntervalMilliseconds ?? 1_000,
    burstCapacity: overrides.burstCapacity ?? 10,
    enabled: true,
    priority: 10,
    metadata: { fixture: "rate" },
  };
}

function rateDemand(overrides: Partial<{
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
}> = {}) {
  return {
    requestId: overrides.requestId ?? "request-1",
    sessionId: overrides.sessionId ?? "session-1",
    providerId: overrides.providerId ?? "anthropic",
    credentialId: overrides.credentialId ?? "credential-1",
    modelId: overrides.modelId ?? "claude-test",
    requests: overrides.requests ?? 1,
    inputTokens: overrides.inputTokens ?? 100,
    outputTokens: overrides.outputTokens ?? 50,
    estimatedCost: overrides.estimatedCost ?? 0.01,
    priority: overrides.priority ?? 10,
    deadlineAt: overrides.deadlineAt ?? Number.MAX_SAFE_INTEGER,
  };
}

describe("provider credential custody", () => {
  test("stores the secret in the vault and exposes only metadata in snapshots", async () => {
    const vault = new InMemorySecretVault();
    const runtime = new ProviderCredentialRuntime(vault, {
      ids: ids("credential-secret"),
    });
    const registered = await runtime.register(credentialInput());
    expect(registered.vaultReference).toBe(
      "zyra-credential/credential-1",
    );
    expect(vault.size()).toBe(1);
    const serialized = JSON.stringify(runtime.snapshot());
    expect(serialized).not.toContain("secret-value-1");
    expect(serialized).not.toContain('"value"');
    const resolved = await runtime.resolve("credential-1");
    expect(resolved.headers).toEqual({ "x-api-key": "secret-value-1" });
    expect(resolved.signingMaterial).toBeNull();
  });

  test("selects credentials by provider model scope and priority", async () => {
    const runtime = new ProviderCredentialRuntime(new InMemorySecretVault(), {
      ids: ids("credential-select"),
    });
    await runtime.register(
      credentialInput({
        credentialId: "low",
        priority: 1,
        scopes: ["messages:create", "files:read"],
      }),
    );
    await runtime.register(
      credentialInput({
        credentialId: "high",
        priority: 100,
        scopes: ["messages:create"],
      }),
    );
    await runtime.register(
      credentialInput({
        credentialId: "other-provider",
        providerId: "openai",
        priority: 1_000,
      }),
    );
    expect(
      runtime.select({
        providerId: "anthropic",
        modelId: "claude-test",
        requiredScopes: ["messages:create"],
      }).credentialId,
    ).toBe("high");
    expect(
      runtime.select({
        providerId: "anthropic",
        modelId: "claude-test",
        requiredScopes: ["files:read"],
      }).credentialId,
    ).toBe("low");
  });

  test("builds bearer oauth and custom authentication headers", async () => {
    const vault = new InMemorySecretVault();
    const runtime = new ProviderCredentialRuntime(vault, {
      ids: ids("credential-headers"),
    });
    await runtime.register(
      credentialInput({
        credentialId: "bearer",
        kind: "bearer_token",
        secret: { value: "bearer-secret" },
      }),
    );
    await runtime.register(
      credentialInput({
        credentialId: "oauth",
        kind: "oauth2",
        secret: { value: "oauth-secret", refreshToken: "refresh-secret" },
      }),
    );
    await runtime.register(
      credentialInput({
        credentialId: "custom",
        kind: "custom_header",
        headerName: "x-tenant-token",
        secret: { value: "tenant-secret" },
      }),
    );
    expect((await runtime.resolve("bearer")).headers.authorization).toBe(
      "Bearer bearer-secret",
    );
    expect((await runtime.resolve("oauth")).headers.authorization).toBe(
      "Bearer oauth-secret",
    );
    expect((await runtime.resolve("custom")).headers["x-tenant-token"]).toBe(
      "tenant-secret",
    );
  });

  test("returns signing material only for the SigV4 strategy", async () => {
    const runtime = new ProviderCredentialRuntime(new InMemorySecretVault(), {
      ids: ids("credential-sigv4"),
    });
    await runtime.register(
      credentialInput({
        credentialId: "aws",
        providerId: "bedrock",
        kind: "aws_sigv4",
        secret: { value: "access-key" },
        allowedModels: ["anthropic.claude-test"],
      }),
    );
    const resolved = await runtime.resolve("aws");
    expect(resolved.headers).toEqual({});
    expect(resolved.signingMaterial).toEqual({ value: "access-key" });
  });

  test("places a failed credential in exponential cooldown and then reactivates it", async () => {
    const clock = new ManualClock(1_000);
    const runtime = new ProviderCredentialRuntime(new InMemorySecretVault(), {
      clock,
      ids: ids("credential-cooldown"),
      cooldownBaseMilliseconds: 100,
      quarantineFailureThreshold: 5,
    });
    await runtime.register(credentialInput());
    const failed = runtime.recordFailure("credential-1", "rate_limited");
    expect(failed.status).toBe("cooldown");
    expect(failed.cooldownUntil).toBe(1_100);
    await expect(runtime.resolve("credential-1")).rejects.toThrow(
      "credential_not_active",
    );
    clock.advance(100);
    const resolved = await runtime.resolve("credential-1");
    expect(resolved.credentialId).toBe("credential-1");
    expect(runtime.get("credential-1").status).toBe("active");
  });

  test("quarantines after the configured failure threshold", async () => {
    const runtime = new ProviderCredentialRuntime(new InMemorySecretVault(), {
      ids: ids("credential-quarantine"),
      quarantineFailureThreshold: 2,
      cooldownBaseMilliseconds: 0,
    });
    await runtime.register(credentialInput());
    runtime.recordFailure("credential-1", "transport");
    const quarantined = runtime.recordFailure("credential-1", "authentication");
    expect(quarantined.status).toBe("quarantined");
    expect(() =>
      runtime.select({
        providerId: "anthropic",
        modelId: "claude-test",
      }),
    ).toThrow("no_eligible_credential");
    expect(runtime.unquarantine("credential-1").status).toBe("active");
  });

  test("grants a single refresh lease and rotates the secret atomically", async () => {
    const clock = new ManualClock(1_000);
    const vault = new InMemorySecretVault();
    const runtime = new ProviderCredentialRuntime(vault, {
      clock,
      ids: ids("credential-refresh"),
      refreshLeaseMilliseconds: 500,
    });
    await runtime.register(
      credentialInput({
        kind: "oauth2",
        secret: { value: "old-token", refreshToken: "refresh-token" },
        refreshAfter: 1_000,
        expiresAt: 2_000,
      }),
    );
    expect(runtime.needsRefresh("credential-1")).toBe(true);
    const lease = runtime.acquireRefreshLease("credential-1", "worker-1");
    expect(runtime.acquireRefreshLease("credential-1", "worker-1").leaseId).toBe(
      lease.leaseId,
    );
    expect(() =>
      runtime.acquireRefreshLease("credential-1", "worker-2"),
    ).toThrow("credential_refresh_lease_held");
    const refreshed = await runtime.refresh(lease.leaseId, async (record, secret) => {
      expect(record.status).toBe("refreshing");
      expect(secret.refreshToken).toBe("refresh-token");
      return {
        secret: { value: "new-token", refreshToken: "refresh-token-2" },
        expiresAt: 5_000,
        refreshAfter: 4_000,
        scopes: ["messages:create", "files:read"],
      };
    });
    expect(refreshed.status).toBe("active");
    expect(refreshed.expiresAt).toBe(5_000);
    expect((await runtime.resolve("credential-1")).headers.authorization).toBe(
      "Bearer new-token",
    );
  });

  test("expires an abandoned refresh lease and restores active status", async () => {
    const clock = new ManualClock(100);
    const runtime = new ProviderCredentialRuntime(new InMemorySecretVault(), {
      clock,
      ids: ids("credential-lease-expire"),
      refreshLeaseMilliseconds: 10,
    });
    await runtime.register(credentialInput());
    runtime.acquireRefreshLease("credential-1", "worker-1");
    expect(runtime.get("credential-1").status).toBe("refreshing");
    clock.advance(10);
    expect(runtime.snapshot().leases).toEqual([]);
    expect(runtime.get("credential-1").status).toBe("active");
  });

  test("requires the expected revision when rotating a credential", async () => {
    const runtime = new ProviderCredentialRuntime(new InMemorySecretVault(), {
      ids: ids("credential-cas"),
    });
    const record = await runtime.register(credentialInput());
    await expect(
      runtime.rotate("credential-1", record.revision + 1, {
        value: "new-secret",
      }),
    ).rejects.toThrow("credential_revision_conflict");
    const rotated = await runtime.rotate("credential-1", record.revision, {
      value: "new-secret",
    });
    expect(rotated.revision).toBe(record.revision + 1);
    expect((await runtime.resolve("credential-1")).headers["x-api-key"]).toBe(
      "new-secret",
    );
  });

  test("revocation deletes secret material and prevents resolution", async () => {
    const vault = new InMemorySecretVault();
    const runtime = new ProviderCredentialRuntime(vault, {
      ids: ids("credential-revoke"),
    });
    await runtime.register(credentialInput());
    const revoked = await runtime.revoke("credential-1");
    expect(revoked.status).toBe("revoked");
    expect(vault.size()).toBe(0);
    await expect(runtime.resolve("credential-1")).rejects.toThrow(
      "credential_not_active",
    );
  });

  test("restores metadata while reusing an external persistent vault", async () => {
    const vault = new InMemorySecretVault();
    const runtime = new ProviderCredentialRuntime(vault, {
      ids: ids("credential-snapshot"),
    });
    await runtime.register(credentialInput());
    const snapshot = runtime.snapshot();
    const restored = new ProviderCredentialRuntime(vault, {
      ids: ids("credential-after"),
    });
    restored.restore(snapshot);
    expect(restored.list("anthropic")).toHaveLength(1);
    expect((await restored.resolve("credential-1")).headers["x-api-key"]).toBe(
      "secret-value-1",
    );
    snapshot.records[0]!.providerId = "tampered";
    expect(() => restored.restore(snapshot)).toThrow(
      "credential_snapshot_checksum_mismatch",
    );
  });
});

describe("provider routing policy", () => {
  test("selects the only route satisfying capability and privacy constraints", () => {
    const runtime = new ProviderRoutingRuntime();
    runtime.register(
      routeDefinition({
        routeId: "public-text",
        capabilities: ["text"],
        privacyClasses: ["public"],
      }),
    );
    runtime.register(
      routeDefinition({
        routeId: "internal-tools",
        endpointId: "endpoint-internal",
        capabilities: ["text", "tools"],
        privacyClasses: ["internal"],
      }),
    );
    const decision = runtime.decide(
      routeRequest({ requiredCapabilities: ["tools"], privacyClass: "internal" }),
    );
    expect(decision.routeId).toBe("internal-tools");
    const publicScore = runtime
      .score(routeRequest({ requiredCapabilities: ["tools"], privacyClass: "internal" }))
      .find((score) => score.routeId === "public-text");
    expect(publicScore?.eligible).toBe(false);
    expect(publicScore?.reasons).toContain("missing_capability:tools");
    expect(publicScore?.reasons).toContain("privacy_class_mismatch");
  });

  test("rejects routes whose context or output limits are too small", () => {
    const runtime = new ProviderRoutingRuntime();
    runtime.register(
      routeDefinition({
        maximumContextTokens: 1_000,
        maximumOutputTokens: 100,
      }),
    );
    const scores = runtime.score(
      routeRequest({
        estimatedInputTokens: 1_001,
        requestedOutputTokens: 101,
      }),
    );
    expect(scores[0]?.eligible).toBe(false);
    expect(scores[0]?.reasons).toContain("context_limit_exceeded");
    expect(scores[0]?.reasons).toContain("output_limit_exceeded");
    expect(() =>
      runtime.decide(
        routeRequest({
          estimatedInputTokens: 1_001,
          requestedOutputTokens: 101,
        }),
      ),
    ).toThrow("no_eligible_provider_route");
  });

  test("enforces estimated cost and latency ceilings", () => {
    const runtime = new ProviderRoutingRuntime();
    runtime.register(
      routeDefinition({
        inputCostPerMillion: 100,
        outputCostPerMillion: 200,
        baseLatencyMilliseconds: 2_000,
      }),
    );
    const score = runtime.score(
      routeRequest({
        estimatedInputTokens: 10_000,
        requestedOutputTokens: 10_000,
        maximumCost: 1,
        maximumLatencyMilliseconds: 1_000,
      }),
    )[0]!;
    expect(score.estimatedCost).toBe(3);
    expect(score.reasons).toContain("cost_limit_exceeded");
    expect(score.reasons).toContain("latency_limit_exceeded");
  });

  test("reserves route concurrency and refuses saturation", () => {
    const runtime = new ProviderRoutingRuntime();
    runtime.register(routeDefinition({ concurrencyLimit: 1 }));
    const firstDecision = runtime.decide(routeRequest({ requestId: "request-1" }));
    runtime.acquire(firstDecision);
    const saturated = runtime.score(routeRequest({ requestId: "request-2" }))[0]!;
    expect(saturated.eligible).toBe(false);
    expect(saturated.reasons).toContain("concurrency_saturated");
    expect(() =>
      runtime.acquire({ ...firstDecision, requestId: "request-2" }),
    ).toThrow("provider_route_capacity_exhausted");
  });

  test("records success, updates latency EWMA, and creates session affinity", () => {
    const clock = new ManualClock(1_000);
    const runtime = new ProviderRoutingRuntime({
      clock,
      latencyEwmaAlpha: 0.5,
      availabilityEwmaAlpha: 0.5,
    });
    runtime.register(routeDefinition({ baseLatencyMilliseconds: 100 }));
    const decision = runtime.decide(routeRequest());
    runtime.acquire(decision);
    const state = runtime.recordSuccess("request-1", 300, "session-1");
    expect(state.status).toBe("available");
    expect(state.latencyEwmaMilliseconds).toBe(200);
    expect(state.successes).toBe(1);
    expect(state.inFlight).toBe(0);
    const sticky = runtime.decide(routeRequest({ requestId: "request-2" }));
    expect(sticky.routeId).toBe("route-primary");
    expect(runtime.snapshot().sessionAffinity).toEqual([
      { sessionId: "session-1", routeId: "route-primary" },
    ]);
  });

  test("opens a circuit after repeated transport failures", () => {
    const clock = new ManualClock(1_000);
    const runtime = new ProviderRoutingRuntime({
      clock,
      circuitFailureThreshold: 2,
      circuitBaseOpenMilliseconds: 100,
    });
    runtime.register(routeDefinition());
    for (const requestId of ["request-1", "request-2"]) {
      const decision = runtime.decide(
        routeRequest({ requestId, allowDegraded: requestId === "request-2" }),
      );
      runtime.acquire(decision);
      runtime.recordFailure(requestId, "transport");
    }
    const state = runtime.getState("route-primary");
    expect(state.status).toBe("open");
    expect(state.openUntil).toBe(1_100);
    expect(() => runtime.decide(routeRequest({ requestId: "request-3" }))).toThrow(
      "no_eligible_provider_route",
    );
    clock.advance(100);
    expect(runtime.getState("route-primary").status).toBe("degraded");
    expect(
      runtime.decide(
        routeRequest({ requestId: "request-3", allowDegraded: true }),
      ).routeId,
    ).toBe("route-primary");
  });

  test("opens a circuit immediately for authentication failures", () => {
    const runtime = new ProviderRoutingRuntime({ circuitFailureThreshold: 10 });
    runtime.register(routeDefinition());
    const decision = runtime.decide(routeRequest());
    runtime.acquire(decision);
    const state = runtime.recordFailure("request-1", "authentication");
    expect(state.status).toBe("open");
    expect(state.consecutiveFailures).toBe(1);
    expect(state.lastFailureClass).toBe("authentication");
  });

  test("honors explicit exclusion and a preferred provider bonus", () => {
    const runtime = new ProviderRoutingRuntime();
    runtime.register(
      routeDefinition({
        routeId: "anthropic-route",
        providerId: "anthropic",
        priority: 0,
      }),
    );
    runtime.register(
      routeDefinition({
        routeId: "bedrock-route",
        providerId: "bedrock",
        endpointId: "bedrock-endpoint",
        priority: 0,
      }),
    );
    expect(
      runtime.decide(
        routeRequest({
          preferredProviderIds: ["bedrock"],
          excludedRouteIds: ["anthropic-route"],
        }),
      ).routeId,
    ).toBe("bedrock-route");
  });

  test("expires an abandoned lease without leaking concurrency", () => {
    const clock = new ManualClock(1_000);
    const runtime = new ProviderRoutingRuntime({
      clock,
      leaseMilliseconds: 10,
    });
    runtime.register(routeDefinition({ concurrencyLimit: 1 }));
    runtime.acquire(runtime.decide(routeRequest()));
    expect(runtime.getState("route-primary").inFlight).toBe(1);
    clock.advance(10);
    expect(runtime.getState("route-primary").inFlight).toBe(0);
    expect(
      runtime.decide(routeRequest({ requestId: "request-2" })).routeId,
    ).toBe("route-primary");
  });

  test("extends a live lease to the request-scoped provider timeout", () => {
    const clock = new ManualClock(1_000);
    const runtime = new ProviderRoutingRuntime({
      clock,
      leaseMilliseconds: 10,
    });
    runtime.register(routeDefinition({ concurrencyLimit: 1 }));
    runtime.acquire(runtime.decide(routeRequest()), 50);
    clock.advance(10);
    expect(runtime.getState("route-primary").inFlight).toBe(1);
    clock.advance(39);
    expect(runtime.recordSuccess("request-1", 49).successes).toBe(1);
  });

  test("restores routes leases and affinity with checksum validation", () => {
    const runtime = new ProviderRoutingRuntime();
    runtime.register(routeDefinition());
    const first = runtime.decide(routeRequest());
    runtime.acquire(first);
    runtime.recordSuccess("request-1", 100, "session-1");
    const snapshot = runtime.snapshot();
    const restored = new ProviderRoutingRuntime();
    restored.restore(snapshot);
    expect(restored.getState("route-primary").successes).toBe(1);
    expect(restored.snapshot().sessionAffinity).toHaveLength(1);
    snapshot.definitions[0]!.priority += 1;
    expect(() => restored.restore(snapshot)).toThrow(
      "provider_routing_snapshot_checksum_mismatch",
    );
  });
});

describe("provider rate limit reservations", () => {
  test("reserves capacity and commits actual usage with a refund", () => {
    const runtime = new ProviderRateLimitRuntime({ ids: ids("rate-commit") });
    runtime.register(rateDefinition({ capacity: 10, burstCapacity: 10 }));
    const reservation = runtime.reserve(rateDemand({ requests: 4 }));
    expect(reservation.allocations[0]).toMatchObject({
      amount: 4,
      availableBefore: 10,
      availableAfter: 6,
    });
    expect(runtime.getBucket("limit-requests").reserved).toBe(4);
    const committed = runtime.commit(reservation.reservationId, { requests: 3 });
    expect(committed.status).toBe("committed");
    expect(runtime.getBucket("limit-requests")).toMatchObject({
      available: 7,
      reserved: 0,
      consumed: 3,
    });
  });

  test("releases a held reservation without consuming quota", () => {
    const runtime = new ProviderRateLimitRuntime({ ids: ids("rate-release") });
    runtime.register(rateDefinition({ capacity: 5, burstCapacity: 5 }));
    const reservation = runtime.reserve(rateDemand({ requests: 2 }));
    const released = runtime.release(reservation.reservationId);
    expect(released.status).toBe("released");
    expect(runtime.getBucket("limit-requests")).toMatchObject({
      available: 5,
      reserved: 0,
      consumed: 0,
    });
    expect(runtime.release(reservation.reservationId).status).toBe("released");
  });

  test("denies demand above capacity and computes the next refill time", () => {
    const clock = new ManualClock(1_000);
    const runtime = new ProviderRateLimitRuntime({
      clock,
      ids: ids("rate-deny"),
    });
    runtime.register(
      rateDefinition({
        capacity: 2,
        burstCapacity: 2,
        refillAmount: 1,
        refillIntervalMilliseconds: 100,
      }),
    );
    const decision = runtime.inspect(rateDemand({ requests: 3 }));
    expect(decision.allowed).toBe(false);
    expect(decision.limitingIds).toEqual(["limit-requests"]);
    expect(decision.retryAt).toBe(1_100);
    expect(() => runtime.reserve(rateDemand({ requests: 3 }))).toThrow(
      "rate_limit_capacity_denied",
    );
  });

  test("applies multiple scoped dimensions to one atomic reservation", () => {
    const runtime = new ProviderRateLimitRuntime({ ids: ids("rate-multi") });
    runtime.register(
      rateDefinition({
        limitId: "global-requests",
        scopeKind: "global",
        scopeId: "all",
        dimension: "requests",
        capacity: 10,
        burstCapacity: 10,
      }),
    );
    runtime.register(
      rateDefinition({
        limitId: "model-input",
        scopeKind: "model",
        scopeId: "claude-test",
        dimension: "input_tokens",
        capacity: 1_000,
        burstCapacity: 1_000,
      }),
    );
    runtime.register(
      rateDefinition({
        limitId: "session-cost",
        scopeKind: "session",
        scopeId: "session-1",
        dimension: "cost",
        capacity: 1,
        burstCapacity: 1,
      }),
    );
    const reservation = runtime.reserve(
      rateDemand({ requests: 1, inputTokens: 250, estimatedCost: 0.25 }),
    );
    expect(reservation.allocations.map((item) => item.limitId)).toEqual([
      "global-requests",
      "model-input",
      "session-cost",
    ]);
    runtime.commit(reservation.reservationId, {
      requests: 1,
      input_tokens: 200,
      cost: 0.2,
    });
    expect(runtime.getBucket("model-input").available).toBe(800);
    expect(runtime.getBucket("session-cost").available).toBeCloseTo(0.8);
  });

  test("rejects actual usage that exceeds both reservation and free capacity", () => {
    const runtime = new ProviderRateLimitRuntime({ ids: ids("rate-actual") });
    runtime.register(rateDefinition({ capacity: 2, burstCapacity: 2 }));
    const reservation = runtime.reserve(rateDemand({ requests: 1 }));
    expect(() =>
      runtime.commit(reservation.reservationId, { requests: 3 }),
    ).toThrow("rate_limit_actual_exceeds_capacity");
    expect(runtime.getBucket("limit-requests").reserved).toBe(1);
  });

  test("expires held reservations and returns their capacity", () => {
    const clock = new ManualClock(1_000);
    const runtime = new ProviderRateLimitRuntime({
      clock,
      ids: ids("rate-expire"),
      reservationTtlMilliseconds: 10,
    });
    runtime.register(rateDefinition({ capacity: 2, burstCapacity: 2 }));
    const reservation = runtime.reserve(
      rateDemand({ requests: 2, deadlineAt: 10_000 }),
    );
    expect(runtime.getBucket("limit-requests").available).toBe(0);
    clock.advance(10);
    const snapshot = runtime.snapshot();
    expect(
      snapshot.reservations.find(
        (item) => item.reservationId === reservation.reservationId,
      )?.status,
    ).toBe("expired");
    expect(runtime.getBucket("limit-requests").available).toBe(2);
  });

  test("uses provider remaining and retry-after headers as hard ceilings", () => {
    const clock = new ManualClock(1_000);
    const runtime = new ProviderRateLimitRuntime({ clock, ids: ids("rate-header") });
    runtime.register(rateDefinition({ capacity: 10, burstCapacity: 10 }));
    const updated = runtime.applyProviderHeaders({
      providerId: "anthropic",
      credentialId: "credential-1",
      modelId: "claude-test",
      headers: {
        remainingRequests: 2,
        resetRequestsAt: 2_000,
        retryAfterMilliseconds: 500,
      },
    });
    expect(updated).toEqual(["limit-requests"]);
    expect(runtime.getBucket("limit-requests").available).toBe(2);
    const decision = runtime.inspect(rateDemand({ requests: 1 }));
    expect(decision.allowed).toBe(false);
    expect(decision.retryAt).toBe(2_000);
  });

  test("refills discrete token bucket intervals without exceeding burst", () => {
    const clock = new ManualClock(1_000);
    const runtime = new ProviderRateLimitRuntime({
      clock,
      ids: ids("rate-refill"),
    });
    runtime.register(
      rateDefinition({
        capacity: 2,
        burstCapacity: 4,
        refillAmount: 1,
        refillIntervalMilliseconds: 100,
      }),
    );
    const reservation = runtime.reserve(rateDemand({ requests: 2 }));
    runtime.commit(reservation.reservationId, { requests: 2 });
    expect(runtime.getBucket("limit-requests").available).toBe(0);
    clock.advance(250);
    expect(runtime.getBucket("limit-requests").available).toBe(2);
    clock.advance(1_000);
    expect(runtime.getBucket("limit-requests").available).toBe(4);
  });

  test("restores buckets and rejects a tampered reservation snapshot", () => {
    const runtime = new ProviderRateLimitRuntime({ ids: ids("rate-snapshot") });
    runtime.register(rateDefinition({ capacity: 5, burstCapacity: 5 }));
    runtime.reserve(rateDemand({ requests: 2 }));
    const snapshot = runtime.snapshot();
    const restored = new ProviderRateLimitRuntime({ ids: ids("rate-after") });
    restored.restore(snapshot);
    expect(restored.getBucket("limit-requests").reserved).toBe(2);
    snapshot.buckets[0]!.available += 1;
    expect(() => restored.restore(snapshot)).toThrow(
      "rate_limit_snapshot_checksum_mismatch",
    );
  });
});
