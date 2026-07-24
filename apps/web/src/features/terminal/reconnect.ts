export type TerminalConnectionPhase =
  | "idle"
  | "ticket"
  | "connecting"
  | "open"
  | "backoff"
  | "closed"
  | "failed"

export interface TerminalReconnectSnapshot {
  phase: TerminalConnectionPhase
  generation: number
  attempt: number
  cursor: number
  ticketIssuedAt?: number
  connectedAt?: number
  retryAt?: number
  lastCloseCode?: number
  lastError?: string
}

export class TerminalReconnectCoordinator {
  readonly #baseDelayMs: number
  readonly #maximumDelayMs: number
  readonly #maximumAttempts: number
  readonly #jitter: (maximum: number) => number
  #state: TerminalReconnectSnapshot

  constructor(options: {
    cursor?: number
    baseDelayMs?: number
    maximumDelayMs?: number
    maximumAttempts?: number
    jitter?: (maximum: number) => number
  } = {}) {
    this.#baseDelayMs = Math.max(25, Math.floor(options.baseDelayMs ?? 250))
    this.#maximumDelayMs = Math.max(this.#baseDelayMs, Math.floor(options.maximumDelayMs ?? 4_000))
    this.#maximumAttempts = Math.max(1, Math.floor(options.maximumAttempts ?? 12))
    this.#jitter = options.jitter ?? ((maximum) => Math.floor(Math.random() * Math.max(1, maximum)))
    this.#state = Object.freeze({
      phase: "idle",
      generation: 0,
      attempt: 0,
      cursor: this.#cursor(options.cursor ?? 0),
    })
  }

  requestTicket(now = Date.now()): TerminalReconnectSnapshot {
    if (this.#state.phase === "closed") return this.#state
    this.#state = Object.freeze({
      ...this.#state,
      phase: "ticket",
      generation: this.#state.generation + 1,
      ticketIssuedAt: now,
      retryAt: undefined,
      lastError: undefined,
    })
    return this.#state
  }

  connecting(generation: number): TerminalReconnectSnapshot {
    if (generation !== this.#state.generation || this.#state.phase !== "ticket") return this.#state
    this.#state = Object.freeze({ ...this.#state, phase: "connecting" })
    return this.#state
  }

  opened(generation: number, cursor: number, now = Date.now()): TerminalReconnectSnapshot {
    if (generation !== this.#state.generation) return this.#state
    if (this.#state.phase !== "connecting" && this.#state.phase !== "ticket") return this.#state
    this.#state = Object.freeze({
      ...this.#state,
      phase: "open",
      attempt: 0,
      cursor: this.#cursor(cursor),
      connectedAt: now,
      retryAt: undefined,
      lastCloseCode: undefined,
      lastError: undefined,
    })
    return this.#state
  }

  advanceCursor(cursor: number): TerminalReconnectSnapshot {
    const selected = this.#cursor(cursor)
    if (selected < this.#state.cursor) return this.#state
    this.#state = Object.freeze({ ...this.#state, cursor: selected })
    return this.#state
  }

  disconnected(
    options: { code?: number; reason?: string; retryable?: boolean; now?: number } = {},
  ): TerminalReconnectSnapshot {
    if (this.#state.phase === "closed") return this.#state
    const code = options.code
    const retryable = options.retryable ?? (code !== 1000 && code !== 1008)
    if (!retryable || this.#state.attempt >= this.#maximumAttempts) {
      this.#state = Object.freeze({
        ...this.#state,
        phase: "failed",
        lastCloseCode: code,
        lastError: options.reason || "Terminal connection ended.",
        retryAt: undefined,
      })
      return this.#state
    }
    const attempt = this.#state.attempt + 1
    const exponential = Math.min(this.#maximumDelayMs, this.#baseDelayMs * 2 ** Math.min(10, attempt - 1))
    const delay = Math.min(this.#maximumDelayMs, exponential + this.#jitter(Math.ceil(exponential * 0.2)))
    this.#state = Object.freeze({
      ...this.#state,
      phase: "backoff",
      attempt,
      retryAt: (options.now ?? Date.now()) + delay,
      lastCloseCode: code,
      lastError: options.reason,
    })
    return this.#state
  }

  ready(now = Date.now()): boolean {
    return this.#state.phase === "backoff" && (this.#state.retryAt ?? Infinity) <= now
  }

  retry(now = Date.now()): TerminalReconnectSnapshot {
    if (!this.ready(now)) return this.#state
    return this.requestTicket(now)
  }

  fail(error: unknown): TerminalReconnectSnapshot {
    this.#state = Object.freeze({
      ...this.#state,
      phase: "failed",
      lastError: error instanceof Error ? error.message : String(error),
      retryAt: undefined,
    })
    return this.#state
  }

  close(code = 1000): TerminalReconnectSnapshot {
    this.#state = Object.freeze({
      ...this.#state,
      phase: "closed",
      lastCloseCode: code,
      retryAt: undefined,
    })
    return this.#state
  }

  snapshot(): TerminalReconnectSnapshot {
    return this.#state
  }

  #cursor(value: number): number {
    if (!Number.isSafeInteger(value) || value < 0) throw new RangeError("Terminal cursor is invalid.")
    return value
  }
}

export function terminalSocketUrl(
  baseUrl: string,
  path: string,
  options: { ticket: string; cursor: number; protocol?: string },
): string {
  const base = new URL(baseUrl)
  if (base.protocol !== "http:" && base.protocol !== "https:") {
    throw new TypeError("Terminal API URL must use HTTP or HTTPS.")
  }
  if (
    !path.startsWith("/") || path.startsWith("//") || path.includes("\\")
    || path.split("/").some((part) => part === "." || part === "..")
  ) throw new TypeError("Terminal socket path is unsafe.")
  if (!options.ticket || options.ticket.length > 8_192) throw new TypeError("Terminal ticket is invalid.")
  if (!Number.isSafeInteger(options.cursor) || options.cursor < 0) throw new TypeError("Terminal cursor is invalid.")
  const url = new URL(path, base)
  if (url.origin !== base.origin) throw new TypeError("Terminal socket changed origin.")
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:"
  url.searchParams.set("ticket", options.ticket)
  url.searchParams.set("cursor", String(options.cursor))
  url.searchParams.set("protocol", options.protocol ?? "zyra.terminal.v1")
  return url.toString()
}
