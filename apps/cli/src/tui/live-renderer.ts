import type { Writable } from "node:stream"

type TerminalOutput = Writable & {
  isTTY?: boolean
  columns?: number
  rows?: number
  on?: (event: "resize", listener: () => void) => unknown
  once?: (event: "drain", listener: () => void) => unknown
  off?: (event: "resize", listener: () => void) => unknown
  removeListener?: (event: "drain", listener: () => void) => unknown
}

export interface LiveRendererDiagnostics {
  requested: number
  snapshots: number
  writes: number
  coalesced: number
  backpressureCount: number
}

export interface LiveRenderFrame {
  text: string
  cursor?: { row: number; column: number }
}

export class LiveProductRenderer {
  readonly #output: TerminalOutput
  readonly #tty: boolean
  readonly #renderSnapshot: () => string | LiveRenderFrame
  #renderedLines = 0
  #cursorRowFromTop: number | undefined
  #latest = ""
  #closed = false
  #dirty = false
  #scheduled = false
  #rendering = false
  #backpressured = false
  #requested = 0
  #snapshots = 0
  #writes = 0
  #coalesced = 0
  #backpressureCount = 0
  #resize = () => this.render()
  #drain = () => {
    this.#backpressured = false
    this.#flush()
  }

  constructor(output: Writable, renderSnapshot: () => string | LiveRenderFrame) {
    this.#output = output as TerminalOutput
    this.#tty = this.#output.isTTY === true
    this.#renderSnapshot = renderSnapshot
  }

  get width(): number { return Math.max(40, Math.floor(this.#output.columns ?? 120)) }
  get height(): number { return Math.max(8, Math.floor((this.#output.rows ?? 32) - 1)) }
  get alternateScreenUsed(): false { return false }
  get diagnostics(): LiveRendererDiagnostics {
    return Object.freeze({
      requested: this.#requested,
      snapshots: this.#snapshots,
      writes: this.#writes,
      coalesced: this.#coalesced,
      backpressureCount: this.#backpressureCount,
    })
  }

  start(): void {
    if (this.#closed) return
    this.#output.on?.("resize", this.#resize)
    this.renderNow()
  }

  render(): void {
    if (this.#closed) return
    this.#requested += 1
    if (this.#dirty) this.#coalesced += 1
    this.#dirty = true
    if (this.#backpressured || this.#rendering || this.#scheduled) return
    this.#scheduled = true
    queueMicrotask(() => {
      this.#scheduled = false
      this.#flush()
    })
  }

  renderNow(): void {
    if (this.#closed) return
    this.#requested += 1
    if (this.#dirty) this.#coalesced += 1
    this.#dirty = true
    this.#flush()
  }

  finish(): void {
    if (this.#closed) return
    this.#dirty = true
    this.#flush(true)
    this.close()
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    this.#output.off?.("resize", this.#resize)
    this.#output.removeListener?.("drain", this.#drain)
  }

  #flush(force = false): void {
    if (this.#closed || this.#rendering || (!force && this.#backpressured) || !this.#dirty) return
    this.#rendering = true
    this.#dirty = false
    try {
      const rendered = this.#renderSnapshot()
      const frame = typeof rendered === "string" ? { text: rendered } : rendered
      this.#latest = frame.text
      this.#snapshots += 1
      if (!this.#tty && !force) return
      const clear = this.#tty && this.#renderedLines > 0
        ? `\r${(this.#cursorRowFromTop ?? this.#renderedLines) > 0 ? `\u001b[${this.#cursorRowFromTop ?? this.#renderedLines}A` : ""}\u001b[J`
        : ""
      const renderedLines = Math.max(1, this.#latest.split("\n").length - 1)
      const cursor = frame.cursor && this.#tty
        ? `\u001b[${Math.max(0, renderedLines - frame.cursor.row)}A\r${frame.cursor.column > 0 ? `\u001b[${frame.cursor.column}C` : ""}`
        : ""
      const writable = this.#output.write(`${clear}${this.#latest}${cursor}`)
      this.#writes += 1
      this.#renderedLines = renderedLines
      this.#cursorRowFromTop = frame.cursor?.row
      if (!writable && !force) {
        this.#backpressured = true
        this.#backpressureCount += 1
        this.#output.once?.("drain", this.#drain)
      }
    } finally {
      this.#rendering = false
    }
    if (this.#dirty && !this.#backpressured && !this.#scheduled) {
      this.#scheduled = true
      queueMicrotask(() => {
        this.#scheduled = false
        this.#flush()
      })
    }
  }
}
