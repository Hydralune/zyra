import {
  TimelineEventKind,
  type TimelineEdge,
  type TimelineRow,
  type WorkerCausalTimelineProjection,
} from "../projection/index.ts"
import type {
  TimelineFold,
  TimelineFoldKind,
  TimelineFoldOptions,
  TimelineFoldProjection,
} from "./contracts.ts"

interface FoldCandidate {
  kind: TimelineFoldKind
  start: number
  end: number
  reason: string
}

const DEFAULTS: Required<
  Omit<TimelineFoldOptions, "expandedFoldIds">
> = Object.freeze({
  timeBucketMs: 30_000,
  minimumTimeFoldRows: 8,
  minimumCausalFoldRows: 5,
  maximumFoldRows: 500,
  preserveCritical: true,
  preserveFailures: true,
  preserveControls: true,
})

function freeze<T>(values: Iterable<T>): readonly T[] {
  return Object.freeze([...values])
}

function unique(values: Iterable<string>): readonly string[] {
  return freeze([...new Set([...values].filter(Boolean))])
}

function time(value: string): number {
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : 0
}

function boundedInteger(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  const candidate = value ?? fallback
  if (!Number.isFinite(candidate)) return fallback
  return Math.max(minimum, Math.min(maximum, Math.floor(candidate)))
}

function options(
  value: TimelineFoldOptions,
): Required<Omit<TimelineFoldOptions, "expandedFoldIds">> & {
  expandedFoldIds: ReadonlySet<string>
} {
  return Object.freeze({
    timeBucketMs: boundedInteger(
      value.timeBucketMs,
      DEFAULTS.timeBucketMs,
      100,
      24 * 60 * 60 * 1000,
    ),
    minimumTimeFoldRows: boundedInteger(
      value.minimumTimeFoldRows,
      DEFAULTS.minimumTimeFoldRows,
      2,
      10_000,
    ),
    minimumCausalFoldRows: boundedInteger(
      value.minimumCausalFoldRows,
      DEFAULTS.minimumCausalFoldRows,
      2,
      10_000,
    ),
    maximumFoldRows: boundedInteger(
      value.maximumFoldRows,
      DEFAULTS.maximumFoldRows,
      2,
      100_000,
    ),
    preserveCritical:
      value.preserveCritical ?? DEFAULTS.preserveCritical,
    preserveFailures:
      value.preserveFailures ?? DEFAULTS.preserveFailures,
    preserveControls:
      value.preserveControls ?? DEFAULTS.preserveControls,
    expandedFoldIds: value.expandedFoldIds ?? new Set<string>(),
  })
}

function rowContainsControl(row: TimelineRow): boolean {
  return (
    row.rowKind === TimelineEventKind.COMMAND ||
    row.tags.some((tag) =>
      /control|command|permission|intervention|sealed/i.test(tag),
    ) ||
    row.evidence.some((evidence) =>
      ["command", "permission"].includes(evidence.kind),
    )
  )
}

function protectedRow(
  row: TimelineRow,
  config: ReturnType<typeof options>,
): boolean {
  if (config.preserveCritical && row.critical) return true
  if (
    config.preserveFailures &&
    (row.rowKind === TimelineEventKind.FAILURE ||
      row.phase === "failed")
  ) {
    return true
  }
  if (config.preserveControls && rowContainsControl(row)) return true
  return false
}

function repeatedSignature(row: TimelineRow): string {
  return [
    row.workerId ?? "",
    row.rowKind,
    row.phase,
    row.title.toLocaleLowerCase().replace(/\d+/g, "#"),
    row.summary
      .toLocaleLowerCase()
      .replace(/[a-f0-9]{8,}/g, "<id>")
      .replace(/\d+/g, "#")
      .slice(0, 120),
  ].join("|")
}

function repeatedCandidates(
  rows: readonly TimelineRow[],
  config: ReturnType<typeof options>,
): FoldCandidate[] {
  const result: FoldCandidate[] = []
  let start = 0
  while (start < rows.length) {
    if (protectedRow(rows[start], config)) {
      start += 1
      continue
    }
    const signature = repeatedSignature(rows[start])
    let end = start + 1
    while (
      end < rows.length &&
      end - start < config.maximumFoldRows &&
      !protectedRow(rows[end], config) &&
      repeatedSignature(rows[end]) === signature
    ) {
      end += 1
    }
    if (end - start >= config.minimumCausalFoldRows) {
      result.push({
        kind: "repeated",
        start,
        end: end - 1,
        reason: "repeated equivalent runtime facts",
      })
    }
    start = Math.max(start + 1, end)
  }
  return result
}

function recoveryCandidates(
  rows: readonly TimelineRow[],
  config: ReturnType<typeof options>,
): FoldCandidate[] {
  const result: FoldCandidate[] = []
  let start = 0
  while (start < rows.length) {
    const recoveryId = rows[start].recoveryId
    if (!recoveryId || protectedRow(rows[start], config)) {
      start += 1
      continue
    }
    let end = start + 1
    while (
      end < rows.length &&
      end - start < config.maximumFoldRows &&
      !protectedRow(rows[end], config) &&
      rows[end].recoveryId === recoveryId
    ) {
      end += 1
    }
    if (end - start >= config.minimumCausalFoldRows) {
      result.push({
        kind: "recovery",
        start,
        end: end - 1,
        reason: `long recovery chain ${recoveryId}`,
      })
    }
    start = Math.max(start + 1, end)
  }
  return result
}

function idleCandidates(
  rows: readonly TimelineRow[],
  config: ReturnType<typeof options>,
): FoldCandidate[] {
  const result: FoldCandidate[] = []
  let start = 0
  while (start < rows.length) {
    const workerId = rows[start].workerId
    const idle =
      !rows[start].effective &&
      !rows[start].terminal &&
      !rows[start].partial &&
      !protectedRow(rows[start], config)
    if (!workerId || !idle) {
      start += 1
      continue
    }
    let end = start + 1
    while (
      end < rows.length &&
      end - start < config.maximumFoldRows &&
      rows[end].workerId === workerId &&
      !rows[end].effective &&
      !rows[end].terminal &&
      !rows[end].partial &&
      !protectedRow(rows[end], config)
    ) {
      end += 1
    }
    if (end - start >= config.minimumTimeFoldRows) {
      result.push({
        kind: "worker-idle",
        start,
        end: end - 1,
        reason: `non-effective worker ${workerId} observations`,
      })
    }
    start = Math.max(start + 1, end)
  }
  return result
}

function timeCandidates(
  rows: readonly TimelineRow[],
  config: ReturnType<typeof options>,
): FoldCandidate[] {
  const result: FoldCandidate[] = []
  let start = 0
  while (start < rows.length) {
    if (protectedRow(rows[start], config)) {
      start += 1
      continue
    }
    const bucket = Math.floor(time(rows[start].committedAt) / config.timeBucketMs)
    let end = start + 1
    while (
      end < rows.length &&
      end - start < config.maximumFoldRows &&
      !protectedRow(rows[end], config) &&
      Math.floor(time(rows[end].committedAt) / config.timeBucketMs) ===
        bucket
    ) {
      end += 1
    }
    if (end - start >= config.minimumTimeFoldRows) {
      result.push({
        kind: "time",
        start,
        end: end - 1,
        reason: `${config.timeBucketMs}ms temporal bucket`,
      })
    }
    start = Math.max(start + 1, end)
  }
  return result
}

function eventToRowIndex(rows: readonly TimelineRow[]): Map<string, number> {
  const result = new Map<string, number>()
  rows.forEach((row, index) => {
    for (const eventId of row.eventIds) result.set(eventId, index)
  })
  return result
}

function causalCandidates(
  projection: WorkerCausalTimelineProjection,
  rows: readonly TimelineRow[],
  config: ReturnType<typeof options>,
): FoldCandidate[] {
  const byEvent = eventToRowIndex(rows)
  const incoming = new Map<number, number[]>()
  const outgoing = new Map<number, number[]>()
  for (const edge of projection.graph.edges) {
    const source = byEvent.get(edge.sourceEventId)
    const target = byEvent.get(edge.targetEventId)
    if (
      source === undefined ||
      target === undefined ||
      source === target
    ) {
      continue
    }
    const out = outgoing.get(source) ?? []
    out.push(target)
    outgoing.set(source, out)
    const input = incoming.get(target) ?? []
    input.push(source)
    incoming.set(target, input)
  }
  const result: FoldCandidate[] = []
  let start = 0
  while (start < rows.length) {
    if (protectedRow(rows[start], config)) {
      start += 1
      continue
    }
    let end = start
    while (
      end + 1 < rows.length &&
      end - start + 1 < config.maximumFoldRows &&
      !protectedRow(rows[end + 1], config)
    ) {
      const successors = outgoing.get(end) ?? []
      const predecessors = incoming.get(end + 1) ?? []
      const connected =
        successors.includes(end + 1) ||
        predecessors.includes(end) ||
        rows[end].correlationId === rows[end + 1].correlationId ||
        (rows[end].workerId &&
          rows[end].workerId === rows[end + 1].workerId &&
          rows[end].endSequence + 1 >= rows[end + 1].sequence)
      if (!connected) break
      const externalIncoming = predecessors.some((index) => index < start)
      const externalOutgoing = successors.some((index) => index > end + 1)
      if (externalIncoming && end > start) break
      if (externalOutgoing && end > start) break
      end += 1
    }
    if (end - start + 1 >= config.minimumCausalFoldRows) {
      result.push({
        kind: "causal",
        start,
        end,
        reason: "linear causal segment with stable ownership",
      })
    }
    start = Math.max(start + 1, end + 1)
  }
  return result
}

const KIND_PRIORITY: Readonly<Record<TimelineFoldKind, number>> =
  Object.freeze({
    recovery: 50,
    repeated: 40,
    causal: 30,
    "worker-idle": 20,
    time: 10,
  })

function selectCandidates(
  candidates: readonly FoldCandidate[],
  rowCount: number,
): readonly FoldCandidate[] {
  const occupied = new Uint8Array(rowCount)
  const selected: FoldCandidate[] = []
  const ordered = [...candidates].sort(
    (left, right) =>
      KIND_PRIORITY[right.kind] - KIND_PRIORITY[left.kind] ||
      right.end - right.start - (left.end - left.start) ||
      left.start - right.start,
  )
  for (const candidate of ordered) {
    let overlap = false
    for (let index = candidate.start; index <= candidate.end; index += 1) {
      if (occupied[index]) {
        overlap = true
        break
      }
    }
    if (overlap) continue
    for (let index = candidate.start; index <= candidate.end; index += 1) {
      occupied[index] = 1
    }
    selected.push(candidate)
  }
  return freeze(selected.sort((a, b) => a.start - b.start))
}

function edgeBoundaries(
  projection: WorkerCausalTimelineProjection,
  eventIds: ReadonlySet<string>,
): {
  incoming: readonly string[]
  outgoing: readonly string[]
  boundary: readonly string[]
} {
  const incoming: string[] = []
  const outgoing: string[] = []
  for (const edge of projection.graph.edges) {
    const sourceInside = eventIds.has(edge.sourceEventId)
    const targetInside = eventIds.has(edge.targetEventId)
    if (!sourceInside && targetInside) incoming.push(edge.sourceEventId)
    if (sourceInside && !targetInside) outgoing.push(edge.targetEventId)
  }
  return {
    incoming: unique(incoming),
    outgoing: unique(outgoing),
    boundary: unique([...incoming, ...outgoing]),
  }
}

function foldId(
  kind: TimelineFoldKind,
  rows: readonly TimelineRow[],
): string {
  const first = rows[0]
  const last = rows.at(-1) as TimelineRow
  return `fold:${kind}:${first.key}:${last.key}:${rows.length}`
}

function buildFold(
  candidate: FoldCandidate,
  rows: readonly TimelineRow[],
  projection: WorkerCausalTimelineProjection,
  expanded: ReadonlySet<string>,
): TimelineFold {
  const segment = rows.slice(candidate.start, candidate.end + 1)
  const first = segment[0]
  const last = segment.at(-1) as TimelineRow
  const eventIds = unique(segment.flatMap((row) => row.eventIds))
  const boundaries = edgeBoundaries(projection, new Set(eventIds))
  const id = foldId(candidate.kind, segment)
  const containsCritical = segment.some((row) => row.critical)
  const containsFailure = segment.some(
    (row) =>
      row.rowKind === TimelineEventKind.FAILURE ||
      row.phase === "failed",
  )
  const containsRecovery = segment.some(
    (row) =>
      row.rowKind === TimelineEventKind.RECOVERY ||
      Boolean(row.recoveryId),
  )
  const hasControl = segment.some(rowContainsControl)
  return Object.freeze({
    id,
    kind: candidate.kind,
    label:
      candidate.kind === "repeated"
        ? `${segment.length} repeated events`
        : candidate.kind === "recovery"
          ? `${segment.length}-row recovery chain`
          : candidate.kind === "worker-idle"
            ? `${segment.length} worker observations`
            : candidate.kind === "causal"
              ? `${segment.length}-row causal segment`
              : `${segment.length} events in time bucket`,
    startIndex: candidate.start,
    endIndex: candidate.end,
    startSequence: first.sequence,
    endSequence: last.endSequence,
    startedAt: first.occurredAt,
    endedAt: last.committedAt,
    durationMs: Math.max(
      0,
      time(last.committedAt) - time(first.occurredAt),
    ),
    rowKeys: unique(segment.map((row) => row.key)),
    eventIds,
    workerIds: unique(
      segment
        .map((row) => row.workerId)
        .filter((value): value is string => Boolean(value)),
    ),
    phases: freeze([...new Set(segment.map((row) => row.phase))]),
    kinds: freeze([...new Set(segment.map((row) => row.rowKind))]),
    boundaryEventIds: boundaries.boundary,
    incomingEventIds: boundaries.incoming,
    outgoingEventIds: boundaries.outgoing,
    hiddenCount: Math.max(0, segment.length - 2),
    effectiveCount: segment.filter((row) => row.effective).length,
    containsCritical,
    containsFailure,
    containsRecovery,
    containsControl: hasControl,
    expanded: expanded.has(id),
    safeToCollapse:
      !containsCritical &&
      !containsFailure &&
      !hasControl &&
      boundaries.boundary.length <= 256,
    reason: candidate.reason,
  })
}

function visibleRows(
  rows: readonly TimelineRow[],
  folds: readonly TimelineFold[],
): {
  rows: readonly TimelineRow[]
  folded: ReadonlySet<string>
  byRow: ReadonlyMap<string, TimelineFold>
} {
  const hidden = new Set<string>()
  const byRow = new Map<string, TimelineFold>()
  for (const fold of folds) {
    for (const rowKey of fold.rowKeys) byRow.set(rowKey, fold)
    if (fold.expanded || !fold.safeToCollapse) continue
    // Keep first and last row visible as causal boundary anchors.
    for (const rowKey of fold.rowKeys.slice(1, -1)) hidden.add(rowKey)
  }
  return {
    rows: freeze(rows.filter((row) => !hidden.has(row.key))),
    folded: hidden,
    byRow,
  }
}

export function foldTimelineProjection(
  projection: WorkerCausalTimelineProjection,
  rows: readonly TimelineRow[] = projection.rows,
  input: TimelineFoldOptions = {},
): TimelineFoldProjection {
  const config = options(input)
  const candidates = selectCandidates(
    [
      ...recoveryCandidates(rows, config),
      ...repeatedCandidates(rows, config),
      ...causalCandidates(projection, rows, config),
      ...idleCandidates(rows, config),
      ...timeCandidates(rows, config),
    ],
    rows.length,
  )
  const folds = freeze(
    candidates.map((candidate) =>
      buildFold(
        candidate,
        rows,
        projection,
        config.expandedFoldIds,
      ),
    ),
  )
  const visible = visibleRows(rows, folds)
  const effectiveRowCount = rows.filter((row) => row.effective).length
  return Object.freeze({
    folds,
    visibleRows: visible.rows,
    foldedRowKeys: visible.folded,
    foldByRowKey: visible.byRow,
    hiddenRowCount: visible.folded.size,
    effectiveRowCount,
    revision: [
      projection.projectionRevision,
      rows.length,
      folds.map((fold) => `${fold.id}:${fold.expanded ? 1 : 0}`).join(","),
    ].join("|"),
  })
}

export function toggleTimelineFold(
  current: ReadonlySet<string>,
  foldIdValue: string,
): ReadonlySet<string> {
  const next = new Set(current)
  if (next.has(foldIdValue)) next.delete(foldIdValue)
  else next.add(foldIdValue)
  return next
}

export function foldForTimelineRow(
  projection: TimelineFoldProjection,
  rowKey: string,
): TimelineFold | undefined {
  return projection.foldByRowKey.get(rowKey)
}

export function edgesCrossingFold(
  fold: TimelineFold,
  edges: readonly TimelineEdge[],
): readonly TimelineEdge[] {
  const eventIds = new Set(fold.eventIds)
  return freeze(
    edges.filter(
      (edge) =>
        eventIds.has(edge.sourceEventId) !==
        eventIds.has(edge.targetEventId),
    ),
  )
}
