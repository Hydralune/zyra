import type { TaskApi, TerminalCreateInput, TerminalKillInput } from "../../api/task-api.ts"
import {
  TERMINAL_PROTOCOL,
  TERMINAL_SOCKET_PROTOCOL,
  decodeTerminalFrameData,
  encodeTerminalClientCommand,
  parseTerminalServerFrame,
  parseTerminalSession,
  type TerminalBinding,
  type TerminalClientCommand,
  type TerminalErrorFrame,
  type TerminalServerFrame,
  type TerminalSessionProjection,
  type TerminalStatusProjection,
} from "./contracts.ts"
import { TerminalReplayWindow, type TerminalReplayState } from "./backpressure.ts"
import { TerminalScreen, type TerminalScreenSnapshot } from "./screen.ts"
import { TerminalOrderedWriter } from "./writer.ts"
import { TerminalInputBudget, normalizeTerminalKey, normalizeTerminalPaste, splitTerminalInput, type TerminalKeyboardEvent } from "./input.ts"
import { TerminalResizeCoordinator } from "./resize.ts"
import { TerminalReconnectCoordinator, type TerminalReconnectSnapshot } from "./reconnect.ts"
import { TerminalStructuredOutput, type TerminalStructuredLine } from "./structured.ts"
import { TerminalTabStore } from "./tabs.ts"
import { TerminalTranscript, type TerminalTranscriptSnapshot } from "./transcript.ts"
import { TerminalDiagnostics, type TerminalDiagnosticSnapshot } from "./diagnostics.ts"
import { TerminalProtocolAudit, type TerminalProtocolAuditSnapshot } from "./protocol-audit.ts"

export interface TerminalSocketEventMap {
  open: Event
  message: MessageEvent
  error: Event
  close: CloseEvent
}

export interface TerminalSocket {
  readonly readyState: number
  readonly OPEN: number
  binaryType: BinaryType
  send(data: string | ArrayBufferLike | Blob | ArrayBufferView): void
  close(code?: number, reason?: string): void
  addEventListener<K extends keyof TerminalSocketEventMap>(
    kind: K,
    listener: (event: TerminalSocketEventMap[K]) => void,
  ): void
  removeEventListener<K extends keyof TerminalSocketEventMap>(
    kind: K,
    listener: (event: TerminalSocketEventMap[K]) => void,
  ): void
}

export interface TerminalRuntimeOptions {
  taskApi: TaskApi
  tabs: TerminalTabStore
  socket?: (url: string, protocols: string | string[]) => TerminalSocket
  now?: () => number
  origin?: () => string
  reconnect?: boolean
}

export interface TerminalRuntimeSnapshot {
  terminal?: TerminalSessionProjection
  binding?: TerminalBinding
  status?: TerminalStatusProjection
  screen: TerminalScreenSnapshot
  replay: TerminalReplayState
  reconnect: TerminalReconnectSnapshot
  structured: readonly TerminalStructuredLine[]
  transcript: TerminalTranscriptSnapshot
  diagnostics: TerminalDiagnosticSnapshot
  protocolAudit: TerminalProtocolAuditSnapshot
  connectionId?: string
  connected: boolean
  inputSequence: number
  resizeSequence: number
  error?: { code: string; message: string; retryable: boolean }
  revision: number
}

interface RuntimeSession {
  projection?: TerminalSessionProjection
  screen: TerminalScreen
  replay: TerminalReplayWindow
  reconnect: TerminalReconnectCoordinator
  structured: TerminalStructuredOutput
  transcript: TerminalTranscript
  diagnostics: TerminalDiagnostics
  protocolAudit: TerminalProtocolAudit
  writer: TerminalOrderedWriter
  inputBudget: TerminalInputBudget
  resize: TerminalResizeCoordinator
  socket?: TerminalSocket
  connectionId?: string
  inputSequence: number
  resizeSequence: number
  revision: number
  error?: TerminalRuntimeSnapshot["error"]
  disposed: boolean
  connectPromise?: Promise<TerminalRuntimeSnapshot>
  reconnectTimer?: ReturnType<typeof setTimeout>
  listeners: Set<(snapshot: TerminalRuntimeSnapshot) => void>
  socketCleanup?: () => void
}

function defaultSocket(url: string, protocols: string | string[]): TerminalSocket {
  return new WebSocket(url, protocols) as unknown as TerminalSocket
}

function defaultOrigin(): string {
  if (typeof window !== "undefined" && window.location?.origin) return window.location.origin
  return "http://127.0.0.1:8000"
}

function terminalProjectionValue(value: unknown): unknown {
  if (!value || typeof value !== "object" || Array.isArray(value)) return value
  const input = value as Record<string, unknown>
  if (typeof input.error === "string") {
    throw new Error(typeof input.message === "string" ? input.message : input.error)
  }
  return input.terminal ?? value
}

export class TerminalRuntime {
  readonly #taskApi: TaskApi
  readonly #tabs: TerminalTabStore
  readonly #socketFactory: (url: string, protocols: string | string[]) => TerminalSocket
  readonly #now: () => number
  readonly #origin: () => string
  readonly #reconnectEnabled: boolean
  #sessions = new Map<string, RuntimeSession>()

  constructor(options: TerminalRuntimeOptions) {
    this.#taskApi = options.taskApi
    this.#tabs = options.tabs
    this.#socketFactory = options.socket ?? defaultSocket
    this.#now = options.now ?? Date.now
    this.#origin = options.origin ?? defaultOrigin
    this.#reconnectEnabled = options.reconnect !== false
  }

  async create(input: TerminalCreateInput): Promise<TerminalRuntimeSnapshot> {
    const response = await this.#taskApi.terminalCreate(input)
    const projection = parseTerminalSession(terminalProjectionValue(response))
    if (projection.permission.effect === "allow") {
      for (const [id, pending] of this.#sessions) {
        const previous = pending.projection
        if (id !== projection.binding.terminalId && previous?.status.phase === "permission_pending"
          && previous.binding.taskId === projection.binding.taskId
          && previous.binding.sessionId === projection.binding.sessionId
          && previous.binding.toolCallId === projection.binding.toolCallId) {
          this.closeTab(id)
          this.dispose(id)
        }
      }
    }
    const session = this.#ensure(projection.binding.terminalId, {
      binding: projection.binding,
      rows: projection.status.rows,
      cols: projection.status.cols,
      cursor: projection.status.earliestCursor,
    })
    session.projection = projection
    session.replay.restore({
      cursor: projection.status.earliestCursor,
      acknowledgedCursor: projection.status.earliestCursor,
      earliestCursor: projection.status.earliestCursor,
    })
    this.#tabs.open(projection.binding, {
      title: projection.status.title,
      phase: projection.status.phase,
    })
    this.#changed(session)
    if (projection.permission.effect === "allow" && projection.status.phase === "running") {
      await this.connect(projection.binding.terminalId)
    }
    return this.snapshot(projection.binding.terminalId)
  }

  async adopt(value: unknown, options: { connect?: boolean } = {}): Promise<TerminalRuntimeSnapshot> {
    const projection = parseTerminalSession(terminalProjectionValue(value))
    const session = this.#ensure(projection.binding.terminalId, {
      binding: projection.binding,
      rows: projection.status.rows,
      cols: projection.status.cols,
      cursor: projection.status.earliestCursor,
    })
    if (
      session.projection
      && session.projection.binding.taskId !== projection.binding.taskId
    ) throw new TypeError("Terminal identity cannot be adopted by another task.")
    session.projection = projection
    session.transcript.permission(projection.permission.effect, {
      decisionId: projection.permission.decisionId,
      reasonCode: projection.permission.reasonCode,
      requestId: projection.permission.requestId,
    })
    session.transcript.status(projection.status)
    session.screen.resize(projection.status.rows, projection.status.cols)
    this.#tabs.open(projection.binding, {
      title: projection.status.title,
      phase: projection.status.phase,
    })
    this.#changed(session)
    if (options.connect !== false && projection.status.phase === "running") {
      await this.connect(projection.binding.terminalId)
    }
    return this.snapshot(projection.binding.terminalId)
  }

  async refresh(taskId: string, terminalId: string): Promise<TerminalRuntimeSnapshot> {
    const value = await this.#taskApi.terminalGet(taskId, terminalId)
    return this.adopt(value, { connect: false })
  }

  async connect(terminalId: string): Promise<TerminalRuntimeSnapshot> {
    const session = this.#require(terminalId)
    if (session.disposed) throw new Error("Terminal runtime is disposed.")
    if (session.socket) return this.snapshot(terminalId)
    if (session.connectPromise) return session.connectPromise
    const pending = this.#connectAttempt(session, terminalId)
    session.connectPromise = pending
    try {
      return await pending
    } finally {
      if (session.connectPromise === pending) session.connectPromise = undefined
    }
  }

  async #connectAttempt(
    session: RuntimeSession,
    terminalId: string,
  ): Promise<TerminalRuntimeSnapshot> {
    const projection = this.#projection(session)
    const reconnect = session.reconnect.requestTicket(this.#now())
    this.#changed(session)
    try {
      const ticketValue = await this.#taskApi.terminalTicket({
        taskId: projection.binding.taskId,
        runId: projection.binding.runId,
        terminalId,
        sessionId: projection.binding.sessionId,
        cursor: session.replay.snapshot().acknowledgedCursor,
        origin: this.#origin(),
      })
      if (session.disposed) throw new Error("Terminal runtime was disposed during ticket issue.")
      const state = session.reconnect.snapshot()
      if (
        state.generation !== reconnect.generation
        || state.phase === "closed"
      ) {
        throw new Error("Terminal connection attempt was cancelled.")
      }
      const ticketProjection = parseTerminalSession(terminalProjectionValue(ticketValue))
      if (ticketProjection.binding.terminalId !== terminalId) {
        throw new TypeError("Terminal ticket returned another terminal binding.")
      }
      session.projection = ticketProjection
      session.reconnect.connecting(reconnect.generation)
      const url = this.#taskApi.terminalWebSocketUrl(
        projection.binding.taskId,
        terminalId,
        ticketProjection.socketPath,
        {
          ticket: ticketProjection.ticket ?? "",
          cursor: session.replay.snapshot().acknowledgedCursor,
          protocol: TERMINAL_PROTOCOL,
        },
      )
      this.#openSocket(session, url, reconnect.generation)
      this.#changed(session)
      return this.snapshot(terminalId)
    } catch (error) {
      const state = session.reconnect.snapshot()
      if (
        state.generation === reconnect.generation
        && state.phase !== "closed"
      ) {
        session.reconnect.fail(error)
        session.error = {
          code: "terminal_ticket_failed",
          message: error instanceof Error ? error.message : String(error),
          retryable: true,
        }
        this.#changed(session)
      }
      throw error
    }
  }

  input(
    terminalId: string,
    data: string,
    options: { actorId?: string; permissionPermitId?: string; paste?: boolean } = {},
  ): readonly number[] {
    const session = this.#require(terminalId)
    const projection = this.#projection(session)
    if (projection.status.phase !== "running") throw new Error("Terminal is not running.")
    session.error = undefined
    const reservation = session.inputBudget.reserve(data, { paste: options.paste, now: this.#now() })
    if (!reservation.accepted) {
      throw new RangeError(`Terminal input rejected: ${reservation.reason}; retry=${reservation.retryAfterMs}ms.`)
    }
    const sequences: number[] = []
    for (const chunk of splitTerminalInput(data)) {
      const sequence = ++session.inputSequence
      this.#sendCommand(session, {
        kind: "input",
        protocol: TERMINAL_PROTOCOL,
        terminalId,
        connectionId: this.#connection(session),
        sequence,
        data: chunk,
        actorId: options.actorId ?? "zyra-web-terminal",
        permissionPermitId: options.permissionPermitId,
      })
      session.transcript.input(chunk, sequence)
      session.diagnostics.input(chunk, sequence)
      sequences.push(sequence)
    }
    this.#changed(session)
    return Object.freeze(sequences)
  }

  key(
    terminalId: string,
    event: TerminalKeyboardEvent,
    options: { actorId?: string; permissionPermitId?: string } = {},
  ): number | undefined {
    const session = this.#require(terminalId)
    const screen = session.screen.snapshot()
    const value = normalizeTerminalKey(event, {
      applicationCursorKeys: screen.applicationCursorKeys,
      bracketedPaste: screen.bracketedPaste,
    })
    if (value === undefined) return undefined
    return this.input(terminalId, value, options)[0]
  }

  paste(
    terminalId: string,
    value: string,
    options: { actorId?: string; permissionPermitId?: string } = {},
  ): readonly number[] {
    const session = this.#require(terminalId)
    const screen = session.screen.snapshot()
    return this.input(
      terminalId,
      normalizeTerminalPaste(value, {
        applicationCursorKeys: screen.applicationCursorKeys,
        bracketedPaste: screen.bracketedPaste,
      }),
      { ...options, paste: true },
    )
  }

  resize(terminalId: string, rows: number, cols: number): void {
    const session = this.#require(terminalId)
    session.screen.resize(rows, cols)
    session.resize.schedule({ rows, cols })
    this.#tabs.update(terminalId, { rows, cols })
    session.transcript.resize(rows, cols, session.resizeSequence + 1)
    session.diagnostics.resize(rows, cols, session.resizeSequence + 1)
    this.#changed(session)
  }

  async kill(input: TerminalKillInput): Promise<TerminalRuntimeSnapshot> {
    const response = await this.#taskApi.terminalKill(input)
    const projection = parseTerminalSession(terminalProjectionValue(response))
    const session = this.#require(input.terminalId)
    session.projection = projection
    this.#tabs.update(input.terminalId, { phase: projection.status.phase })
    this.#changed(session)
    return this.snapshot(input.terminalId)
  }

  disconnect(terminalId: string, code = 1000, reason = "Viewer closed."): void {
    const session = this.#require(terminalId)
    if (session.reconnectTimer) clearTimeout(session.reconnectTimer)
    session.reconnectTimer = undefined
    session.socketCleanup?.()
    session.socketCleanup = undefined
    const socket = session.socket
    session.socket = undefined
    session.connectionId = undefined
    session.reconnect.close(code)
    if (socket && socket.readyState <= socket.OPEN) socket.close(code, reason.slice(0, 120))
    this.#changed(session)
  }

  closeTab(terminalId: string): void {
    const session = this.#sessions.get(terminalId)
    if (session) this.disconnect(terminalId)
    this.#tabs.close(terminalId)
  }

  dispose(terminalId?: string): void {
    const ids = terminalId ? [terminalId] : [...this.#sessions.keys()]
    for (const id of ids) {
      const session = this.#sessions.get(id)
      if (!session) continue
      session.disposed = true
      if (session.reconnectTimer) clearTimeout(session.reconnectTimer)
      session.socketCleanup?.()
      const socket = session.socket
      if (socket && socket.readyState <= socket.OPEN) {
        socket.close(1000, "Terminal runtime disposed.")
      }
      session.socket = undefined
      session.resize.close()
      session.writer.close()
      session.listeners.clear()
      this.#sessions.delete(id)
    }
  }

  listen(
    terminalId: string,
    listener: (snapshot: TerminalRuntimeSnapshot) => void,
  ): () => void {
    const session = this.#require(terminalId)
    session.listeners.add(listener)
    listener(this.snapshot(terminalId))
    return () => session.listeners.delete(listener)
  }

  snapshot(terminalId: string): TerminalRuntimeSnapshot {
    const session = this.#require(terminalId)
    return Object.freeze({
      terminal: session.projection,
      binding: session.projection?.binding,
      status: session.projection?.status,
      screen: session.screen.snapshot(),
      replay: session.replay.snapshot(),
      reconnect: session.reconnect.snapshot(),
      structured: session.structured.snapshot().lines,
      transcript: session.transcript.snapshot(),
      diagnostics: session.diagnostics.snapshot(),
      protocolAudit: session.protocolAudit.snapshot(),
      connectionId: session.connectionId,
      connected: Boolean(session.socket && session.connectionId),
      inputSequence: session.inputSequence,
      resizeSequence: session.resizeSequence,
      error: session.error ? Object.freeze({ ...session.error }) : undefined,
      revision: session.revision,
    })
  }

  list(): readonly TerminalRuntimeSnapshot[] {
    return Object.freeze([...this.#sessions.keys()].map((id) => this.snapshot(id)))
  }

  #ensure(
    terminalId: string,
    options: { binding: TerminalBinding; rows: number; cols: number; cursor: number },
  ): RuntimeSession {
    const existing = this.#sessions.get(terminalId)
    if (existing) return existing
    const screen = new TerminalScreen({ rows: options.rows, cols: options.cols })
    const session = {
      screen,
      replay: new TerminalReplayWindow({ cursor: options.cursor, earliestCursor: options.cursor }),
      reconnect: new TerminalReconnectCoordinator({ cursor: options.cursor }),
      structured: new TerminalStructuredOutput(),
      transcript: new TerminalTranscript(options.binding),
      diagnostics: new TerminalDiagnostics(options.binding, { now: this.#now }),
      protocolAudit: new TerminalProtocolAudit(options.binding, { now: this.#now }),
      writer: undefined as unknown as TerminalOrderedWriter,
      inputBudget: new TerminalInputBudget(),
      resize: undefined as unknown as TerminalResizeCoordinator,
      inputSequence: 0,
      resizeSequence: 0,
      revision: 0,
      disposed: false,
      listeners: new Set<(snapshot: TerminalRuntimeSnapshot) => void>(),
    } satisfies RuntimeSession
    session.writer = new TerminalOrderedWriter(async (value) => {
      session.screen.write(value)
      session.structured.push(value)
    })
    session.resize = new TerminalResizeCoordinator(async (value) => {
      session.resizeSequence = value.sequence
      this.#sendCommand(session, {
        kind: "resize",
        protocol: TERMINAL_PROTOCOL,
        terminalId,
        connectionId: this.#connection(session),
        sequence: value.sequence,
        rows: value.rows,
        cols: value.cols,
      })
      this.#changed(session)
    })
    this.#sessions.set(terminalId, session)
    return session
  }

  #openSocket(session: RuntimeSession, url: string, generation: number): void {
    const previous = session.socket
    session.socketCleanup?.()
    if (previous && previous.readyState <= previous.OPEN) {
      previous.close(1000, "Superseded terminal connection.")
    }
    const socket = this.#socketFactory(url, TERMINAL_SOCKET_PROTOCOL)
    socket.binaryType = "arraybuffer"
    session.socket = socket
    const current = () => (
      !session.disposed
      && session.socket === socket
      && session.reconnect.snapshot().generation === generation
    )
    const open = () => {
      if (!current()) return
      session.reconnect.opened(generation, session.replay.snapshot().acknowledgedCursor, this.#now())
      const state = session.reconnect.snapshot()
      session.diagnostics.reconnect(
        state.generation,
        state.attempt,
        state.cursor,
      )
      session.transcript.reconnect("open", {
        generation: state.generation,
        attempt: state.attempt,
        cursor: state.cursor,
      })
      this.#changed(session)
    }
    const message = (event: MessageEvent) => {
      if (!current()) return
      void this.#message(session, event).catch((error) => {
        if (!current()) return
        session.error = {
          code: "terminal_frame_rejected",
          message: error instanceof Error ? error.message : String(error),
          retryable: false,
        }
        this.#changed(session)
        socket.close(1008, "Terminal frame rejected.")
      })
    }
    const error = () => {
      if (!current()) return
      session.error = {
        code: "terminal_socket_error",
        message: "Terminal WebSocket reported an error.",
        retryable: true,
      }
      session.transcript.error(
        session.error.code,
        session.error.message,
        session.error.retryable,
      )
      session.diagnostics.transportError(
        session.error.code,
        session.error.message,
        session.error.retryable,
      )
      this.#changed(session)
    }
    const close = (event: CloseEvent) => {
      if (!current()) return
      session.socket = undefined
      session.connectionId = undefined
      session.socketCleanup?.()
      session.socketCleanup = undefined
      const ended = ["exited", "killed", "timed_out", "crashed"].includes(session.projection?.status.phase ?? "")
      const state = ended ? session.reconnect.close(event.code) : session.reconnect.disconnected({
        code: event.code,
        reason: event.reason,
        retryable: event.code !== 1000 && event.code !== 1008 && event.code !== 4001,
        now: this.#now(),
      })
      session.transcript.reconnect(state.phase, {
        generation: state.generation,
        attempt: state.attempt,
        cursor: state.cursor,
        delayMs: state.retryAt ? Math.max(0, state.retryAt - this.#now()) : undefined,
      })
      session.diagnostics.disconnected(
        event.code,
        event.reason,
        event.code !== 1000 && event.code !== 1008 && event.code !== 4001,
      )
      this.#changed(session)
      if (this.#reconnectEnabled && state.phase === "backoff" && state.retryAt) {
        const delay = Math.max(0, state.retryAt - this.#now())
        session.reconnectTimer = setTimeout(() => {
          session.reconnectTimer = undefined
          session.reconnect.retry(this.#now())
          void this.connect(this.#projection(session).binding.terminalId).catch((failure) => {
            session.reconnect.fail(failure)
            this.#changed(session)
          })
        }, delay)
      }
    }
    socket.addEventListener("open", open)
    socket.addEventListener("message", message)
    socket.addEventListener("error", error)
    socket.addEventListener("close", close)
    session.socketCleanup = () => {
      socket.removeEventListener("open", open)
      socket.removeEventListener("message", message)
      socket.removeEventListener("error", error)
      socket.removeEventListener("close", close)
    }
  }

  async #message(session: RuntimeSession, event: MessageEvent): Promise<void> {
    const value = await decodeTerminalFrameData(event.data)
    const frame = parseTerminalServerFrame(value, this.#projection(session).binding)
    if (session.protocolAudit.server(frame) === "rejected") {
      throw new Error(`Terminal protocol audit rejected server ${frame.kind} frame.`)
    }
    if (frame.kind === "hello") {
      session.connectionId = frame.connectionId
      session.replay.restore({
        cursor: frame.acceptedCursor,
        acknowledgedCursor: frame.acceptedCursor,
        earliestCursor: frame.earliestCursor,
      })
      session.projection = {
        ...this.#projection(session),
        status: frame.status,
      }
      session.transcript.status(frame.status)
      session.diagnostics.connected(session.reconnect.snapshot().generation, {
        acceptedCursor: frame.acceptedCursor,
        earliestCursor: frame.earliestCursor,
        maximumUnackedBytes: frame.maximumUnackedBytes,
      })
      session.diagnostics.status(frame.status)
      session.screen.resize(frame.status.rows, frame.status.cols)
      session.error = undefined
      this.#changed(session)
      return
    }
    if (frame.kind === "output") {
      session.diagnostics.output(frame)
      const admission = session.replay.admit(frame)
      if (admission.needsResync) {
        session.error = {
          code: admission.reason ?? "terminal_resync_required",
          message: "Terminal output has a cursor or sequence gap.",
          retryable: true,
        }
        session.diagnostics.protocolError(
          session.error.code,
          session.error.message,
          session.error.retryable,
        )
        this.#changed(session)
        return
      }
      if (!admission.accepted || !admission.frame) return
      await session.writer.push(admission.frame.text)
      session.transcript.output(admission.frame.text, {
        firstCursor: admission.frame.firstCursor,
        nextCursor: admission.frame.nextCursor,
        redacted: admission.frame.redacted,
        sequence: admission.frame.sequence,
      })
      session.replay.acknowledge(admission.frame.nextCursor)
      session.diagnostics.acknowledge(admission.frame.nextCursor)
      session.reconnect.advanceCursor(admission.frame.nextCursor)
      this.#tabs.update(admission.frame.binding.terminalId, {
        acknowledgedCursor: admission.frame.nextCursor,
      })
      this.#sendCommand(session, {
        kind: "ack",
        protocol: TERMINAL_PROTOCOL,
        terminalId: admission.frame.binding.terminalId,
        connectionId: this.#connection(session),
        sequence: admission.frame.sequence,
        cursor: admission.frame.nextCursor,
      })
      this.#changed(session)
      return
    }
    if (frame.kind === "cursor") {
      session.reconnect.advanceCursor(frame.cursor)
      this.#changed(session)
      return
    }
    if (frame.kind === "resync") {
      session.replay.resync(frame)
      session.diagnostics.resync(frame)
      session.reconnect.advanceCursor(frame.acceptedCursor)
      this.#changed(session)
      return
    }
    if (frame.kind === "backpressure") {
      session.replay.applyBackpressure(frame)
      session.transcript.backpressure(
        frame.unackedBytes,
        frame.maximumUnackedBytes,
        frame.paused,
      )
      session.diagnostics.backpressure(frame)
      this.#changed(session)
      return
    }
    if (frame.kind === "spill") {
      session.replay.addSpill(frame.spill)
      session.transcript.spill(frame.spill)
      session.diagnostics.spill(frame.spill)
      this.#changed(session)
      return
    }
    if (frame.kind === "status") {
      session.projection = { ...this.#projection(session), status: frame.status }
      session.transcript.status(frame.status)
      session.diagnostics.status(frame.status)
      this.#tabs.update(frame.binding.terminalId, {
        phase: frame.status.phase,
        title: frame.status.title,
      })
      if (frame.status.phase !== "running") session.structured.finish()
      this.#changed(session)
      return
    }
    if (frame.kind === "error") {
      this.#applyError(session, frame)
      return
    }
    if (frame.kind === "pong") {
      this.#changed(session)
    }
  }

  #applyError(session: RuntimeSession, frame: TerminalErrorFrame): void {
    session.error = { code: frame.code, message: frame.message, retryable: frame.retryable }
    session.transcript.error(frame.code, frame.message, frame.retryable)
    session.diagnostics.protocolError(frame.code, frame.message, frame.retryable)
    this.#changed(session)
    const socket = session.socket
    if (!frame.retryable && socket && socket.readyState === socket.OPEN) {
      socket.close(frame.closeCode ?? 1008, frame.code.slice(0, 120))
    }
  }

  #sendCommand(session: RuntimeSession, command: TerminalClientCommand): void {
    const outcome = session.protocolAudit.client(command)
    if (outcome === "rejected") {
      throw new Error(`Terminal protocol audit rejected client ${command.kind} command.`)
    }
    if (outcome === "duplicate") return
    const socket = session.socket
    if (!socket || socket.readyState !== socket.OPEN) throw new Error("Terminal socket is not open.")
    socket.send(encodeTerminalClientCommand(command))
  }

  #connection(session: RuntimeSession): string {
    if (!session.connectionId) throw new Error("Terminal connection handshake is incomplete.")
    return session.connectionId
  }

  #projection(session: RuntimeSession): TerminalSessionProjection {
    if (!session.projection) throw new Error("Terminal projection is not loaded.")
    return session.projection
  }

  #require(terminalId: string): RuntimeSession {
    const session = this.#sessions.get(terminalId)
    if (!session) throw new Error(`Terminal ${terminalId} is not loaded.`)
    return session
  }

  #changed(session: RuntimeSession): void {
    session.revision += 1
    const terminalId = session.projection?.binding.terminalId
    if (!terminalId) return
    const snapshot = this.snapshot(terminalId)
    for (const listener of session.listeners) listener(snapshot)
  }
}
