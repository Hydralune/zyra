import assert from "node:assert/strict";
import { test } from "node:test";
import {
  digest,
  E03RuntimeError,
  response,
  type ControlCommand,
  type E03ControlEnvelope,
  type E03ControlResponse,
} from "../../src/e03/contracts.ts";
import type { JsonObject } from "../../src/contracts.ts";
import {
  ControlRouteRegistry,
  StructuredControlRouter,
  type ControlDelegate,
  type ControlRouteDescriptor,
  type E03CommandHandler,
} from "../../src/control/router.ts";
import type { AgentControlHandler } from "../../src/control/session-handler.ts";

function assertRuntimeCode(error: unknown, code: string): boolean {
  assert.ok(error instanceof E03RuntimeError);
  assert.equal(error.code, code);
  assert.ok(error.message.length > 0);
  return true;
}

function envelope(
  label: string,
  command: ControlCommand = "agent.status",
  body: JsonObject = { task_id: `task-${label}` },
): E03ControlEnvelope {
  return {
    schema_version: "3.0",
    request_id: `request-${label}`,
    idempotency_key: `idempotency-${label}`,
    run_id: "run-routing-e03",
    session_id: "session-routing-e03",
    parent_task_id: "parent-routing-e03",
    expected_revision: 3,
    command,
    body,
  };
}

function registerRoute(
  registry: ControlRouteRegistry,
  label: string,
  overrides: Partial<
    Omit<
      ControlRouteDescriptor,
      "routeId" | "registeredAt" | "revision" | "digest"
    >
  > = {},
): ControlRouteDescriptor {
  return registry.register({
    routeId: `route-${label}`,
    command: overrides.command ?? "agent.status",
    owner: overrides.owner ?? "E03",
    handlerId: overrides.handlerId ?? `handler-${label}`,
    priority: overrides.priority ?? 10,
    state: overrides.state ?? "active",
    mutation: overrides.mutation ?? false,
    physicalEffect: overrides.physicalEffect ?? false,
    supportsReplay: overrides.supportsReplay ?? true,
    supportsLostAck: overrides.supportsLostAck ?? true,
    maximumInFlight: overrides.maximumInFlight ?? 2,
    timeoutMs: overrides.timeoutMs ?? 30_000,
    registeredAt: "2026-07-18T19:00:00.000Z",
  });
}

function registryWithRoute(
  label: string,
  overrides: Parameters<typeof registerRoute>[2] = {},
) {
  const registry = new ControlRouteRegistry();
  const route = registerRoute(registry, label, overrides);
  return { registry, route };
}

function successfulControlResponse(
  request: E03ControlEnvelope,
  owner: "E01" | "E02" | "E03",
  result: JsonObject = {},
): E03ControlResponse {
  return response({
    ok: true,
    request_id: request.request_id,
    command: request.command,
    phase: "commit",
    revision: request.expected_revision + 1,
    dispatch_count: 1,
    state: { canonical_owner: owner },
    result,
  });
}

function recordingRouter() {
  const calls = {
    agent: [] as string[],
    e03: [] as string[],
    e01: [] as string[],
    e02: [] as string[],
  };
  const agent = {
    async execute(request: E03ControlEnvelope): Promise<E03ControlResponse> {
      calls.agent.push(request.command);
      return successfulControlResponse(request, "E03", { handler: "agent" });
    },
    recoverLostAck(key: string): E03ControlResponse | null {
      return key === "known-lost-ack"
        ? response({
            ok: true,
            request_id: "recovered-request",
            command: "agent.status",
            phase: "ack",
            replayed: true,
          })
        : null;
    },
  } as unknown as AgentControlHandler;
  const e03: E03CommandHandler = {
    async execute(request) {
      calls.e03.push(request.command);
      return successfulControlResponse(request, "E03", { handler: "e03" });
    },
  };
  const e01: ControlDelegate = {
    async execute(request) {
      calls.e01.push(request.command);
      return successfulControlResponse(request, "E01", { handler: "e01" });
    },
  };
  const e02: ControlDelegate = {
    async execute(request) {
      calls.e02.push(request.command);
      return successfulControlResponse(request, "E02", { handler: "e02" });
    },
  };
  return {
    calls,
    agent,
    e03,
    e01,
    e02,
    router: new StructuredControlRouter(agent, e03, e01, e02),
  };
}

test("e03 structured router dispatches ordinary agent controls to agent handler", async () => {
  const fixture = recordingRouter();
  const request = envelope("router-status", "agent.status");
  const result = await fixture.router.dispatch(request);
  assert.equal(result.ok, true);
  assert.equal(result.request_id, request.request_id);
  assert.equal(result.command, "agent.status");
  assert.equal(result.state?.canonical_owner, "E03");
  assert.equal(result.result?.handler, "agent");
  assert.deepEqual(fixture.calls.agent, ["agent.status"]);
  assert.deepEqual(fixture.calls.e03, []);
  assert.deepEqual(fixture.calls.e01, []);
  assert.deepEqual(fixture.calls.e02, []);
});

test("e03 structured router dispatches agent creation to E03 coordinator", async () => {
  const fixture = recordingRouter();
  const request = envelope("router-create", "agent.create", {
    task_id: "router-child",
    agent: "reviewer",
    prompt: "review",
  });
  const result = await fixture.router.dispatch(request);
  assert.equal(result.ok, true);
  assert.equal(result.result?.handler, "e03");
  assert.equal(result.state?.canonical_owner, "E03");
  assert.deepEqual(fixture.calls.e03, ["agent.create"]);
  assert.deepEqual(fixture.calls.agent, []);
});

test("e03 structured router dispatches fanout and worktree controls to E03", async () => {
  const fixture = recordingRouter();
  const fanout = envelope("router-fanout", "team.fanout", {
    parent_task_id: "fanout-parent",
    targets: [],
  });
  const prepare = envelope("router-worktree", "worktree.prepare", {
    task_id: "worktree-task",
    workspace_root: "G:/agent-zoo/zyra",
    base_revision: "abc123",
  });
  const fanoutResult = await fixture.router.dispatch(fanout);
  const worktreeResult = await fixture.router.dispatch(prepare);
  assert.equal(fanoutResult.result?.handler, "e03");
  assert.equal(worktreeResult.result?.handler, "e03");
  assert.deepEqual(fixture.calls.e03, ["team.fanout", "worktree.prepare"]);
  assert.deepEqual(fixture.calls.agent, []);
});

test("e03 structured router delegates E01 session commands", async () => {
  const fixture = recordingRouter();
  const compact = envelope("router-compact", "context.compact", {});
  const model = envelope("router-model", "session.model", { model: "model-x" });
  const compactResult = await fixture.router.dispatch(compact);
  const modelResult = await fixture.router.dispatch(model);
  assert.equal(compactResult.ok, true);
  assert.equal(compactResult.state?.canonical_owner, "E01");
  assert.equal(modelResult.state?.canonical_owner, "E01");
  assert.deepEqual(fixture.calls.e01, ["context.compact", "session.model"]);
  assert.deepEqual(fixture.calls.e03, []);
  assert.deepEqual(fixture.calls.agent, []);
});

test("e03 structured router delegates E02 capability commands", async () => {
  const fixture = recordingRouter();
  const permission = envelope("router-permission", "permission.inspect", {});
  const mcp = envelope("router-mcp", "mcp.inspect", {});
  const skills = envelope("router-skills", "skills.inspect", {});
  assert.equal(
    (await fixture.router.dispatch(permission)).state?.canonical_owner,
    "E02",
  );
  assert.equal(
    (await fixture.router.dispatch(mcp)).state?.canonical_owner,
    "E02",
  );
  assert.equal(
    (await fixture.router.dispatch(skills)).state?.canonical_owner,
    "E02",
  );
  assert.deepEqual(fixture.calls.e02, [
    "permission.inspect",
    "mcp.inspect",
    "skills.inspect",
  ]);
  assert.deepEqual(fixture.calls.e03, []);
});

test("e03 structured router rejects unavailable E01 delegate without fallback", async () => {
  const fixture = recordingRouter();
  const router = new StructuredControlRouter(fixture.agent, fixture.e03);
  const request = envelope("router-no-e01", "context.clear", {});
  const result = await router.dispatch(request);
  assert.equal(result.ok, false);
  assert.equal(result.phase, "rejected");
  assert.equal(result.error, "e01_control_delegate_unavailable");
  assert.equal(result.request_id, request.request_id);
  assert.equal(result.command, request.command);
  assert.equal(result.python_fallback_attempted, false);
  assert.deepEqual(fixture.calls.agent, []);
  assert.deepEqual(fixture.calls.e03, []);
});

test("e03 structured router rejects unavailable E02 delegate without fallback", async () => {
  const fixture = recordingRouter();
  const router = new StructuredControlRouter(fixture.agent, fixture.e03);
  const request = envelope("router-no-e02", "mcp.inspect", {});
  const result = await router.dispatch(request);
  assert.equal(result.ok, false);
  assert.equal(result.phase, "rejected");
  assert.equal(result.error, "e02_control_delegate_unavailable");
  assert.equal(result.request_id, request.request_id);
  assert.equal(result.command, request.command);
  assert.equal(result.python_logical_owner, false);
  assert.deepEqual(fixture.calls.agent, []);
  assert.deepEqual(fixture.calls.e03, []);
});

test("e03 structured router failure rejects direct E01 owner mismatch", async () => {
  const fixture = recordingRouter();
  const request = envelope("router-e01-mismatch", "agent.status");
  await assert.rejects(
    () => fixture.router.delegateE01(request),
    (error) => assertRuntimeCode(error, "control_owner_mismatch"),
  );
});

test("e03 structured router failure rejects direct E02 owner mismatch", async () => {
  const fixture = recordingRouter();
  const request = envelope("router-e02-mismatch", "agent.status");
  await assert.rejects(
    () => fixture.router.delegateE02(request),
    (error) => assertRuntimeCode(error, "control_owner_mismatch"),
  );
});

test("e03 structured router failure rejects delegated response identity mismatch", async () => {
  const fixture = recordingRouter();
  const delegate: ControlDelegate = {
    async execute(request) {
      return response({
        ok: true,
        request_id: `${request.request_id}-wrong`,
        command: request.command,
        phase: "commit",
        state: { canonical_owner: "E01" },
      });
    },
  };
  const router = new StructuredControlRouter(
    fixture.agent,
    fixture.e03,
    delegate,
    fixture.e02,
  );
  await assert.rejects(
    () => router.dispatch(envelope("router-identity", "context.compact", {})),
    (error) => assertRuntimeCode(error, "delegated_control_identity"),
  );
});

test("e03 structured router failure rejects false E03 ownership claim", async () => {
  const fixture = recordingRouter();
  const delegate: ControlDelegate = {
    async execute(request) {
      return successfulControlResponse(request, "E03", { false_claim: true });
    },
  };
  const router = new StructuredControlRouter(
    fixture.agent,
    fixture.e03,
    delegate,
    fixture.e02,
  );
  await assert.rejects(
    () => router.dispatch(envelope("router-owner", "context.clear", {})),
    (error) => assertRuntimeCode(error, "delegated_control_owner"),
  );
});

test("e03 structured router lost-ack recovery stays with E03 agent handler", () => {
  const fixture = recordingRouter();
  const request = envelope("router-lost-ack", "agent.status");
  request.idempotency_key = "known-lost-ack";
  const recovered = fixture.router.recoverLostAck(request);
  assert.ok(recovered);
  assert.equal(recovered?.ok, true);
  assert.equal(recovered?.phase, "ack");
  assert.equal(recovered?.replayed, true);
  assert.equal(recovered?.request_id, "recovered-request");
  request.idempotency_key = "unknown-lost-ack";
  assert.equal(fixture.router.recoverLostAck(request), null);
});

test("e03 structured router does not claim predecessor lost-ack recovery", () => {
  const fixture = recordingRouter();
  assert.equal(
    fixture.router.recoverLostAck(
      envelope("router-e01-ack", "context.compact", {}),
    ),
    null,
  );
  assert.equal(
    fixture.router.recoverLostAck(
      envelope("router-e02-ack", "permission.inspect", {}),
    ),
    null,
  );
});

test("e03 route registry registers a canonical route descriptor", () => {
  const { registry, route } = registryWithRoute("register");
  assert.equal(route.routeId, "route-register");
  assert.equal(route.command, "agent.status");
  assert.equal(route.owner, "E03");
  assert.equal(route.handlerId, "handler-register");
  assert.equal(route.priority, 10);
  assert.equal(route.state, "active");
  assert.equal(route.mutation, false);
  assert.equal(route.physicalEffect, false);
  assert.equal(route.supportsReplay, true);
  assert.equal(route.supportsLostAck, true);
  assert.equal(route.maximumInFlight, 2);
  assert.equal(route.timeoutMs, 30_000);
  assert.equal(route.revision, 1);
  assert.equal(route.digest.length, 64);
  assert.deepEqual(registry.snapshot().routes, [route]);
  assert.deepEqual(registry.snapshot().leases, []);
});

test("e03 route registry re-registration advances revision and retains origin", () => {
  const { registry, route: first } = registryWithRoute("revision");
  const second = registerRoute(registry, "revision", {
    priority: 3,
    maximumInFlight: 5,
    timeoutMs: 60_000,
  });
  assert.equal(second.routeId, first.routeId);
  assert.equal(second.command, first.command);
  assert.equal(second.registeredAt, first.registeredAt);
  assert.equal(second.revision, 2);
  assert.equal(second.priority, 3);
  assert.equal(second.maximumInFlight, 5);
  assert.equal(second.timeoutMs, 60_000);
  assert.notEqual(second.digest, first.digest);
  assert.equal(registry.snapshot().routes.length, 1);
});

test("e03 route registry decision selects lowest priority active route", () => {
  const registry = new ControlRouteRegistry();
  const slow = registerRoute(registry, "slow", { priority: 20 });
  const preferred = registerRoute(registry, "preferred", { priority: 1 });
  const middle = registerRoute(registry, "middle", { priority: 10 });
  const decision = registry.decide(
    envelope("decision-priority"),
    "2026-07-18T19:00:01.000Z",
  );
  assert.equal(decision.accepted, true);
  assert.equal(decision.code, "control_route_accepted");
  assert.equal(decision.routeId, preferred.routeId);
  assert.equal(decision.handlerId, preferred.handlerId);
  assert.equal(decision.owner, "E03");
  assert.equal(decision.routeState, "active");
  assert.equal(decision.inFlight, 0);
  assert.equal(decision.maximumInFlight, preferred.maximumInFlight);
  assert.equal(decision.selectedAt, "2026-07-18T19:00:01.000Z");
  assert.equal(decision.digest.length, 64);
  assert.notEqual(decision.routeId, slow.routeId);
  assert.notEqual(decision.routeId, middle.routeId);
});

test("e03 route registry decision falls through a saturated route", () => {
  const registry = new ControlRouteRegistry();
  const first = registerRoute(registry, "saturated-first", {
    priority: 1,
    maximumInFlight: 1,
  });
  const second = registerRoute(registry, "saturated-second", {
    priority: 2,
    maximumInFlight: 1,
  });
  const firstLease = registry.acquire(
    envelope("saturated-request-first"),
    "2026-07-18T19:00:02.000Z",
  );
  assert.equal(firstLease.routeId, first.routeId);
  const decision = registry.decide(
    envelope("saturated-request-second"),
    "2026-07-18T19:00:03.000Z",
  );
  assert.equal(decision.accepted, true);
  assert.equal(decision.routeId, second.routeId);
  assert.equal(decision.inFlight, 0);
  assert.equal(decision.maximumInFlight, 1);
});

test("e03 route registry failure reports missing route", () => {
  const registry = new ControlRouteRegistry();
  const decision = registry.decide(envelope("decision-missing"));
  assert.equal(decision.accepted, false);
  assert.equal(decision.code, "control_route_missing");
  assert.equal(decision.routeId, null);
  assert.equal(decision.owner, null);
  assert.equal(decision.handlerId, null);
  assert.equal(decision.routeState, null);
  assert.equal(decision.inFlight, 0);
  assert.equal(decision.maximumInFlight, 0);
  assert.throws(
    () => registry.acquire(envelope("acquire-missing")),
    (error) => assertRuntimeCode(error, "control_route_missing"),
  );
});

test("e03 route registry failure reports unavailable disabled route", () => {
  const { registry, route } = registryWithRoute("disabled", {
    state: "disabled",
  });
  const decision = registry.decide(envelope("decision-disabled"));
  assert.equal(decision.accepted, false);
  assert.equal(decision.code, "control_route_unavailable");
  assert.equal(decision.routeId, route.routeId);
  assert.equal(decision.routeState, "disabled");
  assert.equal(decision.handlerId, route.handlerId);
  assert.throws(
    () => registry.acquire(envelope("acquire-disabled")),
    (error) => assertRuntimeCode(error, "control_route_unavailable"),
  );
});

test("e03 route registry failure reports saturation at maximum in-flight", () => {
  const { registry, route } = registryWithRoute("saturated", {
    maximumInFlight: 1,
  });
  const first = registry.acquire(
    envelope("saturation-first"),
    "2026-07-18T19:00:04.000Z",
  );
  assert.equal(first.routeId, route.routeId);
  const decision = registry.decide(
    envelope("saturation-second"),
    "2026-07-18T19:00:05.000Z",
  );
  assert.equal(decision.accepted, false);
  assert.equal(decision.code, "control_route_saturated");
  assert.equal(decision.routeId, route.routeId);
  assert.equal(decision.inFlight, 1);
  assert.equal(decision.maximumInFlight, 1);
  assert.throws(
    () => registry.acquire(envelope("saturation-second")),
    (error) => assertRuntimeCode(error, "control_route_saturated"),
  );
});

test("e03 route lease acquisition records route custody and deadline", () => {
  const { registry, route } = registryWithRoute("lease", { timeoutMs: 10_000 });
  const request = envelope("lease-acquire");
  const lease = registry.acquire(request, "2026-07-18T19:10:00.000Z");
  assert.match(lease.routeLeaseId, /^control-route-lease-/);
  assert.equal(lease.routeId, route.routeId);
  assert.equal(lease.command, request.command);
  assert.equal(lease.requestId, request.request_id);
  assert.equal(lease.idempotencyKey, request.idempotency_key);
  assert.equal(lease.handlerId, route.handlerId);
  assert.equal(lease.owner, route.owner);
  assert.equal(lease.expectedRouteRevision, route.revision);
  assert.equal(lease.acquiredAt, "2026-07-18T19:10:00.000Z");
  assert.equal(lease.expiresAt, "2026-07-18T19:10:10.000Z");
  assert.equal(lease.releasedAt, null);
  assert.equal(lease.outcome, "active");
  assert.equal(lease.errorCode, "");
  assert.equal(lease.revision, 1);
  assert.equal(lease.digest.length, 64);
});

test("e03 route lease acquisition replays by idempotency key", () => {
  const { registry } = registryWithRoute("lease-replay");
  const request = envelope("lease-replay-request");
  const first = registry.acquire(request, "2026-07-18T19:11:00.000Z");
  const replay = registry.acquire(
    { ...request, request_id: "different-request-id" },
    "2026-07-18T19:11:05.000Z",
  );
  assert.equal(replay.routeLeaseId, first.routeLeaseId);
  assert.equal(replay.digest, first.digest);
  assert.equal(replay.requestId, first.requestId);
  assert.equal(replay.acquiredAt, first.acquiredAt);
  assert.equal(registry.snapshot().leases.length, 1);
});

test("e03 route lease release records successful physical completion", () => {
  const { registry } = registryWithRoute("release-success");
  const lease = registry.acquire(
    envelope("release-success-request"),
    "2026-07-18T19:12:00.000Z",
  );
  const released = registry.release({
    routeLeaseId: lease.routeLeaseId,
    outcome: "succeeded",
    now: "2026-07-18T19:12:02.000Z",
  });
  assert.equal(released.outcome, "succeeded");
  assert.equal(released.releasedAt, "2026-07-18T19:12:02.000Z");
  assert.equal(released.errorCode, "");
  assert.equal(released.revision, 2);
  assert.notEqual(released.digest, lease.digest);
  const decision = registry.decide(envelope("post-release-capacity"));
  assert.equal(decision.accepted, true);
  assert.equal(decision.inFlight, 0);
});

test("e03 route lease failure release records error code", () => {
  const { registry } = registryWithRoute("release-failure");
  const lease = registry.acquire(
    envelope("release-failure-request"),
    "2026-07-18T19:13:00.000Z",
  );
  const released = registry.release({
    routeLeaseId: lease.routeLeaseId,
    outcome: "failed",
    errorCode: "handler_crashed",
    now: "2026-07-18T19:13:03.000Z",
  });
  assert.equal(released.outcome, "failed");
  assert.equal(released.errorCode, "handler_crashed");
  assert.equal(released.releasedAt, "2026-07-18T19:13:03.000Z");
  assert.equal(released.revision, 2);
  const replay = registry.release({
    routeLeaseId: lease.routeLeaseId,
    outcome: "cancelled",
    errorCode: "ignored",
  });
  assert.equal(replay.digest, released.digest);
  assert.equal(replay.outcome, "failed");
});

test("e03 route lease failure detects route revision change", () => {
  const { registry, route } = registryWithRoute("stale-release");
  const lease = registry.acquire(
    envelope("stale-release-request"),
    "2026-07-18T19:14:00.000Z",
  );
  const changed = registerRoute(registry, "stale-release", { priority: 1 });
  assert.equal(changed.routeId, route.routeId);
  assert.equal(changed.revision, route.revision + 1);
  assert.throws(
    () =>
      registry.release({
        routeLeaseId: lease.routeLeaseId,
        outcome: "succeeded",
        now: "2026-07-18T19:14:01.000Z",
      }),
    (error) => assertRuntimeCode(error, "control_route_stale_revision"),
  );
});

test("e03 route lease permits release while a changed route drains", () => {
  const { registry } = registryWithRoute("draining-release");
  const lease = registry.acquire(
    envelope("draining-release-request"),
    "2026-07-18T19:15:00.000Z",
  );
  registerRoute(registry, "draining-release", { state: "draining" });
  const released = registry.release({
    routeLeaseId: lease.routeLeaseId,
    outcome: "succeeded",
    now: "2026-07-18T19:15:01.000Z",
  });
  assert.equal(released.outcome, "succeeded");
  assert.equal(released.releasedAt, "2026-07-18T19:15:01.000Z");
  assert.equal(
    registry.decide(envelope("draining-new-request")).accepted,
    false,
  );
  assert.equal(
    registry.decide(envelope("draining-new-request")).code,
    "control_route_unavailable",
  );
});

test("e03 route lease expiry reports only active expired custody", () => {
  const { registry } = registryWithRoute("expiry", { timeoutMs: 1_000 });
  const expired = registry.acquire(
    envelope("expiry-active"),
    "2026-07-18T19:16:00.000Z",
  );
  const settled = registry.acquire(
    envelope("expiry-settled"),
    "2026-07-18T19:16:00.100Z",
  );
  registry.release({
    routeLeaseId: settled.routeLeaseId,
    outcome: "succeeded",
    now: "2026-07-18T19:16:00.500Z",
  });
  assert.deepEqual(registry.expired("2026-07-18T19:16:00.999Z"), []);
  const values = registry.expired("2026-07-18T19:16:01.500Z");
  assert.equal(values.length, 1);
  assert.equal(values[0]?.routeLeaseId, expired.routeLeaseId);
  assert.equal(values[0]?.outcome, "active");
});

test("e03 route failure prevents disabling route with active custody", () => {
  const { registry, route } = registryWithRoute("active-disable");
  const lease = registry.acquire(envelope("active-disable-request"));
  assert.equal(lease.routeId, route.routeId);
  assert.throws(
    () => registry.setState(route.routeId, "disabled"),
    (error) => assertRuntimeCode(error, "control_route_active_disable"),
  );
  const draining = registry.setState(route.routeId, "draining");
  assert.equal(draining.state, "draining");
  assert.equal(draining.revision, route.revision + 1);
});

test("e03 route state can disable after active custody releases", () => {
  const { registry, route } = registryWithRoute("disable-released");
  const lease = registry.acquire(envelope("disable-released-request"));
  registry.release({ routeLeaseId: lease.routeLeaseId, outcome: "succeeded" });
  const disabled = registry.setState(route.routeId, "disabled");
  assert.equal(disabled.state, "disabled");
  assert.equal(disabled.revision, route.revision + 1);
  const decision = registry.decide(envelope("disabled-after-release"));
  assert.equal(decision.accepted, false);
  assert.equal(decision.code, "control_route_unavailable");
});

test("e03 route snapshot restores routing and active lease capacity", () => {
  const registry = new ControlRouteRegistry();
  const first = registerRoute(registry, "snapshot-first", {
    priority: 1,
    maximumInFlight: 1,
  });
  const second = registerRoute(registry, "snapshot-second", {
    priority: 2,
    maximumInFlight: 1,
  });
  const lease = registry.acquire(
    envelope("snapshot-active"),
    "2026-07-18T19:20:00.000Z",
  );
  assert.equal(lease.routeId, first.routeId);
  const snapshot = registry.snapshot();
  const restored = new ControlRouteRegistry();
  restored.restore(snapshot);
  assert.deepEqual(restored.snapshot(), snapshot);
  const decision = restored.decide(
    envelope("snapshot-next"),
    "2026-07-18T19:20:01.000Z",
  );
  assert.equal(decision.accepted, true);
  assert.equal(decision.routeId, second.routeId);
  assert.equal(decision.inFlight, 0);
});

test("e03 route restore failure rejects corrupt descriptor", () => {
  const { registry, route } = registryWithRoute("restore-corrupt-route");
  const corrupted = { ...route, priority: route.priority + 1 };
  assert.throws(
    () => registry.restore({ routes: [corrupted], leases: [] }),
    (error) => assertRuntimeCode(error, "control_route_checksum"),
  );
});

test("e03 route restore failure rejects duplicate descriptor", () => {
  const { registry, route } = registryWithRoute("restore-duplicate-route");
  assert.throws(
    () =>
      registry.restore({ routes: [route, structuredClone(route)], leases: [] }),
    (error) => assertRuntimeCode(error, "duplicate_control_route"),
  );
});

test("e03 route restore failure rejects orphaned lease", () => {
  const { registry } = registryWithRoute("restore-orphan-source");
  const lease = registry.acquire(envelope("restore-orphan-request"));
  assert.throws(
    () => registry.restore({ routes: [], leases: [lease] }),
    (error) => assertRuntimeCode(error, "control_route_lease_route_missing"),
  );
});

test("e03 route restore failure rejects duplicate lease identity", () => {
  const { registry, route } = registryWithRoute("restore-duplicate-lease");
  const lease = registry.acquire(envelope("restore-duplicate-lease-request"));
  assert.throws(
    () =>
      registry.restore({
        routes: [route],
        leases: [lease, structuredClone(lease)],
      }),
    (error) => assertRuntimeCode(error, "duplicate_control_route_lease"),
  );
});

test("e03 route registration failure rejects invalid descriptor bounds", () => {
  const registry = new ControlRouteRegistry();
  assert.throws(
    () => registerRoute(registry, "invalid-flight", { maximumInFlight: 0 }),
    (error) => assertRuntimeCode(error, "control_route_invalid"),
  );
  assert.throws(
    () => registerRoute(registry, "invalid-timeout", { timeoutMs: 0 }),
    (error) => assertRuntimeCode(error, "control_route_invalid"),
  );
  assert.throws(
    () => registerRoute(registry, "invalid-priority", { priority: -1 }),
    (error) => assertRuntimeCode(error, "control_route_invalid"),
  );
});

test("e03 route registration failure prevents command reassignment", () => {
  const { registry } = registryWithRoute("command-change");
  assert.throws(
    () =>
      registerRoute(registry, "command-change", {
        command: "agent.cancel",
        mutation: true,
        physicalEffect: true,
      }),
    (error) => assertRuntimeCode(error, "control_route_command_changed"),
  );
});

test("e03 route failure rejects release of unknown lease", () => {
  const { registry } = registryWithRoute("missing-lease");
  assert.throws(
    () =>
      registry.release({
        routeLeaseId: "missing-route-lease",
        outcome: "failed",
        errorCode: "missing",
      }),
    (error) => assertRuntimeCode(error, "control_route_lease_missing"),
  );
});

test("e03 route decision digest commits selected routing facts", () => {
  const { registry, route } = registryWithRoute("decision-digest", {
    owner: "E02",
    command: "permission.inspect",
    handlerId: "permission-handler",
  });
  const decision = registry.decide(
    envelope("decision-digest", "permission.inspect", {}),
    "2026-07-18T19:30:00.000Z",
  );
  const { digest: checksum, ...payload } = decision;
  assert.equal(decision.accepted, true);
  assert.equal(decision.routeId, route.routeId);
  assert.equal(decision.owner, "E02");
  assert.equal(decision.handlerId, "permission-handler");
  assert.equal(checksum, digest(payload));
});
