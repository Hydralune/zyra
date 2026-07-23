import type {
  BranchView,
  CheckpointView,
  RequirementChangeView,
  RouteDecisionView,
  TopologyEdgeView,
  TopologyEntityBase,
  TopologyNodeView,
  TopologyProjectionContext,
  TopologyProjectionDiagnostics,
} from "./contracts.ts"
import { uniqueStrings } from "./record-reader.ts"

export interface DiagnosticInputs {
  nodes: readonly TopologyNodeView[]
  edges: readonly TopologyEdgeView[]
  routes: readonly RouteDecisionView[]
  checkpoints: readonly CheckpointView[]
  branches: readonly BranchView[]
  changes: readonly RequirementChangeView[]
}

export function buildProjectionDiagnostics(
  context: TopologyProjectionContext,
  inputs: DiagnosticInputs,
): TopologyProjectionDiagnostics {
  const { state, taskId, options } = context
  const cursor = state.cursors[taskId]
  const runtime = state.runtimes[taskId]
  const committedSequence = cursor?.committedSequence ?? runtime?.lastSequence ?? 0
  const highWatermark = cursor?.highWatermark ?? committedSequence
  const lag = Math.max(0, highWatermark - committedSequence)
  const missingSequences = findMissingSequences(
    context.records.map((record) => record.sequence),
    runtime?.firstSequence ?? 0,
    committedSequence,
  )
  const rejected = rejectedEntityIds(inputs)
  const missingEvidence = missingEvidenceEntityIds(inputs)
  const ambiguous = ambiguousEntityIds(inputs)
  const warnings: string[] = []
  const errors: string[] = []
  if (!cursor) warnings.push("cursor_missing")
  if (!runtime) warnings.push("runtime_missing")
  if (lag > 0) warnings.push(`projection_lag:${lag}`)
  if (missingSequences.length > 0) {
    warnings.push(`missing_sequences:${missingSequences.length}`)
  }
  if (runtime && runtime.duplicateCount > 0) {
    warnings.push(`duplicate_events:${runtime.duplicateCount}`)
  }
  if (runtime && runtime.staleCount > 0) {
    warnings.push(`stale_events:${runtime.staleCount}`)
  }
  if (missingEvidence.length > 0) {
    warnings.push(`missing_evidence:${missingEvidence.length}`)
  }
  if (ambiguous.length > 0) {
    warnings.push(`ambiguous_entities:${ambiguous.length}`)
  }
  const missingEndpoints = inputs.edges.filter(
    (edge) => edge.sourceMissing || edge.targetMissing,
  )
  if (missingEndpoints.length > 0) {
    warnings.push(`missing_edge_endpoints:${missingEndpoints.length}`)
  }
  const invalidLineage = inputs.checkpoints.filter(
    (checkpoint) => !checkpoint.lineageValid,
  )
  if (invalidLineage.length > 0) {
    errors.push(`invalid_checkpoint_lineage:${invalidLineage.length}`)
  }
  const invalidVisibility = inputs.checkpoints.filter(
    (checkpoint) => !checkpoint.visibilityValid,
  )
  if (invalidVisibility.length > 0) {
    errors.push(`invalid_write_visibility:${invalidVisibility.length}`)
  }
  const hiddenCommittedBranches = inputs.branches.filter(
    (branch) =>
      ["committed", "rebased", "replayed"].includes(branch.outcome) &&
      !branch.visibleInCanonicalState,
  )
  if (hiddenCommittedBranches.length > 0) {
    errors.push(`branch_commit_order_conflict:${hiddenCommittedBranches.length}`)
  }
  if (options.strict) {
    if (missingSequences.length > 0) errors.push("strict_sequence_gap")
    if (missingEvidence.length > 0) errors.push("strict_missing_evidence")
    if (ambiguous.length > 0) errors.push("strict_ambiguous_entity")
  }
  const snapshotComplete = cursor?.snapshotComplete ?? false
  const connected = runtime?.connected ?? false
  return Object.freeze({
    ready:
      !options.disabled &&
      snapshotComplete &&
      lag === 0 &&
      errors.length === 0,
    disabled: options.disabled,
    snapshotComplete,
    connected,
    projectionRevision: state.revision,
    cursorGeneration: cursor?.generation ?? runtime?.generation ?? 0,
    committedSequence,
    highWatermark,
    lag,
    duplicateEvents: runtime?.duplicateCount ?? state.diagnostics.duplicateEvents,
    staleEvents: runtime?.staleCount ?? state.diagnostics.staleEvents,
    missingSequences: Object.freeze(missingSequences),
    warnings: Object.freeze(uniqueStrings(warnings)),
    errors: Object.freeze(uniqueStrings(errors)),
    rejectedEntityIds: Object.freeze(rejected),
    missingEvidenceEntityIds: Object.freeze(missingEvidence),
    ambiguousEntityIds: Object.freeze(ambiguous),
    sensitiveFieldDrops: context.sensitiveFieldDrops.value,
  })
}

function findMissingSequences(
  sequences: readonly number[],
  firstSequence: number,
  committedSequence: number,
): number[] {
  if (committedSequence <= 0 || sequences.length === 0) return []
  const seen = new Set(sequences.filter((sequence) => sequence > 0))
  const start = Math.max(
    firstSequence > 0 ? firstSequence : Math.min(...seen),
    committedSequence - 10_000,
  )
  const output: number[] = []
  for (let sequence = start; sequence <= committedSequence; sequence += 1) {
    if (!seen.has(sequence)) output.push(sequence)
    if (output.length >= 1_000) break
  }
  return output
}

function allEntities(inputs: DiagnosticInputs): TopologyEntityBase[] {
  return [
    ...inputs.nodes,
    ...inputs.edges,
    ...inputs.routes,
    ...inputs.checkpoints,
    ...inputs.branches,
    ...inputs.changes,
  ]
}

function rejectedEntityIds(inputs: DiagnosticInputs): string[] {
  return uniqueStrings(
    allEntities(inputs)
      .filter(
        (entity) =>
          entity.state === "rejected" ||
          entity.state === "conflicted" ||
          !entity.effective,
      )
      .map((entity) => `${entity.kind}:${entity.id}`),
  )
}

function missingEvidenceEntityIds(inputs: DiagnosticInputs): string[] {
  return uniqueStrings(
    allEntities(inputs)
      .filter(
        (entity) =>
          entity.evidence.eventIds.length === 0 ||
          entity.evidence.lastSequence === undefined,
      )
      .map((entity) => `${entity.kind}:${entity.id}`),
  )
}

function ambiguousEntityIds(inputs: DiagnosticInputs): string[] {
  const output: string[] = []
  const nodesById = groupBy(inputs.nodes, (node) => node.id)
  const routesById = groupBy(inputs.routes, (route) => route.routeId)
  const branchesById = groupBy(inputs.branches, (branch) => branch.branchId)
  for (const [id, nodes] of nodesById) {
    if (
      new Set(nodes.map((node) => `${node.graphRevision}:${node.revision}`)).size <
        nodes.length ||
      new Set(nodes.map((node) => node.namespace)).size > 1
    ) {
      output.push(`node:${id}`)
    }
  }
  for (const [id, routes] of routesById) {
    if (
      new Set(routes.map((route) => route.selectedCandidateId ?? "none")).size >
      1
    ) {
      output.push(`route:${id}`)
    }
  }
  for (const [id, branches] of branchesById) {
    if (new Set(branches.map((branch) => branch.outcome)).size > 1) {
      output.push(`branch:${id}`)
    }
  }
  return uniqueStrings(output)
}

function groupBy<T>(
  values: readonly T[],
  keyOf: (value: T) => string,
): ReadonlyMap<string, readonly T[]> {
  const output = new Map<string, T[]>()
  for (const value of values) {
    const key = keyOf(value)
    const items = output.get(key) ?? []
    items.push(value)
    output.set(key, items)
  }
  return output
}
