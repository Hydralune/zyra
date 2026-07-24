import type {
  TimelineEventFacts,
  TimelineRecoveryAttempt,
  TimelineRecoveryChain,
  TimelineWorkerEpoch,
} from "./contracts.ts"
import { TimelinePhase } from "./contracts.ts"
import {
  compareTimelineOrder,
  readNumber,
  readString,
  readStringArray,
} from "./event-reader.ts"

interface AttemptDraft {
  id: string
  recoveryId: string
  failureId?: string
  attempt: number
  strategy?: string
  workerId?: string
  previousWorkerId?: string
  replacementWorkerId?: string
  checkpointId?: string
  events: TimelineEventFacts[]
  duplicateEventIds: string[]
}

interface ChainDraft {
  id: string
  failureId: string
  events: TimelineEventFacts[]
  attempts: AttemptDraft[]
}

export interface TimelineRecoveryResult {
  chains: readonly TimelineRecoveryChain[]
  attemptByEvent: Readonly<Record<string, string>>
  chainByEvent: Readonly<Record<string, string>>
  duplicateRecoveryCount: number
  unmatchedRecoveryEventIds: readonly string[]
}

function recoveryIdentity(facts: TimelineEventFacts): string | undefined {
  const type = facts.event.eventType.toLowerCase()
  const recoveryEvent =
    Boolean(facts.event.recoveryId) ||
    type.includes("recover") ||
    type.includes("retry") ||
    type.includes("resume") ||
    type.includes("replan")
  return (
    facts.event.recoveryId ??
    (recoveryEvent ? facts.recovery?.id : undefined) ??
    (recoveryEvent ? facts.recoveryId : undefined) ??
    (recoveryEvent
      ? readString(facts.safeAttributes, "recovery_id", "plan_id")
      : undefined)
  )
}

function failureIdentity(facts: TimelineEventFacts): string | undefined {
  return (
    facts.failureId ??
    facts.recovery?.failureId ??
    readString(facts.safeAttributes, "failure_id", "fault_id")
  )
}

function attemptNumber(facts: TimelineEventFacts): number {
  return Math.max(
    0,
    Math.trunc(
      facts.recovery?.attempt ??
        readNumber(facts.safeAttributes, "attempt", "attempt_number", "retry") ??
        0,
    ),
  )
}

function strategy(facts: TimelineEventFacts): string | undefined {
  return (
    facts.recovery?.strategy ??
    readString(
      facts.safeAttributes,
      "strategy",
      "recovery_strategy",
      "operation",
    )
  )
}

function recoveryWorkerId(facts: TimelineEventFacts): string | undefined {
  return (
    facts.event.workerId ??
    facts.worker?.id ??
    readString(facts.safeAttributes, "worker_id")
  )
}

function eventSignature(facts: TimelineEventFacts): string {
  return [
    facts.event.eventType,
    facts.event.terminal ? "terminal" : "open",
    facts.phase ?? TimelinePhase.UNKNOWN,
    facts.recoveryId ?? "",
    facts.failureId ?? "",
    readString(facts.safeAttributes, "status", "result_status") ?? "",
  ].join("|")
}

function createAttempt(
  facts: TimelineEventFacts,
  attemptOverride?: number,
): AttemptDraft {
  const recoveryId =
    recoveryIdentity(facts) ?? `recovery:${facts.event.eventId}`
  const attempt = attemptOverride ?? attemptNumber(facts)
  return {
    id: `${recoveryId}:attempt:${attempt}`,
    recoveryId,
    failureId: failureIdentity(facts),
    attempt,
    strategy: strategy(facts),
    workerId: recoveryWorkerId(facts),
    previousWorkerId:
      facts.recovery?.previousWorkerId ??
      readString(
        facts.safeAttributes,
        "previous_worker_id",
        "failed_worker_id",
      ),
    replacementWorkerId:
      facts.recovery?.replacementWorkerId ??
      readString(
        facts.safeAttributes,
        "replacement_worker_id",
        "new_worker_id",
      ),
    checkpointId:
      facts.recovery?.resumedCheckpointId ??
      facts.event.checkpointId ??
      readString(facts.safeAttributes, "checkpoint_id"),
    events: [facts],
    duplicateEventIds: [],
  }
}

function addAttemptEvent(
  attempt: AttemptDraft,
  facts: TimelineEventFacts,
): void {
  if (
    attempt.events.some(
      (candidate) => candidate.event.eventId === facts.event.eventId,
    )
  ) {
    attempt.duplicateEventIds.push(facts.event.eventId)
    return
  }
  const signature = eventSignature(facts)
  const duplicateTerminal = attempt.events.find(
    (candidate) =>
      candidate.event.terminal &&
      facts.event.terminal &&
      eventSignature(candidate) === signature,
  )
  if (duplicateTerminal) {
    attempt.duplicateEventIds.push(facts.event.eventId)
    return
  }
  attempt.events.push(facts)
  attempt.events.sort((left, right) =>
    compareTimelineOrder(left.order, right.order),
  )
  attempt.failureId ??= failureIdentity(facts)
  attempt.strategy ??= strategy(facts)
  attempt.workerId ??= recoveryWorkerId(facts)
  attempt.previousWorkerId ??=
    facts.recovery?.previousWorkerId ??
    readString(facts.safeAttributes, "previous_worker_id", "failed_worker_id")
  attempt.replacementWorkerId ??=
    facts.recovery?.replacementWorkerId ??
    readString(
      facts.safeAttributes,
      "replacement_worker_id",
      "new_worker_id",
    )
  attempt.checkpointId ??=
    facts.recovery?.resumedCheckpointId ??
    facts.event.checkpointId ??
    readString(facts.safeAttributes, "checkpoint_id")
}

function attemptPhase(
  attempt: AttemptDraft,
): TimelineRecoveryAttempt["phase"] {
  const last = attempt.events.at(-1)!
  const type = last.event.eventType.toLowerCase()
  const explicit = readString(
    last.safeAttributes,
    "status",
    "result_status",
    "phase",
  )?.toLowerCase()
  if (
    explicit === "cancelled" ||
    explicit === "canceled" ||
    explicit === "aborted" ||
    type.includes("cancel") ||
    type.includes("abort")
  ) {
    return "cancelled"
  }
  if (
    last.phase === TimelinePhase.FAILED ||
    explicit === "failed" ||
    explicit === "error" ||
    type.includes("failed") ||
    type.includes("exhausted")
  ) {
    return "failed"
  }
  if (
    last.phase === TimelinePhase.COMPLETED ||
    explicit === "completed" ||
    explicit === "succeeded" ||
    type.includes("completed") ||
    type.includes("committed") ||
    type.includes("resumed")
  ) {
    return "completed"
  }
  if (
    type.includes("planned") ||
    explicit === "planned" ||
    explicit === "pending"
  ) {
    return "planned"
  }
  if (
    attempt.events.length === 1 &&
    (last.partial || !last.event.terminal) &&
    !type.includes("started") &&
    !type.includes("running")
  ) {
    return "partial"
  }
  return "running"
}

function finalizeAttempt(draft: AttemptDraft): TimelineRecoveryAttempt {
  const first = draft.events[0]!
  const last = draft.events.at(-1)!
  const phase = attemptPhase(draft)
  return Object.freeze({
    id: draft.id,
    recoveryId: draft.recoveryId,
    failureId: draft.failureId,
    attempt: draft.attempt,
    strategy: draft.strategy,
    workerId: draft.workerId,
    previousWorkerId: draft.previousWorkerId,
    replacementWorkerId: draft.replacementWorkerId,
    checkpointId: draft.checkpointId,
    startEventId: first.event.eventId,
    endEventId: last.event.eventId,
    startSequence: first.order.sequence,
    endSequence: last.order.sequence,
    phase,
    reason:
      last.recovery?.reason ??
      readString(last.safeAttributes, "reason") ??
      last.summary,
    eventIds: Object.freeze(draft.events.map((facts) => facts.event.eventId)),
    duplicateEventIds: Object.freeze([...new Set(draft.duplicateEventIds)].sort()),
    partial:
      phase === "partial" ||
      draft.events.some((facts) => facts.partial) ||
      !draft.failureId,
    terminal:
      phase === "completed" || phase === "failed" || phase === "cancelled",
  })
}

function recoveryEvents(
  events: readonly TimelineEventFacts[],
): readonly TimelineEventFacts[] {
  return events.filter(
    (facts) =>
      facts.kinds.includes("failure") ||
      facts.kinds.includes("recovery") ||
      Boolean(facts.failureId) ||
      Boolean(facts.recoveryId) ||
      Boolean(facts.recovery),
  )
}

function buildAttempts(
  events: readonly TimelineEventFacts[],
): {
  attempts: AttemptDraft[]
  unmatched: string[]
} {
  const attempts: AttemptDraft[] = []
  const currentByRecovery = new Map<string, AttemptDraft>()
  const unmatched: string[] = []
  const ordered = [...recoveryEvents(events)].sort((left, right) =>
    compareTimelineOrder(left.order, right.order),
  )
  for (const facts of ordered) {
    const recoveryId = recoveryIdentity(facts)
    if (!recoveryId) {
      if (!facts.kinds.includes("failure")) {
        unmatched.push(facts.event.eventId)
      }
      continue
    }
    const identity = recoveryId
    const existing = currentByRecovery.get(identity)
    const declared = attemptNumber(facts)
    const type = facts.event.eventType.toLowerCase()
    const startsAttempt =
      type.includes("started") ||
      type.includes("planned") ||
      type.includes("retrying") ||
      type.includes("resuming")
    const existingTerminal =
      existing?.events.some((candidate) => candidate.event.terminal) ?? false
    const declaredAdvances =
      existing !== undefined && declared > existing.attempt
    if (!existing || (startsAttempt && (existingTerminal || declaredAdvances))) {
      const ordinal =
        existing === undefined
          ? Math.max(1, declared && declared < 2 ? declared : 1)
          : Math.max(existing.attempt + 1, declared)
      const created = createAttempt(facts, ordinal)
      attempts.push(created)
      currentByRecovery.set(identity, created)
      continue
    }
    addAttemptEvent(existing, facts)
  }
  attempts.sort((left, right) => {
    const leftFirst = left.events[0]!
    const rightFirst = right.events[0]!
    return (
      compareTimelineOrder(leftFirst.order, rightFirst.order) ||
      left.id.localeCompare(right.id)
    )
  })
  return { attempts, unmatched }
}

function inferFailureFromCausation(
  attempt: AttemptDraft,
  eventsById: ReadonlyMap<string, TimelineEventFacts>,
): string | undefined {
  if (attempt.failureId) return attempt.failureId
  const first = attempt.events[0]
  if (!first) return undefined
  const visited = new Set<string>()
  let current = first
  for (let depth = 0; depth < 64; depth += 1) {
    const causeId = current.event.causationId
    if (!causeId || visited.has(causeId)) break
    visited.add(causeId)
    const cause = eventsById.get(causeId)
    if (!cause) break
    const failureId = failureIdentity(cause)
    if (failureId) return failureId
    if (cause.kinds.includes("failure")) return `failure:${cause.event.eventId}`
    current = cause
  }
  return undefined
}

function buildChains(
  attempts: AttemptDraft[],
  events: readonly TimelineEventFacts[],
): ChainDraft[] {
  const eventsById = new Map(
    events.map((facts) => [facts.event.eventId, facts] as const),
  )
  const byFailure = new Map<string, ChainDraft>()
  for (const attempt of attempts) {
    attempt.failureId ??= inferFailureFromCausation(attempt, eventsById)
    const failureId =
      attempt.failureId ?? `failure:unmatched:${attempt.recoveryId}`
    let chain = byFailure.get(failureId)
    if (!chain) {
      chain = {
        id: `recovery-chain:${failureId}`,
        failureId,
        events: [],
        attempts: [],
      }
      byFailure.set(failureId, chain)
    }
    chain.attempts.push(attempt)
    chain.events.push(...attempt.events)
  }
  for (const facts of recoveryEvents(events)) {
    const failureId = failureIdentity(facts)
    if (!failureId) continue
    let chain = byFailure.get(failureId)
    if (!chain) {
      chain = {
        id: `recovery-chain:${failureId}`,
        failureId,
        events: [],
        attempts: [],
      }
      byFailure.set(failureId, chain)
    }
    if (
      !chain.events.some(
        (candidate) => candidate.event.eventId === facts.event.eventId,
      )
    ) {
      chain.events.push(facts)
    }
  }
  for (const chain of byFailure.values()) {
    chain.events.sort((left, right) =>
      compareTimelineOrder(left.order, right.order),
    )
    chain.attempts.sort(
      (left, right) =>
        left.attempt - right.attempt ||
        compareTimelineOrder(left.events[0]!.order, right.events[0]!.order) ||
        left.id.localeCompare(right.id),
    )
  }
  return [...byFailure.values()].sort((left, right) => {
    const leftFirst = left.events[0]
    const rightFirst = right.events[0]
    if (!leftFirst || !rightFirst) return left.id.localeCompare(right.id)
    return (
      compareTimelineOrder(leftFirst.order, rightFirst.order) ||
      left.id.localeCompare(right.id)
    )
  })
}

function exhausted(
  attempts: readonly TimelineRecoveryAttempt[],
  events: readonly TimelineEventFacts[],
): boolean {
  if (attempts.length === 0) {
    return events.some((facts) =>
      facts.event.eventType.toLowerCase().includes("exhausted"),
    )
  }
  const last = attempts.at(-1)!
  if (last.phase !== "failed") return false
  const explicit = events.some((facts) => {
    const type = facts.event.eventType.toLowerCase()
    const status = readString(
      facts.safeAttributes,
      "status",
      "result_status",
    )?.toLowerCase()
    return type.includes("exhausted") || status === "exhausted"
  })
  return explicit || attempts.every((attempt) => attempt.phase === "failed")
}

function chainEventIds(
  chain: ChainDraft,
  attempts: readonly TimelineRecoveryAttempt[],
): readonly string[] {
  return Object.freeze(
    [
      ...new Set([
        ...chain.events.map((facts) => facts.event.eventId),
        ...attempts.flatMap((attempt) => attempt.eventIds),
      ]),
    ].sort((left, right) => {
      const leftFacts = chain.events.find(
        (facts) => facts.event.eventId === left,
      )
      const rightFacts = chain.events.find(
        (facts) => facts.event.eventId === right,
      )
      if (!leftFacts || !rightFacts) return left.localeCompare(right)
      return compareTimelineOrder(leftFacts.order, rightFacts.order)
    }),
  )
}

function finalizeChain(
  draft: ChainDraft,
  workerEpochs: readonly TimelineWorkerEpoch[],
): TimelineRecoveryChain {
  const attempts = draft.attempts.map(finalizeAttempt)
  const eventIds = chainEventIds(draft, attempts)
  const first = draft.events[0] ?? draft.attempts[0]?.events[0]
  const last =
    draft.events.at(-1) ?? draft.attempts.at(-1)?.events.at(-1) ?? first
  const replacementWorkerIds = [
    ...new Set(
      attempts
        .map((attempt) => attempt.replacementWorkerId)
        .filter((value): value is string => Boolean(value)),
    ),
  ].sort()
  const workerIds = [
    ...new Set(
      [
        ...attempts.flatMap((attempt) => [
          attempt.workerId,
          attempt.previousWorkerId,
          attempt.replacementWorkerId,
        ]),
        ...workerEpochs
          .filter((epoch) =>
            epoch.failureIds.includes(draft.failureId),
          )
          .map((epoch) => epoch.workerId),
      ].filter((value): value is string => Boolean(value)),
    ),
  ].sort()
  const checkpointIds = [
    ...new Set(
      [
        ...attempts.map((attempt) => attempt.checkpointId),
        ...draft.events.flatMap((facts) => [
          facts.event.checkpointId,
          ...readStringArray(
            facts.safeAttributes,
            "checkpoint_ids",
            "resume_checkpoint_ids",
          ),
        ]),
      ].filter((value): value is string => Boolean(value)),
    ),
  ].sort()
  const resolved = attempts.some((attempt) => attempt.phase === "completed")
  return Object.freeze({
    id: draft.id,
    failureId: draft.failureId,
    workerIds: Object.freeze(workerIds),
    attempts: Object.freeze(attempts),
    firstEventId: first?.event.eventId ?? `missing:${draft.failureId}`,
    lastEventId: last?.event.eventId ?? `missing:${draft.failureId}`,
    startSequence: first?.order.sequence ?? 0,
    endSequence: last?.order.sequence ?? 0,
    resolved,
    exhausted: exhausted(attempts, draft.events),
    duplicateTerminalCount: attempts.reduce(
      (total, attempt) => total + attempt.duplicateEventIds.length,
      0,
    ),
    replacementWorkerIds: Object.freeze(replacementWorkerIds),
    checkpointIds: Object.freeze(checkpointIds),
    eventIds,
  })
}

export function buildTimelineRecoveryChains(
  events: readonly TimelineEventFacts[],
  workerEpochs: readonly TimelineWorkerEpoch[],
): TimelineRecoveryResult {
  const { attempts, unmatched } = buildAttempts(events)
  const drafts = buildChains(attempts, events)
  const attemptByEvent: Record<string, string> = {}
  const chainByEvent: Record<string, string> = {}
  const chains = drafts.map((draft) => {
    const chain = finalizeChain(draft, workerEpochs)
    for (const attempt of chain.attempts) {
      for (const eventId of attempt.eventIds) attemptByEvent[eventId] = attempt.id
    }
    for (const eventId of chain.eventIds) chainByEvent[eventId] = chain.id
    return chain
  })
  const duplicateRecoveryCount = chains.reduce(
    (total, chain) => total + chain.duplicateTerminalCount,
    0,
  )
  return Object.freeze({
    chains: Object.freeze(chains),
    attemptByEvent: Object.freeze(attemptByEvent),
    chainByEvent: Object.freeze(chainByEvent),
    duplicateRecoveryCount,
    unmatchedRecoveryEventIds: Object.freeze([...new Set(unmatched)].sort()),
  })
}
