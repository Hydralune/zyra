export interface DraftStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

export interface CommandDraft {
  scope: string
  value: string
  cursor: number
  revision: number
  updatedAt: number
}

export interface SubmissionCapture {
  id: string
  scope: string
  value: string
  cursor: number
  draftRevision: number
  capturedAt: number
}

function normalizeScope(value: string): string {
  const scope = value.trim().toLowerCase()
  if (!/^[a-z0-9][a-z0-9._:-]{0,240}$/.test(scope)) {
    throw new TypeError(`Invalid command draft scope: ${value}`)
  }
  return scope
}

function normalizeDraftValue(value: string): string {
  if (new TextEncoder().encode(value).byteLength > 256 * 1024) {
    throw new TypeError("Command draft exceeds 256 KiB.")
  }
  return value.replace(/\u0000/g, "")
}

function captureId(scope: string, revision: number): string {
  const random = Math.random().toString(36).slice(2, 10)
  return `capture_${scope.replace(/[^a-z0-9]/g, "_")}_${revision.toString(36)}_${random}`
}

function validDraft(value: unknown): value is CommandDraft {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false
  const draft = value as Record<string, unknown>
  return (
    typeof draft.scope === "string" &&
    typeof draft.value === "string" &&
    typeof draft.cursor === "number" &&
    Number.isSafeInteger(draft.cursor) &&
    typeof draft.revision === "number" &&
    Number.isSafeInteger(draft.revision) &&
    typeof draft.updatedAt === "number" &&
    Number.isFinite(draft.updatedAt)
  )
}

function cloneDraft(draft: CommandDraft): CommandDraft {
  return { ...draft }
}

export class CommandDraftStore {
  readonly #storage?: DraftStorage
  readonly #storageKey: string
  readonly #drafts = new Map<string, CommandDraft>()
  readonly #captures = new Map<string, SubmissionCapture>()
  readonly #listeners = new Set<(draft: CommandDraft | undefined) => void>()
  #closed = false

  constructor(options: { storage?: DraftStorage; storageKey?: string } = {}) {
    this.#storage = options.storage
    this.#storageKey = options.storageKey ?? "zyra.workbench.command-drafts.v1"
    this.#load()
  }

  get(scopeValue: string): CommandDraft | undefined {
    const scope = normalizeScope(scopeValue)
    const draft = this.#drafts.get(scope)
    return draft ? cloneDraft(draft) : undefined
  }

  set(scopeValue: string, value: string, cursor?: number): CommandDraft {
    this.#assertOpen()
    const scope = normalizeScope(scopeValue)
    const normalized = normalizeDraftValue(value)
    const previous = this.#drafts.get(scope)
    const position = Math.max(0, Math.min(normalized.length, Math.floor(cursor ?? normalized.length)))
    if (previous && previous.value === normalized && previous.cursor === position) {
      return cloneDraft(previous)
    }
    const draft: CommandDraft = {
      scope,
      value: normalized,
      cursor: position,
      revision: (previous?.revision ?? 0) + 1,
      updatedAt: Date.now(),
    }
    if (normalized) this.#drafts.set(scope, draft)
    else this.#drafts.delete(scope)
    this.#persist()
    this.#emit(normalized ? draft : undefined)
    return cloneDraft(draft)
  }

  clear(scopeValue: string): boolean {
    this.#assertOpen()
    const scope = normalizeScope(scopeValue)
    const removed = this.#drafts.delete(scope)
    if (removed) {
      this.#persist()
      this.#emit(undefined)
    }
    return removed
  }

  capture(scopeValue: string): SubmissionCapture {
    this.#assertOpen()
    const scope = normalizeScope(scopeValue)
    const draft = this.#drafts.get(scope) ?? {
      scope,
      value: "",
      cursor: 0,
      revision: 0,
      updatedAt: Date.now(),
    }
    const capture: SubmissionCapture = {
      id: captureId(scope, draft.revision),
      scope,
      value: draft.value,
      cursor: draft.cursor,
      draftRevision: draft.revision,
      capturedAt: Date.now(),
    }
    this.#captures.set(capture.id, capture)
    this.#trimCaptures()
    return { ...capture }
  }

  captureValue(scopeValue: string, value: string, cursor?: number): SubmissionCapture {
    this.set(scopeValue, value, cursor)
    return this.capture(scopeValue)
  }

  clearCaptured(captureIdValue: string): boolean {
    this.#assertOpen()
    const capture = this.#captures.get(captureIdValue)
    if (!capture) return false
    const current = this.#drafts.get(capture.scope)
    if (
      current &&
      current.revision === capture.draftRevision &&
      current.value === capture.value
    ) {
      this.#drafts.delete(capture.scope)
      this.#persist()
      this.#emit(undefined)
    }
    return true
  }

  restore(captureIdValue: string): CommandDraft | undefined {
    this.#assertOpen()
    const capture = this.#captures.get(captureIdValue)
    if (!capture) return undefined
    this.#captures.delete(captureIdValue)
    const current = this.#drafts.get(capture.scope)
    const unchanged =
      !current ||
      (current.value === "" && current.revision <= capture.draftRevision + 1)
    if (!unchanged) return undefined
    const restored: CommandDraft = {
      scope: capture.scope,
      value: capture.value,
      cursor: Math.max(0, Math.min(capture.value.length, capture.cursor)),
      revision: Math.max(current?.revision ?? 0, capture.draftRevision) + 1,
      updatedAt: Date.now(),
    }
    if (restored.value) this.#drafts.set(restored.scope, restored)
    else this.#drafts.delete(restored.scope)
    this.#persist()
    this.#emit(restored.value ? restored : undefined)
    return cloneDraft(restored)
  }

  commit(captureIdValue: string): boolean {
    this.#assertOpen()
    const capture = this.#captures.get(captureIdValue)
    if (!capture) return false
    this.#captures.delete(captureIdValue)
    const current = this.#drafts.get(capture.scope)
    if (
      current &&
      (current.value !== capture.value || current.revision > capture.draftRevision + 1)
    ) {
      return true
    }
    this.#drafts.delete(capture.scope)
    this.#persist()
    this.#emit(undefined)
    return true
  }

  move(fromScopeValue: string, toScopeValue: string): CommandDraft | undefined {
    this.#assertOpen()
    const from = normalizeScope(fromScopeValue)
    const to = normalizeScope(toScopeValue)
    if (from === to) return this.get(from)
    const source = this.#drafts.get(from)
    if (!source) return undefined
    const target = this.#drafts.get(to)
    const selected =
      target && target.updatedAt > source.updatedAt
        ? target
        : {
            ...source,
            scope: to,
            revision: (target?.revision ?? 0) + 1,
            updatedAt: Date.now(),
          }
    this.#drafts.delete(from)
    this.#drafts.set(to, selected)
    this.#persist()
    this.#emit(selected)
    return cloneDraft(selected)
  }

  list(): CommandDraft[] {
    return [...this.#drafts.values()]
      .sort((left, right) => right.updatedAt - left.updatedAt)
      .map(cloneDraft)
  }

  listen(listener: (draft: CommandDraft | undefined) => void): () => void {
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    this.#captures.clear()
    this.#listeners.clear()
  }

  #load(): void {
    const raw = this.#storage?.getItem(this.#storageKey)
    if (!raw) return
    try {
      const values = JSON.parse(raw)
      if (!Array.isArray(values)) throw new TypeError("Draft storage must be an array.")
      for (const value of values) {
        if (!validDraft(value) || !value.value) continue
        const scope = normalizeScope(value.scope)
        const draft: CommandDraft = {
          ...value,
          scope,
          cursor: Math.max(0, Math.min(value.value.length, value.cursor)),
        }
        const previous = this.#drafts.get(scope)
        if (!previous || previous.updatedAt < draft.updatedAt) this.#drafts.set(scope, draft)
        if (this.#drafts.size >= 30) break
      }
    } catch {
      this.#storage?.removeItem(this.#storageKey)
      this.#drafts.clear()
    }
  }

  #persist(): void {
    if (!this.#storage) return
    try {
      const values = this.list().slice(0, 30)
      this.#storage.setItem(this.#storageKey, JSON.stringify(values))
    } catch {
      // Privacy mode or quota errors cannot block command execution.
    }
  }

  #trimCaptures(): void {
    if (this.#captures.size <= 100) return
    const ordered = [...this.#captures.values()].sort((left, right) => left.capturedAt - right.capturedAt)
    for (const capture of ordered.slice(0, ordered.length - 100)) {
      this.#captures.delete(capture.id)
    }
  }

  #emit(draft: CommandDraft | undefined): void {
    for (const listener of this.#listeners) {
      try {
        listener(draft ? cloneDraft(draft) : undefined)
      } catch {
        // Draft observers cannot change captured submission semantics.
      }
    }
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Command draft store is closed.")
  }
}

export function browserCommandDraftStore(): CommandDraftStore {
  return new CommandDraftStore({
    storage: typeof localStorage === "undefined" ? undefined : localStorage,
  })
}
