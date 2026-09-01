export const PASTE_BURST_CHAR_INTERVAL_MS = 8
export const PASTE_BURST_IDLE_MS = 60
export const PASTE_BURST_ENTER_WINDOW_MS = 120

export type PasteBurstCharDecision =
  | { kind: "hold" }
  | { kind: "insert" }
  | { kind: "buffer" }
  | { kind: "retro"; characters: number }

export type PasteBurstFlush =
  | { kind: "none" }
  | { kind: "typed"; text: string }
  | { kind: "paste"; text: string }
  | { kind: "overflow" }

export type PasteBurstNewlineDecision = "buffer" | "insert" | "submit"

/**
 * Detects the rapid key streams used for paste by Windows terminals that do
 * not reliably deliver bracketed-paste sequences. It deliberately does not
 * touch the draft so the composer remains the sole input-state owner.
 */
export class PasteBurstDetector {
  readonly #maxBytes: number
  #lastCharacterAt: number | undefined
  #consecutiveCharacters = 0
  #enterWindowUntil: number | undefined
  #held: { character: string; at: number } | undefined
  #buffer = ""
  #bufferBytes = 0
  #active = false
  #overflow = false

  constructor(maxBytes: number) {
    this.#maxBytes = Math.max(1, Math.floor(maxBytes))
  }

  onCharacter(character: string, now: number): PasteBurstCharDecision {
    this.#noteCharacter(now)
    if (this.#active) {
      this.#append(character)
      this.#extendEnterWindow(now)
      return { kind: "buffer" }
    }

    const ascii = /^[\x20-\x7e]$/u.test(character)
    if (ascii && this.#held && now - this.#held.at <= PASTE_BURST_CHAR_INTERVAL_MS) {
      this.#active = true
      this.#append(this.#held.character)
      this.#held = undefined
      this.#append(character)
      this.#extendEnterWindow(now)
      return { kind: "buffer" }
    }

    if (this.#consecutiveCharacters >= 3) {
      return { kind: "retro", characters: this.#consecutiveCharacters - 1 }
    }

    if (ascii) {
      this.#held = { character, at: now }
      return { kind: "hold" }
    }
    return { kind: "insert" }
  }

  acceptRetroactive(grabbed: string, character: string, now: number): void {
    this.#active = true
    this.#append(grabbed)
    this.#append(character)
    this.#extendEnterWindow(now)
  }

  onNewline(now: number): PasteBurstNewlineDecision {
    if (this.#active || this.#held) {
      if (this.#held) {
        this.#active = true
        this.#append(this.#held.character)
        this.#held = undefined
      }
      this.#append("\n")
      this.#lastCharacterAt = now
      this.#extendEnterWindow(now)
      return "buffer"
    }
    if (this.#enterWindowUntil !== undefined && now <= this.#enterWindowUntil) {
      this.#extendEnterWindow(now)
      return "insert"
    }
    this.#enterWindowUntil = undefined
    return "submit"
  }

  flushIfDue(now: number): PasteBurstFlush {
    if (this.#active && this.#lastCharacterAt !== undefined
      && now - this.#lastCharacterAt > PASTE_BURST_IDLE_MS) {
      return this.#takeBuffered()
    }
    if (this.#held && now - this.#held.at > PASTE_BURST_CHAR_INTERVAL_MS) {
      const text = this.#held.character
      this.#held = undefined
      return { kind: "typed", text }
    }
    return { kind: "none" }
  }

  flushBeforeControl(): PasteBurstFlush {
    const result = this.#active || this.#overflow
      ? this.#takeBuffered()
      : this.#held
        ? { kind: "typed" as const, text: this.#held.character }
        : { kind: "none" as const }
    this.resetClassification()
    return result
  }

  nextFlushDelay(now: number): number | undefined {
    if (this.#active && this.#lastCharacterAt !== undefined) {
      return Math.max(1, PASTE_BURST_IDLE_MS + 1 - (now - this.#lastCharacterAt))
    }
    if (this.#held) return Math.max(1, PASTE_BURST_CHAR_INTERVAL_MS + 1 - (now - this.#held.at))
    return undefined
  }

  resetClassification(): void {
    this.#lastCharacterAt = undefined
    this.#consecutiveCharacters = 0
    this.#enterWindowUntil = undefined
    this.#held = undefined
    this.#buffer = ""
    this.#bufferBytes = 0
    this.#active = false
    this.#overflow = false
  }

  #noteCharacter(now: number): void {
    this.#consecutiveCharacters = this.#lastCharacterAt !== undefined
      && now - this.#lastCharacterAt <= PASTE_BURST_CHAR_INTERVAL_MS
      ? this.#consecutiveCharacters + 1
      : 1
    this.#lastCharacterAt = now
  }

  #extendEnterWindow(now: number): void {
    this.#enterWindowUntil = now + PASTE_BURST_ENTER_WINDOW_MS
  }

  #append(value: string): void {
    if (this.#overflow || !value) return
    const bytes = Buffer.byteLength(value, "utf8")
    if (this.#bufferBytes + bytes > this.#maxBytes) {
      this.#buffer = ""
      this.#bufferBytes = 0
      this.#overflow = true
      return
    }
    this.#buffer += value
    this.#bufferBytes += bytes
  }

  #takeBuffered(): PasteBurstFlush {
    const overflow = this.#overflow
    const text = this.#buffer
    this.#buffer = ""
    this.#bufferBytes = 0
    this.#active = false
    this.#overflow = false
    return overflow ? { kind: "overflow" } : text ? { kind: "paste", text } : { kind: "none" }
  }
}
