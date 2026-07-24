import type {
  TimelineBackgroundLifecycle,
  TimelineEventFacts,
} from "./contracts.ts"
import { TimelinePhase } from "./contracts.ts"
import {
  compareTimelineOrder,
  readString,
} from "./event-reader.ts"

interface BackgroundDraft {
  id: string
  jobId: string
  workerId?: string
  type?: string
  label?: string
  events: TimelineEventFacts[]
  parkEventIds: string[]
  reviveEventIds: string[]
}

export interface BackgroundLifecycleResult {
  lifecycles: readonly TimelineBackgroundLifecycle[]
  lifecycleIdByEvent: Readonly<Record<string, string>>
  danglingParkEventIds: readonly string[]
  repeatedReviveEventIds: readonly string[]
}

function backgroundJobId(facts: TimelineEventFacts): string | undefined {
  return (
    facts.backgroundJobId ??
    readString(
      facts.safeAttributes,
      "background_job_id",
      "job_id",
    )
  )
}

function createDraft(
  jobId: string,
  facts: TimelineEventFacts,
): BackgroundDraft {
  return {
    id: `background-lifecycle:${jobId}`,
    jobId,
    workerId: facts.event.workerId ?? facts.worker?.id,
    type: readString(facts.safeAttributes, "job_type"),
    label: readString(facts.safeAttributes, "label", "summary"),
    events: [],
    parkEventIds: [],
    reviveEventIds: [],
  }
}

function eventRole(
  facts: TimelineEventFacts,
): "park" | "revive" | "terminal" | "running" | "other" {
  const type = facts.event.eventType.toLowerCase()
  const status = readString(
    facts.safeAttributes,
    "status",
    "result_status",
    "phase",
  )?.toLowerCase()
  if (
    type.includes("park") ||
    type.includes("suspend") ||
    status === "parked" ||
    status === "suspended"
  ) {
    return "park"
  }
  if (
    type.includes("revive") ||
    type.includes("resume") ||
    type.includes("restored") ||
    status === "reviving" ||
    status === "resumed"
  ) {
    return "revive"
  }
  if (
    facts.event.terminal ||
    facts.phase === TimelinePhase.COMPLETED ||
    facts.phase === TimelinePhase.FAILED ||
    facts.phase === TimelinePhase.CANCELLED
  ) {
    return "terminal"
  }
  if (
    facts.phase === TimelinePhase.RUNNING ||
    type.includes("running") ||
    type.includes("started")
  ) {
    return "running"
  }
  return "other"
}

function addEvent(draft: BackgroundDraft, facts: TimelineEventFacts): void {
  if (
    draft.events.some(
      (candidate) => candidate.event.eventId === facts.event.eventId,
    )
  ) {
    return
  }
  draft.events.push(facts)
  draft.events.sort((left, right) =>
    compareTimelineOrder(left.order, right.order),
  )
  draft.workerId ??= facts.event.workerId ?? facts.worker?.id
  draft.type ??= readString(facts.safeAttributes, "job_type")
  draft.label ??= readString(facts.safeAttributes, "label", "summary")
  const role = eventRole(facts)
  if (role === "park") draft.parkEventIds.push(facts.event.eventId)
  if (role === "revive") draft.reviveEventIds.push(facts.event.eventId)
}

function lifecycleStatus(
  draft: BackgroundDraft,
): TimelineBackgroundLifecycle["status"] {
  const last = draft.events.at(-1)
  if (!last) return "unknown"
  const role = eventRole(last)
  const status = readString(
    last.safeAttributes,
    "status",
    "result_status",
    "phase",
  )?.toLowerCase()
  if (role === "park") return "parked"
  if (role === "revive") return "reviving"
  if (
    last.phase === TimelinePhase.FAILED ||
    status === "failed" ||
    status === "error"
  ) {
    return "failed"
  }
  if (
    last.phase === TimelinePhase.CANCELLED ||
    status === "cancelled" ||
    status === "canceled" ||
    status === "aborted"
  ) {
    return "cancelled"
  }
  if (
    last.phase === TimelinePhase.COMPLETED ||
    status === "completed" ||
    status === "done" ||
    (last.event.terminal && last.event.effective)
  ) {
    return "completed"
  }
  if (role === "running" || last.phase === TimelinePhase.RUNNING) {
    return "running"
  }
  return "unknown"
}

function finalize(draft: BackgroundDraft): TimelineBackgroundLifecycle {
  const first = draft.events[0]!
  const last = draft.events.at(-1)!
  const status = lifecycleStatus(draft)
  const unmatchedPark =
    draft.parkEventIds.length > draft.reviveEventIds.length &&
    status !== "completed" &&
    status !== "failed" &&
    status !== "cancelled"
  return Object.freeze({
    id: draft.id,
    jobId: draft.jobId,
    workerId: draft.workerId,
    type: draft.type,
    label: draft.label,
    status,
    startEventId: first.event.eventId,
    endEventId: last.event.eventId,
    eventIds: Object.freeze(draft.events.map((facts) => facts.event.eventId)),
    parkEventIds: Object.freeze([...new Set(draft.parkEventIds)]),
    reviveEventIds: Object.freeze([...new Set(draft.reviveEventIds)]),
    revivalCount: draft.reviveEventIds.length,
    partial:
      unmatchedPark ||
      draft.events.some((facts) => facts.partial) ||
      status === "unknown",
  })
}

function isBackgroundEvent(facts: TimelineEventFacts): boolean {
  return (
    facts.kinds.includes("background") ||
    Boolean(backgroundJobId(facts)) ||
    facts.event.eventType.toLowerCase().includes("park") ||
    facts.event.eventType.toLowerCase().includes("revive")
  )
}

export function buildBackgroundLifecycles(
  input: readonly TimelineEventFacts[],
): BackgroundLifecycleResult {
  const events = input
    .filter(isBackgroundEvent)
    .sort((left, right) => compareTimelineOrder(left.order, right.order))
  const drafts = new Map<string, BackgroundDraft>()
  const lifecycleIdByEvent: Record<string, string> = {}
  for (const facts of events) {
    const jobId =
      backgroundJobId(facts) ??
      `${facts.event.workerId ?? facts.worker?.id ?? "worker"}:${facts.event.correlationId}`
    let draft = drafts.get(jobId)
    if (!draft) {
      draft = createDraft(jobId, facts)
      drafts.set(jobId, draft)
    }
    addEvent(draft, facts)
    lifecycleIdByEvent[facts.event.eventId] = draft.id
  }
  const lifecycles = [...drafts.values()]
    .map(finalize)
    .sort((left, right) => {
      const leftFacts = events.find(
        (facts) => facts.event.eventId === left.startEventId,
      )
      const rightFacts = events.find(
        (facts) => facts.event.eventId === right.startEventId,
      )
      if (!leftFacts || !rightFacts) return left.id.localeCompare(right.id)
      return (
        compareTimelineOrder(leftFacts.order, rightFacts.order) ||
        left.id.localeCompare(right.id)
      )
    })
  const danglingParkEventIds = lifecycles
    .filter(
      (lifecycle) =>
        lifecycle.status === "parked" ||
        (lifecycle.parkEventIds.length > lifecycle.reviveEventIds.length &&
          lifecycle.partial),
    )
    .flatMap((lifecycle) =>
      lifecycle.parkEventIds.slice(lifecycle.reviveEventIds.length),
    )
  const repeatedReviveEventIds: string[] = []
  for (const lifecycle of lifecycles) {
    let previousRevive = false
    for (const eventId of lifecycle.eventIds) {
      const facts = events.find((candidate) => candidate.event.eventId === eventId)
      if (!facts) continue
      const role = eventRole(facts)
      if (role === "revive" && previousRevive) repeatedReviveEventIds.push(eventId)
      previousRevive = role === "revive"
      if (role === "park" || role === "running" || role === "terminal") {
        previousRevive = false
      }
    }
  }
  return Object.freeze({
    lifecycles: Object.freeze(lifecycles),
    lifecycleIdByEvent: Object.freeze(lifecycleIdByEvent),
    danglingParkEventIds: Object.freeze(danglingParkEventIds),
    repeatedReviveEventIds: Object.freeze(repeatedReviveEventIds),
  })
}
