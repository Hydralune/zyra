import type { CanonicalProjectionState } from "../../../state/contracts.ts"
import { projectionSelector } from "../../../state/selectors.ts"
import { buildBackgroundLifecycles } from "./background-lifecycle.ts"
import { buildBrowserSteps } from "./browser-step-adapter.ts"
import {
  attachRowKeysToEdges,
  buildTimelineCausalGraph,
} from "./causal-graph.ts"
import {
  TimelineProjectionError,
  type TimelineDiagnostics,
  type TimelineProjectionEngineAudit,
  type TimelineProjectionEngineOptions,
  type WorkerCausalTimelineOptions,
  type WorkerCausalTimelineProjection,
  type WorkerCausalTimelineSelector,
} from "./contracts.ts"
import { buildTimelineCriticalPath } from "./critical-path.ts"
import {
  buildTimelineRows,
  markCriticalRows,
} from "./event-reconciliation.ts"
import { readTaskTimelineEvents } from "./event-reader.ts"
import { buildTimelineRecoveryChains } from "./recovery-chain.ts"
import { buildTimelineWorkerEpochs } from "./worker-epochs.ts"

const DEFAULT_OPTIONS: Required<WorkerCausalTimelineOptions> = Object.freeze({
  disabled: false,
  includeNonEffective: true,
  includePartial: true,
  maximumEvents: 100_000,
  maximumClosureDepth: 256,
  maximumAlternativePaths: 4,
  coalesceWindowMs: 60_000,
})

function boundedInteger(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  if (!Number.isSafeInteger(value)) return fallback
  return Math.min(maximum, Math.max(minimum, Number(value)))
}

export function normalizeWorkerTimelineOptions(
  options: WorkerCausalTimelineOptions = {},
): Required<WorkerCausalTimelineOptions> {
  return Object.freeze({
    disabled: options.disabled === true,
    includeNonEffective: options.includeNonEffective !== false,
    includePartial: options.includePartial !== false,
    maximumEvents: boundedInteger(
      options.maximumEvents,
      DEFAULT_OPTIONS.maximumEvents,
      1,
      1_000_000,
    ),
    maximumClosureDepth: boundedInteger(
      options.maximumClosureDepth,
      DEFAULT_OPTIONS.maximumClosureDepth,
      1,
      4_096,
    ),
    maximumAlternativePaths: boundedInteger(
      options.maximumAlternativePaths,
      DEFAULT_OPTIONS.maximumAlternativePaths,
      1,
      32,
    ),
    coalesceWindowMs: boundedInteger(
      options.coalesceWindowMs,
      DEFAULT_OPTIONS.coalesceWindowMs,
      0,
      24 * 60 * 60 * 1000,
    ),
  })
}

function optionKey(options: Required<WorkerCausalTimelineOptions>): string {
  return JSON.stringify(options)
}

function diagnostics(
  state: CanonicalProjectionState,
  taskId: string,
  input: {
    eventCount: number
    effectiveEventCount: number
    duplicateRowEventCount: number
    lateEventCount: number
    partialEventCount: number
    missingCauseCount: number
    missingEvidenceCount: number
    cycleCount: number
    workerEpochCount: number
    recoveryChainCount: number
    duplicateRecoveryCount: number
  },
): TimelineDiagnostics {
  const cursor = state.cursors[taskId]
  const runtime = state.runtimes[taskId]
  const committedSequence =
    cursor?.committedSequence ?? runtime?.lastSequence ?? 0
  const highWatermark = cursor?.highWatermark ?? committedSequence
  const lag = Math.max(0, highWatermark - committedSequence)
  const warnings: string[] = []
  if (!cursor?.snapshotComplete) warnings.push("Canonical snapshot is partial.")
  if (lag > 0) warnings.push(`${lag} canonical event(s) remain behind the high watermark.`)
  if (input.missingCauseCount) {
    warnings.push(`${input.missingCauseCount} causal source(s) are outside the retained projection.`)
  }
  if (input.missingEvidenceCount) {
    warnings.push(`${input.missingEvidenceCount} evidence reference(s) are unresolved.`)
  }
  if (input.cycleCount) {
    warnings.push(`${input.cycleCount} event(s) participate in a causal cycle.`)
  }
  if (input.duplicateRecoveryCount) {
    warnings.push(`${input.duplicateRecoveryCount} duplicate recovery terminal event(s) were collapsed.`)
  }
  return Object.freeze({
    ready: Boolean(
      state.tasks[taskId] &&
        (cursor?.snapshotComplete ?? true) &&
        lag === 0,
    ),
    disabled: false,
    revision: state.revision,
    committedSequence,
    highWatermark,
    lag,
    eventCount: input.eventCount,
    effectiveEventCount: input.effectiveEventCount,
    duplicateEventCount:
      (runtime?.duplicateCount ?? 0) + input.duplicateRowEventCount,
    staleEventCount: runtime?.staleCount ?? 0,
    lateEventCount: input.lateEventCount,
    partialEventCount: input.partialEventCount,
    missingCauseCount: input.missingCauseCount,
    missingEvidenceCount: input.missingEvidenceCount,
    cycleCount: input.cycleCount,
    workerEpochCount: input.workerEpochCount,
    recoveryChainCount: input.recoveryChainCount,
    duplicateRecoveryCount: input.duplicateRecoveryCount,
    warnings: Object.freeze(warnings),
  })
}

function emptyProjection(
  state: CanonicalProjectionState,
  taskId: string,
): WorkerCausalTimelineProjection {
  const emptyIndex = Object.freeze({})
  const empty = Object.freeze([]) as readonly never[]
  const cursor = state.cursors[taskId]
  return Object.freeze({
    taskId,
    runIds: empty,
    projectionRevision: state.revision,
    committedAtMs: state.committedAtMs,
    rows: empty,
    events: empty,
    graph: Object.freeze({
      eventIds: empty,
      edges: empty,
      incomingByEvent: emptyIndex,
      outgoingByEvent: emptyIndex,
      ancestorsByEvent: emptyIndex,
      descendantsByEvent: emptyIndex,
      depthByEvent: emptyIndex,
      components: empty,
      roots: empty,
      leaves: empty,
      missingEventIds: empty,
      cycleEventIds: empty,
    }),
    workerEpochs: empty,
    recoveryChains: empty,
    backgroundLifecycles: empty,
    browserSteps: empty,
    criticalPath: Object.freeze({
      eventIds: empty,
      rowKeys: empty,
      workerIds: empty,
      totalWeight: 0,
      durationMs: 0,
      complete: false,
      missingEventIds: empty,
      alternatives: empty,
    }),
    rowsByKey: emptyIndex,
    rowKeyByEvent: emptyIndex,
    rowKeysByWorker: emptyIndex,
    rowKeysByPhase: emptyIndex,
    rowKeysByKind: emptyIndex,
    diagnostics: Object.freeze({
      ready: Boolean(state.tasks[taskId] && cursor?.snapshotComplete),
      disabled: false,
      revision: state.revision,
      committedSequence: cursor?.committedSequence ?? 0,
      highWatermark: cursor?.highWatermark ?? 0,
      lag: Math.max(
        0,
        (cursor?.highWatermark ?? 0) - (cursor?.committedSequence ?? 0),
      ),
      eventCount: 0,
      effectiveEventCount: 0,
      duplicateEventCount: state.runtimes[taskId]?.duplicateCount ?? 0,
      staleEventCount: state.runtimes[taskId]?.staleCount ?? 0,
      lateEventCount: 0,
      partialEventCount: 0,
      missingCauseCount: 0,
      missingEvidenceCount: 0,
      cycleCount: 0,
      workerEpochCount: 0,
      recoveryChainCount: 0,
      duplicateRecoveryCount: 0,
      warnings: Object.freeze(
        state.tasks[taskId]
          ? []
          : ["Task is not present in the canonical projection."],
      ),
    }),
  }) as WorkerCausalTimelineProjection
}

export function buildWorkerCausalTimeline(
  state: CanonicalProjectionState,
  taskId: string,
  inputOptions: WorkerCausalTimelineOptions = {},
): WorkerCausalTimelineProjection {
  const options = normalizeWorkerTimelineOptions(inputOptions)
  if (options.disabled) {
    throw new TimelineProjectionError(
      "timeline_projection_disabled",
      "Worker causal timeline projection is disabled.",
      { taskId, revision: state.revision },
    )
  }
  const events = readTaskTimelineEvents(state, taskId, {
    includeNonEffective: options.includeNonEffective,
    includePartial: options.includePartial,
    maximumEvents: options.maximumEvents,
  })
  if (!events.length) return emptyProjection(state, taskId)
  const graph = buildTimelineCausalGraph(
    state,
    events,
    options.maximumClosureDepth,
  )
  const epochs = buildTimelineWorkerEpochs(events, graph)
  const recoveries = buildTimelineRecoveryChains(events, epochs.epochs)
  const backgrounds = buildBackgroundLifecycles(events)
  const browser = buildBrowserSteps(events)
  const reconciled = buildTimelineRows({
    state,
    events,
    graph,
    workerEpochs: epochs.epochs,
    epochIdByEvent: epochs.epochIdByEvent,
    phaseByEvent: epochs.phaseByEvent,
    recoveryChains: recoveries.chains,
    recoveryAttemptByEvent: recoveries.attemptByEvent,
    backgroundLifecycles: backgrounds.lifecycles,
    backgroundLifecycleIdByEvent: backgrounds.lifecycleIdByEvent,
    browserSteps: browser.steps,
    browserStepIdByEvent: browser.stepIdByEvent,
    coalesceWindowMs: options.coalesceWindowMs,
  })
  const criticalPath = buildTimelineCriticalPath(
    graph,
    events,
    reconciled.rows,
    reconciled.rowKeyByEvent,
    options.maximumAlternativePaths,
  )
  const marked = markCriticalRows(
    reconciled,
    new Set(criticalPath.eventIds),
  )
  const graphWithRows = attachRowKeysToEdges(graph, marked.rowKeyByEvent)
  const missingEvidenceCount = events.reduce(
    (total, facts) => total + facts.missingRefs.length,
    0,
  )
  const runIds = Object.freeze([
    ...new Set(events.map((facts) => facts.event.runId)),
  ])
  return Object.freeze({
    taskId,
    runIds,
    projectionRevision: state.revision,
    committedAtMs: state.committedAtMs,
    rows: marked.rows,
    events,
    graph: graphWithRows,
    workerEpochs: epochs.epochs,
    recoveryChains: recoveries.chains,
    backgroundLifecycles: backgrounds.lifecycles,
    browserSteps: browser.steps,
    criticalPath,
    rowsByKey: marked.rowsByKey,
    rowKeyByEvent: marked.rowKeyByEvent,
    rowKeysByWorker: marked.rowKeysByWorker,
    rowKeysByPhase: marked.rowKeysByPhase,
    rowKeysByKind: marked.rowKeysByKind,
    diagnostics: diagnostics(state, taskId, {
      eventCount: events.length,
      effectiveEventCount: events.filter((facts) => facts.event.effective).length,
      duplicateRowEventCount: marked.duplicateRowEventCount,
      lateEventCount: marked.lateEventCount,
      partialEventCount: events.filter((facts) => facts.partial).length,
      missingCauseCount: graph.missingEventIds.length,
      missingEvidenceCount,
      cycleCount: graph.cycleEventIds.length,
      workerEpochCount: epochs.epochs.length,
      recoveryChainCount: recoveries.chains.length,
      duplicateRecoveryCount: recoveries.duplicateRecoveryCount,
    }),
  })
}

function sameTimelineProjection(
  left: WorkerCausalTimelineProjection,
  right: WorkerCausalTimelineProjection,
): boolean {
  if (left === right) return true
  if (
    left.taskId !== right.taskId ||
    left.projectionRevision !== right.projectionRevision ||
    left.rows.length !== right.rows.length ||
    left.events.length !== right.events.length
  ) {
    return false
  }
  const leftLast = left.rows.at(-1)
  const rightLast = right.rows.at(-1)
  return (
    leftLast?.key === rightLast?.key &&
    leftLast?.endSequence === rightLast?.endSequence &&
    left.diagnostics.committedSequence ===
      right.diagnostics.committedSequence &&
    left.diagnostics.highWatermark === right.diagnostics.highWatermark
  )
}

export function selectWorkerCausalTimeline(
  taskId: string,
  options: WorkerCausalTimelineOptions = {},
): WorkerCausalTimelineSelector {
  const normalized = normalizeWorkerTimelineOptions(options)
  return projectionSelector(
    `worker-causal-timeline:${taskId}:${optionKey(normalized)}`,
    [
      `task:${taskId}`,
      `causal:task:${taskId}`,
      `cursor:${taskId}`,
      "domain:worker",
      "domain:tool",
      "domain:artifact",
      "domain:permission",
      "domain:scheduler",
      "domain:recovery",
      "domain:command",
      "domain:session",
    ],
    (state) => buildWorkerCausalTimeline(state, taskId, normalized),
    sameTimelineProjection,
  )
}

interface ProjectionCacheEntry {
  state: CanonicalProjectionState
  taskId: string
  optionKey: string
  value: WorkerCausalTimelineProjection
}

export class WorkerCausalTimelineProjectionEngine {
  readonly #options: Required<WorkerCausalTimelineOptions>
  readonly #now: () => number
  #closed = false
  #projectionCount = 0
  #lastTaskId?: string
  #lastRevision?: number
  #lastEventCount = 0
  #lastRowCount = 0
  #lastDurationMs = 0
  #lastError?: string
  #cache?: ProjectionCacheEntry

  constructor(options: TimelineProjectionEngineOptions = {}) {
    this.#options = normalizeWorkerTimelineOptions(options)
    this.#now = options.now ?? (() => Date.now())
  }

  project(
    state: CanonicalProjectionState,
    taskId: string,
    options: WorkerCausalTimelineOptions = {},
  ): WorkerCausalTimelineProjection {
    if (this.#closed) {
      throw new TimelineProjectionError(
        "timeline_projection_closed",
        "Worker causal timeline projection engine is closed.",
        { taskId },
      )
    }
    const normalized = normalizeWorkerTimelineOptions({
      ...this.#options,
      ...options,
      disabled: this.#options.disabled || options.disabled,
    })
    if (normalized.disabled) {
      this.#lastError = "Worker causal timeline projection is disabled."
      throw new TimelineProjectionError(
        "timeline_projection_disabled",
        this.#lastError,
        { taskId, revision: state.revision },
      )
    }
    const key = optionKey(normalized)
    if (
      this.#cache?.state === state &&
      this.#cache.taskId === taskId &&
      this.#cache.optionKey === key
    ) {
      return this.#cache.value
    }
    const started = this.#now()
    try {
      const value = buildWorkerCausalTimeline(state, taskId, normalized)
      this.#projectionCount += 1
      this.#lastTaskId = taskId
      this.#lastRevision = state.revision
      this.#lastEventCount = value.events.length
      this.#lastRowCount = value.rows.length
      this.#lastDurationMs = Math.max(0, this.#now() - started)
      this.#lastError = undefined
      this.#cache = { state, taskId, optionKey: key, value }
      return value
    } catch (error) {
      this.#lastError = error instanceof Error ? error.message : String(error)
      throw error
    }
  }

  invalidate(taskId?: string): void {
    if (!this.#cache) return
    if (!taskId || this.#cache.taskId === taskId) this.#cache = undefined
  }

  close(): void {
    this.#closed = true
    this.#cache = undefined
  }

  audit(): TimelineProjectionEngineAudit {
    return Object.freeze({
      closed: this.#closed,
      disabled: this.#options.disabled,
      projectionCount: this.#projectionCount,
      lastTaskId: this.#lastTaskId,
      lastRevision: this.#lastRevision,
      lastEventCount: this.#lastEventCount,
      lastRowCount: this.#lastRowCount,
      lastDurationMs: this.#lastDurationMs,
      lastError: this.#lastError,
    })
  }
}
