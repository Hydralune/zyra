import type { Readable, Writable } from "node:stream"

type RawInput = Readable & { setRawMode?: (enabled: boolean) => void }

const activeSessions = new Set<TerminalSessionGuard>()
// Replay the safe baseline before enabling paste mode. This repairs stale terminal
// state left behind by an uncatchable previous process termination on next launch.
const TERMINAL_SAFE_BASELINE = "\u001b[0m\u001b[?25h\u001b[?2004l"
const TERMINAL_RESTORE = "\u001b[0m\u001b[?25h\u001b[?2004l"

export interface TerminalSessionOptions {
  bracketedPaste?: boolean
}

export class TerminalSessionGuard {
  readonly #input: RawInput
  readonly #output: Writable
  readonly #bracketedPaste: boolean
  #active = false

  constructor(input: Readable, output: Writable, options: TerminalSessionOptions = {}) {
    this.#input = input as RawInput
    this.#output = output
    // Windows terminals can deliver paste as rapid key events and an
    // uncatchable TerminateProcess cannot run cleanup. Avoid leaving a
    // persistent terminal mode behind and let the composer use paste-burst
    // detection instead.
    this.#bracketedPaste = options.bracketedPaste ?? process.platform !== "win32"
  }

  get active(): boolean { return this.#active }
  get bracketedPasteEnabled(): boolean { return this.#bracketedPaste }
  ownsInput(input: Readable): boolean { return this.#active && this.#input === input }

  enter(): void {
    if (this.#active) { this.#input.resume(); return }
    this.#active = true
    activeSessions.add(this)
    this.#input.setRawMode?.(true)
    this.#input.resume()
    this.#write(`${TERMINAL_SAFE_BASELINE}${this.#bracketedPaste ? "\u001b[?2004h" : ""}`)
  }

  suspend(): void {
    this.#input.pause()
  }

  restore(): void {
    if (!this.#active) return
    this.#active = false
    activeSessions.delete(this)
    try { this.#input.setRawMode?.(false) } catch { /* emergency cleanup is best effort */ }
    try { this.#input.pause() } catch { /* emergency cleanup is best effort */ }
    this.#write(TERMINAL_RESTORE)
  }

  #write(value: string): void {
    try { this.#output.write(value) } catch { /* output may already be unavailable */ }
  }
}

export function terminalSessionOwnsInput(input: Readable): boolean {
  return [...activeSessions].some((session) => session.ownsInput(input))
}

export function emergencyTerminalCleanup(): void {
  for (const session of [...activeSessions]) session.restore()
}

export function installEmergencyTerminalCleanup(): () => void {
  const cleanup = () => emergencyTerminalCleanup()
  process.on("uncaughtExceptionMonitor", cleanup)
  process.on("exit", cleanup)
  return () => {
    process.removeListener("uncaughtExceptionMonitor", cleanup)
    process.removeListener("exit", cleanup)
  }
}
