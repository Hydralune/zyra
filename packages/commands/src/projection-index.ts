import type {
  CommandCorrelation,
  CommandQueuePhase,
  CommandReceipt,
} from "./contracts.ts"
import type { ProjectedCommandLike } from "./queue.ts"

export interface CommandProjectionRecord extends ProjectedCommandLike {
  status?: string
  scope?: string
  mode?: string
  summary?: string
  title?: string
  terminal?: boolean
  revision?: number
  aggregateSequence?: number
  firstEventId?: string
  lastEventId?: string
  causationId?: string
  spanId?: string
  parentSpanId?: string
  toolCallId?: string
  artifactIds?: readonly string[]
  checkpointId?: string
  attributes?: Readonly<Record<string, unknown>>
  metadata?: Readonly<Record<string, unknown>>
}

export interface IndexedCommandCausalEvent {
  eventId: string
  eventType?: string
  taskId: string
  runId?: string
  sessionId?: string
  correlationId?: string
  causationId?: string
  spanId?: string
  parentSpanId?: string
  toolCallId?: string
  artifactIds?: readonly string[]
  checkpointId?: string
  controlCommandId?: string
  mutationId?: string
  failureId?: string
  recoveryId?: string
  sequence?: number
  aggregateSequence?: number
  createdAt?: string
  committedAt?: string
  summary?: string
  terminal?: boolean
  effective?: boolean
  entityRefs?: readonly string[]
}

export interface CommandProjectionQuery {
  taskId?: string
  runId?: string
  sessionId?: string
  names?: readonly string[]
  phases?: readonly CommandQueuePhase[]
  terminal?: boolean
  text?: string
  afterSequence?: number
  beforeSequence?: number
  limit?: number
}

export interface CommandProjectionTrace {
  command: CommandProjectionRecord
  events: readonly IndexedCommandCausalEvent[]
  correlation: CommandCorrelation
  roots: readonly string[]
  leaves: readonly string[]
  missingParents: readonly string[]
  complete: boolean
}

export interface CommandProjectionIndexAudit {
  revision: number
  commands: number
  events: number
  taskBuckets: number
  identityBuckets: number
  duplicateCommandIds: readonly string[]
  duplicateEventIds: readonly string[]
  orphanEventIds: readonly string[]
  commandsWithoutEvents: readonly string[]
  eventsWithoutCommands: readonly string[]
}

type IndexMap = Map<string, Set<string>>

function normalized(value: unknown): string {
  return typeof value === "string" ? value.trim() : ""
}

function keys(values: readonly unknown[]): string[] {
  return [...new Set(values.map(normalized).filter(Boolean))]
}

function add(index: IndexMap, key: unknown, value: string): void {
  const identity = normalized(key)
  if (!identity) return
  const bucket = index.get(identity) ?? new Set<string>()
  bucket.add(value)
  index.set(identity, bucket)
}

function values(index: IndexMap, key: unknown): string[] {
  const bucket = index.get(normalized(key))
  return bucket ? [...bucket] : []
}

function queuePhase(value: string): CommandQueuePhase {
  const candidate = value.trim().toLowerCase()
  if (candidate === "reserved") return "reserved"
  if (["running", "started", "dispatched"].includes(candidate)) return "running"
  if (["completed", "succeeded", "applied", "committed"].includes(candidate)) {
    return "completed"
  }
  if (["failed", "rejected", "denied", "conflict"].includes(candidate)) {
    return "failed"
  }
  if (["cancelled", "canceled"].includes(candidate)) return "cancelled"
  if (["expired", "timeout", "timed_out"].includes(candidate)) return "expired"
  return "queued"
}

function terminalPhase(value: CommandQueuePhase): boolean {
  return ["completed", "failed", "cancelled", "expired"].includes(value)
}

function eventOrder(
  left: IndexedCommandCausalEvent,
  right: IndexedCommandCausalEvent,
): number {
  const aggregate =
    Number(left.aggregateSequence ?? 0) -
    Number(right.aggregateSequence ?? 0)
  if (aggregate) return aggregate
  const sequence = Number(left.sequence ?? 0) - Number(right.sequence ?? 0)
  if (sequence) return sequence
  const time =
    Date.parse(left.committedAt ?? left.createdAt ?? "") -
    Date.parse(right.committedAt ?? right.createdAt ?? "")
  if (Number.isFinite(time) && time) return time
  return left.eventId.localeCompare(right.eventId)
}

function commandOrder(
  left: CommandProjectionRecord,
  right: CommandProjectionRecord,
): number {
  const aggregate =
    Number(right.aggregateSequence ?? 0) -
    Number(left.aggregateSequence ?? 0)
  if (aggregate) return aggregate
  const sequence = Number(right.sequence ?? 0) - Number(left.sequence ?? 0)
  if (sequence) return sequence
  const time =
    Date.parse(right.updatedAt ?? "") -
    Date.parse(left.updatedAt ?? "")
  if (Number.isFinite(time) && time) return time
  return right.id.localeCompare(left.id)
}

function copyCommand(value: CommandProjectionRecord): CommandProjectionRecord {
  return Object.freeze({
    ...value,
    artifactIds: Object.freeze([...(value.artifactIds ?? [])]),
    attributes: Object.freeze({ ...(value.attributes ?? {}) }),
    metadata: Object.freeze({ ...(value.metadata ?? {}) }),
  })
}

function copyEvent(value: IndexedCommandCausalEvent): IndexedCommandCausalEvent {
  return Object.freeze({
    ...value,
    artifactIds: Object.freeze([...(value.artifactIds ?? [])]),
    entityRefs: Object.freeze([...(value.entityRefs ?? [])]),
  })
}

function identityValues(command: CommandProjectionRecord): string[] {
  const attributes = command.attributes ?? {}
  const metadata = command.metadata ?? {}
  return keys([
    command.id,
    command.controlCommandId,
    command.correlationId,
    command.receiptId,
    command.firstEventId,
    command.lastEventId,
    attributes.request_id,
    attributes.requestId,
    attributes.command_id,
    attributes.commandId,
    attributes.queue_id,
    attributes.queueId,
    metadata.request_id,
    metadata.requestId,
    metadata.command_id,
    metadata.commandId,
    metadata.queue_id,
    metadata.queueId,
  ])
}

function commandText(command: CommandProjectionRecord): string {
  return [
    command.commandName,
    command.title,
    command.summary,
    command.scope,
    command.mode,
    command.errorCode,
  ].map(normalized).join(" ").toLowerCase()
}

function eventTouchesCommand(
  event: IndexedCommandCausalEvent,
  command: CommandProjectionRecord,
): boolean {
  const identities = new Set(identityValues(command))
  if (keys([
    event.controlCommandId,
    event.correlationId,
    event.eventId,
    event.causationId,
  ]).some((value) => identities.has(value))) {
    return true
  }
  return (event.entityRefs ?? []).some((value) => identities.has(value))
}

export class CommandProjectionIndex {
  readonly #commands = new Map<string, CommandProjectionRecord>()
  readonly #events = new Map<string, IndexedCommandCausalEvent>()
  readonly #commandIdentities: IndexMap = new Map()
  readonly #commandsByTask: IndexMap = new Map()
  readonly #commandsByRun: IndexMap = new Map()
  readonly #commandsBySession: IndexMap = new Map()
  readonly #eventsByCommand: IndexMap = new Map()
  readonly #eventsByCorrelation: IndexMap = new Map()
  readonly #eventsBySpan: IndexMap = new Map()
  readonly #eventsByMutation: IndexMap = new Map()
  readonly #eventsByCheckpoint: IndexMap = new Map()
  readonly #eventsByArtifact: IndexMap = new Map()
  readonly #eventsByToolCall: IndexMap = new Map()
  readonly #eventsByFailure: IndexMap = new Map()
  readonly #eventsByRecovery: IndexMap = new Map()
  readonly #listeners = new Set<() => void>()
  #duplicateCommandIds: string[] = []
  #duplicateEventIds: string[] = []
  #revision = 0
  #closed = false

  get revision(): number {
    return this.#revision
  }

  subscribe = (listener: () => void): (() => void) => {
    this.#assertOpen()
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  replace(
    commandValues: readonly CommandProjectionRecord[],
    eventValues: readonly IndexedCommandCausalEvent[],
  ): void {
    this.#assertOpen()
    this.#clearIndexes()
    const duplicateCommands = new Set<string>()
    for (const source of commandValues) {
      const id = normalized(source.id)
      const taskId = normalized(source.taskId)
      const runId = normalized(source.runId)
      if (!id || !taskId || !runId) continue
      if (this.#commands.has(id)) duplicateCommands.add(id)
      const command = copyCommand({
        ...source,
        id,
        taskId,
        runId,
        sessionId: normalized(source.sessionId) || undefined,
      })
      this.#commands.set(id, command)
    }
    const duplicateEvents = new Set<string>()
    for (const source of eventValues) {
      const eventId = normalized(source.eventId)
      const taskId = normalized(source.taskId)
      if (!eventId || !taskId) continue
      if (this.#events.has(eventId)) duplicateEvents.add(eventId)
      this.#events.set(eventId, copyEvent({ ...source, eventId, taskId }))
    }
    this.#duplicateCommandIds = [...duplicateCommands].sort()
    this.#duplicateEventIds = [...duplicateEvents].sort()
    this.#buildIndexes()
    this.#revision += 1
    this.#emit()
  }

  commands(query: CommandProjectionQuery = {}): CommandProjectionRecord[] {
    this.#assertOpen()
    let candidates: CommandProjectionRecord[]
    if (query.taskId) {
      candidates = values(this.#commandsByTask, query.taskId)
        .map((id) => this.#commands.get(id))
        .filter((value): value is CommandProjectionRecord => Boolean(value))
    } else if (query.runId) {
      candidates = values(this.#commandsByRun, query.runId)
        .map((id) => this.#commands.get(id))
        .filter((value): value is CommandProjectionRecord => Boolean(value))
    } else if (query.sessionId) {
      candidates = values(this.#commandsBySession, query.sessionId)
        .map((id) => this.#commands.get(id))
        .filter((value): value is CommandProjectionRecord => Boolean(value))
    } else {
      candidates = [...this.#commands.values()]
    }
    const names = new Set((query.names ?? []).map((value) => value.toLowerCase()))
    const phases = new Set(query.phases ?? [])
    const text = normalized(query.text).toLowerCase()
    const after = query.afterSequence ?? Number.NEGATIVE_INFINITY
    const before = query.beforeSequence ?? Number.POSITIVE_INFINITY
    const limit = Math.max(1, Math.min(5000, Math.floor(query.limit ?? 500)))
    return candidates
      .filter((command) => !query.runId || command.runId === query.runId)
      .filter((command) => !query.sessionId || command.sessionId === query.sessionId)
      .filter((command) =>
        !names.size || names.has(normalized(command.commandName).toLowerCase()))
      .filter((command) => {
        const phase = queuePhase(command.lifecycle || command.status || "")
        return !phases.size || phases.has(phase)
      })
      .filter((command) => {
        if (query.terminal === undefined) return true
        const phase = queuePhase(command.lifecycle || command.status || "")
        return terminalPhase(phase) === query.terminal
      })
      .filter((command) => {
        const sequence = Number(command.sequence ?? 0)
        return sequence > after && sequence < before
      })
      .filter((command) => !text || commandText(command).includes(text))
      .sort(commandOrder)
      .slice(0, limit)
      .map(copyCommand)
  }

  queueForTask(taskId: string): ProjectedCommandLike[] {
    return this.commands({ taskId, limit: 5000 }).map((command) => ({
      id: command.id,
      taskId: command.taskId,
      runId: command.runId,
      sessionId: command.sessionId,
      lifecycle: command.lifecycle || command.status || "queued",
      priority: command.priority,
      queuePosition: command.queuePosition,
      commandName: command.commandName,
      controlCommandId: command.controlCommandId,
      correlationId: command.correlationId,
      createdAt: command.createdAt,
      updatedAt: command.updatedAt,
      sequence: command.sequence,
      errorCode: command.errorCode,
      receiptId: command.receiptId,
    }))
  }

  find(identity: string): CommandProjectionRecord | undefined {
    this.#assertOpen()
    const normalizedIdentity = normalized(identity)
    if (!normalizedIdentity) return undefined
    const direct = this.#commands.get(normalizedIdentity)
    if (direct) return copyCommand(direct)
    const ids = values(this.#commandIdentities, normalizedIdentity)
    const selected = ids
      .map((id) => this.#commands.get(id))
      .filter((value): value is CommandProjectionRecord => Boolean(value))
      .sort(commandOrder)[0]
    return selected ? copyCommand(selected) : undefined
  }

  trace(identity: string): CommandProjectionTrace | undefined {
    const command = this.find(identity)
    if (!command) return undefined
    const eventIds = this.#eventIds(command)
    const events = eventIds
      .map((eventId) => this.#events.get(eventId))
      .filter((value): value is IndexedCommandCausalEvent => Boolean(value))
      .sort(eventOrder)
      .map(copyEvent)
    const eventSet = new Set(events.map((event) => event.eventId))
    const roots: string[] = []
    const leaves = new Set(eventSet)
    const missingParents = new Set<string>()
    for (const event of events) {
      const parent = normalized(event.causationId)
      if (!parent) {
        roots.push(event.eventId)
      } else if (eventSet.has(parent)) {
        leaves.delete(parent)
      } else {
        missingParents.add(parent)
        roots.push(event.eventId)
      }
    }
    const correlation = this.#correlation(command, events)
    return Object.freeze({
      command,
      events: Object.freeze(events),
      correlation,
      roots: Object.freeze([...new Set(roots)]),
      leaves: Object.freeze([...leaves]),
      missingParents: Object.freeze([...missingParents]),
      complete: correlation.complete && missingParents.size === 0,
    })
  }

  traceReceipt(receipt: CommandReceipt): CommandProjectionTrace | undefined {
    const identities = [
      receipt.commandId,
      receipt.requestId,
      receipt.queueId,
      ...receipt.eventIds,
    ].filter(Boolean) as string[]
    for (const identity of identities) {
      const found = this.trace(identity)
      if (found) return found
    }
    return undefined
  }

  audit(): CommandProjectionIndexAudit {
    this.#assertOpen()
    const commandsWithoutEvents: string[] = []
    for (const command of this.#commands.values()) {
      if (!this.#eventIds(command).length) commandsWithoutEvents.push(command.id)
    }
    const eventsWithoutCommands: string[] = []
    const orphanEventIds: string[] = []
    const eventIds = new Set(this.#events.keys())
    for (const event of this.#events.values()) {
      const related = values(this.#eventsByCommand, event.eventId)
      if (
        event.controlCommandId &&
        !this.find(event.controlCommandId)
      ) {
        eventsWithoutCommands.push(event.eventId)
      }
      if (
        event.causationId &&
        !eventIds.has(event.causationId) &&
        !related.length
      ) {
        orphanEventIds.push(event.eventId)
      }
    }
    return Object.freeze({
      revision: this.#revision,
      commands: this.#commands.size,
      events: this.#events.size,
      taskBuckets: this.#commandsByTask.size,
      identityBuckets: this.#commandIdentities.size,
      duplicateCommandIds: Object.freeze([...this.#duplicateCommandIds]),
      duplicateEventIds: Object.freeze([...this.#duplicateEventIds]),
      orphanEventIds: Object.freeze([...new Set(orphanEventIds)].sort()),
      commandsWithoutEvents: Object.freeze(commandsWithoutEvents.sort()),
      eventsWithoutCommands: Object.freeze([...new Set(eventsWithoutCommands)].sort()),
    })
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    this.#clearIndexes()
    this.#listeners.clear()
  }

  #buildIndexes(): void {
    for (const command of this.#commands.values()) {
      add(this.#commandsByTask, command.taskId, command.id)
      add(this.#commandsByRun, command.runId, command.id)
      add(this.#commandsBySession, command.sessionId, command.id)
      for (const identity of identityValues(command)) {
        add(this.#commandIdentities, identity, command.id)
      }
    }
    for (const event of this.#events.values()) {
      add(this.#eventsByCorrelation, event.correlationId, event.eventId)
      add(this.#eventsBySpan, event.spanId, event.eventId)
      add(this.#eventsBySpan, event.parentSpanId, event.eventId)
      add(this.#eventsByMutation, event.mutationId, event.eventId)
      add(this.#eventsByCheckpoint, event.checkpointId, event.eventId)
      add(this.#eventsByToolCall, event.toolCallId, event.eventId)
      add(this.#eventsByFailure, event.failureId, event.eventId)
      add(this.#eventsByRecovery, event.recoveryId, event.eventId)
      for (const artifactId of event.artifactIds ?? []) {
        add(this.#eventsByArtifact, artifactId, event.eventId)
      }
      const matching = new Set<string>()
      for (const identity of keys([
        event.controlCommandId,
        event.correlationId,
        event.eventId,
        event.causationId,
        ...(event.entityRefs ?? []),
      ])) {
        for (const commandId of values(this.#commandIdentities, identity)) {
          matching.add(commandId)
        }
      }
      if (!matching.size) {
        for (const command of this.#commands.values()) {
          if (eventTouchesCommand(event, command)) matching.add(command.id)
        }
      }
      for (const commandId of matching) {
        add(this.#eventsByCommand, commandId, event.eventId)
        add(this.#eventsByCommand, event.eventId, commandId)
      }
    }
  }

  #eventIds(command: CommandProjectionRecord): string[] {
    const selected = new Set<string>()
    for (const eventId of values(this.#eventsByCommand, command.id)) {
      selected.add(eventId)
    }
    for (const identity of identityValues(command)) {
      if (this.#events.has(identity)) selected.add(identity)
      for (const eventId of values(this.#eventsByCorrelation, identity)) {
        selected.add(eventId)
      }
    }
    return [...selected]
  }

  #correlation(
    command: CommandProjectionRecord,
    events: readonly IndexedCommandCausalEvent[],
  ): CommandCorrelation {
    const spanIds = new Set<string>()
    const mutationIds = new Set<string>()
    const checkpointIds = new Set<string>()
    const artifactIds = new Set<string>(command.artifactIds ?? [])
    const toolCallIds = new Set<string>()
    const failureIds = new Set<string>()
    const recoveryIds = new Set<string>()
    for (const event of events) {
      if (event.spanId) spanIds.add(event.spanId)
      if (event.parentSpanId) spanIds.add(event.parentSpanId)
      if (event.mutationId) mutationIds.add(event.mutationId)
      if (event.checkpointId) checkpointIds.add(event.checkpointId)
      if (event.toolCallId) toolCallIds.add(event.toolCallId)
      if (event.failureId) failureIds.add(event.failureId)
      if (event.recoveryId) recoveryIds.add(event.recoveryId)
      for (const artifactId of event.artifactIds ?? []) artifactIds.add(artifactId)
    }
    const missing: string[] = []
    if (!events.length) missing.push("event")
    if (
      ["applied", "committed", "completed"].includes(
        normalized(command.lifecycle || command.status).toLowerCase(),
      ) &&
      !mutationIds.size
    ) {
      missing.push("mutation")
    }
    return Object.freeze({
      commandId: command.controlCommandId ?? command.id,
      requestId:
        normalized(command.attributes?.request_id) ||
        normalized(command.metadata?.request_id) ||
        command.correlationId ||
        undefined,
      queueId:
        normalized(command.attributes?.queue_id) ||
        normalized(command.metadata?.queue_id) ||
        undefined,
      eventIds: Object.freeze(events.map((event) => event.eventId)),
      spanIds: Object.freeze([...spanIds]),
      mutationIds: Object.freeze([...mutationIds]),
      checkpointIds: Object.freeze([...checkpointIds]),
      artifactIds: Object.freeze([...artifactIds]),
      toolCallIds: Object.freeze([...toolCallIds]),
      failureIds: Object.freeze([...failureIds]),
      recoveryIds: Object.freeze([...recoveryIds]),
      complete: missing.length === 0,
      missing: Object.freeze(missing),
    })
  }

  #clearIndexes(): void {
    this.#commands.clear()
    this.#events.clear()
    this.#commandIdentities.clear()
    this.#commandsByTask.clear()
    this.#commandsByRun.clear()
    this.#commandsBySession.clear()
    this.#eventsByCommand.clear()
    this.#eventsByCorrelation.clear()
    this.#eventsBySpan.clear()
    this.#eventsByMutation.clear()
    this.#eventsByCheckpoint.clear()
    this.#eventsByArtifact.clear()
    this.#eventsByToolCall.clear()
    this.#eventsByFailure.clear()
    this.#eventsByRecovery.clear()
  }

  #emit(): void {
    for (const listener of [...this.#listeners]) {
      try {
        listener()
      } catch {
        // Projection observers cannot change canonical index contents.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Command projection index is closed.")
  }
}
