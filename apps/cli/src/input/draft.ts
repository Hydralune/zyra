import { createHash } from "node:crypto"

const LARGE_PASTE_BYTES = 8 * 1024

export interface DraftSnapshot {
  text: string
  cursor: number
  display: string
  pasteRefs: readonly string[]
}

function boundedCursor(text: string, cursor: number): number {
  return Math.max(0, Math.min(text.length, Math.floor(cursor)))
}

export class PromptDraft {
  #text = ""
  #cursor = 0
  #stashed: DraftSnapshot | undefined
  readonly #pastes = new Map<string, string>()

  get empty(): boolean { return this.#text.length === 0 }

  snapshot(): DraftSnapshot {
    const refs = [...this.#pastes.keys()].filter((ref) => this.#text.includes(ref))
    return Object.freeze({
      text: this.#text,
      cursor: this.#cursor,
      display: this.#text,
      pasteRefs: Object.freeze(refs),
    })
  }

  set(text: string, cursor = text.length): DraftSnapshot {
    this.#text = text
    this.#cursor = boundedCursor(text, cursor)
    return this.snapshot()
  }

  insert(text: string): DraftSnapshot {
    const next = `${this.#text.slice(0, this.#cursor)}${text}${this.#text.slice(this.#cursor)}`
    return this.set(next, this.#cursor + text.length)
  }

  deleteBackward(): DraftSnapshot {
    if (this.#cursor <= 0) return this.snapshot()
    const previous = this.#cursor - 1
    this.#text = `${this.#text.slice(0, previous)}${this.#text.slice(this.#cursor)}`
    this.#cursor = previous
    return this.snapshot()
  }

  move(offset: number): DraftSnapshot {
    this.#cursor = boundedCursor(this.#text, this.#cursor + offset)
    return this.snapshot()
  }

  newline(): DraftSnapshot { return this.insert("\n") }

  paste(text: string): DraftSnapshot {
    const bytes = new TextEncoder().encode(text).byteLength
    if (bytes < LARGE_PASTE_BYTES) return this.insert(text)
    const digest = createHash("sha256").update(text).digest("hex").slice(0, 16)
    const lines = text.split(/\r?\n/).length
    const ref = `@paste:${digest}:${lines}`
    this.#pastes.set(ref, text)
    return this.insert(ref)
  }

  submit(): string | undefined {
    let expanded = this.#text
    for (const [ref, value] of this.#pastes) expanded = expanded.replaceAll(ref, value)
    if (!expanded.trim()) return undefined
    this.#text = ""
    this.#cursor = 0
    this.#pastes.clear()
    return expanded
  }

  cancel(): DraftSnapshot | undefined {
    if (this.empty) return undefined
    this.#stashed = this.snapshot()
    this.#text = ""
    this.#cursor = 0
    return this.#stashed
  }

  restore(): DraftSnapshot | undefined {
    if (!this.#stashed) return undefined
    const restored = this.#stashed
    this.#stashed = undefined
    this.#text = restored.text
    this.#cursor = restored.cursor
    return this.snapshot()
  }

  complete(candidates: readonly string[]): readonly string[] {
    const before = this.#text.slice(0, this.#cursor)
    const token = before.match(/(?:^|\s)([/@][^\s]*)$/)?.[1] ?? ""
    if (!token) return []
    return candidates.filter((candidate) => candidate.startsWith(token)).sort()
  }
}

export class PromptHistory {
  readonly #entries: string[] = []
  readonly #limit: number
  #index = 0

  constructor(limit = 500) { this.#limit = Math.max(1, limit) }

  push(value: string): void {
    const selected = value.trim()
    if (!selected || this.#entries.at(-1) === value) return
    this.#entries.push(value)
    if (this.#entries.length > this.#limit) this.#entries.shift()
    this.#index = this.#entries.length
  }

  previous(currentLine: number): string | undefined {
    if (currentLine !== 0 || !this.#entries.length) return undefined
    this.#index = Math.max(0, this.#index - 1)
    return this.#entries[this.#index]
  }

  next(currentLine: number, totalLines: number): string | undefined {
    if (currentLine !== totalLines - 1 || !this.#entries.length) return undefined
    this.#index = Math.min(this.#entries.length, this.#index + 1)
    return this.#index === this.#entries.length ? "" : this.#entries[this.#index]
  }

  search(query: string): string | undefined {
    const lowered = query.toLowerCase()
    return [...this.#entries].reverse().find((entry) => entry.toLowerCase().includes(lowered))
  }
}
