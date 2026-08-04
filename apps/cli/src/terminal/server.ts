import { createHash, randomBytes, randomUUID, timingSafeEqual } from "node:crypto"
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http"
import { realpath } from "node:fs/promises"
import type { ChildProcess } from "node:child_process"
import { executeTerminalAction } from "./actions.ts"
import {
  checksum,
  createFrame,
  parseDispatchRequest,
  TerminalProtocolError,
  type FrameKind,
  type TerminalActionResult,
  type TerminalDispatchRequest,
  type TerminalFrame,
} from "./contracts.ts"

const CAPABILITIES = Object.freeze(["artifact", "checkpoint", "code-change", "shell"] as const)
const OPERATIONS = Object.freeze([
  "tool.artifact_write",
  "tool.file_delete",
  "tool.file_edit",
  "tool.file_read",
  "tool.file_write",
  "tool.shell",
  "tool.web_search",
] as const)
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]", "::1"])

type DispatchStatus = "accepted" | "running" | "cancelling" | "cancelled" | "succeeded" | "failed"

interface DispatchRecord {
  dispatchId: string
  envelopeId: string
  leaseId: string
  inputDigest: string
  idempotencyKey: string
  status: DispatchStatus
  startedAt: number
  completedAt?: number
  frames: TerminalFrame[]
  controller: AbortController
  child?: ChildProcess
  result?: TerminalActionResult
}

export interface TerminalNodeStatus {
  schema: "zyra.cli-terminal-status/v1"
  backend_id: string
  generation: string
  worker: "CodeWorkerRuntime"
  accepting: boolean
  draining: boolean
  active_dispatches: number
  terminal_dispatches: number
  registered: boolean
  real_terminal_dispatch_claimed: boolean
}

export interface TerminalNodeServerOptions {
  startupRoot?: string
  backendId?: string
  generation?: string
  ownerId?: string
  capabilityToken?: string
  maximumConcurrency?: number
  maximumRequestBytes?: number
  maximumResponseBytes?: number
}

function json(response: ServerResponse, status: number, value: Record<string, unknown>, generation: string): void {
  const body = Buffer.from(JSON.stringify(value), "utf8")
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": body.byteLength,
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "x-zyra-backend-generation": generation,
  })
  response.end(body)
}

function genericNotFound(response: ServerResponse, generation: string): void {
  json(response, 404, { schema: "zyra.backend-error/v1", error: "not_found", message: "Endpoint not found." }, generation)
}

function protocolError(error: unknown): TerminalProtocolError {
  if (error instanceof TerminalProtocolError) return error
  if (error instanceof Error && error.name === "AbortError") {
    return new TerminalProtocolError("Dispatch cancelled.", "dispatch_aborted", 409)
  }
  return new TerminalProtocolError(
    error instanceof Error ? error.message : "Terminal action failed.",
    "execution_failed",
    422,
    { recoveryIntent: "reconcile", cause: error },
  )
}

function failureKind(error: TerminalProtocolError): string {
  return new Set([
    "backend_unavailable",
    "backend_timeout",
    "backend_capacity",
    "backend_protocol",
    "workspace_unavailable",
    "workspace_corrupt",
    "lease_conflict",
    "lease_expired",
    "turn_timeout",
    "execution_failed",
    "dispatch_aborted",
    "provider_failure",
  ]).has(error.code) ? error.code : "backend_protocol"
}

function safeHost(request: IncomingMessage): boolean {
  const raw = request.headers.host ?? ""
  try {
    return LOOPBACK_HOSTS.has(new URL(`http://${raw}`).hostname)
  } catch {
    return false
  }
}

function secureSegment(left: string, right: string): boolean {
  const a = createHash("sha256").update(left, "utf8").digest()
  const b = createHash("sha256").update(right, "utf8").digest()
  return timingSafeEqual(a, b)
}

function redactValue(value: unknown, secrets: readonly string[]): unknown {
  if (typeof value === "string") {
    let rendered = value
    for (const secret of secrets) {
      if (secret.length >= 8) rendered = rendered.replaceAll(secret, "[redacted]")
    }
    return rendered
  }
  if (Array.isArray(value)) return value.map((item) => redactValue(item, secrets))
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([key, item]) => [key, redactValue(item, secrets)]))
  }
  return value
}

async function body(request: IncomingMessage, maximumBytes: number): Promise<Buffer> {
  const chunks: Buffer[] = []
  let size = 0
  let exceeded = false
  for await (const raw of request) {
    const chunk = Buffer.isBuffer(raw) ? raw : Buffer.from(raw)
    size += chunk.byteLength
    if (size > maximumBytes) exceeded = true
    else chunks.push(chunk)
  }
  if (exceeded) throw new TerminalProtocolError("Request body exceeds the terminal budget.", "backend_protocol", 413)
  return Buffer.concat(chunks)
}

function objectBody(raw: Buffer): Record<string, unknown> {
  if (!raw.byteLength) return {}
  try {
    const value = JSON.parse(raw.toString("utf8"))
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("object required")
    return value as Record<string, unknown>
  } catch (error) {
    throw new TerminalProtocolError("Control request is not valid JSON.", "backend_protocol", 400, { cause: error })
  }
}

export class TerminalNodeServer {
  readonly startupRoot: string
  readonly backendId: string
  readonly generation: string
  readonly ownerId: string
  readonly capabilityToken: string
  readonly maximumConcurrency: number
  readonly maximumRequestBytes: number
  readonly maximumResponseBytes: number
  #server?: Server
  #port?: number
  #accepting = true
  #registered = false
  #stopping = false
  #dispatches = new Map<string, DispatchRecord>()
  #idempotency = new Map<string, DispatchRecord>()

  private constructor(input: Required<TerminalNodeServerOptions>) {
    this.startupRoot = input.startupRoot
    this.backendId = input.backendId
    this.generation = input.generation
    this.ownerId = input.ownerId
    this.capabilityToken = input.capabilityToken
    this.maximumConcurrency = input.maximumConcurrency
    this.maximumRequestBytes = input.maximumRequestBytes
    this.maximumResponseBytes = input.maximumResponseBytes
  }

  static async create(options: TerminalNodeServerOptions = {}): Promise<TerminalNodeServer> {
    const startupRoot = await realpath(options.startupRoot ?? process.cwd())
    return new TerminalNodeServer({
      startupRoot,
      backendId: options.backendId ?? `terminal_${randomUUID().replaceAll("-", "")}`,
      generation: options.generation ?? randomUUID(),
      ownerId: options.ownerId ?? `zyra-cli:${process.pid}:${randomUUID()}`,
      capabilityToken: options.capabilityToken ?? randomBytes(32).toString("base64url"),
      maximumConcurrency: options.maximumConcurrency ?? 2,
      maximumRequestBytes: options.maximumRequestBytes ?? 16 * 1024 * 1024,
      maximumResponseBytes: options.maximumResponseBytes ?? 16 * 1024 * 1024,
    })
  }

  get endpoint(): string {
    if (!this.#port) throw new Error("Terminal node is not listening.")
    return `http://127.0.0.1:${this.#port}/capability/${this.capabilityToken}`
  }

  get safeEndpoint(): string {
    if (!this.#port) return "loopback:unbound"
    return `loopback:${this.#port}`
  }

  get activeDispatches(): number {
    return [...this.#dispatches.values()].filter((record) => ["accepted", "running", "cancelling"].includes(record.status)).length
  }

  get terminalDispatches(): number {
    return this.#dispatches.size - this.activeDispatches
  }

  markRegistered(value: boolean): void {
    this.#registered = value
  }

  status(): TerminalNodeStatus {
    return Object.freeze({
      schema: "zyra.cli-terminal-status/v1",
      backend_id: this.backendId,
      generation: this.generation,
      worker: "CodeWorkerRuntime",
      accepting: this.#accepting && !this.#stopping,
      draining: !this.#accepting,
      active_dispatches: this.activeDispatches,
      terminal_dispatches: this.terminalDispatches,
      registered: this.#registered,
      real_terminal_dispatch_claimed: this.#registered,
    })
  }

  async listen(): Promise<void> {
    if (this.#server) return
    const server = createServer((request, response) => {
      void this.#handle(request, response).catch((error) => {
        if (response.headersSent) response.destroy(error instanceof Error ? error : undefined)
        else {
          const selected = protocolError(error)
          json(response, selected.status, {
            schema: "zyra.backend-error/v1",
            error: selected.code,
            message: selected.message,
            retryable: selected.retryable,
            recovery_intent: selected.recoveryIntent,
          }, this.generation)
        }
      })
    })
    server.requestTimeout = 0
    server.headersTimeout = 10_000
    server.keepAliveTimeout = 2_000
    await new Promise<void>((resolveListen, reject) => {
      server.once("error", reject)
      server.listen(0, "127.0.0.1", () => resolveListen())
    })
    const address = server.address()
    if (!address || typeof address === "string" || address.address !== "127.0.0.1") {
      server.close()
      throw new Error("Terminal node failed to bind a loopback TCP endpoint.")
    }
    this.#server = server
    this.#port = address.port
  }

  async drain(reason = "terminal node draining"): Promise<void> {
    this.#accepting = false
    for (const record of this.#dispatches.values()) {
      if (["accepted", "running"].includes(record.status)) {
        record.status = "cancelling"
        record.controller.abort(new Error(reason))
        record.child?.kill("SIGKILL")
      }
    }
  }

  resume(): void {
    if (this.#stopping) throw new TerminalProtocolError("Terminal node is stopping.", "backend_unavailable", 409)
    this.#accepting = true
  }

  async settle(timeoutMs = 5_000): Promise<void> {
    const deadline = Date.now() + Math.max(100, timeoutMs)
    while (this.activeDispatches && Date.now() < deadline) {
      await new Promise((resolveWait) => setTimeout(resolveWait, 25))
    }
    if (this.activeDispatches) {
      for (const record of this.#dispatches.values()) {
        if (["accepted", "running", "cancelling"].includes(record.status)) {
          record.controller.abort(new Error("terminal shutdown deadline exceeded"))
          record.child?.kill("SIGKILL")
        }
      }
    }
  }

  async close(): Promise<void> {
    if (!this.#server) return
    this.#stopping = true
    this.#accepting = false
    const server = this.#server
    await new Promise<void>((resolveClose) => {
      server.close(() => resolveClose())
      server.closeIdleConnections()
      server.closeAllConnections()
    })
    this.#server = undefined
    this.#port = undefined
  }

  async #handle(request: IncomingMessage, response: ServerResponse): Promise<void> {
    response.setHeader("cache-control", "no-store")
    response.setHeader("x-content-type-options", "nosniff")
    response.setHeader("x-zyra-backend-generation", this.generation)
    if (!safeHost(request)) return genericNotFound(response, this.generation)
    const rawPath = (request.url ?? "").split("?", 1)[0]
    const segments = rawPath.split("/").filter(Boolean)
    if (segments.length < 3 || segments[0] !== "capability" || !secureSegment(segments[1] ?? "", this.capabilityToken)) {
      return genericNotFound(response, this.generation)
    }
    const route = `/${segments.slice(2).join("/")}`
    if (request.method === "GET" && route === "/health") return this.#health(response)
    if (request.method === "POST" && route === "/v1/dispatch") return this.#dispatch(request, response)
    if (request.method === "GET" && route === "/v1/dispatches") return this.#list(response)
    const dispatchMatch = /^\/v1\/dispatches\/([^/]+)$/u.exec(route)
    if (request.method === "GET" && dispatchMatch) return this.#inspect(response, decodeURIComponent(dispatchMatch[1]))
    const cancelDispatchMatch = /^\/v1\/dispatches\/([^/]+)\/cancel$/u.exec(route)
    if (request.method === "POST" && cancelDispatchMatch) return this.#cancel(request, response, decodeURIComponent(cancelDispatchMatch[1]))
    const cancelEnvelopeMatch = /^\/v1\/envelopes\/([^/]+)\/cancel$/u.exec(route)
    if (request.method === "POST" && cancelEnvelopeMatch) return this.#cancel(request, response, decodeURIComponent(cancelEnvelopeMatch[1]))
    if (request.method === "POST" && route === "/v1/control/drain") return this.#drainRequest(request, response)
    if (request.method === "POST" && route === "/v1/control/resume") return this.#resumeRequest(request, response)
    return genericNotFound(response, this.generation)
  }

  #health(response: ServerResponse): void {
    const value = this.status()
    json(response, 200, {
      schema: "zyra.backend-health/v1",
      backend_id: this.backendId,
      ok: true,
      worker: "CodeWorkerRuntime",
      runtime_worker: "CodeWorkerRuntime",
      generation: this.generation,
      capabilities: [...CAPABILITIES],
      operations: [...OPERATIONS],
      maximum_concurrency: this.maximumConcurrency,
      active_dispatches: value.active_dispatches,
      terminal_dispatches: value.terminal_dispatches,
      accepting: value.accepting,
      draining: value.draining,
      checked_at: Date.now() / 1000,
    }, this.generation)
  }

  #safeRecord(record: DispatchRecord): Record<string, unknown> {
    return {
      schema: "zyra.backend-dispatch-status/v1",
      dispatch_id: record.dispatchId,
      envelope_id: record.envelopeId,
      backend_lease_id: record.leaseId,
      status: record.status,
      generation: this.generation,
      started_at: record.startedAt,
      completed_at: record.completedAt ?? null,
      frame_count: record.frames.length,
      output_observed: record.frames.some((frame) => frame.output_observed),
      result_digest: record.result ? checksum(record.result) : null,
    }
  }

  #list(response: ServerResponse): void {
    json(response, 200, {
      schema: "zyra.backend-dispatch-list/v1",
      backend_id: this.backendId,
      generation: this.generation,
      dispatches: [...this.#dispatches.values()].map((record) => this.#safeRecord(record)),
    }, this.generation)
  }

  #inspect(response: ServerResponse, id: string): void {
    const record = this.#dispatches.get(id)
    if (!record) return genericNotFound(response, this.generation)
    json(response, 200, { ...this.#safeRecord(record), frames: record.frames }, this.generation)
  }

  async #cancel(request: IncomingMessage, response: ServerResponse, id: string): Promise<void> {
    const payload = objectBody(await body(request, 64 * 1024))
    const record = this.#dispatches.get(id)
    if (!record) return genericNotFound(response, this.generation)
    const reason = typeof payload.reason === "string" && payload.reason.trim() ? payload.reason.trim() : "remote cancellation requested"
    if (["accepted", "running"].includes(record.status)) {
      record.status = "cancelling"
      record.controller.abort(new Error(reason))
      record.child?.kill("SIGKILL")
    }
    json(response, 200, { ...this.#safeRecord(record), cancellation_requested: true, reason }, this.generation)
  }

  async #drainRequest(request: IncomingMessage, response: ServerResponse): Promise<void> {
    const payload = objectBody(await body(request, 64 * 1024))
    const reason = typeof payload.reason === "string" && payload.reason.trim() ? payload.reason.trim() : "remote drain requested"
    await this.drain(reason)
    json(response, 200, { ...this.status(), reason }, this.generation)
  }

  async #resumeRequest(request: IncomingMessage, response: ServerResponse): Promise<void> {
    objectBody(await body(request, 64 * 1024))
    this.resume()
    json(response, 200, { ...this.status(), reason: "remote resume requested" }, this.generation)
  }

  async #dispatch(request: IncomingMessage, response: ServerResponse): Promise<void> {
    if (!this.#accepting || this.#stopping) {
      throw new TerminalProtocolError("Terminal node is draining.", "backend_unavailable", 503, { retryable: true, recoveryIntent: "change_backend" })
    }
    if (this.activeDispatches >= this.maximumConcurrency) {
      throw new TerminalProtocolError("Terminal node concurrency is exhausted.", "resource_exhausted", 429, { retryable: true, recoveryIntent: "change_backend" })
    }
    const parsed = parseDispatchRequest(await body(request, this.maximumRequestBytes), this.maximumRequestBytes)
    this.#validateEnvelope(parsed)
    this.#validateHeaders(request, parsed)
    const idempotencyHeader = request.headers["idempotency-key"]
    const idempotencyKey = typeof idempotencyHeader === "string" ? idempotencyHeader : parsed.envelope.idempotency_key
    if (!idempotencyKey || idempotencyKey !== parsed.envelope.idempotency_key) {
      throw new TerminalProtocolError("Idempotency binding changed.", "backend_protocol", 409)
    }
    const replay = this.#idempotency.get(idempotencyKey)
    if (replay) {
      if (replay.inputDigest !== parsed.input_digest) {
        throw new TerminalProtocolError("Idempotency key was reused with a different input digest.", "lease_conflict", 409)
      }
      if (["accepted", "running", "cancelling"].includes(replay.status)) {
        throw new TerminalProtocolError("Idempotent dispatch is already active.", "lease_conflict", 409)
      }
      return this.#replay(response, replay)
    }
    const record: DispatchRecord = {
      dispatchId: parsed.envelope.envelope_id,
      envelopeId: parsed.envelope.envelope_id,
      leaseId: parsed.envelope.backend_lease_id,
      inputDigest: parsed.input_digest,
      idempotencyKey,
      status: "accepted",
      startedAt: Date.now() / 1000,
      frames: [],
      controller: new AbortController(),
    }
    this.#dispatches.set(record.dispatchId, record)
    this.#idempotency.set(idempotencyKey, record)
    response.writeHead(200, {
      "content-type": "application/x-ndjson; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      "x-zyra-backend-generation": this.generation,
    })
    let responseBytes = 0
    const secrets = [
      this.capabilityToken,
      this.startupRoot,
      parsed.envelope.workspace_root,
      parsed.envelope.artifact_root,
      ...Object.entries(process.env)
        .filter(([key, value]) => /(?:credential|password|secret|token|api[_-]?key)/iu.test(key) && typeof value === "string")
        .map(([, value]) => value as string),
    ]
    const redact = <T>(value: T): T => redactValue(value, secrets) as T
    const emit = (kind: FrameKind, payload: Record<string, unknown>, outputObserved: boolean) => {
      const frame = createFrame(record.frames.length + 1, kind, redact(payload), outputObserved)
      const line = `${JSON.stringify(frame)}\n`
      responseBytes += Buffer.byteLength(line, "utf8")
      if (responseBytes > this.maximumResponseBytes) {
        record.controller.abort(new Error("terminal response budget exceeded"))
        record.child?.kill("SIGKILL")
        throw new TerminalProtocolError("Terminal response exceeds the transport budget.", "backend_protocol", 422, { recoveryIntent: "reconcile" })
      }
      record.frames.push(frame)
      response.write(line)
    }
    try {
      emit("accepted", { dispatch_id: record.dispatchId, envelope_id: record.envelopeId, backend_lease_id: record.leaseId, generation: this.generation }, false)
      record.status = "running"
      emit("progress", { phase: "permission_receipt_admitted", permission_receipt_id: parsed.payload.permission.receipt_id, generation: this.generation }, false)
      const actionResult = await executeTerminalAction({
        startupRoot: this.startupRoot,
        envelope: parsed.envelope,
        action: parsed.payload,
        signal: record.controller.signal,
        events: {
          redact,
          stdout: (value) => emit("stdout", { text: value }, true),
          stderr: (value) => emit("stderr", { text: value }, true),
          heartbeat: () => emit("heartbeat", { phase: "running", generation: this.generation }, false),
          artifact: (value) => emit("artifact", value, true),
          process: (child) => { record.child = child },
        },
      })
      if (record.controller.signal.aborted) throw record.controller.signal.reason
      const safeResult = redact(actionResult)
      record.result = safeResult
      record.status = "succeeded"
      record.completedAt = Date.now() / 1000
      emit("result", { ...safeResult }, true)
      response.end()
    } catch (error) {
      record.completedAt = Date.now() / 1000
      if (record.controller.signal.aborted) {
        record.status = "cancelled"
        emit("cancelled", { reason: error instanceof Error ? error.message : "Dispatch cancelled.", generation: this.generation }, record.frames.some((frame) => frame.output_observed))
      } else {
        const selected = protocolError(error)
        record.status = "failed"
        emit("error", {
          kind: failureKind(selected),
          code: selected.code,
          message: selected.message,
          retryable: selected.retryable,
          recovery_intent: selected.recoveryIntent,
          generation: this.generation,
        }, record.frames.some((frame) => frame.output_observed))
      }
      response.end()
    } finally {
      record.child = undefined
    }
  }

  #replay(response: ServerResponse, record: DispatchRecord): void {
    response.writeHead(200, {
      "content-type": "application/x-ndjson; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      "x-zyra-backend-generation": this.generation,
      "x-zyra-replayed": "true",
    })
    for (const frame of record.frames) response.write(`${JSON.stringify(frame)}\n`)
    response.end()
  }

  #validateEnvelope(request: TerminalDispatchRequest): void {
    const envelope = request.envelope
    const required = [
      envelope.envelope_id,
      envelope.run_id,
      envelope.task_id,
      envelope.turn_id,
      envelope.backend_lease_id,
      envelope.provider_route_id,
      envelope.provider_route_checksum,
      envelope.provider_credential_fingerprint,
      envelope.provider_transport_id,
      envelope.m0_execution_ref,
    ]
    if (required.some((value) => typeof value !== "string" || !value.trim())) {
      throw new TerminalProtocolError("Dispatch envelope omitted a required identity or route reference.", "backend_protocol")
    }
    if (
      envelope.backend_id !== this.backendId
      || envelope.runtime_worker !== "CodeWorkerRuntime"
      || envelope.backend_kind !== "edge_http"
      || envelope.backend_location !== "local"
      || !Number.isSafeInteger(envelope.provider_catalog_revision)
      || envelope.provider_catalog_revision < 1
      || !Number.isSafeInteger(envelope.provider_credential_version)
      || envelope.provider_credential_version < 1
      || !Number.isSafeInteger(envelope.attempt)
      || envelope.attempt < 1
    ) {
      throw new TerminalProtocolError("Dispatch envelope attestation does not match this terminal node.", "backend_protocol", 409)
    }
    const headerEnvelope = request.envelope.envelope_id
    if (!headerEnvelope) throw new TerminalProtocolError("Envelope identity is invalid.", "backend_protocol")
  }

  #validateHeaders(request: IncomingMessage, dispatch: TerminalDispatchRequest): void {
    const expected: Record<string, string> = {
      "x-zyra-envelope-id": dispatch.envelope.envelope_id,
      "x-zyra-backend-lease-id": dispatch.envelope.backend_lease_id,
      "x-zyra-provider-route-ref": dispatch.envelope.provider_route_id,
      "x-zyra-m0-execution-ref": dispatch.envelope.m0_execution_ref,
    }
    for (const [name, value] of Object.entries(expected)) {
      if (request.headers[name] !== value) {
        throw new TerminalProtocolError(`Dispatch header ${name} does not match the signed envelope.`, "backend_protocol", 409)
      }
    }
    const contentType = String(request.headers["content-type"] ?? "").toLocaleLowerCase()
    if (!contentType.startsWith("application/json")) {
      throw new TerminalProtocolError("Dispatch content type must be application/json.", "backend_protocol", 415)
    }
  }
}

export const TERMINAL_CAPABILITIES = CAPABILITIES
export const TERMINAL_OPERATIONS = OPERATIONS
