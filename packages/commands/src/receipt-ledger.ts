import type {
  CommandReceipt,
  CommandReceiptPhase,
} from "./contracts.ts"
import { commandReceiptTerminal } from "./receipts.ts"

export interface ReceiptLedgerSnapshot {
  revision: number
  receipts: readonly CommandReceipt[]
  byRequest: Readonly<Record<string, CommandReceipt>>
  byCommand: Readonly<Record<string, CommandReceipt>>
  byQueue: Readonly<Record<string, CommandReceipt>>
  pending: readonly CommandReceipt[]
  terminal: readonly CommandReceipt[]
  conflicts: readonly string[]
}

function sameReceipt(left: CommandReceipt, right: CommandReceipt): boolean {
  return (
    left.requestId === right.requestId &&
    left.commandId === right.commandId &&
    left.name === right.name &&
    left.taskId === right.taskId &&
    left.runId === right.runId
  )
}

function phaseRank(phase: CommandReceiptPhase): number {
  return {
    received: 0,
    validated: 1,
    queued: 2,
    running: 3,
    applied: 4,
    rejected: 4,
    expired: 4,
    cancelled: 4,
  }[phase]
}

function preferred(
  left: CommandReceipt,
  right: CommandReceipt,
): CommandReceipt {
  if (right.replayed && !left.replayed) return left
  if (left.replayed && !right.replayed) return right
  const difference = phaseRank(right.phase) - phaseRank(left.phase)
  if (difference > 0) return right
  if (difference < 0) return left
  const rightTime = Date.parse(right.finishedAt ?? right.createdAt)
  const leftTime = Date.parse(left.finishedAt ?? left.createdAt)
  return rightTime >= leftTime ? right : left
}

export class CommandReceiptLedger {
  readonly #byRequest = new Map<string, CommandReceipt>()
  readonly #byCommand = new Map<string, CommandReceipt>()
  readonly #byQueue = new Map<string, CommandReceipt>()
  readonly #conflicts = new Set<string>()
  readonly #listeners = new Set<() => void>()
  #snapshot: ReceiptLedgerSnapshot = Object.freeze({
    revision: 0,
    receipts: Object.freeze([]),
    byRequest: Object.freeze({}),
    byCommand: Object.freeze({}),
    byQueue: Object.freeze({}),
    pending: Object.freeze([]),
    terminal: Object.freeze([]),
    conflicts: Object.freeze([]),
  })
  #closed = false

  getSnapshot = (): ReceiptLedgerSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  remember(receipt: CommandReceipt): CommandReceipt {
    this.#assertOpen()
    const requestExisting = this.#byRequest.get(receipt.requestId)
    const commandExisting = this.#byCommand.get(receipt.commandId)
    if (requestExisting && !sameReceipt(requestExisting, receipt)) {
      this.#conflicts.add(`request:${receipt.requestId}`)
      throw new TypeError(
        `Command receipt request identity conflict: ${receipt.requestId}`,
      )
    }
    if (commandExisting && !sameReceipt(commandExisting, receipt)) {
      this.#conflicts.add(`command:${receipt.commandId}`)
      throw new TypeError(
        `Command receipt command identity conflict: ${receipt.commandId}`,
      )
    }
    const selected = preferred(
      requestExisting ?? commandExisting ?? receipt,
      receipt,
    )
    this.#byRequest.set(selected.requestId, selected)
    this.#byCommand.set(selected.commandId, selected)
    if (selected.queueId) {
      const queueExisting = this.#byQueue.get(selected.queueId)
      if (queueExisting && !sameReceipt(queueExisting, selected)) {
        this.#conflicts.add(`queue:${selected.queueId}`)
        throw new TypeError(
          `Command receipt queue identity conflict: ${selected.queueId}`,
        )
      }
      this.#byQueue.set(
        selected.queueId,
        queueExisting ? preferred(queueExisting, selected) : selected,
      )
    }
    this.#publish()
    return selected
  }

  request(requestId: string): CommandReceipt | undefined {
    return this.#byRequest.get(requestId)
  }

  command(commandId: string): CommandReceipt | undefined {
    return this.#byCommand.get(commandId)
  }

  queue(queueId: string): CommandReceipt | undefined {
    return this.#byQueue.get(queueId)
  }

  list(input: {
    taskId?: string
    runId?: string
    name?: string
    phases?: readonly CommandReceiptPhase[]
    limit?: number
  } = {}): CommandReceipt[] {
    const phases = input.phases ? new Set(input.phases) : undefined
    return [...this.#byCommand.values()]
      .filter((receipt) => !input.taskId || receipt.taskId === input.taskId)
      .filter((receipt) => !input.runId || receipt.runId === input.runId)
      .filter((receipt) => !input.name || receipt.name === input.name)
      .filter((receipt) => !phases || phases.has(receipt.phase))
      .sort((left, right) =>
        Date.parse(right.finishedAt ?? right.createdAt) -
        Date.parse(left.finishedAt ?? left.createdAt))
      .slice(0, Math.max(1, Math.min(5000, Math.floor(input.limit ?? 500))))
  }

  prune(input: {
    olderThanMs: number
    retainTerminal?: number
  }): CommandReceipt[] {
    this.#assertOpen()
    const threshold = Date.now() - Math.max(0, input.olderThanMs)
    const retain = Math.max(0, Math.floor(input.retainTerminal ?? 100))
    const terminal = this.list({
      phases: ["applied", "rejected", "expired", "cancelled"],
      limit: 5000,
    })
    const protectedIds = new Set(
      terminal.slice(0, retain).map((receipt) => receipt.commandId),
    )
    const removed: CommandReceipt[] = []
    for (const receipt of terminal.slice(retain)) {
      const finished = Date.parse(receipt.finishedAt ?? receipt.createdAt)
      if (finished > threshold || protectedIds.has(receipt.commandId)) continue
      this.#byRequest.delete(receipt.requestId)
      this.#byCommand.delete(receipt.commandId)
      if (receipt.queueId) this.#byQueue.delete(receipt.queueId)
      removed.push(receipt)
    }
    if (removed.length) this.#publish()
    return removed
  }

  close(): void {
    this.#closed = true
    this.#listeners.clear()
  }

  #publish(): void {
    const receipts = Object.freeze(this.list({ limit: 5000 }))
    this.#snapshot = Object.freeze({
      revision: this.#snapshot.revision + 1,
      receipts,
      byRequest: Object.freeze(Object.fromEntries(this.#byRequest)),
      byCommand: Object.freeze(Object.fromEntries(this.#byCommand)),
      byQueue: Object.freeze(Object.fromEntries(this.#byQueue)),
      pending: Object.freeze(receipts.filter((receipt) => !commandReceiptTerminal(receipt))),
      terminal: Object.freeze(receipts.filter(commandReceiptTerminal)),
      conflicts: Object.freeze([...this.#conflicts].sort()),
    })
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // Ledger observers cannot change receipt identity.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Command receipt ledger is closed.")
  }
}
