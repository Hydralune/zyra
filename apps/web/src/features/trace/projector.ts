import type { CanonicalProjectionState } from "../../state/contracts.ts"
import { projectionSelector } from "../../state/selectors.ts"
import { buildTraceCriticalPath, markTraceCriticalPath } from "./analysis/critical-path.ts"
import {
  TRACE_PROJECTION_SCHEMA,
  TraceProjectionError,
  type CausalTraceProjection,
  type CausalTraceSelector,
  type TraceProjectionOptions,
} from "./contracts.ts"
import { buildTraceBase } from "./index/builder.ts"

const DEFAULT_OPTIONS: Required<TraceProjectionOptions> = Object.freeze({
  disabled: false,
  includeNonEffective: true,
  includePartial: true,
  maximumEvents: 150_000,
  maximumClosureDepth: 512,
  maximumAlternativePaths: 6,
  maximumAttributesPerEvent: 48,
  lateSequenceTolerance: 2,
})

function boundedInteger(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  if (!Number.isSafeInteger(value)) return fallback
  return Math.max(minimum, Math.min(maximum, Number(value)))
}

export function normalizeTraceProjectionOptions(
  options: TraceProjectionOptions = {},
): Required<TraceProjectionOptions> {
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
      8_192,
    ),
    maximumAlternativePaths: boundedInteger(
      options.maximumAlternativePaths,
      DEFAULT_OPTIONS.maximumAlternativePaths,
      0,
      32,
    ),
    maximumAttributesPerEvent: boundedInteger(
      options.maximumAttributesPerEvent,
      DEFAULT_OPTIONS.maximumAttributesPerEvent,
      0,
      256,
    ),
    lateSequenceTolerance: boundedInteger(
      options.lateSequenceTolerance,
      DEFAULT_OPTIONS.lateSequenceTolerance,
      0,
      10_000,
    ),
  })
}

function optionKey(options: Required<TraceProjectionOptions>): string {
  return [
    options.disabled ? 1 : 0,
    options.includeNonEffective ? 1 : 0,
    options.includePartial ? 1 : 0,
    options.maximumEvents,
    options.maximumClosureDepth,
    options.maximumAlternativePaths,
    options.maximumAttributesPerEvent,
    options.lateSequenceTolerance,
  ].join(":")
}

function emptyProjection(
  state: CanonicalProjectionState,
  taskId: string,
): CausalTraceProjection {
  const emptyArray = Object.freeze([]) as readonly never[]
  const emptyRecord = Object.freeze({})
  const cursor = state.cursors[taskId]
  const committedSequence = cursor?.committedSequence ?? 0
  const highWatermark = cursor?.highWatermark ?? committedSequence
  return Object.freeze({
    schema: TRACE_PROJECTION_SCHEMA,
    taskId,
    runIds: emptyArray,
    projectionRevision: state.revision,
    committedAtMs: state.committedAtMs,
    nodes: emptyArray,
    edges: emptyArray,
    nodesByKey: emptyRecord,
    edgesById: emptyRecord,
    admissionByEvent: emptyRecord,
    hierarchy: Object.freeze({
      rootKeys: emptyArray,
      childrenByKey: emptyRecord,
      parentByKey: emptyRecord,
      depthByKey: emptyRecord,
      ancestorsByKey: emptyRecord,
      descendantsByKey: emptyRecord,
    }),
    indexes: Object.freeze({
      nodeKeysByEvent: emptyRecord,
      nodeKeysBySpan: emptyRecord,
      nodeKeysByWorker: emptyRecord,
      nodeKeysByToolCall: emptyRecord,
      nodeKeysByArtifact: emptyRecord,
      nodeKeysByMutation: emptyRecord,
      nodeKeysByCheckpoint: emptyRecord,
      nodeKeysByFailure: emptyRecord,
      nodeKeysByRecovery: emptyRecord,
      nodeKeysBySemantic: emptyRecord,
      edgeIdsByEvent: emptyRecord,
      eventIdByMutation: emptyRecord,
      producerEventIdsByArtifact: emptyRecord,
    }),
    criticalPath: Object.freeze({
      nodeKeys: emptyArray,
      eventIds: emptyArray,
      edgeIds: emptyArray,
      workerIds: emptyArray,
      providerIds: emptyArray,
      totalWeight: 0,
      durationMs: 0,
      complete: false,
      cycleNodeKeys: emptyArray,
      missingNodeKeys: emptyArray,
      bottlenecks: emptyArray,
      alternatives: emptyArray,
    }),
    quarantine: emptyArray,
    diagnostics: Object.freeze({
      ready: Boolean(state.tasks[taskId] && (cursor?.snapshotComplete ?? true)),
      disabled: false,
      projectionRevision: state.revision,
      committedSequence,
      highWatermark,
      lag: Math.max(0, highWatermark - committedSequence),
      eventCount: 0,
      nodeCount: 0,
      edgeCount: 0,
      partialCount: 0,
      lateCount: 0,
      orphanCount: 0,
      quarantineCount: 0,
      missingSpanCount: 0,
      missingArtifactCount: 0,
      identityConflictCount: 0,
      cycleCount: 0,
      warnings: Object.freeze(
        state.tasks[taskId]
          ? []
          : ["Task is not present in the canonical projection."],
      ),
    }),
  }) as CausalTraceProjection
}

export function buildCausalTraceProjection(
  state: CanonicalProjectionState,
  taskId: string,
  inputOptions: TraceProjectionOptions = {},
): CausalTraceProjection {
  const options = normalizeTraceProjectionOptions(inputOptions)
  if (options.disabled) {
    throw new TraceProjectionError(
      "trace_projection_disabled",
      "Causal trace projection and typed joins are disabled.",
      { taskId, revision: state.revision },
    )
  }
  if (!state.causality.byTask[taskId]?.length) return emptyProjection(state, taskId)
  const base = buildTraceBase(state, taskId, options)
  const criticalPath = buildTraceCriticalPath(
    base.nodes,
    base.edges,
    options.maximumAlternativePaths,
  )
  const marked = markTraceCriticalPath(base.nodes, base.edges, criticalPath)
  const nodesByKey = Object.freeze(
    Object.fromEntries(marked.nodes.map((node) => [node.key, node])),
  )
  const edgesById = Object.freeze(
    Object.fromEntries(marked.edges.map((edge) => [edge.id, edge])),
  )
  const diagnostics = Object.freeze({
    ...base.diagnostics,
    cycleCount: criticalPath.cycleNodeKeys.length,
    warnings: Object.freeze([
      ...base.diagnostics.warnings,
      ...(criticalPath.cycleNodeKeys.length
        ? [`${criticalPath.cycleNodeKeys.length} event node(s) participate in causal cycles.`]
        : []),
      ...(criticalPath.missingNodeKeys.length
        ? [`${criticalPath.missingNodeKeys.length} critical-path endpoint(s) are outside the retained projection.`]
        : []),
    ]),
  })
  return Object.freeze({
    schema: TRACE_PROJECTION_SCHEMA,
    taskId,
    runIds: Object.freeze([...new Set(marked.nodes.map((node) => node.runId).filter(Boolean))]),
    projectionRevision: state.revision,
    committedAtMs: state.committedAtMs,
    nodes: marked.nodes,
    edges: marked.edges,
    nodesByKey,
    edgesById,
    admissionByEvent: base.admissionByEvent,
    hierarchy: base.hierarchy,
    indexes: base.indexes,
    criticalPath,
    quarantine: base.quarantine,
    diagnostics,
  })
}

function sameTraceProjection(
  left: CausalTraceProjection,
  right: CausalTraceProjection,
): boolean {
  if (left === right) return true
  if (
    left.taskId !== right.taskId ||
    left.projectionRevision !== right.projectionRevision ||
    left.nodes.length !== right.nodes.length ||
    left.edges.length !== right.edges.length ||
    left.diagnostics.committedSequence !== right.diagnostics.committedSequence ||
    left.diagnostics.highWatermark !== right.diagnostics.highWatermark
  ) {
    return false
  }
  const leftLast = left.nodes.at(-1)
  const rightLast = right.nodes.at(-1)
  return leftLast?.key === rightLast?.key && leftLast?.endSequence === rightLast?.endSequence
}

export function selectCausalTrace(
  taskId: string,
  options: TraceProjectionOptions = {},
): CausalTraceSelector {
  const normalized = normalizeTraceProjectionOptions(options)
  return projectionSelector(
    `causal-trace:${taskId}:${optionKey(normalized)}`,
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
    (state) => buildCausalTraceProjection(state, taskId, normalized),
    sameTraceProjection,
  )
}

export interface CausalTraceProjectionEngineAudit {
  closed: boolean
  disabled: boolean
  projectionCount: number
  cacheHitCount: number
  lastTaskId?: string
  lastRevision?: number
  lastEventCount: number
  lastNodeCount: number
  lastEdgeCount: number
  lastDurationMs: number
  lastError?: string
}

interface TraceProjectionCache {
  state: CanonicalProjectionState
  taskId: string
  optionKey: string
  projection: CausalTraceProjection
}

export class CausalTraceProjectionEngine {
  readonly #options: Required<TraceProjectionOptions>
  readonly #now: () => number
  #closed = false
  #projectionCount = 0
  #cacheHitCount = 0
  #lastTaskId?: string
  #lastRevision?: number
  #lastEventCount = 0
  #lastNodeCount = 0
  #lastEdgeCount = 0
  #lastDurationMs = 0
  #lastError?: string
  #cache?: TraceProjectionCache

  constructor(options: TraceProjectionOptions & { now?: () => number } = {}) {
    this.#options = normalizeTraceProjectionOptions(options)
    this.#now = options.now ?? (() => Date.now())
  }

  project(
    state: CanonicalProjectionState,
    taskId: string,
    options: TraceProjectionOptions = {},
  ): CausalTraceProjection {
    if (this.#closed) {
      throw new TraceProjectionError(
        "trace_projection_closed",
        "Causal trace projection engine is closed.",
        { taskId },
      )
    }
    const normalized = normalizeTraceProjectionOptions({
      ...this.#options,
      ...options,
      disabled: this.#options.disabled || options.disabled,
    })
    if (normalized.disabled) {
      this.#lastError = "Causal trace projection and typed joins are disabled."
      throw new TraceProjectionError(
        "trace_projection_disabled",
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
      this.#cacheHitCount += 1
      return this.#cache.projection
    }
    const started = this.#now()
    try {
      const projection = buildCausalTraceProjection(state, taskId, normalized)
      this.#projectionCount += 1
      this.#lastTaskId = taskId
      this.#lastRevision = state.revision
      this.#lastEventCount = projection.diagnostics.eventCount
      this.#lastNodeCount = projection.nodes.length
      this.#lastEdgeCount = projection.edges.length
      this.#lastDurationMs = Math.max(0, this.#now() - started)
      this.#lastError = undefined
      this.#cache = { state, taskId, optionKey: key, projection }
      return projection
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

  audit(): CausalTraceProjectionEngineAudit {
    return Object.freeze({
      closed: this.#closed,
      disabled: this.#options.disabled,
      projectionCount: this.#projectionCount,
      cacheHitCount: this.#cacheHitCount,
      lastTaskId: this.#lastTaskId,
      lastRevision: this.#lastRevision,
      lastEventCount: this.#lastEventCount,
      lastNodeCount: this.#lastNodeCount,
      lastEdgeCount: this.#lastEdgeCount,
      lastDurationMs: this.#lastDurationMs,
      lastError: this.#lastError,
    })
  }
}
