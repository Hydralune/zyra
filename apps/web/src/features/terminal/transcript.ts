import type {
  TerminalBinding,
  TerminalPhase,
  TerminalSpillProjection,
  TerminalStatusProjection,
} from "./contracts.ts"
import { classifyTerminalLine, redactTerminalText, type TerminalStructuredLine } from "./structured.ts"

export type TerminalTranscriptKind =
  | "input"
  | "output"
  | "resize"
  | "status"
  | "permission"
  | "reconnect"
  | "backpressure"
  | "spill"
  | "error"

export interface TerminalTranscriptEntry {
  id: string
  kind: TerminalTranscriptKind
  sequence: number
  createdAt: string
  firstCursor?: number
  nextCursor?: number
  inputSequence?: number
  resizeSequence?: number
  rows?: number
  cols?: number
  phase?: TerminalPhase
  text?: string
  structured?: TerminalStructuredLine
  code?: string
  retryable?: boolean
  permissionEffect?: "allow" | "ask" | "deny"
  spill?: TerminalSpillProjection
  metadata: Readonly<Record<string, string | number | boolean>>
}

export interface TerminalTranscriptSnapshot {
  terminalId: string
  entries: readonly TerminalTranscriptEntry[]
  revision: number
  sequence: number
  droppedEntries: number
  droppedBytes: number
  outputBytes: number
  inputBytes: number
  errors: number
  reconnects: number
  phase?: TerminalPhase
  firstCursor: number
  nextCursor: number
}

interface MutableTranscriptEntry extends TerminalTranscriptEntry {
  byteLength: number
}

export interface TerminalTranscriptOptions {
  maximumEntries?: number
  maximumBytes?: number
  now?: () => number
  secrets?: readonly string[]
}

function frozenMetadata(
  value: Readonly<Record<string, string | number | boolean>> = {},
): Readonly<Record<string, string | number | boolean>> {
  return Object.freeze({ ...value })
}

function byteLength(value: string | undefined): number {
  return value ? new TextEncoder().encode(value).byteLength : 0
}

function entrySize(entry: Omit<MutableTranscriptEntry, "byteLength">): number {
  return (
    byteLength(entry.text)
    + byteLength(entry.code)
    + byteLength(entry.spill?.artifactId)
    + 128
  )
}

function timestamp(now: () => number): string {
  return new Date(now()).toISOString()
}

function statusMetadata(
  status: TerminalStatusProjection,
): Readonly<Record<string, string | number | boolean>> {
  return frozenMetadata({
    cursor: status.cursor,
    earliestCursor: status.earliestCursor,
    viewers: status.viewers,
    spilledBytes: status.spilledBytes,
    binaryBytes: status.binaryBytes,
    stateMutationId: status.stateMutationId,
    ...(status.pid === undefined ? {} : { pid: status.pid }),
    ...(status.exitCode === undefined ? {} : { exitCode: status.exitCode }),
    ...(status.signal ? { signal: status.signal } : {}),
  })
}

export class TerminalTranscript {
  readonly #binding: TerminalBinding
  readonly #maximumEntries: number
  readonly #maximumBytes: number
  readonly #now: () => number
  readonly #secrets: readonly string[]
  #entries: MutableTranscriptEntry[] = []
  #revision = 0
  #sequence = 0
  #bytes = 0
  #droppedEntries = 0
  #droppedBytes = 0
  #outputBytes = 0
  #inputBytes = 0
  #errors = 0
  #reconnects = 0
  #phase?: TerminalPhase
  #firstCursor = 0
  #nextCursor = 0

  constructor(binding: TerminalBinding, options: TerminalTranscriptOptions = {}) {
    this.#binding = binding
    this.#maximumEntries = Math.min(
      100_000,
      Math.max(100, Math.floor(options.maximumEntries ?? 10_000)),
    )
    this.#maximumBytes = Math.min(
      256 * 1_024 * 1_024,
      Math.max(64 * 1_024, Math.floor(options.maximumBytes ?? 8 * 1_024 * 1_024)),
    )
    this.#now = options.now ?? Date.now
    this.#secrets = Object.freeze(
      [...new Set(options.secrets ?? [])]
        .filter((value) => value.length >= 4)
        .sort((left, right) => right.length - left.length),
    )
  }

  input(value: string, inputSequence: number): TerminalTranscriptEntry {
    const redacted = redactTerminalText(value, this.#secrets)
    this.#inputBytes += byteLength(value)
    return this.#append({
      kind: "input",
      inputSequence,
      text: redacted.text,
      metadata: frozenMetadata({
        redacted: redacted.redacted,
        byteLength: byteLength(value),
      }),
    })
  }

  output(
    value: string,
    options: {
      firstCursor: number
      nextCursor: number
      redacted: boolean
      sequence: number
    },
  ): readonly TerminalTranscriptEntry[] {
    const redacted = redactTerminalText(value, this.#secrets)
    const outputBytes = Math.max(0, options.nextCursor - options.firstCursor)
    this.#outputBytes += outputBytes
    if (this.#entries.length === 0 || this.#firstCursor === 0) {
      this.#firstCursor = options.firstCursor
    }
    this.#nextCursor = Math.max(this.#nextCursor, options.nextCursor)
    const pieces = value.split(/\n/u)
    const entries: TerminalTranscriptEntry[] = []
    let cursor = options.firstCursor
    for (let index = 0; index < pieces.length; index += 1) {
      const raw = pieces[index] ?? ""
      const material = index + 1 < pieces.length ? `${raw}\n` : raw
      if (!material) continue
      const length = byteLength(material)
      const nextCursor = Math.min(options.nextCursor, cursor + length)
      const safe = redactTerminalText(material, this.#secrets)
      entries.push(this.#append({
        kind: "output",
        firstCursor: cursor,
        nextCursor,
        text: safe.text,
        structured: classifyTerminalLine(safe.text.replace(/\r?\n$/u, "")),
        metadata: frozenMetadata({
          frameSequence: options.sequence,
          redacted: options.redacted || redacted.redacted || safe.redacted,
          byteLength: length,
        }),
      }))
      cursor = nextCursor
    }
    return Object.freeze(entries)
  }

  resize(rows: number, cols: number, resizeSequence: number): TerminalTranscriptEntry {
    return this.#append({
      kind: "resize",
      resizeSequence,
      rows,
      cols,
      metadata: frozenMetadata(),
    })
  }

  status(status: TerminalStatusProjection): TerminalTranscriptEntry | undefined {
    if (
      this.#phase === status.phase
      && this.#nextCursor === status.cursor
      && this.#entries.at(-1)?.kind === "status"
      && this.#entries.at(-1)?.metadata.stateMutationId === status.stateMutationId
    ) return undefined
    this.#phase = status.phase
    this.#firstCursor = status.earliestCursor
    this.#nextCursor = status.cursor
    return this.#append({
      kind: "status",
      phase: status.phase,
      rows: status.rows,
      cols: status.cols,
      metadata: statusMetadata(status),
    })
  }

  permission(
    effect: "allow" | "ask" | "deny",
    options: { decisionId: string; reasonCode: string; requestId?: string },
  ): TerminalTranscriptEntry {
    return this.#append({
      kind: "permission",
      permissionEffect: effect,
      code: options.reasonCode,
      metadata: frozenMetadata({
        decisionId: options.decisionId,
        ...(options.requestId ? { requestId: options.requestId } : {}),
      }),
    })
  }

  reconnect(
    phase: string,
    options: { generation: number; attempt: number; cursor: number; delayMs?: number },
  ): TerminalTranscriptEntry {
    this.#reconnects += 1
    return this.#append({
      kind: "reconnect",
      code: phase,
      firstCursor: options.cursor,
      nextCursor: options.cursor,
      metadata: frozenMetadata({
        generation: options.generation,
        attempt: options.attempt,
        ...(options.delayMs === undefined ? {} : { delayMs: options.delayMs }),
      }),
    })
  }

  backpressure(
    unackedBytes: number,
    maximumUnackedBytes: number,
    paused: boolean,
  ): TerminalTranscriptEntry {
    return this.#append({
      kind: "backpressure",
      code: paused ? "paused" : "resumed",
      metadata: frozenMetadata({
        unackedBytes,
        maximumUnackedBytes,
        paused,
      }),
    })
  }

  spill(spill: TerminalSpillProjection): TerminalTranscriptEntry {
    return this.#append({
      kind: "spill",
      firstCursor: spill.firstCursor,
      nextCursor: spill.nextCursor,
      spill,
      metadata: frozenMetadata({
        binary: spill.binary,
        redacted: spill.redacted,
        byteLength: spill.byteLength,
      }),
    })
  }

  error(code: string, message: string, retryable: boolean): TerminalTranscriptEntry {
    this.#errors += 1
    const redacted = redactTerminalText(message, this.#secrets)
    return this.#append({
      kind: "error",
      code: code.slice(0, 256),
      text: redacted.text.slice(0, 8_192),
      retryable,
      metadata: frozenMetadata({ redacted: redacted.redacted }),
    })
  }

  snapshot(): TerminalTranscriptSnapshot {
    return Object.freeze({
      terminalId: this.#binding.terminalId,
      entries: Object.freeze(
        this.#entries.map(({ byteLength: _size, ...entry }) => Object.freeze({
          ...entry,
          metadata: frozenMetadata(entry.metadata),
        })),
      ),
      revision: this.#revision,
      sequence: this.#sequence,
      droppedEntries: this.#droppedEntries,
      droppedBytes: this.#droppedBytes,
      outputBytes: this.#outputBytes,
      inputBytes: this.#inputBytes,
      errors: this.#errors,
      reconnects: this.#reconnects,
      phase: this.#phase,
      firstCursor: this.#firstCursor,
      nextCursor: this.#nextCursor,
    })
  }

  clear(): TerminalTranscriptSnapshot {
    this.#droppedEntries += this.#entries.length
    this.#droppedBytes += this.#bytes
    this.#entries = []
    this.#bytes = 0
    this.#revision += 1
    return this.snapshot()
  }

  #append(
    value: Omit<
      TerminalTranscriptEntry,
      "id" | "sequence" | "createdAt"
    >,
  ): TerminalTranscriptEntry {
    const sequence = ++this.#sequence
    const entryWithoutSize = {
      ...value,
      id: `${this.#binding.terminalId}:transcript:${sequence}`,
      sequence,
      createdAt: timestamp(this.#now),
      metadata: frozenMetadata(value.metadata),
    } satisfies Omit<MutableTranscriptEntry, "byteLength">
    const entry: MutableTranscriptEntry = {
      ...entryWithoutSize,
      byteLength: entrySize(entryWithoutSize),
    }
    this.#entries.push(entry)
    this.#bytes += entry.byteLength
    this.#trim()
    this.#revision += 1
    const { byteLength: _size, ...projection } = entry
    return Object.freeze(projection)
  }

  #trim(): void {
    while (
      this.#entries.length > this.#maximumEntries
      || this.#bytes > this.#maximumBytes
    ) {
      const removed = this.#entries.shift()
      if (!removed) break
      this.#bytes = Math.max(0, this.#bytes - removed.byteLength)
      this.#droppedEntries += 1
      this.#droppedBytes += removed.byteLength
    }
  }
}
