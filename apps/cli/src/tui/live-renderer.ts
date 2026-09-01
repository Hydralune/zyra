import type { Writable } from "node:stream"

type TerminalOutput = Writable & {
  isTTY?: boolean
  columns?: number
  rows?: number
  on?: (event: "resize", listener: () => void) => unknown
  off?: (event: "resize", listener: () => void) => unknown
}

export class LiveProductRenderer {
  readonly #output: TerminalOutput
  readonly #tty: boolean
  readonly #renderSnapshot: () => string
  #renderedLines = 0
  #latest = ""
  #closed = false
  #resize = () => this.render()

  constructor(output: Writable, renderSnapshot: () => string) {
    this.#output = output as TerminalOutput
    this.#tty = this.#output.isTTY === true
    this.#renderSnapshot = renderSnapshot
  }

  get width(): number { return Math.max(40, Math.floor(this.#output.columns ?? 120)) }
  get height(): number { return Math.max(8, Math.floor((this.#output.rows ?? 32) - 1)) }
  get alternateScreenUsed(): false { return false }

  start(): void {
    if (this.#closed) return
    this.#output.on?.("resize", this.#resize)
    this.render()
  }

  render(): void {
    if (this.#closed) return
    this.#latest = this.#renderSnapshot()
    if (!this.#tty) return
    if (this.#renderedLines > 0) {
      this.#output.write(`\r\u001b[${this.#renderedLines}A\u001b[J`)
    }
    this.#output.write(this.#latest)
    this.#renderedLines = Math.max(1, this.#latest.split("\n").length - 1)
  }

  finish(): void {
    if (this.#closed) return
    if (this.#tty) this.render()
    else if (this.#latest) this.#output.write(this.#latest)
    this.close()
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    this.#output.off?.("resize", this.#resize)
  }
}
