import type { CommandReceipt } from "./contracts.ts"
import type { CommandProjectionTrace } from "./projection-index.ts"
import {
  buildCommandResultModel,
  filterCommandResult,
  windowCommandResult,
  type CommandResultModel,
  type ResultModelOptions,
} from "./result-model.ts"

export type ResultOverlayPhase = "open" | "closed" | "expired"

export interface CommandResultRecord {
  id: string
  commandId: string
  requestId: string
  taskId: string
  runId: string
  phase: ResultOverlayPhase
  openedAt: number
  updatedAt: number
  closedAt?: number
  closeReason?: string
  viewCount: number
  model: CommandResultModel
}

export interface CommandResultStoreSnapshot {
  schema: "zyra.command-result-store/v1"
  enabled: boolean
  revision: number
  activeId?: string
  active?: CommandResultRecord
  open: readonly CommandResultRecord[]
  recent: readonly CommandResultRecord[]
  lastError?: string
}

function recordId(receipt: CommandReceipt): string {
  return `command-result:${receipt.commandId}`
}

function copyRecord(value: CommandResultRecord): CommandResultRecord {
  return {
    ...value,
    model: value.model,
  }
}

function resultOrder(
  left: CommandResultRecord,
  right: CommandResultRecord,
): number {
  const updated = right.updatedAt - left.updatedAt
  if (updated) return updated
  return right.id.localeCompare(left.id)
}

function includesAll(value: string, terms: readonly string[]): boolean {
  return terms.every((term) => value.includes(term))
}

export class CommandResultStore {
  readonly #now: () => number
  readonly #listeners = new Set<() => void>()
  readonly #records = new Map<string, CommandResultRecord>()
  readonly #identityIndex = new Map<string, string>()
  #snapshot: CommandResultStoreSnapshot = Object.freeze({
    schema: "zyra.command-result-store/v1",
    enabled: true,
    revision: 0,
    open: Object.freeze([]),
    recent: Object.freeze([]),
  })
  #maximumRecords = 200
  #retentionMs = 24 * 60 * 60 * 1000
  #closed = false
  #disabledReason = "Command result overlays are disabled."

  constructor(options: {
    now?: () => number
    maximumRecords?: number
    retentionMs?: number
  } = {}) {
    this.#now = options.now ?? Date.now
    if (options.maximumRecords !== undefined) {
      this.#maximumRecords = Math.max(
        1,
        Math.min(5000, Math.floor(options.maximumRecords)),
      )
    }
    if (options.retentionMs !== undefined) {
      this.#retentionMs = Math.max(
        60_000,
        Math.min(30 * 24 * 60 * 60 * 1000, options.retentionMs),
      )
    }
  }

  getSnapshot = (): CommandResultStoreSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  open(
    receipt: CommandReceipt,
    options: ResultModelOptions & { trace?: CommandProjectionTrace } = {},
  ): CommandResultRecord {
    this.#assertAvailable()
    const id = recordId(receipt)
    const now = this.#now()
    const prior = this.#records.get(id)
    const model = buildCommandResultModel(receipt, options)
    const selected: CommandResultRecord = {
      id,
      commandId: receipt.commandId,
      requestId: receipt.requestId,
      taskId: receipt.taskId,
      runId: receipt.runId,
      phase: "open",
      openedAt: prior?.openedAt ?? now,
      updatedAt: now,
      closedAt: undefined,
      closeReason: undefined,
      viewCount: (prior?.viewCount ?? 0) + 1,
      model,
    }
    this.#records.set(id, selected)
    this.#index(selected)
    this.#replace({
      activeId: id,
      active: copyRecord(selected),
      lastError: undefined,
    })
    this.prune()
    return copyRecord(selected)
  }

  update(
    identity: string,
    receipt: CommandReceipt,
    trace?: CommandProjectionTrace,
  ): CommandResultRecord {
    this.#assertAvailable()
    const selected = this.find(identity)
    if (!selected) return this.open(receipt, { trace })
    if (
      selected.commandId !== receipt.commandId ||
      selected.requestId !== receipt.requestId
    ) {
      throw new TypeError(
        "Command result update cannot replace canonical receipt identity.",
      )
    }
    const current = this.#records.get(selected.id)!
    current.model = buildCommandResultModel(receipt, { trace })
    current.updatedAt = this.#now()
    current.phase = "open"
    current.closedAt = undefined
    current.closeReason = undefined
    this.#replace({
      activeId: current.id,
      active: copyRecord(current),
      lastError: undefined,
    })
    return copyRecord(current)
  }

  activate(identity: string): CommandResultRecord | undefined {
    this.#assertAvailable()
    const selected = this.find(identity)
    if (!selected) return undefined
    const current = this.#records.get(selected.id)!
    if (current.phase !== "open") {
      current.phase = "open"
      current.openedAt = this.#now()
      current.closedAt = undefined
      current.closeReason = undefined
    }
    current.updatedAt = this.#now()
    current.viewCount += 1
    this.#replace({
      activeId: current.id,
      active: copyRecord(current),
    })
    return copyRecord(current)
  }

  close(
    identity?: string,
    reason = "Command result overlay dismissed.",
  ): CommandResultRecord | undefined {
    this.#assertOpen()
    const id = identity
      ? this.#identityIndex.get(identity) ?? identity
      : this.#snapshot.activeId
    if (!id) return undefined
    const current = this.#records.get(id)
    if (!current || current.phase !== "open") return undefined
    const now = this.#now()
    current.phase = "closed"
    current.closedAt = now
    current.updatedAt = now
    current.closeReason = reason.trim() || "Command result overlay dismissed."
    const next = [...this.#records.values()]
      .filter((record) => record.phase === "open" && record.id !== current.id)
      .sort(resultOrder)[0]
    this.#replace({
      activeId: next?.id,
      active: next ? copyRecord(next) : undefined,
    })
    return copyRecord(current)
  }

  closeTask(
    taskId: string,
    reason = "Task route changed.",
  ): CommandResultRecord[] {
    this.#assertOpen()
    const closed: CommandResultRecord[] = []
    for (const record of this.#records.values()) {
      if (record.taskId !== taskId || record.phase !== "open") continue
      const selected = this.close(record.id, reason)
      if (selected) closed.push(selected)
    }
    return closed
  }

  find(identity: string): CommandResultRecord | undefined {
    this.#assertOpen()
    const normalized = identity.trim()
    if (!normalized) return undefined
    const id = this.#identityIndex.get(normalized) ?? normalized
    const selected = this.#records.get(id)
    return selected ? copyRecord(selected) : undefined
  }

  query(input: {
    text?: string
    taskId?: string
    phases?: readonly ResultOverlayPhase[]
    commands?: readonly string[]
    limit?: number
  } = {}): CommandResultRecord[] {
    this.#assertOpen()
    const terms = (input.text ?? "")
      .trim()
      .toLowerCase()
      .split(/\s+/)
      .filter(Boolean)
    const phases = new Set(input.phases ?? [])
    const commands = new Set(
      (input.commands ?? []).map((value) => value.trim().toLowerCase()),
    )
    const limit = Math.max(1, Math.min(1000, Math.floor(input.limit ?? 100)))
    return [...this.#records.values()]
      .filter((record) => !input.taskId || record.taskId === input.taskId)
      .filter((record) => !phases.size || phases.has(record.phase))
      .filter((record) => !commands.size || commands.has(record.model.name.toLowerCase()))
      .filter((record) => !terms.length || includesAll(record.model.searchText, terms))
      .sort(resultOrder)
      .slice(0, limit)
      .map(copyRecord)
  }

  filtered(
    identity: string,
    query: string,
    window?: { offset: number; limit: number },
  ): CommandResultModel | undefined {
    const selected = this.find(identity)
    if (!selected) return undefined
    const filtered = filterCommandResult(selected.model, query)
    return window
      ? windowCommandResult(filtered, window.offset, window.limit)
      : filtered
  }

  prune(): number {
    this.#assertOpen()
    const cutoff = this.#now() - this.#retentionMs
    const ordered = [...this.#records.values()].sort(resultOrder)
    const remove = ordered.filter((record, index) =>
      record.phase !== "open" &&
      (index >= this.#maximumRecords || record.updatedAt < cutoff))
    for (const record of remove) {
      this.#records.delete(record.id)
      for (const [identity, id] of this.#identityIndex) {
        if (id === record.id) this.#identityIndex.delete(identity)
      }
    }
    if (remove.length) this.#publish()
    return remove.length
  }

  disable(reason = "Command result overlays are disabled."): void {
    if (this.#closed || !this.#snapshot.enabled) return
    this.#disabledReason = reason.trim() || "Command result overlays are disabled."
    for (const record of this.#records.values()) {
      if (record.phase !== "open") continue
      record.phase = "closed"
      record.closedAt = this.#now()
      record.updatedAt = record.closedAt
      record.closeReason = this.#disabledReason
    }
    this.#replace({
      enabled: false,
      activeId: undefined,
      active: undefined,
      lastError: this.#disabledReason,
    })
  }

  enable(): void {
    this.#assertOpen()
    if (this.#snapshot.enabled) return
    this.#replace({
      enabled: true,
      lastError: undefined,
    })
  }

  closeStore(reason = "Command result store closed."): void {
    if (this.#closed) return
    for (const record of this.#records.values()) {
      if (record.phase !== "open") continue
      record.phase = "closed"
      record.closedAt = this.#now()
      record.updatedAt = record.closedAt
      record.closeReason = reason
    }
    this.#closed = true
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      enabled: false,
      activeId: undefined,
      active: undefined,
      open: Object.freeze([]),
      recent: Object.freeze(
        [...this.#records.values()].sort(resultOrder).slice(0, 100).map(copyRecord),
      ),
      revision: this.#snapshot.revision + 1,
    })
    this.#listeners.clear()
    this.#identityIndex.clear()
  }

  #index(record: CommandResultRecord): void {
    this.#identityIndex.set(record.id, record.id)
    this.#identityIndex.set(record.commandId, record.id)
    this.#identityIndex.set(record.requestId, record.id)
    for (const eventId of record.model.eventIds) {
      this.#identityIndex.set(eventId, record.id)
    }
    if (record.model.checkpointRef) {
      this.#identityIndex.set(record.model.checkpointRef, record.id)
    }
  }

  #publish(): void {
    this.#replace({})
  }

  #replace(patch: Partial<CommandResultStoreSnapshot>): void {
    const ordered = [...this.#records.values()].sort(resultOrder)
    const replacesActiveId =
      Object.prototype.hasOwnProperty.call(patch, "activeId")
    const replacesActive =
      Object.prototype.hasOwnProperty.call(patch, "active")
    const activeId = replacesActiveId
      ? patch.activeId
      : this.#snapshot.activeId
    const active = replacesActive
      ? patch.active
      : activeId
        ? this.#records.get(activeId)
        : undefined
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      ...patch,
      activeId,
      active: active ? copyRecord(active) : undefined,
      open: Object.freeze(
        ordered.filter((record) => record.phase === "open").map(copyRecord),
      ),
      recent: Object.freeze(ordered.slice(0, 100).map(copyRecord)),
      revision: this.#snapshot.revision + 1,
    })
    for (const listener of [...this.#listeners]) {
      try {
        listener()
      } catch {
        // Overlay observers cannot change receipt or result state.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Command result store is closed.")
  }

  #assertAvailable(): void {
    this.#assertOpen()
    if (!this.#snapshot.enabled) throw new TypeError(this.#disabledReason)
  }
}
