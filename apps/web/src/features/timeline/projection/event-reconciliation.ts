import type { CanonicalProjectionState } from "../../../state/contracts.ts"
import type {
  TimelineBackgroundLifecycle,
  TimelineBrowserStep,
  TimelineCausalGraph,
  TimelineEventFacts,
  TimelineRecoveryChain,
  TimelineRow,
  TimelineWorkerEpoch,
} from "./contracts.ts"
import { TimelineEventKind, TimelinePhase } from "./contracts.ts"
import {
  compareTimelineOrder,
  phaseSeverity,
  primaryKind,
} from "./event-reader.ts"
import { buildTimelineDrilldowns, buildTimelineEvidence } from "./drilldown.ts"

interface RowDraft {
  key: string
  identity: string
  facts: TimelineEventFacts[]
  kind: TimelineRow["rowKind"]
  workerEpochId?: string
  duplicateCount: number
  late: boolean
}

export interface TimelineRowReconciliationInput {
  state: CanonicalProjectionState
  events: readonly TimelineEventFacts[]
  graph: TimelineCausalGraph
  workerEpochs: readonly TimelineWorkerEpoch[]
  epochIdByEvent: Readonly<Record<string, string>>
  phaseByEvent: Readonly<Record<string, string>>
  recoveryChains: readonly TimelineRecoveryChain[]
  recoveryAttemptByEvent: Readonly<Record<string, string>>
  backgroundLifecycles: readonly TimelineBackgroundLifecycle[]
  backgroundLifecycleIdByEvent: Readonly<Record<string, string>>
  browserSteps: readonly TimelineBrowserStep[]
  browserStepIdByEvent: Readonly<Record<string, string>>
  coalesceWindowMs: number
}

export interface TimelineRowReconciliationResult {
  rows: readonly TimelineRow[]
  rowsByKey: Readonly<Record<string, TimelineRow>>
  rowKeyByEvent: Readonly<Record<string, string>>
  rowKeysByWorker: Readonly<Record<string, readonly string[]>>
  rowKeysByPhase: Readonly<Record<string, readonly string[]>>
  rowKeysByKind: Readonly<Record<string, readonly string[]>>
  duplicateRowEventCount: number
  lateEventCount: number
}

function eventIdentity(
  facts: TimelineEventFacts,
  input: TimelineRowReconciliationInput,
): string {
  const eventId = facts.event.eventId
  if (facts.permissionId && facts.kinds.includes(TimelineEventKind.POLICY)) {
    const type = facts.event.eventType.toLowerCase()
    const stage =
      type.includes("asked") ||
      type.includes("requested") ||
      type.includes("pending") ||
      type.includes("awaiting")
        ? "request"
        : "decision"
    return `permission:${facts.permissionId}:${stage}`
  }
  const browserStep = input.browserStepIdByEvent[eventId]
  if (browserStep) {
    const type = facts.event.eventType.toLowerCase()
    const stage =
      !facts.event.terminal &&
      (type.includes("started") ||
        type.includes("requested") ||
        type.includes("running") ||
        type.includes("action"))
        ? "active"
        : "settled"
    return `browser:${browserStep}:${stage}`
  }
  const background = input.backgroundLifecycleIdByEvent[eventId]
  if (background) return `background:${background}`
  // A failed event can also be indexed into the recovery attempt that it
  // triggered. Keep the failure as its own stable row so operators can see
  // the boundary between the terminal worker fact and the subsequent plan.
  const type = facts.event.eventType.toLowerCase()
  const explicitRecoveryEvent =
    type.includes("recover") ||
    type.includes("retry") ||
    type.includes("resume") ||
    type.includes("replan")
  if (
    facts.failureId &&
    facts.kinds.includes(TimelineEventKind.FAILURE) &&
    !explicitRecoveryEvent
  ) {
    return `failure:${facts.failureId}`
  }
  const recoveryAttempt = input.recoveryAttemptByEvent[eventId]
  if (recoveryAttempt) return `recovery:${recoveryAttempt}`
  if (facts.event.toolCallId || facts.tool?.id) {
    return `tool:${facts.event.toolCallId ?? facts.tool?.id}`
  }
  if (
    facts.routeId &&
    (facts.kinds.includes(TimelineEventKind.ROUTE) ||
      facts.kinds.includes(TimelineEventKind.PLACEMENT))
  ) {
    return `route:${facts.routeId}`
  }
  if (facts.event.checkpointId) {
    return `checkpoint:${facts.event.checkpointId}`
  }
  return `event:${eventId}`
}

function groupKind(
  facts: TimelineEventFacts,
  identity: string,
): TimelineRow["rowKind"] {
  if (identity.startsWith("browser:")) return TimelineEventKind.BROWSER_STEP
  if (identity.startsWith("background:")) return TimelineEventKind.BACKGROUND
  if (identity.startsWith("recovery:")) return TimelineEventKind.RECOVERY
  if (identity.startsWith("tool:")) return TimelineEventKind.TOOL
  if (identity.startsWith("permission:")) return TimelineEventKind.POLICY
  if (identity.startsWith("failure:")) return TimelineEventKind.FAILURE
  if (identity.startsWith("route:")) return TimelineEventKind.ROUTE
  if (identity.startsWith("checkpoint:")) return TimelineEventKind.CHECKPOINT
  return primaryKind(facts.kinds)
}

function stableRowKey(
  identity: string,
  kind: TimelineRow["rowKind"],
  facts: TimelineEventFacts,
): string {
  return `${kind}:${identity}:${facts.event.runId}`
}

function isSemanticDuplicate(
  left: TimelineEventFacts,
  right: TimelineEventFacts,
): boolean {
  return (
    left.event.eventType === right.event.eventType &&
    left.event.terminal === right.event.terminal &&
    left.event.mutationId === right.event.mutationId &&
    left.phase === right.phase &&
    left.summary === right.summary
  )
}

function committedOutOfOrder(
  facts: TimelineEventFacts,
  previous: TimelineEventFacts | undefined,
): boolean {
  if (!previous) return false
  if (facts.order.sequence >= previous.order.sequence) return false
  return facts.order.committedAtMs >= previous.order.committedAtMs
}

function buildDrafts(
  input: TimelineRowReconciliationInput,
): RowDraft[] {
  const drafts = new Map<string, RowDraft>()
  let previousByCommit: TimelineEventFacts | undefined
  const committed = [...input.events].sort(
    (left, right) =>
      left.order.committedAtMs - right.order.committedAtMs ||
      left.order.createdAtMs - right.order.createdAtMs ||
      left.event.eventId.localeCompare(right.event.eventId),
  )
  const lateIds = new Set<string>()
  let highSequence = 0
  for (const facts of committed) {
    if (facts.order.sequence < highSequence) lateIds.add(facts.event.eventId)
    highSequence = Math.max(highSequence, facts.order.sequence)
    if (committedOutOfOrder(facts, previousByCommit)) {
      lateIds.add(facts.event.eventId)
    }
    previousByCommit = facts
  }
  for (const facts of input.events) {
    if (facts.event.terminal) continue
    const stepId = input.browserStepIdByEvent[facts.event.eventId]
    if (!stepId) continue
    const settled = input.events.some(
      (candidate) =>
        input.browserStepIdByEvent[candidate.event.eventId] === stepId &&
        candidate.event.terminal &&
        candidate.order.sequence < facts.order.sequence,
    )
    if (settled) lateIds.add(facts.event.eventId)
  }
  for (const facts of input.events) {
    const identity = eventIdentity(facts, input)
    const kind = groupKind(facts, identity)
    const key = stableRowKey(identity, kind, facts)
    const existing = drafts.get(key)
    if (!existing) {
      drafts.set(key, {
        key,
        identity,
        facts: [facts],
        kind,
        workerEpochId: input.epochIdByEvent[facts.event.eventId],
        duplicateCount: 0,
        late: lateIds.has(facts.event.eventId),
      })
      continue
    }
    if (
      existing.facts.some((candidate) => candidate.event.terminal) &&
      !facts.event.terminal
    ) {
      existing.late = true
    }
    const last = existing.facts.at(-1)
    const outsideWindow =
      last &&
      input.coalesceWindowMs > 0 &&
      facts.order.committedAtMs - last.order.committedAtMs >
        input.coalesceWindowMs &&
      identity.startsWith("event:")
    if (outsideWindow) {
      const splitKey = `${key}:sequence:${facts.order.sequence}`
      drafts.set(splitKey, {
        key: splitKey,
        identity: `${identity}:${facts.order.sequence}`,
        facts: [facts],
        kind,
        workerEpochId: input.epochIdByEvent[facts.event.eventId],
        duplicateCount: 0,
        late: lateIds.has(facts.event.eventId),
      })
      continue
    }
    if (
      existing.facts.some((candidate) =>
        isSemanticDuplicate(candidate, facts),
      )
    ) {
      existing.duplicateCount += 1
    }
    existing.facts.push(facts)
    existing.facts.sort((left, right) =>
      compareTimelineOrder(left.order, right.order),
    )
    existing.workerEpochId ??= input.epochIdByEvent[facts.event.eventId]
    existing.late ||= lateIds.has(facts.event.eventId)
  }
  return [...drafts.values()].sort((left, right) => {
    const leftFirst = left.facts[0]!
    const rightFirst = right.facts[0]!
    return (
      compareTimelineOrder(leftFirst.order, rightFirst.order) ||
      left.key.localeCompare(right.key)
    )
  })
}

function rowPhase(
  draft: RowDraft,
  input: TimelineRowReconciliationInput,
): TimelineRow["phase"] {
  let result = TimelinePhase.UNKNOWN as TimelineRow["phase"]
  for (const facts of draft.facts) {
    const phase =
      (facts.phase && facts.phase !== TimelinePhase.UNKNOWN
        ? facts.phase
        : (input.phaseByEvent[facts.event.eventId] as TimelineRow["phase"])) ??
      TimelinePhase.UNKNOWN
    if (facts.event.terminal) result = phase
    else if (phaseSeverity(phase) > phaseSeverity(result)) result = phase
  }
  const last = draft.facts.at(-1)
  if (last) {
    const lastPhase =
      (last.phase && last.phase !== TimelinePhase.UNKNOWN
        ? last.phase
        : (input.phaseByEvent[last.event.eventId] as TimelineRow["phase"]))
    if (lastPhase && lastPhase !== TimelinePhase.UNKNOWN) result = lastPhase
  }
  return result
}

function rowTitle(
  draft: RowDraft,
  input: TimelineRowReconciliationInput,
): string {
  if (draft.identity.startsWith("browser:")) {
    const stepId = draft.identity
      .slice("browser:".length)
      .replace(/:(active|settled)$/, "")
    const step = input.browserSteps.find((candidate) => candidate.id === stepId)
    if (step?.actionNames.length === 1) return step.actionNames[0]!
    if (step?.actionNames.length) {
      return `${step.actionNames[0]} +${step.actionNames.length - 1} browser actions`
    }
    return "Browser step"
  }
  if (draft.identity.startsWith("background:")) {
    const id = draft.identity.slice("background:".length)
    const lifecycle = input.backgroundLifecycles.find(
      (candidate) => candidate.id === id,
    )
    return lifecycle?.label ?? `Background job ${lifecycle?.jobId ?? id}`
  }
  if (draft.identity.startsWith("recovery:")) {
    const attemptId = draft.identity.slice("recovery:".length)
    const attempt = input.recoveryChains
      .flatMap((chain) => chain.attempts)
      .find((candidate) => candidate.id === attemptId)
    return attempt?.strategy
      ? `Recovery: ${attempt.strategy}`
      : `Recovery attempt ${attempt?.attempt ?? ""}`.trim()
  }
  if (draft.identity.startsWith("tool:")) {
    const tool = draft.facts.find((facts) => facts.tool)?.tool
    return tool?.toolName ?? `Tool ${draft.identity.slice("tool:".length)}`
  }
  if (draft.identity.startsWith("permission:")) {
    const permission = draft.facts.find((facts) => facts.permission)?.permission
    return permission?.toolName
      ? `Permission: ${permission.toolName}`
      : "Policy decision"
  }
  if (draft.identity.startsWith("failure:")) {
    return `Failure ${draft.identity.slice("failure:".length)}`
  }
  if (draft.identity.startsWith("route:")) {
    return `Route ${draft.identity.slice("route:".length)}`
  }
  if (draft.identity.startsWith("checkpoint:")) {
    return `Checkpoint ${draft.identity.slice("checkpoint:".length)}`
  }
  return draft.facts[0]!.title
}

function rowSummary(
  draft: RowDraft,
  input: TimelineRowReconciliationInput,
): string {
  if (draft.identity.startsWith("browser:")) {
    const step = input.browserSteps.find(
      (candidate) =>
        candidate.id ===
        draft.identity
          .slice("browser:".length)
          .replace(/:(active|settled)$/, ""),
    )
    if (step?.error) return step.error
    if (step) {
      const pieces = [
        `${step.actionEventIds.length} action${step.actionEventIds.length === 1 ? "" : "s"}`,
        `${step.resultEventIds.length} result${step.resultEventIds.length === 1 ? "" : "s"}`,
        `${step.stateEventIds.length} state capture${step.stateEventIds.length === 1 ? "" : "s"}`,
      ]
      return pieces.join(" · ")
    }
  }
  if (draft.identity.startsWith("background:")) {
    const lifecycle = input.backgroundLifecycles.find(
      (candidate) =>
        candidate.id === draft.identity.slice("background:".length),
    )
    if (lifecycle) {
      return `${lifecycle.status} · ${lifecycle.revivalCount} revival${lifecycle.revivalCount === 1 ? "" : "s"}`
    }
  }
  if (draft.identity.startsWith("recovery:")) {
    const attempt = input.recoveryChains
      .flatMap((chain) => chain.attempts)
      .find(
        (candidate) =>
          candidate.id === draft.identity.slice("recovery:".length),
      )
    if (attempt) {
      return (
        attempt.reason ??
        `${attempt.phase} attempt ${attempt.attempt} for ${attempt.failureId ?? "unknown failure"}`
      )
    }
  }
  const summaries = [
    ...new Set(
      draft.facts
        .map((facts) => facts.summary.trim())
        .filter(Boolean),
    ),
  ]
  return summaries.slice(0, 3).join(" → ").slice(0, 900)
}

function eventIds(draft: RowDraft): readonly string[] {
  return Object.freeze(draft.facts.map((facts) => facts.event.eventId))
}

function artifactIds(draft: RowDraft): readonly string[] {
  return Object.freeze(
    [
      ...new Set(
        draft.facts.flatMap((facts) => [
          ...facts.event.artifactIds,
          ...facts.artifacts.map((artifact) => artifact.id),
        ]),
      ),
    ].sort(),
  )
}

function mutationIds(draft: RowDraft): readonly string[] {
  return Object.freeze(
    [...new Set(draft.facts.map((facts) => facts.event.mutationId))].sort(),
  )
}

function edgeIds(
  draft: RowDraft,
  graph: TimelineCausalGraph,
  direction: "incoming" | "outgoing",
): readonly string[] {
  const source =
    direction === "incoming" ? graph.incomingByEvent : graph.outgoingByEvent
  return Object.freeze(
    [
      ...new Set(
        draft.facts.flatMap(
          (facts) => source[facts.event.eventId] ?? [],
        ),
      ),
    ].sort(),
  )
}

function tags(
  draft: RowDraft,
  phase: TimelineRow["phase"],
): readonly string[] {
  const values = new Set<string>([draft.kind, phase])
  for (const facts of draft.facts) {
    for (const kind of facts.kinds) values.add(kind)
    if (!facts.event.effective) values.add("non-effective")
    if (facts.partial) values.add("partial")
    if (facts.event.terminal) values.add("terminal")
    if (facts.event.checkpointId) values.add("checkpoint")
    if (facts.failureId) values.add("failure")
    if (facts.recoveryId) values.add("recovery")
  }
  if (draft.duplicateCount) values.add("deduplicated")
  if (draft.late) values.add("late")
  return Object.freeze([...values].sort())
}

function combinedEvidence(
  draft: RowDraft,
  input: TimelineRowReconciliationInput,
): TimelineRow["evidence"] {
  const values = draft.facts.flatMap((facts) =>
    buildTimelineEvidence(input.state, facts),
  )
  const byIdentity = new Map<string, (typeof values)[number]>()
  for (const value of values) {
    const id = `${value.kind}:${value.id}`
    const existing = byIdentity.get(id)
    if (!existing) {
      byIdentity.set(id, value)
      continue
    }
    byIdentity.set(
      id,
      Object.freeze({
        ...existing,
        eventIds: Object.freeze([
          ...new Set([...existing.eventIds, ...value.eventIds]),
        ]),
        missing: existing.missing && value.missing,
        terminal: existing.terminal || value.terminal,
      }),
    )
  }
  return Object.freeze(
    [...byIdentity.values()].sort(
      (left, right) =>
        Number(left.missing) - Number(right.missing) ||
        left.kind.localeCompare(right.kind) ||
        left.id.localeCompare(right.id),
    ),
  )
}

function combinedDrilldowns(
  draft: RowDraft,
  input: TimelineRowReconciliationInput,
): TimelineRow["drilldowns"] {
  const values = draft.facts.flatMap((facts) =>
    buildTimelineDrilldowns(input.state, facts),
  )
  return Object.freeze(
    values.filter(
      (value, index) =>
        values.findIndex(
          (candidate) =>
            candidate.kind === value.kind &&
            candidate.entityId === value.entityId,
        ) === index,
    ),
  )
}

function buildRow(
  draft: RowDraft,
  input: TimelineRowReconciliationInput,
): TimelineRow {
  const first = draft.facts[0]!
  const last = draft.facts.at(-1)!
  const phase = rowPhase(draft, input)
  const workerId =
    last.event.workerId ??
    last.worker?.id ??
    first.event.workerId ??
    first.worker?.id
  const epochId =
    draft.workerEpochId ??
    draft.facts
      .map((facts) => input.epochIdByEvent[facts.event.eventId])
      .find(Boolean)
  const depth = Math.max(
    0,
    ...draft.facts.map(
      (facts) => input.graph.depthByEvent[facts.event.eventId] ?? 0,
    ),
  )
  return Object.freeze({
    key: draft.key,
    taskId: first.event.taskId,
    runId: first.event.runId,
    rowKind: draft.kind,
    phase,
    primaryEventId: first.event.eventId,
    eventIds: eventIds(draft),
    sequence: first.order.sequence,
    endSequence: last.order.sequence,
    occurredAt: first.event.createdAt,
    committedAt: last.event.committedAt,
    title: rowTitle(draft, input),
    summary: rowSummary(draft, input),
    workerId,
    workerEpochId: epochId,
    leaseId: last.leaseId ?? first.leaseId,
    nodeId: last.event.nodeId ?? first.event.nodeId,
    routeId: last.routeId ?? first.routeId,
    placementId: last.placementId ?? first.placementId,
    toolCallId:
      last.event.toolCallId ??
      last.tool?.id ??
      first.event.toolCallId ??
      first.tool?.id,
    permissionId: last.permissionId ?? first.permissionId,
    failureId: last.failureId ?? first.failureId,
    recoveryId: last.recoveryId ?? first.recoveryId,
    backgroundJobId: last.backgroundJobId ?? first.backgroundJobId,
    browserStepId: last.browserStepId ?? first.browserStepId,
    spanId: last.event.spanId ?? first.event.spanId,
    correlationId: first.event.correlationId,
    causationId: first.event.causationId,
    depth,
    critical: false,
    terminal: draft.facts.some((facts) => facts.event.terminal),
    effective: draft.facts.some((facts) => facts.event.effective),
    partial: draft.facts.some((facts) => facts.partial),
    duplicateCount: draft.duplicateCount,
    late: draft.late,
    evidence: combinedEvidence(draft, input),
    drilldowns: combinedDrilldowns(draft, input),
    artifactIds: artifactIds(draft),
    mutationIds: mutationIds(draft),
    incomingEdgeIds: edgeIds(draft, input.graph, "incoming"),
    outgoingEdgeIds: edgeIds(draft, input.graph, "outgoing"),
    tags: tags(draft, phase),
  })
}

function indexRows(
  rows: readonly TimelineRow[],
  key: (row: TimelineRow) => string | undefined,
): Readonly<Record<string, readonly string[]>> {
  const result: Record<string, string[]> = {}
  for (const row of rows) {
    const id = key(row)
    if (!id) continue
    const values = result[id] ?? []
    values.push(row.key)
    result[id] = values
  }
  return Object.freeze(
    Object.fromEntries(
      Object.entries(result).map(([id, values]) => [
        id,
        Object.freeze(values),
      ]),
    ),
  )
}

export function buildTimelineRows(
  input: TimelineRowReconciliationInput,
): TimelineRowReconciliationResult {
  const drafts = buildDrafts(input)
  const rows = drafts.map((draft) => buildRow(draft, input))
  const rowsByKey: Record<string, TimelineRow> = {}
  const rowKeyByEvent: Record<string, string> = {}
  for (const row of rows) {
    rowsByKey[row.key] = row
    for (const eventId of row.eventIds) rowKeyByEvent[eventId] = row.key
  }
  return Object.freeze({
    rows: Object.freeze(rows),
    rowsByKey: Object.freeze(rowsByKey),
    rowKeyByEvent: Object.freeze(rowKeyByEvent),
    rowKeysByWorker: indexRows(rows, (row) => row.workerId),
    rowKeysByPhase: indexRows(rows, (row) => row.phase),
    rowKeysByKind: indexRows(rows, (row) => row.rowKind),
    duplicateRowEventCount: drafts.reduce(
      (total, draft) => total + draft.duplicateCount,
      0,
    ),
    lateEventCount: drafts.filter((draft) => draft.late).length,
  })
}

export function markCriticalRows(
  result: TimelineRowReconciliationResult,
  criticalEventIds: ReadonlySet<string>,
): TimelineRowReconciliationResult {
  const rows = result.rows.map((row) => {
    const critical = row.eventIds.some((eventId) =>
      criticalEventIds.has(eventId),
    )
    return critical === row.critical
      ? row
      : Object.freeze({ ...row, critical })
  })
  const rowsByKey = Object.freeze(
    Object.fromEntries(rows.map((row) => [row.key, row])),
  )
  return Object.freeze({ ...result, rows: Object.freeze(rows), rowsByKey })
}
