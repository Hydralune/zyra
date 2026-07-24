export interface TerminalWriteReceipt {
  sequence: number
  characters: number
  byteLength: number
  queuedAt: number
  startedAt: number
  finishedAt: number
}

interface WriteEntry {
  sequence: number
  value: string
  queuedAt: number
  resolve: (receipt: TerminalWriteReceipt) => void
  reject: (error: unknown) => void
}

export class TerminalOrderedWriter {
  readonly #write: (value: string) => void | Promise<void>
  readonly #maximumQueuedBytes: number
  readonly #encoder = new TextEncoder()
  #queue: WriteEntry[] = []
  #queuedBytes = 0
  #sequence = 0
  #running = false
  #closed = false
  #closeReason: unknown
  #drainWaiters: Array<() => void> = []

  constructor(
    write: (value: string) => void | Promise<void>,
    options: { maximumQueuedBytes?: number } = {},
  ) {
    this.#write = write
    this.#maximumQueuedBytes = Math.min(
      64 * 1024 * 1024,
      Math.max(1_024, Math.floor(options.maximumQueuedBytes ?? 8 * 1024 * 1024)),
    )
  }

  push(value: string): Promise<TerminalWriteReceipt> {
    if (this.#closed) return Promise.reject(this.#closeReason ?? new Error("Terminal writer is closed."))
    if (!value) {
      return Promise.resolve({
        sequence: this.#sequence,
        characters: 0,
        byteLength: 0,
        queuedAt: Date.now(),
        startedAt: Date.now(),
        finishedAt: Date.now(),
      })
    }
    const bytes = this.#encoder.encode(value).byteLength
    if (bytes > this.#maximumQueuedBytes || this.#queuedBytes + bytes > this.#maximumQueuedBytes) {
      return Promise.reject(new RangeError("Terminal renderer queue exceeded its byte budget."))
    }
    const sequence = ++this.#sequence
    const queuedAt = Date.now()
    const promise = new Promise<TerminalWriteReceipt>((resolve, reject) => {
      this.#queue.push({ sequence, value, queuedAt, resolve, reject })
    })
    this.#queuedBytes += bytes
    void this.#drain()
    return promise
  }

  flush(): Promise<void> {
    if (!this.#running && this.#queue.length === 0) return Promise.resolve()
    return new Promise((resolve) => this.#drainWaiters.push(resolve))
  }

  close(reason: unknown = new Error("Terminal writer closed.")): void {
    if (this.#closed) return
    this.#closed = true
    this.#closeReason = reason
    const pending = this.#queue.splice(0)
    this.#queuedBytes = 0
    for (const entry of pending) entry.reject(reason)
    this.#settleDrains()
  }

  snapshot(): Readonly<Record<string, number | boolean>> {
    return Object.freeze({
      sequence: this.#sequence,
      queuedEntries: this.#queue.length,
      queuedBytes: this.#queuedBytes,
      running: this.#running,
      closed: this.#closed,
    })
  }

  async #drain(): Promise<void> {
    if (this.#running || this.#closed) return
    this.#running = true
    try {
      while (!this.#closed) {
        const entry = this.#queue.shift()
        if (!entry) break
        const bytes = this.#encoder.encode(entry.value).byteLength
        this.#queuedBytes = Math.max(0, this.#queuedBytes - bytes)
        const startedAt = Date.now()
        try {
          await this.#write(entry.value)
          entry.resolve({
            sequence: entry.sequence,
            characters: entry.value.length,
            byteLength: bytes,
            queuedAt: entry.queuedAt,
            startedAt,
            finishedAt: Date.now(),
          })
        } catch (error) {
          entry.reject(error)
          this.close(error)
          break
        }
      }
    } finally {
      this.#running = false
      if (!this.#queue.length) this.#settleDrains()
      else if (!this.#closed) void this.#drain()
    }
  }

  #settleDrains(): void {
    const waiters = this.#drainWaiters.splice(0)
    for (const resolve of waiters) resolve()
  }
}
