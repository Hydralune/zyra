import type {
  TerminalBinding,
  TerminalClientCommand,
  TerminalServerFrame,
} from "./contracts.ts"

export type TerminalProtocolDirection = "client" | "server"
export type TerminalProtocolOutcome = "accepted" | "duplicate" | "rejected"

export interface TerminalProtocolAuditEntry {
  id: string
  direction: TerminalProtocolDirection
  kind: TerminalClientCommand["kind"] | TerminalServerFrame["kind"]
  outcome: TerminalProtocolOutcome
  sequence?: number
  firstCursor?: number
  nextCursor?: number
  connectionId?: string
  createdAt: string
  code?: string
  detail: Readonly<Record<string, string | number | boolean>>
}

export interface TerminalProtocolAuditSnapshot {
  terminalId: string
  connectionId?: string
  serverFrames: number
  clientFrames: number
  acceptedFrames: number
  duplicateFrames: number
  rejectedFrames: number
  outputSequence: number
  inputSequence: number
  resizeSequence: number
  acknowledgedSequence: number
  acceptedCursor: number
  acknowledgedCursor: number
  earliestCursor: number
  generation: number
  entries: readonly TerminalProtocolAuditEntry[]
  revision: number
}

export interface TerminalProtocolAuditOptions {
  maximumEntries?: number
  now?: () => number
}

function sameBinding(left: TerminalBinding, right: TerminalBinding): boolean {
  return (
    left.taskId === right.taskId
    && left.runId === right.runId
    && left.terminalId === right.terminalId
    && left.sessionId === right.sessionId
    && left.workspaceId === right.workspaceId
    && left.workspaceRevision === right.workspaceRevision
    && left.workerId === right.workerId
    && left.commandId === right.commandId
    && left.toolCallId === right.toolCallId
    && left.spanId === right.spanId
  )
}

function detail(
  value: Readonly<Record<string, string | number | boolean>> = {},
): Readonly<Record<string, string | number | boolean>> {
  return Object.freeze({ ...value })
}

function commandSequence(command: TerminalClientCommand): number | undefined {
  return "sequence" in command ? command.sequence : undefined
}

function serverSequence(frame: TerminalServerFrame): number | undefined {
  return "sequence" in frame ? frame.sequence : undefined
}

export class TerminalProtocolAudit {
  readonly #binding: TerminalBinding
  readonly #maximumEntries: number
  readonly #now: () => number
  #connectionId?: string
  #serverFrames = 0
  #clientFrames = 0
  #acceptedFrames = 0
  #duplicateFrames = 0
  #rejectedFrames = 0
  #outputSequence = 0
  #inputSequence = 0
  #resizeSequence = 0
  #acknowledgedSequence = 0
  #acceptedCursor = 0
  #acknowledgedCursor = 0
  #earliestCursor = 0
  #generation = 0
  #entrySequence = 0
  #entries: TerminalProtocolAuditEntry[] = []
  #revision = 0

  constructor(binding: TerminalBinding, options: TerminalProtocolAuditOptions = {}) {
    this.#binding = binding
    this.#maximumEntries = Math.min(
      100_000,
      Math.max(100, Math.floor(options.maximumEntries ?? 5_000)),
    )
    this.#now = options.now ?? Date.now
  }

  nextGeneration(): number {
    this.#generation += 1
    this.#connectionId = undefined
    this.#outputSequence = 0
    this.#acknowledgedSequence = 0
    this.#revision += 1
    return this.#generation
  }

  server(frame: TerminalServerFrame): TerminalProtocolOutcome {
    this.#serverFrames += 1
    if ("binding" in frame && frame.binding && !sameBinding(frame.binding, this.#binding)) {
      return this.#rejected(
        "server",
        frame.kind,
        "terminal.protocol_binding_mismatch",
        "Server frame binding differs from the adopted terminal.",
        serverSequence(frame),
      )
    }
    if (frame.kind === "hello") return this.#hello(frame)
    if (frame.kind === "output") return this.#output(frame)
    if (frame.kind === "cursor") {
      if (frame.cursor < this.#acceptedCursor) {
        return this.#rejected(
          "server",
          frame.kind,
          "terminal.protocol_cursor_regression",
          "Server cursor regressed behind admitted output.",
          frame.sequence,
          { cursor: frame.cursor, acceptedCursor: this.#acceptedCursor },
        )
      }
      this.#acceptedCursor = frame.cursor
      return this.#accepted("server", frame.kind, frame.sequence, {
        cursor: frame.cursor,
      })
    }
    if (frame.kind === "resync") {
      if (
        frame.acceptedCursor < frame.earliestCursor
        || frame.acceptedCursor > Math.max(frame.requestedCursor, this.#acceptedCursor)
      ) {
        return this.#rejected(
          "server",
          frame.kind,
          "terminal.protocol_resync_window_invalid",
          "Server resync cursor is outside the declared replay window.",
          undefined,
          {
            requestedCursor: frame.requestedCursor,
            acceptedCursor: frame.acceptedCursor,
            earliestCursor: frame.earliestCursor,
          },
        )
      }
      this.#acceptedCursor = frame.acceptedCursor
      this.#acknowledgedCursor = frame.acceptedCursor
      this.#earliestCursor = frame.earliestCursor
      this.#outputSequence = 0
      return this.#accepted("server", frame.kind, undefined, {
        reason: frame.reason,
        acceptedCursor: frame.acceptedCursor,
      })
    }
    if (frame.kind === "backpressure") {
      if (frame.requiredCursor > this.#acceptedCursor) {
        return this.#rejected(
          "server",
          frame.kind,
          "terminal.protocol_backpressure_cursor_invalid",
          "Backpressure requires a cursor the browser has not received.",
          undefined,
          {
            requiredCursor: frame.requiredCursor,
            acceptedCursor: this.#acceptedCursor,
          },
        )
      }
      return this.#accepted("server", frame.kind, undefined, {
        paused: frame.paused,
        unackedBytes: frame.unackedBytes,
      })
    }
    if (frame.kind === "spill") {
      if (
        frame.spill.firstCursor < 0
        || frame.spill.nextCursor < frame.spill.firstCursor
      ) {
        return this.#rejected(
          "server",
          frame.kind,
          "terminal.protocol_spill_cursor_invalid",
          "Spill artifact has an invalid cursor range.",
        )
      }
      return this.#accepted("server", frame.kind, undefined, {
        artifactId: frame.spill.artifactId,
        firstCursor: frame.spill.firstCursor,
        nextCursor: frame.spill.nextCursor,
      })
    }
    if (frame.kind === "status") {
      if (frame.status.earliestCursor > frame.status.cursor) {
        return this.#rejected(
          "server",
          frame.kind,
          "terminal.protocol_status_cursor_invalid",
          "Status earliest cursor exceeds its current cursor.",
        )
      }
      this.#earliestCursor = frame.status.earliestCursor
      this.#acceptedCursor = Math.max(this.#acceptedCursor, frame.status.cursor)
      return this.#accepted("server", frame.kind, undefined, {
        phase: frame.status.phase,
        cursor: frame.status.cursor,
      })
    }
    if (frame.kind === "error") {
      return this.#accepted("server", frame.kind, undefined, {
        code: frame.code,
        retryable: frame.retryable,
      })
    }
    return this.#accepted("server", frame.kind, undefined, {
      nonce: frame.nonce,
    })
  }

  client(command: TerminalClientCommand): TerminalProtocolOutcome {
    this.#clientFrames += 1
    if (command.terminalId !== this.#binding.terminalId) {
      return this.#rejected(
        "client",
        command.kind,
        "terminal.protocol_client_binding_mismatch",
        "Client command targets another terminal.",
        commandSequence(command),
      )
    }
    if (this.#connectionId && command.connectionId !== this.#connectionId) {
      return this.#rejected(
        "client",
        command.kind,
        "terminal.protocol_connection_mismatch",
        "Client command targets another socket generation.",
        commandSequence(command),
      )
    }
    if (command.kind === "input") {
      if (command.sequence <= this.#inputSequence) {
        return this.#duplicate("client", command.kind, command.sequence, {
          previousSequence: this.#inputSequence,
        })
      }
      this.#inputSequence = command.sequence
      return this.#accepted("client", command.kind, command.sequence, {
        byteLength: new TextEncoder().encode(command.data).byteLength,
      })
    }
    if (command.kind === "resize") {
      if (command.sequence <= this.#resizeSequence) {
        return this.#duplicate("client", command.kind, command.sequence, {
          previousSequence: this.#resizeSequence,
        })
      }
      this.#resizeSequence = command.sequence
      return this.#accepted("client", command.kind, command.sequence, {
        rows: command.rows,
        cols: command.cols,
      })
    }
    if (command.kind === "ack") {
      if (
        command.cursor < this.#acknowledgedCursor
        || command.cursor > this.#acceptedCursor
      ) {
        return this.#rejected(
          "client",
          command.kind,
          "terminal.protocol_ack_cursor_invalid",
          "Client acknowledgement is outside the admitted cursor window.",
          command.sequence,
          {
            cursor: command.cursor,
            acknowledgedCursor: this.#acknowledgedCursor,
            acceptedCursor: this.#acceptedCursor,
          },
        )
      }
      this.#acknowledgedCursor = command.cursor
      this.#acknowledgedSequence = Math.max(
        this.#acknowledgedSequence,
        command.sequence,
      )
      return this.#accepted("client", command.kind, command.sequence, {
        cursor: command.cursor,
      })
    }
    if (command.kind === "kill") {
      return this.#accepted("client", command.kind, command.sequence, {
        reasonLength: command.reason.length,
      })
    }
    return this.#accepted("client", command.kind, undefined, {
      nonce: command.nonce,
    })
  }

  snapshot(): TerminalProtocolAuditSnapshot {
    return Object.freeze({
      terminalId: this.#binding.terminalId,
      connectionId: this.#connectionId,
      serverFrames: this.#serverFrames,
      clientFrames: this.#clientFrames,
      acceptedFrames: this.#acceptedFrames,
      duplicateFrames: this.#duplicateFrames,
      rejectedFrames: this.#rejectedFrames,
      outputSequence: this.#outputSequence,
      inputSequence: this.#inputSequence,
      resizeSequence: this.#resizeSequence,
      acknowledgedSequence: this.#acknowledgedSequence,
      acceptedCursor: this.#acceptedCursor,
      acknowledgedCursor: this.#acknowledgedCursor,
      earliestCursor: this.#earliestCursor,
      generation: this.#generation,
      entries: Object.freeze(this.#entries.map((entry) => Object.freeze({
        ...entry,
        detail: detail(entry.detail),
      }))),
      revision: this.#revision,
    })
  }

  #hello(frame: Extract<TerminalServerFrame, { kind: "hello" }>): TerminalProtocolOutcome {
    if (this.#connectionId && this.#connectionId !== frame.connectionId) {
      this.nextGeneration()
    }
    this.#connectionId = frame.connectionId
    this.#acceptedCursor = frame.acceptedCursor
    this.#acknowledgedCursor = frame.acceptedCursor
    this.#earliestCursor = frame.earliestCursor
    this.#outputSequence = 0
    return this.#accepted("server", frame.kind, undefined, {
      connectionId: frame.connectionId,
      acceptedCursor: frame.acceptedCursor,
      earliestCursor: frame.earliestCursor,
    })
  }

  #output(frame: Extract<TerminalServerFrame, { kind: "output" }>): TerminalProtocolOutcome {
    if (frame.sequence <= this.#outputSequence) {
      return this.#duplicate("server", frame.kind, frame.sequence, {
        previousSequence: this.#outputSequence,
      })
    }
    if (
      this.#outputSequence > 0
      && (
        frame.sequence !== this.#outputSequence + 1
        || frame.firstCursor !== this.#acceptedCursor
      )
    ) {
      return this.#rejected(
        "server",
        frame.kind,
        "terminal.protocol_output_gap",
        "Output frame has a sequence or cursor gap.",
        frame.sequence,
        {
          previousSequence: this.#outputSequence,
          expectedCursor: this.#acceptedCursor,
          firstCursor: frame.firstCursor,
        },
      )
    }
    this.#outputSequence = frame.sequence
    this.#acceptedCursor = frame.nextCursor
    return this.#accepted("server", frame.kind, frame.sequence, {
      firstCursor: frame.firstCursor,
      nextCursor: frame.nextCursor,
      byteLength: frame.byteLength,
      redacted: frame.redacted,
    })
  }

  #accepted(
    direction: TerminalProtocolDirection,
    kind: TerminalProtocolAuditEntry["kind"],
    sequence?: number,
    value: Readonly<Record<string, string | number | boolean>> = {},
  ): TerminalProtocolOutcome {
    this.#acceptedFrames += 1
    this.#append(direction, kind, "accepted", sequence, undefined, value)
    return "accepted"
  }

  #duplicate(
    direction: TerminalProtocolDirection,
    kind: TerminalProtocolAuditEntry["kind"],
    sequence?: number,
    value: Readonly<Record<string, string | number | boolean>> = {},
  ): TerminalProtocolOutcome {
    this.#duplicateFrames += 1
    this.#append(direction, kind, "duplicate", sequence, undefined, value)
    return "duplicate"
  }

  #rejected(
    direction: TerminalProtocolDirection,
    kind: TerminalProtocolAuditEntry["kind"],
    code: string,
    message: string,
    sequence?: number,
    value: Readonly<Record<string, string | number | boolean>> = {},
  ): TerminalProtocolOutcome {
    this.#rejectedFrames += 1
    this.#append(direction, kind, "rejected", sequence, code, {
      ...value,
      message,
    })
    return "rejected"
  }

  #append(
    direction: TerminalProtocolDirection,
    kind: TerminalProtocolAuditEntry["kind"],
    outcome: TerminalProtocolOutcome,
    sequence?: number,
    code?: string,
    value: Readonly<Record<string, string | number | boolean>> = {},
  ): void {
    const id = `${this.#binding.terminalId}:protocol:${++this.#entrySequence}`
    this.#entries.push(Object.freeze({
      id,
      direction,
      kind,
      outcome,
      sequence,
      firstCursor: typeof value.firstCursor === "number" ? value.firstCursor : undefined,
      nextCursor: typeof value.nextCursor === "number" ? value.nextCursor : undefined,
      connectionId: this.#connectionId,
      createdAt: new Date(this.#now()).toISOString(),
      code,
      detail: detail(value),
    }))
    if (this.#entries.length > this.#maximumEntries) {
      this.#entries.splice(0, this.#entries.length - this.#maximumEntries)
    }
    this.#revision += 1
  }
}
