import {
  type CommandCoordinatorRecord,
  type CommandCoordinatorSnapshot,
  type CommandDeliveryMode,
  type CommandQueueItem,
  type CommandQueueSnapshot,
  type CommandReceipt,
  type CommandTaskContext,
  type CommandTransport,
  type CommandTransportRequest,
} from "./contracts.ts"
import { createCommandIdentity } from "./identity.ts"
import { parseCommand } from "./parser.ts"
import { CommandExecutionPolicy } from "./policy.ts"
import {
  admitQueueSnapshot,
  CommandQueueProjection,
} from "./queue.ts"
import {
  admitCommandReceipt,
  commandReceiptRetryable,
} from "./receipts.ts"
import {
  buildCommandRequest,
  retryCommandRequest,
} from "./request.ts"
import type { CommandRegistry } from "./registry.ts"

function operationId(commandId: string): string {
  return `command-operation:${commandId}`
}

function cloneRecord(
  record: CommandCoordinatorRecord,
): CommandCoordinatorRecord {
  return {
    ...record,
    identity: { ...record.identity },
    descriptor: {
      ...record.descriptor,
      aliases: [...record.descriptor.aliases],
      arguments: record.descriptor.arguments.map((value) => ({ ...value })),
      keywords: [...record.descriptor.keywords],
    },
    parsed: {
      ...record.parsed,
      tokens: record.parsed.tokens.map((value) => ({ ...value })),
      errors: record.parsed.errors.map((value) => ({ ...value })),
      arguments: {
        values: { ...record.parsed.arguments.values },
        positional: [...record.parsed.arguments.positional],
        flags: { ...record.parsed.arguments.flags },
        errors: record.parsed.arguments.errors.map((value) => ({ ...value })),
        hints: [...record.parsed.arguments.hints],
      },
    },
    context: { ...record.context },
  }
}

function message(error: unknown): string {
  return error instanceof Error
    ? error.message
    : String(error || "Command operation failed.")
}

const TRANSIENT_ARGUMENT_REDACTION = "[REDACTED_TRANSIENT_ARGUMENT]"

function retainedRequest(
  request: CommandTransportRequest,
  transientNames: readonly string[],
): CommandTransportRequest {
  if (!transientNames.length) return request
  const argumentsValue = { ...request.arguments }
  for (const name of transientNames) {
    if (Object.prototype.hasOwnProperty.call(argumentsValue, name)) {
      argumentsValue[name] = TRANSIENT_ARGUMENT_REDACTION
    }
  }
  return Object.freeze({
    ...request,
    arguments: Object.freeze(argumentsValue),
  })
}

export class CommandCoordinator {
  readonly #registry: CommandRegistry
  readonly #transport: CommandTransport
  readonly #context: () => CommandTaskContext
  readonly #policy: CommandExecutionPolicy
  readonly #listeners = new Set<() => void>()
  readonly #records = new Map<string, CommandCoordinatorRecord>()
  readonly #requests = new Map<string, CommandTransportRequest>()
  readonly #transientOperations = new Set<string>()
  readonly #inflight = new Map<string, Promise<CommandReceipt>>()
  readonly #controllers = new Map<string, AbortController>()
  readonly #receipts = new Map<string, CommandReceipt>()
  #queue?: CommandQueueProjection
  #snapshot: CommandCoordinatorSnapshot = Object.freeze({
    enabled: true,
    revision: 0,
    busy: false,
    records: Object.freeze([]),
  })
  #closed = false
  #disabledReason = "Command coordinator is disabled."

  constructor(options: {
    registry: CommandRegistry
    transport: CommandTransport
    context: () => CommandTaskContext
  }) {
    this.#registry = options.registry
    this.#transport = options.transport
    this.#context = options.context
    this.#policy = new CommandExecutionPolicy(options.registry)
  }

  getSnapshot = (): CommandCoordinatorSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  get queue(): CommandQueueProjection | undefined {
    return this.#queue
  }

  submit(
    text: string,
    options: {
      mode?: CommandDeliveryMode
      context?: Partial<CommandTaskContext>
      actorId?: string
      timeoutMs?: number
      retryOf?: string
      argumentOverrides?: Readonly<Record<string, unknown>>
    } = {},
  ): Promise<CommandReceipt> {
    this.#assertAvailable()
    const context = {
      ...this.#context(),
      ...options.context,
    }
    const parsed = parseCommand(text, this.#registry)
    const mode = options.mode ?? "enqueue"
    const decision = this.#policy.assert({
      parsed,
      context,
      mode,
      busy: this.#snapshot.busy,
    })
    const descriptor = parsed.descriptor!
    const identity = createCommandIdentity({
      taskId: context.taskId ?? "",
      runId: context.runId ?? "",
      sessionId: context.sessionId ?? `task:${context.taskId ?? ""}`,
      text: parsed.normalized,
      arguments: {
        ...parsed.arguments.values,
        ...options.argumentOverrides,
      },
      mode,
      retryOf: options.retryOf,
    })
    const existing = this.#inflight.get(identity.fingerprint)
    if (existing) return existing
    const controller = new AbortController()
    const request = buildCommandRequest({
      parsed,
      context,
      mode,
      actorId: options.actorId,
      identity,
      retryOf: options.retryOf,
      signal: controller.signal,
      timeoutMs: options.timeoutMs,
      argumentOverrides: options.argumentOverrides,
    })
    const now = Date.now()
    const record: CommandCoordinatorRecord = {
      operationId: operationId(request.commandId),
      identity,
      descriptor,
      parsed,
      context,
      mode,
      priority: decision.priority,
      phase: "validated",
      createdAt: now,
      updatedAt: now,
      attempt: 1,
      retryOf: options.retryOf,
    }
    this.#records.set(record.operationId, record)
    const transientNames = Object.keys(options.argumentOverrides ?? {})
    this.#requests.set(
      record.operationId,
      retainedRequest(request, transientNames),
    )
    if (transientNames.length) this.#transientOperations.add(record.operationId)
    this.#controllers.set(record.operationId, controller)
    this.#publish(record)
    const promise = this.#dispatch(record, request)
      .finally(() => {
        if (this.#inflight.get(identity.fingerprint) === promise) {
          this.#inflight.delete(identity.fingerprint)
        }
        this.#controllers.delete(record.operationId)
      })
    this.#inflight.set(identity.fingerprint, promise)
    return promise
  }

  async retry(
    operationIdValue: string,
    options: {
      mode?: CommandDeliveryMode
      timeoutMs?: number
    } = {},
  ): Promise<CommandReceipt> {
    this.#assertAvailable()
    const previous = this.#records.get(operationIdValue)
    const previousRequest = this.#requests.get(operationIdValue)
    if (!previous || !previousRequest || !previous.receipt) {
      throw new TypeError("Command retry requires a settled operation.")
    }
    if (this.#transientOperations.has(operationIdValue)) {
      throw new TypeError(
        "Command retry requires fresh transient arguments and cannot reuse a retained secret.",
      )
    }
    if (!commandReceiptRetryable(previous.receipt)) {
      throw new TypeError("Command receipt is not retryable.")
    }
    const controller = new AbortController()
    const request = retryCommandRequest({
      parsed: previous.parsed,
      context: {
        ...this.#context(),
        taskId: previous.context.taskId,
        runId: previous.context.runId,
        sessionId: previous.context.sessionId,
      },
      previous: previousRequest,
      mode: options.mode,
      signal: controller.signal,
    })
    const identity = {
      requestId: request.requestId,
      commandId: request.commandId,
      idempotencyKey: request.idempotencyKey,
      fingerprint: `${previous.identity.fingerprint}:retry:${previous.attempt + 1}`,
    }
    const now = Date.now()
    const record: CommandCoordinatorRecord = {
      ...cloneRecord(previous),
      operationId: operationId(request.commandId),
      identity,
      context: {
        ...previous.context,
        ...this.#context(),
      },
      mode: request.deliveryMode,
      priority: request.priority,
      phase: "validated",
      createdAt: now,
      updatedAt: now,
      attempt: previous.attempt + 1,
      retryOf: previous.identity.requestId,
      receipt: undefined,
      error: undefined,
    }
    this.#records.set(record.operationId, record)
    this.#requests.set(record.operationId, request)
    this.#controllers.set(record.operationId, controller)
    this.#publish(record)
    try {
      return await this.#dispatch(record, request)
    } finally {
      this.#controllers.delete(record.operationId)
    }
  }

  retryQueueItem(
    item: CommandQueueItem,
    options: {
      mode?: CommandDeliveryMode
      timeoutMs?: number
    } = {},
  ): Promise<CommandReceipt> {
    this.#assertAvailable()
    if (!item.retryable || !item.terminal) {
      throw new TypeError("Only a terminal retryable queue item can be retried.")
    }
    const text = item.text?.trim() || item.name
    if (!text || !item.requestId) {
      throw new TypeError(
        "Restored command retry requires command text and request identity.",
      )
    }
    return this.submit(text, {
      mode: options.mode,
      timeoutMs: options.timeoutMs,
      retryOf: item.requestId,
      context: {
        taskId: item.taskId,
        runId: item.runId,
        sessionId: item.sessionId,
      },
    })
  }

  async cancel(
    operationIdValue: string,
    reason = "Cancelled from the Zyra command surface.",
  ): Promise<CommandReceipt> {
    this.#assertAvailable()
    const record = this.#records.get(operationIdValue)
    const request = this.#requests.get(operationIdValue)
    if (!record || !request) {
      throw new TypeError("Unknown command operation.")
    }
    const controller = this.#controllers.get(operationIdValue)
    if (controller && !controller.signal.aborted) {
      controller.abort(reason)
    }
    const raw = await this.#transport.cancel({
      taskId: request.taskId,
      requestId: request.requestId,
      reason,
      idempotencyKey: `${request.idempotencyKey}:cancel`,
    })
    const receipt = admitCommandReceipt(raw, request)
    record.phase = receipt.phase
    record.receipt = receipt
    record.updatedAt = Date.now()
    record.error = receipt.error?.message
    this.#receipts.set(receipt.commandId, receipt)
    this.#publish(record)
    this.#queue?.reconcile({
      receipts: [receipt],
    })
    return receipt
  }

  async cancelQueueItem(
    item: CommandQueueItem,
    reason = "Cancelled from the Zyra command queue preview.",
  ): Promise<CommandReceipt> {
    this.#assertAvailable()
    if (!item.cancellable || item.phase !== "queued") {
      throw new TypeError("Only a queued cancellable command can be cancelled.")
    }
    if (!item.requestId || !item.taskId || !item.runId) {
      throw new TypeError(
        "Restored command cancellation requires task, run, and request identities.",
      )
    }
    const text = item.text?.trim() || item.name
    if (!text) {
      throw new TypeError(
        "Restored command cancellation requires canonical command text.",
      )
    }
    const parsed = parseCommand(text, this.#registry)
    if (!parsed.descriptor || parsed.errors.length) {
      throw new TypeError(
        parsed.errors.map((entry) => entry.message).join(" ") ||
        "Restored queue item does not identify a registered command.",
      )
    }
    const context: CommandTaskContext = {
      ...this.#context(),
      taskId: item.taskId,
      runId: item.runId,
      sessionId: item.sessionId,
    }
    const generated = createCommandIdentity({
      taskId: item.taskId,
      runId: item.runId,
      sessionId: item.sessionId,
      text: parsed.normalized,
      arguments: parsed.arguments.values,
      mode: "enqueue",
    })
    const request = buildCommandRequest({
      parsed,
      context,
      mode: "enqueue",
      identity: {
        requestId: item.requestId,
        commandId: item.commandId ?? generated.commandId,
        idempotencyKey:
          item.idempotencyKey ??
          `${generated.idempotencyKey}:restored`,
        fingerprint: generated.fingerprint,
      },
    })
    const now = Date.now()
    const record: CommandCoordinatorRecord = {
      operationId: operationId(request.commandId),
      identity: {
        requestId: request.requestId,
        commandId: request.commandId,
        idempotencyKey: request.idempotencyKey,
        fingerprint: generated.fingerprint,
      },
      descriptor: parsed.descriptor,
      parsed,
      context,
      mode: "enqueue",
      priority: item.priority,
      phase: "running",
      createdAt: now,
      updatedAt: now,
      attempt: 1,
    }
    this.#records.set(record.operationId, record)
    this.#requests.set(record.operationId, request)
    this.#publish(record)
    const raw = await this.#transport.cancel({
      taskId: request.taskId,
      requestId: request.requestId,
      reason,
      idempotencyKey: `${request.idempotencyKey}:cancel`,
    })
    const receipt = admitCommandReceipt(raw, request)
    record.phase = receipt.phase
    record.receipt = receipt
    record.updatedAt = Date.now()
    record.error = receipt.error?.message
    this.#receipts.set(receipt.commandId, receipt)
    this.#publish(record)
    this.#queue?.reconcile({
      receipts: [receipt],
    })
    return receipt
  }

  async restoreQueue(input: {
    taskId: string
    sessionId?: string
    includeTerminal?: boolean
    signal?: AbortSignal
  }): Promise<CommandQueueSnapshot> {
    this.#assertAvailable()
    const raw = await this.#transport.queue(input)
    const snapshot = admitQueueSnapshot(raw, {
      taskId: input.taskId,
      sessionId: input.sessionId,
      restored: true,
    })
    if (
      !this.#queue ||
      this.#queue.getSnapshot().taskId !== input.taskId
    ) {
      this.#queue?.close()
      this.#queue = new CommandQueueProjection(
        input.taskId,
        input.sessionId,
      )
    }
    this.#queue.replace(snapshot)
    return this.#queue.getSnapshot()
  }

  reconcileQueue(input: {
    projections?: Parameters<CommandQueueProjection["reconcile"]>[0]["projections"]
    receipts?: readonly CommandReceipt[]
  }): CommandQueueSnapshot | undefined {
    return this.#queue?.reconcile(input)
  }

  records(limit = 100): CommandCoordinatorRecord[] {
    return [...this.#records.values()]
      .sort((left, right) => right.updatedAt - left.updatedAt)
      .slice(0, Math.max(1, Math.min(1000, Math.floor(limit))))
      .map(cloneRecord)
  }

  receipts(): CommandReceipt[] {
    return [...this.#receipts.values()]
      .sort((left, right) =>
        Date.parse(right.createdAt) - Date.parse(left.createdAt))
  }

  disable(reason = "Command coordinator is disabled."): void {
    if (!this.#snapshot.enabled) return
    this.#disabledReason = reason.trim() || "Command coordinator is disabled."
    for (const controller of this.#controllers.values()) {
      if (!controller.signal.aborted) controller.abort(this.#disabledReason)
    }
    this.#policy.disable(this.#disabledReason)
    this.#registry.disable(this.#disabledReason)
    this.#replace({
      enabled: false,
      busy: false,
      lastError: this.#disabledReason,
    })
  }

  enable(): void {
    this.#assertOpen()
    this.#policy.enable()
    this.#registry.enable()
    this.#replace({
      enabled: true,
      lastError: undefined,
    })
  }

  close(reason = "Command coordinator closed."): void {
    if (this.#closed) return
    this.#closed = true
    for (const controller of this.#controllers.values()) {
      if (!controller.signal.aborted) controller.abort(reason)
    }
    this.#controllers.clear()
    this.#inflight.clear()
    this.#requests.clear()
    this.#transientOperations.clear()
    this.#listeners.clear()
    this.#queue?.close()
    this.#queue = undefined
  }

  async #dispatch(
    record: CommandCoordinatorRecord,
    request: CommandTransportRequest,
  ): Promise<CommandReceipt> {
    record.phase = "running"
    record.updatedAt = Date.now()
    this.#publish(record)
    try {
      const raw = await this.#transport.submit(request)
      const receipt = admitCommandReceipt(raw, request)
      record.phase = receipt.phase
      record.receipt = receipt
      record.error = receipt.error?.message
      record.updatedAt = Date.now()
      this.#receipts.set(receipt.commandId, receipt)
      this.#publish(record)
      if (receipt.queueId) {
        if (!this.#queue) {
          this.#queue = new CommandQueueProjection(
            receipt.taskId,
            receipt.sessionId,
          )
        }
        this.#queue.reconcile({
          receipts: [receipt],
        })
      }
      return receipt
    } catch (cause) {
      record.phase =
        request.signal?.aborted
          ? "cancelled"
          : "rejected"
      record.error = message(
        request.signal?.aborted
          ? request.signal.reason
          : cause,
      )
      record.updatedAt = Date.now()
      this.#publish(record)
      throw cause
    }
  }

  #publish(active: CommandCoordinatorRecord): void {
    const busy = [...this.#records.values()].some((record) =>
      ["validated", "running"].includes(record.phase))
    this.#replace({
      active: cloneRecord(active),
      busy,
      lastReceipt: active.receipt,
      lastError: active.error,
    })
  }

  #replace(patch: Partial<CommandCoordinatorSnapshot>): void {
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      ...patch,
      records: Object.freeze(this.records(100)),
      revision: this.#snapshot.revision + 1,
    })
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // An observer cannot mutate command execution.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw new TypeError("Command coordinator is closed.")
    }
  }

  #assertAvailable(): void {
    this.#assertOpen()
    if (!this.#snapshot.enabled) {
      throw new TypeError(this.#disabledReason)
    }
  }
}
