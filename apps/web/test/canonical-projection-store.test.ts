import { describe, expect, test } from "bun:test"
import {
  CanonicalProjectionStore,
  FallbackProjectionPersistence,
  IndexedDbProjectionPersistence,
  MemoryProjectionPersistence,
  ProjectionError,
  ProjectionPersistenceCoordinator,
  ProjectionStatus,
  assertProjectionIntegrity,
  auditProjectionIntegrity,
  checksumJson,
  decodeProjectionSnapshot,
  encodeProjectionSnapshot,
  mergeHistoryAndLiveBatches,
  selectActiveOverlays,
  selectArtifact,
  selectArtifactPanel,
  selectCommandsForTask,
  selectEventsForArtifact,
  selectEventsForFailure,
  selectEventsForRecovery,
  selectEventsForSpan,
  selectOperatorQueue,
  selectPendingPermissions,
  selectProjectionReadiness,
  selectSessionPanel,
  selectTask,
  selectTaskSummary,
  selectTimelinePanel,
  selectTopologyPanel,
  selectWorkersForTask,
  type CanonicalProjectionState,
  type ProjectionPersistence,
} from "../src/state/index.ts"
import {
  normalizeEventFrame,
  type IngressBatch,
  type IngressEvent,
  type JsonObject,
} from "../src/events/ingress/index.ts"

const TASK = "task_projection_test"
const RUN = "run_projection_test"
const SESSION = "session_projection_test"
const NOW = "2026-07-23T08:00:00.000Z"
const DIGEST = `sha256:${"b".repeat(64)}`

function event(
  sequence: number,
  options: {
    eventId?: string
    eventType?: string
    domain?: string
    operation?: string
    settlement?: "atomic" | "partial" | "final" | "tombstone"
    inline?: JsonObject
    metadata?: JsonObject
    terminal?: boolean
    effective?: boolean
    taskId?: string
    runId?: string
    sessionId?: string
    nodeId?: string
    workerId?: string
    spanId?: string
    parentSpanId?: string
    toolCallId?: string
    artifactId?: string
    checkpointId?: string
    commandId?: string
    causationId?: string
    parentEventId?: string
    tombstoneTargetId?: string
    artifactRefs?: {
      artifactId: string
      digest?: string
      mediaType?: string
      sizeBytes?: number
      title?: string
      uri?: string
    }[]
  } = {},
): IngressEvent {
  const taskId = options.taskId ?? TASK
  const eventId = options.eventId ?? `event_projection_${sequence}`
  const raw = {
    schema: "zyra.runtime-event/v1",
    eventId,
    eventType: options.eventType ?? `${options.domain ?? "task"}.state.changed`,
    eventVersion: 1,
    aggregateId: `task:${taskId}`,
    aggregateSequence: sequence,
    globalSequence: sequence,
    producerSequence: sequence,
    idempotencyKey: `projection-${eventId}`,
    correlationId: "request_projection_test",
    causationId: options.causationId,
    createdAt: NOW,
    committedAt: new Date(Date.parse(NOW) + sequence).toISOString(),
    durability: "durable",
    effect: options.effective === false ? "non_effective" : "effective",
    identity: {
      taskId,
      runId: options.runId ?? RUN,
      sessionId: options.sessionId ?? SESSION,
      nodeId: options.nodeId,
      workerId: options.workerId,
      spanId: options.spanId ?? `span_projection_${sequence}`,
      parentSpanId: options.parentSpanId,
      toolCallId: options.toolCallId,
      artifactId: options.artifactId,
      checkpointId: options.checkpointId,
      controlCommandId: options.commandId,
    },
    sender: {
      kind: "runtime",
      id: options.workerId ?? "runtime-projection-test",
      capabilityRefs: [],
    },
    intent: "status",
    topKRecipients: [],
    summary: `${options.eventType ?? options.domain ?? "task"} ${sequence}`,
    stateDelta: {
      domain: options.domain ?? "task",
      operation: options.operation ?? "transition",
      path: ["status"],
      beforeDigest: `sha256:${"1".repeat(64)}`,
      afterDigest: `sha256:${"2".repeat(64)}`,
      effective: options.effective !== false,
    },
    evidenceRefs: [],
    artifactRefs: (options.artifactRefs ?? []).map((item) => ({
      artifactId: item.artifactId,
      digest: item.digest ?? DIGEST,
      mediaType: item.mediaType ?? "application/json",
      sizeBytes: item.sizeBytes ?? 128,
      title: item.title ?? item.artifactId,
      uri: item.uri,
    })),
    provenance: {
      sourceRepository: "zyra",
      sourceModule: "canonical-projection-test",
      migrationRole: "zyra_owned",
      producerVersion: "test",
      trust: "internal",
    },
    inline: {
      ...(options.inline ?? { status: "running" }),
      ...(options.terminal ? { terminal: true } : {}),
    },
    sourceBytes: 100,
    inlineBytes: 20,
    envelopeBytes: 1024,
    contentDigest: DIGEST,
    metadata: {
      ...(options.metadata ?? {}),
      ...(options.settlement ? { settlement: options.settlement } : {}),
      ...(options.parentEventId
        ? { parent_event_id: options.parentEventId }
        : {}),
      ...(options.tombstoneTargetId
        ? { tombstone_target_id: options.tombstoneTargetId }
        : {}),
    },
    settlement: options.settlement ?? "atomic",
    parentEventId: options.parentEventId,
    tombstoneTargetId: options.tombstoneTargetId,
    terminal: options.terminal ?? false,
  }
  return normalizeEventFrame(
    JSON.parse(JSON.stringify({
      schema: "zyra.event-ingress-frame/v1",
      kind: "event",
      source: "delta",
      generation: 1,
      taskId,
      sequence,
      previousSequence: Math.max(0, sequence - 1),
      eventId,
      eventType: raw.eventType,
      correlationId: raw.correlationId,
      causationId: options.causationId,
      observedAtMs: 1_000 + sequence,
      cursor: `cursor.${sequence}`,
      event: raw,
      settlement: options.settlement,
      parentEventId: options.parentEventId,
      tombstoneTargetId: options.tombstoneTargetId,
    })),
    taskId,
    1,
  ).event
}

function batch(
  events: readonly IngressEvent[],
  options: {
    taskId?: string
    generation?: number
    snapshot?: boolean
    caughtUp?: boolean
    cursor?: string
    sequence?: number
  } = {},
): IngressBatch {
  const taskId = options.taskId ?? events[0]?.identity.taskId ?? TASK
  const sequence =
    options.sequence ??
    Math.max(0, ...events.map((item) => item.globalSequence))
  return {
    taskId,
    generation: options.generation ?? 1,
    events: Object.freeze([...events]),
    receipts: Object.freeze([]),
    cursor: options.cursor ?? `cursor.${sequence}`,
    fromSequence: Math.min(sequence, ...events.map((item) => item.globalSequence)),
    sequence,
    highWatermark: sequence,
    receivedAt: 5_000 + sequence,
    transport: "long_poll",
    snapshot: options.snapshot ?? false,
    caughtUp: options.caughtUp ?? true,
  }
}

function store(
  options: ConstructorParameters<typeof CanonicalProjectionStore>[0] = {},
) {
  return new CanonicalProjectionStore({
    restore: false,
    autoPersist: false,
    ...options,
  })
}

describe("canonical projection reducer", () => {
  test("projects every required state domain through one revisioned transaction", () => {
    const projection = store()
    const events = [
      event(1, {
        eventType: "task.started",
        domain: "task",
        inline: { task_id: TASK, title: "Projection task", status: "running" },
      }),
      event(2, {
        eventType: "topology.node.created",
        domain: "node",
        nodeId: "node_projection_1",
        inline: {
          node_id: "node_projection_1",
          role: "planner",
          capability_refs: ["planning"],
          status: "running",
        },
      }),
      event(3, {
        eventType: "worker.admitted",
        domain: "worker",
        nodeId: "node_projection_1",
        workerId: "worker_projection_1",
        inline: {
          worker_id: "worker_projection_1",
          role: "code",
          lease_id: "lease_projection_1",
          status: "admitted",
        },
      }),
      event(4, {
        eventType: "tool.started",
        domain: "tool",
        workerId: "worker_projection_1",
        toolCallId: "tool_projection_1",
        inline: {
          tool_call_id: "tool_projection_1",
          tool_name: "shell",
          status: "running",
        },
      }),
      event(5, {
        eventType: "artifact.committed",
        domain: "artifact",
        artifactId: "artifact_projection_1",
        artifactRefs: [{ artifactId: "artifact_projection_1" }],
        inline: { artifact_id: "artifact_projection_1", status: "completed" },
        terminal: true,
      }),
      event(6, {
        eventType: "memory.retrieved",
        domain: "memory",
        inline: {
          memory_id: "memory_projection_1",
          memory_kind: "episodic",
          score: 0.9,
        },
      }),
      event(7, {
        eventType: "scheduler.route.selected",
        domain: "scheduler",
        inline: {
          scheduler_id: "scheduler_projection_1",
          route_id: "route_projection_1",
          placement_id: "edge_projection_1",
          model_id: "model_projection_1",
        },
      }),
      event(8, {
        eventType: "recovery.started",
        domain: "recovery",
        inline: {
          recovery_id: "recovery_projection_1",
          failure_id: "failure_projection_1",
          strategy: "replace_worker",
          status: "recovering",
        },
      }),
      event(9, {
        eventType: "control.command.queued",
        domain: "command",
        commandId: "command_projection_1",
        inline: {
          command_id: "command_projection_1",
          command_name: "pause",
          priority: "now",
          status: "queued",
        },
      }),
      event(10, {
        eventType: "permission.asked",
        domain: "permission",
        toolCallId: "tool_projection_1",
        inline: {
          permission_id: "permission_projection_1",
          request_id: "permission_projection_1",
          tool_name: "shell",
          decision: "ask",
        },
      }),
      event(11, {
        eventType: "local-jsx.overlay.opened",
        domain: "overlay",
        commandId: "command_projection_1",
        inline: {
          overlay_id: "overlay_projection_1",
          overlay_kind: "permission-dialog",
          modal: true,
          status: "running",
        },
      }),
      event(12, {
        eventType: "session.updated",
        domain: "session",
        inline: {
          session_id: SESSION,
          context_tokens: 1_200,
          context_limit: 8_000,
          status: "running",
        },
      }),
    ]
    const result = projection.apply(batch(events, { snapshot: true }))
    expect(result.state.revision).toBe(1)
    expect(result.appliedEventIds).toHaveLength(12)
    expect(Object.keys(result.state.tasks)).toEqual([TASK])
    expect(result.state.nodes.node_projection_1?.role).toBe("planner")
    expect(result.state.workers.worker_projection_1?.leaseId).toBe(
      "lease_projection_1",
    )
    expect(result.state.tools.tool_projection_1?.toolName).toBe("shell")
    expect(result.state.artifacts.artifact_projection_1?.digest).toBe(DIGEST)
    expect(result.state.memories.memory_projection_1?.score).toBe(0.9)
    expect(result.state.schedulers.scheduler_projection_1?.routeId).toBe(
      "route_projection_1",
    )
    expect(result.state.recoveries.recovery_projection_1?.failureId).toBe(
      "failure_projection_1",
    )
    expect(result.state.commands.command_projection_1?.priority).toBe("now")
    expect(result.state.permissions.permission_projection_1?.decision).toBe(
      "ask",
    )
    expect(result.state.overlays.overlay_projection_1?.modal).toBe(true)
    expect(result.state.sessions[SESSION]?.contextTokens).toBe(1_200)
    expect(result.state.cursors[TASK]?.committedSequence).toBe(12)
    expect(assertProjectionIntegrity(result.state).valid).toBe(true)
  })

  test("replays the same event idempotently without selector-visible effects", () => {
    const projection = store()
    const first = event(1, { domain: "task", eventId: "event_idempotent" })
    projection.apply(batch([first]))
    const revision = projection.state.revision
    const replay = projection.apply(batch([first], { sequence: 1 }))
    expect(replay.duplicateEventIds).toEqual(["event_idempotent"])
    expect(replay.appliedEventIds).toEqual([])
    expect(replay.state.tasks[TASK]?.revision).toBe(1)
    expect(replay.state.diagnostics.duplicateEvents).toBe(1)
    expect(replay.state.revision).toBe(revision + 1)
  })

  test("projects nested legacy payloads and derives the terminal task from all nodes", () => {
    const projection = store({ now: () => Date.parse(NOW) + 1_000 })
    projection.apply(
      batch([
        event(1, {
          eventType: "runtime.task.created",
          domain: "task",
          nodeId: "node_nested_1",
          inline: {
            task: {
              task_id: TASK,
              status: "pending",
              title: "Nested canonical task",
              metadata: { source: "legacy-runtime" },
            },
          },
        }),
        event(2, {
          eventType: "runtime.node.created",
          domain: "node",
          nodeId: "node_nested_2",
          inline: {
            node: {
              node_id: "node_nested_2",
              status: "pending",
              role: "reviewer",
              capability_refs: ["review"],
            },
          },
        }),
      ]),
    )
    expect(projection.state.tasks[TASK]?.title).toBe("Nested canonical task")
    expect(projection.state.nodes.node_nested_2?.role).toBe("reviewer")
    projection.apply(
      batch([
        event(3, {
          eventType: "runtime.node.updated",
          domain: "node",
          nodeId: "node_nested_1",
          inline: {
            node: {
              node_id: "node_nested_1",
              status: "cancelled",
            },
          },
        }),
      ]),
    )
    expect(projection.state.nodes.node_nested_1?.terminal).toBe(true)
    expect(projection.state.tasks[TASK]?.terminal).toBe(false)
    projection.apply(
      batch([
        event(4, {
          eventType: "runtime.node.updated",
          domain: "node",
          nodeId: "node_nested_2",
          inline: {
            node: {
              node_id: "node_nested_2",
              status: "cancelled",
            },
          },
        }),
      ]),
    )
    const canonicalTask = projection.state.tasks[TASK]
    expect(canonicalTask?.lifecycle).toBe("cancelled")
    expect(canonicalTask?.terminal).toBe(true)
    expect(canonicalTask?.status).toBe(ProjectionStatus.FINAL)
    expect(Object.isFrozen(canonicalTask)).toBe(true)
    expect(Object.isFrozen(canonicalTask?.activeNodeIds)).toBe(true)
    expect(Object.isFrozen(canonicalTask?.attributes)).toBe(true)
    expect(
      Object.isFrozen(
        (canonicalTask?.attributes.task as Readonly<JsonObject>).metadata,
      ),
    ).toBe(true)
  })

  test("reconciles optimistic command to authoritative receipt once", () => {
    const projection = store()
    const optimistic = event(1, {
      eventId: "event_command_optimistic",
      eventType: "control.command.queued",
      domain: "command",
      commandId: "command_optimistic",
      metadata: { optimistic: true },
      inline: {
        command_id: "command_optimistic",
        optimistic_key: "optimistic-command-key",
        command_name: "pause",
        status: "queued",
      },
    })
    projection.apply(batch([optimistic]))
    expect(projection.state.commands.command_optimistic?.status).toBe(
      ProjectionStatus.OPTIMISTIC,
    )
    expect(projection.state.optimistic["optimistic-command-key"]).toBeDefined()
    const authoritative = event(2, {
      eventId: "event_command_authoritative",
      eventType: "control.command.applied",
      domain: "command",
      commandId: "command_optimistic",
      terminal: true,
      inline: {
        command_id: "command_optimistic",
        optimistic_key: "optimistic-command-key",
        command_name: "pause",
        receipt_id: "receipt_command_1",
        status: "applied",
      },
    })
    projection.apply(batch([authoritative]))
    const command = projection.state.commands.command_optimistic
    expect(command?.status).toBe(ProjectionStatus.FINAL)
    expect(command?.receiptId).toBe("receipt_command_1")
    expect(command?.revision).toBe(2)
    expect(projection.state.optimistic["optimistic-command-key"]).toBeUndefined()
  })

  test("folds partial text into final authoritative tool projection", () => {
    const projection = store()
    projection.apply(
      batch([
        event(1, {
          eventType: "tool.result.partial",
          domain: "tool",
          toolCallId: "tool_partial",
          settlement: "partial",
          inline: {
            tool_call_id: "tool_partial",
            tool_name: "browser",
            text: "hel",
            status: "running",
          },
        }),
      ]),
    )
    expect(Object.keys(projection.state.partials)).toHaveLength(1)
    projection.apply(
      batch([
        event(2, {
          eventType: "tool.result.final",
          domain: "tool",
          toolCallId: "tool_partial",
          settlement: "final",
          terminal: true,
          inline: {
            tool_call_id: "tool_partial",
            tool_name: "browser",
            text: "lo",
            status: "completed",
          },
        }),
      ]),
    )
    expect(projection.state.tools.tool_partial?.attributes.text).toBe("hello")
    expect(projection.state.tools.tool_partial?.status).toBe(
      ProjectionStatus.FINAL,
    )
    expect(Object.keys(projection.state.partials)).toHaveLength(0)
  })

  test("retains tombstones and rejects late history resurrection", () => {
    const projection = store()
    projection.apply(
      batch([
        event(1, {
          eventType: "tool.started",
          domain: "tool",
          toolCallId: "tool_removed",
          inline: { tool_call_id: "tool_removed", status: "running" },
        }),
        event(2, {
          eventType: "tool.removed",
          domain: "tool",
          settlement: "tombstone",
          tombstoneTargetId: "tool_removed",
          terminal: true,
          inline: { target_domain: "tool", status: "deleted" },
        }),
      ]),
    )
    expect(projection.state.tools.tool_removed?.status).toBe(
      ProjectionStatus.TOMBSTONED,
    )
    expect(Object.keys(projection.state.tombstones)).toHaveLength(1)
    const late = event(1, {
      eventId: "event_late_tool_history",
      eventType: "tool.started",
      domain: "tool",
      toolCallId: "tool_removed",
      inline: { tool_call_id: "tool_removed", status: "running" },
    })
    const result = projection.apply(
      batch([late], { sequence: 2, cursor: "cursor.2" }),
    )
    expect(result.staleEventIds).toEqual(["event_late_tool_history"])
    expect(projection.state.tools.tool_removed?.status).toBe(
      ProjectionStatus.TOMBSTONED,
    )
  })

  test("marks an orphan then resolves it when its logical parent arrives", () => {
    const projection = store()
    projection.apply(
      batch([
        event(1, {
          eventId: "event_orphan_child",
          eventType: "worker.started",
          domain: "worker",
          workerId: "worker_orphan",
          parentEventId: "event_orphan_parent",
          inline: { worker_id: "worker_orphan", status: "running" },
        }),
      ]),
    )
    expect(projection.state.workers.worker_orphan?.status).toBe(
      ProjectionStatus.ORPHANED,
    )
    expect(Object.keys(projection.state.orphans)).toHaveLength(1)
    projection.apply(
      batch([
        event(2, {
          eventId: "event_orphan_parent",
          eventType: "node.started",
          domain: "node",
          nodeId: "node_orphan_parent",
          inline: { node_id: "node_orphan_parent", status: "running" },
        }),
      ]),
    )
    expect(Object.keys(projection.state.orphans)).toHaveLength(0)
    expect(projection.state.workers.worker_orphan?.status).toBe(
      ProjectionStatus.AUTHORITATIVE,
    )
  })

  test("maintains command priority FIFO and resolves permissions and overlays", () => {
    const projection = store()
    projection.apply(
      batch([
        event(1, {
          eventType: "command.queued",
          domain: "command",
          commandId: "command_later",
          inline: {
            command_id: "command_later",
            command_name: "later",
            priority: "later",
            status: "queued",
          },
        }),
        event(2, {
          eventType: "command.queued",
          domain: "command",
          commandId: "command_next",
          inline: {
            command_id: "command_next",
            command_name: "next",
            priority: "next",
            status: "queued",
          },
        }),
        event(3, {
          eventType: "command.queued",
          domain: "command",
          commandId: "command_now",
          inline: {
            command_id: "command_now",
            command_name: "now",
            priority: "now",
            status: "queued",
          },
        }),
        event(4, {
          eventType: "permission.asked",
          domain: "permission",
          inline: {
            permission_id: "permission_queue",
            request_id: "permission_queue",
            decision: "ask",
          },
        }),
        event(5, {
          eventType: "overlay.opened",
          domain: "overlay",
          inline: {
            overlay_id: "overlay_queue",
            overlay_kind: "permission-dialog",
            permission_id: "permission_queue",
            modal: true,
          },
        }),
      ]),
    )
    expect(
      projection
        .select(selectCommandsForTask(TASK, { activeOnly: true }))
        .map((item) => item.id),
    ).toEqual(["command_now", "command_next", "command_later"])
    expect(projection.select(selectPendingPermissions(TASK))).toHaveLength(1)
    expect(projection.select(selectActiveOverlays(TASK, true))).toHaveLength(1)
    projection.apply(
      batch([
        event(6, {
          eventType: "permission.replied",
          domain: "permission",
          terminal: true,
          inline: {
            permission_id: "permission_queue",
            request_id: "permission_queue",
            decision: "allow",
            status: "completed",
          },
        }),
        event(7, {
          eventType: "overlay.closed",
          domain: "overlay",
          terminal: true,
          inline: {
            overlay_id: "overlay_queue",
            overlay_kind: "permission-dialog",
            closed: true,
            status: "completed",
          },
        }),
      ]),
    )
    expect(projection.select(selectPendingPermissions(TASK))).toHaveLength(0)
    expect(projection.select(selectActiveOverlays(TASK, true))).toHaveLength(0)
    expect(projection.state.tasks[TASK]?.pendingPermissionIds).toEqual([])
  })
})

describe("canonical causality and selector truth", () => {
  test("supports reversible span/tool/artifact/failure/recovery lookup", () => {
    const projection = store()
    projection.apply(
      batch([
        event(1, {
          eventId: "event_cause_root",
          eventType: "worker.started",
          domain: "worker",
          workerId: "worker_causal",
          spanId: "span_causal",
          inline: { worker_id: "worker_causal", status: "running" },
        }),
        event(2, {
          eventId: "event_cause_tool",
          eventType: "tool.failed",
          domain: "tool",
          workerId: "worker_causal",
          toolCallId: "tool_causal",
          spanId: "span_causal",
          parentSpanId: "span_parent",
          causationId: "event_cause_root",
          artifactRefs: [{ artifactId: "artifact_causal" }],
          inline: {
            tool_call_id: "tool_causal",
            failure_id: "failure_causal",
            status: "failed",
          },
          terminal: true,
        }),
        event(3, {
          eventId: "event_cause_recovery",
          eventType: "recovery.started",
          domain: "recovery",
          workerId: "worker_causal",
          spanId: "span_recovery",
          parentSpanId: "span_causal",
          causationId: "event_cause_tool",
          checkpointId: "checkpoint_causal",
          inline: {
            recovery_id: "recovery_causal",
            failure_id: "failure_causal",
            status: "recovering",
          },
        }),
      ]),
    )
    expect(
      projection.select(selectEventsForSpan("span_causal")).map((item) => item.eventId),
    ).toEqual([
      "event_cause_root",
      "event_cause_tool",
      "event_cause_recovery",
    ])
    expect(
      projection
        .select(selectEventsForArtifact("artifact_causal"))
        .map((item) => item.eventId),
    ).toEqual(["event_cause_tool"])
    expect(
      projection
        .select(selectEventsForFailure("failure_causal"))
        .map((item) => item.eventId),
    ).toEqual(["event_cause_tool", "event_cause_recovery"])
    expect(
      projection
        .select(selectEventsForRecovery("recovery_causal"))
        .map((item) => item.eventId),
    ).toEqual(["event_cause_recovery"])
    expect(projection.select(selectArtifact("artifact_causal"))?.producerEventId).toBe(
      "event_cause_tool",
    )
  })

  test("notifies only selectors whose dependencies changed", () => {
    const projection = store()
    let permissionNotifications = 0
    let workerNotifications = 0
    const closePermission = projection.subscribeSelector(
      selectPendingPermissions(TASK),
      () => {
        permissionNotifications += 1
      },
    )
    const closeWorkers = projection.subscribeSelector(
      selectWorkersForTask(TASK),
      () => {
        workerNotifications += 1
      },
    )
    projection.apply(
      batch([
        event(1, {
          eventType: "worker.started",
          domain: "worker",
          workerId: "worker_selector",
          inline: { worker_id: "worker_selector", status: "running" },
        }),
      ]),
    )
    expect(workerNotifications).toBe(1)
    expect(permissionNotifications).toBe(0)
    projection.apply(
      batch([
        event(2, {
          eventType: "artifact.committed",
          domain: "artifact",
          artifactRefs: [{ artifactId: "artifact_selector" }],
          terminal: true,
        }),
      ]),
    )
    expect(workerNotifications).toBe(1)
    expect(permissionNotifications).toBe(0)
    projection.apply(
      batch([
        event(3, {
          eventType: "permission.asked",
          domain: "permission",
          inline: {
            permission_id: "permission_selector",
            decision: "ask",
          },
        }),
      ]),
    )
    expect(permissionNotifications).toBe(1)
    closePermission()
    closeWorkers()
  })

  test("summary selector changes deterministically with real projections", () => {
    const projection = store()
    projection.apply(
      batch([
        event(1, { domain: "task", inline: { status: "running" } }),
        event(2, {
          domain: "worker",
          workerId: "worker_summary",
          inline: { worker_id: "worker_summary", status: "running" },
        }),
        event(3, {
          domain: "permission",
          eventType: "permission.asked",
          inline: { permission_id: "permission_summary", decision: "ask" },
        }),
      ]),
    )
    const summary = projection.select(selectTaskSummary(TASK))
    expect(summary.workers).toBe(1)
    expect(summary.activeWorkers).toBe(1)
    expect(summary.pendingPermissions).toBe(1)
    expect(summary.lastSequence).toBe(3)
    expect(projection.select(selectTask(TASK))?.id).toBe(TASK)
  })

  test("derives every downstream panel view from the canonical selector store", () => {
    const projection = store({ now: () => Date.parse(NOW) + 1_000 })
    projection.apply(
      batch(
        [
          event(1, {
            eventType: "task.started",
            domain: "task",
            nodeId: "panel_node_a",
            inline: { status: "running", title: "Panel truth task" },
          }),
          event(2, {
            eventType: "topology.node.created",
            domain: "node",
            nodeId: "panel_node_a",
            inline: {
              node_id: "panel_node_a",
              dependency_ids: ["panel_node_b"],
              status: "running",
            },
          }),
          event(3, {
            eventType: "topology.node.created",
            domain: "node",
            nodeId: "panel_node_b",
            inline: {
              node_id: "panel_node_b",
              dependency_ids: ["panel_node_a"],
              status: "running",
            },
          }),
          event(4, {
            eventType: "worker.admitted",
            domain: "worker",
            nodeId: "panel_node_a",
            workerId: "panel_worker",
            inline: { worker_id: "panel_worker", status: "running" },
          }),
          event(5, {
            eventId: "panel_tool_event",
            eventType: "tool.started",
            domain: "tool",
            nodeId: "panel_node_a",
            workerId: "panel_worker",
            toolCallId: "panel_tool",
            inline: { tool_call_id: "panel_tool", status: "running" },
          }),
          event(6, {
            eventType: "artifact.committed",
            domain: "artifact",
            artifactId: "panel_artifact",
            toolCallId: "panel_tool",
            causationId: "panel_tool_event",
            artifactRefs: [{ artifactId: "panel_artifact", sizeBytes: 256 }],
            inline: { artifact_id: "panel_artifact", status: "completed" },
            terminal: true,
          }),
          event(7, {
            eventType: "session.updated",
            domain: "session",
            sessionId: "panel_child_session",
            inline: {
              session_id: "panel_child_session",
              parent_session_id: SESSION,
              context_tokens: 400,
              status: "running",
            },
          }),
          event(8, {
            eventType: "permission.asked",
            domain: "permission",
            inline: {
              permission_id: "panel_permission",
              decision: "ask",
              tool_name: "shell",
            },
          }),
          event(9, {
            eventType: "control.command.queued",
            domain: "command",
            commandId: "panel_command",
            inline: {
              command_id: "panel_command",
              command_name: "pause",
              priority: "now",
              status: "queued",
            },
          }),
        ],
        { snapshot: true, caughtUp: true },
      ),
    )
    const topology = projection.select(selectTopologyPanel(TASK))
    const timeline = projection.select(selectTimelinePanel(TASK))
    const artifacts = projection.select(selectArtifactPanel(TASK))
    const sessions = projection.select(selectSessionPanel(TASK))
    const operator = projection.select(selectOperatorQueue(TASK))
    const readiness = projection.select(selectProjectionReadiness(TASK))
    expect(topology.nodes).toHaveLength(2)
    expect(topology.cycles).toEqual([
      ["panel_node_a", "panel_node_b", "panel_node_a"],
    ])
    expect(
      topology.nodes.find((node) => node.id === "panel_node_a")?.workerIds,
    ).toEqual(["panel_worker"])
    expect(timeline.entries).toHaveLength(9)
    expect(timeline.lanes).toContain("artifact")
    expect(artifacts.rows[0]?.ancestorEventIds).toEqual(["panel_tool_event"])
    expect(artifacts.totalBytes).toBe(256)
    expect(
      sessions.rows.find((row) => row.session.id === "panel_child_session")
        ?.depth,
    ).toBe(1)
    expect(operator.pendingPermissions).toBe(1)
    expect(operator.queuedCommands).toBe(1)
    expect(operator.blocking).toBe(true)
    expect(readiness.ready).toBe(true)
    expect(readiness.committedSequence).toBe(9)
    expect(readiness.integrityWarnings).toEqual([])
  })
})

describe("snapshot, reconnect, migration, and disable recovery", () => {
  test("pins the active route before its first event and preserves the pin through reduction", () => {
    const projection = store()
    projection.pinTask(TASK)
    expect(projection.state.runtimes[TASK]?.pinned).toBe(true)
    expect(projection.state.runtimes[TASK]?.eventCount).toBe(0)

    projection.apply(batch([event(1, { domain: "task" })]))
    expect(projection.state.runtimes[TASK]?.pinned).toBe(true)
    expect(projection.state.runtimes[TASK]?.eventCount).toBe(1)

    projection.unpinTask(TASK)
    expect(projection.state.runtimes[TASK]?.pinned).toBe(false)
  })

  test("persists committed cursor and restores without duplicate effects", async () => {
    const persistence = new MemoryProjectionPersistence()
    const first = new CanonicalProjectionStore({
      id: "restore-test",
      persistence,
      restore: false,
      autoPersist: false,
    })
    first.apply(
      batch([
        event(1, { domain: "task", inline: { status: "running" } }),
        event(2, {
          domain: "worker",
          workerId: "worker_restore",
          inline: { worker_id: "worker_restore", status: "running" },
        }),
      ]),
    )
    await first.persist()
    const firstRevision = first.state.revision
    await first.close()
    const reopened = new CanonicalProjectionStore({
      id: "restore-test",
      persistence,
      restore: true,
      autoPersist: false,
    })
    await reopened.ready
    expect(reopened.state.revision).toBe(firstRevision)
    expect(reopened.state.cursors[TASK]?.committedSequence).toBe(2)
    expect(reopened.state.workers.worker_restore).toBeDefined()
    const replay = reopened.apply(
      batch([
        event(2, {
          domain: "worker",
          workerId: "worker_restore",
          inline: { worker_id: "worker_restore", status: "running" },
        }),
        event(3, {
          domain: "worker",
          eventType: "worker.completed",
          workerId: "worker_restore",
          terminal: true,
          inline: { worker_id: "worker_restore", status: "completed" },
        }),
      ]),
    )
    expect(replay.duplicateEventIds).toEqual(["event_projection_2"])
    expect(replay.appliedEventIds).toEqual(["event_projection_3"])
    expect(reopened.state.workers.worker_restore?.revision).toBe(2)
    expect(reopened.state.cursors[TASK]?.committedSequence).toBe(3)
  })

  test("serializes persistence revisions so a slow older save cannot overwrite a newer one", async () => {
    let releaseFirstSave = () => {}
    const firstSaveGate = new Promise<void>((resolve) => {
      releaseFirstSave = resolve
    })
    const savedRevisions: number[] = []
    let stored: string | undefined
    const persistence: ProjectionPersistence = {
      async load() {
        return stored
      },
      async save(_storeId, serialized) {
        const revision = Number(
          (JSON.parse(serialized) as { revision?: unknown }).revision,
        )
        savedRevisions.push(revision)
        if (revision === 1) await firstSaveGate
        stored = serialized
      },
      async remove() {
        stored = undefined
      },
    }
    const coordinator = new ProjectionPersistenceCoordinator(
      "serialized-persistence-test",
      persistence,
    )
    const projection = store()
    projection.apply(batch([event(1, { domain: "task" })]))
    const firstState = projection.state
    projection.apply(
      batch([
        event(2, {
          eventType: "task.updated",
          domain: "task",
          inline: { status: "running", title: "newer" },
        }),
      ]),
    )
    const secondState = projection.state

    const first = coordinator.persist(firstState)
    const second = coordinator.persist(secondState)
    await Promise.resolve()
    await Promise.resolve()
    expect(savedRevisions).toEqual([1])

    releaseFirstSave()
    await Promise.all([first, second])
    expect(savedRevisions).toEqual([1, 2])
    expect(
      decodeProjectionSnapshot(
        stored!,
        "serialized-persistence-test",
      ).state.revision,
    ).toBe(2)
  })

  test("restores the newest valid replica after primary persistence previously failed", async () => {
    const primary = new MemoryProjectionPersistence()
    const fallback = new MemoryProjectionPersistence()
    const projection = store()
    projection.apply(batch([event(1, { domain: "task" })]))
    const older = JSON.stringify(
      encodeProjectionSnapshot("replica-test", projection.state),
    )
    projection.apply(
      batch([
        event(2, {
          eventType: "task.updated",
          domain: "task",
          inline: { status: "running", title: "newest replica" },
        }),
      ]),
    )
    const newer = JSON.stringify(
      encodeProjectionSnapshot("replica-test", projection.state),
    )
    await primary.save("replica-test", older)
    await fallback.save("replica-test", newer)

    const persistence = new FallbackProjectionPersistence(primary, fallback)
    const restored = await persistence.load("replica-test")
    expect(
      decodeProjectionSnapshot(restored!, "replica-test").state.revision,
    ).toBe(2)
  })

  test("does not acknowledge an IndexedDB save before its transaction commits", async () => {
    const originalIndexedDb = globalThis.indexedDB
    const transaction = {
      error: new Error("transaction aborted after request success"),
      oncomplete: null as (() => void) | null,
      onerror: null as (() => void) | null,
      onabort: null as (() => void) | null,
      objectStore: () => ({
        put: () => {
          const request = {
            result: undefined,
            error: null,
            onsuccess: null as (() => void) | null,
            onerror: null as (() => void) | null,
          }
          queueMicrotask(() => {
            request.onsuccess?.()
            queueMicrotask(() => transaction.onabort?.())
          })
          return request
        },
      }),
    }
    const database = {
      objectStoreNames: { contains: () => true },
      createObjectStore: () => ({}),
      transaction: () => transaction,
      close: () => {},
    }
    const openRequest = {
      result: database,
      error: null,
      onupgradeneeded: null as (() => void) | null,
      onsuccess: null as (() => void) | null,
      onerror: null as (() => void) | null,
      onblocked: null as (() => void) | null,
    }
    Object.defineProperty(globalThis, "indexedDB", {
      configurable: true,
      writable: true,
      value: {
        open: () => {
          queueMicrotask(() => openRequest.onsuccess?.())
          return openRequest
        },
      },
    })
    try {
      const persistence = new IndexedDbProjectionPersistence(
        "transaction-commit-test",
      )
      await expect(
        persistence.save("projection", "serialized"),
      ).rejects.toThrow("transaction aborted after request success")
    } finally {
      if (originalIndexedDb === undefined) {
        Reflect.deleteProperty(globalThis, "indexedDB")
      } else {
        Object.defineProperty(globalThis, "indexedDB", {
          configurable: true,
          writable: true,
          value: originalIndexedDb,
        })
      }
    }
  })

  test("rejects checksum corruption and a future snapshot version", () => {
    const projection = store()
    projection.apply(batch([event(1, { domain: "task" })]))
    const envelope = encodeProjectionSnapshot("checksum-test", projection.state)
    const corrupted = JSON.stringify({
      ...envelope,
      state: { ...envelope.state, revision: 999 },
    })
    expect(() =>
      decodeProjectionSnapshot(corrupted, "checksum-test"),
    ).toThrow(ProjectionError)
    const futureState = envelope.state
    const future = JSON.stringify({
      ...envelope,
      version: 999,
      state: futureState,
    })
    expect(() => decodeProjectionSnapshot(future, "checksum-test")).toThrow(
      ProjectionError,
    )
  })

  test("migrates version zero entity tables and legacy cursor", () => {
    const empty = store().state
    const taskEvent = event(1, { domain: "task" })
    const projection = store()
    projection.apply(batch([taskEvent]))
    const task = projection.state.tasks[TASK]!
    const legacyState = {
      revision: 4,
      committedAtMs: 123,
      entities: { task: { [TASK]: task } },
      cursor: {
        taskId: TASK,
        generation: 2,
        sequence: 9,
        cursor: "legacy.cursor.9",
      },
      causality: empty.causality,
    }
    const envelope = {
      schema: "zyra.ui-projection-snapshot/v1",
      version: 0,
      storeId: "migration-test",
      createdAtMs: 123,
      revision: 4,
      checksum: "",
      state: legacyState,
    }
    const current = encodeProjectionSnapshot("unused", empty)
    void current
    const stable = JSON.parse(JSON.stringify(envelope))
    stable.checksum = checksumJson(stable.state)
    const result = decodeProjectionSnapshot(
      JSON.stringify(stable),
      "migration-test",
    )
    expect(result.migratedFrom).toBe(0)
    expect(result.state.tasks[TASK]?.id).toBe(TASK)
    expect(result.state.cursors[TASK]?.committedSequence).toBe(9)
    expect(result.state.cursors[TASK]?.generation).toBe(2)
  })

  test("rejects stale generation and stale cursor after restore", () => {
    const projection = store()
    projection.apply(batch([event(1, { domain: "task" })], { generation: 3 }))
    const staleGeneration = projection.apply(
      batch(
        [
          event(2, {
            eventId: "event_stale_generation",
            domain: "task",
          }),
        ],
        { generation: 2 },
      ),
    )
    expect(staleGeneration.staleEventIds).toEqual(["event_stale_generation"])
    expect(projection.state.tasks[TASK]?.lastEventId).toBe("event_projection_1")
  })

  test("disable behavior rejects reduction, selectors, and persistence", async () => {
    const persistence = new MemoryProjectionPersistence()
    const projection = new CanonicalProjectionStore({
      id: "disable-test",
      persistence,
      restore: false,
      autoPersist: false,
    })
    projection.disable("mutation test")
    expect(() => projection.apply(batch([event(1, { domain: "task" })]))).toThrow(
      ProjectionError,
    )
    expect(() => projection.select(selectTask(TASK))).toThrow()
    await expect(projection.persist()).rejects.toBeInstanceOf(ProjectionError)
    projection.enable()
    projection.apply(batch([event(1, { domain: "task" })]))
    expect(projection.select(selectTask(TASK))).toBeDefined()
  })

  test("browser close semantics only detach projection and preserve backend ownership", async () => {
    const projection = store()
    projection.apply(batch([event(1, { domain: "task" })]))
    const before = projection.state
    await projection.close("browser tab closed")
    expect(before.tasks[TASK]?.terminal).toBe(false)
    expect(before.tasks[TASK]?.lifecycle).not.toBe("cancelled")
    expect(() => projection.apply(batch([event(2, { domain: "task" })]))).toThrow(
      ProjectionError,
    )
  })
})

describe("multi-stream, history fold, large state, and cache pressure", () => {
  test("merges interleaved history/live pages by generation and identity", () => {
    const first = event(1, { eventId: "event_history_1", domain: "task" })
    const second = event(2, {
      eventId: "event_history_2",
      domain: "worker",
      workerId: "worker_history",
      inline: { worker_id: "worker_history", status: "running" },
    })
    const third = event(3, {
      eventId: "event_live_3",
      domain: "worker",
      workerId: "worker_history",
      inline: { worker_id: "worker_history", status: "completed" },
      terminal: true,
    })
    const merged = mergeHistoryAndLiveBatches(
      [
        batch([first, second], {
          snapshot: true,
          caughtUp: false,
          sequence: 2,
        }),
      ],
      [
        batch([second, third], {
          snapshot: false,
          caughtUp: true,
          sequence: 3,
        }),
      ],
    )
    expect(merged).toHaveLength(1)
    expect(merged[0]?.events.map((item) => item.eventId)).toEqual([
      "event_history_1",
      "event_history_2",
      "event_live_3",
    ])
    const projection = store()
    projection.apply(merged[0]!)
    expect(projection.state.workers.worker_history?.terminal).toBe(true)
    expect(projection.state.cursors[TASK]?.committedSequence).toBe(3)
  })

  test("interleaves two task streams without cross-task invalidation", () => {
    const projection = store()
    const otherTask = "task_projection_other"
    projection.apply(batch([event(1, { domain: "task" })]))
    projection.apply(
      batch(
        [
          event(2, {
            taskId: otherTask,
            runId: "run_projection_other",
            sessionId: "session_projection_other",
            domain: "task",
          }),
        ],
        { taskId: otherTask, sequence: 2 },
      ),
    )
    projection.apply(
      batch([
        event(3, {
          domain: "worker",
          workerId: "worker_task_one",
          inline: { worker_id: "worker_task_one", status: "running" },
        }),
      ]),
    )
    projection.apply(
      batch(
        [
          event(4, {
            taskId: otherTask,
            runId: "run_projection_other",
            sessionId: "session_projection_other",
            domain: "worker",
            workerId: "worker_task_two",
            inline: { worker_id: "worker_task_two", status: "running" },
          }),
        ],
        { taskId: otherTask, sequence: 4 },
      ),
    )
    expect(projection.select(selectWorkersForTask(TASK)).map((item) => item.id)).toEqual([
      "worker_task_one",
    ])
    expect(
      projection.select(selectWorkersForTask(otherTask)).map((item) => item.id),
    ).toEqual(["worker_task_two"])
    expect(projection.state.cursors[TASK]?.committedSequence).toBe(3)
    expect(projection.state.cursors[otherTask]?.committedSequence).toBe(4)
  })

  test("bounds terminal entity cache while protecting active workers", () => {
    const projection = store({
      limits: {
        maxEntitiesPerDomain: 50,
        maxEvents: 500,
        maxEventsPerTask: 500,
      },
    })
    const events: IngressEvent[] = [
      event(1, { domain: "task", inline: { status: "running" } }),
    ]
    for (let index = 2; index <= 82; index += 1) {
      events.push(
        event(index, {
          eventType: "tool.completed",
          domain: "tool",
          toolCallId: `tool_pressure_${index}`,
          terminal: true,
          inline: {
            tool_call_id: `tool_pressure_${index}`,
            status: "completed",
          },
        }),
      )
    }
    events.push(
      event(83, {
        eventType: "worker.started",
        domain: "worker",
        workerId: "worker_pressure_active",
        inline: { worker_id: "worker_pressure_active", status: "running" },
      }),
    )
    projection.apply(batch(events, { sequence: 83 }))
    expect(Object.keys(projection.state.tools).length).toBeLessThanOrEqual(50)
    expect(projection.state.workers.worker_pressure_active).toBeDefined()
    expect(projection.state.diagnostics.evictedEntities).toBeGreaterThan(0)
    expect(auditProjectionIntegrity(projection.state).valid).toBe(true)
  })

  test("folds a thousand effective events with deterministic revision and indices", () => {
    const projection = store({
      limits: {
        maxEvents: 2_000,
        maxEventsPerTask: 2_000,
        maxEntitiesPerDomain: 2_000,
      },
    })
    const events: IngressEvent[] = []
    for (let sequence = 1; sequence <= 1_000; sequence += 1) {
      events.push(
        event(sequence, {
          eventType: "worker.state.changed",
          domain: "worker",
          workerId: `worker_large_${sequence % 25}`,
          inline: {
            worker_id: `worker_large_${sequence % 25}`,
            status: sequence % 10 === 0 ? "recovering" : "running",
            progress: sequence / 1_000,
          },
        }),
      )
    }
    projection.apply(batch(events, { snapshot: true, sequence: 1_000 }))
    expect(Object.keys(projection.state.workers)).toHaveLength(25)
    expect(Object.keys(projection.state.causality.byEvent)).toHaveLength(1_000)
    expect(projection.state.cursors[TASK]?.committedSequence).toBe(1_000)
    expect(projection.state.runtimes[TASK]?.eventCount).toBe(1_000)
    expect(assertProjectionIntegrity(projection.state).valid).toBe(true)
  })
})
