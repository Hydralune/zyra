import type {
  TimelineCausalGraph,
  TimelineEventFacts,
  TimelineWorkerEpoch,
} from "./contracts.ts"
import { TimelineEdgeKind, TimelinePhase } from "./contracts.ts"
import { compareTimelineOrder, readString } from "./event-reader.ts"
import {
  buildWorkerPhaseTrace,
  terminalPhase,
  type TimelinePhaseTrace,
} from "./phase-machine.ts"

interface EpochDraft {
  id: string
  workerId: string
  taskId: string
  runId: string
  nodeId?: string
  role?: string
  leaseId?: string
  routeId?: string
  placementId?: string
  previousEpochId?: string
  replacementEpochId?: string
  replacementReason?: string
  events: TimelineEventFacts[]
  duplicateEventCount: number
  lateEventCount: number
}

export interface TimelineWorkerEpochResult {
  epochs: readonly TimelineWorkerEpoch[]
  epochIdByEvent: Readonly<Record<string, string>>
  phaseByEvent: Readonly<Record<string, string>>
  traces: Readonly<Record<string, TimelinePhaseTrace>>
  replacementPairs: readonly {
    previousEpochId: string
    replacementEpochId: string
    recoveryId?: string
    eventId: string
  }[]
}

function workerIdentity(facts: TimelineEventFacts): string | undefined {
  return facts.event.workerId ?? facts.worker?.id
}

function epochId(facts: TimelineEventFacts, ordinal: number): string {
  return [
    "worker-epoch",
    workerIdentity(facts) ?? "unassigned",
    facts.leaseId ?? "lease-none",
    facts.order.sequence,
    ordinal,
  ].join(":")
}

function shouldSplitEpoch(
  current: EpochDraft,
  facts: TimelineEventFacts,
): boolean {
  const latest = current.events.at(-1)
  if (!latest) return false
  const eventType = facts.event.eventType.toLowerCase()
  if (facts.event.runId !== current.runId) return true
  if (
    eventType.includes("lease.replaced") ||
    eventType.includes("lease.reassigned") ||
    eventType.includes("worker.restarted") ||
    eventType.includes("worker.revived")
  ) {
    return true
  }
  if (facts.leaseId && current.leaseId && facts.leaseId !== current.leaseId) {
    return true
  }
  if (facts.routeId && current.routeId && facts.routeId !== current.routeId) {
    const explicitReplacement =
      facts.event.eventType.toLowerCase().includes("replace") ||
      facts.event.eventType.toLowerCase().includes("reroute") ||
      facts.event.eventType.toLowerCase().includes("failover")
    if (explicitReplacement) return true
  }
  if (
    facts.placementId &&
    current.placementId &&
    facts.placementId !== current.placementId
  ) {
    return true
  }
  if (
    latest.event.terminal &&
    (facts.event.eventType.toLowerCase().includes("restart") ||
      facts.event.eventType.toLowerCase().includes("revive") ||
      facts.event.eventType.toLowerCase().includes("resumed") ||
      facts.kinds.includes("recovery"))
  ) {
    return true
  }
  return false
}

function replacementReason(
  previous: EpochDraft,
  next: EpochDraft,
): string {
  if (previous.leaseId !== next.leaseId) return "lease replacement"
  if (previous.routeId !== next.routeId) return "route replacement"
  if (previous.placementId !== next.placementId) return "placement replacement"
  if (previous.runId !== next.runId) return "run restart"
  return "worker revival"
}

function createDraft(
  facts: TimelineEventFacts,
  ordinal: number,
): EpochDraft {
  const workerId = workerIdentity(facts) ?? `unassigned:${facts.event.nodeId ?? "task"}`
  return {
    id: epochId(facts, ordinal),
    workerId,
    taskId: facts.event.taskId,
    runId: facts.event.runId,
    nodeId: facts.event.nodeId ?? facts.worker?.nodeId,
    role:
      facts.worker?.role ??
      readString(facts.safeAttributes, "role", "worker_role"),
    leaseId: facts.leaseId,
    routeId: facts.routeId,
    placementId: facts.placementId,
    events: [facts],
    duplicateEventCount: 0,
    lateEventCount: 0,
  }
}

function addFacts(draft: EpochDraft, facts: TimelineEventFacts): void {
  const duplicate = draft.events.find(
    (candidate) => candidate.event.eventId === facts.event.eventId,
  )
  if (duplicate) {
    draft.duplicateEventCount += 1
    return
  }
  const latest = draft.events.at(-1)
  if (latest && compareTimelineOrder(facts.order, latest.order) < 0) {
    draft.lateEventCount += 1
  }
  draft.events.push(facts)
  draft.events.sort((left, right) =>
    compareTimelineOrder(left.order, right.order),
  )
  draft.nodeId ??= facts.event.nodeId ?? facts.worker?.nodeId
  draft.role ??=
    facts.worker?.role ??
    readString(facts.safeAttributes, "role", "worker_role")
  draft.leaseId ??= facts.leaseId
  draft.routeId ??= facts.routeId
  draft.placementId ??= facts.placementId
}

function groupWorkerEvents(
  events: readonly TimelineEventFacts[],
): Map<string, TimelineEventFacts[]> {
  const groups = new Map<string, TimelineEventFacts[]>()
  const unassignedByNode = new Map<string, TimelineEventFacts[]>()
  for (const facts of events) {
    const workerId = workerIdentity(facts)
    if (workerId) {
      const values = groups.get(workerId) ?? []
      values.push(facts)
      groups.set(workerId, values)
      continue
    }
    if (
      !facts.event.nodeId ||
      (!facts.kinds.includes("worker") &&
        !facts.kinds.includes("tool") &&
        !facts.kinds.includes("failure") &&
        !facts.kinds.includes("recovery"))
    ) {
      continue
    }
    const nodeKey = `${facts.event.runId}:${facts.event.nodeId}`
    const values = unassignedByNode.get(nodeKey) ?? []
    values.push(facts)
    unassignedByNode.set(nodeKey, values)
  }
  for (const [nodeKey, values] of unassignedByNode) {
    groups.set(`unassigned:${nodeKey}`, values)
  }
  for (const values of groups.values()) {
    values.sort((left, right) => compareTimelineOrder(left.order, right.order))
  }
  return groups
}

function splitEpochs(
  workerEvents: Map<string, TimelineEventFacts[]>,
): EpochDraft[] {
  const result: EpochDraft[] = []
  for (const values of workerEvents.values()) {
    let current: EpochDraft | undefined
    let ordinal = 0
    for (const facts of values) {
      if (!current || shouldSplitEpoch(current, facts)) {
        const previous = current
        current = createDraft(facts, ordinal)
        ordinal += 1
        if (previous) {
          if (
            facts.event.eventType.toLowerCase().includes("lease.") &&
            previous.leaseId === current.leaseId
          ) {
            previous.leaseId = undefined
          }
          previous.replacementEpochId = current.id
          current.previousEpochId = previous.id
          const reason = replacementReason(previous, current)
          previous.replacementReason = reason
          current.replacementReason = reason
        }
        result.push(current)
      } else {
        addFacts(current, facts)
      }
    }
  }
  result.sort((left, right) => {
    const leftFacts = left.events[0]
    const rightFacts = right.events[0]
    if (!leftFacts || !rightFacts) return left.id.localeCompare(right.id)
    return (
      compareTimelineOrder(leftFacts.order, rightFacts.order) ||
      left.id.localeCompare(right.id)
    )
  })
  return result
}

function propagateGraphReplacements(
  drafts: EpochDraft[],
  graph: TimelineCausalGraph,
): readonly {
  previousEpochId: string
  replacementEpochId: string
  recoveryId?: string
  eventId: string
}[] {
  const byEvent = new Map<string, EpochDraft>()
  for (const draft of drafts) {
    for (const facts of draft.events) byEvent.set(facts.event.eventId, draft)
  }
  const pairs: Array<{
    previousEpochId: string
    replacementEpochId: string
    recoveryId?: string
    eventId: string
  }> = []
  for (const edge of graph.edges) {
    if (
      edge.kind !== TimelineEdgeKind.WORKER_REPLACEMENT &&
      edge.kind !== TimelineEdgeKind.LEASE_REPLACEMENT &&
      edge.kind !== TimelineEdgeKind.BACKGROUND_REVIVE
    ) {
      continue
    }
    const previous = byEvent.get(edge.sourceEventId)
    const replacement = byEvent.get(edge.targetEventId)
    if (!previous || !replacement || previous.id === replacement.id) continue
    previous.replacementEpochId = replacement.id
    replacement.previousEpochId = previous.id
    const reason =
      edge.kind === TimelineEdgeKind.LEASE_REPLACEMENT
        ? "lease replacement"
        : edge.kind === TimelineEdgeKind.BACKGROUND_REVIVE
          ? "background revival"
          : "recovery worker replacement"
    previous.replacementReason = reason
    replacement.replacementReason = reason
    pairs.push(
      Object.freeze({
        previousEpochId: previous.id,
        replacementEpochId: replacement.id,
        recoveryId:
          typeof edge.metadata.recoveryId === "string"
            ? edge.metadata.recoveryId
            : undefined,
        eventId: edge.targetEventId,
      }),
    )
  }
  for (const draft of drafts) {
    if (!draft.replacementEpochId) continue
    if (
      pairs.some(
        (pair) =>
          pair.previousEpochId === draft.id &&
          pair.replacementEpochId === draft.replacementEpochId,
      )
    ) {
      continue
    }
    pairs.push(
      Object.freeze({
        previousEpochId: draft.id,
        replacementEpochId: draft.replacementEpochId,
        eventId:
          drafts
            .find((candidate) => candidate.id === draft.replacementEpochId)
            ?.events[0]?.event.eventId ?? draft.events.at(-1)!.event.eventId,
      }),
    )
  }
  return Object.freeze(
    pairs.sort(
      (left, right) =>
        left.eventId.localeCompare(right.eventId) ||
        left.previousEpochId.localeCompare(right.previousEpochId),
    ),
  )
}

function uniqueSorted(values: readonly (string | undefined)[]): readonly string[] {
  return Object.freeze(
    [...new Set(values.filter((value): value is string => Boolean(value)))].sort(),
  )
}

function finalizeEpoch(
  draft: EpochDraft,
  trace: TimelinePhaseTrace,
): TimelineWorkerEpoch {
  const start = draft.events[0]!
  const end = draft.events.at(-1)!
  return Object.freeze({
    id: draft.id,
    workerId: draft.workerId,
    taskId: draft.taskId,
    runId: draft.runId,
    nodeId: draft.nodeId,
    role: draft.role,
    leaseId: draft.leaseId,
    routeId: draft.routeId,
    placementId: draft.placementId,
    previousEpochId: draft.previousEpochId,
    replacementEpochId: draft.replacementEpochId,
    replacementReason: draft.replacementReason,
    startEventId: start.event.eventId,
    endEventId: end.event.eventId,
    startSequence: start.order.sequence,
    endSequence: end.order.sequence,
    startedAt: start.event.committedAt,
    endedAt: end.event.committedAt,
    terminal:
      terminalPhase(trace.finalPhase) ||
      Boolean(end.event.terminal && trace.finalPhase !== TimelinePhase.RECOVERING),
    finalPhase: trace.finalPhase,
    phases: trace.intervals,
    eventIds: Object.freeze(draft.events.map((facts) => facts.event.eventId)),
    toolCallIds: uniqueSorted(
      draft.events.map((facts) => facts.event.toolCallId ?? facts.tool?.id),
    ),
    artifactIds: uniqueSorted(
      draft.events.flatMap((facts) =>
        facts.artifacts.map((artifact) => artifact.id),
      ),
    ),
    failureIds: uniqueSorted(draft.events.map((facts) => facts.failureId)),
    recoveryIds: uniqueSorted(draft.events.map((facts) => facts.recoveryId)),
    duplicateEventCount: draft.duplicateEventCount,
    lateEventCount: draft.lateEventCount,
  })
}

export function buildTimelineWorkerEpochs(
  events: readonly TimelineEventFacts[],
  graph: TimelineCausalGraph,
): TimelineWorkerEpochResult {
  const drafts = splitEpochs(groupWorkerEvents(events))
  const replacementPairs = propagateGraphReplacements(drafts, graph)
  const traces: Record<string, TimelinePhaseTrace> = {}
  const epochIdByEvent: Record<string, string> = {}
  const phaseByEvent: Record<string, string> = {}
  const epochs = drafts.map((draft) => {
    const trace = buildWorkerPhaseTrace(
      draft.workerId,
      draft.id,
      draft.events,
    )
    traces[draft.id] = trace
    for (const transition of trace.transitions) {
      epochIdByEvent[transition.eventId] = draft.id
      phaseByEvent[transition.eventId] = transition.to
    }
    return finalizeEpoch(draft, trace)
  })
  return Object.freeze({
    epochs: Object.freeze(epochs),
    epochIdByEvent: Object.freeze(epochIdByEvent),
    phaseByEvent: Object.freeze(phaseByEvent),
    traces: Object.freeze(traces),
    replacementPairs,
  })
}

export function epochForEvent(
  result: TimelineWorkerEpochResult,
  eventId: string,
): TimelineWorkerEpoch | undefined {
  const epochId = result.epochIdByEvent[eventId]
  return epochId
    ? result.epochs.find((epoch) => epoch.id === epochId)
    : undefined
}

export function currentEpochForWorker(
  result: TimelineWorkerEpochResult,
  workerId: string,
): TimelineWorkerEpoch | undefined {
  return [...result.epochs]
    .filter((epoch) => epoch.workerId === workerId)
    .sort(
      (left, right) =>
        right.endSequence - left.endSequence || right.id.localeCompare(left.id),
    )[0]
}

export function workerReplacementLineage(
  result: TimelineWorkerEpochResult,
  epochId: string,
): readonly string[] {
  const lineage: string[] = []
  const visited = new Set<string>()
  let current = result.epochs.find((epoch) => epoch.id === epochId)
  while (current && !visited.has(current.id)) {
    visited.add(current.id)
    lineage.unshift(current.id)
    current = current.previousEpochId
      ? result.epochs.find((epoch) => epoch.id === current!.previousEpochId)
      : undefined
  }
  return Object.freeze(lineage)
}

export function workerEpochDescendants(
  result: TimelineWorkerEpochResult,
  epochId: string,
): readonly string[] {
  const descendants: string[] = []
  const visited = new Set([epochId])
  let current = result.epochs.find((epoch) => epoch.id === epochId)
  while (current?.replacementEpochId) {
    const nextId = current.replacementEpochId
    if (visited.has(nextId)) break
    visited.add(nextId)
    descendants.push(nextId)
    current = result.epochs.find((epoch) => epoch.id === nextId)
  }
  return Object.freeze(descendants)
}
