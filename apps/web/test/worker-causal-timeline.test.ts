import { describe, expect, test } from "bun:test"
import {
  CanonicalProjectionStore,
  TimelineProjectionError,
  WorkerCausalTimelineProjectionEngine,
  buildWorkerCausalTimeline,
  filterTimelineRows,
  revealTimelineRow,
  selectWorkerCausalTimeline,
  windowTimelineRows,
  type WorkerCausalTimelineProjection,
} from "../src/state/index.ts"
import { TimelineWorkbenchController } from "../src/features/timeline/view/controller.ts"
import {
  normalizeEventFrame,
  type IngressBatch,
  type IngressEvent,
  type JsonObject,
} from "../src/events/ingress/index.ts"
import { TimelinePhase } from "../src/features/timeline/projection/contracts.ts"

const TASK = "task_worker_timeline"
const RUN = "run_worker_timeline"
const SESSION = "session_worker_timeline"
const NOW = "2026-07-24T08:00:00.000Z"
const DIGEST = `sha256:${"e".repeat(64)}`

interface TimelineEventOptions {
  eventId?: string
  eventType: string
  domain: string
  inline?: JsonObject
  metadata?: JsonObject
  workerId?: string
  nodeId?: string
  spanId?: string
  parentSpanId?: string
  toolCallId?: string
  artifactId?: string
  checkpointId?: string
  commandId?: string
  causationId?: string
  correlationId?: string
  terminal?: boolean
  effective?: boolean
  committedOffset?: number
  createdOffset?: number
  artifactRefs?: Array<{
    artifactId: string
    title?: string
    mediaType?: string
    sizeBytes?: number
  }>
}

function timelineEvent(
  sequence: number,
  options: TimelineEventOptions,
): IngressEvent {
  const eventId = options.eventId ?? `timeline_event_${sequence}`
  const correlationId =
    options.correlationId ?? "request_worker_timeline"
  const raw = {
    schema: "zyra.runtime-event/v1",
    eventId,
    eventType: options.eventType,
    eventVersion: 1,
    aggregateId: `task:${TASK}`,
    aggregateSequence: sequence,
    globalSequence: sequence,
    producerSequence: sequence,
    idempotencyKey: `worker-timeline-${eventId}`,
    correlationId,
    causationId: options.causationId,
    createdAt: new Date(
      Date.parse(NOW) + (options.createdOffset ?? sequence * 100),
    ).toISOString(),
    committedAt: new Date(
      Date.parse(NOW) + (options.committedOffset ?? sequence * 100),
    ).toISOString(),
    durability: "durable",
    effect: options.effective === false ? "non_effective" : "effective",
    identity: {
      taskId: TASK,
      runId: RUN,
      sessionId: SESSION,
      nodeId: options.nodeId,
      workerId: options.workerId,
      spanId: options.spanId ?? `span_timeline_${sequence}`,
      parentSpanId: options.parentSpanId,
      toolCallId: options.toolCallId,
      artifactId: options.artifactId,
      checkpointId: options.checkpointId,
      controlCommandId: options.commandId,
    },
    sender: {
      kind: "runtime",
      id: options.workerId ?? "worker-timeline-runtime",
      capabilityRefs: [],
    },
    intent: "status",
    topKRecipients: [],
    summary: `${options.eventType} ${sequence}`,
    stateDelta: {
      domain: options.domain,
      operation: "transition",
      path: ["status"],
      beforeDigest: `sha256:${"1".repeat(64)}`,
      afterDigest: `sha256:${"2".repeat(64)}`,
      effective: options.effective !== false,
    },
    evidenceRefs: [],
    artifactRefs: (options.artifactRefs ?? []).map((artifact) => ({
      artifactId: artifact.artifactId,
      digest: DIGEST,
      mediaType: artifact.mediaType ?? "application/json",
      sizeBytes: artifact.sizeBytes ?? 128,
      title: artifact.title ?? artifact.artifactId,
    })),
    provenance: {
      sourceRepository: "zyra",
      sourceModule: "worker-causal-timeline-test",
      migrationRole: "zyra_owned",
      producerVersion: "test",
      trust: "internal",
    },
    inline: {
      ...(options.inline ?? {}),
      ...(options.terminal ? { terminal: true } : {}),
    },
    sourceBytes: 256,
    inlineBytes: 128,
    envelopeBytes: 2048,
    contentDigest: DIGEST,
    metadata: options.metadata ?? {},
    settlement: "atomic",
    terminal: options.terminal ?? false,
  }
  return normalizeEventFrame(
    JSON.parse(
      JSON.stringify({
        schema: "zyra.event-ingress-frame/v1",
        kind: "event",
        source: "delta",
        generation: 1,
        taskId: TASK,
        sequence,
        previousSequence: Math.max(0, sequence - 1),
        eventId,
        eventType: options.eventType,
        correlationId,
        causationId: options.causationId,
        observedAtMs: 20_000 + (options.committedOffset ?? sequence),
        cursor: `cursor.${sequence}`,
        event: raw,
      }),
    ),
    TASK,
    1,
  ).event
}

function timelineBatch(
  events: readonly IngressEvent[],
  options: {
    sequence?: number
    highWatermark?: number
    snapshot?: boolean
    caughtUp?: boolean
  } = {},
): IngressBatch {
  const sequence =
    options.sequence ??
    Math.max(0, ...events.map((event) => event.globalSequence))
  const highWatermark = options.highWatermark ?? sequence
  return {
    taskId: TASK,
    generation: 1,
    events: Object.freeze([...events]),
    receipts: Object.freeze([]),
    cursor: `cursor.${sequence}`,
    fromSequence: Math.min(
      sequence,
      ...events.map((event) => event.globalSequence),
    ),
    sequence,
    highWatermark,
    receivedAt: 30_000 + sequence,
    transport: "long_poll",
    snapshot: options.snapshot ?? true,
    caughtUp: options.caughtUp ?? highWatermark === sequence,
  }
}

function projectionStore(): CanonicalProjectionStore {
  return new CanonicalProjectionStore({
    restore: false,
    autoPersist: false,
    now: () => Date.parse(NOW) + 90_000,
  })
}

function completeScenario(): IngressEvent[] {
  return [
    timelineEvent(1, {
      eventId: "timeline_task_started",
      eventType: "task.started",
      domain: "task",
      nodeId: "node_plan",
      inline: {
        task_id: TASK,
        title: "Worker timeline task",
        status: "running",
      },
    }),
    timelineEvent(2, {
      eventId: "timeline_node_created",
      eventType: "topology.node.created",
      domain: "node",
      nodeId: "node_execute",
      causationId: "timeline_task_started",
      inline: {
        node_id: "node_execute",
        role: "browser-worker",
        status: "running",
      },
    }),
    timelineEvent(3, {
      eventId: "timeline_worker_queued",
      eventType: "worker.queued",
      domain: "worker",
      nodeId: "node_execute",
      workerId: "worker_primary",
      causationId: "timeline_node_created",
      inline: {
        worker_id: "worker_primary",
        lease_id: "lease_primary_1",
        role: "browser-worker",
        status: "queued",
      },
    }),
    timelineEvent(4, {
      eventId: "timeline_worker_admitted",
      eventType: "worker.admitted",
      domain: "worker",
      nodeId: "node_execute",
      workerId: "worker_primary",
      causationId: "timeline_worker_queued",
      inline: {
        worker_id: "worker_primary",
        lease_id: "lease_primary_1",
        route_id: "route_edge",
        placement_id: "placement_edge",
        role: "browser-worker",
        status: "admitted",
      },
    }),
    timelineEvent(5, {
      eventId: "timeline_worker_started",
      eventType: "worker.started",
      domain: "worker",
      nodeId: "node_execute",
      workerId: "worker_primary",
      causationId: "timeline_worker_admitted",
      inline: {
        worker_id: "worker_primary",
        lease_id: "lease_primary_1",
        status: "starting",
      },
    }),
    timelineEvent(6, {
      eventId: "timeline_worker_running",
      eventType: "worker.running",
      domain: "worker",
      nodeId: "node_execute",
      workerId: "worker_primary",
      causationId: "timeline_worker_started",
      inline: {
        worker_id: "worker_primary",
        lease_id: "lease_primary_1",
        status: "running",
      },
    }),
    timelineEvent(7, {
      eventId: "timeline_browser_action",
      eventType: "browser.action.started",
      domain: "tool",
      nodeId: "node_execute",
      workerId: "worker_primary",
      spanId: "span_browser_step",
      parentSpanId: "span_timeline_6",
      toolCallId: "tool_browser_1",
      causationId: "timeline_worker_running",
      inline: {
        tool_call_id: "tool_browser_1",
        tool_name: "browser.click",
        browser_step_id: "browser_step_1",
        action_id: "action_click_1",
        action_name: "click",
        status: "running",
      },
    }),
    timelineEvent(8, {
      eventId: "timeline_permission_asked",
      eventType: "permission.asked",
      domain: "permission",
      nodeId: "node_execute",
      workerId: "worker_primary",
      spanId: "span_browser_step",
      toolCallId: "tool_browser_1",
      causationId: "timeline_browser_action",
      inline: {
        permission_id: "permission_browser_1",
        request_id: "permission_browser_1",
        tool_name: "browser.click",
        decision: "ask",
        status: "waiting_policy",
      },
    }),
    timelineEvent(9, {
      eventId: "timeline_permission_allowed",
      eventType: "permission.replied",
      domain: "permission",
      nodeId: "node_execute",
      workerId: "worker_primary",
      spanId: "span_browser_step",
      toolCallId: "tool_browser_1",
      causationId: "timeline_permission_asked",
      inline: {
        permission_id: "permission_browser_1",
        request_id: "permission_browser_1",
        tool_name: "browser.click",
        decision: "allow",
        resolved_at: "2026-07-24T08:00:00.900Z",
        status: "completed",
      },
      terminal: true,
    }),
    timelineEvent(10, {
      eventId: "timeline_browser_result",
      eventType: "browser.action.completed",
      domain: "tool",
      nodeId: "node_execute",
      workerId: "worker_primary",
      spanId: "span_browser_step",
      toolCallId: "tool_browser_1",
      causationId: "timeline_permission_allowed",
      inline: {
        tool_call_id: "tool_browser_1",
        tool_name: "browser.click",
        browser_step_id: "browser_step_1",
        action_id: "action_click_1",
        result_status: "completed",
        duration_ms: 300,
        status: "completed",
      },
      terminal: true,
    }),
    timelineEvent(11, {
      eventId: "timeline_browser_state",
      eventType: "browser.state.snapshot",
      domain: "artifact",
      nodeId: "node_execute",
      workerId: "worker_primary",
      spanId: "span_browser_step",
      toolCallId: "tool_browser_1",
      artifactId: "artifact_browser_1",
      causationId: "timeline_browser_result",
      artifactRefs: [
        {
          artifactId: "artifact_browser_1",
          title: "Browser screenshot",
          mediaType: "image/png",
          sizeBytes: 2_048,
        },
      ],
      inline: {
        artifact_id: "artifact_browser_1",
        browser_step_id: "browser_step_1",
        status: "completed",
      },
      terminal: true,
    }),
    timelineEvent(12, {
      eventId: "timeline_watchdog_failure",
      eventType: "watchdog.worker.failed",
      domain: "worker",
      nodeId: "node_execute",
      workerId: "worker_primary",
      causationId: "timeline_browser_state",
      inline: {
        worker_id: "worker_primary",
        lease_id: "lease_primary_1",
        failure_id: "failure_worker_1",
        error_code: "heartbeat_timeout",
        reason: "heartbeat timeout",
        status: "failed",
      },
      terminal: true,
    }),
    timelineEvent(13, {
      eventId: "timeline_recovery_started",
      eventType: "recovery.started",
      domain: "recovery",
      nodeId: "node_execute",
      workerId: "worker_primary",
      causationId: "timeline_watchdog_failure",
      checkpointId: "checkpoint_before_failure",
      inline: {
        recovery_id: "recovery_worker_1",
        failure_id: "failure_worker_1",
        attempt: 1,
        strategy: "replace_worker",
        previous_worker_id: "worker_primary",
        replacement_worker_id: "worker_replacement",
        checkpoint_id: "checkpoint_before_failure",
        status: "recovering",
      },
    }),
    timelineEvent(14, {
      eventId: "timeline_replacement_admitted",
      eventType: "worker.admitted",
      domain: "worker",
      nodeId: "node_execute",
      workerId: "worker_replacement",
      causationId: "timeline_recovery_started",
      inline: {
        worker_id: "worker_replacement",
        lease_id: "lease_replacement_1",
        route_id: "route_local",
        placement_id: "placement_local",
        status: "admitted",
      },
    }),
    timelineEvent(15, {
      eventId: "timeline_recovery_completed",
      eventType: "recovery.completed",
      domain: "recovery",
      nodeId: "node_execute",
      workerId: "worker_replacement",
      causationId: "timeline_replacement_admitted",
      checkpointId: "checkpoint_before_failure",
      inline: {
        recovery_id: "recovery_worker_1",
        failure_id: "failure_worker_1",
        attempt: 1,
        strategy: "replace_worker",
        previous_worker_id: "worker_primary",
        replacement_worker_id: "worker_replacement",
        checkpoint_id: "checkpoint_before_failure",
        status: "completed",
      },
      terminal: true,
    }),
    timelineEvent(16, {
      eventId: "timeline_recovery_duplicate",
      eventType: "recovery.completed",
      domain: "recovery",
      nodeId: "node_execute",
      workerId: "worker_replacement",
      causationId: "timeline_replacement_admitted",
      checkpointId: "checkpoint_before_failure",
      inline: {
        recovery_id: "recovery_worker_1",
        failure_id: "failure_worker_1",
        attempt: 1,
        strategy: "replace_worker",
        previous_worker_id: "worker_primary",
        replacement_worker_id: "worker_replacement",
        checkpoint_id: "checkpoint_before_failure",
        status: "completed",
      },
      terminal: true,
    }),
    timelineEvent(17, {
      eventId: "timeline_background_started",
      eventType: "background.job.started",
      domain: "worker",
      nodeId: "node_execute",
      workerId: "worker_replacement",
      causationId: "timeline_recovery_completed",
      inline: {
        worker_id: "worker_replacement",
        background_job_id: "background_job_1",
        job_id: "background_job_1",
        job_type: "task",
        label: "verify recovery",
        status: "running",
      },
    }),
    timelineEvent(18, {
      eventId: "timeline_background_parked",
      eventType: "background.job.parked",
      domain: "worker",
      nodeId: "node_execute",
      workerId: "worker_replacement",
      causationId: "timeline_background_started",
      inline: {
        worker_id: "worker_replacement",
        background_job_id: "background_job_1",
        job_id: "background_job_1",
        status: "parked",
      },
    }),
    timelineEvent(19, {
      eventId: "timeline_background_revived",
      eventType: "background.job.revived",
      domain: "worker",
      nodeId: "node_execute",
      workerId: "worker_replacement",
      causationId: "timeline_background_parked",
      inline: {
        worker_id: "worker_replacement",
        background_job_id: "background_job_1",
        job_id: "background_job_1",
        status: "reviving",
      },
    }),
    timelineEvent(20, {
      eventId: "timeline_background_completed",
      eventType: "background.job.completed",
      domain: "worker",
      nodeId: "node_execute",
      workerId: "worker_replacement",
      causationId: "timeline_background_revived",
      inline: {
        worker_id: "worker_replacement",
        background_job_id: "background_job_1",
        job_id: "background_job_1",
        status: "completed",
      },
      terminal: true,
    }),
    timelineEvent(21, {
      eventId: "timeline_worker_completed",
      eventType: "worker.completed",
      domain: "worker",
      nodeId: "node_execute",
      workerId: "worker_replacement",
      causationId: "timeline_background_completed",
      inline: {
        worker_id: "worker_replacement",
        lease_id: "lease_replacement_1",
        status: "completed",
      },
      terminal: true,
    }),
  ]
}

function projectComplete(): {
  store: CanonicalProjectionStore
  projection: WorkerCausalTimelineProjection
} {
  const store = projectionStore()
  store.apply(timelineBatch(completeScenario()))
  return {
    store,
    projection: buildWorkerCausalTimeline(store.state, TASK),
  }
}

describe("worker causal timeline projection", () => {
  test("projects canonical lifecycle, causal joins, worker replacement, browser steps, and background revival", () => {
    const { projection } = projectComplete()
    const phases = new Set(projection.rows.map((row) => row.phase))
    expect(phases).toContain(TimelinePhase.QUEUED)
    expect(phases).toContain(TimelinePhase.ADMITTED)
    expect(phases).toContain(TimelinePhase.STARTING)
    expect(phases).toContain(TimelinePhase.RUNNING)
    expect(phases).toContain(TimelinePhase.WAITING_TOOL)
    expect(phases).toContain(TimelinePhase.WAITING_POLICY)
    expect(phases).toContain(TimelinePhase.FAILED)
    expect(phases).toContain(TimelinePhase.RECOVERING)
    expect(phases).toContain(TimelinePhase.COMPLETED)
    expect(projection.workerEpochs.map((epoch) => epoch.workerId)).toEqual(
      expect.arrayContaining(["worker_primary", "worker_replacement"]),
    )
    const primary = projection.workerEpochs.find(
      (epoch) => epoch.workerId === "worker_primary",
    )
    const replacement = projection.workerEpochs.find(
      (epoch) => epoch.workerId === "worker_replacement",
    )
    expect(primary?.failureIds).toContain("failure_worker_1")
    expect(replacement?.leaseId).toBe("lease_replacement_1")
    expect(
      projection.graph.edges.some(
        (edge) =>
          edge.kind === "worker-replacement" &&
          edge.targetEventId === "timeline_replacement_admitted" &&
          edge.metadata.previousWorkerId === "worker_primary" &&
          edge.metadata.replacementWorkerId === "worker_replacement",
      ),
    ).toBe(true)
    expect(projection.browserSteps).toHaveLength(1)
    expect(projection.browserSteps[0]?.actionEventIds).toContain(
      "timeline_browser_action",
    )
    expect(projection.browserSteps[0]?.resultEventIds).toContain(
      "timeline_browser_result",
    )
    expect(projection.browserSteps[0]?.stateEventIds).toContain(
      "timeline_browser_state",
    )
    expect(projection.browserSteps[0]?.artifactIds).toContain(
      "artifact_browser_1",
    )
    expect(projection.backgroundLifecycles[0]?.revivalCount).toBe(1)
    expect(projection.backgroundLifecycles[0]?.status).toBe("completed")
    expect(projection.rowKeyByEvent.timeline_browser_action).not.toBe(
      projection.rowKeyByEvent.timeline_browser_result,
    )
    expect(projection.rowKeyByEvent.timeline_browser_result).toBe(
      projection.rowKeyByEvent.timeline_browser_state,
    )
  })

  test("joins failure, recovery, checkpoint, route, mutation, artifact, and tool evidence", () => {
    const { projection } = projectComplete()
    const failureRow =
      projection.rowsByKey[
        projection.rowKeyByEvent.timeline_watchdog_failure!
      ]
    const recoveryRow =
      projection.rowsByKey[
        projection.rowKeyByEvent.timeline_recovery_started!
      ]
    const browserRow =
      projection.rowsByKey[
        projection.rowKeyByEvent.timeline_browser_state!
      ]
    expect(failureRow?.failureId).toBe("failure_worker_1")
    expect(
      failureRow?.evidence.some(
        (item) =>
          item.kind === "failure" && item.id === "failure_worker_1",
      ),
    ).toBe(true)
    expect(recoveryRow?.recoveryId).toBe("recovery_worker_1")
    expect(
      recoveryRow?.evidence.some(
        (item) => item.kind === "checkpoint",
      ),
    ).toBe(true)
    expect(
      recoveryRow?.drilldowns.some(
        (target) =>
          target.kind === "recovery" &&
          target.entityId === "recovery_worker_1",
      ),
    ).toBe(true)
    expect(browserRow?.toolCallId).toBe("tool_browser_1")
    expect(browserRow?.artifactIds).toContain("artifact_browser_1")
    expect(browserRow?.mutationIds.length).toBeGreaterThan(0)
    expect(
      projection.rows.some(
        (row) =>
          row.routeId === "route_edge" ||
          row.routeId === "route_local",
      ),
    ).toBe(true)
  })

  test("collapses duplicate recovery terminals without collapsing distinct attempts", () => {
    const { projection } = projectComplete()
    const chain = projection.recoveryChains.find(
      (candidate) => candidate.failureId === "failure_worker_1",
    )
    expect(chain).toBeDefined()
    expect(chain?.attempts).toHaveLength(1)
    expect(chain?.attempts[0]?.eventIds).toContain(
      "timeline_recovery_completed",
    )
    expect(chain?.attempts[0]?.duplicateEventIds).toContain(
      "timeline_recovery_duplicate",
    )
    expect(chain?.duplicateTerminalCount).toBe(1)
    expect(projection.diagnostics.duplicateRecoveryCount).toBe(1)

    const store = projectionStore()
    const events = [
      ...completeScenario(),
      timelineEvent(22, {
        eventId: "timeline_recovery_attempt_2",
        eventType: "recovery.started",
        domain: "recovery",
        workerId: "worker_replacement",
        causationId: "timeline_recovery_completed",
        inline: {
          recovery_id: "recovery_worker_1",
          failure_id: "failure_worker_1",
          attempt: 2,
          strategy: "reroute",
          status: "recovering",
        },
      }),
    ]
    store.apply(timelineBatch(events))
    const updated = buildWorkerCausalTimeline(store.state, TASK)
    const updatedChain = updated.recoveryChains.find(
      (candidate) => candidate.failureId === "failure_worker_1",
    )
    expect(updatedChain?.attempts.map((attempt) => attempt.attempt)).toEqual([
      1,
      2,
    ])
  })

  test("derives a deterministic critical path from explicit and implicit causal edges", () => {
    const { projection } = projectComplete()
    expect(projection.criticalPath.eventIds[0]).toBe(
      "timeline_task_started",
    )
    expect(projection.criticalPath.eventIds).toContain(
      "timeline_watchdog_failure",
    )
    expect(projection.criticalPath.eventIds).toContain(
      "timeline_recovery_started",
    )
    expect(projection.criticalPath.rowKeys.length).toBeGreaterThan(0)
    expect(projection.criticalPath.workerIds).toEqual(
      expect.arrayContaining(["worker_primary", "worker_replacement"]),
    )
    expect(projection.criticalPath.totalWeight).toBeGreaterThan(0)
    expect(
      projection.criticalPath.rowKeys.every(
        (key) => projection.rowsByKey[key]?.critical,
      ),
    ).toBe(true)
    const second = projectComplete().projection
    expect(second.criticalPath.eventIds).toEqual(
      projection.criticalPath.eventIds,
    )
    expect(second.criticalPath.totalWeight).toBe(
      projection.criticalPath.totalWeight,
    )
  })

  test("preserves hidden causality summaries under worker, status, type, time, and search filters", () => {
    const { projection } = projectComplete()
    const filtered = filterTimelineRows(
      projection.rows,
      projection.graph,
      {
        workerIds: ["worker_replacement"],
        phases: [TimelinePhase.COMPLETED],
        search: "background",
      },
    )
    expect(filtered.matchedRowCount).toBeGreaterThan(0)
    expect(filtered.hiddenRowCount).toBeGreaterThan(0)
    expect(filtered.gaps.length).toBeGreaterThan(0)
    expect(
      filtered.gaps.some(
        (gap) =>
          gap.hiddenEventIds.length > 0 &&
          gap.boundaryEventIds.length > 0,
      ),
    ).toBe(true)
    const failureOnly = filterTimelineRows(
      projection.rows,
      projection.graph,
      { failuresOnly: true },
    )
    expect(
      failureOnly.rows.some(
        (row) =>
          row.failureId === "failure_worker_1" ||
          row.recoveryId === "recovery_worker_1",
      ),
    ).toBe(true)
    const critical = filterTimelineRows(
      projection.rows,
      projection.graph,
      { criticalOnly: true },
    )
    expect(critical.matchedRowCount).toBeGreaterThan(0)
    expect(critical.rows.some((row) => row.critical)).toBe(true)
  })

  test("reconciles partial and late events into stable semantic row identities", () => {
    const store = projectionStore()
    const initial = [
      timelineEvent(1, {
        eventId: "partial_task",
        eventType: "task.started",
        domain: "task",
        inline: { task_id: TASK, status: "running" },
      }),
      timelineEvent(3, {
        eventId: "partial_result",
        eventType: "browser.action.completed",
        domain: "tool",
        workerId: "worker_partial",
        toolCallId: "tool_partial",
        causationId: "missing_action_event",
        committedOffset: 300,
        inline: {
          tool_call_id: "tool_partial",
          browser_step_id: "browser_partial",
          action_id: "action_partial",
          status: "completed",
        },
        terminal: true,
      }),
    ]
    store.apply(
      timelineBatch(initial, {
        sequence: 3,
        highWatermark: 4,
        caughtUp: false,
      }),
    )
    const partial = buildWorkerCausalTimeline(store.state, TASK)
    expect(partial.browserSteps[0]?.partial).toBe(true)
    expect(partial.graph.missingEventIds).toContain("missing_action_event")
    expect(partial.diagnostics.ready).toBe(false)
    const originalKey = partial.rowKeyByEvent.partial_result

    store.apply(
      timelineBatch(
        [
          timelineEvent(4, {
            eventId: "missing_action_event",
            eventType: "browser.action.started",
            domain: "tool",
            workerId: "worker_partial",
            toolCallId: "tool_partial",
            causationId: "partial_task",
            committedOffset: 500,
            inline: {
              tool_call_id: "tool_partial",
              browser_step_id: "browser_partial",
              action_id: "action_partial",
              action_name: "click",
              status: "running",
            },
          }),
        ],
        {
          sequence: 4,
          highWatermark: 4,
          snapshot: false,
          caughtUp: true,
        },
      ),
    )
    const reconciled = buildWorkerCausalTimeline(store.state, TASK)
    expect(reconciled.rowKeyByEvent.missing_action_event).not.toBe(originalKey)
    expect(reconciled.rowKeyByEvent.partial_result).toBe(originalKey)
    expect(reconciled.browserSteps[0]?.partial).toBe(false)
    expect(
      reconciled.rowsByKey[
        reconciled.rowKeyByEvent.missing_action_event!
      ]?.late,
    ).toBe(true)
    expect(reconciled.diagnostics.ready).toBe(true)
  })

  test("creates a new epoch for lease replacement without rewriting the earlier epoch", () => {
    const store = projectionStore()
    store.apply(
      timelineBatch([
        timelineEvent(1, {
          eventType: "task.started",
          domain: "task",
          inline: { task_id: TASK, status: "running" },
        }),
        timelineEvent(2, {
          eventId: "lease_one",
          eventType: "worker.running",
          domain: "worker",
          workerId: "worker_lease",
          inline: {
            worker_id: "worker_lease",
            lease_id: "lease_1",
            status: "running",
          },
        }),
        timelineEvent(3, {
          eventId: "lease_two",
          eventType: "worker.lease.replaced",
          domain: "worker",
          workerId: "worker_lease",
          causationId: "lease_one",
          inline: {
            worker_id: "worker_lease",
            lease_id: "lease_2",
            status: "starting",
          },
        }),
        timelineEvent(4, {
          eventId: "lease_two_running",
          eventType: "worker.running",
          domain: "worker",
          workerId: "worker_lease",
          causationId: "lease_two",
          inline: {
            worker_id: "worker_lease",
            lease_id: "lease_2",
            status: "running",
          },
        }),
      ]),
    )
    const projection = buildWorkerCausalTimeline(store.state, TASK)
    const epochs = projection.workerEpochs.filter(
      (epoch) => epoch.workerId === "worker_lease",
    )
    expect(epochs).toHaveLength(2)
    expect(epochs[0]?.leaseId).toBeUndefined()
    expect(epochs[1]?.leaseId).toBe("lease_2")
    expect(epochs[0]?.replacementEpochId).toBe(epochs[1]?.id)
    expect(epochs[1]?.previousEpochId).toBe(epochs[0]?.id)
    expect(epochs[0]?.eventIds).not.toContain("lease_two")
    expect(
      projection.graph.edges.some(
        (edge) =>
          edge.kind === "lease-replacement" &&
          edge.sourceEventId === "lease_one" &&
          edge.targetEventId === "lease_two",
      ),
    ).toBe(true)
  })

  test("keeps stable bounded windows and can reveal an event row outside the active window", () => {
    const { projection } = projectComplete()
    const first = windowTimelineRows(projection.rows, {
      start: 0,
      count: 4,
      overscan: 1,
    })
    expect(first.total).toBe(projection.rows.length)
    expect(first.rows.length).toBeLessThanOrEqual(6)
    expect(first.afterCount).toBeGreaterThan(0)
    const target = projection.rows.at(-1)!
    const revealed = revealTimelineRow(
      projection.rows,
      target.key,
      4,
      1,
    )
    expect(revealed.rows.map((row) => row.key)).toContain(target.key)
    expect(revealed.pinnedRowKeys).toContain(target.key)
    expect(
      revealTimelineRow(projection.rows, target.key, 4, 1).revisionKey,
    ).toBe(revealed.revisionKey)
  })

  test("selector is revision-aware and the projection engine caches only identical canonical state", () => {
    const store = projectionStore()
    store.apply(timelineBatch(completeScenario()))
    const selector = selectWorkerCausalTimeline(TASK)
    const selected = store.select(selector)
    expect(selected.taskId).toBe(TASK)
    expect(selected.projectionRevision).toBe(store.state.revision)
    const engine = new WorkerCausalTimelineProjectionEngine({
      now: (() => {
        let value = 100
        return () => ++value
      })(),
    })
    const first = engine.project(store.state, TASK)
    const cached = engine.project(store.state, TASK)
    expect(cached).toBe(first)
    expect(engine.audit().projectionCount).toBe(1)
    store.apply(
      timelineBatch(
        [
          timelineEvent(22, {
            eventType: "worker.updated",
            domain: "worker",
            workerId: "worker_replacement",
            inline: {
              worker_id: "worker_replacement",
              lease_id: "lease_replacement_1",
              status: "completed",
            },
            terminal: true,
          }),
        ],
        { sequence: 22, snapshot: false },
      ),
    )
    const updated = engine.project(store.state, TASK)
    expect(updated).not.toBe(first)
    expect(updated.projectionRevision).toBeGreaterThan(
      first.projectionRevision,
    )
    expect(engine.audit().projectionCount).toBe(2)
    engine.close()
    expect(() => engine.project(store.state, TASK)).toThrow(
      "projection engine is closed",
    )
  })

  test("fails closed when the dedicated projector is disabled", () => {
    const { store } = projectComplete()
    expect(() =>
      buildWorkerCausalTimeline(store.state, TASK, { disabled: true }),
    ).toThrow(TimelineProjectionError)
    expect(() =>
      buildWorkerCausalTimeline(store.state, TASK, { disabled: true }),
    ).toThrow("Worker causal timeline projection is disabled")
    const engine = new WorkerCausalTimelineProjectionEngine({
      disabled: true,
    })
    expect(() => engine.project(store.state, TASK)).toThrow(
      "Worker causal timeline projection is disabled",
    )
    expect(engine.audit().disabled).toBe(true)
  })

  test("transient controller changes filters, selection, expansion, and window without mutating the projection", () => {
    const { projection } = projectComplete()
    const controller = new TimelineWorkbenchController(TASK, projection, {
      windowCount: 20,
      overscan: 2,
    })
    const originalRows = projection.rows
    const failureRow = projection.rows.find(
      (row) => row.failureId === "failure_worker_1",
    )!
    expect(controller.selectRow(failureRow.key)).toBe(true)
    expect(controller.getSnapshot().selectedRow?.key).toBe(failureRow.key)
    controller.toggleExpanded(failureRow.key)
    expect(
      controller.getSnapshot().view.expandedRowKeys.has(failureRow.key),
    ).toBe(true)
    controller.toggleFailures()
    expect(controller.getSnapshot().view.filter.failuresOnly).toBe(true)
    expect(
      controller.getSnapshot().view.filtered.rows.some(
        (row) => row.failureId === "failure_worker_1",
      ),
    ).toBe(true)
    controller.setSearch("background")
    expect(controller.getSnapshot().view.filter.search).toBe("background")
    controller.resetFilters()
    expect(controller.getSnapshot().view.filtered.activeFilterCount).toBe(0)
    expect(projection.rows).toBe(originalRows)
    expect(projection.rows.length).toBeGreaterThan(0)
    controller.close()
    expect(controller.getSnapshot().closed).toBe(true)
  })
})
