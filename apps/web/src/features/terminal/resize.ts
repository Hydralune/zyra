export interface TerminalSize {
  rows: number
  cols: number
}

export interface TerminalResizeReceipt extends TerminalSize {
  sequence: number
  scheduledAt: number
  sentAt: number
}

function size(value: TerminalSize): TerminalSize {
  if (!Number.isFinite(value.rows) || !Number.isFinite(value.cols)) {
    throw new TypeError("Terminal dimensions must be finite.")
  }
  return {
    rows: Math.min(500, Math.max(2, Math.floor(value.rows))),
    cols: Math.min(1_000, Math.max(2, Math.floor(value.cols))),
  }
}

export class TerminalResizeCoordinator {
  readonly #send: (value: TerminalResizeReceipt) => void | Promise<void>
  readonly #delayMs: number
  #last?: TerminalSize
  #pending?: TerminalSize & { scheduledAt: number }
  #timer?: ReturnType<typeof setTimeout>
  #sequence = 0
  #closed = false

  constructor(
    send: (value: TerminalResizeReceipt) => void | Promise<void>,
    options: { delayMs?: number } = {},
  ) {
    this.#send = send
    this.#delayMs = Math.min(1_000, Math.max(0, Math.floor(options.delayMs ?? 80)))
  }

  schedule(value: TerminalSize): void {
    if (this.#closed) return
    const selected = size(value)
    if (
      (this.#pending && this.#pending.rows === selected.rows && this.#pending.cols === selected.cols)
      || (!this.#pending && this.#last?.rows === selected.rows && this.#last.cols === selected.cols)
    ) return
    this.#pending = { ...selected, scheduledAt: Date.now() }
    if (this.#timer) return
    if (!this.#last || this.#delayMs === 0) {
      void this.flush()
      return
    }
    this.#timer = setTimeout(() => {
      this.#timer = undefined
      void this.flush()
    }, this.#delayMs)
  }

  async flush(): Promise<TerminalResizeReceipt | undefined> {
    if (this.#timer) {
      clearTimeout(this.#timer)
      this.#timer = undefined
    }
    if (this.#closed) return undefined
    const pending = this.#pending
    this.#pending = undefined
    if (!pending) return undefined
    if (this.#last?.rows === pending.rows && this.#last.cols === pending.cols) return undefined
    const receipt = {
      rows: pending.rows,
      cols: pending.cols,
      sequence: ++this.#sequence,
      scheduledAt: pending.scheduledAt,
      sentAt: Date.now(),
    }
    await this.#send(receipt)
    this.#last = { rows: receipt.rows, cols: receipt.cols }
    if (this.#pending) this.schedule(this.#pending)
    return receipt
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    if (this.#timer) clearTimeout(this.#timer)
    this.#timer = undefined
    this.#pending = undefined
  }

  snapshot(): Readonly<Record<string, unknown>> {
    return Object.freeze({
      sequence: this.#sequence,
      last: this.#last ? Object.freeze({ ...this.#last }) : undefined,
      pending: this.#pending ? Object.freeze({ ...this.#pending }) : undefined,
      scheduled: Boolean(this.#timer),
      closed: this.#closed,
    })
  }
}
