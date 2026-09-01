import { createHash } from "node:crypto"

const LARGE_PASTE_BYTES = 8 * 1024
export const MAX_PROMPT_BYTES = 256 * 1024

export class PromptInputLimitError extends RangeError {
  constructor() {
    super(`输入超过 ${MAX_PROMPT_BYTES} bytes；请改用文件或 artifact 引用。`)
    this.name = "PromptInputLimitError"
  }
}

export interface DraftSnapshot {
  text: string
  cursor: number
  display: string
  pasteRefs: readonly string[]
}

export interface DraftPersistenceSnapshot {
  text: string
  cursor: number
}

interface DraftState {
  text: string
  cursor: number
  pastes: Map<string, string>
}

const segmenter = typeof Intl.Segmenter === "function"
  ? new Intl.Segmenter(undefined, { granularity: "grapheme" })
  : undefined

function boundaries(text: string): number[] {
  if (!segmenter) {
    const result = [0]
    let offset = 0
    for (const value of Array.from(text)) {
      offset += value.length
      result.push(offset)
    }
    return result
  }
  const result = [...segmenter.segment(text)].map((item) => item.index)
  if (result[0] !== 0) result.unshift(0)
  if (result.at(-1) !== text.length) result.push(text.length)
  return result
}

function boundedCursor(text: string, cursor: number): number {
  return Math.max(0, Math.min(text.length, Math.floor(cursor)))
}

export class PromptDraft {
  #text = ""
  #cursor = 0
  #stashed: DraftSnapshot | undefined
  readonly #pastes = new Map<string, string>()
  readonly #undo: DraftState[] = []
  readonly #redo: DraftState[] = []
  readonly #undoLimit = 200

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

  persistenceSnapshot(): DraftPersistenceSnapshot {
    let text = this.#text
    let cursor = this.#cursor
    for (const [ref, value] of this.#pastes) {
      let offset = 0
      while (true) {
        const index = text.indexOf(ref, offset)
        if (index < 0) break
        text = `${text.slice(0, index)}${value}${text.slice(index + ref.length)}`
        if (index < cursor) cursor += value.length - ref.length
        offset = index + value.length
      }
    }
    return Object.freeze({ text, cursor: boundedCursor(text, cursor) })
  }

  set(text: string, cursor = text.length, record = true): DraftSnapshot {
    if (Buffer.byteLength(text, "utf8") > MAX_PROMPT_BYTES) throw new PromptInputLimitError()
    if (record && (text !== this.#text || cursor !== this.#cursor)) this.#checkpoint()
    this.#text = text
    this.#cursor = boundedCursor(text, cursor)
    return this.snapshot()
  }

  insert(text: string): DraftSnapshot {
    if (!text) return this.snapshot()
    const next = `${this.#text.slice(0, this.#cursor)}${text}${this.#text.slice(this.#cursor)}`
    if (Buffer.byteLength(next, "utf8") > MAX_PROMPT_BYTES) throw new PromptInputLimitError()
    this.#checkpoint()
    this.#text = next
    this.#cursor += text.length
    return this.snapshot()
  }

  deleteBackward(): DraftSnapshot {
    if (this.#cursor <= 0) return this.snapshot()
    this.#checkpoint()
    const previous = boundaries(this.#text).filter((value) => value < this.#cursor).at(-1) ?? 0
    this.#text = `${this.#text.slice(0, previous)}${this.#text.slice(this.#cursor)}`
    this.#cursor = previous
    return this.snapshot()
  }

  deleteForward(): DraftSnapshot {
    if (this.#cursor >= this.#text.length) return this.snapshot()
    this.#checkpoint()
    const next = boundaries(this.#text).find((value) => value > this.#cursor) ?? this.#text.length
    this.#text = `${this.#text.slice(0, this.#cursor)}${this.#text.slice(next)}`
    return this.snapshot()
  }

  move(offset: number): DraftSnapshot {
    const points = boundaries(this.#text)
    const current = Math.max(0, points.findIndex((value) => value >= this.#cursor))
    this.#cursor = points[Math.max(0, Math.min(points.length - 1, current + Math.trunc(offset)))] ?? this.#cursor
    return this.snapshot()
  }

  moveWord(direction: -1 | 1): DraftSnapshot {
    if (direction < 0) {
      const before = this.#text.slice(0, this.#cursor)
      this.#cursor = before.search(/\S+\s*$/u)
      if (this.#cursor < 0) this.#cursor = 0
    } else {
      const after = this.#text.slice(this.#cursor)
      const match = after.match(/^\s*\S+/u)
      this.#cursor = boundedCursor(this.#text, this.#cursor + (match?.[0].length ?? after.length))
    }
    return this.snapshot()
  }

  home(): DraftSnapshot {
    this.#cursor = (this.#text.lastIndexOf("\n", Math.max(0, this.#cursor - 1)) + 1)
    return this.snapshot()
  }

  end(): DraftSnapshot {
    const next = this.#text.indexOf("\n", this.#cursor)
    this.#cursor = next < 0 ? this.#text.length : next
    return this.snapshot()
  }

  undo(): DraftSnapshot {
    const prior = this.#undo.pop()
    if (!prior) return this.snapshot()
    this.#redo.push(this.#capture())
    this.#restoreState(prior)
    return this.snapshot()
  }

  redo(): DraftSnapshot {
    const next = this.#redo.pop()
    if (!next) return this.snapshot()
    this.#undo.push(this.#capture())
    this.#restoreState(next)
    return this.snapshot()
  }

  newline(): DraftSnapshot { return this.insert("\n") }

  paste(text: string): DraftSnapshot {
    const bytes = new TextEncoder().encode(text).byteLength
    if (bytes + Buffer.byteLength(this.#text, "utf8") > MAX_PROMPT_BYTES) throw new PromptInputLimitError()
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
    this.#undo.length = 0
    this.#redo.length = 0
    return expanded
  }

  cancel(): DraftSnapshot | undefined {
    if (this.empty) return undefined
    this.#stashed = this.snapshot()
    this.#text = ""
    this.#cursor = 0
    this.#undo.length = 0
    this.#redo.length = 0
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

  #capture(): DraftState {
    return { text: this.#text, cursor: this.#cursor, pastes: new Map(this.#pastes) }
  }

  #restoreState(state: DraftState): void {
    this.#text = state.text
    this.#cursor = state.cursor
    this.#pastes.clear()
    for (const [key, value] of state.pastes) this.#pastes.set(key, value)
  }

  #checkpoint(): void {
    this.#undo.push(this.#capture())
    if (this.#undo.length > this.#undoLimit) this.#undo.shift()
    this.#redo.length = 0
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
