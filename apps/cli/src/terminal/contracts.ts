import { createHash, timingSafeEqual } from "node:crypto"

export const TRANSPORT_REQUEST_SCHEMA = "zyra.backend-transport-request/v1" as const
export const TRANSPORT_RESPONSE_SCHEMA = "zyra.backend-transport-response/v1" as const
export const ACTION_SCHEMA = "zyra.terminal-action/v1" as const
export const ACTION_RESULT_SCHEMA = "zyra.terminal-action-result/v1" as const

export const FRAME_KINDS = Object.freeze([
  "accepted",
  "progress",
  "stdout",
  "stderr",
  "artifact",
  "heartbeat",
  "result",
  "error",
  "cancelled",
] as const)

export type FrameKind = typeof FRAME_KINDS[number]

export interface TerminalEnvelope {
  schema: string
  envelope_id: string
  run_id: string
  task_id: string
  turn_id: string
  runtime_worker: string
  backend_lease_id: string
  backend_id: string
  backend_kind: string
  backend_location: string
  workspace_root: string
  artifact_root: string
  provider_route_id: string
  provider_route_checksum: string
  provider_catalog_revision: number
  provider_credential_version: number
  provider_credential_fingerprint: string
  provider_transport_id: string
  m0_execution_ref: string
  idempotency_key: string
  deadline_at: number
  attempt: number
  created_at: number
  checksum: string
  [key: string]: unknown
}

export interface TerminalAction {
  schema: typeof ACTION_SCHEMA
  tool_name: string
  tool_call_id: string
  arguments: Record<string, unknown>
  metadata: Record<string, unknown>
  permission: Record<string, unknown>
}

export interface TerminalDispatchRequest {
  schema: typeof TRANSPORT_REQUEST_SCHEMA
  envelope: TerminalEnvelope
  operation: string
  payload: TerminalAction
  input_digest: string
  rawBytes: number
}

export interface TerminalFrame {
  sequence: number
  kind: FrameKind
  created_at: number
  payload: Record<string, unknown>
  output_observed: boolean
  digest: string
}

export interface TerminalActionResult {
  schema: typeof ACTION_RESULT_SCHEMA
  tool_call_id: string
  ok: boolean
  summary: string
  output: Record<string, unknown>
  error?: string
  metadata: Record<string, string>
}

export class TerminalProtocolError extends Error {
  readonly status: number
  readonly code: string
  readonly retryable: boolean
  readonly recoveryIntent: "none" | "change_backend" | "reconcile"

  constructor(
    message: string,
    code: string,
    status = 400,
    options: { retryable?: boolean; recoveryIntent?: "none" | "change_backend" | "reconcile"; cause?: unknown } = {},
  ) {
    super(message, { cause: options.cause })
    this.name = "TerminalProtocolError"
    this.status = status
    this.code = code
    this.retryable = options.retryable === true
    this.recoveryIntent = options.recoveryIntent ?? "none"
  }
}

function sorted(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sorted)
  if (!value || typeof value !== "object") return value
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, sorted(item)]),
  )
}

export function canonicalJson(value: unknown): string {
  return JSON.stringify(sorted(value))
}

export function checksum(value: unknown): string {
  return `sha256:${createHash("sha256").update(canonicalJson(value), "utf8").digest("hex")}`
}

export function checksumRaw(canonical: string): string {
  return `sha256:${createHash("sha256").update(canonical, "utf8").digest("hex")}`
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TerminalProtocolError(`${label} must be an object.`, "backend_protocol")
  }
  return value as Record<string, unknown>
}

function nonEmpty(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new TerminalProtocolError(`${label} must be a non-empty string.`, "backend_protocol")
  }
  return value
}

function digest(value: unknown, label: string): string {
  const selected = nonEmpty(value, label)
  if (!/^sha256:[a-f0-9]{64}$/.test(selected)) {
    throw new TerminalProtocolError(`${label} is invalid.`, "backend_protocol")
  }
  return selected
}

interface RawField {
  key: string
  raw: string
  value: string
}

function stringEnd(source: string, start: number): number {
  let escaped = false
  for (let index = start + 1; index < source.length; index += 1) {
    const character = source[index]
    if (escaped) escaped = false
    else if (character === "\\") escaped = true
    else if (character === "\"") return index + 1
  }
  throw new TerminalProtocolError("JSON string is not terminated.", "backend_protocol")
}

function valueEnd(source: string, start: number): number {
  if (source[start] === "\"") return stringEnd(source, start)
  if (source[start] !== "{" && source[start] !== "[") {
    let index = start
    while (index < source.length && source[index] !== "," && source[index] !== "}") index += 1
    return index
  }
  const open = source[start]
  const close = open === "{" ? "}" : "]"
  let depth = 0
  for (let index = start; index < source.length; index += 1) {
    if (source[index] === "\"") {
      index = stringEnd(source, index) - 1
      continue
    }
    if (source[index] === open) depth += 1
    else if (source[index] === close) {
      depth -= 1
      if (depth === 0) return index + 1
    }
  }
  throw new TerminalProtocolError("JSON container is not terminated.", "backend_protocol")
}

function objectFields(source: string): RawField[] {
  if (!source.startsWith("{") || !source.endsWith("}")) {
    throw new TerminalProtocolError("Canonical JSON object is invalid.", "backend_protocol")
  }
  const fields: RawField[] = []
  let index = 1
  while (index < source.length - 1) {
    if (source[index] !== "\"") {
      throw new TerminalProtocolError("Canonical JSON object contains invalid whitespace or keys.", "backend_protocol")
    }
    const keyEnd = stringEnd(source, index)
    const rawKey = source.slice(index, keyEnd)
    if (source[keyEnd] !== ":") {
      throw new TerminalProtocolError("Canonical JSON object field is invalid.", "backend_protocol")
    }
    const start = index
    const itemStart = keyEnd + 1
    const itemEnd = valueEnd(source, itemStart)
    fields.push({ key: JSON.parse(rawKey) as string, raw: source.slice(start, itemEnd), value: source.slice(itemStart, itemEnd) })
    index = itemEnd
    if (index === source.length - 1) break
    if (source[index] !== ",") {
      throw new TerminalProtocolError("Canonical JSON object separator is invalid.", "backend_protocol")
    }
    index += 1
  }
  return fields
}

function exactDigest(left: string, right: string): boolean {
  const a = Buffer.from(left, "utf8")
  const b = Buffer.from(right, "utf8")
  return a.length === b.length && timingSafeEqual(a, b)
}

export function parseDispatchRequest(raw: Buffer, maximumBytes: number): TerminalDispatchRequest {
  if (raw.byteLength === 0 || raw.byteLength > maximumBytes) {
    throw new TerminalProtocolError("Dispatch request body exceeds the configured budget.", "backend_protocol", 413)
  }
  const source = raw.toString("utf8")
  let value: Record<string, unknown>
  try {
    value = record(JSON.parse(source), "dispatch request")
  } catch (error) {
    if (error instanceof TerminalProtocolError) throw error
    throw new TerminalProtocolError("Dispatch request is not valid UTF-8 JSON.", "backend_protocol", 400, { cause: error })
  }
  if (value.schema !== TRANSPORT_REQUEST_SCHEMA) {
    throw new TerminalProtocolError("Dispatch request schema is unsupported.", "backend_protocol")
  }
  const fields = new Map(objectFields(source).map((field) => [field.key, field]))
  const envelopeField = fields.get("envelope")
  const operationField = fields.get("operation")
  const payloadField = fields.get("payload")
  if (!envelopeField || !operationField || !payloadField) {
    throw new TerminalProtocolError("Dispatch request omitted canonical fields.", "backend_protocol")
  }
  const envelope = record(value.envelope, "dispatch envelope") as TerminalEnvelope
  const payload = record(value.payload, "terminal action") as unknown as TerminalAction
  const operation = nonEmpty(value.operation, "dispatch operation")
  const inputDigest = digest(value.input_digest, "dispatch input digest")
  const envelopeDigest = digest(envelope.checksum, "dispatch envelope checksum")
  const envelopeFields = objectFields(envelopeField.value)
  const unsignedEnvelope = `{${envelopeFields.filter((field) => field.key !== "checksum").map((field) => field.raw).join(",")}}`
  if (!exactDigest(checksumRaw(unsignedEnvelope), envelopeDigest)) {
    throw new TerminalProtocolError("Dispatch envelope checksum does not match.", "backend_protocol")
  }
  const inputCanonical = `{"m0_execution_ref":${JSON.stringify(nonEmpty(envelope.m0_execution_ref, "m0 execution ref"))},"operation":${operationField.value},"payload":${payloadField.value}}`
  if (!exactDigest(checksumRaw(inputCanonical), inputDigest)) {
    throw new TerminalProtocolError("Dispatch input digest does not match.", "backend_protocol")
  }
  if (payload.schema !== ACTION_SCHEMA || operation !== `tool.${payload.tool_name}`) {
    throw new TerminalProtocolError("Terminal action binding is invalid.", "backend_protocol")
  }
  nonEmpty(payload.tool_call_id, "tool call id")
  payload.arguments = record(payload.arguments, "terminal action arguments")
  payload.metadata = record(payload.metadata, "terminal action metadata")
  payload.permission = record(payload.permission, "terminal permission receipt")
  return { schema: TRANSPORT_REQUEST_SCHEMA, envelope, operation, payload, input_digest: inputDigest, rawBytes: raw.byteLength }
}

export function createFrame(
  sequence: number,
  kind: FrameKind,
  payload: Record<string, unknown>,
  outputObserved: boolean,
): TerminalFrame {
  const body = {
    sequence,
    kind,
    created_at: Date.now() / 1000,
    payload,
    output_observed: outputObserved,
  }
  return { ...body, digest: checksum(body) }
}
