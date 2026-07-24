import type {
  TerminalBackpressureFrame,
  TerminalBinding,
  TerminalOutputFrame,
  TerminalPhase,
  TerminalResyncFrame,
  TerminalSpillProjection,
  TerminalStatusProjection,
} from "./contracts.ts"

export type TerminalDiagnosticSeverity = "info" | "warning" | "error"

export interface TerminalDiagnosticRecord {
  id: string
  code: string
  severity: TerminalDiagnosticSeverity
  message: string
  createdAt: string
  retryable: boolean
  details: Readonly<Record<string, string | number | boolean>>
}

export interface TerminalDiagnosticSample {
  sampledAt: string
  cursor: number
  acknowledgedCursor: number
  unackedBytes: number
  outputBytes: number
  outputFrames: number
  inputFrames: number
  reconnects: number
  backpressureCount: number
}

export interface TerminalDiagnosticSnapshot {
  terminalId: string
  phase?: TerminalPhase
  connected: boolean
  connectionGeneration: number
  outputFrames: number
  outputBytes: number
  inputFrames: number
  inputBytes: number
  resizeFrames: number
  acknowledgedCursor: number
  observedCursor: number
  earliestCursor: number
  unackedBytes: number
  maximumUnackedBytes: number
  reconnects: number
  resyncs: number
  backpressureCount: number
  spills: number
  spilledBytes: number
  redactedFrames: number
  binaryBytes: number
  protocolErrors: number
  transportErrors: number
  lastOutputAt?: string
  lastInputAt?: string
  lastConnectedAt?: string
  lastDisconnectedAt?: string
  records: readonly TerminalDiagnosticRecord[]
  samples: readonly TerminalDiagnosticSample[]
  health: "healthy" | "degraded" | "failed" | "closed"
  revision: number
}

export interface TerminalDiagnosticsOptions {
  maximumRecords?: number
  maximumSamples?: number
  sampleIntervalMs?: number
  now?: () => number
}

function frozenDetails(
  value: Readonly<Record<string, string | number | boolean>> = {},
): Readonly<Record<string, string | number | boolean>> {
  return Object.freeze({ ...value })
}

function timestamp(now: () => number): string {
  return new Date(now()).toISOString()
}

function bytes(value: string): number {
  return new TextEncoder().encode(value).byteLength
}

export class TerminalDiagnostics {
  readonly #binding: TerminalBinding
  readonly #maximumRecords: number
  readonly #maximumSamples: number
  readonly #sampleIntervalMs: number
  readonly #now: () => number
  #phase?: TerminalPhase
  #connected = false
  #generation = 0
  #outputFrames = 0
  #outputBytes = 0
  #inputFrames = 0
  #inputBytes = 0
  #resizeFrames = 0
  #acknowledgedCursor = 0
  #observedCursor = 0
  #earliestCursor = 0
  #maximumUnackedBytes = 4 * 1_024 * 1_024
  #reconnects = 0
  #resyncs = 0
  #backpressureCount = 0
  #spills = 0
  #spilledBytes = 0
  #redactedFrames = 0
  #binaryBytes = 0
  #protocolErrors = 0
  #transportErrors = 0
  #lastOutputAt?: string
  #lastInputAt?: string
  #lastConnectedAt?: string
  #lastDisconnectedAt?: string
  #records: TerminalDiagnosticRecord[] = []
  #samples: TerminalDiagnosticSample[] = []
  #lastSampleAt = 0
  #recordSequence = 0
  #revision = 0

  constructor(binding: TerminalBinding, options: TerminalDiagnosticsOptions = {}) {
    this.#binding = binding
    this.#maximumRecords = Math.min(
      10_000,
      Math.max(10, Math.floor(options.maximumRecords ?? 500)),
    )
    this.#maximumSamples = Math.min(
      100_000,
      Math.max(10, Math.floor(options.maximumSamples ?? 2_000)),
    )
    this.#sampleIntervalMs = Math.min(
      60_000,
      Math.max(100, Math.floor(options.sampleIntervalMs ?? 1_000)),
    )
    this.#now = options.now ?? Date.now
  }

  connected(
    generation: number,
    options: {
      acceptedCursor: number
      earliestCursor: number
      maximumUnackedBytes: number
    },
  ): TerminalDiagnosticSnapshot {
    if (this.#connected && generation !== this.#generation) {
      this.#record(
        "terminal.connection_generation_replaced",
        "warning",
        "A live terminal connection was replaced by another generation.",
        true,
        { previousGeneration: this.#generation, generation },
      )
    }
    if (generation < this.#generation) {
      this.#record(
        "terminal.connection_generation_stale",
        "error",
        "A stale terminal connection generation attempted to become active.",
        false,
        { previousGeneration: this.#generation, generation },
      )
      return this.snapshot()
    }
    this.#connected = true
    this.#generation = generation
    this.#acknowledgedCursor = options.acceptedCursor
    this.#observedCursor = Math.max(this.#observedCursor, options.acceptedCursor)
    this.#earliestCursor = options.earliestCursor
    this.#maximumUnackedBytes = options.maximumUnackedBytes
    this.#lastConnectedAt = timestamp(this.#now)
    this.#revision += 1
    this.#sample(true)
    return this.snapshot()
  }

  disconnected(
    code: number,
    reason: string,
    retryable: boolean,
  ): TerminalDiagnosticSnapshot {
    this.#connected = false
    this.#lastDisconnectedAt = timestamp(this.#now)
    if (code !== 1000) {
      this.#transportErrors += 1
      this.#record(
        "terminal.transport_disconnected",
        retryable ? "warning" : "error",
        reason || `Terminal transport closed with code ${code}.`,
        retryable,
        { code, generation: this.#generation },
      )
    }
    this.#revision += 1
    this.#sample(true)
    return this.snapshot()
  }

  reconnect(generation: number, attempt: number, cursor: number): void {
    this.#reconnects += 1
    this.#generation = Math.max(this.#generation, generation)
    this.#record(
      "terminal.transport_reconnect",
      attempt > 3 ? "warning" : "info",
      `Terminal reconnect attempt ${attempt} started.`,
      true,
      { generation, attempt, cursor },
    )
    this.#sample()
  }

  output(frame: TerminalOutputFrame): void {
    if (frame.binding.terminalId !== this.#binding.terminalId) {
      this.protocolError(
        "terminal.output_binding_mismatch",
        "Output frame belongs to another terminal.",
        false,
      )
      return
    }
    if (frame.firstCursor !== this.#observedCursor) {
      this.#record(
        "terminal.output_cursor_gap",
        "warning",
        "Terminal output cursor is not contiguous with the observed stream.",
        true,
        {
          expectedCursor: this.#observedCursor,
          firstCursor: frame.firstCursor,
          nextCursor: frame.nextCursor,
          sequence: frame.sequence,
        },
      )
    }
    this.#outputFrames += 1
    this.#outputBytes += frame.byteLength
    this.#observedCursor = Math.max(this.#observedCursor, frame.nextCursor)
    if (frame.redacted) this.#redactedFrames += 1
    this.#lastOutputAt = timestamp(this.#now)
    this.#revision += 1
    this.#sample()
  }

  acknowledge(cursor: number): void {
    if (cursor < this.#acknowledgedCursor || cursor > this.#observedCursor) {
      this.#record(
        "terminal.ack_cursor_invalid",
        "error",
        "Terminal acknowledgement is outside the admitted cursor window.",
        false,
        {
          acknowledgedCursor: this.#acknowledgedCursor,
          observedCursor: this.#observedCursor,
          cursor,
        },
      )
      return
    }
    this.#acknowledgedCursor = cursor
    this.#revision += 1
    this.#sample()
  }

  input(value: string, sequence: number): void {
    this.#inputFrames += 1
    this.#inputBytes += bytes(value)
    this.#lastInputAt = timestamp(this.#now)
    this.#revision += 1
    if (!this.#connected) {
      this.#record(
        "terminal.input_while_disconnected",
        "error",
        "Terminal input was attempted without a live socket.",
        true,
        { sequence, byteLength: bytes(value) },
      )
    }
    this.#sample()
  }

  resize(rows: number, cols: number, sequence: number): void {
    this.#resizeFrames += 1
    this.#revision += 1
    if (rows < 2 || cols < 2) {
      this.#record(
        "terminal.resize_invalid",
        "error",
        "Terminal resize is below the supported viewport dimensions.",
        false,
        { rows, cols, sequence },
      )
    }
    this.#sample()
  }

  status(status: TerminalStatusProjection): void {
    if (status.earliestCursor > status.cursor) {
      this.protocolError(
        "terminal.status_cursor_invalid",
        "Terminal status earliest cursor exceeds its current cursor.",
        false,
      )
      return
    }
    this.#phase = status.phase
    this.#observedCursor = Math.max(this.#observedCursor, status.cursor)
    this.#earliestCursor = status.earliestCursor
    this.#spilledBytes = Math.max(this.#spilledBytes, status.spilledBytes)
    this.#binaryBytes = Math.max(this.#binaryBytes, status.binaryBytes)
    this.#revision += 1
    this.#sample()
  }

  resync(frame: TerminalResyncFrame): void {
    this.#resyncs += 1
    this.#earliestCursor = frame.earliestCursor
    this.#acknowledgedCursor = frame.acceptedCursor
    this.#observedCursor = Math.max(this.#observedCursor, frame.acceptedCursor)
    this.#record(
      `terminal.resync_${frame.reason}`,
      "warning",
      `Terminal replay resynchronized because of ${frame.reason}.`,
      true,
      {
        requestedCursor: frame.requestedCursor,
        acceptedCursor: frame.acceptedCursor,
        earliestCursor: frame.earliestCursor,
        spills: frame.spills.length,
      },
    )
    this.#sample(true)
  }

  backpressure(frame: TerminalBackpressureFrame): void {
    this.#backpressureCount += 1
    this.#maximumUnackedBytes = frame.maximumUnackedBytes
    this.#record(
      frame.paused
        ? "terminal.backpressure_paused"
        : "terminal.backpressure_resumed",
      frame.paused ? "warning" : "info",
      frame.paused
        ? "Terminal server paused output until the browser acknowledges data."
        : "Terminal output resumed after acknowledgement.",
      true,
      {
        unackedBytes: frame.unackedBytes,
        maximumUnackedBytes: frame.maximumUnackedBytes,
        requiredCursor: frame.requiredCursor,
      },
    )
    this.#sample(true)
  }

  spill(spill: TerminalSpillProjection): void {
    this.#spills += 1
    this.#spilledBytes += spill.byteLength
    if (spill.binary) this.#binaryBytes += spill.byteLength
    this.#record(
      "terminal.output_spilled",
      spill.binary ? "warning" : "info",
      spill.binary
        ? "Binary terminal output was moved to an immutable artifact."
        : "Terminal output exceeded memory bounds and was moved to an artifact.",
      false,
      {
        artifactId: spill.artifactId,
        byteLength: spill.byteLength,
        firstCursor: spill.firstCursor,
        nextCursor: spill.nextCursor,
        binary: spill.binary,
        redacted: spill.redacted,
      },
    )
    this.#sample(true)
  }

  protocolError(code: string, message: string, retryable: boolean): void {
    this.#protocolErrors += 1
    this.#record(code, "error", message, retryable)
    this.#sample(true)
  }

  transportError(code: string, message: string, retryable: boolean): void {
    this.#transportErrors += 1
    this.#record(code, retryable ? "warning" : "error", message, retryable)
    this.#sample(true)
  }

  snapshot(): TerminalDiagnosticSnapshot {
    const unackedBytes = Math.max(
      0,
      this.#observedCursor - this.#acknowledgedCursor,
    )
    const terminal = this.#phase && !["opening", "running", "permission_pending"].includes(this.#phase)
    const health = terminal
      ? "closed"
      : this.#protocolErrors > 0 || (!this.#connected && this.#transportErrors > 3)
        ? "failed"
        : this.#transportErrors > 0
          || this.#resyncs > 0
          || unackedBytes >= this.#maximumUnackedBytes
          ? "degraded"
          : "healthy"
    return Object.freeze({
      terminalId: this.#binding.terminalId,
      phase: this.#phase,
      connected: this.#connected,
      connectionGeneration: this.#generation,
      outputFrames: this.#outputFrames,
      outputBytes: this.#outputBytes,
      inputFrames: this.#inputFrames,
      inputBytes: this.#inputBytes,
      resizeFrames: this.#resizeFrames,
      acknowledgedCursor: this.#acknowledgedCursor,
      observedCursor: this.#observedCursor,
      earliestCursor: this.#earliestCursor,
      unackedBytes,
      maximumUnackedBytes: this.#maximumUnackedBytes,
      reconnects: this.#reconnects,
      resyncs: this.#resyncs,
      backpressureCount: this.#backpressureCount,
      spills: this.#spills,
      spilledBytes: this.#spilledBytes,
      redactedFrames: this.#redactedFrames,
      binaryBytes: this.#binaryBytes,
      protocolErrors: this.#protocolErrors,
      transportErrors: this.#transportErrors,
      lastOutputAt: this.#lastOutputAt,
      lastInputAt: this.#lastInputAt,
      lastConnectedAt: this.#lastConnectedAt,
      lastDisconnectedAt: this.#lastDisconnectedAt,
      records: Object.freeze(this.#records.map((value) => Object.freeze({
        ...value,
        details: frozenDetails(value.details),
      }))),
      samples: Object.freeze(this.#samples.map((value) => Object.freeze({ ...value }))),
      health,
      revision: this.#revision,
    })
  }

  #record(
    code: string,
    severity: TerminalDiagnosticSeverity,
    message: string,
    retryable: boolean,
    details: Readonly<Record<string, string | number | boolean>> = {},
  ): void {
    const sequence = ++this.#recordSequence
    this.#records.push(Object.freeze({
      id: `${this.#binding.terminalId}:diagnostic:${sequence}`,
      code: code.slice(0, 256),
      severity,
      message: message.slice(0, 8_192),
      createdAt: timestamp(this.#now),
      retryable,
      details: frozenDetails(details),
    }))
    if (this.#records.length > this.#maximumRecords) {
      this.#records.splice(0, this.#records.length - this.#maximumRecords)
    }
    this.#revision += 1
  }

  #sample(force = false): void {
    const now = this.#now()
    if (!force && now - this.#lastSampleAt < this.#sampleIntervalMs) return
    this.#lastSampleAt = now
    this.#samples.push(Object.freeze({
      sampledAt: new Date(now).toISOString(),
      cursor: this.#observedCursor,
      acknowledgedCursor: this.#acknowledgedCursor,
      unackedBytes: Math.max(0, this.#observedCursor - this.#acknowledgedCursor),
      outputBytes: this.#outputBytes,
      outputFrames: this.#outputFrames,
      inputFrames: this.#inputFrames,
      reconnects: this.#reconnects,
      backpressureCount: this.#backpressureCount,
    }))
    if (this.#samples.length > this.#maximumSamples) {
      this.#samples.splice(0, this.#samples.length - this.#maximumSamples)
    }
  }
}
