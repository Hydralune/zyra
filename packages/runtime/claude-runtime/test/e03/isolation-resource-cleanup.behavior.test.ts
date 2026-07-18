import assert from "node:assert/strict";
import { test } from "node:test";
import { resolve } from "node:path";
import { digest, E03RuntimeError } from "../../src/e03/contracts.ts";
import {
  IsolationMergeRuntime,
  type CleanupReceipt,
  WorktreeCleanupSettlementRuntime,
} from "../../src/isolation/merge-runtime.ts";
import {
  IsolationRequestRuntime,
  type IsolationResourceBudget,
  IsolationResourceReservationRuntime,
} from "../../src/isolation/request-runtime.ts";
import { scope, task, TestClock } from "./fixtures.ts";

function assertCode(error: unknown, code: string): boolean {
  assert.ok(error instanceof E03RuntimeError);
  assert.equal(error.code, code);
  assert.ok(error.message.trim());
  return true;
}

function resourceBudget(
  overrides: Partial<IsolationResourceBudget> = {},
): IsolationResourceBudget {
  return {
    cpuMillis: overrides.cpuMillis ?? 10_000,
    memoryByteMillis: overrides.memoryByteMillis ?? 20_000,
    filesystemWriteBytes: overrides.filesystemWriteBytes ?? 30_000,
    networkEgressBytes: overrides.networkEgressBytes ?? 40_000,
    processCount: overrides.processCount ?? 5,
  };
}

function resourceRuntime(label: string) {
  const clock = new TestClock("2026-07-18T13:00:00.000Z");
  const runtime = new IsolationResourceReservationRuntime(clock);
  const reservation = runtime.reserve({
    requestId: `request-${label}`,
    taskId: `task-${label}`,
    leaseId: `lease-${label}`,
    runtimeId: `runtime-${label}`,
    budget: resourceBudget(),
    reservationFence: `fence-${label}`,
    leaseMs: 60_000,
  });
  return { clock, runtime, reservation };
}

function activeResourceRuntime(label: string) {
  const harness = resourceRuntime(label);
  const active = harness.runtime.activate({
    reservationId: harness.reservation.reservationId,
    expectedRevision: harness.reservation.revision,
    leaseId: harness.reservation.leaseId,
    runtimeId: harness.reservation.runtimeId,
  });
  return { ...harness, active };
}

function cleanupReceipt(label: string): CleanupReceipt {
  const payload = {
    cleanupId: `cleanup-${label}`,
    taskId: `task-${label}`,
    requestId: `request-${label}`,
    leaseId: `lease-${label}`,
    removed: true,
    retainedArtifacts: [`artifact-${label}`],
    error: "",
    completedAt: "2026-07-18T14:00:00.000Z",
  };
  return { ...payload, digest: digest(payload) };
}

function cleanupRuntime(label: string) {
  const clock = new TestClock("2026-07-18T14:05:00.000Z");
  const runtime = new WorktreeCleanupSettlementRuntime(clock);
  const cleanup = cleanupReceipt(label);
  const planned = runtime.plan({
    cleanup,
    resources: [
      {
        resourceKind: "worktree",
        resourceId: `worktree-${label}`,
        expectedPresent: false,
      },
      {
        resourceKind: "process",
        resourceId: `process-${label}`,
        expectedPresent: false,
      },
    ],
  });
  return { clock, runtime, cleanup, planned };
}

test("e03.isolation reserves a zero-consumption budget", () => {
  const { runtime, reservation } = resourceRuntime("reserve");
  assert.equal(reservation.state, "reserved");
  assert.equal(reservation.revision, 1);
  assert.equal(reservation.requestId, "request-reserve");
  assert.equal(reservation.taskId, "task-reserve");
  assert.equal(reservation.leaseId, "lease-reserve");
  assert.equal(reservation.runtimeId, "runtime-reserve");
  assert.deepEqual(reservation.budget, resourceBudget());
  assert.deepEqual(
    reservation.consumed,
    resourceBudget({
      cpuMillis: 0,
      memoryByteMillis: 0,
      filesystemWriteBytes: 0,
      networkEgressBytes: 0,
      processCount: 0,
    }),
  );
  assert.deepEqual(reservation.usageSampleIds, []);
  assert.equal(runtime.snapshot().reservations.length, 1);
});

test("e03.isolation replays reservation fence", () => {
  const { runtime, reservation } = resourceRuntime("fence-replay");
  const replay = runtime.reserve({
    requestId: "request-fence-replay-second",
    taskId: reservation.taskId,
    leaseId: reservation.leaseId,
    runtimeId: reservation.runtimeId,
    budget: resourceBudget({ cpuMillis: 1 }),
    reservationFence: reservation.reservationFence,
    leaseMs: 120_000,
  });
  assert.equal(replay.reservationId, reservation.reservationId);
  assert.equal(replay.digest, reservation.digest);
  assert.deepEqual(replay.budget, reservation.budget);
  assert.equal(replay.expiresAt, reservation.expiresAt);
  assert.equal(runtime.snapshot().reservations.length, 1);
});

test("e03.isolation rejects reservation fence conflict", () => {
  const { runtime, reservation } = resourceRuntime("fence-conflict");
  assert.throws(
    () =>
      runtime.reserve({
        requestId: "request-fence-conflict-other",
        taskId: "task-other",
        leaseId: "lease-other",
        runtimeId: "runtime-other",
        budget: resourceBudget(),
        reservationFence: reservation.reservationFence,
        leaseMs: 60_000,
      }),
    (error) =>
      assertCode(error, "isolation_resource_reservation_fence_conflict"),
  );
  assert.equal(runtime.snapshot().reservations.length, 1);
  assert.equal(runtime.snapshot().reservations[0]?.taskId, reservation.taskId);
});

test("e03.isolation rejects active reservation conflict", () => {
  const { runtime, reservation } = resourceRuntime("active-conflict");
  assert.throws(
    () =>
      runtime.reserve({
        requestId: "request-active-conflict-second",
        taskId: reservation.taskId,
        leaseId: "lease-active-conflict-second",
        runtimeId: "runtime-active-conflict-second",
        budget: resourceBudget(),
        reservationFence: "fence-active-conflict-second",
        leaseMs: 60_000,
      }),
    (error) =>
      assertCode(error, "isolation_resource_reservation_active_conflict"),
  );
  assert.equal(runtime.snapshot().activeReservationIdByTask.length, 1);
});

test("e03.isolation rejects invalid negative budget", () => {
  const runtime = new IsolationResourceReservationRuntime();
  assert.throws(
    () =>
      runtime.reserve({
        requestId: "request-negative",
        taskId: "task-negative",
        leaseId: "lease-negative",
        runtimeId: "runtime-negative",
        budget: resourceBudget({ networkEgressBytes: -1 }),
        reservationFence: "fence-negative",
        leaseMs: 60_000,
      }),
    (error) => assertCode(error, "isolation_resource_budget_invalid"),
  );
  assert.equal(runtime.snapshot().reservations.length, 0);
});

test("e03.isolation activates matching reservation", () => {
  const { runtime, reservation, active } = activeResourceRuntime("activate");
  assert.equal(active.state, "active");
  assert.equal(active.revision, reservation.revision + 1);
  assert.ok(active.activatedAt);
  assert.equal(active.leaseId, reservation.leaseId);
  assert.equal(active.runtimeId, reservation.runtimeId);
  const replay = runtime.activate({
    reservationId: active.reservationId,
    expectedRevision: active.revision,
    leaseId: active.leaseId,
    runtimeId: active.runtimeId,
  });
  assert.equal(replay.digest, active.digest);
  assert.equal(replay.revision, active.revision);
});

test("e03.isolation rejects stale activation binding", () => {
  const { runtime, reservation } = resourceRuntime("activation-stale");
  assert.throws(
    () =>
      runtime.activate({
        reservationId: reservation.reservationId,
        expectedRevision: reservation.revision,
        leaseId: "stale-lease",
        runtimeId: reservation.runtimeId,
      }),
    (error) =>
      assertCode(error, "isolation_resource_activation_binding_mismatch"),
  );
  assert.equal(runtime.snapshot().reservations[0]?.state, "reserved");
  assert.equal(runtime.snapshot().reservations[0]?.revision, 1);
});

test("e03.isolation rejects activation after timeout", () => {
  const { runtime, reservation } = resourceRuntime("activation-timeout");
  assert.throws(
    () =>
      runtime.activate({
        reservationId: reservation.reservationId,
        expectedRevision: reservation.revision,
        leaseId: reservation.leaseId,
        runtimeId: reservation.runtimeId,
        now: "2026-07-18T14:00:00.000Z",
      }),
    (error) => assertCode(error, "isolation_resource_reservation_expired"),
  );
  assert.equal(runtime.snapshot().reservations[0]?.state, "reserved");
});

test("e03.isolation records monotonic resource usage", () => {
  const { runtime, active } = activeResourceRuntime("usage");
  const firstUsage = resourceBudget({
    cpuMillis: 100,
    memoryByteMillis: 200,
    filesystemWriteBytes: 300,
    networkEgressBytes: 400,
    processCount: 1,
  });
  const first = runtime.recordUsage({
    reservationId: active.reservationId,
    expectedRevision: active.revision,
    leaseId: active.leaseId,
    observerId: "observer-usage",
    cumulative: firstUsage,
    evidenceDigest: digest("usage-evidence-1"),
  });
  assert.equal(first.sample.sequence, 1);
  assert.deepEqual(first.sample.cumulative, firstUsage);
  assert.equal(first.reservation.revision, active.revision + 1);
  assert.deepEqual(first.reservation.consumed, firstUsage);
  assert.deepEqual(first.reservation.usageSampleIds, [first.sample.sampleId]);
  assert.equal(runtime.snapshot().samples.length, 1);
});

test("e03.isolation rejects non-monotonic usage", () => {
  const { runtime, active } = activeResourceRuntime("usage-regression");
  const first = runtime.recordUsage({
    reservationId: active.reservationId,
    expectedRevision: active.revision,
    leaseId: active.leaseId,
    observerId: "observer-regression",
    cumulative: resourceBudget({ cpuMillis: 500 }),
    evidenceDigest: digest("regression-first"),
  });
  assert.throws(
    () =>
      runtime.recordUsage({
        reservationId: first.reservation.reservationId,
        expectedRevision: first.reservation.revision,
        leaseId: first.reservation.leaseId,
        observerId: "observer-regression",
        cumulative: resourceBudget({ cpuMillis: 499 }),
        evidenceDigest: digest("regression-second"),
      }),
    (error) => assertCode(error, "isolation_resource_usage_non_monotonic"),
  );
  assert.equal(runtime.snapshot().samples.length, 1);
  assert.equal(runtime.snapshot().reservations[0]?.consumed.cpuMillis, 500);
});

test("e03.isolation rejects stale usage lease", () => {
  const { runtime, active } = activeResourceRuntime("usage-stale-lease");
  assert.throws(
    () =>
      runtime.recordUsage({
        reservationId: active.reservationId,
        expectedRevision: active.revision,
        leaseId: "lease-from-old-attempt",
        observerId: "observer-stale",
        cumulative: resourceBudget({ cpuMillis: 1 }),
        evidenceDigest: digest("stale-usage"),
      }),
    (error) => assertCode(error, "isolation_resource_usage_lease_stale"),
  );
  assert.equal(runtime.snapshot().samples.length, 0);
  assert.deepEqual(runtime.snapshot().reservations[0]?.usageSampleIds, []);
});

test("e03.isolation settles usage and detects exceeded dimensions", () => {
  const { runtime, active } = activeResourceRuntime("settlement-exceeded");
  const usage = resourceBudget({
    cpuMillis: 10_001,
    memoryByteMillis: 20_000,
    filesystemWriteBytes: 31_000,
    networkEgressBytes: 40_000,
    processCount: 6,
  });
  const recorded = runtime.recordUsage({
    reservationId: active.reservationId,
    expectedRevision: active.revision,
    leaseId: active.leaseId,
    observerId: "observer-settlement",
    cumulative: usage,
    evidenceDigest: digest("settlement-usage"),
  });
  const settling = runtime.beginSettlement({
    reservationId: recorded.reservation.reservationId,
    expectedRevision: recorded.reservation.revision,
    leaseId: recorded.reservation.leaseId,
  });
  const settled = runtime.settle({
    reservationId: settling.reservationId,
    expectedRevision: settling.revision,
    leaseId: settling.leaseId,
    outcome: "completed",
    terminalEffectDigest: digest("terminal-effect"),
  });
  assert.equal(settled.reservation.state, "settled");
  assert.equal(settled.settlement.outcome, "completed");
  assert.deepEqual(settled.settlement.finalUsage, usage);
  assert.deepEqual(settled.settlement.exceededDimensions.sort(), [
    "cpuMillis",
    "filesystemWriteBytes",
    "processCount",
  ]);
  assert.equal(runtime.snapshot().activeReservationIdByTask.length, 0);
});

test("e03.isolation replays completed settlement", () => {
  const { runtime, active } = activeResourceRuntime("settlement-replay");
  const settling = runtime.beginSettlement({
    reservationId: active.reservationId,
    expectedRevision: active.revision,
    leaseId: active.leaseId,
  });
  const first = runtime.settle({
    reservationId: settling.reservationId,
    expectedRevision: settling.revision,
    leaseId: settling.leaseId,
    outcome: "completed",
    terminalEffectDigest: digest("settlement-replay-effect"),
  });
  const replay = runtime.settle({
    reservationId: first.reservation.reservationId,
    expectedRevision: first.reservation.revision,
    leaseId: first.reservation.leaseId,
    outcome: "failed",
    terminalEffectDigest: digest("changed-effect"),
  });
  assert.equal(replay.reservation.digest, first.reservation.digest);
  assert.equal(replay.settlement.digest, first.settlement.digest);
  assert.equal(replay.settlement.outcome, "completed");
  assert.equal(runtime.snapshot().settlements.length, 1);
});

test("e03.isolation rejects settlement before begin", () => {
  const { runtime, active } = activeResourceRuntime("settlement-invalid");
  assert.throws(
    () =>
      runtime.settle({
        reservationId: active.reservationId,
        expectedRevision: active.revision,
        leaseId: active.leaseId,
        outcome: "failed",
        terminalEffectDigest: digest("invalid-settlement"),
      }),
    (error) => assertCode(error, "isolation_resource_settlement_invalid_state"),
  );
  assert.equal(runtime.snapshot().settlements.length, 0);
  assert.equal(runtime.snapshot().reservations[0]?.state, "active");
});

test("e03.isolation expires lost reservations", () => {
  const { runtime, reservation } = resourceRuntime("expire-lost");
  const expired = runtime.expire("2026-07-18T15:00:00.000Z");
  assert.equal(expired.length, 1);
  assert.equal(expired[0]?.reservationId, reservation.reservationId);
  assert.equal(expired[0]?.state, "expired");
  assert.equal(runtime.snapshot().activeReservationIdByTask.length, 0);
  assert.equal(runtime.snapshot().reservations[0]?.state, "expired");
});

test("e03.isolation revokes active resource custody", () => {
  const { runtime, active } = activeResourceRuntime("revoke");
  const revoked = runtime.revoke({
    reservationId: active.reservationId,
    expectedRevision: active.revision,
    leaseId: active.leaseId,
  });
  assert.equal(revoked.state, "revoked");
  assert.equal(revoked.revision, active.revision + 1);
  assert.equal(runtime.snapshot().activeReservationIdByTask.length, 0);
  assert.equal(runtime.snapshot().reservations[0]?.digest, revoked.digest);
});

test("e03.isolation restores resource usage chain", () => {
  const { runtime, active } = activeResourceRuntime("restore-chain");
  const first = runtime.recordUsage({
    reservationId: active.reservationId,
    expectedRevision: active.revision,
    leaseId: active.leaseId,
    observerId: "observer-restore",
    cumulative: resourceBudget({ cpuMillis: 100 }),
    evidenceDigest: digest("restore-first"),
  });
  runtime.recordUsage({
    reservationId: first.reservation.reservationId,
    expectedRevision: first.reservation.revision,
    leaseId: first.reservation.leaseId,
    observerId: "observer-restore",
    cumulative: resourceBudget({ cpuMillis: 200 }),
    evidenceDigest: digest("restore-second"),
  });
  const snapshot = runtime.snapshot();
  const restored = new IsolationResourceReservationRuntime();
  restored.restore(snapshot);
  assert.deepEqual(restored.snapshot(), snapshot);
  assert.equal(restored.snapshot().samples.length, 2);
  assert.equal(restored.snapshot().samples[1]?.sequence, 2);
  assert.equal(
    restored.snapshot().samples[1]?.previousSampleDigest,
    restored.snapshot().samples[0]?.digest,
  );
});

test("e03.isolation rejects corrupt resource snapshot", () => {
  const { runtime } = resourceRuntime("snapshot-corrupt");
  const snapshot = runtime.snapshot();
  const tampered = {
    ...snapshot,
    activeReservationIdByTask: [["task-orphan", "reservation-orphan"]] as Array<
      [string, string]
    >,
  };
  const restored = new IsolationResourceReservationRuntime();
  assert.throws(
    () => restored.restore(tampered),
    (error) => assertCode(error, "isolation_resource_snapshot_corrupt"),
  );
  assert.equal(restored.snapshot().reservations.length, 0);
});

test("e03.cleanup plans resource claims", () => {
  const { runtime, cleanup, planned } = cleanupRuntime("plan");
  assert.equal(planned.cleanupId, cleanup.cleanupId);
  assert.equal(planned.taskId, cleanup.taskId);
  assert.equal(planned.leaseId, cleanup.leaseId);
  assert.equal(planned.state, "planned");
  assert.equal(planned.revision, 1);
  assert.equal(planned.requiredClaimIds.length, 2);
  assert.deepEqual(planned.satisfiedClaimIds, []);
  assert.deepEqual(planned.blockingClaimIds, []);
  assert.deepEqual(planned.retainedArtifactIds, cleanup.retainedArtifacts);
  assert.equal(runtime.snapshot().settlements.length, 1);
  assert.equal(runtime.snapshot().claims.length, 2);
});

test("e03.cleanup replays plan for same receipt", () => {
  const { runtime, cleanup, planned } = cleanupRuntime("plan-replay");
  const replay = runtime.plan({
    cleanup,
    resources: [{ resourceKind: "mount", resourceId: "ignored-new-resource" }],
  });
  assert.equal(replay.settlementId, planned.settlementId);
  assert.equal(replay.digest, planned.digest);
  assert.deepEqual(replay.requiredClaimIds, planned.requiredClaimIds);
  assert.equal(runtime.snapshot().settlements.length, 1);
  assert.equal(runtime.snapshot().claims.length, 2);
});

test("e03.cleanup rejects duplicate resource claim", () => {
  const runtime = new WorktreeCleanupSettlementRuntime();
  const cleanup = cleanupReceipt("duplicate-claim");
  assert.throws(
    () =>
      runtime.plan({
        cleanup,
        resources: [
          { resourceKind: "worktree", resourceId: "same-resource" },
          { resourceKind: "worktree", resourceId: "same-resource" },
        ],
      }),
    (error) => assertCode(error, "cleanup_settlement_resource_duplicate"),
  );
  assert.equal(runtime.snapshot().settlements.length, 0);
});

test("e03.cleanup verifies absent resources and releases", () => {
  const { runtime, cleanup, planned } = cleanupRuntime("release");
  let settlement = runtime.begin({
    settlementId: planned.settlementId,
    expectedRevision: planned.revision,
    cleanupReceiptDigest: cleanup.digest,
  });
  for (const claim of runtime.snapshot().claims) {
    runtime.observe({
      settlementId: settlement.settlementId,
      claimId: claim.claimId,
      expectedSettlementRevision: settlement.revision,
      expectedClaimRevision: claim.revision,
      observerId: "cleanup-observer",
      observedPresent: false,
      observationDigest: digest({ claim: claim.claimId, present: false }),
    });
  }
  settlement = runtime.reconcile({
    settlementId: settlement.settlementId,
    expectedRevision: settlement.revision,
  });
  assert.equal(settlement.state, "verified");
  assert.equal(settlement.satisfiedClaimIds.length, 2);
  assert.deepEqual(settlement.blockingClaimIds, []);
  const released = runtime.release({
    settlementId: settlement.settlementId,
    expectedRevision: settlement.revision,
    leaseId: cleanup.leaseId,
    releaseFence: "release-fence",
  });
  assert.equal(released.state, "released");
  assert.equal(released.releaseFence, "release-fence");
  assert.equal(runtime.snapshot().evidence.length, 2);
});

test("e03.cleanup rejects release before verification", () => {
  const { runtime, cleanup, planned } = cleanupRuntime("release-unverified");
  assert.throws(
    () =>
      runtime.release({
        settlementId: planned.settlementId,
        expectedRevision: planned.revision,
        leaseId: cleanup.leaseId,
        releaseFence: "premature-fence",
      }),
    (error) => assertCode(error, "cleanup_settlement_not_verified"),
  );
  assert.equal(runtime.snapshot().settlements[0]?.state, "planned");
});

test("e03.cleanup rejects incomplete observation set", () => {
  const { runtime, cleanup, planned } = cleanupRuntime(
    "observation-incomplete",
  );
  const collecting = runtime.begin({
    settlementId: planned.settlementId,
    expectedRevision: planned.revision,
    cleanupReceiptDigest: cleanup.digest,
  });
  const claim = runtime.snapshot().claims[0]!;
  runtime.observe({
    settlementId: collecting.settlementId,
    claimId: claim.claimId,
    expectedSettlementRevision: collecting.revision,
    expectedClaimRevision: claim.revision,
    observerId: "partial-observer",
    observedPresent: false,
    observationDigest: digest("partial-observation"),
  });
  assert.throws(
    () =>
      runtime.reconcile({
        settlementId: collecting.settlementId,
        expectedRevision: collecting.revision,
      }),
    (error) => assertCode(error, "cleanup_settlement_observation_incomplete"),
  );
  assert.equal(runtime.snapshot().settlements[0]?.state, "collecting");
});

test("e03.cleanup reopens blocking claim", () => {
  const { runtime, cleanup, planned } = cleanupRuntime("reopen");
  let settlement = runtime.begin({
    settlementId: planned.settlementId,
    expectedRevision: planned.revision,
    cleanupReceiptDigest: cleanup.digest,
  });
  const claims = runtime.snapshot().claims;
  for (const [index, claim] of claims.entries())
    runtime.observe({
      settlementId: settlement.settlementId,
      claimId: claim.claimId,
      expectedSettlementRevision: settlement.revision,
      expectedClaimRevision: claim.revision,
      observerId: "reopen-observer",
      observedPresent: index === 0,
      observationDigest: digest({ claim: claim.claimId, index }),
    });
  settlement = runtime.reconcile({
    settlementId: settlement.settlementId,
    expectedRevision: settlement.revision,
  });
  assert.equal(settlement.state, "incomplete");
  assert.equal(settlement.blockingClaimIds.length, 1);
  const reopened = runtime.reopen({
    settlementId: settlement.settlementId,
    expectedRevision: settlement.revision,
    resolvedClaimIds: [settlement.blockingClaimIds[0]!],
  });
  assert.equal(reopened.state, "collecting");
  assert.deepEqual(reopened.blockingClaimIds, []);
  const reset = runtime
    .snapshot()
    .claims.find((claim) => claim.claimId === settlement.blockingClaimIds[0]);
  assert.equal(reset?.observedPresent, null);
  assert.equal(reset?.observationDigest, "");
});

test("e03.cleanup rejects stale settlement revision", () => {
  const { runtime, cleanup, planned } = cleanupRuntime("stale-revision");
  const collecting = runtime.begin({
    settlementId: planned.settlementId,
    expectedRevision: planned.revision,
    cleanupReceiptDigest: cleanup.digest,
  });
  assert.equal(collecting.revision, planned.revision + 1);
  assert.throws(
    () =>
      runtime.begin({
        settlementId: planned.settlementId,
        expectedRevision: planned.revision,
        cleanupReceiptDigest: cleanup.digest,
      }),
    (error) => assertCode(error, "cleanup_settlement_stale_revision"),
  );
  assert.equal(runtime.snapshot().settlements[0]?.state, "collecting");
});

test("e03.cleanup rejects receipt digest tamper", () => {
  const { runtime, planned } = cleanupRuntime("receipt-tamper");
  assert.throws(
    () =>
      runtime.begin({
        settlementId: planned.settlementId,
        expectedRevision: planned.revision,
        cleanupReceiptDigest: digest("tampered-cleanup-receipt"),
      }),
    (error) => assertCode(error, "cleanup_settlement_receipt_digest_mismatch"),
  );
  assert.equal(runtime.snapshot().settlements[0]?.state, "planned");
});

test("e03.cleanup restores evidence custody", () => {
  const { runtime, cleanup, planned } = cleanupRuntime("restore");
  const collecting = runtime.begin({
    settlementId: planned.settlementId,
    expectedRevision: planned.revision,
    cleanupReceiptDigest: cleanup.digest,
  });
  const claim = runtime.snapshot().claims[0]!;
  runtime.observe({
    settlementId: collecting.settlementId,
    claimId: claim.claimId,
    expectedSettlementRevision: collecting.revision,
    expectedClaimRevision: claim.revision,
    observerId: "restore-observer",
    observedPresent: false,
    observationDigest: digest("restore-observation"),
  });
  const snapshot = runtime.snapshot();
  const restored = new WorktreeCleanupSettlementRuntime();
  restored.restore(snapshot);
  assert.deepEqual(restored.snapshot(), snapshot);
  assert.equal(restored.snapshot().settlements.length, 1);
  assert.equal(restored.snapshot().claims.length, 2);
  assert.equal(restored.snapshot().evidence.length, 1);
  assert.equal(restored.snapshot().evidence[0]?.observerId, "restore-observer");
});

test("e03.cleanup rejects corrupt snapshot", () => {
  const { runtime } = cleanupRuntime("snapshot-corrupt");
  const snapshot = runtime.snapshot();
  const tampered = {
    ...snapshot,
    claims: snapshot.claims.slice(0, 1),
  };
  const restored = new WorktreeCleanupSettlementRuntime();
  assert.throws(
    () => restored.restore(tampered),
    (error) => assertCode(error, "cleanup_settlement_snapshot_corrupt"),
  );
  assert.equal(restored.snapshot().settlements.length, 0);
  assert.equal(restored.snapshot().claims.length, 0);
});

function worktreeCustody(label: string) {
  const clock = new TestClock("2026-07-18T15:00:00.000Z");
  const workspaceRoot = resolve(process.cwd());
  const state = task(`worktree-${label}`, {
    scope: scope({ workspaceRoots: [workspaceRoot], isolationModes: ["worktree"] }),
  });
  const requests = new IsolationRequestRuntime(clock);
  const request = requests.prepare(state, {
    mode: "worktree",
    workspaceRoot,
    baseRevision: `base-${label}`,
    branchName: `zyra/review/${label}`,
    expectedArtifacts: [`artifacts/${label}.json`],
    idempotencyKey: `isolation-${label}`,
  });
  const receiptPayload = {
    receiptId: `isolation-receipt-${label}`,
    requestId: request.requestId,
    taskId: state.identity.taskId,
    leaseId: state.identity.leaseId,
    accepted: true,
    workspacePath: resolve(workspaceRoot, ".tmp", `worktree-${label}`),
    observedBaseRevision: request.baseRevision,
    resultingRevision: request.baseRevision,
    dirtyBaseline: false,
    nestedRepository: false,
    mergeConflict: false,
    cleanupFailed: false,
    workspaceDisposition: "created" as const,
    physicalBackend: "git-worktree",
    worktreeHead: request.baseRevision,
    worktreeBranch: request.branchName,
    artifacts: [],
    error: "",
    completedAt: clock.now(),
  };
  const receipt = { ...receiptPayload, digest: digest(receiptPayload) };
  return { clock, state, requests, request, receipt, workspaceRoot };
}

test("e03 worktree custody traverses prepare receipt commit merge conflict and cleanup", () => {
  const harness = worktreeCustody("success");
  const recorded = harness.requests.recordReceipt(harness.request, harness.receipt);
  const committed = harness.requests.commit(harness.state, harness.request, recorded);
  assert.equal(committed.isolation?.requestId, harness.request.requestId);
  assert.equal(committed.isolationReceipt?.receiptId, harness.receipt.receiptId);

  const merges = new IsolationMergeRuntime(harness.clock);
  const mergePayload = {
    mergeId: "merge-success",
    taskId: committed.identity.taskId,
    requestId: harness.request.requestId,
    leaseId: committed.identity.leaseId,
    expectedBaseRevision: harness.request.baseRevision,
    sourceRevision: harness.receipt.resultingRevision,
    targetRevision: "target-before",
    accepted: true,
    conflictedPaths: [],
    resultingRevision: "target-after",
    error: "",
    completedAt: harness.clock.now(),
  };
  const merged = merges.merge(
    committed,
    harness.request,
    harness.receipt,
    { ...mergePayload, digest: digest(mergePayload) },
  );
  assert.equal(merged.deliveries.at(-1)?.summary, "worktree merged");

  const conflictPayload = {
    ...mergePayload,
    mergeId: "merge-conflict",
    accepted: false,
    conflictedPaths: ["src/conflict.ts"],
    resultingRevision: "",
    error: "merge conflict",
  };
  const conflicted = merges.merge(
    committed,
    harness.request,
    harness.receipt,
    { ...conflictPayload, digest: digest(conflictPayload) },
  );
  assert.equal(conflicted.deliveries.at(-1)?.summary, "worktree merge conflict");

  const cleanupPayload = {
    cleanupId: "cleanup-success",
    taskId: committed.identity.taskId,
    requestId: harness.request.requestId,
    leaseId: committed.identity.leaseId,
    removed: true,
    retainedArtifacts: ["artifacts/success.json"],
    error: "",
    completedAt: harness.clock.now(),
  };
  const cleaned = merges.cleanup(
    committed,
    harness.request,
    { ...cleanupPayload, digest: digest(cleanupPayload) },
  );
  assert.equal(cleaned.deliveries.at(-1)?.summary, "worktree cleaned");
});

test("e03 worktree custody rejects escape receipt identity rejection revision conflict and cleanup tamper", () => {
  const harness = worktreeCustody("failure");
  assert.throws(
    () => harness.requests.validateWorkspace(resolve(harness.workspaceRoot, ".."), [harness.workspaceRoot]),
    (error) => assertCode(error, "workspace_escape"),
  );
  assert.throws(
    () => harness.requests.prepare(harness.state, {
      mode: "worktree",
      workspaceRoot: harness.workspaceRoot,
      baseRevision: "",
      idempotencyKey: "missing-base",
    }),
    (error) => assertCode(error, "missing_base_revision"),
  );

  const { digest: _receiptDigest, ...receiptPayload } = harness.receipt;
  const foreignPayload = { ...receiptPayload, taskId: "foreign-task" };
  assert.throws(
    () => harness.requests.recordReceipt(harness.request, { ...foreignPayload, digest: digest(foreignPayload) }),
    (error) => assertCode(error, "isolation_receipt_identity"),
  );
  const rejectedPayload = { ...receiptPayload, accepted: false, workspacePath: "", error: "physical deny" };
  assert.throws(
    () => harness.requests.commit(harness.state, harness.request, { ...rejectedPayload, digest: digest(rejectedPayload) }),
    (error) => assertCode(error, "isolation_rejected"),
  );

  const merges = new IsolationMergeRuntime(harness.clock);
  const mergePayload = {
    mergeId: "merge-failure",
    taskId: harness.state.identity.taskId,
    requestId: harness.request.requestId,
    leaseId: harness.state.identity.leaseId,
    expectedBaseRevision: "wrong-base",
    sourceRevision: harness.receipt.resultingRevision,
    targetRevision: "target-before",
    accepted: true,
    conflictedPaths: [],
    resultingRevision: "target-after",
    error: "",
    completedAt: harness.clock.now(),
  };
  assert.throws(
    () => merges.merge(harness.state, harness.request, harness.receipt, { ...mergePayload, digest: digest(mergePayload) }),
    (error) => assertCode(error, "merge_revision_mismatch"),
  );
  assert.throws(
    () => merges.recordConflict(harness.state, { ...mergePayload, conflictedPaths: [], digest: digest(mergePayload) }),
    (error) => assertCode(error, "empty_merge_conflict"),
  );
  const cleanupPayload = {
    cleanupId: "cleanup-failure",
    taskId: harness.state.identity.taskId,
    requestId: harness.request.requestId,
    leaseId: harness.state.identity.leaseId,
    removed: true,
    retainedArtifacts: [],
    error: "",
    completedAt: harness.clock.now(),
  };
  assert.throws(
    () => merges.cleanup(harness.state, harness.request, { ...cleanupPayload, digest: digest("tampered") }),
    (error) => assertCode(error, "cleanup_receipt_checksum"),
  );
});
