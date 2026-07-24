import { describe, expect, test } from "bun:test"
import {
  TimelineEventKind,
  TimelinePhase,
  type TimelineRow,
  type WorkerCausalTimelineProjection,
} from "../src/features/timeline/projection/index.ts"
import {
  ScaledTimelineRuntime,
  TimelineSearchIndex,
  TimelineVirtualizer,
  foldTimelineProjection,
  projectTimelineGoalDrift,
  projectTimelineOverlays,
} from "../src/features/timeline/scale/index.ts"

const TASK = "task_scaled_timeline"
const RUN = "run_scaled_timeline"
const START = Date.parse("2026-07-24T09:00:00.000Z")

function row(index: number): TimelineRow {
  const control = index === 1_500
  const requirement = index === 2_500
  const failure = index === 3_500
  const heartbeat = index % 97 === 0
  const replay = index % 211 === 0
  const rowKind = control
    ? TimelineEventKind.COMMAND
    : failure
      ? TimelineEventKind.FAILURE
      : requirement
        ? TimelineEventKind.MUTATION
        : TimelineEventKind.WORKER
  const title = control
    ? "/reassign worker timeline control"
    : requirement
      ? "requirement changed require verifier evidence"
      : failure
        ? "worker failure heartbeat timeout"
        : heartbeat
          ? "worker heartbeat"
          : replay
            ? "checkpoint replay observation"
            : "worker execution observation"
  return Object.freeze({
    key: `row_${index}`,
    taskId: TASK,
    runId: RUN,
    rowKind,
    phase: failure ? TimelinePhase.FAILED : TimelinePhase.RUNNING,
    primaryEventId: `event_${index}`,
    eventIds: Object.freeze([`event_${index}`]),
    sequence: index + 1,
    endSequence: index + 1,
    occurredAt: new Date(START + index * 10).toISOString(),
    committedAt: new Date(START + index * 10 + 2).toISOString(),
    title,
    summary: `${title}; worker=worker_${index % 12}; batch=${Math.floor(index / 100)}`,
    workerId: `worker_${index % 12}`,
    workerEpochId: `epoch_${Math.floor(index / 500)}`,
    leaseId: `lease_${Math.floor(index / 500)}`,
    nodeId: `node_${index % 30}`,
    routeId: index % 401 === 0 ? `route_${index}` : undefined,
    placementId: index % 401 === 0 ? `placement_${index}` : undefined,
    failureId: failure ? "failure_scaled" : undefined,
    recoveryId:
      index >= 3_500 && index < 3_650 ? "recovery_scaled" : undefined,
    correlationId: `correlation_${Math.floor(index / 50)}`,
    causationId: index ? `event_${index - 1}` : undefined,
    depth: index,
    critical: control || failure || requirement,
    terminal: failure,
    effective: !heartbeat && !replay,
    partial: false,
    duplicateCount: 0,
    late: index % 701 === 0,
    evidence: Object.freeze(
      control
        ? [
            Object.freeze({
              kind: "command" as const,
              id: "command_reassign_scaled",
              label: "/reassign",
              eventIds: Object.freeze([`event_${index}`]),
              missing: false,
              terminal: true,
              status: "applied",
              metadata: Object.freeze({ request_id: "request_scaled" }),
            }),
          ]
        : [],
    ),
    drilldowns: Object.freeze([]),
    artifactIds: Object.freeze([]),
    mutationIds: Object.freeze(requirement ? ["mutation_goal_scaled"] : []),
    incomingEdgeIds: Object.freeze(index ? [`edge_${index - 1}_${index}`] : []),
    outgoingEdgeIds: Object.freeze(
      index < 4_999 ? [`edge_${index}_${index + 1}`] : [],
    ),
    tags: Object.freeze(
      control
        ? ["timeline-control", "worker-replacement"]
        : requirement
          ? ["requirement-change", "goal-update"]
          : [],
    ),
  })
}

function projection(count = 5_000): WorkerCausalTimelineProjection {
  const rows = Object.freeze(
    Array.from({ length: count }, (_, index) => row(index)),
  )
  const rowsByKey = Object.fromEntries(rows.map((item) => [item.key, item]))
  const rowKeyByEvent = Object.fromEntries(
    rows.map((item) => [item.primaryEventId, item.key]),
  )
  const edges = Object.freeze(
    rows.slice(1).map((item, index) =>
      Object.freeze({
        id: `edge_${index}_${index + 1}`,
        kind: "causation" as const,
        sourceEventId: `event_${index}`,
        targetEventId: item.primaryEventId,
        sourceRowKey: `row_${index}`,
        targetRowKey: item.key,
        explicit: true,
        missingSource: false,
        missingTarget: false,
        weight: 1,
        label: "causes",
        metadata: Object.freeze({}),
      }),
    ),
  )
  return {
    taskId: TASK,
    runIds: Object.freeze([RUN]),
    projectionRevision: count,
    committedAtMs: START + count * 10,
    rows,
    events: Object.freeze([]),
    graph: {
      eventIds: Object.freeze(rows.map((item) => item.primaryEventId)),
      edges,
      incomingByEvent: Object.freeze({}),
      outgoingByEvent: Object.freeze({}),
      ancestorsByEvent: Object.freeze({}),
      descendantsByEvent: Object.freeze({}),
      depthByEvent: Object.freeze({}),
      components: Object.freeze([]),
      roots: Object.freeze(["event_0"]),
      leaves: Object.freeze([`event_${count - 1}`]),
      missingEventIds: Object.freeze([]),
      cycleEventIds: Object.freeze([]),
    },
    workerEpochs: Object.freeze([]),
    recoveryChains: Object.freeze([]),
    backgroundLifecycles: Object.freeze([]),
    browserSteps: Object.freeze([]),
    criticalPath: {
      eventIds: Object.freeze(["event_1500", "event_2500", "event_3500"]),
      rowKeys: Object.freeze(["row_1500", "row_2500", "row_3500"]),
      workerIds: Object.freeze(["worker_0", "worker_4", "worker_8"]),
      totalWeight: 3,
      startedAt: rows[1500]?.occurredAt,
      endedAt: rows[3500]?.committedAt,
      durationMs: 20_000,
      complete: true,
      missingEventIds: Object.freeze([]),
      alternatives: Object.freeze([]),
    },
    rowsByKey: Object.freeze(rowsByKey),
    rowKeyByEvent: Object.freeze(rowKeyByEvent),
    rowKeysByWorker: Object.freeze({}),
    rowKeysByPhase: Object.freeze({}),
    rowKeysByKind: Object.freeze({}),
    diagnostics: {
      ready: true,
      disabled: false,
      revision: count,
      committedSequence: count,
      highWatermark: count,
      lag: 0,
      eventCount: count,
      effectiveEventCount: rows.filter((item) => item.effective).length,
      duplicateEventCount: 0,
      staleEventCount: 0,
      lateEventCount: rows.filter((item) => item.late).length,
      partialEventCount: 0,
      missingCauseCount: 0,
      missingEvidenceCount: 0,
      cycleCount: 0,
      workerEpochCount: 10,
      recoveryChainCount: 1,
      duplicateRecoveryCount: 0,
      warnings: Object.freeze([]),
    },
  }
}

describe("scaled recovery timeline", () => {
  test("virtualizes five thousand measured rows with bounded mounted nodes and far selection", () => {
    const source = projection()
    const virtualizer = new TimelineVirtualizer({
      estimatedRowHeight: 90,
      overscanPixels: 600,
      maximumRenderedRows: 120,
    })
    virtualizer.setRows(source.rows)
    virtualizer.measure("row_0", 180)
    virtualizer.measure("row_2500", 240)
    const range = virtualizer.range(220_000, 700, {
      pinRowKeys: ["row_0"],
      anchorKey: "row_2500",
    })
    expect(range.totalHeight).toBeGreaterThan(400_000)
    expect(range.rows.length).toBeLessThanOrEqual(122)
    expect(range.rows.some((item) => item.key === "row_0")).toBe(true)
    expect(range.rows.some((item) => item.key === "row_2500")).toBe(true)
    const anchor = virtualizer.captureAnchor(220_000)
    virtualizer.setRows(source.rows.slice(5))
    expect(virtualizer.restoreAnchor(anchor)).toBeGreaterThan(0)
  })

  test("causal folds hide repetition but preserve failures, controls and boundary anchors", () => {
    const source = projection()
    const folded = foldTimelineProjection(source, source.rows, {
      minimumCausalFoldRows: 8,
      minimumTimeFoldRows: 12,
      maximumFoldRows: 240,
    })
    expect(folded.folds.length).toBeGreaterThan(5)
    expect(folded.hiddenRowCount).toBeGreaterThan(100)
    expect(folded.visibleRows.some((item) => item.key === "row_1500")).toBe(
      true,
    )
    expect(folded.visibleRows.some((item) => item.key === "row_3500")).toBe(
      true,
    )
    for (const fold of folded.folds.filter((item) => item.safeToCollapse)) {
      expect(fold.rowKeys[0]).toBeDefined()
      expect(fold.rowKeys.at(-1)).toBeDefined()
      expect(fold.containsFailure).toBe(false)
      expect(fold.containsControl).toBe(false)
    }
  })

  test("incremental search finds worker, control, requirement and failure evidence", () => {
    const source = projection()
    const index = new TimelineSearchIndex()
    const first = index.rebuild(source.rows)
    expect(first.added).toBe(5_000)
    expect(first.removed).toBe(0)
    const repeated = index.rebuild(source.rows)
    expect(repeated.unchanged).toBe(5_000)
    expect(index.search({ text: "/reassign", limit: 20 }).matches[0]?.rowKey).toBe(
      "row_1500",
    )
    expect(
      index.search({ text: "require verifier", limit: 20 }).matches[0]?.rowKey,
    ).toBe("row_2500")
    expect(
      index.search({
        text: "heartbeat timeout",
        criticalOnly: true,
        limit: 20,
      }).matches[0]?.rowKey,
    ).toBe("row_3500")
    expect(
      index.search({
        text: "worker_7",
        workerIds: ["worker_7"],
        limit: 5,
      }).matches.every((match) => source.rows[match.index]?.workerId === "worker_7"),
    ).toBe(true)
  })

  test("goal drift and overlays distinguish effective transitions from heartbeat, replay and no-op", () => {
    const source = projection()
    const drift = projectTimelineGoalDrift(source)
    expect(drift.epochs.some((epoch) => epoch.kind === "requirement-change")).toBe(
      true,
    )
    expect(drift.driftRowKeys.has("row_2500")).toBe(true)
    const overlays = projectTimelineOverlays(source, drift)
    expect(overlays.byRowKey.get("row_1500")?.kinds).toContain("control")
    expect(overlays.byRowKey.get("row_1500")?.kinds).toContain(
      "worker-replacement",
    )
    expect(overlays.byRowKey.get("row_2500")?.kinds).toContain("goal-drift")
    expect(overlays.byRowKey.get("row_3500")?.kinds).toContain("fault")
    expect(overlays.byRowKey.get("row_97")?.effectiveStep).toBe(false)
    expect(overlays.byRowKey.get("row_211")?.effectiveStep).toBe(false)
    expect(overlays.excludedNoopCount).toBeGreaterThan(50)
    expect(overlays.effectiveStepCount).toBeLessThan(source.rows.length)
  })

  test("composed runtime survives rapid scroll, search, fold and projection updates", () => {
    const source = projection()
    const runtime = new ScaledTimelineRuntime({
      viewportHeight: 720,
      virtualizer: { maximumRenderedRows: 140 },
    })
    let scaled = runtime.setProjection(source)
    expect(scaled.thousandsMode).toBe(true)
    expect(scaled.renderedRows).toBeLessThanOrEqual(141)
    for (let index = 0; index < 40; index += 1) {
      scaled =
        runtime.setViewport(index * 8_000, 720) ??
        scaled
      if (index % 5 === 0) {
        scaled =
          runtime.setSearch({
            text: index % 10 ? "worker execution" : "require verifier",
            limit: 5_000,
          }) ?? scaled
      }
    }
    expect(scaled.search?.totalMatches).toBeGreaterThan(0)
    const fold = scaled.folds.folds.find((item) => item.safeToCollapse)
    if (fold) {
      const expanded = runtime.toggleFold(fold.id)
      expect(expanded?.folds.folds.find((item) => item.id === fold.id)?.expanded).toBe(
        true,
      )
    }
    runtime.setSearch(undefined)
    const selected = runtime.selectRow("row_3500")
    expect(selected?.selectedRow?.failureId).toBe("failure_scaled")
    expect(selected?.virtual.rows.some((item) => item.key === "row_3500")).toBe(
      true,
    )
    runtime.close()
    expect(() => runtime.setProjection(source)).toThrow()
  })
})
