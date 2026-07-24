import type {
  TimelineCausalGraph,
  TimelineFilter,
  TimelineFilteredView,
  TimelineHiddenGap,
  TimelineRow,
} from "./contracts.ts"
import { TimelineEventKind, TimelinePhase } from "./contracts.ts"

interface NormalizedFilter {
  workerIds: ReadonlySet<string>
  phases: ReadonlySet<string>
  kinds: ReadonlySet<string>
  fromMs?: number
  toMs?: number
  search: string
  criticalOnly: boolean
  failuresOnly: boolean
  includeNonEffective: boolean
  includePartial: boolean
}

function time(value: string | number | undefined): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value
  if (typeof value === "string") {
    const parsed = Date.parse(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return undefined
}

function normalize(filter: TimelineFilter = {}): NormalizedFilter {
  return {
    workerIds: new Set(filter.workerIds ?? []),
    phases: new Set(filter.phases ?? []),
    kinds: new Set(filter.kinds ?? []),
    fromMs: time(filter.from),
    toMs: time(filter.to),
    search: filter.search?.trim().toLocaleLowerCase() ?? "",
    criticalOnly: filter.criticalOnly === true,
    failuresOnly: filter.failuresOnly === true,
    includeNonEffective: filter.includeNonEffective === true,
    includePartial: filter.includePartial !== false,
  }
}

function rowTime(row: TimelineRow): number {
  const committed = Date.parse(row.committedAt)
  if (Number.isFinite(committed)) return committed
  const occurred = Date.parse(row.occurredAt)
  return Number.isFinite(occurred) ? occurred : 0
}

function searchableText(row: TimelineRow): string {
  return [
    row.title,
    row.summary,
    row.workerId,
    row.workerEpochId,
    row.leaseId,
    row.nodeId,
    row.routeId,
    row.placementId,
    row.toolCallId,
    row.permissionId,
    row.failureId,
    row.recoveryId,
    row.backgroundJobId,
    row.browserStepId,
    row.spanId,
    row.correlationId,
    row.causationId,
    ...row.eventIds,
    ...row.artifactIds,
    ...row.mutationIds,
    ...row.tags,
    ...row.evidence.flatMap((item) => [item.kind, item.id, item.label, item.status]),
  ]
    .filter(Boolean)
    .join("\n")
    .toLocaleLowerCase()
}

function matches(row: TimelineRow, filter: NormalizedFilter): boolean {
  if (
    filter.workerIds.size &&
    (!row.workerId || !filter.workerIds.has(row.workerId))
  ) {
    return false
  }
  if (filter.phases.size && !filter.phases.has(row.phase)) return false
  if (filter.kinds.size && !filter.kinds.has(row.rowKind)) return false
  const timestamp = rowTime(row)
  if (filter.fromMs !== undefined && timestamp < filter.fromMs) return false
  if (filter.toMs !== undefined && timestamp > filter.toMs) return false
  if (filter.search && !searchableText(row).includes(filter.search)) return false
  if (filter.criticalOnly && !row.critical) return false
  if (
    filter.failuresOnly &&
    row.phase !== TimelinePhase.FAILED &&
    row.phase !== TimelinePhase.RECOVERING &&
    row.rowKind !== TimelineEventKind.FAILURE &&
    row.rowKind !== TimelineEventKind.RECOVERY &&
    !row.failureId &&
    !row.recoveryId
  ) {
    return false
  }
  if (!filter.includeNonEffective && !row.effective && !row.terminal) {
    return false
  }
  if (!filter.includePartial && row.partial) return false
  return true
}

function activeFilterCount(filter: NormalizedFilter): number {
  let count = 0
  if (filter.workerIds.size) count += 1
  if (filter.phases.size) count += 1
  if (filter.kinds.size) count += 1
  if (filter.fromMs !== undefined) count += 1
  if (filter.toMs !== undefined) count += 1
  if (filter.search) count += 1
  if (filter.criticalOnly) count += 1
  if (filter.failuresOnly) count += 1
  if (filter.includeNonEffective) count += 1
  if (!filter.includePartial) count += 1
  return count
}

function countValues(
  rows: readonly TimelineRow[],
  value: (row: TimelineRow) => string,
): Readonly<Record<string, number>> {
  const result: Record<string, number> = {}
  for (const row of rows) {
    const key = value(row)
    result[key] = (result[key] ?? 0) + 1
  }
  return Object.freeze(result)
}

function boundaryEventIds(
  hidden: readonly TimelineRow[],
  before: TimelineRow | undefined,
  after: TimelineRow | undefined,
  graph: TimelineCausalGraph,
): readonly string[] {
  const hiddenEvents = new Set(hidden.flatMap((row) => row.eventIds))
  const visibleBoundary = new Set<string>()
  for (const row of hidden) {
    for (const eventId of row.eventIds) {
      for (const ancestor of graph.ancestorsByEvent[eventId] ?? []) {
        if (!hiddenEvents.has(ancestor)) visibleBoundary.add(ancestor)
      }
      for (const descendant of graph.descendantsByEvent[eventId] ?? []) {
        if (!hiddenEvents.has(descendant)) visibleBoundary.add(descendant)
      }
    }
  }
  if (before) {
    for (const eventId of before.eventIds) visibleBoundary.add(eventId)
  }
  if (after) {
    for (const eventId of after.eventIds) visibleBoundary.add(eventId)
  }
  return Object.freeze([...visibleBoundary].sort())
}

function causalBridge(
  hidden: readonly TimelineRow[],
  before: TimelineRow | undefined,
  after: TimelineRow | undefined,
  graph: TimelineCausalGraph,
): boolean {
  if (!before || !after) return false
  const hiddenEvents = new Set(hidden.flatMap((row) => row.eventIds))
  for (const source of before.eventIds) {
    const descendants = new Set(graph.descendantsByEvent[source] ?? [])
    for (const target of after.eventIds) {
      if (!descendants.has(target)) continue
      if (
        [...hiddenEvents].some(
          (eventId) =>
            descendants.has(eventId) &&
            (graph.ancestorsByEvent[target] ?? []).includes(eventId),
        )
      ) {
        return true
      }
    }
  }
  return false
}

function hiddenGap(
  hidden: readonly TimelineRow[],
  before: TimelineRow | undefined,
  after: TimelineRow | undefined,
  graph: TimelineCausalGraph,
): TimelineHiddenGap {
  const first = hidden[0]!
  const last = hidden.at(-1)!
  const hiddenEventIds = [...new Set(hidden.flatMap((row) => row.eventIds))]
  return Object.freeze({
    id: `gap:${before?.key ?? "start"}:${after?.key ?? "end"}:${first.sequence}:${last.endSequence}`,
    beforeRowKey: before?.key,
    afterRowKey: after?.key,
    hiddenRowKeys: Object.freeze(hidden.map((row) => row.key)),
    hiddenEventIds: Object.freeze(hiddenEventIds),
    count: hidden.length,
    startSequence: first.sequence,
    endSequence: last.endSequence,
    phaseCounts: countValues(hidden, (row) => row.phase),
    kindCounts: countValues(hidden, (row) => row.rowKind),
    workerIds: Object.freeze(
      [
        ...new Set(
          hidden
            .map((row) => row.workerId)
            .filter((value): value is string => Boolean(value)),
        ),
      ].sort(),
    ),
    boundaryEventIds: boundaryEventIds(hidden, before, after, graph),
    containsCritical: hidden.some((row) => row.critical),
    containsFailure: hidden.some(
      (row) =>
        row.phase === TimelinePhase.FAILED ||
        row.rowKind === TimelineEventKind.FAILURE ||
        Boolean(row.failureId),
    ),
    containsRecovery: hidden.some(
      (row) =>
        row.phase === TimelinePhase.RECOVERING ||
        row.rowKind === TimelineEventKind.RECOVERY ||
        Boolean(row.recoveryId),
    ),
    causalBridge: causalBridge(hidden, before, after, graph),
  })
}

function gaps(
  rows: readonly TimelineRow[],
  visible: ReadonlySet<string>,
  graph: TimelineCausalGraph,
): readonly TimelineHiddenGap[] {
  const result: TimelineHiddenGap[] = []
  let pending: TimelineRow[] = []
  let before: TimelineRow | undefined
  for (const row of rows) {
    if (!visible.has(row.key)) {
      pending.push(row)
      continue
    }
    if (pending.length) {
      result.push(hiddenGap(pending, before, row, graph))
      pending = []
    }
    before = row
  }
  if (pending.length) result.push(hiddenGap(pending, before, undefined, graph))
  return Object.freeze(result)
}

function causalBoundaryRows(
  rows: readonly TimelineRow[],
  matched: ReadonlySet<string>,
  graph: TimelineCausalGraph,
): ReadonlySet<string> {
  const byEvent = new Map<string, TimelineRow>()
  for (const row of rows) {
    for (const eventId of row.eventIds) byEvent.set(eventId, row)
  }
  const retained = new Set<string>()
  for (const row of rows) {
    if (!matched.has(row.key)) continue
    for (const eventId of row.eventIds) {
      const ancestors = graph.ancestorsByEvent[eventId] ?? []
      const descendants = graph.descendantsByEvent[eventId] ?? []
      const nearestAncestor = [...ancestors]
        .map((id) => byEvent.get(id))
        .find((candidate) => candidate && !matched.has(candidate.key))
      const nearestDescendant = [...descendants]
        .map((id) => byEvent.get(id))
        .find((candidate) => candidate && !matched.has(candidate.key))
      if (nearestAncestor) retained.add(nearestAncestor.key)
      if (nearestDescendant) retained.add(nearestDescendant.key)
    }
  }
  return retained
}

function retainedBoundaryEventIds(
  rows: readonly TimelineRow[],
  retained: ReadonlySet<string>,
): readonly string[] {
  return Object.freeze(
    rows
      .filter((row) => retained.has(row.key))
      .flatMap((row) => row.eventIds),
  )
}

export function filterTimelineRows(
  rows: readonly TimelineRow[],
  graph: TimelineCausalGraph,
  filter: TimelineFilter = {},
): TimelineFilteredView {
  const normalized = normalize(filter)
  const matched = new Set(
    rows.filter((row) => matches(row, normalized)).map((row) => row.key),
  )
  const retained = causalBoundaryRows(rows, matched, graph)
  const visible = new Set([...matched, ...retained])
  const selected = rows.filter((row) => visible.has(row.key))
  const hidden = rows.filter((row) => !visible.has(row.key))
  return Object.freeze({
    rows: Object.freeze(selected),
    gaps: gaps(rows, visible, graph),
    hiddenRowCount: hidden.length,
    hiddenEventCount: new Set(hidden.flatMap((row) => row.eventIds)).size,
    matchedRowCount: matched.size,
    visibleEventIds: Object.freeze(selected.flatMap((row) => row.eventIds)),
    retainedBoundaryEventIds: retainedBoundaryEventIds(rows, retained),
    activeFilterCount: activeFilterCount(normalized),
  })
}

export function availableTimelineWorkers(
  rows: readonly TimelineRow[],
): readonly {
  workerId: string
  rowCount: number
  failureCount: number
  recoveryCount: number
  phases: readonly string[]
}[] {
  const byWorker = new Map<string, TimelineRow[]>()
  for (const row of rows) {
    if (!row.workerId) continue
    const values = byWorker.get(row.workerId) ?? []
    values.push(row)
    byWorker.set(row.workerId, values)
  }
  return Object.freeze(
    [...byWorker.entries()]
      .map(([workerId, values]) =>
        Object.freeze({
          workerId,
          rowCount: values.length,
          failureCount: values.filter(
            (row) =>
              row.rowKind === TimelineEventKind.FAILURE ||
              row.phase === TimelinePhase.FAILED,
          ).length,
          recoveryCount: values.filter(
            (row) =>
              row.rowKind === TimelineEventKind.RECOVERY ||
              row.phase === TimelinePhase.RECOVERING,
          ).length,
          phases: Object.freeze([...new Set(values.map((row) => row.phase))]),
        }),
      )
      .sort(
        (left, right) =>
          right.failureCount - left.failureCount ||
          right.rowCount - left.rowCount ||
          left.workerId.localeCompare(right.workerId),
      ),
  )
}

export function availableTimelineKinds(
  rows: readonly TimelineRow[],
): readonly {
  kind: string
  count: number
}[] {
  const counts = countValues(rows, (row) => row.rowKind)
  return Object.freeze(
    Object.entries(counts)
      .map(([kind, count]) => Object.freeze({ kind, count }))
      .sort(
        (left, right) =>
          right.count - left.count || left.kind.localeCompare(right.kind),
      ),
  )
}

export function availableTimelinePhases(
  rows: readonly TimelineRow[],
): readonly {
  phase: string
  count: number
}[] {
  const counts = countValues(rows, (row) => row.phase)
  return Object.freeze(
    Object.entries(counts)
      .map(([phase, count]) => Object.freeze({ phase, count }))
      .sort(
        (left, right) =>
          right.count - left.count || left.phase.localeCompare(right.phase),
      ),
  )
}
