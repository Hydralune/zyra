import type { TerminalBinding, TerminalPhase } from "./contracts.ts"

export interface TerminalTab {
  terminalId: string
  taskId: string
  workspaceId: string
  sessionId: string
  title: string
  titleNumber: number
  phase: TerminalPhase
  acknowledgedCursor: number
  scrollRow: number
  rows: number
  cols: number
  pinned: boolean
  openedAt: string
  updatedAt: string
}

export interface TerminalTabState {
  version: 2
  workspaceKey: string
  active?: string
  tabs: readonly TerminalTab[]
  revision: number
}

export interface TerminalTabStorage {
  load(key: string): unknown
  save(key: string, value: TerminalTabState): void
  remove(key: string): void
}

const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:@/+~-]*$/u

function identifier(value: unknown): string | undefined {
  if (typeof value !== "string" || value.length < 1 || value.length > 255) return undefined
  return IDENTIFIER.test(value) ? value : undefined
}

function integer(value: unknown, fallback: number, minimum = 0, maximum = Number.MAX_SAFE_INTEGER): number {
  if (!Number.isSafeInteger(value)) return fallback
  return Math.min(maximum, Math.max(minimum, Number(value)))
}

function phase(value: unknown): TerminalPhase {
  if (
    value === "opening" || value === "running" || value === "permission_pending"
    || value === "exited" || value === "killed" || value === "timed_out"
    || value === "crashed" || value === "rejected"
  ) return value
  return "crashed"
}

function timestamp(value: unknown): string {
  if (typeof value === "string" && Number.isFinite(Date.parse(value))) return value
  return new Date(0).toISOString()
}

function parseTab(value: unknown): TerminalTab | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined
  const input = value as Record<string, unknown>
  const terminalId = identifier(input.terminalId ?? input.terminal_id ?? input.id)
  const taskId = identifier(input.taskId ?? input.task_id)
  const workspaceId = identifier(input.workspaceId ?? input.workspace_id)
  const sessionId = identifier(input.sessionId ?? input.session_id)
  if (!terminalId || !taskId || !workspaceId || !sessionId) return undefined
  return {
    terminalId,
    taskId,
    workspaceId,
    sessionId,
    title: typeof input.title === "string" ? input.title.slice(0, 256) : "Terminal",
    titleNumber: integer(input.titleNumber ?? input.title_number, 0, 0, 10_000),
    phase: phase(input.phase),
    acknowledgedCursor: integer(
      input.acknowledgedCursor ?? input.acknowledged_cursor ?? input.cursor,
      0,
    ),
    scrollRow: integer(input.scrollRow ?? input.scroll_row ?? input.scrollY, 0),
    rows: integer(input.rows, 24, 2, 500),
    cols: integer(input.cols, 80, 2, 1_000),
    pinned: input.pinned === true,
    openedAt: timestamp(input.openedAt ?? input.opened_at),
    updatedAt: timestamp(input.updatedAt ?? input.updated_at),
  }
}

export function migrateTerminalTabs(value: unknown, workspaceKey: string): TerminalTabState {
  const input = value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
  const rawTabs = Array.isArray(input.tabs)
    ? input.tabs
    : Array.isArray(input.all)
      ? input.all
      : []
  const seen = new Set<string>()
  const tabs: TerminalTab[] = []
  for (const raw of rawTabs) {
    const tab = parseTab(raw)
    if (!tab || seen.has(tab.terminalId)) continue
    seen.add(tab.terminalId)
    tabs.push(tab)
  }
  const active = identifier(input.active)
  return Object.freeze({
    version: 2,
    workspaceKey,
    active: active && seen.has(active) ? active : tabs[0]?.terminalId,
    tabs: Object.freeze(tabs),
    revision: integer(input.revision, 0),
  })
}

export class TerminalTabStore {
  readonly #workspaceKey: string
  readonly #storage?: TerminalTabStorage
  readonly #maximumTabs: number
  #state: TerminalTabState
  #listeners = new Set<(state: TerminalTabState) => void>()

  constructor(
    workspaceKey: string,
    options: { storage?: TerminalTabStorage; maximumTabs?: number } = {},
  ) {
    if (!workspaceKey || workspaceKey.length > 1_024) throw new TypeError("Terminal workspace key is invalid.")
    this.#workspaceKey = workspaceKey
    this.#storage = options.storage
    this.#maximumTabs = Math.min(100, Math.max(1, Math.floor(options.maximumTabs ?? 20)))
    this.#state = migrateTerminalTabs(this.#storage?.load(this.#key()), workspaceKey)
    this.#prune()
  }

  snapshot(): TerminalTabState {
    return this.#state
  }

  listen(listener: (state: TerminalTabState) => void): () => void {
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  open(binding: TerminalBinding, options: { title?: string; phase?: TerminalPhase } = {}): TerminalTabState {
    const now = new Date().toISOString()
    const existing = this.#state.tabs.find((tab) => tab.terminalId === binding.terminalId)
    const next: TerminalTab = existing
      ? {
          ...existing,
          taskId: binding.taskId,
          workspaceId: binding.workspaceId,
          sessionId: binding.sessionId,
          title: options.title?.slice(0, 256) || existing.title,
          phase: options.phase ?? existing.phase,
          updatedAt: now,
        }
      : {
          terminalId: binding.terminalId,
          taskId: binding.taskId,
          workspaceId: binding.workspaceId,
          sessionId: binding.sessionId,
          title: options.title?.slice(0, 256) || `Terminal ${this.#nextNumber()}`,
          titleNumber: this.#nextNumber(),
          phase: options.phase ?? "opening",
          acknowledgedCursor: 0,
          scrollRow: 0,
          rows: 24,
          cols: 80,
          pinned: false,
          openedAt: now,
          updatedAt: now,
        }
    const tabs = existing
      ? this.#state.tabs.map((tab) => tab.terminalId === next.terminalId ? next : tab)
      : [...this.#state.tabs, next]
    return this.#commit({ ...this.#state, active: next.terminalId, tabs })
  }

  select(terminalId: string): TerminalTabState {
    if (!this.#state.tabs.some((tab) => tab.terminalId === terminalId)) return this.#state
    return this.#commit({ ...this.#state, active: terminalId })
  }

  update(terminalId: string, patch: Partial<Omit<TerminalTab, "terminalId" | "taskId" | "workspaceId" | "sessionId">>): TerminalTabState {
    const tabs = this.#state.tabs.map((tab) => {
      if (tab.terminalId !== terminalId) return tab
      return {
        ...tab,
        ...patch,
        title: patch.title === undefined ? tab.title : patch.title.slice(0, 256),
        acknowledgedCursor: patch.acknowledgedCursor === undefined
          ? tab.acknowledgedCursor
          : integer(patch.acknowledgedCursor, tab.acknowledgedCursor),
        scrollRow: patch.scrollRow === undefined ? tab.scrollRow : integer(patch.scrollRow, tab.scrollRow),
        rows: patch.rows === undefined ? tab.rows : integer(patch.rows, tab.rows, 2, 500),
        cols: patch.cols === undefined ? tab.cols : integer(patch.cols, tab.cols, 2, 1_000),
        updatedAt: new Date().toISOString(),
      }
    })
    return this.#commit({ ...this.#state, tabs })
  }

  close(terminalId: string): TerminalTabState {
    const index = this.#state.tabs.findIndex((tab) => tab.terminalId === terminalId)
    if (index < 0) return this.#state
    const tabs = this.#state.tabs.filter((tab) => tab.terminalId !== terminalId)
    const active = this.#state.active === terminalId
      ? tabs[Math.max(0, index - 1)]?.terminalId ?? tabs[0]?.terminalId
      : this.#state.active
    return this.#commit({ ...this.#state, active, tabs })
  }

  move(terminalId: string, target: number): TerminalTabState {
    const tabs = [...this.#state.tabs]
    const index = tabs.findIndex((tab) => tab.terminalId === terminalId)
    if (index < 0) return this.#state
    const selected = tabs.splice(index, 1)[0]!
    tabs.splice(Math.min(tabs.length, Math.max(0, Math.floor(target))), 0, selected)
    return this.#commit({ ...this.#state, tabs })
  }

  clear(): TerminalTabState {
    this.#storage?.remove(this.#key())
    return this.#commit({ ...this.#state, active: undefined, tabs: [] }, false)
  }

  #nextNumber(): number {
    const used = new Set(this.#state.tabs.map((tab) => tab.titleNumber).filter((value) => value > 0))
    for (let value = 1; value <= this.#maximumTabs + 1; value += 1) {
      if (!used.has(value)) return value
    }
    return this.#state.tabs.length + 1
  }

  #prune(): void {
    if (this.#state.tabs.length <= this.#maximumTabs) return
    const keep = [...this.#state.tabs]
      .sort((a, b) => Number(b.pinned) - Number(a.pinned) || Date.parse(b.updatedAt) - Date.parse(a.updatedAt))
      .slice(0, this.#maximumTabs)
    const ids = new Set(keep.map((tab) => tab.terminalId))
    this.#state = Object.freeze({
      ...this.#state,
      active: this.#state.active && ids.has(this.#state.active) ? this.#state.active : keep[0]?.terminalId,
      tabs: Object.freeze(this.#state.tabs.filter((tab) => ids.has(tab.terminalId))),
    })
  }

  #commit(next: Omit<TerminalTabState, "version" | "workspaceKey" | "revision"> & Partial<TerminalTabState>, persist = true): TerminalTabState {
    this.#state = Object.freeze({
      version: 2,
      workspaceKey: this.#workspaceKey,
      active: next.active,
      tabs: Object.freeze([...next.tabs]),
      revision: this.#state.revision + 1,
    })
    this.#prune()
    if (persist) this.#storage?.save(this.#key(), this.#state)
    for (const listener of this.#listeners) listener(this.#state)
    return this.#state
  }

  #key(): string {
    return `zyra:terminal-tabs:v2:${encodeURIComponent(this.#workspaceKey)}`
  }
}

export function browserTerminalTabStorage(storage: Storage): TerminalTabStorage {
  return {
    load(key) {
      const value = storage.getItem(key)
      if (!value) return undefined
      try { return JSON.parse(value) } catch { return undefined }
    },
    save(key, value) { storage.setItem(key, JSON.stringify(value)) },
    remove(key) { storage.removeItem(key) },
  }
}
