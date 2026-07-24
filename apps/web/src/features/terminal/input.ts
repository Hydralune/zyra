export interface TerminalKeyboardEvent {
  key: string
  code?: string
  ctrlKey?: boolean
  altKey?: boolean
  shiftKey?: boolean
  metaKey?: boolean
  repeat?: boolean
}

export interface TerminalInputModes {
  applicationCursorKeys: boolean
  bracketedPaste: boolean
  newlineMode?: boolean
}

export interface TerminalInputBudgetOptions {
  maximumFrameBytes?: number
  maximumBurstBytes?: number
  refillBytesPerSecond?: number
  maximumPasteBytes?: number
}

export interface TerminalInputReservation {
  accepted: boolean
  bytes: number
  available: number
  retryAfterMs: number
  reason?: "empty" | "frame_too_large" | "burst_exhausted" | "paste_too_large"
}

const encoder = new TextEncoder()

function ctrlCharacter(value: string): string | undefined {
  if (value.length !== 1) return undefined
  const code = value.toUpperCase().charCodeAt(0)
  if (code >= 64 && code <= 95) return String.fromCharCode(code - 64)
  if (value === " ") return "\u0000"
  return undefined
}

function modifierParameter(event: TerminalKeyboardEvent): number {
  let value = 1
  if (event.shiftKey) value += 1
  if (event.altKey) value += 2
  if (event.ctrlKey) value += 4
  if (event.metaKey) value += 8
  return value
}

function cursorSequence(
  final: "A" | "B" | "C" | "D" | "H" | "F",
  event: TerminalKeyboardEvent,
  modes: TerminalInputModes,
): string {
  const modifier = modifierParameter(event)
  if (modifier === 1 && modes.applicationCursorKeys) return `\u001bO${final}`
  if (modifier === 1) return `\u001b[${final}`
  return `\u001b[1;${modifier}${final}`
}

function tildeSequence(code: number, event: TerminalKeyboardEvent): string {
  const modifier = modifierParameter(event)
  if (modifier === 1) return `\u001b[${code}~`
  return `\u001b[${code};${modifier}~`
}

function functionSequence(index: number, event: TerminalKeyboardEvent): string | undefined {
  const modifier = modifierParameter(event)
  if (index >= 1 && index <= 4) {
    const final = ["P", "Q", "R", "S"][index - 1]!
    if (modifier === 1) return `\u001bO${final}`
    return `\u001b[1;${modifier}${final}`
  }
  const codes = [15, 17, 18, 19, 20, 21, 23, 24, 25, 26, 28, 29]
  const code = codes[index - 5]
  return code === undefined ? undefined : tildeSequence(code, event)
}

export function normalizeTerminalKey(
  event: TerminalKeyboardEvent,
  modes: TerminalInputModes,
): string | undefined {
  if (event.metaKey && !event.altKey && !event.ctrlKey) return undefined
  if (event.key === "Enter") return modes.newlineMode ? "\r\n" : "\r"
  if (event.key === "Tab") return event.shiftKey ? "\u001b[Z" : "\t"
  if (event.key === "Backspace") return event.altKey ? "\u001b\u007f" : "\u007f"
  if (event.key === "Escape" || event.key === "Esc") return "\u001b"
  if (event.key === "ArrowUp") return cursorSequence("A", event, modes)
  if (event.key === "ArrowDown") return cursorSequence("B", event, modes)
  if (event.key === "ArrowRight") return cursorSequence("C", event, modes)
  if (event.key === "ArrowLeft") return cursorSequence("D", event, modes)
  if (event.key === "Home") return cursorSequence("H", event, modes)
  if (event.key === "End") return cursorSequence("F", event, modes)
  if (event.key === "Insert") return tildeSequence(2, event)
  if (event.key === "Delete") return tildeSequence(3, event)
  if (event.key === "PageUp") return tildeSequence(5, event)
  if (event.key === "PageDown") return tildeSequence(6, event)
  const functionMatch = /^F(\d{1,2})$/u.exec(event.key)
  if (functionMatch) return functionSequence(Number(functionMatch[1]), event)
  if (event.ctrlKey && !event.metaKey) {
    const control = ctrlCharacter(event.key)
    if (control !== undefined) return event.altKey ? `\u001b${control}` : control
  }
  if (event.key.length === 1 && !event.ctrlKey) {
    return event.altKey ? `\u001b${event.key}` : event.key
  }
  return undefined
}

export function normalizeTerminalPaste(
  value: string,
  modes: TerminalInputModes,
  maximumBytes = 256 * 1_024,
): string {
  const normalized = value
    .replace(/\u0000/gu, "")
    .replace(/\r\n|\r|\n/gu, "\r")
  const bytes = encoder.encode(normalized)
  if (bytes.byteLength > maximumBytes) {
    throw new RangeError(`Terminal paste exceeds ${maximumBytes} bytes.`)
  }
  if (!modes.bracketedPaste) return normalized
  return `\u001b[200~${normalized.replace(/\u001b\[201~/gu, "")}\u001b[201~`
}

export function splitTerminalInput(
  value: string,
  maximumFrameBytes = 16 * 1_024,
): readonly string[] {
  if (maximumFrameBytes < 1) throw new RangeError("Terminal frame budget must be positive.")
  const chunks: string[] = []
  let current = ""
  let currentBytes = 0
  for (const character of value) {
    const bytes = encoder.encode(character).byteLength
    if (bytes > maximumFrameBytes) throw new RangeError("One terminal character exceeds the frame budget.")
    if (currentBytes + bytes > maximumFrameBytes && current) {
      chunks.push(current)
      current = ""
      currentBytes = 0
    }
    current += character
    currentBytes += bytes
  }
  if (current) chunks.push(current)
  return Object.freeze(chunks)
}

export class TerminalInputBudget {
  readonly #maximumFrameBytes: number
  readonly #maximumBurstBytes: number
  readonly #refillBytesPerSecond: number
  readonly #maximumPasteBytes: number
  #available: number
  #updatedAt: number

  constructor(options: TerminalInputBudgetOptions = {}, now = Date.now()) {
    this.#maximumFrameBytes = Math.min(
      256 * 1_024,
      Math.max(256, Math.floor(options.maximumFrameBytes ?? 16 * 1_024)),
    )
    this.#maximumBurstBytes = Math.min(
      4 * 1024 * 1024,
      Math.max(this.#maximumFrameBytes, Math.floor(options.maximumBurstBytes ?? 256 * 1_024)),
    )
    this.#refillBytesPerSecond = Math.min(
      4 * 1024 * 1024,
      Math.max(1, Math.floor(options.refillBytesPerSecond ?? 64 * 1_024)),
    )
    this.#maximumPasteBytes = Math.min(
      4 * 1024 * 1024,
      Math.max(this.#maximumFrameBytes, Math.floor(options.maximumPasteBytes ?? 256 * 1_024)),
    )
    this.#available = this.#maximumBurstBytes
    this.#updatedAt = now
  }

  reserve(value: string, options: { paste?: boolean; now?: number } = {}): TerminalInputReservation {
    const now = options.now ?? Date.now()
    this.#refill(now)
    const bytes = encoder.encode(value).byteLength
    if (bytes === 0) {
      return { accepted: false, bytes, available: Math.floor(this.#available), retryAfterMs: 0, reason: "empty" }
    }
    if (bytes > this.#maximumFrameBytes && !options.paste) {
      return {
        accepted: false,
        bytes,
        available: Math.floor(this.#available),
        retryAfterMs: 0,
        reason: "frame_too_large",
      }
    }
    if (options.paste && bytes > this.#maximumPasteBytes) {
      return {
        accepted: false,
        bytes,
        available: Math.floor(this.#available),
        retryAfterMs: 0,
        reason: "paste_too_large",
      }
    }
    if (bytes > this.#available) {
      const missing = bytes - this.#available
      return {
        accepted: false,
        bytes,
        available: Math.floor(this.#available),
        retryAfterMs: Math.ceil(missing / this.#refillBytesPerSecond * 1_000),
        reason: "burst_exhausted",
      }
    }
    this.#available -= bytes
    return {
      accepted: true,
      bytes,
      available: Math.floor(this.#available),
      retryAfterMs: 0,
    }
  }

  snapshot(now = Date.now()): Readonly<Record<string, number>> {
    this.#refill(now)
    return Object.freeze({
      maximumFrameBytes: this.#maximumFrameBytes,
      maximumBurstBytes: this.#maximumBurstBytes,
      refillBytesPerSecond: this.#refillBytesPerSecond,
      maximumPasteBytes: this.#maximumPasteBytes,
      available: Math.floor(this.#available),
      updatedAt: this.#updatedAt,
    })
  }

  #refill(now: number): void {
    if (!Number.isFinite(now) || now <= this.#updatedAt) return
    const elapsed = (now - this.#updatedAt) / 1_000
    this.#available = Math.min(
      this.#maximumBurstBytes,
      this.#available + elapsed * this.#refillBytesPerSecond,
    )
    this.#updatedAt = now
  }
}
