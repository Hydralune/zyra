import type { StreamingResponseHandle } from "../../../../../packages/core/typed-api-client/src/index.ts"
import type { TaskApi } from "../../api/task-api.ts"
import {
  TransportKind,
  type EventIngressDataSource,
  type IngressCapabilities,
  type NormalizedIngressFilter,
  type TransportOpenContext,
  type TransportSession,
} from "./contracts.ts"
import {
  EventIngressError,
  IngressErrorCode,
  classifyIngressError,
} from "./errors.ts"
import { abortableSleep } from "./reconnect.ts"

const MAX_SSE_LINE_BYTES = 256 * 1024
const MAX_SSE_EVENT_BYTES = 4 * 1024 * 1024
const MAX_WEBSOCKET_MESSAGE_BYTES = 4 * 1024 * 1024
const MAX_PENDING_MESSAGES = 4096
const encoder = new TextEncoder()

interface SseEvent {
  event: string
  data: string
  id?: string
  retry?: number
}

interface Deferred<T> {
  promise: Promise<T>
  resolve(value: T): void
  reject(reason: unknown): void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((accept, decline) => {
    resolve = accept
    reject = decline
  })
  return { promise, resolve, reject }
}

class AsyncMessageQueue<T> {
  readonly #capacity: number
  readonly #items: T[] = []
  readonly #waiters: Deferred<IteratorResult<T>>[] = []
  #closed = false
  #error: unknown

  constructor(capacity = MAX_PENDING_MESSAGES) {
    this.#capacity = Math.max(1, Math.floor(capacity))
  }

  get size(): number {
    return this.#items.length
  }

  get closed(): boolean {
    return this.#closed
  }

  push(value: T): void {
    if (this.#closed) return
    const waiter = this.#waiters.shift()
    if (waiter) {
      waiter.resolve({ done: false, value })
      return
    }
    if (this.#items.length >= this.#capacity) {
      this.fail(
        new EventIngressError(
          IngressErrorCode.BUFFER_OVERFLOW,
          "Transport message queue exceeded its bounded capacity.",
          { retryable: true, resyncRequired: true },
        ),
      )
      return
    }
    this.#items.push(value)
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    for (const waiter of this.#waiters.splice(0)) {
      waiter.resolve({ done: true, value: undefined })
    }
  }

  fail(error: unknown): void {
    if (this.#closed) return
    this.#closed = true
    this.#error = error
    for (const waiter of this.#waiters.splice(0)) waiter.reject(error)
  }

  async next(): Promise<IteratorResult<T>> {
    if (this.#items.length) {
      return { done: false, value: this.#items.shift()! }
    }
    if (this.#error !== undefined) throw this.#error
    if (this.#closed) return { done: true, value: undefined }
    const waiter = deferred<IteratorResult<T>>()
    this.#waiters.push(waiter)
    return waiter.promise
  }

  iterable(): AsyncIterable<T> {
    const queue = this
    return {
      [Symbol.asyncIterator]() {
        return {
          next: () => queue.next(),
        }
      },
    }
  }
}

export class SseFrameDecoder {
  readonly #decoder = new TextDecoder("utf-8", { fatal: true })
  #buffer = ""
  #eventName = ""
  #eventId: string | undefined
  #retry: number | undefined
  #data: string[] = []
  #eventBytes = 0
  #closed = false

  push(chunk: Uint8Array): SseEvent[] {
    if (this.#closed) {
      throw new EventIngressError(
        IngressErrorCode.TRANSPORT_PROTOCOL,
        "Cannot push bytes after the SSE decoder is closed.",
      )
    }
    let text: string
    try {
      text = this.#decoder.decode(chunk, { stream: true })
    } catch (error) {
      throw new EventIngressError(
        IngressErrorCode.TRANSPORT_PROTOCOL,
        "SSE stream contains invalid UTF-8.",
        { resyncRequired: true, cause: error },
      )
    }
    this.#buffer += text
    if (encoder.encode(this.#buffer).byteLength > MAX_SSE_EVENT_BYTES) {
      throw new EventIngressError(
        IngressErrorCode.TRANSPORT_PROTOCOL,
        "SSE parser buffer exceeds the bounded event limit.",
        { resyncRequired: true },
      )
    }
    return this.#consumeLines(false)
  }

  finish(): SseEvent[] {
    if (this.#closed) return []
    this.#closed = true
    let tail: string
    try {
      tail = this.#decoder.decode()
    } catch (error) {
      throw new EventIngressError(
        IngressErrorCode.TRANSPORT_PROTOCOL,
        "SSE stream ended with invalid UTF-8.",
        { resyncRequired: true, cause: error },
      )
    }
    this.#buffer += tail
    const events = this.#consumeLines(true)
    const final = this.#dispatch()
    if (final) events.push(final)
    return events
  }

  reset(): void {
    this.#buffer = ""
    this.#eventName = ""
    this.#eventId = undefined
    this.#retry = undefined
    this.#data = []
    this.#eventBytes = 0
    this.#closed = false
  }

  #consumeLines(final: boolean): SseEvent[] {
    const events: SseEvent[] = []
    while (true) {
      const newline = this.#buffer.indexOf("\n")
      if (newline < 0) {
        if (final && this.#buffer) {
          const line = this.#buffer.endsWith("\r")
            ? this.#buffer.slice(0, -1)
            : this.#buffer
          this.#buffer = ""
          const event = this.#line(line)
          if (event) events.push(event)
        }
        break
      }
      let line = this.#buffer.slice(0, newline)
      this.#buffer = this.#buffer.slice(newline + 1)
      if (line.endsWith("\r")) line = line.slice(0, -1)
      if (encoder.encode(line).byteLength > MAX_SSE_LINE_BYTES) {
        throw new EventIngressError(
          IngressErrorCode.TRANSPORT_PROTOCOL,
          "One SSE line exceeds the protocol limit.",
          { resyncRequired: true },
        )
      }
      const event = this.#line(line)
      if (event) events.push(event)
    }
    return events
  }

  #line(line: string): SseEvent | undefined {
    if (!line) return this.#dispatch()
    if (line.startsWith(":")) return undefined
    const separator = line.indexOf(":")
    const field = separator < 0 ? line : line.slice(0, separator)
    let value = separator < 0 ? "" : line.slice(separator + 1)
    if (value.startsWith(" ")) value = value.slice(1)
    if (field === "event") {
      this.#eventName = value
    } else if (field === "data") {
      this.#eventBytes += encoder.encode(value).byteLength
      if (this.#eventBytes > MAX_SSE_EVENT_BYTES) {
        throw new EventIngressError(
          IngressErrorCode.TRANSPORT_PROTOCOL,
          "One SSE event exceeds the protocol limit.",
          { resyncRequired: true },
        )
      }
      this.#data.push(value)
    } else if (field === "id") {
      if (!value.includes("\0")) this.#eventId = value
    } else if (field === "retry") {
      if (/^\d+$/.test(value)) this.#retry = Math.min(60_000, Number(value))
    }
    return undefined
  }

  #dispatch(): SseEvent | undefined {
    if (!this.#data.length) {
      this.#eventName = ""
      this.#retry = undefined
      this.#eventBytes = 0
      return undefined
    }
    const event: SseEvent = {
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

function parseSseData(event: SseEvent): unknown {
  try {
    return JSON.parse(event.data)
  } catch (error) {
    throw new EventIngressError(
      IngressErrorCode.TRANSPORT_PROTOCOL,
      `SSE ${event.event} frame contains malformed JSON.`,
      {
        resyncRequired: true,
        cause: error,
        context: {
          details: {
            event: event.event,
            id: event.id ?? null,
            dataBytes: encoder.encode(event.data).byteLength,
          },
        },
      },
    )
  }
}

class SseTransportSession implements TransportSession {
  readonly kind = TransportKind.SSE
  readonly openedAtMs: number
  readonly #handle: StreamingResponseHandle
  readonly #queue = new AsyncMessageQueue<unknown>()
  readonly #reader: ReadableStreamDefaultReader<Uint8Array>
  readonly #decoder = new SseFrameDecoder()
  readonly #pump: Promise<void>
  #closed = false

  constructor(handle: StreamingResponseHandle) {
    this.#handle = handle
    this.openedAtMs = handle.openedAt
    const reader = handle.response.body?.getReader()
    if (!reader) {
      handle.close("SSE response body was not readable.")
      throw new EventIngressError(
        IngressErrorCode.TRANSPORT_PROTOCOL,
        "SSE response body is not readable.",
        { retryable: true },
      )
    }
    this.#reader = reader
    this.#pump = this.#run()
  }

  get closed(): boolean {
    return this.#closed
  }

  frames(): AsyncIterable<unknown> {
    return this.#queue.iterable()
  }

  async close(reason?: unknown): Promise<void> {
    if (this.#closed) return
    this.#closed = true
    this.#queue.close()
    try {
      await this.#reader.cancel(reason)
    } catch {
      // Cancelling an already closed reader is harmless.
    }
    this.#handle.close(reason)
    try {
      await this.#pump
    } catch {
      // The queue already received the pump failure.
    }
  }

  async #run(): Promise<void> {
    try {
      while (!this.#closed) {
        const result = await this.#reader.read()
        if (result.done) break
        for (const event of this.#decoder.push(result.value)) {
          this.#queue.push(parseSseData(event))
        }
      }
      for (const event of this.#decoder.finish()) {
        this.#queue.push(parseSseData(event))
      }
      this.#queue.close()
    } catch (error) {
      if (!this.#closed) {
        this.#queue.fail(
          classifyIngressError(error, {
            transport: TransportKind.SSE,
          }),
        )
      }
    } finally {
      this.#closed = true
      this.#handle.close("SSE stream completed.")
    }
  }
}

interface WebSocketLike {
  binaryType: BinaryType
  readyState: number
  send(data: string | ArrayBufferLike | Blob | ArrayBufferView): void
  close(code?: number, reason?: string): void
  addEventListener(
    type: "open" | "message" | "error" | "close",
    listener: EventListenerOrEventListenerObject,
    options?: AddEventListenerOptions,
  ): void
  removeEventListener(
    type: "open" | "message" | "error" | "close",
    listener: EventListenerOrEventListenerObject,
  ): void
}

export type WebSocketFactory = (
  url: string,
  protocols?: string | string[],
) => WebSocketLike

function defaultWebSocketFactory(url: string, protocols?: string | string[]): WebSocketLike {
  if (typeof WebSocket !== "function") {
    throw new EventIngressError(
      IngressErrorCode.TRANSPORT_UNAVAILABLE,
      "This browser does not provide WebSocket.",
      { retryable: true },
    )
  }
  return new WebSocket(url, protocols) as WebSocketLike
}

async function websocketText(data: unknown): Promise<string> {
  if (typeof data === "string") return data
  if (data instanceof ArrayBuffer) {
    if (data.byteLength > MAX_WEBSOCKET_MESSAGE_BYTES) {
      throw new EventIngressError(
        IngressErrorCode.TRANSPORT_PROTOCOL,
        "WebSocket message exceeds the protocol limit.",
        { resyncRequired: true },
      )
    }
    return new TextDecoder("utf-8", { fatal: true }).decode(data)
  }
  if (ArrayBuffer.isView(data)) {
    if (data.byteLength > MAX_WEBSOCKET_MESSAGE_BYTES) {
      throw new EventIngressError(
        IngressErrorCode.TRANSPORT_PROTOCOL,
        "WebSocket message exceeds the protocol limit.",
        { resyncRequired: true },
      )
    }
    return new TextDecoder("utf-8", { fatal: true }).decode(data)
  }
  if (typeof Blob !== "undefined" && data instanceof Blob) {
    if (data.size > MAX_WEBSOCKET_MESSAGE_BYTES) {
      throw new EventIngressError(
        IngressErrorCode.TRANSPORT_PROTOCOL,
        "WebSocket message exceeds the protocol limit.",
        { resyncRequired: true },
      )
    }
    return data.text()
  }
  throw new EventIngressError(
    IngressErrorCode.TRANSPORT_PROTOCOL,
    "WebSocket delivered an unsupported message type.",
    { resyncRequired: true },
  )
}

class WebSocketTransportSession implements TransportSession {
  readonly kind = TransportKind.WEBSOCKET
  readonly openedAtMs: number
  readonly #socket: WebSocketLike
  readonly #queue = new AsyncMessageQueue<unknown>()
  readonly #opened = deferred<void>()
  #closed = false
  #openSettled = false

  constructor(socket: WebSocketLike, signal: AbortSignal) {
    this.#socket = socket
    this.openedAtMs = Date.now()
    socket.binaryType = "arraybuffer"
    socket.addEventListener("open", this.#onOpen)
    socket.addEventListener("message", this.#onMessage)
    socket.addEventListener("error", this.#onError)
    socket.addEventListener("close", this.#onClose)
    if (signal.aborted) this.close(signal.reason)
    else signal.addEventListener("abort", () => this.close(signal.reason), { once: true })
  }

  get closed(): boolean {
    return this.#closed
  }

  async ready(timeoutMs = 10_000): Promise<void> {
    await Promise.race([
      this.#opened.promise,
      new Promise<never>((_, reject) => {
        setTimeout(
          () =>
            reject(
              new EventIngressError(
                IngressErrorCode.TRANSPORT_TIMEOUT,
                "WebSocket did not open before its deadline.",
                { retryable: true },
              ),
            ),
          timeoutMs,
        )
      }),
    ])
  }

  frames(): AsyncIterable<unknown> {
    return this.#queue.iterable()
  }

  close(reason?: unknown): void {
    if (this.#closed) return
    this.#closed = true
    this.#queue.close()
    try {
      this.#socket.close(1000, String(reason ?? "Event ingress session closed.").slice(0, 123))
    } catch {
      // Socket may already be in a terminal state.
    }
    this.#removeListeners()
  }

  readonly #onOpen = () => {
    if (this.#openSettled) return
    this.#openSettled = true
    this.#opened.resolve()
  }

  readonly #onMessage = (raw: Event) => {
    const event = raw as MessageEvent
    void websocketText(event.data)
      .then((text) => {
        if (encoder.encode(text).byteLength > MAX_WEBSOCKET_MESSAGE_BYTES) {
          throw new EventIngressError(
            IngressErrorCode.TRANSPORT_PROTOCOL,
            "WebSocket message exceeds the protocol limit.",
            { resyncRequired: true },
          )
        }
        return JSON.parse(text)
      })
      .then((value) => this.#queue.push(value))
      .catch((error) => {
        this.#queue.fail(
          classifyIngressError(error, { transport: TransportKind.WEBSOCKET }),
        )
        this.close(error)
      })
  }

  readonly #onError = () => {
    const error = new EventIngressError(
      IngressErrorCode.TRANSPORT_DISCONNECTED,
      "WebSocket event ingress transport failed.",
      { retryable: true, context: { transport: TransportKind.WEBSOCKET } },
    )
    if (!this.#openSettled) {
      this.#openSettled = true
      this.#opened.reject(error)
    }
    this.#queue.fail(error)
  }

  readonly #onClose = (raw: Event) => {
    const event = raw as CloseEvent
    this.#closed = true
    if (!this.#openSettled) {
      this.#openSettled = true
      this.#opened.reject(
        new EventIngressError(
          IngressErrorCode.TRANSPORT_DISCONNECTED,
          `WebSocket closed before opening (${event.code}).`,
          { retryable: event.code !== 1008 },
        ),
      )
    }
    if (event.code === 1000) this.#queue.close()
    else {
      this.#queue.fail(
        new EventIngressError(
          IngressErrorCode.TRANSPORT_DISCONNECTED,
          `WebSocket closed with code ${event.code}: ${event.reason || "no reason"}.`,
          { retryable: event.code !== 1008 },
        ),
      )
    }
    this.#removeListeners()
  }

  #removeListeners(): void {
    this.#socket.removeEventListener("open", this.#onOpen)
    this.#socket.removeEventListener("message", this.#onMessage)
    this.#socket.removeEventListener("error", this.#onError)
    this.#socket.removeEventListener("close", this.#onClose)
  }
}

class LongPollTransportSession implements TransportSession {
  readonly kind = TransportKind.LONG_POLL
  readonly openedAtMs: number
  readonly #source: EventIngressDataSource
  readonly #context: TransportOpenContext
  readonly #controller = new AbortController()
  #closed = false
  #cursor: string

  constructor(source: EventIngressDataSource, context: TransportOpenContext) {
    this.#source = source
    this.#context = context
    this.#cursor = context.cursor
    this.openedAtMs = Date.now()
    if (context.signal.aborted) this.close(context.signal.reason)
    else {
      context.signal.addEventListener(
        "abort",
        () => this.close(context.signal.reason),
        { once: true },
      )
    }
  }

  get closed(): boolean {
    return this.#closed
  }

  frames(): AsyncIterable<unknown> {
    const session = this
    return {
      async *[Symbol.asyncIterator]() {
        while (!session.#closed && !session.#controller.signal.aborted) {
          const page = await session.#source.delta(session.#context.taskId, {
            cursor: session.#cursor,
            generation: session.#context.generation,
            limit: session.#context.pageSize,
            waitMs: session.#context.waitMs,
            filter: session.#context.filter,
            signal: session.#controller.signal,
          })
          const record =
            page && typeof page === "object" && !Array.isArray(page)
              ? (page as Record<string, unknown>)
              : {}
          const cursor = String(record.cursor || "").trim()
          if (!cursor) {
            throw new EventIngressError(
              IngressErrorCode.CURSOR_MISSING,
              "Long-poll delta page omitted its cursor.",
              { resyncRequired: true },
            )
          }
          session.#cursor = cursor
          yield page
          if (record.hasMore === true) continue
          if (session.#context.waitMs === 0) {
            await abortableSleep(25, session.#controller.signal)
          }
        }
      },
    }
  }

  close(reason?: unknown): void {
    if (this.#closed) return
    this.#closed = true
    this.#controller.abort(reason)
  }
}

function filterQuery(filter: NormalizedIngressFilter): {
  eventTypes?: readonly string[]
  intent?: string
  correlationId?: string
  artifactId?: string
} {
  return {
    eventTypes: filter.eventTypes,
    intent: filter.intents.length === 1 ? filter.intents[0] : undefined,
    correlationId:
      filter.correlationIds.length === 1 ? filter.correlationIds[0] : undefined,
    artifactId: filter.artifactIds.length === 1 ? filter.artifactIds[0] : undefined,
  }
}

export class TaskApiEventIngressSource implements EventIngressDataSource {
  readonly #api: TaskApi
  readonly #webSocketFactory: WebSocketFactory

  constructor(
    api: TaskApi,
    options: { webSocketFactory?: WebSocketFactory } = {},
  ) {
    this.#api = api
    this.#webSocketFactory = options.webSocketFactory ?? defaultWebSocketFactory
  }

  capabilities(
    taskId: string,
    filter: NormalizedIngressFilter,
    signal?: AbortSignal,
    generation?: number,
    cursor?: string,
  ): Promise<unknown> {
    return this.#api.eventIngressCapabilities(taskId, {
      ...filterQuery(filter),
      signal,
      generation,
      cursor,
    })
  }

  snapshot(
    taskId: string,
    options: {
      cursor?: string
      generation: number
      limit: number
      filter: NormalizedIngressFilter
      signal?: AbortSignal
    },
  ): Promise<unknown> {
    return this.#api.eventIngressSnapshot(taskId, {
      ...filterQuery(options.filter),
      cursor: options.cursor,
      generation: options.generation,
      limit: options.limit,
      signal: options.signal,
    })
  }

  delta(
    taskId: string,
    options: {
      cursor: string
      generation: number
      limit: number
      waitMs: number
      filter: NormalizedIngressFilter
      signal?: AbortSignal
    },
  ): Promise<unknown> {
    return this.#api.eventIngressDelta(taskId, {
      ...filterQuery(options.filter),
      cursor: options.cursor,
      generation: options.generation,
      limit: options.limit,
      waitMs: options.waitMs,
      signal: options.signal,
    })
  }

  async openSse(context: TransportOpenContext): Promise<TransportSession> {
    const handle = await this.#api.openEventIngressSse(context.taskId, {
      ...filterQuery(context.filter),
      cursor: context.cursor,
      generation: context.generation,
      limit: context.pageSize,
      waitMs: context.waitMs,
      streamMs: Math.max(1_000, context.heartbeatTimeoutMs * 2),
      heartbeatMs: Math.max(250, Math.floor(context.heartbeatTimeoutMs / 3)),
      timeoutMs: Math.max(30_000, context.heartbeatTimeoutMs * 3),
      signal: context.signal,
    })
    const contentType = handle.response.headers.get("content-type")?.toLowerCase() ?? ""
    if (!contentType.startsWith("text/event-stream")) {
      handle.close("Unexpected SSE content type.")
      throw new EventIngressError(
        IngressErrorCode.TRANSPORT_PROTOCOL,
        `SSE endpoint returned ${contentType || "[missing content type]"}.`,
        { retryable: true },
      )
    }
    return new SseTransportSession(handle)
  }

  async openWebSocket(context: TransportOpenContext): Promise<TransportSession> {
    const descriptor = context.capabilities.transports.find(
      (item) => item.kind === TransportKind.WEBSOCKET,
    )
    if (!descriptor?.available) {
      throw new EventIngressError(
        IngressErrorCode.TRANSPORT_UNAVAILABLE,
        descriptor?.reason || "Server did not advertise WebSocket event ingress.",
        { retryable: true, context: { transport: TransportKind.WEBSOCKET } },
      )
    }
    const query = {
      cursor: context.cursor,
      generation: context.generation,
      limit: context.pageSize,
      wait_ms: context.waitMs,
      event_types: context.filter.eventTypes.join(",") || undefined,
      intent: context.filter.intents.length === 1 ? context.filter.intents[0] : undefined,
      correlation_id:
        context.filter.correlationIds.length === 1
          ? context.filter.correlationIds[0]
          : undefined,
      artifact_id:
        context.filter.artifactIds.length === 1
          ? context.filter.artifactIds[0]
          : undefined,
    }
    const url = this.#api.eventIngressWebSocketUrl(
      context.taskId,
      descriptor.path,
      query,
    )
    const socket = this.#webSocketFactory(
      url,
      descriptor.protocols?.length ? [...descriptor.protocols] : undefined,
    )
    const session = new WebSocketTransportSession(socket, context.signal)
    await session.ready()
    socket.send(
      JSON.stringify({
        schema: "zyra.event-ingress-subscribe/v1",
        taskId: context.taskId,
        generation: context.generation,
        cursor: context.cursor,
        filter: JSON.parse(context.filter.digestMaterial),
      }),
    )
    return session
  }

  openLongPoll(context: TransportOpenContext): TransportSession {
    return new LongPollTransportSession(this, context)
  }
}

export function createLongPollSession(
  source: EventIngressDataSource,
  context: TransportOpenContext,
): TransportSession {
  return new LongPollTransportSession(source, context)
}
