const DEFAULT_MAX_LINE_BYTES = 256 * 1024
const DEFAULT_MAX_EVENT_BYTES = 4 * 1024 * 1024

export interface ServerSentEvent {
  event: string
  data: string
  id?: string
  retry?: number
}

export interface ServerSentEventDecoderOptions {
  maximumLineBytes?: number
  maximumEventBytes?: number
}

export class ServerSentEventDecoder {
  readonly #decoder = new TextDecoder("utf-8", { fatal: true })
  readonly #encoder = new TextEncoder()
  readonly #maximumLineBytes: number
  readonly #maximumEventBytes: number
  #buffer = ""
  #eventName = ""
  #eventId: string | undefined
  #retry: number | undefined
  #data: string[] = []
  #eventBytes = 0
  #closed = false

  constructor(options: ServerSentEventDecoderOptions = {}) {
    this.#maximumLineBytes = options.maximumLineBytes ?? DEFAULT_MAX_LINE_BYTES
    this.#maximumEventBytes = options.maximumEventBytes ?? DEFAULT_MAX_EVENT_BYTES
  }

  push(chunk: Uint8Array): ServerSentEvent[] {
    if (this.#closed) throw new TypeError("Cannot push bytes after the SSE decoder is closed.")
    this.#buffer += this.#decode(chunk, true)
    if (this.#encoder.encode(this.#buffer).byteLength > this.#maximumEventBytes) {
      throw new TypeError("SSE parser buffer exceeds the bounded event limit.")
    }
    return this.#consume(false)
  }

  finish(): ServerSentEvent[] {
    if (this.#closed) return []
    this.#closed = true
    this.#buffer += this.#decode(undefined, false)
    const events = this.#consume(true)
    const last = this.#dispatch()
    if (last) events.push(last)
    return events
  }

  #decode(chunk: Uint8Array | undefined, stream: boolean): string {
    try {
      return chunk ? this.#decoder.decode(chunk, { stream }) : this.#decoder.decode()
    } catch (error) {
      throw new TypeError("SSE stream contains invalid UTF-8.", { cause: error })
    }
  }

  #consume(final: boolean): ServerSentEvent[] {
    const events: ServerSentEvent[] = []
    while (true) {
      const newline = this.#buffer.indexOf("\n")
      if (newline < 0) {
        if (final && this.#buffer) {
          const line = this.#buffer.endsWith("\r") ? this.#buffer.slice(0, -1) : this.#buffer
          this.#buffer = ""
          const event = this.#line(line)
          if (event) events.push(event)
        }
        return events
      }
      let line = this.#buffer.slice(0, newline)
      this.#buffer = this.#buffer.slice(newline + 1)
      if (line.endsWith("\r")) line = line.slice(0, -1)
      if (this.#encoder.encode(line).byteLength > this.#maximumLineBytes) {
        throw new TypeError("One SSE line exceeds the protocol limit.")
      }
      const event = this.#line(line)
      if (event) events.push(event)
    }
  }

  #line(line: string): ServerSentEvent | undefined {
    if (!line) return this.#dispatch()
    if (line.startsWith(":")) return undefined
    const separator = line.indexOf(":")
    const field = separator < 0 ? line : line.slice(0, separator)
    let value = separator < 0 ? "" : line.slice(separator + 1)
    if (value.startsWith(" ")) value = value.slice(1)
    if (field === "event") this.#eventName = value
    else if (field === "data") {
      this.#eventBytes += this.#encoder.encode(value).byteLength
      if (this.#eventBytes > this.#maximumEventBytes) {
        throw new TypeError("One SSE event exceeds the protocol limit.")
      }
      this.#data.push(value)
    } else if (field === "id" && !value.includes("\0")) this.#eventId = value
    else if (field === "retry" && /^\d+$/.test(value)) this.#retry = Math.min(60_000, Number(value))
    return undefined
  }

  #dispatch(): ServerSentEvent | undefined {
    if (!this.#data.length) {
      this.#eventName = ""
      this.#retry = undefined
      this.#eventBytes = 0
      return undefined
    }
    const event = {
      event: this.#eventName || "message",
      data: this.#data.join("\n"),
      id: this.#eventId,
      retry: this.#retry,
    }
    this.#eventName = ""
    this.#retry = undefined
    this.#data = []
    this.#eventBytes = 0
    return event
  }
}

export async function* readServerSentEvents(response: Response): AsyncGenerator<ServerSentEvent> {
  const contentType = response.headers.get("content-type")?.toLowerCase() ?? ""
  if (!contentType.includes("text/event-stream")) {
    throw new TypeError("Streaming endpoint did not return text/event-stream.")
  }
  const reader = response.body?.getReader()
  if (!reader) throw new TypeError("SSE response body is not readable.")
  const decoder = new ServerSentEventDecoder()
  try {
    while (true) {
      const item = await reader.read()
      if (item.done) break
      for (const event of decoder.push(item.value)) yield event
    }
    for (const event of decoder.finish()) yield event
  } finally {
    reader.releaseLock()
  }
}
