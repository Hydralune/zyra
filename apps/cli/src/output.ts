import type { Writable } from "node:stream"
import {
  CLI_RECORD_SCHEMA,
  CLI_RESULT_SCHEMA,
  type CliRecord,
  type CliResultRecord,
} from "./contracts.ts"

const SENSITIVE_KEY = /(authorization|cookie|credential|password|secret|token|cwd|workspace[_-]?path|event[_-]?log|sqlite|permission[_-]?store|artifact[_-]?root|tool[_-]?workspace)/i
const WINDOWS_ABSOLUTE_PATH = /^[A-Za-z]:[\\/]/
const UNIX_ABSOLUTE_PATH = /^\/(?!\/)/
const EMBEDDED_WINDOWS_ABSOLUTE_PATH = /(^|[\s("'=])([A-Za-z]:[\\/][^\s"'<>|]*)/g
const EMBEDDED_UNIX_ABSOLUTE_PATH = /(^|[\s("'=])(\/(?!\/)[^\s"'<>]*)/g
const CAPABILITY_PATH = /([\\/]capability[\\/])[^\\/?#\s]+/gi
const URL_USERINFO = /([a-z][a-z0-9+.-]*:\/\/)[^/@\s]+@/gi
const AUTHORIZATION_VALUE = /\b(Bearer|Basic)\s+[^\s,;]+/gi
const INLINE_SECRET_VALUE = /\b(api[_-]?key|token|password|secret|credential)\s*[:=]\s*["']?[^\s"',;&]+/gi
const ANSI_ESCAPE = /\u001b(?:\[[0-?]*[ -/]*[@-~]|\][^\u0007]*(?:\u0007|\u001b\\))/g

function scrubString(value: string): string {
  const withoutAnsi = value
    .replace(ANSI_ESCAPE, "")
    .replace(CAPABILITY_PATH, "$1[redacted]")
    .replace(URL_USERINFO, "$1[redacted]@")
    .replace(AUTHORIZATION_VALUE, "$1 [redacted]")
    .replace(INLINE_SECRET_VALUE, "$1=[redacted]")
  if (WINDOWS_ABSOLUTE_PATH.test(withoutAnsi) || UNIX_ABSOLUTE_PATH.test(withoutAnsi)) {
    return "[redacted-path]"
  }
  return withoutAnsi
    .replace(EMBEDDED_WINDOWS_ABSOLUTE_PATH, "$1[redacted-path]")
    .replace(EMBEDDED_UNIX_ABSOLUTE_PATH, "$1[redacted-path]")
}

export function sanitizeForOutput(
  value: unknown,
  seen: WeakSet<object> = new WeakSet<object>(),
): unknown {
  if (value === null || value === undefined || typeof value === "boolean" || typeof value === "number") {
    return value
  }
  if (typeof value === "string") return scrubString(value)
  if (typeof value === "bigint") return value.toString()
  if (typeof value !== "object") return String(value)
  if (seen.has(value)) return "[circular]"
  seen.add(value)
  if (Array.isArray(value)) {
    return value.slice(0, 10_000).map((entry) => sanitizeForOutput(entry, seen))
  }
  const result: Record<string, unknown> = {}
  for (const [key, entry] of Object.entries(value as Record<string, unknown>)) {
    result[key] = SENSITIVE_KEY.test(key)
      ? "[redacted]"
      : sanitizeForOutput(entry, seen)
  }
  return result
}

function streamErrorCode(error: unknown): string {
  return String((error as NodeJS.ErrnoException | undefined)?.code || "")
}

export class JsonlWriter {
  readonly #stream: Writable
  #closed = false
  #error?: Error

  constructor(stream: Writable) {
    this.#stream = stream
    stream.on("error", (error: NodeJS.ErrnoException) => {
      if (error.code === "EPIPE") {
        this.#closed = true
        return
      }
      this.#error = error
      this.#closed = true
    })
  }

  get closed(): boolean {
    return this.#closed
  }

  get error(): Error | undefined {
    return this.#error
  }

  write(value: unknown): boolean {
    if (this.#closed) return false
    const safe = sanitizeForOutput(value)
    let line: string
    try {
      line = `${JSON.stringify(safe)}\n`
    } catch (error) {
      this.#error = error instanceof Error ? error : new Error(String(error))
      this.#closed = true
      return false
    }
    try {
      this.#stream.write(line, (error?: Error | null) => {
        if (!error) return
        if (streamErrorCode(error) === "EPIPE") this.#closed = true
        else {
          this.#error = error
          this.#closed = true
        }
      })
      return true
    } catch (error) {
      if (streamErrorCode(error) === "EPIPE") this.#closed = true
      else {
        this.#error = error instanceof Error ? error : new Error(String(error))
        this.#closed = true
      }
      return false
    }
  }
}

export class CliOutput {
  readonly #stdout: JsonlWriter
  readonly #stderr: Writable
  readonly requestId: string
  readonly command: string
  #diagnosticsClosed = false

  constructor(input: {
    stdout: Writable
    stderr: Writable
    requestId: string
    command: string
  }) {
    this.#stdout = new JsonlWriter(input.stdout)
    this.#stderr = input.stderr
    this.requestId = input.requestId
    this.command = input.command
    input.stderr.on("error", (error: NodeJS.ErrnoException) => {
      if (error.code === "EPIPE") this.#diagnosticsClosed = true
    })
  }

  get pipeClosed(): boolean {
    return this.#stdout.closed
  }

  event(
    payload: Readonly<Record<string, unknown>>,
    binding: {
      taskId?: string
      runId?: string
      cursor?: string
      generation?: number
    } = {},
  ): void {
    const record: CliRecord = {
      schema: CLI_RECORD_SCHEMA,
      type: "event",
      timestamp: new Date().toISOString(),
      request_id: this.requestId,
      command: this.command,
      task_id: binding.taskId,
      run_id: binding.runId,
      cursor: binding.cursor,
      generation: binding.generation,
      payload,
    }
    this.#stdout.write(record)
  }

  result(input: Omit<CliResultRecord, "schema" | "type" | "timestamp" | "request_id" | "command">): void {
    this.#stdout.write({
      schema: CLI_RESULT_SCHEMA,
      type: "result",
      timestamp: new Date().toISOString(),
      request_id: this.requestId,
      command: this.command,
      ...input,
    } satisfies CliResultRecord)
  }

  diagnostic(message: string): void {
    if (this.#diagnosticsClosed) return
    const safe = scrubString(message)
    try {
      this.#stderr.write(`${safe}\n`)
    } catch (error) {
      if (streamErrorCode(error) === "EPIPE") this.#diagnosticsClosed = true
    }
  }
}
