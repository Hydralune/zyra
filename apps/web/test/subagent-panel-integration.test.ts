import { describe, expect, test } from "bun:test"
import type { CommandReceipt } from "../../../packages/commands/src/contracts.ts"
import type {
  CanonicalProjectionState,
  CausalEventProjection,
  TaskProjection,
  WorkerProjection,
} from "../src/state/contracts.ts"
import { createEmptyProjectionState } from "../src/state/state.ts"
import {
  buildSubagentProjection,
  LateResultQuarantine,
  SubagentControlRuntime,
  SubagentLifecycleMonitor,
  SubagentPanelController,
} from "../src/features/subagents/index.ts"

const NOW = "2026-07-25T08:00:00.000Z"

function entity(overrides: Record<string, unknown>) {
  return {
    id: "entity",
    domain: "worker",
    taskId: "task-1",
    runId: "run-1",
    sessionId: "session-1",
    workerId: "worker-child-1",
    lifecycle: "running",
    status: "authoritative",
    revision: 1,
    sequence: 1,
    aggregateSequence: 1,
    createdAt: "2026-07-25T07:55:00.000Z",
    updatedAt: NOW,
    firstEventId: "event-created",
    lastEventId: "event-heartbeat",
    correlationId: "correlation-child-1",
    artifactIds: [],
    title: "child reviewer",
    summary: "running",
    terminal: false,
    effective: true,
    attributes: {},
    metadata: {},
    ...overrides,
  }
}

function event(
  eventId: string,
  sequence: number,
  eventType: string,
  overrides: Partial<CausalEventProjection> = {},
): CausalEventProjection {
  return {
    eventId,
    eventType,
    taskId: "task-1",
    runId: "run-1",
    sessionId: "session-1",
    workerId: "worker-child-1",
    artifactIds: [],
    correlationId: "correlation-child-1",
    mutationId: `mutation-${eventId}`,
    sequence,
    aggregateSequence: sequence,
    createdAt: NOW,
    committedAt: NOW,
    summary: eventType,
    terminal: false,
    effective: true,
    entityRefs: ["worker:worker-child-1", "subagent:child-1"],
    ...overrides,
  }
}

function worker(overrides: Record<string, unknown> = {}): WorkerProjection {
  const overrideAttributes =
    (overrides.attributes as Record<string, unknown> | undefined) ?? {}
  const { attributes: _attributes, ...workerOverrides } = overrides
  return entity({
    id: "worker-child-1",
    role: "subagent:reviewer",
    capabilityRefs: ["review"],
    leaseId: "lease-1",
    routeId: "route-edge",
    placementId: "placement-edge",
    health: "healthy",
    checkpointId: "checkpoint-child-1",
    attributes: {
      subagent_id: "child-1",
      parent_task_id: "task-1",
      parent_run_id: "run-1",
      parent_session_id: "session-1",
      child_session_id: "session-child-1",
      owner_id: "agent-owner",
      definition_name: "reviewer",
      definition_digest: "sha256:definition",
      context_digest: "sha256:context",
      prompt_digest: "sha256:prompt",
      idempotency_key: "agent-create-idempotency",
      execution_mode: "background",
      status: "running",
      depth: 1,
      attempt: 0,
      last_heartbeat_at: "2026-07-25T07:59:55.000Z",
      heartbeat_interval_ms: 10_000,
      heartbeat_timeout_ms: 60_000,
      scope: {
        scope_digest: "sha256:scope",
        permission_mode: "default",
        permission_ceiling_digest: "sha256:ceiling",
        tool_catalog_digest: "sha256:catalog",
        parent_tools: ["Read", "Write", "Bash"],
        child_tools: ["Read"],
        denied_tools: ["Bash"],
        skills: ["review"],
        mcp_servers: ["docs"],
        memory_scope: "child",
        isolation: "worktree",
        lineage: ["task-1", "child-1"],
        max_depth: 3,
        allow_kill: true,
        allow_steer: true,
      },
      budget: {
        max_turns: 12,
        turns_used: 4,
        max_tool_calls: 48,
        tool_calls_used: 8,
        max_input_tokens: 64_000,
        input_tokens_used: 12_000,
        max_output_tokens: 16_000,
        output_tokens_used: 2_000,
        max_result_chars: 120_000,
        result_chars_used: 2_400,
        max_wall_time_ms: 900_000,
        wall_time_ms: 300_000,
        max_children: 2,
        child_count: 0,
        max_depth: 3,
      },
      ...overrideAttributes,
    },
    ...workerOverrides,
  }) as unknown as WorkerProjection
}

function fixture(options: {
  childWorker?: WorkerProjection
  events?: readonly CausalEventProjection[]
  revision?: number
  connected?: boolean
} = {}): CanonicalProjectionState {
  const state = createEmptyProjectionState(Date.parse(NOW)) as unknown as {
    -readonly [K in keyof CanonicalProjectionState]: CanonicalProjectionState[K]
  }
  const task = entity({
    id: "task-1",
    domain: "task",
    workerId: undefined,
    activeNodeIds: [],
    workerIds: ["worker-child-1"],
    sessionIds: ["session-1"],
    artifactIds: [],
    pendingPermissionIds: [],
    commandIds: [],
    recoveryIds: [],
  }) as unknown as TaskProjection
  const childWorker = options.childWorker ?? worker()
  const events = options.events ?? [
    event("event-created", 1, "runtime.subagent.created"),
    event("event-heartbeat", 2, "runtime.subagent.progress"),
  ]
  const byEvent = Object.fromEntries(events.map((entry) => [entry.eventId, entry]))
  return {
    ...state,
    revision: options.revision ?? 2,
    tasks: { "task-1": task },
    workers: { [childWorker.id]: childWorker },
    causality: {
      ...state.causality,
      byEvent,
      byTask: { "task-1": events.map((entry) => entry.eventId) },
      byWorker: { "worker-child-1": events.map((entry) => entry.eventId) },
      eventOrder: events.map((entry) => entry.eventId),
    },
    runtimes: {
      "task-1": {
        taskId: "task-1",
        generation: 1,
        firstSequence: 1,
        lastSequence: events.at(-1)?.sequence ?? 0,
        eventCount: events.length,
        duplicateCount: 0,
        staleCount: 0,
        orphanCount: 0,
        tombstoneCount: 0,
        lastEventAtMs: Date.parse(NOW),
        pinned: true,
        connected: options.connected !== false,
      },
    },
  } as CanonicalProjectionState
}

function receipt(overrides: Partial<CommandReceipt> = {}): CommandReceipt {
  return {
    schema: "zyra.command-receipt/v1",
    requestId: "request-control-1",
    commandId: "command-control-1",
    taskId: "task-1",
    runId: "run-1",
    sessionId: "session-child-1",
    name: "/agents",
    scope: "task",
    mode: "enqueue",
    priority: "next",
    idempotencyKey: "command-owner-idempotency-1",
    phase: "applied",
    summary: "owner accepted control",
    displayText: "owner accepted control",
    data: {},
    eventIds: [],
    replayed: false,
    durable: true,
    executed: true,
    interventionCounted: false,
    humanInterventionCount: 0,
    operatorInterventionAttemptCount: 0,
    createdAt: NOW,
    finishedAt: NOW,
    raw: {},
    ...overrides,
  }
}

describe("subagent canonical panel", () => {
  test("projects exact hierarchy, scope, heartbeat, budget, result and checkpoint facts", () => {
    const child2 = worker({
      id: "worker-child-2",
      workerId: "worker-child-2",
      sequence: 3,
      attributes: {
        subagent_id: "child-2",
        parent_task_id: "child-1",
        parent_run_id: "run-1",
        child_session_id: "session-child-2",
        owner_id: "agent-owner",
        depth: 2,
        attempt: 0,
        status: "completed",
        result_digest: "sha256:result",
        result_summary: "Reviewed the implementation.",
        artifact_ids: ["artifact-review"],
        last_heartbeat_at: "2026-07-25T07:59:59.000Z",
        scope: {
          scope_digest: "sha256:scope-2",
          permission_mode: "default",
          child_tools: ["Read"],
          denied_tools: ["Write", "Bash"],
          lineage: ["child-1", "child-2"],
          max_depth: 3,
        },
        budget: {
          max_turns: 4,
          turns_used: 4,
          max_tool_calls: 10,
          tool_calls_used: 5,
          max_depth: 3,
        },
      },
      lifecycle: "completed",
      terminal: true,
    })
    const state = fixture()
    const withChild = {
      ...state,
      workers: {
        ...state.workers,
        [child2.id]: child2,
      },
    } as CanonicalProjectionState
    const projection = buildSubagentProjection(withChild, "task-1", {
      runId: "run-1",
      nowMs: Date.parse(NOW),
      includeRejected: true,
    })
    expect(projection.rows.map((row) => row.id)).toContain("child-1")
    expect(projection.rows.map((row) => row.id)).toContain("child-2")
    expect(projection.rowById["child-1"]?.scope.childTools).toEqual(["Read"])
    expect(projection.rowById["child-1"]?.scope.deniedTools).toEqual(["Bash"])
    expect(projection.rowById["child-1"]?.heartbeat.phase).toBe("healthy")
    expect(projection.rowById["child-1"]?.budget.turnsUsed).toBe(4)
    expect(projection.rowById["child-1"]?.checkpointId).toBe("checkpoint-child-1")
    expect(projection.hierarchy.nodes["child-2"]?.parentId).toBe("child-1")
    expect(projection.hierarchy.nodes["child-2"]?.depth).toBe(1)
    expect(projection.rowById["child-2"]?.result?.digest).toBe("sha256:result")
  })

  test("detects crash, reconnect and permanently quarantines results after a kill fence", () => {
    const killed = event("event-killed", 3, "runtime.subagent.cancelled", {
      terminal: true,
      summary: "killed by canonical owner",
    })
    const late = event("event-late-result", 4, "runtime.subagent.completed", {
      artifactIds: ["artifact-late"],
      summary: "late output",
    })
    const state = fixture({
      childWorker: worker({
        lifecycle: "cancelled",
        terminal: true,
        revision: 3,
        sequence: 4,
        attributes: {
          status: "cancelled",
          attempt: 1,
          error_code: "heartbeat_timeout",
          error_message: "worker heartbeat timeout",
        },
      }),
      events: [
        event("event-created", 1, "runtime.subagent.created"),
        event("event-failed", 2, "runtime.subagent.failed", {
          failureId: "failure-child-1",
          summary: "worker crashed",
        }),
        killed,
        late,
      ],
      revision: 5,
    })
    const projection = buildSubagentProjection(state, "task-1", {
      nowMs: Date.parse(NOW),
      includeRejected: true,
    })
    const row = projection.rowById["child-1"]!
    expect(row.attempt).toBe(1)
    expect(row.reconnected).toBe(true)
    expect(row.lateResults).toHaveLength(1)
    expect(row.quarantined).toBe(true)
    expect(row.lateResults[0]?.artifactIds).toEqual(["artifact-late"])
    const quarantine = new LateResultQuarantine()
    quarantine.replace(projection)
    expect(() => quarantine.release()).toThrow("cannot be released")
    const monitor = new SubagentLifecycleMonitor({
      now: () => new Date(NOW),
    })
    const incidents = monitor.observe(projection)
    expect(incidents.some((incident) => incident.kind === "late_result")).toBe(true)
  })

  test("rejects cross-run identities and secret-bearing canonical attributes", () => {
    const state = fixture({
      childWorker: worker({
        runId: "run-other",
        attributes: {
          api_key: "sk-secret-material-1234567890",
        },
      }),
    })
    const projection = buildSubagentProjection(state, "task-1", {
      runId: "run-1",
      nowMs: Date.parse(NOW),
      includeRejected: true,
    })
    expect(projection.rowById["child-1"]?.admitted).toBe(false)
    expect(projection.rowById["child-1"]?.controlEligible).toBe(false)
    expect(
      projection.rowById["child-1"]?.issues.some((issue) => issue.severity === "error"),
    ).toBe(true)
  })
})

describe("subagent permission-bound controls", () => {
  test("deduplicates nonce/idempotency, waits for canonical kill effect, and rejects owner drift", async () => {
    let state = fixture()
    let submits = 0
    const listeners = new Set<() => void>()
    const commands = {
      async submit() {
        submits += 1
        return receipt()
      },
    }
    const projection = () => buildSubagentProjection(state, "task-1", {
      nowMs: Date.parse(NOW),
      includeRejected: true,
    })
    const runtime = new SubagentControlRuntime({
      commands,
      state: () => state,
      projection,
      now: () => new Date(NOW),
      nonce: () => "nonce-control-1",
      online: () => true,
    })
    const first = runtime.kill({
      childId: "child-1",
      reason: "stop compromised child",
      nonce: "nonce-control-1",
      idempotencyKey: "subagent-kill-idempotency-1",
    })
    const replay = runtime.kill({
      childId: "child-1",
      reason: "stop compromised child",
      nonce: "nonce-control-1",
      idempotencyKey: "subagent-kill-idempotency-1",
    })
    expect((await first).phase).toBe("reconciling")
    expect((await replay).replayCount).toBeGreaterThanOrEqual(1)
    expect(submits).toBe(1)
    const killEvent = event("event-control-killed", 3, "runtime.subagent.cancelled", {
      terminal: true,
      controlCommandId: "command-control-1",
      summary: "canonical owner killed child",
    })
    state = {
      ...state,
      revision: state.revision + 1,
      workers: {
        "worker-child-1": worker({
          lifecycle: "cancelled",
          terminal: true,
          revision: 2,
          sequence: 3,
          controlCommandId: "command-control-1",
          attributes: {
            status: "cancelled",
          },
        }),
      },
      causality: {
        ...state.causality,
        byEvent: {
          ...state.causality.byEvent,
          [killEvent.eventId]: killEvent,
        },
        byTask: {
          "task-1": [...(state.causality.byTask["task-1"] ?? []), killEvent.eventId],
        },
        byWorker: {
          "worker-child-1": [
            ...(state.causality.byWorker["worker-child-1"] ?? []),
            killEvent.eventId,
          ],
        },
        byControlCommand: {
          "command-control-1": [killEvent.eventId],
        },
        eventOrder: [...state.causality.eventOrder, killEvent.eventId],
      },
    } as CanonicalProjectionState
    runtime.reconcile()
    expect(runtime.getSnapshot().active?.phase).toBe("committed")
    expect(runtime.getSnapshot().active?.effect?.semanticEffect).toBe("terminal_cancel")
    runtime.close()
    expect(listeners.size).toBe(0)
  })

  test("sealed kill records a denial intervention, never submits, and keeps human count zero", async () => {
    const state = fixture()
    let submits = 0
    let interventions = 0
    const runtime = new SubagentControlRuntime({
      commands: {
        async submit() {
          submits += 1
          return receipt()
        },
      },
      state: () => state,
      projection: () => buildSubagentProjection(state, "task-1", {
        nowMs: Date.parse(NOW),
        includeRejected: true,
      }),
      interventions: {
        async record(input) {
          interventions += 1
          expect(input.binding.childId).toBe("child-1")
          return {
            id: "intervention-1",
            interventionCounted: true,
            humanInterventionCount: 0,
            operatorInterventionAttemptCount: 1,
            automaticRecoveryAction: "fail_closed",
            eventIds: ["event-sealed-denial"],
          }
        },
      },
      now: () => new Date(NOW),
      online: () => true,
    })
    runtime.setSealed(true)
    const operation = await runtime.kill({
      childId: "child-1",
      reason: "sealed attempt",
    })
    expect(operation.phase).toBe("denied")
    expect(submits).toBe(0)
    expect(interventions).toBe(1)
    expect(runtime.getSnapshot().interventions[0]?.humanInterventionCount).toBe(0)
    expect(runtime.getSnapshot().interventions[0]?.noHumanWait).toBe(true)
  })
})

describe("subagent controller close, restore, disconnect and disable", () => {
  test("viewer close only detaches, reopen restores canonical state, and disable fails closed", async () => {
    let state = fixture()
    const listeners = new Set<() => void>()
    const projections = {
      get state() {
        return state
      },
      subscribe(listener: () => void) {
        listeners.add(listener)
        return () => listeners.delete(listener)
      },
    }
    let submits = 0
    const controller = new SubagentPanelController({
      projections: projections as never,
      commands: {
        async submit() {
          submits += 1
          return receipt()
        },
      },
      now: () => new Date(NOW),
      online: () => true,
    })
    controller.bind("task-1", "run-1")
    controller.select("child-1")
    expect(controller.getSnapshot().selectedId).toBe("child-1")
    expect(listeners.size).toBe(1)
    controller.closeViewer()
    expect(listeners.size).toBe(0)
    expect(controller.getSnapshot().viewerOpen).toBe(false)
    expect(submits).toBe(0)
    state = fixture({
      childWorker: worker({
        revision: 2,
        sequence: 3,
        attributes: {
          status: "running",
          turns_used: 5,
        },
      }),
      revision: 3,
    })
    controller.openViewer()
    expect(listeners.size).toBe(1)
    expect(controller.getSnapshot().projection?.canonicalRevision).toBe(3)
    expect(controller.getSnapshot().restoreGeneration).toBe(1)
    controller.disconnected("test disconnect")
    await expect(controller.kill({
      childId: "child-1",
      reason: "must not reach owner",
    })).rejects.toThrow("disconnected")
    controller.reconnected()
    controller.disable("feature binding disabled")
    expect(listeners.size).toBe(0)
    await expect(controller.steer({
      childId: "child-1",
      instruction: "continue",
    })).rejects.toThrow("feature binding disabled")
    expect(submits).toBe(0)
    controller.close()
  })
})
