import type {
  TimelineBrowserStep,
  TimelineEventFacts,
} from "./contracts.ts"
import { TimelinePhase } from "./contracts.ts"
import {
  compareTimelineOrder,
  readString,
  readStringArray,
} from "./event-reader.ts"

interface BrowserStepDraft {
  id: string
  workerId?: string
  events: TimelineEventFacts[]
  actionEventIds: string[]
  resultEventIds: string[]
  stateEventIds: string[]
  actionNames: string[]
  artifactIds: string[]
  errors: string[]
}

export interface BrowserStepProjectionResult {
  steps: readonly TimelineBrowserStep[]
  stepIdByEvent: Readonly<Record<string, string>>
  unmatchedActionEventIds: readonly string[]
  unmatchedResultEventIds: readonly string[]
}

function browserCategory(
  facts: TimelineEventFacts,
): "action" | "result" | "state" | "other" {
  const type = facts.event.eventType.toLowerCase()
  const action = readString(
    facts.safeAttributes,
    "action",
    "action_name",
    "tool_name",
  )
  const status = readString(
    facts.safeAttributes,
    "result_status",
    "status",
  )?.toLowerCase()
  if (
    type.includes("action.result") ||
    type.includes("action.completed") ||
    type.includes("action.failed") ||
    type.includes("observation") ||
    type.includes("browser.result")
  ) {
    return "result"
  }
  if (
    type.includes("snapshot") ||
    type.includes("dom.") ||
    type.includes("page.state") ||
    type.includes("browser.state") ||
    type.includes("screenshot")
  ) {
    return "state"
  }
  if (
    action ||
    type.includes("action") ||
    type.includes("navigate") ||
    type.includes("click") ||
    type.includes("type") ||
    type.includes("scroll") ||
    type.includes("browser.tool")
  ) {
    return "action"
  }
  if (status === "completed" || status === "failed") return "result"
  return "other"
}

function explicitStepId(facts: TimelineEventFacts): string | undefined {
  return (
    facts.browserStepId ??
    readString(
      facts.safeAttributes,
      "browser_step_id",
      "step_id",
      "action_id",
    )
  )
}

function actionIdentity(facts: TimelineEventFacts): string | undefined {
  return readString(
    facts.safeAttributes,
    "action_id",
    "tool_call_id",
  ) ?? facts.event.toolCallId
}

function inferredStepId(
  facts: TimelineEventFacts,
  byAction: ReadonlyMap<string, string>,
): string {
  const explicit = explicitStepId(facts)
  if (explicit) return explicit
  const actionId = actionIdentity(facts)
  if (actionId && byAction.has(actionId)) return byAction.get(actionId)!
  if (facts.event.causationId) return `browser-step:${facts.event.causationId}`
  if (facts.event.spanId) return `browser-step:span:${facts.event.spanId}`
  if (facts.event.correlationId) {
    return `browser-step:correlation:${facts.event.correlationId}:${Math.floor(
      facts.order.sequence / 8,
    )}`
  }
  return `browser-step:event:${facts.event.eventId}`
}

function createDraft(id: string, facts: TimelineEventFacts): BrowserStepDraft {
  return {
    id,
    workerId: facts.event.workerId ?? facts.worker?.id,
    events: [],
    actionEventIds: [],
    resultEventIds: [],
    stateEventIds: [],
    actionNames: [],
    artifactIds: [],
    errors: [],
  }
}

function actionNames(facts: TimelineEventFacts): readonly string[] {
  const names = new Set<string>()
  const direct = readString(
    facts.safeAttributes,
    "action_name",
    "action",
    "tool_name",
  )
  if (direct) names.add(direct)
  for (const name of readStringArray(
    facts.safeAttributes,
    "action_names",
    "actions",
  )) {
    names.add(name)
  }
  if (!names.size && browserCategory(facts) === "action") {
    const tokens = facts.event.eventType.split(".")
    const fallback = tokens.at(-1)
    if (fallback && fallback !== "started" && fallback !== "requested") {
      names.add(fallback)
    }
  }
  return Object.freeze([...names])
}

function errorText(facts: TimelineEventFacts): string | undefined {
  const direct = readString(
    facts.safeAttributes,
    "error_code",
    "reason",
    "result",
  )
  if (
    facts.phase === TimelinePhase.FAILED ||
    facts.event.eventType.toLowerCase().includes("failed") ||
    facts.tool?.errorCode
  ) {
    return facts.tool?.errorCode ?? direct ?? facts.summary
  }
  return undefined
}

function addEvent(draft: BrowserStepDraft, facts: TimelineEventFacts): void {
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
  const category = browserCategory(facts)
  if (category === "action") draft.actionEventIds.push(facts.event.eventId)
  if (category === "result") draft.resultEventIds.push(facts.event.eventId)
  if (category === "state") draft.stateEventIds.push(facts.event.eventId)
  for (const name of actionNames(facts)) {
    if (!draft.actionNames.includes(name)) draft.actionNames.push(name)
  }
  for (const artifact of facts.artifacts) {
    if (!draft.artifactIds.includes(artifact.id)) {
      draft.artifactIds.push(artifact.id)
    }
  }
  const error = errorText(facts)
  if (error && !draft.errors.includes(error)) draft.errors.push(error)
}

function status(
  draft: BrowserStepDraft,
): TimelineBrowserStep["status"] {
  if (draft.errors.length) return "failed"
  const last = draft.events.at(-1)
  if (!last) return "partial"
  if (
    draft.resultEventIds.length &&
    (last.event.terminal ||
      last.phase === TimelinePhase.COMPLETED ||
      last.event.eventType.toLowerCase().includes("completed"))
  ) {
    return "completed"
  }
  if (draft.actionEventIds.length && !draft.resultEventIds.length) {
    return last.event.terminal ? "partial" : "running"
  }
  if (draft.resultEventIds.length && !draft.actionEventIds.length) {
    return "partial"
  }
  return last.event.terminal ? "completed" : "running"
}

function finalize(draft: BrowserStepDraft): TimelineBrowserStep {
  const first = draft.events[0]!
  const last = draft.events.at(-1)!
  const currentStatus = status(draft)
  return Object.freeze({
    id: draft.id,
    workerId: draft.workerId,
    actionEventIds: Object.freeze([...new Set(draft.actionEventIds)]),
    resultEventIds: Object.freeze([...new Set(draft.resultEventIds)]),
    stateEventIds: Object.freeze([...new Set(draft.stateEventIds)]),
    artifactIds: Object.freeze([...new Set(draft.artifactIds)].sort()),
    actionNames: Object.freeze([...new Set(draft.actionNames)]),
    startEventId: first.event.eventId,
    endEventId: last.event.eventId,
    startSequence: first.order.sequence,
    endSequence: last.order.sequence,
    status: currentStatus,
    error: draft.errors[0],
    partial:
      currentStatus === "partial" ||
      !draft.actionEventIds.length ||
      !draft.resultEventIds.length ||
      draft.events.some((facts) => facts.partial),
  })
}

function isBrowserEvent(facts: TimelineEventFacts): boolean {
  return (
    facts.kinds.includes("browser-step") ||
    Boolean(facts.browserStepId) ||
    Boolean(readString(facts.safeAttributes, "action_id", "browser_step_id"))
  )
}

export function buildBrowserSteps(
  input: readonly TimelineEventFacts[],
): BrowserStepProjectionResult {
  const events = input
    .filter(isBrowserEvent)
    .sort((left, right) => compareTimelineOrder(left.order, right.order))
  const byAction = new Map<string, string>()
  for (const facts of events) {
    const id = explicitStepId(facts)
    const actionId = actionIdentity(facts)
    if (id && actionId) byAction.set(actionId, id)
  }
  const drafts = new Map<string, BrowserStepDraft>()
  const stepIdByEvent: Record<string, string> = {}
  for (const facts of events) {
    const id = inferredStepId(facts, byAction)
    let draft = drafts.get(id)
    if (!draft) {
      draft = createDraft(id, facts)
      drafts.set(id, draft)
    }
    addEvent(draft, facts)
    stepIdByEvent[facts.event.eventId] = id
    const actionId = actionIdentity(facts)
    if (actionId) byAction.set(actionId, id)
  }
  const steps = [...drafts.values()]
    .map(finalize)
    .sort(
      (left, right) =>
        left.startSequence - right.startSequence ||
        left.id.localeCompare(right.id),
    )
  const unmatchedActionEventIds = steps
    .filter((step) => step.actionEventIds.length && !step.resultEventIds.length)
    .flatMap((step) => step.actionEventIds)
  const unmatchedResultEventIds = steps
    .filter((step) => step.resultEventIds.length && !step.actionEventIds.length)
    .flatMap((step) => step.resultEventIds)
  return Object.freeze({
    steps: Object.freeze(steps),
    stepIdByEvent: Object.freeze(stepIdByEvent),
    unmatchedActionEventIds: Object.freeze(unmatchedActionEventIds),
    unmatchedResultEventIds: Object.freeze(unmatchedResultEventIds),
  })
}

export function browserStepEventIds(
  step: TimelineBrowserStep,
): readonly string[] {
  return Object.freeze([
    ...new Set([
      ...step.actionEventIds,
      ...step.resultEventIds,
      ...step.stateEventIds,
    ]),
  ])
}

export function browserStepLabel(step: TimelineBrowserStep): string {
  if (step.actionNames.length === 0) return "Browser step"
  if (step.actionNames.length === 1) return step.actionNames[0]!
  return `${step.actionNames[0]} +${step.actionNames.length - 1}`
}
