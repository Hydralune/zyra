import type { Readable, Writable } from "node:stream"

type RawInput = Readable & { setRawMode?: (enabled: boolean) => void }

const activeSessions = new Set<TerminalSessionGuard>()
// Replay the safe baseline before enabling paste mode. This repairs stale terminal
// state left behind by an uncatchable previous process termination on next launch.
const TERMINAL_ENTER = "\u001b[0m\u001b[?25h\u001b[?2004l\u001b[?2004h"
const TERMINAL_RESTORE = "\u001b[0m\u001b[?25h\u001b[?2004l"

export class TerminalSessionGuard {
  readonly #input: RawInput
  readonly #output: Writable
  #active = false

  constructor(input: Readable, output: Writable) {
    this.#input = input as RawInput
    this.#output = output
  }

  get active(): boolean { return this.#active }

  enter(): void {
    if (this.#active) return
    this.#active = true
    activeSessions.add(this)
    this.#input.setRawMode?.(true)
    this.#input.resume()
    this.#write(TERMINAL_ENTER)
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
