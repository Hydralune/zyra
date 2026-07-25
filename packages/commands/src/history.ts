import type {
  CommandDeliveryMode,
  CommandReceipt,
} from "./contracts.ts"
import { commandHash } from "./identity.ts"

export interface CommandHistoryEntry {
  id: string
  scope: string
  taskId?: string
  runId?: string
  sessionId?: string
  commandId?: string
  requestId?: string
  name: string
  text: string
  mode: CommandDeliveryMode
  phase?: string
  createdAt: number
  updatedAt: number
  useCount: number
  replayed: boolean
  redacted: boolean
}

export interface CommandHistorySnapshot {
  schema: "zyra.command-history/v1"
  enabled: boolean
  revision: number
  entries: readonly CommandHistoryEntry[]
  scopeCounts: Readonly<Record<string, number>>
  restored: boolean
  persistedAt?: number
  lastError?: string
}

export interface CommandHistoryPersistence {
  load(): string | undefined | Promise<string | undefined>
  save(value: string): void | Promise<void>
  clear?(): void | Promise<void>
}

interface PersistedHistory {
  schema: "zyra.command-history/v1"
  version: 1
  createdAt: number
  checksum: string
  entries: CommandHistoryEntry[]
}

function normalizeScope(value: string | undefined): string {
  return value?.trim() || "global"
}

function normalizeName(value: string): string {
  const trigger = value.trim().split(/\s+/, 1)[0]?.toLowerCase() ?? ""
  return trigger.startsWith("/") ? trigger : ""
}

function protectedText(value: string): { text: string; redacted: boolean } {
  const text = value.trim()
  const name = normalizeName(text)
  if (name === "/btw") return { text: "/btw [redacted]", redacted: true }
  const patterns = [
    /(--?(?:token|secret|password|authorization|credential)(?:=|\s+))("[^"]*"|'[^']*'|\S+)/gi,
    /("(?:token|secret|password|authorization|credential)"\s*:\s*)"[^"]*"/gi,
  ]
  let selected = text
  for (const pattern of patterns) selected = selected.replace(pattern, "$1[redacted]")
  return {
    text: selected,
    redacted: selected !== text,
  }
}

function entryKey(scope: string, text: string, mode: CommandDeliveryMode): string {
  return commandHash(`${scope}\u0000${mode}\u0000${text}`)
}

function copyEntry(value: CommandHistoryEntry): CommandHistoryEntry {
  return { ...value }
}

function safeEntry(value: unknown): CommandHistoryEntry | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined
  const source = value as Record<string, unknown>
  const scope = normalizeScope(typeof source.scope === "string" ? source.scope : undefined)
  const text = typeof source.text === "string" ? source.text.trim() : ""
  const name = typeof source.name === "string"
    ? normalizeName(source.name)
    : normalizeName(text)
  const mode =
    source.mode === "steer" || source.mode === "interrupt"
      ? source.mode
      : "enqueue"
  const createdAt = Number(source.createdAt)
  const updatedAt = Number(source.updatedAt)
  if (
    !text ||
    !name ||
    !Number.isFinite(createdAt) ||
    !Number.isFinite(updatedAt)
  ) {
    return undefined
  }
  const safe = protectedText(text)
  return {
    id: entryKey(scope, safe.text, mode),
    scope,
    taskId: typeof source.taskId === "string" ? source.taskId : undefined,
    runId: typeof source.runId === "string" ? source.runId : undefined,
    sessionId: typeof source.sessionId === "string" ? source.sessionId : undefined,
    commandId: typeof source.commandId === "string" ? source.commandId : undefined,
    requestId: typeof source.requestId === "string" ? source.requestId : undefined,
    name,
    text: safe.text,
    mode,
    phase: typeof source.phase === "string" ? source.phase : undefined,
    createdAt,
    updatedAt,
    useCount: Math.max(1, Math.min(1_000_000, Math.floor(Number(source.useCount) || 1))),
    replayed: source.replayed === true,
    redacted: safe.redacted || source.redacted === true,
  }
}

function rank(entry: CommandHistoryEntry, query: string, now: number): number {
  const target = `${entry.name} ${entry.text}`.toLowerCase()
  const normalized = query.trim().toLowerCase()
  let score = Math.log2(entry.useCount + 1) * 100
  const ageHours = Math.max(0, now - entry.updatedAt) / 3_600_000
  score += Math.max(0, 200 - ageHours)
  if (!normalized) return score
  if (entry.name === normalized) score += 2000
  if (entry.name.startsWith(normalized)) score += 1200
  if (target.startsWith(normalized)) score += 800
  const exact = target.indexOf(normalized)
  if (exact >= 0) score += 600 - Math.min(500, exact)
  let position = -1
  for (const character of normalized) {
    position = target.indexOf(character, position + 1)
    if (position < 0) return Number.NEGATIVE_INFINITY
    score += Math.max(1, 20 - position)
  }
  return score
}

export class CommandHistoryStore {
  readonly #now: () => number
  readonly #persistence?: CommandHistoryPersistence
  readonly #listeners = new Set<() => void>()
  readonly #entries = new Map<string, CommandHistoryEntry>()
  readonly #navigation = new Map<string, {
    results: string[]
    index: number
    draft: string
  }>()
  #snapshot: CommandHistorySnapshot = Object.freeze({
    schema: "zyra.command-history/v1",
    enabled: true,
    revision: 0,
    entries: Object.freeze([]),
    scopeCounts: Object.freeze({}),
    restored: false,
  })
  #maximumEntries = 500
  #maximumAgeMs = 30 * 24 * 60 * 60 * 1000
  #persistTimer?: ReturnType<typeof setTimeout>
  #closed = false
  #disabledReason = "Command history is disabled."

  constructor(options: {
    now?: () => number
    persistence?: CommandHistoryPersistence
    maximumEntries?: number
    maximumAgeMs?: number
  } = {}) {
    this.#now = options.now ?? Date.now
    this.#persistence = options.persistence
    if (options.maximumEntries !== undefined) {
      this.#maximumEntries = Math.max(
        1,
        Math.min(10_000, Math.floor(options.maximumEntries)),
      )
    }
    if (options.maximumAgeMs !== undefined) {
      this.#maximumAgeMs = Math.max(
        60_000,
        Math.min(365 * 24 * 60 * 60 * 1000, options.maximumAgeMs),
      )
    }
  }

  getSnapshot = (): CommandHistorySnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  async restore(): Promise<CommandHistorySnapshot> {
    this.#assertAvailable()
    if (!this.#persistence) {
      this.#replace({ restored: true })
      return this.#snapshot
    }
    try {
      const text = await this.#persistence.load()
      if (!text) {
        this.#replace({ restored: true })
        return this.#snapshot
      }
      const value = JSON.parse(text) as Partial<PersistedHistory>
      const entries = Array.isArray(value.entries) ? value.entries : []
      const content = JSON.stringify(entries)
      if (
        value.schema !== "zyra.command-history/v1" ||
        value.version !== 1 ||
        value.checksum !== commandHash(content)
      ) {
        throw new TypeError("Persisted command history checksum is invalid.")
      }
      this.#entries.clear()
      for (const raw of entries) {
        const selected = safeEntry(raw)
        if (selected) this.#entries.set(selected.id, selected)
      }
      this.prune()
      this.#replace({
        restored: true,
        persistedAt: Number(value.createdAt) || undefined,
        lastError: undefined,
      })
      return this.#snapshot
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      this.#replace({ restored: true, lastError: message })
      throw error
    }
  }

  record(input: {
    text: string
    scope?: string
    taskId?: string
    runId?: string
    sessionId?: string
    mode?: CommandDeliveryMode
    receipt?: CommandReceipt
  }): CommandHistoryEntry {
    this.#assertAvailable()
    const protectedValue = protectedText(input.text)
    const name = normalizeName(protectedValue.text)
    if (!name) throw new TypeError("Command history only accepts slash commands.")
    const scope = normalizeScope(input.scope ?? input.taskId)
    const mode = input.mode ?? "enqueue"
    const id = entryKey(scope, protectedValue.text, mode)
    const now = this.#now()
    const prior = this.#entries.get(id)
    const receipt = input.receipt
    const entry: CommandHistoryEntry = {
      id,
      scope,
      taskId: input.taskId ?? receipt?.taskId,
      runId: input.runId ?? receipt?.runId,
      sessionId: input.sessionId ?? receipt?.sessionId,
      commandId: receipt?.commandId ?? prior?.commandId,
      requestId: receipt?.requestId ?? prior?.requestId,
      name,
      text: protectedValue.text,
      mode,
      phase: receipt?.phase ?? prior?.phase,
      createdAt: prior?.createdAt ?? now,
      updatedAt: now,
      useCount: (prior?.useCount ?? 0) + 1,
      replayed: receipt?.replayed ?? false,
      redacted: protectedValue.redacted,
    }
    this.#entries.set(id, entry)
    this.#navigation.delete(scope)
    this.prune()
    this.#publish()
    this.#schedulePersist()
    return copyEntry(entry)
  }

  settle(receipt: CommandReceipt): CommandHistoryEntry | undefined {
    this.#assertAvailable()
    const candidates = [...this.#entries.values()]
      .filter((entry) =>
        entry.requestId === receipt.requestId ||
        entry.commandId === receipt.commandId)
      .sort((left, right) => right.updatedAt - left.updatedAt)
    const selected = candidates[0]
    if (!selected) return undefined
    selected.commandId = receipt.commandId
    selected.requestId = receipt.requestId
    selected.phase = receipt.phase
    selected.replayed = receipt.replayed
    selected.updatedAt = this.#now()
    this.#publish()
    this.#schedulePersist()
    return copyEntry(selected)
  }

  search(
    query: string,
    options: { scope?: string; includeGlobal?: boolean; limit?: number } = {},
  ): CommandHistoryEntry[] {
    this.#assertOpen()
    const scope = options.scope ? normalizeScope(options.scope) : undefined
    const limit = Math.max(1, Math.min(500, Math.floor(options.limit ?? 50)))
    const now = this.#now()
    return [...this.#entries.values()]
      .filter((entry) =>
        !scope ||
        entry.scope === scope ||
        (options.includeGlobal !== false && entry.scope === "global"))
      .map((entry) => ({ entry, score: rank(entry, query, now) }))
      .filter((value) => Number.isFinite(value.score))
      .sort((left, right) => {
        const difference = right.score - left.score
        if (difference) return difference
        return right.entry.updatedAt - left.entry.updatedAt
      })
      .slice(0, limit)
      .map(({ entry }) => copyEntry(entry))
  }

  navigate(input: {
    scope?: string
    direction: "previous" | "next"
    draft: string
    query?: string
  }): { handled: boolean; value: string; cursor: number } {
    this.#assertAvailable()
    const scope = normalizeScope(input.scope)
    let state = this.#navigation.get(scope)
    if (!state || state.draft !== input.draft) {
      state = {
        results: this.search(input.query ?? "", {
          scope,
          includeGlobal: true,
          limit: 100,
        }).map((entry) => entry.text),
        index: -1,
        draft: input.draft,
      }
      this.#navigation.set(scope, state)
    }
    if (!state.results.length) {
      return {
        handled: false,
        value: input.draft,
        cursor: input.draft.length,
      }
    }
    if (input.direction === "previous") {
      state.index = Math.min(state.results.length - 1, state.index + 1)
    } else {
      state.index = Math.max(-1, state.index - 1)
    }
    const value = state.index < 0 ? state.draft : state.results[state.index]!
    return { handled: true, value, cursor: value.length }
  }

  resetNavigation(scope?: string): void {
    if (scope) this.#navigation.delete(normalizeScope(scope))
    else this.#navigation.clear()
  }

  prune(): number {
    this.#assertOpen()
    const cutoff = this.#now() - this.#maximumAgeMs
    const ordered = [...this.#entries.values()]
      .sort((left, right) => right.updatedAt - left.updatedAt)
    const remove = ordered
      .filter((entry, index) =>
        index >= this.#maximumEntries || entry.updatedAt < cutoff)
    for (const entry of remove) this.#entries.delete(entry.id)
    if (remove.length) this.#publish()
    return remove.length
  }

  async persist(): Promise<void> {
    this.#assertAvailable()
    if (!this.#persistence) return
    if (this.#persistTimer) {
      clearTimeout(this.#persistTimer)
      this.#persistTimer = undefined
    }
    const entries = [...this.#entries.values()]
      .sort((left, right) => right.updatedAt - left.updatedAt)
      .map(copyEntry)
    const content = JSON.stringify(entries)
    const createdAt = this.#now()
    const envelope: PersistedHistory = {
      schema: "zyra.command-history/v1",
      version: 1,
      createdAt,
      checksum: commandHash(content),
      entries,
    }
    await this.#persistence.save(JSON.stringify(envelope))
    this.#replace({ persistedAt: createdAt, lastError: undefined })
  }

  async clear(): Promise<void> {
    this.#assertAvailable()
    this.#entries.clear()
    this.#navigation.clear()
    if (this.#persistTimer) clearTimeout(this.#persistTimer)
    this.#persistTimer = undefined
    await this.#persistence?.clear?.()
    this.#publish()
  }

  disable(reason = "Command history is disabled."): void {
    if (this.#closed || !this.#snapshot.enabled) return
    this.#disabledReason = reason.trim() || "Command history is disabled."
    if (this.#persistTimer) clearTimeout(this.#persistTimer)
    this.#persistTimer = undefined
    this.#replace({ enabled: false, lastError: this.#disabledReason })
  }

  enable(): void {
    this.#assertOpen()
    if (this.#snapshot.enabled) return
    this.#replace({ enabled: true, lastError: undefined })
  }

  async close(): Promise<void> {
    if (this.#closed) return
    try {
      if (this.#snapshot.enabled) await this.persist()
    } catch {
      // Close is best effort; the current snapshot already carries errors.
    }
    this.#closed = true
    if (this.#persistTimer) clearTimeout(this.#persistTimer)
    this.#persistTimer = undefined
    this.#listeners.clear()
    this.#navigation.clear()
  }

  #schedulePersist(): void {
    if (!this.#persistence || this.#persistTimer) return
    this.#persistTimer = setTimeout(() => {
      this.#persistTimer = undefined
      void this.persist().catch((error) => {
        this.#replace({
          lastError: error instanceof Error ? error.message : String(error),
        })
      })
    }, 250)
  }

  #publish(): void {
    const entries = [...this.#entries.values()]
      .sort((left, right) => right.updatedAt - left.updatedAt)
      .map(copyEntry)
    const scopeCounts: Record<string, number> = {}
    for (const entry of entries) {
      scopeCounts[entry.scope] = (scopeCounts[entry.scope] ?? 0) + 1
    }
    this.#replace({
      entries: Object.freeze(entries),
      scopeCounts: Object.freeze(scopeCounts),
    })
  }

  #replace(patch: Partial<CommandHistorySnapshot>): void {
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      ...patch,
      revision: this.#snapshot.revision + 1,
    })
    for (const listener of [...this.#listeners]) {
      try {
        listener()
      } catch {
        // History observers cannot alter submission or receipt settlement.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Command history is closed.")
  }

  #assertAvailable(): void {
    this.#assertOpen()
    if (!this.#snapshot.enabled) throw new TypeError(this.#disabledReason)
  }
}
