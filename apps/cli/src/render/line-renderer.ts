import type { Writable } from "node:stream"
import type { SessionViewSnapshot, TranscriptRecord } from "../session/projection.ts"

const CLEAR_LINE = "\r\u001b[2K"

function widthOf(output: Writable): number {
  const columns = "columns" in output && typeof output.columns === "number" ? output.columns : 120
  return Math.max(40, Math.floor(columns))
}

function wrap(value: string, width: number): string[] {
  const result: string[] = []
  for (const source of value.split("\n")) {
    let line = source
    while (line.length > width) {
      result.push(line.slice(0, width))
      line = line.slice(width)
    }
    result.push(line)
  }
  return result
}

export class LineTranscriptRenderer {
  readonly #output: Writable
  readonly #tty: boolean
  readonly #startedAt = Date.now()
  #statusVisible = false

  constructor(output: Writable, tty = Boolean((output as Writable & { isTTY?: boolean }).isTTY)) {
    this.#output = output
    this.#tty = tty
  }

  get alternateScreenUsed(): false { return false }

  header(input: { cwd: string; taskId: string; runId?: string; revision: string }): void {
    this.#append(`session ${input.taskId}${input.runId ? ` / ${input.runId}` : ""} · revision ${input.revision}`)
    this.#append(`cwd ${input.cwd}`)
  }

  record(record: TranscriptRecord): void {
    this.#clearStatus()
    const refs = record.refs.length ? ` [${record.refs.join(", ")}]` : ""
    this.#append(`${String(record.sequence).padStart(6, "0")} ${record.kind.padEnd(8)} ${record.summary}${refs}`)
  }

  status(view: SessionViewSnapshot): void {
    const elapsed = Math.floor((Date.now() - this.#startedAt) / 1000)
    const degraded = view.evicted ? ` · recent-only(-${view.evicted})` : ""
    const unread = view.unread ? ` · unread ${view.unread}` : ""
    const terminal = " · terminal n/a(FE-S04)"
    const line = `[${view.connection}] ${view.action} · step ${view.steps} · ${elapsed}s · permissions ${view.pendingPermissions}${unread}${degraded}${terminal}`
    if (!this.#tty) return
    this.#output.write(`${CLEAR_LINE}${line.slice(0, widthOf(this.#output) - 1)}`)
    this.#statusVisible = true
  }

  finish(view: SessionViewSnapshot): void {
    this.#clearStatus()
    this.#append(`[${view.connection}] revision ${view.revision} · ${view.steps} transitions${view.evicted ? ` · ${view.evicted} outside recent search budget` : ""}`)
  }

  #append(value: string): void {
    for (const line of wrap(value, widthOf(this.#output))) this.#output.write(`${line}\n`)
  }

  #clearStatus(): void {
    if (!this.#statusVisible) return
    this.#output.write(`${CLEAR_LINE}`)
    this.#statusVisible = false
  }
}
