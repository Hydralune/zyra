import type {
  CommandCorrelation,
  CommandReceipt,
} from "./contracts.ts"

export interface CommandCausalEvent {
  eventId: string
  taskId: string
  runId: string
  controlCommandId?: string
  correlationId?: string
  causationId?: string
  spanId?: string
  parentSpanId?: string
  toolCallId?: string
  checkpointId?: string
  mutationId?: string
  failureId?: string
  recoveryId?: string
  artifactIds?: readonly string[]
  sequence?: number
  eventType?: string
  terminal?: boolean
  effective?: boolean
}

function unique(values: readonly (string | undefined)[]): string[] {
  return [...new Set(values.filter((value): value is string => Boolean(value)))]
}

function eventMatches(
  event: CommandCausalEvent,
  receipt: CommandReceipt,
  known: ReadonlySet<string>,
): boolean {
  if (event.taskId !== receipt.taskId || event.runId !== receipt.runId) {
    return false
  }
  if (event.controlCommandId === receipt.commandId) return true
  if (event.correlationId === receipt.requestId) return true
  if (event.causationId === receipt.commandId) return true
  if (known.has(event.eventId)) return true
  if (event.causationId && known.has(event.causationId)) return true
  if (event.parentSpanId && known.has(event.parentSpanId)) return true
  return false
}

export function correlateCommand(
  receipt: CommandReceipt,
  events: readonly CommandCausalEvent[],
): CommandCorrelation {
  const selected = new Map<string, CommandCausalEvent>()
  const known = new Set<string>([
    receipt.commandId,
    receipt.requestId,
    ...receipt.eventIds,
  ])
  let changed = true
  while (changed) {
    changed = false
    for (const event of events) {
      if (selected.has(event.eventId)) continue
      if (!eventMatches(event, receipt, known)) continue
      selected.set(event.eventId, event)
      known.add(event.eventId)
      if (event.spanId) known.add(event.spanId)
      if (event.mutationId) known.add(event.mutationId)
      if (event.checkpointId) known.add(event.checkpointId)
      changed = true
    }
  }
  const values = [...selected.values()].sort(
    (left, right) =>
      (left.sequence ?? 0) - (right.sequence ?? 0) ||
      left.eventId.localeCompare(right.eventId),
  )
  const eventIds = unique([
    ...receipt.eventIds,
    ...values.map((event) => event.eventId),
  ])
  const spanIds = unique(values.flatMap((event) => [event.spanId, event.parentSpanId]))
  const mutationIds = unique(values.map((event) => event.mutationId))
  const checkpointIds = unique([
    receipt.checkpointRef,
    ...values.map((event) => event.checkpointId),
  ])
  const artifactIds = unique(values.flatMap((event) => [...(event.artifactIds ?? [])]))
  const toolCallIds = unique(values.map((event) => event.toolCallId))
  const failureIds = unique(values.map((event) => event.failureId))
  const recoveryIds = unique(values.map((event) => event.recoveryId))
  const missing: string[] = []
  if (!eventIds.length) missing.push("event")
  if (
    receipt.phase === "applied" &&
    !values.some((event) => event.effective !== false)
  ) {
    missing.push("effective-event")
  }
  if (
    receipt.phase === "applied" &&
    !receipt.checkpointRef &&
    !mutationIds.length &&
    !checkpointIds.length &&
    !artifactIds.length &&
    !toolCallIds.length
  ) {
    missing.push("state-effect")
  }
  if (
    ["/inject", "/change", "/verify", "/eval"].includes(receipt.name) &&
    receipt.phase === "applied" &&
    !mutationIds.length &&
    !failureIds.length &&
    !recoveryIds.length &&
    !checkpointIds.length
  ) {
    missing.push("task-graph-effect")
  }
  return {
    commandId: receipt.commandId,
    requestId: receipt.requestId,
    queueId: receipt.queueId,
    eventIds,
    spanIds,
    mutationIds,
    checkpointIds,
    artifactIds,
    toolCallIds,
    failureIds,
    recoveryIds,
    complete: missing.length === 0,
    missing,
  }
}

export function commandEvents(
  commandId: string,
  events: readonly CommandCausalEvent[],
): CommandCausalEvent[] {
  const selected = new Map<string, CommandCausalEvent>()
  const known = new Set([commandId])
  let changed = true
  while (changed) {
    changed = false
    for (const event of events) {
      if (selected.has(event.eventId)) continue
      if (
        event.controlCommandId !== commandId &&
        event.causationId !== commandId &&
        !known.has(event.correlationId ?? "") &&
        !known.has(event.causationId ?? "") &&
        !known.has(event.parentSpanId ?? "")
      ) {
        continue
      }
      selected.set(event.eventId, event)
      known.add(event.eventId)
      if (event.spanId) known.add(event.spanId)
      changed = true
    }
  }
  return [...selected.values()].sort(
    (left, right) =>
      (left.sequence ?? 0) - (right.sequence ?? 0) ||
      left.eventId.localeCompare(right.eventId),
  )
}

export function correlationAudit(
  correlations: readonly CommandCorrelation[],
): {
  complete: number
  incomplete: number
  missing: Readonly<Record<string, number>>
} {
  const missing: Record<string, number> = {}
  for (const correlation of correlations) {
    for (const key of correlation.missing) {
      missing[key] = (missing[key] ?? 0) + 1
    }
  }
  return {
    complete: correlations.filter((value) => value.complete).length,
    incomplete: correlations.filter((value) => !value.complete).length,
    missing,
  }
}
