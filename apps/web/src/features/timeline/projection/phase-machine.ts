import type {
  TimelineEventFacts,
  TimelinePhaseInterval,
  TimelinePhaseValue,
} from "./contracts.ts"
import { TimelinePhase } from "./contracts.ts"
import { compareTimelineOrder, phaseSeverity, readString } from "./event-reader.ts"

const TERMINAL_PHASES = new Set<TimelinePhaseValue>([
  TimelinePhase.COMPLETED,
  TimelinePhase.FAILED,
  TimelinePhase.CANCELLED,
])

const ACTIVE_PHASES = new Set<TimelinePhaseValue>([
  TimelinePhase.STARTING,
  TimelinePhase.RUNNING,
  TimelinePhase.WAITING_TOOL,
  TimelinePhase.WAITING_POLICY,
  TimelinePhase.RECOVERING,
])

const ALLOWED_TRANSITIONS: Readonly<
  Record<TimelinePhaseValue, ReadonlySet<TimelinePhaseValue>>
> = Object.freeze({
  [TimelinePhase.UNKNOWN]: new Set([
    TimelinePhase.QUEUED,
    TimelinePhase.ADMITTED,
    TimelinePhase.STARTING,
    TimelinePhase.RUNNING,
    TimelinePhase.WAITING_TOOL,
    TimelinePhase.WAITING_POLICY,
    TimelinePhase.RECOVERING,
    TimelinePhase.COMPLETED,
    TimelinePhase.FAILED,
    TimelinePhase.CANCELLED,
    TimelinePhase.UNKNOWN,
  ]),
  [TimelinePhase.QUEUED]: new Set([
    TimelinePhase.QUEUED,
    TimelinePhase.ADMITTED,
    TimelinePhase.STARTING,
    TimelinePhase.RUNNING,
    TimelinePhase.CANCELLED,
    TimelinePhase.FAILED,
  ]),
  [TimelinePhase.ADMITTED]: new Set([
    TimelinePhase.ADMITTED,
    TimelinePhase.STARTING,
    TimelinePhase.RUNNING,
    TimelinePhase.WAITING_POLICY,
    TimelinePhase.CANCELLED,
    TimelinePhase.FAILED,
  ]),
  [TimelinePhase.STARTING]: new Set([
    TimelinePhase.STARTING,
    TimelinePhase.RUNNING,
    TimelinePhase.WAITING_TOOL,
    TimelinePhase.WAITING_POLICY,
    TimelinePhase.RECOVERING,
    TimelinePhase.CANCELLED,
    TimelinePhase.FAILED,
  ]),
  [TimelinePhase.RUNNING]: new Set([
    TimelinePhase.RUNNING,
    TimelinePhase.WAITING_TOOL,
    TimelinePhase.WAITING_POLICY,
    TimelinePhase.RECOVERING,
    TimelinePhase.COMPLETED,
    TimelinePhase.FAILED,
    TimelinePhase.CANCELLED,
  ]),
  [TimelinePhase.WAITING_TOOL]: new Set([
    TimelinePhase.WAITING_TOOL,
    TimelinePhase.RUNNING,
    TimelinePhase.WAITING_POLICY,
    TimelinePhase.RECOVERING,
    TimelinePhase.COMPLETED,
    TimelinePhase.FAILED,
    TimelinePhase.CANCELLED,
  ]),
  [TimelinePhase.WAITING_POLICY]: new Set([
    TimelinePhase.WAITING_POLICY,
    TimelinePhase.RUNNING,
    TimelinePhase.WAITING_TOOL,
    TimelinePhase.RECOVERING,
    TimelinePhase.FAILED,
    TimelinePhase.CANCELLED,
  ]),
  [TimelinePhase.RECOVERING]: new Set([
    TimelinePhase.RECOVERING,
    TimelinePhase.STARTING,
    TimelinePhase.RUNNING,
    TimelinePhase.WAITING_TOOL,
    TimelinePhase.WAITING_POLICY,
    TimelinePhase.COMPLETED,
    TimelinePhase.FAILED,
    TimelinePhase.CANCELLED,
  ]),
  [TimelinePhase.COMPLETED]: new Set([
    TimelinePhase.COMPLETED,
    TimelinePhase.RECOVERING,
    TimelinePhase.STARTING,
    TimelinePhase.RUNNING,
  ]),
  [TimelinePhase.FAILED]: new Set([
    TimelinePhase.FAILED,
    TimelinePhase.RECOVERING,
    TimelinePhase.STARTING,
    TimelinePhase.RUNNING,
    TimelinePhase.CANCELLED,
  ]),
  [TimelinePhase.CANCELLED]: new Set([
    TimelinePhase.CANCELLED,
    TimelinePhase.RECOVERING,
    TimelinePhase.STARTING,
    TimelinePhase.RUNNING,
  ]),
})

export interface TimelinePhaseTransition {
  id: string
  workerId: string
  eventId: string
  sequence: number
  from: TimelinePhaseValue
  to: TimelinePhaseValue
  accepted: boolean
  corrected: boolean
  terminalRevival: boolean
  reason: string
}

export interface TimelinePhaseTrace {
  workerId: string
  workerEpochId: string
  transitions: readonly TimelinePhaseTransition[]
  intervals: readonly TimelinePhaseInterval[]
  finalPhase: TimelinePhaseValue
  invalidTransitionCount: number
  terminalRevivalCount: number
}

function eventTime(facts: TimelineEventFacts): number {
  return facts.order.committedAtMs || facts.order.createdAtMs
}

function phaseReason(facts: TimelineEventFacts): string {
  return (
    readString(
      facts.safeAttributes,
      "reason",
      "result_status",
      "status",
      "phase",
      "lifecycle",
    ) ??
    facts.summary ??
    facts.event.eventType
  )
}

function explicitLifecyclePhase(
  facts: TimelineEventFacts,
): TimelinePhaseValue | undefined {
  const value = readString(
    facts.safeAttributes,
    "phase",
    "lifecycle",
    "status",
    "result_status",
  )
    ?.toLowerCase()
    .replaceAll("_", "-")
  switch (value) {
    case "queued":
    case "pending":
    case "submitted":
      return TimelinePhase.QUEUED
    case "admitted":
    case "assigned":
    case "accepted":
      return TimelinePhase.ADMITTED
    case "starting":
    case "initializing":
    case "spawning":
      return TimelinePhase.STARTING
    case "running":
    case "active":
    case "resumed":
      return TimelinePhase.RUNNING
    case "waiting-tool":
    case "waiting-for-tool":
    case "tool-running":
      return TimelinePhase.WAITING_TOOL
    case "waiting-policy":
    case "waiting-for-policy":
    case "waiting-for-confirmation":
    case "awaiting-approval":
    case "ask":
      return TimelinePhase.WAITING_POLICY
    case "recovering":
    case "retrying":
    case "restarting":
    case "reviving":
      return TimelinePhase.RECOVERING
    case "complete":
    case "completed":
    case "done":
    case "finished":
    case "succeeded":
      return TimelinePhase.COMPLETED
    case "failed":
    case "error":
    case "unhealthy":
    case "expired":
    case "rejected":
      return TimelinePhase.FAILED
    case "cancelled":
    case "canceled":
    case "aborted":
    case "killed":
    case "stopped":
      return TimelinePhase.CANCELLED
    default:
      return undefined
  }
}

function resolvedPhase(facts: TimelineEventFacts): TimelinePhaseValue {
  if (facts.phase && facts.phase !== TimelinePhase.UNKNOWN) return facts.phase
  const explicit = explicitLifecyclePhase(facts)
  if (explicit) return explicit
  return TimelinePhase.UNKNOWN
}

function transitionAccepted(
  from: TimelinePhaseValue,
  to: TimelinePhaseValue,
): boolean {
  return ALLOWED_TRANSITIONS[from].has(to)
}

function correctionPhase(
  from: TimelinePhaseValue,
  requested: TimelinePhaseValue,
  facts: TimelineEventFacts,
): TimelinePhaseValue {
  if (requested === TimelinePhase.UNKNOWN) return from
  if (TERMINAL_PHASES.has(from)) {
    if (
      requested === TimelinePhase.RECOVERING ||
      facts.kinds.includes("recovery") ||
      facts.event.eventType.toLowerCase().includes("revive") ||
      facts.event.eventType.toLowerCase().includes("restart")
    ) {
      return TimelinePhase.RECOVERING
    }
    if (facts.event.terminal) return from
  }
  if (
    from === TimelinePhase.QUEUED &&
    requested === TimelinePhase.WAITING_TOOL
  ) {
    return TimelinePhase.STARTING
  }
  if (
    from === TimelinePhase.ADMITTED &&
    requested === TimelinePhase.COMPLETED &&
    !facts.event.terminal
  ) {
    return TimelinePhase.RUNNING
  }
  if (
    ACTIVE_PHASES.has(from) &&
    requested === TimelinePhase.QUEUED &&
    facts.order.sequence > 0
  ) {
    return from
  }
  return requested
}

function intervalId(
  workerEpochId: string,
  phase: TimelinePhaseValue,
  startEventId: string,
): string {
  return `${workerEpochId}:${phase}:${startEventId}`
}

function finalizeInterval(
  workerId: string,
  workerEpochId: string,
  phase: TimelinePhaseValue,
  facts: readonly TimelineEventFacts[],
): TimelinePhaseInterval {
  const start = facts[0]!
  const end = facts.at(-1)!
  const startMs = eventTime(start)
  const endMs = Math.max(startMs, eventTime(end))
  return Object.freeze({
    id: intervalId(workerEpochId, phase, start.event.eventId),
    workerId,
    workerEpochId,
    phase,
    startEventId: start.event.eventId,
    endEventId: end.event.eventId,
    startSequence: start.order.sequence,
    endSequence: end.order.sequence,
    startedAt: start.event.committedAt,
    endedAt: end.event.committedAt,
    durationMs: Math.max(0, endMs - startMs),
    terminal: TERMINAL_PHASES.has(phase) || end.event.terminal,
    eventIds: Object.freeze(facts.map((item) => item.event.eventId)),
    reason: phaseReason(end),
  })
}

function chooseSameSequencePhase(
  current: TimelinePhaseValue,
  candidate: TimelinePhaseValue,
): TimelinePhaseValue {
  if (current === candidate) return current
  const currentTerminal = TERMINAL_PHASES.has(current)
  const candidateTerminal = TERMINAL_PHASES.has(candidate)
  if (currentTerminal !== candidateTerminal) {
    return candidateTerminal ? candidate : current
  }
  return phaseSeverity(candidate) > phaseSeverity(current)
    ? candidate
    : current
}

export function buildWorkerPhaseTrace(
  workerId: string,
  workerEpochId: string,
  input: readonly TimelineEventFacts[],
): TimelinePhaseTrace {
  const events = [...input].sort((left, right) =>
    compareTimelineOrder(left.order, right.order),
  )
  const transitions: TimelinePhaseTransition[] = []
  const intervalFacts: Array<{
    phase: TimelinePhaseValue
    facts: TimelineEventFacts[]
  }> = []
  let current = TimelinePhase.UNKNOWN as TimelinePhaseValue
  let previousSequence = -1
  let invalidTransitionCount = 0
  let terminalRevivalCount = 0
  for (const facts of events) {
    const requested = resolvedPhase(facts)
    const sameSequence = previousSequence === facts.order.sequence
    let candidate = sameSequence
      ? chooseSameSequencePhase(current, requested)
      : requested
    let accepted = transitionAccepted(current, candidate)
    let corrected = false
    if (!accepted) {
      const correctedValue = correctionPhase(current, candidate, facts)
      corrected = correctedValue !== candidate
      candidate = correctedValue
      accepted = transitionAccepted(current, candidate)
      if (!accepted) {
        invalidTransitionCount += 1
        candidate = current
      }
    }
    const terminalRevival =
      TERMINAL_PHASES.has(current) &&
      (candidate === TimelinePhase.RECOVERING ||
        candidate === TimelinePhase.STARTING ||
        candidate === TimelinePhase.RUNNING)
    if (terminalRevival && candidate !== current) terminalRevivalCount += 1
    transitions.push(
      Object.freeze({
        id: `${workerEpochId}:${facts.event.eventId}`,
        workerId,
        eventId: facts.event.eventId,
        sequence: facts.order.sequence,
        from: current,
        to: candidate,
        accepted,
        corrected,
        terminalRevival,
        reason: phaseReason(facts),
      }),
    )
    const last = intervalFacts.at(-1)
    if (!last || last.phase !== candidate) {
      intervalFacts.push({ phase: candidate, facts: [facts] })
    } else {
      last.facts.push(facts)
    }
    current = candidate
    previousSequence = facts.order.sequence
  }
  const intervals = intervalFacts.map((interval) =>
    finalizeInterval(workerId, workerEpochId, interval.phase, interval.facts),
  )
  return Object.freeze({
    workerId,
    workerEpochId,
    transitions: Object.freeze(transitions),
    intervals: Object.freeze(intervals),
    finalPhase: current,
    invalidTransitionCount,
    terminalRevivalCount,
  })
}

export function phaseAtEvent(
  trace: TimelinePhaseTrace,
  eventId: string,
): TimelinePhaseValue {
  return (
    trace.transitions.find((transition) => transition.eventId === eventId)?.to ??
    TimelinePhase.UNKNOWN
  )
}

export function phaseAtSequence(
  trace: TimelinePhaseTrace,
  sequence: number,
): TimelinePhaseValue {
  let result = TimelinePhase.UNKNOWN as TimelinePhaseValue
  for (const transition of trace.transitions) {
    if (transition.sequence > sequence) break
    result = transition.to
  }
  return result
}

export function terminalPhase(phase: TimelinePhaseValue): boolean {
  return TERMINAL_PHASES.has(phase)
}

export function activePhase(phase: TimelinePhaseValue): boolean {
  return ACTIVE_PHASES.has(phase)
}

export function phaseLabel(phase: TimelinePhaseValue): string {
  switch (phase) {
    case TimelinePhase.WAITING_TOOL:
      return "Waiting for tool"
    case TimelinePhase.WAITING_POLICY:
      return "Waiting for policy"
    case TimelinePhase.RECOVERING:
      return "Recovering"
    case TimelinePhase.COMPLETED:
      return "Completed"
    case TimelinePhase.CANCELLED:
      return "Cancelled"
    case TimelinePhase.FAILED:
      return "Failed"
    case TimelinePhase.STARTING:
      return "Starting"
    case TimelinePhase.ADMITTED:
      return "Admitted"
    case TimelinePhase.QUEUED:
      return "Queued"
    case TimelinePhase.RUNNING:
      return "Running"
    default:
      return "Unknown"
  }
}

export function phaseProgress(phase: TimelinePhaseValue): number {
  switch (phase) {
    case TimelinePhase.QUEUED:
      return 0.05
    case TimelinePhase.ADMITTED:
      return 0.12
    case TimelinePhase.STARTING:
      return 0.2
    case TimelinePhase.RUNNING:
      return 0.5
    case TimelinePhase.WAITING_TOOL:
      return 0.55
    case TimelinePhase.WAITING_POLICY:
      return 0.45
    case TimelinePhase.RECOVERING:
      return 0.4
    case TimelinePhase.COMPLETED:
      return 1
    case TimelinePhase.FAILED:
    case TimelinePhase.CANCELLED:
      return 0
    default:
      return 0
  }
}
