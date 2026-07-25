import {
  COMMAND_QUEUE_PROTOCOL,
  type CommandName,
  type CommandPriority,
  type CommandQueueItem,
  type CommandQueuePhase,
  type CommandQueueSnapshot,
  type CommandReceipt,
} from "./contracts.ts"

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : ""
}

function numberValue(value: unknown): number {
  const parsed = typeof value === "number" ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

function phase(value: unknown): CommandQueuePhase {
  const normalized = String(value ?? "").trim().toLowerCase()
  if (normalized === "reserved") return "reserved"
  if (["running", "dispatched", "started"].includes(normalized)) return "running"
  if (["completed", "succeeded", "applied", "committed"].includes(normalized)) {
    return "completed"
  }
  if (["failed", "rejected", "denied", "conflict"].includes(normalized)) {
    return "failed"
  }
  if (["cancelled", "canceled"].includes(normalized)) return "cancelled"
  if (["expired", "timeout", "timed_out"].includes(normalized)) return "expired"
  return "queued"
}

function priority(value: unknown): CommandPriority {
  const normalized = String(value ?? "").trim().toLowerCase()
  if (["now", "urgent", "high"].includes(normalized)) return "now"
  if (["later", "background", "low"].includes(normalized)) return "later"
  return "next"
}

function terminal(value: CommandQueuePhase): boolean {
  return ["completed", "failed", "cancelled", "expired"].includes(value)
}

function commandName(value: unknown): CommandName | undefined {
  const normalized = String(value ?? "").trim().toLowerCase()
  const allowed = new Set<CommandName>([
    "/status",
    "/graph",
    "/trace",
    "/artifacts",
    "/permissions",
    "/btw",
    "/inject",
    "/change",
    "/verify",
    "/eval",
    "/doctor",
  ])
  return allowed.has(normalized as CommandName)
    ? normalized as CommandName
    : undefined
}

function requestFromPayload(value: unknown): Record<string, unknown> {
  const payload = record(value)
  return record(payload.request ?? payload.control_request)
}

function commandText(
  request: Readonly<Record<string, unknown>>,
  metadata: Readonly<Record<string, unknown>>,
): string {
  const direct = stringValue(request.text ?? request.input)
  if (direct) return direct
  const name = stringValue(
    request.canonical_name ??
    request.canonicalName ??
    metadata.command_name ??
    metadata.commandName,
  )
  if (!name) return ""
  const argumentsValue = record(request.arguments)
  const raw = stringValue(
    argumentsValue.raw ??
    argumentsValue.text ??
    argumentsValue.input,
  ).trim()
  return raw ? `${name} ${raw}` : name
}

function itemFromBackend(
  value: unknown,
  position: number,
): CommandQueueItem | undefined {
  const source = record(value)
  const queueId = stringValue(source.queue_id ?? source.queueId)
  const sessionId = stringValue(source.session_id ?? source.sessionId)
  if (!queueId || !sessionId) return undefined
  const payload = record(source.payload)
  const request = requestFromPayload(payload)
  const requestMetadata = record(request.metadata)
  const status = phase(source.status)
  const metadata = record(source.metadata)
  const text = commandText(request, metadata)
  const name = commandName(
    request.canonical_name ??
    request.canonicalName ??
    metadata.command_name ??
    text.split(/\s+/, 1)[0],
  )
  return {
    queueId,
    requestId: stringValue(
      request.request_id ??
      request.requestId,
    ) || undefined,
    commandId: stringValue(
      request.command_id ??
      request.commandId,
    ) || undefined,
    taskId: stringValue(
      request.task_id ??
      request.taskId,
    ) || undefined,
    runId: stringValue(
      request.run_id ??
      request.runId,
    ) || undefined,
    sessionId,
    name,
    text: text || undefined,
    phase: status,
    priority: priority(source.priority),
    sequence: numberValue(source.sequence),
    position,
    idempotencyKey: stringValue(
      source.idempotency_key ??
      source.idempotencyKey,
    ) || undefined,
    retryOf: stringValue(
      request.retry_of_request_id ??
      request.retryOfRequestId ??
      requestMetadata.retry_of_request_id ??
      requestMetadata.retryOfRequestId,
    ) || undefined,
    createdAt: stringValue(
      source.created_at ??
      source.createdAt,
    ) || new Date(0).toISOString(),
    updatedAt: stringValue(
      source.updated_at ??
      source.updatedAt,
    ) || new Date(0).toISOString(),
    claimExpiresAt: stringValue(
      source.claim_expires_at ??
      source.claimExpiresAt,
    ) || undefined,
    error: stringValue(metadata.error) || undefined,
    cancellable: status === "queued",
    retryable: ["failed", "cancelled", "expired"].includes(status),
    terminal: terminal(status),
    source: "backend-queue",
  }
}

export function admitQueueSnapshot(
  rawValue: unknown,
  input: {
    taskId: string
    sessionId?: string
    restored?: boolean
    revision?: number
  },
): CommandQueueSnapshot {
  const raw = record(rawValue)
  const container = record(
    raw.command_queue ??
    raw.commandQueue ??
    raw.queue,
  )
  const source =
    Object.keys(container).length ? container : raw
  const entries = Array.isArray(source.entries)
    ? source.entries
    : Array.isArray(source.items)
      ? source.items
      : []
  const selected = entries
    .map(itemFromBackend)
    .filter((value): value is CommandQueueItem => value !== undefined)
    .filter((item) => !item.taskId || item.taskId === input.taskId)
    .filter((item) => !input.sessionId || item.sessionId === input.sessionId)
  return buildQueueSnapshot(selected, {
    taskId: input.taskId,
    sessionId: input.sessionId,
    sequence: numberValue(source.sequence),
    revision: input.revision ?? numberValue(source.revision),
    restored: input.restored ?? false,
  })
}

export function queueItemFromReceipt(
  receipt: CommandReceipt,
  position = 0,
): CommandQueueItem | undefined {
  if (!receipt.queueId) return undefined
  const queuePhase: CommandQueuePhase =
    receipt.phase === "queued" ? "queued"
    : receipt.phase === "running" ? "running"
    : receipt.phase === "applied" ? "completed"
    : receipt.phase === "cancelled" ? "cancelled"
    : receipt.phase === "expired" ? "expired"
    : "failed"
  return {
    queueId: receipt.queueId,
    requestId: receipt.requestId,
    commandId: receipt.commandId,
    taskId: receipt.taskId,
    runId: receipt.runId,
    sessionId: receipt.sessionId ?? `task:${receipt.taskId}`,
    name: receipt.name,
    phase: queuePhase,
    priority: receipt.priority,
    sequence: 0,
    position,
    idempotencyKey: receipt.idempotencyKey,
    retryOf: receipt.retryOf,
    createdAt: receipt.createdAt,
    updatedAt: receipt.finishedAt ?? receipt.createdAt,
    error: receipt.error?.message,
    cancellable: queuePhase === "queued",
    retryable: ["failed", "cancelled", "expired"].includes(queuePhase),
    terminal: terminal(queuePhase),
    source: "receipt",
  }
}

export interface ProjectedCommandLike {
  id: string
  taskId: string
  runId: string
  sessionId?: string
  lifecycle: string
  priority?: string
  queuePosition?: number
  commandName?: string
  controlCommandId?: string
  correlationId?: string
  createdAt?: string
  updatedAt?: string
  sequence?: number
  errorCode?: string
  receiptId?: string
}

export function queueItemFromProjection(
  value: ProjectedCommandLike,
): CommandQueueItem {
  const itemPhase = phase(value.lifecycle)
  const name = commandName(value.commandName)
  return {
    queueId: value.receiptId
      ? `projection:${value.receiptId}`
      : `projection:${value.id}`,
    requestId: value.correlationId || undefined,
    commandId: value.controlCommandId || value.id,
    taskId: value.taskId,
    runId: value.runId,
    sessionId: value.sessionId ?? `task:${value.taskId}`,
    name,
    phase: itemPhase,
    priority: priority(value.priority),
    sequence: value.sequence ?? 0,
    position: value.queuePosition ?? 0,
    createdAt: value.createdAt ?? new Date(0).toISOString(),
    updatedAt: value.updatedAt ?? new Date(0).toISOString(),
    error: value.errorCode,
    cancellable: itemPhase === "queued",
    retryable: ["failed", "cancelled", "expired"].includes(itemPhase),
    terminal: terminal(itemPhase),
    source: "canonical-projection",
  }
}

function itemIdentity(item: CommandQueueItem): string {
  return (
    item.queueId ||
    item.commandId ||
    item.requestId ||
    `${item.sessionId}:${item.sequence}:${item.name ?? ""}`
  )
}

function preferred(
  left: CommandQueueItem,
  right: CommandQueueItem,
): CommandQueueItem {
  const phaseRank = {
    queued: 0,
    reserved: 1,
    running: 2,
    completed: 3,
    failed: 3,
    cancelled: 3,
    expired: 3,
  } as const
  const sourceRank = {
    "receipt": 0,
    "canonical-projection": 1,
    "backend-queue": 2,
  } as const
  const leftRank = sourceRank[left.source]
  const rightRank = sourceRank[right.source]
  const newest =
    Date.parse(right.updatedAt) > Date.parse(left.updatedAt)
      ? right
      : left
  const phaseDifference = phaseRank[right.phase] - phaseRank[left.phase]
  const authoritative =
    phaseDifference > 0
      ? right
      : phaseDifference < 0
        ? left
        : rightRank > leftRank
          ? right
          : leftRank > rightRank
            ? left
            : newest
  return {
    ...left,
    ...right,
    ...authoritative,
    requestId: authoritative.requestId ?? right.requestId ?? left.requestId,
    commandId: authoritative.commandId ?? right.commandId ?? left.commandId,
    queueId: right.queueId || left.queueId,
    retryOf: right.retryOf ?? left.retryOf,
    error: right.error ?? left.error,
  }
}

export function mergeQueueItems(
  ...groups: readonly (readonly CommandQueueItem[])[]
): CommandQueueItem[] {
  const merged = new Map<string, CommandQueueItem>()
  for (const group of groups) {
    for (const item of group) {
      const identities = [
        item.queueId,
        item.commandId,
        item.requestId,
      ].filter(Boolean) as string[]
      let existingKey = ""
      for (const identity of identities) {
        const found = [...merged].find(([, candidate]) =>
          [
            candidate.queueId,
            candidate.commandId,
            candidate.requestId,
          ].includes(identity),
        )
        if (found) {
          existingKey = found[0]
          break
        }
      }
      const key = existingKey || itemIdentity(item)
      const existing = merged.get(key)
      merged.set(key, existing ? preferred(existing, item) : { ...item })
    }
  }
  return sortQueueItems([...merged.values()])
}

export function sortQueueItems(
  values: readonly CommandQueueItem[],
): CommandQueueItem[] {
  const weight = {
    now: 0,
    next: 1,
    later: 2,
  } as const
  return [...values].sort((left, right) => {
    if (left.terminal !== right.terminal) return left.terminal ? 1 : -1
    const priorityDifference =
      weight[left.priority] -
      weight[right.priority]
    if (priorityDifference) return priorityDifference
    const sequenceDifference = left.sequence - right.sequence
    if (sequenceDifference) return sequenceDifference
    const timeDifference =
      Date.parse(left.createdAt) -
      Date.parse(right.createdAt)
    if (timeDifference) return timeDifference
    return itemIdentity(left).localeCompare(itemIdentity(right))
  }).map((item, position) => ({
    ...item,
    position,
  }))
}

export function buildQueueSnapshot(
  values: readonly CommandQueueItem[],
  options: {
    taskId: string
    sessionId?: string
    sequence?: number
    revision?: number
    restored?: boolean
  },
): CommandQueueSnapshot {
  const items = Object.freeze(sortQueueItems(values))
  return Object.freeze({
    schema: COMMAND_QUEUE_PROTOCOL,
    taskId: options.taskId,
    sessionId: options.sessionId,
    sequence: options.sequence ?? Math.max(0, ...items.map((item) => item.sequence)),
    revision: options.revision ?? 0,
    restored: options.restored ?? false,
    items,
    pending: Object.freeze(items.filter((item) => item.phase === "queued" || item.phase === "reserved")),
    running: Object.freeze(items.filter((item) => item.phase === "running")),
    settled: Object.freeze(items.filter((item) => item.terminal)),
  })
}

export class CommandQueueProjection {
  #snapshot: CommandQueueSnapshot
  readonly #listeners = new Set<() => void>()
  #closed = false

  constructor(taskId: string, sessionId?: string) {
    this.#snapshot = buildQueueSnapshot([], {
      taskId,
      sessionId,
    })
  }

  getSnapshot = (): CommandQueueSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  replace(snapshot: CommandQueueSnapshot): void {
    this.#assertOpen()
    if (snapshot.taskId !== this.#snapshot.taskId) {
      throw new TypeError("Queue snapshot belongs to another task.")
    }
    this.#snapshot = Object.freeze({
      ...snapshot,
      revision: Math.max(snapshot.revision, this.#snapshot.revision + 1),
    })
    this.#emit()
  }

  reconcile(input: {
    backend?: CommandQueueSnapshot
    projections?: readonly ProjectedCommandLike[]
    receipts?: readonly CommandReceipt[]
    restored?: boolean
  }): CommandQueueSnapshot {
    this.#assertOpen()
    const projectionItems = (input.projections ?? [])
      .filter((item) => item.taskId === this.#snapshot.taskId)
      .map(queueItemFromProjection)
    const receiptItems = (input.receipts ?? [])
      .map((receipt) => queueItemFromReceipt(receipt))
      .filter((item): item is CommandQueueItem => item !== undefined)
    const items = mergeQueueItems(
      this.#snapshot.items,
      input.backend?.items ?? [],
      projectionItems,
      receiptItems,
    )
    this.#snapshot = buildQueueSnapshot(items, {
      taskId: this.#snapshot.taskId,
      sessionId: this.#snapshot.sessionId,
      sequence: Math.max(
        this.#snapshot.sequence,
        input.backend?.sequence ?? 0,
      ),
      revision: this.#snapshot.revision + 1,
      restored: input.restored ?? input.backend?.restored ?? this.#snapshot.restored,
    })
    this.#emit()
    return this.#snapshot
  }

  close(): void {
    this.#closed = true
    this.#listeners.clear()
  }

  #emit(): void {
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // A view subscriber cannot change canonical queue reconciliation.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw new TypeError("Command queue projection is closed.")
    }
  }
}
